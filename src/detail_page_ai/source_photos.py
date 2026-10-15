import functools
import hashlib
import io
import logging
import math
import os
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from threading import Lock
from typing import Protocol

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, UnidentifiedImageError

from .assets import MemoryAssetStore
from .dto import GenerationOptions, ProductProfileDto
from .models import (
    FidelityStatus,
    PhotoGenerationFailure,
    PhotoTransform,
    ProductPhoto,
    ProductPhotoSet,
)
from .ports import SourceAssetStore


logger = logging.getLogger(__name__)
_PHOTO_GENERATION_FAILURE_REASON = "생성하지 못했습니다"


def _record_photo_generation_failure(
    failures: list[PhotoGenerationFailure], photo_id: str, exc: Exception
) -> None:
    reason = _PHOTO_GENERATION_FAILURE_REASON
    failures.append(PhotoGenerationFailure(photo_id=photo_id, reason=reason))
    logger.warning(
        "photo generation failed: photo_id=%s reason=%s error=%s",
        photo_id,
        reason,
        exc,
        exc_info=True,
    )


class BackgroundGenerator(Protocol):
    def generate(
        self,
        *,
        profile: ProductProfileDto,
        role: str,
        width: int,
        height: int,
    ) -> bytes:
        ...


class UsageSceneGenerator(Protocol):
    """Generate a finished lifestyle scene for the explicitly allowed lifestyle slot."""

    def generate(
        self,
        *,
        profile: ProductProfileDto,
        role: str,
        source_image: bytes,
        source_mime_type: str,
        width: int,
        height: int,
        direction: str | None = None,
    ) -> bytes:
        ...


class DetailViewGenerator(Protocol):
    """Generate only the explicitly allowed alternative-angle detail slots."""

    def generate(
        self,
        *,
        profile: ProductProfileDto,
        role: str,
        source_image: bytes,
        source_mime_type: str,
        width: int,
        height: int,
        direction: str | None = None,
    ) -> bytes:
        ...


@dataclass(frozen=True, slots=True)
class ProductCutout:
    rgba_png: bytes
    mask_png: bytes
    bbox: tuple[int, int, int, int]
    width: int
    height: int
    source_sha256: str
    cutout_sha256: str
    mask_sha256: str


class CutoutExtractor(Protocol):
    def extract(self, source_image: bytes, source_mime_type: str) -> ProductCutout | None:
        ...


class RembgSessionFactory(Protocol):
    def __call__(self, model_name: str) -> object:
        ...


class RembgSegmenter(Protocol):
    def __call__(self, data: bytes, *, session: object, only_mask: bool) -> bytes:
        ...


def _create_rembg_session(model_name: str) -> object:
    """Create a rembg session on the CPU, or on CUDA when REMBG_USE_CUDA=1."""
    import onnxruntime
    from rembg import new_session

    available_providers = list(onnxruntime.get_available_providers())
    # Stage 2026-10-01: on the L40S shared with both SGLang servers, BiRefNet's
    # 1024x1024 activations did not fit (an 822 MB arena request failed with
    # 5.7 GiB free) while the pod used 9.3 of its 24 GiB RAM. The CPU path peaks
    # near 13 GiB RSS and takes about a minute per cutout on 4 vCPUs.
    use_cuda = (
        os.environ.get("REMBG_USE_CUDA") == "1"
        and "CUDAExecutionProvider" in available_providers
    )
    # On a GPU of its own: grow the arena only by what is requested and skip
    # cuDNN's exhaustive search, which allocates large trial workspaces.
    cuda_provider = (
        "CUDAExecutionProvider",
        {"arena_extend_strategy": "kSameAsRequested", "cudnn_conv_algo_search": "HEURISTIC"},
    )
    providers = [cuda_provider, "CPUExecutionProvider"] if use_cuda else ["CPUExecutionProvider"]
    logger.info(
        "rembg ONNX Runtime providers: available=%s selected=%s",
        available_providers,
        providers,
    )
    return new_session(model_name, providers=providers)


def _encode_png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


def _decode_rgb(data: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("source image could not be decoded") from exc
    return image.convert("RGB")


class SolidBackgroundCutoutExtractor:
    """Legacy heuristic retained for training augmentation and visual comparisons.

    Detail-page generation uses :class:`RembgCutoutExtractor`; this deterministic
    extractor remains the comparison baseline and keeps augmentation reproducible.
    """

    def __init__(
        self,
        *,
        background_tolerance: float = 28.0,
        corner_uniformity_tolerance: float = 24.0,
        min_foreground_ratio: float = 0.01,
        max_foreground_ratio: float = 0.90,
        min_largest_component_ratio: float = 0.50,
    ):
        self.background_tolerance = background_tolerance
        self.corner_uniformity_tolerance = corner_uniformity_tolerance
        self.min_foreground_ratio = min_foreground_ratio
        self.max_foreground_ratio = max_foreground_ratio
        # A genuine product silhouette should have a connected core.  Requiring
        # half of the foreground to share one component rejects scattered
        # pattern/noise fragments while still allowing small detached details.
        self.min_largest_component_ratio = min_largest_component_ratio

    def extract(self, source_image: bytes, source_mime_type: str) -> ProductCutout | None:
        del source_mime_type
        source = _decode_rgb(source_image)
        width, height = source.size
        corners = (
            source.getpixel((0, 0)),
            source.getpixel((width - 1, 0)),
            source.getpixel((0, height - 1)),
            source.getpixel((width - 1, height - 1)),
        )
        background = tuple(round(sum(pixel[channel] for pixel in corners) / 4) for channel in range(3))
        if any(self._distance(pixel, background) > self.corner_uniformity_tolerance for pixel in corners):
            return None

        alpha_values = None
        for multiplier in (1, 2, 4):
            tolerance = self.background_tolerance * multiplier
            candidate_mask = self._connected_foreground_mask(
                source, background, tolerance
            )
            alpha_values = list(candidate_mask.get_flattened_data())
            foreground_ratio = sum(value > 0 for value in alpha_values) / (width * height)
            # A higher tolerance is useful only when the initial foreground is
            # implausibly large.  Frame contact alone must never trigger it:
            # connected foreground can be a valid product boundary.
            if foreground_ratio <= self.max_foreground_ratio:
                break
        if alpha_values is None:
            return None
        mask = Image.new("L", source.size)
        mask.putdata(alpha_values)
        mask = self._refine_mask(mask)
        mask = self._suppress_unreliable_edge_shadow(mask, source, background)
        mask = self._suppress_connected_background_gradient(mask, source, background)
        bbox = mask.getbbox()
        if bbox is None:
            return None
        foreground_count = sum(value > 0 for value in mask.get_flattened_data())
        foreground_ratio = foreground_count / (width * height)
        if not self.min_foreground_ratio <= foreground_ratio <= self.max_foreground_ratio:
            return None
        if (
            self._largest_foreground_component_ratio(mask, foreground_count)
            < self.min_largest_component_ratio
        ):
            return None

        red, green, blue = source.split()
        rgba = Image.merge("RGBA", (red, green, blue, mask))
        rgba_png = _encode_png(rgba)
        mask_png = _encode_png(mask)
        return ProductCutout(
            rgba_png=rgba_png,
            mask_png=mask_png,
            bbox=bbox,
            width=width,
            height=height,
            source_sha256=hashlib.sha256(source_image).hexdigest(),
            cutout_sha256=hashlib.sha256(rgba_png).hexdigest(),
            mask_sha256=hashlib.sha256(mask_png).hexdigest(),
        )

    @staticmethod
    def _refine_mask(mask: Image.Image) -> Image.Image:
        """Remove a one-pixel light halo while keeping source RGB untouched."""
        eroded = mask.filter(ImageFilter.MinFilter(3))
        softened = eroded.filter(ImageFilter.GaussianBlur(0.6))
        # Do not let the blur create alpha outside the original foreground mask.
        return ImageChops.multiply(softened, mask)

    @classmethod
    def _connected_foreground_mask(
        cls,
        source: Image.Image,
        background: tuple[int, int, int],
        tolerance: float,
    ) -> Image.Image:
        """Return foreground after flooding only edge-connected background candidates."""
        width, height = source.size
        source_pixels = source.load()
        background_candidates = bytearray(width * height)
        for y in range(height):
            row_start = y * width
            for x in range(width):
                background_candidates[row_start + x] = int(
                    cls._distance(source_pixels[x, y], background) <= tolerance
                )

        reachable_background = bytearray(width * height)
        pending: deque[tuple[int, int]] = deque()

        def enqueue_if_background(x: int, y: int) -> None:
            index = y * width + x
            if background_candidates[index] and not reachable_background[index]:
                reachable_background[index] = 1
                pending.append((x, y))

        for x in range(width):
            enqueue_if_background(x, 0)
            enqueue_if_background(x, height - 1)
        for y in range(height):
            enqueue_if_background(0, y)
            enqueue_if_background(width - 1, y)

        while pending:
            x, y = pending.popleft()
            for neighbor_x, neighbor_y in (
                (x - 1, y),
                (x + 1, y),
                (x, y - 1),
                (x, y + 1),
            ):
                if 0 <= neighbor_x < width and 0 <= neighbor_y < height:
                    enqueue_if_background(neighbor_x, neighbor_y)

        mask = Image.new("L", source.size)
        mask.putdata(
            [
                0 if reachable_background[index] else 255
                for index in range(width * height)
            ]
        )
        return mask

    def _suppress_unreliable_edge_shadow(
        self,
        mask: Image.Image,
        source: Image.Image,
        background: tuple[int, int, int],
    ) -> Image.Image:
        """Suppress a tolerant edge shadow only when its own mask is trustworthy."""
        # Keep the legacy low-contrast edge-shadow behavior without using bbox
        # contact as an escalation signal.  The wider border flood is accepted
        # only when it still has a substantial, connected foreground core.
        tolerant_mask = self._connected_foreground_mask(
            source, background, self.background_tolerance * 4
        )
        tolerant_mask = self._refine_mask(tolerant_mask)
        tolerant_foreground_count = sum(value > 0 for value in tolerant_mask.get_flattened_data())
        if tolerant_foreground_count == 0:
            return mask
        tolerant_ratio = tolerant_foreground_count / (source.width * source.height)
        if not self.min_foreground_ratio <= tolerant_ratio <= self.max_foreground_ratio:
            return mask
        if (
            self._largest_foreground_component_ratio(
                tolerant_mask, tolerant_foreground_count
            )
            < self.min_largest_component_ratio
        ):
            return mask
        return ImageChops.multiply(mask, tolerant_mask)

    def _suppress_connected_background_gradient(
        self,
        mask: Image.Image,
        source: Image.Image,
        background: tuple[int, int, int],
    ) -> Image.Image:
        """Remove only a neutral, brighter gradient that reaches the frame."""
        width, height = source.size
        source_pixels = source.load()
        background_brightness = sum(background) / 3
        candidates = bytearray(width * height)
        clearable = bytearray(width * height)
        fadeable = bytearray(width * height)
        for y in range(height):
            row_start = y * width
            for x in range(width):
                pixel = source_pixels[x, y]
                brightness = sum(pixel) / 3
                chroma = max(pixel) - min(pixel)
                neutral = chroma <= self.corner_uniformity_tolerance
                # Let the flood cross the gradual gradient, but only clear
                # pixels that are unambiguously brighter than the background.
                candidates[row_start + x] = int(
                    neutral and brightness >= background_brightness - 10
                )
                clearable[row_start + x] = int(
                    neutral and brightness >= background_brightness + 60
                )
                fadeable[row_start + x] = int(
                    neutral and brightness >= background_brightness + 30
                )

        reachable_background = bytearray(width * height)
        pending: deque[tuple[int, int]] = deque()

        def enqueue_if_candidate(x: int, y: int) -> None:
            index = y * width + x
            if candidates[index] and not reachable_background[index]:
                reachable_background[index] = 1
                pending.append((x, y))

        for x in range(width):
            enqueue_if_candidate(x, 0)
            enqueue_if_candidate(x, height - 1)
        for y in range(height):
            enqueue_if_candidate(0, y)
            enqueue_if_candidate(width - 1, y)

        while pending:
            x, y = pending.popleft()
            for neighbor_x, neighbor_y in (
                (x - 1, y),
                (x + 1, y),
                (x, y - 1),
                (x, y + 1),
            ):
                if 0 <= neighbor_x < width and 0 <= neighbor_y < height:
                    enqueue_if_candidate(neighbor_x, neighbor_y)

        values = list(mask.get_flattened_data())
        shadow_fade_alpha = 40
        for index in range(width * height):
            if not reachable_background[index] or not values[index]:
                continue
            if clearable[index]:
                values[index] = 0
            elif fadeable[index]:
                # Keep ambiguous product-colored pixels intact, but make the
                # connected neutral shadow nearly disappear on a new backdrop.
                values[index] = min(values[index], shadow_fade_alpha)
        mask.putdata(values)
        return mask

    @staticmethod
    def _largest_foreground_component_ratio(
        mask: Image.Image, foreground_count: int
    ) -> float:
        if foreground_count == 0:
            return 0.0
        width, height = mask.size
        pixels = mask.load()
        visited = bytearray(width * height)
        largest_component = 0
        for y in range(height):
            for x in range(width):
                index = y * width + x
                if visited[index] or pixels[x, y] == 0:
                    continue
                visited[index] = 1
                pending: deque[int] = deque((index,))
                component_size = 0
                while pending:
                    current = pending.popleft()
                    component_size += 1
                    current_x = current % width
                    current_y = current // width
                    for neighbor_x, neighbor_y in (
                        (current_x - 1, current_y),
                        (current_x + 1, current_y),
                        (current_x, current_y - 1),
                        (current_x, current_y + 1),
                    ):
                        if 0 <= neighbor_x < width and 0 <= neighbor_y < height:
                            neighbor = neighbor_y * width + neighbor_x
                            if (
                                not visited[neighbor]
                                and pixels[neighbor_x, neighbor_y] > 0
                            ):
                                visited[neighbor] = 1
                                pending.append(neighbor)
                largest_component = max(largest_component, component_size)
        return largest_component / foreground_count

    @staticmethod
    def _distance(left: tuple[int, int, int], right: tuple[int, int, int]) -> float:
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)))


class RembgCutoutExtractor:
    """Build a source-RGB cutout from a reusable rembg segmentation session."""

    # BiRefNet-General evaluates at 1024px and was chosen over U2Net/ISNet for
    # its finer boundary preservation on translucent petals and glossy nacre.
    model_name = "birefnet-general"
    cached_cutouts = 8

    def __init__(
        self,
        *,
        session: object | None = None,
        session_factory: RembgSessionFactory | None = None,
        segmenter: RembgSegmenter | None = None,
        min_foreground_ratio: float = 0.005,
        max_foreground_ratio: float = 0.995,
        visible_alpha_threshold: int = 8,
    ) -> None:
        if not 0 <= min_foreground_ratio < max_foreground_ratio <= 1:
            raise ValueError("foreground ratios must satisfy 0 <= min < max <= 1")
        if not 0 <= visible_alpha_threshold <= 255:
            raise ValueError("visible_alpha_threshold must be between 0 and 255")

        self._session = session
        self._session_factory = session_factory
        self._segmenter = segmenter
        self._session_lock = Lock()
        self._cutouts: OrderedDict[str, ProductCutout] = OrderedDict()
        self._cutouts_lock = Lock()
        self.min_foreground_ratio = min_foreground_ratio
        self.max_foreground_ratio = max_foreground_ratio
        self.visible_alpha_threshold = visible_alpha_threshold

    def extract(self, source_image: bytes, source_mime_type: str) -> ProductCutout | None:
        # The photo generator and the fidelity validator ask for the same
        # source's cutout several times per render, and on Stage's CPU each
        # BiRefNet pass takes tens of seconds. The cutout depends only on the
        # source bytes, so reuse successful ones; failures are retried.
        key = hashlib.sha256(source_image).hexdigest()
        with self._cutouts_lock:
            cached = self._cutouts.get(key)
            if cached is not None:
                self._cutouts.move_to_end(key)
                return cached
        cutout = self._extract_uncached(source_image, source_mime_type)
        if cutout is not None:
            with self._cutouts_lock:
                self._cutouts[key] = cutout
                while len(self._cutouts) > self.cached_cutouts:
                    self._cutouts.popitem(last=False)
        return cutout

    def _extract_uncached(self, source_image: bytes, source_mime_type: str) -> ProductCutout | None:
        del source_mime_type
        try:
            source = _decode_rgb(source_image)
            session = self._session_for_extraction()
            mask_png = self._segmenter(
                source_image,
                session=session,
                only_mask=True,
            )
            mask = self._decode_mask(mask_png, source.size)
        except (OSError, RuntimeError, ValueError):
            return None

        # Low, isolated alpha values are model noise rather than a visible
        # product edge.  Preserve normal soft edges while excluding that noise
        # from both the bbox and the empty/full safety check.
        mask = mask.point(
            lambda alpha: alpha if alpha >= self.visible_alpha_threshold else 0
        )
        foreground_count = sum(value > 0 for value in mask.get_flattened_data())
        foreground_ratio = foreground_count / (source.width * source.height)
        if not self.min_foreground_ratio <= foreground_ratio <= self.max_foreground_ratio:
            return None
        bbox = mask.getbbox()
        if bbox is None:
            return None

        red, green, blue = source.split()
        rgba = Image.merge("RGBA", (red, green, blue, mask))
        rgba_png = _encode_png(rgba)
        mask_png = _encode_png(mask)
        return ProductCutout(
            rgba_png=rgba_png,
            mask_png=mask_png,
            bbox=bbox,
            width=source.width,
            height=source.height,
            source_sha256=hashlib.sha256(source_image).hexdigest(),
            cutout_sha256=hashlib.sha256(rgba_png).hexdigest(),
            mask_sha256=hashlib.sha256(mask_png).hexdigest(),
        )

    def _session_for_extraction(self) -> object:
        with self._session_lock:
            if self._session is None:
                self._load_default_components()
                assert self._session_factory is not None
                self._session = self._session_factory(self.model_name)
            if self._segmenter is None:
                self._load_default_components()
            return self._session

    def _load_default_components(self) -> None:
        """Load rembg only for an extraction that needs an uninjected component."""
        if self._session_factory is not None and self._segmenter is not None:
            return
        # Creating the session remains lazy: the first real extraction populates
        # rembg's ~/.u2net/birefnet-general.onnx cache (rembg 2.0.69), while later
        # extracts reuse this same session object.
        from rembg import remove

        self._session_factory = self._session_factory or _create_rembg_session
        self._segmenter = self._segmenter or remove

    @staticmethod
    def _decode_mask(data: bytes, expected_size: tuple[int, int]) -> Image.Image:
        if not isinstance(data, bytes):
            raise ValueError("rembg mask must be PNG bytes")
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError("rembg mask could not be decoded") from exc
        if image.size != expected_size:
            raise ValueError("rembg mask dimensions differ from the source image")
        return image.convert("L")


@functools.cache
def _shared_rembg_extractor() -> RembgCutoutExtractor:
    """One BiRefNet session per process for the generator and the validator.

    On the CPU each session keeps several GiB: measured with two sessions the
    process grew to 28.7 GiB over two jobs (past the Stage pod's 24 GiB limit),
    and with one shared session it stayed at 15.8 GiB.
    """
    return RembgCutoutExtractor()


class ProductFidelityValidator:
    def __init__(
        self, extractor: CutoutExtractor | None = None
    ) -> None:
        self.extractor = extractor or _shared_rembg_extractor()

    def validate(
        self,
        photo: ProductPhoto,
        *,
        source_images: tuple[tuple[bytes, str], ...] = (),
    ) -> FidelityStatus:
        if photo.asset_mode in {"generated_scene", "generated_view"}:
            role_is_allowed = (
                photo.asset_mode == "generated_scene"
                and photo.photo_id in {"lifestyle", "lifestyle-02"}
            ) or (
                photo.asset_mode == "generated_view"
                and photo.photo_id in {"detail-02", "detail-03", "detail-04", "detail-05"}
            )
            if (
                not role_is_allowed
                or not photo.product_generated
                or not photo.background_generated
                or photo.fidelity_status != "GENERATED"
                or not photo.source_sha256
                or not source_images
            ):
                return "REJECTED"
            source_hashes = {
                hashlib.sha256(data).hexdigest() for data, _ in source_images
            }
            return "GENERATED" if photo.source_sha256 in source_hashes else "REJECTED"
        if photo.product_generated:
            return "REJECTED"
        if photo.asset_mode not in {
            "source",
            "source_original",
            "source_crop",
            "source_composite",
        }:
            return "REJECTED"
        if (
            photo.asset_mode == "source_original"
            and (photo.photo_id != "hero" or photo.fidelity_status != "VERIFIED")
        ):
            return "REJECTED"
        if not photo.source_sha256:
            return "REJECTED"
        if photo.asset_mode == "source_crop" and photo.transform.crop is None:
            return "REJECTED"
        if photo.asset_mode == "source_composite" and (
            not photo.cutout_sha256 or not photo.mask_sha256
        ):
            return "REJECTED"
        if photo.fidelity_status == "REJECTED" or not source_images:
            return "REJECTED"

        source_by_hash = {
            hashlib.sha256(data).hexdigest(): (data, mime_type)
            for data, mime_type in source_images
        }
        source_record = source_by_hash.get(photo.source_sha256)
        if source_record is None:
            return "REJECTED"
        source_data, source_mime_type = source_record

        if photo.asset_mode in {"source", "source_original"}:
            if hashlib.sha256(photo.data).hexdigest() != photo.source_sha256:
                return "REJECTED"
            return photo.fidelity_status

        try:
            source = _decode_rgb(source_data)
            rendered = _decode_rgb(photo.data)
        except ValueError:
            return "REJECTED"

        if photo.asset_mode == "source_crop":
            crop = photo.transform.crop
            if crop is None or not self._crop_in_bounds(crop, source.size):
                return "REJECTED"
            expected = source.crop(crop)
            if rendered.size != expected.size or rendered.tobytes() != expected.tobytes():
                return "REJECTED"
            return photo.fidelity_status

        cutout = self.extractor.extract(source_data, source_mime_type)
        if cutout is None:
            return "REJECTED"
        if (
            photo.source_sha256 != cutout.source_sha256
            or photo.cutout_sha256 != cutout.cutout_sha256
            or photo.mask_sha256 != cutout.mask_sha256
            or photo.transform.crop != cutout.bbox
            or photo.transform.scale <= 0
        ):
            return "REJECTED"

        rgba = Image.open(io.BytesIO(cutout.rgba_png)).convert("RGBA")
        visible = rgba.crop(cutout.bbox)
        resized_size = (
            max(1, round(visible.width * photo.transform.scale)),
            max(1, round(visible.height * photo.transform.scale)),
        )
        resized = visible.resize(resized_size, Image.Resampling.LANCZOS)
        expected_x = (rendered.width - resized.width) // 2
        expected_y = (
            rendered.height
            - resized.height
            - round(rendered.height * 0.14)
            if photo.photo_id in {"lifestyle", "scale"}
            else (rendered.height - resized.height) // 2
        )
        if (photo.transform.x, photo.transform.y) != (expected_x, expected_y):
            return "REJECTED"
        if (
            expected_x < 0
            or expected_y < 0
            or expected_x + resized.width > rendered.width
            or expected_y + resized.height > rendered.height
        ):
            return "REJECTED"

        alpha = resized.getchannel("A")
        opaque_points = 0
        for y in range(resized.height):
            for x in range(resized.width):
                if alpha.getpixel((x, y)) == 255:
                    opaque_points += 1
                    if rendered.getpixel((expected_x + x, expected_y + y)) != resized.getpixel(
                        (x, y)
                    )[:3]:
                        return "REJECTED"
        if opaque_points == 0:
            return "REJECTED"
        return photo.fidelity_status

    @staticmethod
    def _crop_in_bounds(
        crop: tuple[int, int, int, int], size: tuple[int, int]
    ) -> bool:
        left, top, right, bottom = crop
        width, height = size
        return 0 <= left < right <= width and 0 <= top < bottom <= height


class SourcePreservingProductPhotoGenerator:
    _layout_roles = ("hero", "packshot", "detail", "lifestyle")
    _labels = {
        "hero": "원본 보존 대표 이미지",
        "packshot": "원본 보존 팩샷",
        "detail": "원본 디테일 크롭",
        "lifestyle": "원본 제품 활용 장면",
        "lifestyle-02": "AI 생성 활용 장면 추가",
        "scale": "원본 제품 크기 비교",
        "alternate": "추가 원본 구도",
    }

    def __init__(
        self,
        *,
        asset_store: SourceAssetStore | None = None,
        extractor: CutoutExtractor | None = None,
        background_generator: BackgroundGenerator | None = None,
        usage_scene_generator: UsageSceneGenerator | None = None,
        detail_view_generator: DetailViewGenerator | None = None,
        validator: ProductFidelityValidator | None = None,
        canvas_size: tuple[int, int] = (1200, 1200),
        include_scale: bool = False,
        max_generated_photos: int = 5,
        photo_roles: tuple[str, ...] | None = None,
    ):
        if not 0 <= max_generated_photos <= 12:
            raise ValueError("max_generated_photos must be between 0 and 12")
        self.asset_store = asset_store or MemoryAssetStore()
        self.extractor = extractor or _shared_rembg_extractor()
        self.background_generator = background_generator
        self.usage_scene_generator = usage_scene_generator
        self.detail_view_generator = detail_view_generator
        self.validator = validator or ProductFidelityValidator(extractor=self.extractor)
        self.canvas_size = canvas_size
        self.include_scale = include_scale
        self.max_generated_photos = max_generated_photos
        requested_roles = tuple(photo_roles or self._layout_roles)
        allowed_roles = set(self._layout_roles) | {"scale"}
        if not requested_roles or any(role not in allowed_roles for role in requested_roles):
            raise ValueError("photo_roles contains an unsupported role")
        self.photo_roles = requested_roles

    def generate(
        self,
        *,
        source_image: bytes,
        source_mime_type: str,
        profile: ProductProfileDto,
        options: GenerationOptions,
        additional_source_images: tuple[tuple[bytes, str], ...] = (),
    ) -> ProductPhotoSet:
        del options
        source_images = ((source_image, source_mime_type),) + additional_source_images
        roles = list(self.photo_roles)
        if self.include_scale and "scale" not in roles:
            roles.append("scale")
        preserve_arrangement = self._preserve_arrangement(profile)
        provided_count = min(len(source_images), len(roles))
        provided_roles = tuple(roles[:provided_count])
        missing_roles = tuple(roles[provided_count:])
        provided_photos = [
            self._source_original(
                photo_id=role,
                order=index,
                data=data,
                mime_type=mime_type,
                label=self._labels[role],
            )
            for index, (role, (data, mime_type)) in enumerate(
                zip(roles, source_images), start=1
            )
        ]

        source_record = self.asset_store.put(source_image, source_mime_type, "source")
        source_rgb = _decode_rgb(source_image)
        cutout = self.extractor.extract(source_image, source_mime_type)
        reference_args = dict(
            profile=profile,
            source_asset_id=source_record.asset_id,
            source_sha256=source_record.sha256,
            source_image=source_image,
            source_mime_type=source_mime_type,
        )
        photos = list(provided_photos)
        generated_photo_count = 0
        photo_generation_failures: list[PhotoGenerationFailure] = []

        def generation_available() -> bool:
            return generated_photo_count < self.max_generated_photos

        def background_for(role: str) -> tuple[Image.Image, bool]:
            if self.max_generated_photos == 0:
                return self._solid_background("#E9E4DC"), False
            return self._background_for(
                profile=profile,
                role=role,
                photo_generation_failures=photo_generation_failures,
            )

        if cutout is None:
            for role in missing_roles:
                order = len(photos) + 1
                photos.append(
                    self._source_hero(
                        order=order,
                        source_image=source_image,
                        source_mime_type=source_mime_type,
                        source_asset_id=source_record.asset_id,
                        source_sha256=source_record.sha256,
                        size=source_rgb.size,
                    )
                    if role == "hero"
                    else self._source_fallback(
                        role=role,
                        order=order,
                        source_image=source_image,
                        source_mime_type=source_mime_type,
                        source_asset_id=source_record.asset_id,
                        source_sha256=source_record.sha256,
                        size=source_rgb.size,
                    )
                )
            if "lifestyle" in missing_roles:
                index = next(
                    index
                    for index, photo in enumerate(photos)
                    if photo.photo_id == "lifestyle"
                )
                scene = (
                    self._generated_usage_scene(
                        order=photos[index].order,
                        photo_generation_failures=photo_generation_failures,
                        **reference_args,
                    )
                    if generation_available()
                    else None
                )
                if scene is not None:
                    photos[index] = scene
                    generated_photo_count += 1
        else:
            if "hero" in missing_roles:
                photos.append(
                    self._source_hero(
                        order=len(photos) + 1,
                        source_image=source_image,
                        source_mime_type=source_mime_type,
                        source_asset_id=source_record.asset_id,
                        source_sha256=source_record.sha256,
                        size=source_rgb.size,
                    )
                )
            if "packshot" in missing_roles:
                if preserve_arrangement:
                    photos.append(
                        self._source_fallback(
                            role="packshot",
                            order=len(photos) + 1,
                            source_image=source_image,
                            source_mime_type=source_mime_type,
                            source_asset_id=source_record.asset_id,
                            source_sha256=source_record.sha256,
                            size=source_rgb.size,
                        )
                    )
                else:
                    photos.append(
                        self._composite(
                            role="packshot",
                            order=len(photos) + 1,
                            cutout=cutout,
                            source_asset_id=source_record.asset_id,
                            background=self._solid_background("#FFFFFF"),
                            background_generated=False,
                        )
                    )
            detail_crops = []
            if "detail" in missing_roles:
                detail_crops = self._detail_crops(
                    order=len(photos) + 1,
                    source=source_rgb,
                    cutout=cutout,
                    source_asset_id=source_record.asset_id,
                )
                photos.append(detail_crops[0])
            if "lifestyle" in missing_roles:
                generated_scene = (
                    self._generated_usage_scene(
                        profile=profile,
                        order=len(photos) + 1,
                        source_asset_id=source_record.asset_id,
                        source_sha256=source_record.sha256,
                        source_image=source_image,
                        source_mime_type=source_mime_type,
                        photo_generation_failures=photo_generation_failures,
                    )
                    if generation_available()
                    else None
                )
                if generated_scene is not None:
                    photos.append(generated_scene)
                    generated_photo_count += 1
                else:
                    lifestyle_background, generated = background_for("lifestyle")
                    photos.append(
                        self._composite(
                            role="lifestyle",
                            order=len(photos) + 1,
                            cutout=cutout,
                            source_asset_id=source_record.asset_id,
                            background=lifestyle_background,
                            background_generated=generated,
                        )
                    )
            if "scale" in missing_roles:
                if preserve_arrangement:
                    photos.append(
                        self._source_fallback(
                            role="scale",
                            order=len(photos) + 1,
                            source_image=source_image,
                            source_mime_type=source_mime_type,
                            source_asset_id=source_record.asset_id,
                            source_sha256=source_record.sha256,
                            size=source_rgb.size,
                        )
                    )
                else:
                    scale_background, scale_generated = background_for("scale")
                    photos.append(
                        self._composite(
                            role="scale",
                            order=len(photos) + 1,
                            cutout=cutout,
                            source_asset_id=source_record.asset_id,
                            background=scale_background,
                            background_generated=scale_generated,
                        )
                    )

        for index, (alternate_data, alternate_mime) in enumerate(
            source_images[len(roles) :], start=1
        ):
            photos.append(
                self._source_original(
                    photo_id="alternate" if index == 1 else f"alternate-{index:02d}",
                    order=len(photos) + 1,
                    data=alternate_data,
                    mime_type=alternate_mime,
                    label=self._labels["alternate"],
                )
            )

        if "lifestyle" in provided_roles:
            generated_scene = (
                self._generated_usage_scene(
                    order=len(photos) + 1,
                    photo_id="lifestyle-02",
                    photo_generation_failures=photo_generation_failures,
                    **reference_args,
                )
                if generation_available()
                else None
            )
            if generated_scene is not None:
                photos.append(generated_scene)
                generated_photo_count += 1

        if "detail" in provided_roles:
            for role in ("detail-02", "detail-03", "detail-04", "detail-05"):
                if not generation_available():
                    break
                generated_view = self._generated_detail_view(
                    role=role,
                    order=len(photos) + 1,
                    photo_generation_failures=photo_generation_failures,
                    **reference_args,
                )
                if generated_view is not None:
                    photos.append(generated_view)
                    generated_photo_count += 1
        elif "detail" in missing_roles:
            if cutout is None:
                for role in ("detail-02", "detail-03", "detail-04", "detail-05"):
                    if not generation_available():
                        break
                    generated_view = self._generated_detail_view(
                        role=role,
                        order=len(photos) + 1,
                        photo_generation_failures=photo_generation_failures,
                        **reference_args,
                    )
                    if generated_view is not None:
                        photos.append(generated_view)
                        generated_photo_count += 1
            else:
                for detail_photo in detail_crops[1:]:
                    order = len(photos) + 1
                    generated_view = (
                        self._generated_detail_view(
                            role=detail_photo.photo_id,
                            order=order,
                            photo_generation_failures=photo_generation_failures,
                            **reference_args,
                        )
                        if generation_available()
                        else None
                    )
                    if generated_view is not None:
                        photos.append(generated_view)
                        generated_photo_count += 1
                    else:
                        photos.append(replace(detail_photo, order=order))

        safe_photos = []
        for photo in photos:
            status = self.validator.validate(photo, source_images=source_images)
            if status != "REJECTED":
                safe_photos.append(photo)
        return ProductPhotoSet(
            photos=tuple(safe_photos),
            photo_generation_failures=tuple(photo_generation_failures),
        )

    def regenerate_role(
        self,
        *,
        photo: ProductPhoto,
        source_image: bytes,
        source_mime_type: str,
        profile: ProductProfileDto,
        direction: str | None = None,
    ) -> tuple[ProductPhoto | None, tuple[PhotoGenerationFailure, ...]]:
        """Generate one AI photo again in the same slot.

        Source-preserved photos are refused: their pixels must never be redrawn.
        """
        if photo.asset_mode not in {"generated_scene", "generated_view"}:
            raise ValueError("only AI-generated photos can be regenerated")
        source_record = self.asset_store.put(source_image, source_mime_type, "source")
        failures: list[PhotoGenerationFailure] = []
        arguments = dict(
            profile=profile,
            order=photo.order,
            source_asset_id=source_record.asset_id,
            source_sha256=source_record.sha256,
            source_image=source_image,
            source_mime_type=source_mime_type,
            photo_generation_failures=failures,
            direction=direction,
        )
        if photo.asset_mode == "generated_scene":
            regenerated = self._generated_usage_scene(photo_id=photo.photo_id, **arguments)
        else:
            regenerated = self._generated_detail_view(role=photo.photo_id, **arguments)
        return regenerated, tuple(failures)

    def _generated_usage_scene(
        self,
        *,
        profile: ProductProfileDto,
        order: int,
        photo_id: str = "lifestyle",
        source_asset_id: str,
        source_sha256: str,
        source_image: bytes,
        source_mime_type: str,
        photo_generation_failures: list[PhotoGenerationFailure],
        direction: str | None = None,
    ) -> ProductPhoto | None:
        if self.usage_scene_generator is None:
            return None
        try:
            data = self.usage_scene_generator.generate(
                profile=profile,
                role="lifestyle",
                source_image=source_image,
                source_mime_type=source_mime_type,
                width=self.canvas_size[0],
                height=self.canvas_size[1],
                **({"direction": direction} if direction else {}),
            )
            image = _decode_rgb(data)
        except (RuntimeError, ValueError, OSError) as exc:
            _record_photo_generation_failure(
                photo_generation_failures,
                photo_id,
                exc,
            )
            return None
        return ProductPhoto(
            photo_id=photo_id,
            order=order,
            label=(
                "AI 생성 활용 장면"
                if photo_id == "lifestyle"
                else self._labels[photo_id]
            ),
            data=data,
            mime_type="image/png",
            width=image.width,
            height=image.height,
            asset_mode="generated_scene",
            source_asset_id=source_asset_id,
            source_sha256=source_sha256,
            background_generated=True,
            product_generated=True,
            fidelity_status="GENERATED",
        )

    def _generated_detail_view(
        self,
        *,
        profile: ProductProfileDto,
        role: str,
        order: int,
        source_asset_id: str,
        source_sha256: str,
        source_image: bytes,
        source_mime_type: str,
        photo_generation_failures: list[PhotoGenerationFailure],
        direction: str | None = None,
    ) -> ProductPhoto | None:
        if self.detail_view_generator is None:
            return None
        try:
            data = self.detail_view_generator.generate(
                profile=profile,
                role=role,
                source_image=source_image,
                source_mime_type=source_mime_type,
                width=self.canvas_size[0],
                height=self.canvas_size[1],
                **({"direction": direction} if direction else {}),
            )
            image = _decode_rgb(data)
        except (RuntimeError, ValueError, OSError) as exc:
            _record_photo_generation_failure(
                photo_generation_failures,
                role,
                exc,
            )
            return None
        return ProductPhoto(
            photo_id=role,
            order=order,
            label="AI 생성 디테일",
            data=data,
            mime_type="image/png",
            width=image.width,
            height=image.height,
            asset_mode="generated_view",
            source_asset_id=source_asset_id,
            source_sha256=source_sha256,
            background_generated=True,
            product_generated=True,
            fidelity_status="GENERATED",
        )

    def _validated_source_set(
        self,
        source_images: tuple[tuple[bytes, str], ...],
        roles: list[str],
    ) -> ProductPhotoSet:
        photos = []
        for index, (data, mime_type) in enumerate(source_images):
            if index < len(roles):
                photo_id = roles[index]
                label = self._labels[photo_id]
            else:
                alternate_index = index - len(roles) + 1
                photo_id = (
                    "alternate"
                    if alternate_index == 1
                    else f"alternate-{alternate_index:02d}"
                )
                label = self._labels["alternate"]
            photos.append(
                self._source_original(
                    photo_id=photo_id,
                    order=index + 1,
                    data=data,
                    mime_type=mime_type,
                    label=label,
                )
            )
        return ProductPhotoSet(
            photos=tuple(
                photo
                for photo in photos
                if self.validator.validate(photo, source_images=source_images)
                != "REJECTED"
            )
        )

    def _source_original(
        self,
        *,
        photo_id: str,
        order: int,
        data: bytes,
        mime_type: str,
        label: str,
    ) -> ProductPhoto:
        record = self.asset_store.put(data, mime_type, "source")
        image = _decode_rgb(data)
        if photo_id == "hero":
            return self._source_hero(
                order=order,
                source_image=data,
                source_mime_type=mime_type,
                source_asset_id=record.asset_id,
                source_sha256=record.sha256,
                size=image.size,
            )
        return ProductPhoto(
            photo_id=photo_id,
            order=order,
            label=label,
            data=data,
            mime_type=mime_type,
            width=image.width,
            height=image.height,
            asset_mode="source",
            source_asset_id=record.asset_id,
            source_sha256=record.sha256,
            product_generated=False,
            fidelity_status="VERIFIED",
        )

    def _source_hero(
        self,
        *,
        order: int,
        source_image: bytes,
        source_mime_type: str,
        source_asset_id: str,
        source_sha256: str,
        size: tuple[int, int],
    ) -> ProductPhoto:
        """Return the hero's intentionally unmodified source photograph.

        `source_original` is deliberately distinct from `source`/`FALLBACK`:
        the latter records a failed cutout, while this is the hero policy.
        """
        return ProductPhoto(
            photo_id="hero",
            order=order,
            label=self._labels["hero"],
            data=source_image,
            mime_type=source_mime_type,
            width=size[0],
            height=size[1],
            asset_mode="source_original",
            source_asset_id=source_asset_id,
            source_sha256=source_sha256,
            product_generated=False,
            fidelity_status="VERIFIED",
        )

    def _source_fallback(
        self,
        *,
        role: str,
        order: int,
        source_image: bytes,
        source_mime_type: str,
        source_asset_id: str,
        source_sha256: str,
        size: tuple[int, int],
    ) -> ProductPhoto:
        return ProductPhoto(
            photo_id=role,
            order=order,
            label=self._labels[role],
            data=source_image,
            mime_type=source_mime_type,
            width=size[0],
            height=size[1],
            asset_mode="source",
            source_asset_id=source_asset_id,
            source_sha256=source_sha256,
            product_generated=False,
            fidelity_status="FALLBACK",
        )

    def _detail_crops(
        self,
        *,
        order: int,
        source: Image.Image,
        cutout: ProductCutout,
        source_asset_id: str,
    ) -> list[ProductPhoto]:
        left, top, right, bottom = cutout.bbox
        box_width = right - left
        box_height = bottom - top
        crop_width = max(1, round(box_width * 0.62))
        crop_height = max(1, round(box_height * 0.62))
        center_positions = (
            (0.50, 0.50),
            (0.35, 0.35),
            (0.65, 0.65),
            (0.65, 0.35),
            (0.35, 0.65),
        )
        crops = []
        seen: set[tuple[int, int, int, int]] = set()
        for index, (horizontal, vertical) in enumerate(center_positions):
            center_x = round(left + box_width * horizontal)
            center_y = round(top + box_height * vertical)
            crop_left = max(0, min(source.width - crop_width, center_x - crop_width // 2))
            crop_top = max(0, min(source.height - crop_height, center_y - crop_height // 2))
            crop = (
                crop_left,
                crop_top,
                min(source.width, crop_left + crop_width),
                min(source.height, crop_top + crop_height),
            )
            if crop in seen:
                continue
            seen.add(crop)
            crops.append(
                ProductPhoto(
                    photo_id="detail" if index == 0 else f"detail-{index + 1:02d}",
                    order=order + len(crops),
                    label=self._labels["detail"],
                    data=_encode_png(source.crop(crop)),
                    mime_type="image/png",
                    width=crop[2] - crop[0],
                    height=crop[3] - crop[1],
                    asset_mode="source_crop",
                    source_asset_id=source_asset_id,
                    source_sha256=cutout.source_sha256,
                    transform=PhotoTransform(crop=crop),
                    product_generated=False,
                    fidelity_status="VERIFIED",
                )
            )
        return crops

    def _composite(
        self,
        *,
        role: str,
        order: int,
        cutout: ProductCutout,
        source_asset_id: str,
        background: Image.Image,
        background_generated: bool,
    ) -> ProductPhoto:
        canvas_width, canvas_height = self.canvas_size
        canvas = ImageOps.fit(
            background.convert("RGB"),
            self.canvas_size,
            method=Image.Resampling.LANCZOS,
        )
        rgba = Image.open(io.BytesIO(cutout.rgba_png)).convert("RGBA")
        visible = rgba.crop(cutout.bbox)
        max_width = round(canvas_width * (0.72 if role != "lifestyle" else 0.50))
        max_height = round(canvas_height * (0.72 if role != "lifestyle" else 0.50))
        scale = min(max_width / visible.width, max_height / visible.height)
        resized_size = (
            max(1, round(visible.width * scale)),
            max(1, round(visible.height * scale)),
        )
        resized = visible.resize(resized_size, Image.Resampling.LANCZOS)
        x = (canvas_width - resized.width) // 2
        if role in {"lifestyle", "scale"}:
            # Lifestyle/scale shots need a believable ground plane. Anchor the
            # source-preserved product near the lower surface instead of floating
            # it at the canvas center.
            y = canvas_height - resized.height - round(canvas_height * 0.14)
            canvas = self._add_contact_shadow(
                canvas,
                x=x,
                y=y,
                width=resized.width,
                height=resized.height,
            )
        else:
            y = (canvas_height - resized.height) // 2
        canvas.paste(resized, (x, y), resized.getchannel("A"))
        data = _encode_png(canvas)
        return ProductPhoto(
            photo_id=role,
            order=order,
            label=self._labels[role],
            data=data,
            mime_type="image/png",
            width=canvas_width,
            height=canvas_height,
            asset_mode="source_composite",
            source_asset_id=source_asset_id,
            source_sha256=cutout.source_sha256,
            cutout_sha256=cutout.cutout_sha256,
            mask_sha256=cutout.mask_sha256,
            background_generated=background_generated,
            product_generated=False,
            transform=PhotoTransform(scale=scale, x=x, y=y, crop=cutout.bbox),
            fidelity_status="VERIFIED",
        )

    @staticmethod
    def _add_contact_shadow(
        canvas: Image.Image,
        *,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> Image.Image:
        """Add only a soft grounding shadow behind the immutable source pixels."""
        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(shadow)
        shadow_left = x + round(width * 0.10)
        shadow_right = x + round(width * 0.90)
        shadow_y = y + height - max(2, round(height * 0.012))
        draw.ellipse(
            (shadow_left, shadow_y, shadow_right, shadow_y + max(6, round(height * 0.055))),
            fill=(35, 30, 25, 105),
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(max(2, round(width * 0.014))))
        return Image.alpha_composite(canvas.convert("RGBA"), shadow).convert("RGB")

    def _background_for(
        self,
        *,
        profile: ProductProfileDto,
        role: str,
        photo_generation_failures: list[PhotoGenerationFailure],
    ) -> tuple[Image.Image, bool]:
        if self.background_generator is None:
            return self._solid_background("#E9E4DC"), False
        try:
            data = self.background_generator.generate(
                profile=profile,
                role=role,
                width=self.canvas_size[0],
                height=self.canvas_size[1],
            )
            return _decode_rgb(data), True
        except (RuntimeError, ValueError, OSError) as exc:
            _record_photo_generation_failure(
                photo_generation_failures,
                role,
                exc,
            )
            return self._solid_background("#E9E4DC"), False

    @staticmethod
    def _preserve_arrangement(profile: ProductProfileDto) -> bool:
        """Avoid AI recomposition when the input is a photographed multi-item set."""
        visible_components = profile.observations.get("visible_components")
        if isinstance(visible_components, list) and len(visible_components) >= 3:
            return True
        searchable = " ".join(
            value
            for value in (
                profile.product_type,
                profile.display_name or "",
                profile.summary,
                *profile.keywords,
            )
            if value
        ).lower()
        return any(
            marker in searchable
            for marker in (
                "세트",
                "set",
                "컬렉션",
                "collection",
                "병과",
                "잔과",
                "주전자 및",
            )
        )

    def _solid_background(self, color: str) -> Image.Image:
        return Image.new("RGB", self.canvas_size, color)
