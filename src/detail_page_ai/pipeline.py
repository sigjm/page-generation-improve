import base64
import functools
import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Callable, Literal

from .backend_client import BackendDeliveryError
from .ai_dto import AiToBePersistRequestDto
from .dto import (
    AiBePersistAck,
    AiBeProductPersistRequest,
    AiFeResultDto,
    AiFeDraftPreviewDto,
    AiFeDraftResultDto,
    AiFeProductSummaryDto,
    FeDetailPageSectionDto,
    FeProductPhotoDto,
    GenerationMetadataDto,
    GeneratedSectionMetadataDto,
    GeneratedPhotoMetadataDto,
    PhotoGenerationFailureDto,
    ProductProfileDto,
    ApprovedDraftDto,
    UserHintsDto,
)
from .models import GeneratedImage, GenerationOptions, ProductPhotoSet
from .leases import LeaseHeartbeat
from .ports import (
    BackendSink,
    CraftResearcher,
    DetailPageRenderer,
    ProductAnalyzer,
    ProductPhotoGenerator,
    SourceAssetStore,
)
from .persistence import DeliveryOutbox, LeaseOwnershipError, MemoryDeliveryOutbox
from .react_document_builder import (
    build_react_document_from_draft,
    collect_referenced_photo_ids,
    resolve_page_plan_photos,
)
from .source_photos import ProductFidelityValidator
from .validation import (
    ensure_editorial_page_plan,
    sanitize_profile_for_render,
    validate_product_profile,
    validate_source_image,
)


class PendingGenerationNotFound(KeyError):
    """Raised when a BE delivery retry has no stored generation."""


@dataclass(slots=True)
class PipelineResult:
    generation_id: str
    fe_result: AiFeResultDto
    be_ack: AiBePersistAck | None
    backend_delivery_pending: bool
    warning: str | None = None


@dataclass(slots=True)
class DraftPipelineResult:
    draft_id: str
    generation_id: str
    profile: ProductProfileDto
    approved_draft: ApprovedDraftDto
    preview_photo_set: ProductPhotoSet
    fe_draft: AiFeDraftResultDto


logger = logging.getLogger(__name__)


def _one_job_at_a_time(method):
    """Run drafts and renders one at a time.

    The analysis, cutout and image models share one GPU (an L40S on Stage) and
    each keeps the memory it touched. Two jobs at once ran out of GPU memory, so
    a job waits here for the previous one instead.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        job_id = kwargs.get("job_id", args[0] if args else None)
        started = time.monotonic()
        with self._model_lock:
            waited = time.monotonic() - started
            if waited >= 1:
                logger.warning("job %s waited %.1fs for the previous job", job_id, waited)
            return method(self, *args, **kwargs)

    return wrapper


class DetailPagePipeline:
    def __init__(
        self,
        *,
        analyzer: ProductAnalyzer,
        researcher: CraftResearcher | None = None,
        photo_generator: ProductPhotoGenerator | None = None,
        renderer: DetailPageRenderer,
        backend: BackendSink,
        options: GenerationOptions | None = None,
        generation_metadata: GenerationMetadataDto | None = None,
        template_image: bytes | None = None,
        id_factory: Callable[[], str] | None = None,
        max_pending_generations: int = 100,
        outbox: DeliveryOutbox | None = None,
        asset_store: SourceAssetStore | None = None,
        response_asset_mode: Literal["base64", "url", "both"] = "base64",
        craft_confidence_threshold: float = 0.65,
        delivery_worker_id: str | None = None,
        photo_validator: ProductFidelityValidator | None = None,
        delivery_lease_seconds: int = 300,
        heartbeat_interval_seconds: float = 60.0,
        max_image_bytes: int = 10 * 1024 * 1024,
        max_source_images: int = 12,
        max_total_input_bytes: int = 120 * 1024 * 1024,
        require_decodable_images: bool = False,
    ):
        if max_pending_generations < 1:
            raise ValueError("max_pending_generations must be at least 1")
        self.analyzer = analyzer
        self.researcher = researcher
        self.photo_generator = photo_generator
        self.renderer = renderer
        self.backend = backend
        self.options = options or GenerationOptions()
        self.generation_metadata = generation_metadata or GenerationMetadataDto(
            aspect_ratio=self.options.aspect_ratio,
            image_size=self.options.image_size,
            prompt_version="detail-page-v1",
        )
        self.template_image = template_image
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self.max_pending_generations = max_pending_generations
        self.outbox = outbox or MemoryDeliveryOutbox()
        self.asset_store = asset_store
        self.response_asset_mode = response_asset_mode
        self.craft_confidence_threshold = craft_confidence_threshold
        self.delivery_worker_id = delivery_worker_id or f"delivery-{uuid.uuid4()}"
        self.photo_validator = photo_validator or ProductFidelityValidator()
        self.delivery_lease_seconds = delivery_lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.max_image_bytes = max_image_bytes
        self.max_source_images = max_source_images
        self.max_total_input_bytes = max_total_input_bytes
        self.require_decodable_images = require_decodable_images
        self._model_lock = threading.RLock()

    @_one_job_at_a_time
    def run(
        self,
        job_id: str,
        request_id: str,
        source_image: bytes,
        source_mime_type: str,
        *,
        options: GenerationOptions | None = None,
        additional_source_images: tuple[tuple[bytes, str], ...] = (),
        user_hints: UserHintsDto | None = None,
        profile_override: ProductProfileDto | None = None,
        status_callback: Callable[[str, int], None] | None = None,
        generation_id: str | None = None,
        product_id: str | None = None,
        source_asset_id: str | None = None,
    ) -> PipelineResult:
        def emit(status: str, progress: int) -> None:
            if status_callback is not None:
                status_callback(status, progress)

        run_options = options or self.options
        self._validate_inputs(
            source_image,
            source_mime_type,
            additional_source_images,
        )
        source_asset = (
            self.asset_store.put(source_image, source_mime_type, "source")
            if self.asset_store
            else None
        )
        emit("ANALYZING", 15)
        if profile_override is not None:
            profile = profile_override
            validate_product_profile(profile)
        else:
            profile = self._compose_profile(
                source_image=source_image,
                source_mime_type=source_mime_type,
                additional_source_images=additional_source_images,
                user_hints=user_hints,
                status_callback=emit,
            )
        profile = sanitize_profile_for_render(profile, user_hints=user_hints)
        approved_draft = ApprovedDraftDto.from_profile(profile)
        emit("EXTRACTING", 30)
        source_images = ((source_image, source_mime_type),) + additional_source_images
        photo_set, generated_image = self._produce_page(
            source_image=source_image,
            source_mime_type=source_mime_type,
            additional_source_images=additional_source_images,
            profile=profile,
            options=run_options,
            status_callback=emit,
        )
        photo_generation_failures = [
            PhotoGenerationFailureDto(
                photo_id=failure.photo_id,
                reason=failure.reason,
            )
            for failure in photo_set.photo_generation_failures
        ]
        if not generated_image.data:
            raise ValueError("Renderer returned empty image data")

        safe_photos = self._validated_photo_set(
            ProductPhotoSet(photos=generated_image.photos), source_images
        ).photos
        if safe_photos != generated_image.photos:
            generated_image = replace(generated_image, photos=safe_photos)

        generation_id = generation_id or self.id_factory()
        source_hash = hashlib.sha256(source_image).hexdigest()
        generated_hash = hashlib.sha256(generated_image.data).hexdigest()
        generated_asset = (
            self.asset_store.put(
                generated_image.data, generated_image.mime_type, "detail-page"
            )
            if self.asset_store
            else None
        )
        sorted_sections = sorted(generated_image.sections, key=lambda item: item.order)
        sorted_photos = sorted(generated_image.photos, key=lambda item: item.order)
        section_assets = [
            (
                self.asset_store.put(section.data, section.mime_type, "section")
                if self.asset_store
                else None
            )
            for section in sorted_sections
        ]
        photo_assets = [
            (
                self.asset_store.put(photo.data, photo.mime_type, "photo")
                if self.asset_store
                else None
            )
            for photo in sorted_photos
        ]
        section_metadata = [
            GeneratedSectionMetadataDto(
                section_id=section.section_id,
                order=section.order,
                label=section.label,
                mime_type=section.mime_type,
                width=section.width,
                height=section.height,
                sha256=hashlib.sha256(section.data).hexdigest(),
                asset_id=asset.asset_id if asset else None,
            )
            for section, asset in zip(sorted_sections, section_assets, strict=True)
        ]
        photo_metadata = [
            GeneratedPhotoMetadataDto(
                photo_id=photo.photo_id,
                order=photo.order,
                label=photo.label,
                mime_type=photo.mime_type,
                width=photo.width,
                height=photo.height,
                sha256=hashlib.sha256(photo.data).hexdigest(),
                asset_id=asset.asset_id if asset else None,
                asset_mode=photo.asset_mode,
                source_asset_id=photo.source_asset_id,
                source_sha256=photo.source_sha256,
                cutout_sha256=photo.cutout_sha256,
                mask_sha256=photo.mask_sha256,
                background_generated=photo.background_generated,
                product_generated=photo.product_generated,
                transform=photo.transform,
                fidelity_status=photo.fidelity_status,
            )
            for photo, asset in zip(sorted_photos, photo_assets, strict=True)
        ]
        available_photo_ids = {photo.photo_id for photo in sorted_photos}
        resolved_plan = resolve_page_plan_photos(
            approved_draft.page_plan, available_photo_ids
        )
        profile = profile.model_copy(update={"page_plan": resolved_plan})
        react_document = build_react_document_from_draft(
            approved_draft,
            available_photo_ids=available_photo_ids,
        )
        referenced_photo_ids = collect_referenced_photo_ids(react_document)
        unused_generated_photo_ids = list(
            dict.fromkeys(
                photo.photo_id
                for photo in sorted_photos
                if photo.product_generated
                and photo.photo_id not in referenced_photo_ids
            )
        )
        persist_request_model = (
            AiToBePersistRequestDto
            if product_id is not None
            else AiBeProductPersistRequest
        )
        be_request = persist_request_model.from_profile(
            generation_id=generation_id,
            job_id=job_id,
            request_id=request_id,
            source_image_mime_type=source_mime_type,
            source_image_sha256=source_hash,
            source_asset_id=source_asset_id
            or (source_asset.asset_id if source_asset else None),
            generated_image_mime_type=generated_image.mime_type,
            generated_width=generated_image.width,
            generated_height=generated_image.height,
            generated_image_sha256=generated_hash,
            generated_asset_id=generated_asset.asset_id if generated_asset else None,
            user_hints=user_hints,
            profile=profile,
            generation=self.generation_metadata.model_copy(
                update={
                    "aspect_ratio": run_options.aspect_ratio,
                    "image_size": run_options.image_size,
                    "layout_id": profile.layout_id,
                    "research_used": profile.craft_research is not None,
                }
            ),
            generated_sections=section_metadata,
            generated_photos=photo_metadata,
            photo_generation_failures=photo_generation_failures,
            unused_generated_photo_ids=unused_generated_photo_ids,
            react_document=react_document,
            product_id=product_id,
        )

        page_url, page_base64 = self._present_asset(
            generated_image.data, generated_asset
        )
        fe_result = AiFeResultDto(
            generation_id=generation_id,
            product=self._fe_product(profile),
            detail_page={
                "image_url": page_url,
                "image_base64": page_base64,
                "mime_type": generated_image.mime_type,
                "width": generated_image.width,
                "height": generated_image.height,
                "sections": [
                    FeDetailPageSectionDto(
                        section_id=section.section_id,
                        order=section.order,
                        label=section.label,
                        image_url=self._present_asset(section.data, asset)[0],
                        image_base64=self._present_asset(section.data, asset)[1],
                        mime_type=section.mime_type,
                        width=section.width,
                        height=section.height,
                    )
                    for section, asset in zip(
                        sorted_sections, section_assets, strict=True
                    )
                ],
                "photos": [
                    FeProductPhotoDto(
                        photo_id=photo.photo_id,
                        order=photo.order,
                        label=photo.label,
                        image_url=self._present_asset(photo.data, asset)[0],
                        image_base64=self._present_asset(photo.data, asset)[1],
                        mime_type=photo.mime_type,
                        width=photo.width,
                        height=photo.height,
                        asset_mode=photo.asset_mode,
                        source_asset_id=photo.source_asset_id,
                        source_sha256=photo.source_sha256,
                        cutout_sha256=photo.cutout_sha256,
                        mask_sha256=photo.mask_sha256,
                        background_generated=photo.background_generated,
                        product_generated=photo.product_generated,
                        transform=photo.transform,
                        fidelity_status=photo.fidelity_status,
                    )
                    for photo, asset in zip(sorted_photos, photo_assets, strict=True)
                ],
                "photo_generation_failures": photo_generation_failures,
                "unused_generated_photo_ids": unused_generated_photo_ids,
                "react_document": react_document,
            },
        )
        emit("DELIVERING", 90)
        self.outbox.enqueue(be_request, generated_image, "")
        delivery_owner = f"{self.delivery_worker_id}:{uuid.uuid4()}"
        delivery_record = self.outbox.claim(
            generation_id,
            delivery_owner,
            lease_seconds=self.delivery_lease_seconds,
        )
        if delivery_record is None:
            existing = self.outbox.get(generation_id)
            return PipelineResult(
                generation_id=generation_id,
                fe_result=fe_result,
                be_ack=None,
                backend_delivery_pending=existing.status != "DELIVERED",
                warning=(
                    "Backend delivery is already in progress"
                    if existing.status != "DELIVERED"
                    else None
                ),
            )
        try:
            with LeaseHeartbeat(
                lambda: self.outbox.renew_lease(
                    generation_id,
                    delivery_owner,
                    lease_seconds=self.delivery_lease_seconds,
                ),
                interval_seconds=self.heartbeat_interval_seconds,
            ) as heartbeat:
                try:
                    be_ack = self.backend.persist(be_request, generated_image)
                except BackendDeliveryError as exc:
                    heartbeat.ensure_active()
                    self.outbox.mark_failed(
                        generation_id,
                        str(exc),
                        delivery_owner,
                        retryable=exc.retryable,
                    )
                    return PipelineResult(
                        generation_id=generation_id,
                        fe_result=fe_result,
                        be_ack=None,
                        backend_delivery_pending=True,
                        warning=str(exc),
                    )
                heartbeat.ensure_active()
                self.outbox.mark_delivered(generation_id, delivery_owner)
        except LeaseOwnershipError:
            return PipelineResult(
                generation_id=generation_id,
                fe_result=fe_result,
                be_ack=None,
                backend_delivery_pending=True,
                warning="Backend delivery is already in progress",
            )
        return PipelineResult(
            generation_id=generation_id,
            fe_result=fe_result,
            be_ack=be_ack,
            backend_delivery_pending=False,
        )

    @_one_job_at_a_time
    def create_draft(
        self,
        job_id: str,
        request_id: str,
        source_image: bytes,
        source_mime_type: str,
        *,
        additional_source_images: tuple[tuple[bytes, str], ...] = (),
        user_hints: UserHintsDto | None = None,
        source_asset_id: str | None = None,
        status_callback: Callable[[str, int], None] | None = None,
        generation_id: str | None = None,
    ) -> DraftPipelineResult:
        """Analyze the source and return structured JSON without final generation.

        This method deliberately does not call the photo generator, HTML PNG
        renderer, backend sink, or delivery outbox.  Approval is the explicit
        boundary at which those expensive side effects begin.
        """

        def emit(status: str, progress: int) -> None:
            if status_callback is not None:
                status_callback(status, progress)

        self._validate_inputs(source_image, source_mime_type, additional_source_images)
        if self.asset_store:
            stored_source = self.asset_store.put(source_image, source_mime_type, "source")
            resolved_source_asset_id = source_asset_id or stored_source.asset_id
        else:
            resolved_source_asset_id = source_asset_id

        emit("ANALYZING", 15)
        profile = self._compose_profile(
            source_image=source_image,
            source_mime_type=source_mime_type,
            additional_source_images=additional_source_images,
            user_hints=user_hints,
            status_callback=emit,
        )
        profile = sanitize_profile_for_render(profile, user_hints=user_hints)
        emit("EXTRACTING", 60)
        generation_id = generation_id or self.id_factory()
        approved_draft = ApprovedDraftDto.from_profile(profile)
        return self.build_editable_draft(
            job_id=job_id,
            generation_id=generation_id,
            source_image=source_image,
            source_mime_type=source_mime_type,
            profile=profile,
            approved_draft=approved_draft,
            source_asset_id=resolved_source_asset_id,
            user_hints=user_hints,
        )

    def build_editable_draft(
        self,
        *,
        job_id: str,
        generation_id: str,
        source_image: bytes,
        source_mime_type: str,
        profile: ProductProfileDto,
        approved_draft: ApprovedDraftDto,
        source_asset_id: str | None = None,
        user_hints: UserHintsDto | None = None,
    ) -> DraftPipelineResult:
        """Rebuild the JSON draft after creator edits without AI or PNG work."""
        profile = sanitize_profile_for_render(profile, user_hints=user_hints)
        approved_draft = approved_draft.model_copy(update={"page_plan": profile.page_plan})
        preview_photo_set = ProductPhotoSet()
        react_document = build_react_document_from_draft(approved_draft)
        source_hash = hashlib.sha256(source_image).hexdigest()
        preview_image_url, preview_image_base64 = self._present_asset(
            source_image, None
        )
        fe_draft = AiFeDraftResultDto(
            draft_id=job_id,
            generation_id=generation_id,
            source_mime_type=source_mime_type,
            source_sha256=source_hash,
            source_asset_id=source_asset_id,
            product=self._fe_product(profile),
            draft=approved_draft,
            preview=AiFeDraftPreviewDto(
                source_asset_id=source_asset_id,
                source_sha256=source_hash,
                mime_type=source_mime_type,
                image_url=preview_image_url,
                image_base64=preview_image_base64,
            ),
            preview_photos=[],
            photo_generation_failures=[],
            unused_generated_photo_ids=[],
            react_document=react_document,
        )
        return DraftPipelineResult(
            draft_id=job_id,
            generation_id=generation_id,
            profile=profile,
            approved_draft=approved_draft,
            preview_photo_set=preview_photo_set,
            fe_draft=fe_draft,
        )

    def _validate_inputs(
        self,
        source_image: bytes,
        source_mime_type: str,
        additional_source_images: tuple[tuple[bytes, str], ...],
    ) -> None:
        all_images = ((source_image, source_mime_type),) + additional_source_images
        if len(all_images) > self.max_source_images:
            raise ValueError("Too many source images")
        if sum(len(data) for data, _ in all_images) > self.max_total_input_bytes:
            raise ValueError("Source images exceed the total request size")
        for data, mime_type in all_images:
            validate_source_image(
                data,
                mime_type,
                max_bytes=self.max_image_bytes,
                require_decodable=self.require_decodable_images,
            )

    def retry_backend_delivery(self, generation_id: str) -> AiBePersistAck:
        try:
            record = self.outbox.get(generation_id)
        except KeyError as exc:
            raise PendingGenerationNotFound(generation_id) from exc
        if record.status == "DELIVERED":
            raise PendingGenerationNotFound(generation_id)
        delivery_owner = f"{self.delivery_worker_id}:{uuid.uuid4()}"
        delivery_record = self.outbox.claim(
            generation_id,
            delivery_owner,
            lease_seconds=self.delivery_lease_seconds,
        )
        if delivery_record is None:
            raise PendingGenerationNotFound(generation_id)
        try:
            with LeaseHeartbeat(
                lambda: self.outbox.renew_lease(
                    generation_id,
                    delivery_owner,
                    lease_seconds=self.delivery_lease_seconds,
                ),
                interval_seconds=self.heartbeat_interval_seconds,
            ) as heartbeat:
                try:
                    ack = self.backend.persist(
                        delivery_record.request, delivery_record.image
                    )
                except BackendDeliveryError as exc:
                    heartbeat.ensure_active()
                    self.outbox.mark_failed(
                        generation_id,
                        str(exc),
                        delivery_owner,
                        retryable=exc.retryable,
                    )
                    raise
                heartbeat.ensure_active()
                self.outbox.mark_delivered(generation_id, delivery_owner)
        except LeaseOwnershipError as exc:
            raise PendingGenerationNotFound(generation_id) from exc
        return ack

    def recover_fe_result(self, generation_id: str) -> AiFeResultDto:
        record = self.outbox.get(generation_id)
        page_asset = self._recover_asset(record.request.detail_page.asset_id)
        page_url, page_base64 = self._present_asset(record.image.data, page_asset)
        section_assets = {
            (section.section_id, section.order): self._recover_asset(section.asset_id)
            for section in record.request.detail_page.sections
        }
        photo_assets = {
            (photo.photo_id, photo.order): self._recover_asset(photo.asset_id)
            for photo in record.request.detail_page.photos
        }
        react_document = record.request.detail_page.react_document
        if react_document is None:
            react_document = build_react_document_from_draft(
                ApprovedDraftDto.from_profile(record.request.product),
            )
        return AiFeResultDto(
            generation_id=generation_id,
            product=self._fe_product(record.request.product),
            detail_page={
                "image_url": page_url,
                "image_base64": page_base64,
                "mime_type": record.image.mime_type,
                "width": record.image.width,
                "height": record.image.height,
                "sections": [
                    FeDetailPageSectionDto(
                        section_id=section.section_id,
                        order=section.order,
                        label=section.label,
                        image_url=self._present_asset(
                            section.data,
                            section_assets.get((section.section_id, section.order)),
                        )[0],
                        image_base64=self._present_asset(
                            section.data,
                            section_assets.get((section.section_id, section.order)),
                        )[1],
                        mime_type=section.mime_type,
                        width=section.width,
                        height=section.height,
                    )
                    for section in sorted(
                        record.image.sections, key=lambda item: item.order
                    )
                ],
                "photos": [
                    FeProductPhotoDto(
                        photo_id=photo.photo_id,
                        order=photo.order,
                        label=photo.label,
                        image_url=self._present_asset(
                            photo.data,
                            photo_assets.get((photo.photo_id, photo.order)),
                        )[0],
                        image_base64=self._present_asset(
                            photo.data,
                            photo_assets.get((photo.photo_id, photo.order)),
                        )[1],
                        mime_type=photo.mime_type,
                        width=photo.width,
                        height=photo.height,
                        asset_mode=photo.asset_mode,
                        source_asset_id=photo.source_asset_id,
                        source_sha256=photo.source_sha256,
                        cutout_sha256=photo.cutout_sha256,
                        mask_sha256=photo.mask_sha256,
                        background_generated=photo.background_generated,
                        product_generated=photo.product_generated,
                        transform=photo.transform,
                        fidelity_status=photo.fidelity_status,
                    )
                    for photo in sorted(
                        record.image.photos, key=lambda item: item.order
                    )
                ],
                "photo_generation_failures": record.request.detail_page.photo_generation_failures,
                "unused_generated_photo_ids": record.request.detail_page.unused_generated_photo_ids,
                "react_document": react_document,
            },
        )

    def _recover_asset(self, asset_id: str | None):
        if not asset_id or self.asset_store is None:
            return None
        try:
            return self.asset_store.get_record(asset_id)
        except KeyError:
            return None

    def _validated_photo_set(
        self,
        photo_set: ProductPhotoSet,
        source_images: tuple[tuple[bytes, str], ...],
    ) -> ProductPhotoSet:
        verified = []
        for photo in photo_set.photos:
            status = self.photo_validator.validate(
                photo, source_images=source_images
            )
            if status != "REJECTED":
                verified.append(
                    photo
                    if photo.fidelity_status == status
                    else replace(photo, fidelity_status=status)
                )
        return ProductPhotoSet(
            photos=tuple(verified),
            photo_generation_failures=photo_set.photo_generation_failures,
        )

    @staticmethod
    def _fe_product(profile: ProductProfileDto) -> AiFeProductSummaryDto:
        return AiFeProductSummaryDto.from_profile(profile)

    def _present_asset(self, data: bytes, asset) -> tuple[str | None, str | None]:
        url = asset.url if asset is not None else None
        encoded = base64.b64encode(data).decode("ascii")
        if self.response_asset_mode == "both":
            return url, encoded
        if self.response_asset_mode == "url":
            return url, None
        return None, encoded

    def _analyze(
        self,
        source_image: bytes,
        source_mime_type: str,
        user_hints: UserHintsDto | None,
    ) -> ProductProfileDto:
        if user_hints and user_hints.model_dump(exclude_none=True):
            profile = self.analyzer.analyze(
                source_image, source_mime_type, user_hints=user_hints
            )
        else:
            profile = self.analyzer.analyze(source_image, source_mime_type)
        validate_product_profile(profile)
        return profile

    def _research_if_needed(
        self,
        profile: ProductProfileDto,
        user_hints: UserHintsDto | None,
    ) -> ProductProfileDto:
        if not (self.researcher and self._should_research_craft(profile, user_hints)):
            return profile
        research_profile = profile
        if not research_profile.craft_type:
            research_profile = profile.model_copy(
                update={
                    "craft_type": (
                        profile.candidate_types[0]
                        if profile.candidate_types
                        else profile.product_type
                    )
                }
            )
        if user_hints and user_hints.model_dump(exclude_none=True):
            craft_research = self.researcher.research(
                research_profile, user_hints=user_hints
            )
        else:
            craft_research = self.researcher.research(research_profile)
        return profile.model_copy(
            update={
                "craft_type": craft_research.craft_type,
                "craft_research": craft_research,
            }
        )

    def _compose_profile(
        self,
        *,
        source_image: bytes,
        source_mime_type: str,
        additional_source_images: tuple[tuple[bytes, str], ...],
        user_hints: UserHintsDto | None,
        status_callback: Callable[[str, int], None],
    ) -> ProductProfileDto:
        """Analyze, research and plan the page. DetailPageAgent overrides this step."""
        del additional_source_images, status_callback  # the fixed steps read one photo
        profile = self._analyze(source_image, source_mime_type, user_hints)
        profile = self._research_if_needed(profile, user_hints)
        return ensure_editorial_page_plan(profile)

    def _generate_photos(
        self,
        *,
        source_image: bytes,
        source_mime_type: str,
        additional_source_images: tuple[tuple[bytes, str], ...],
        profile: ProductProfileDto,
        options: GenerationOptions,
    ) -> ProductPhotoSet:
        if not self.photo_generator:
            return ProductPhotoSet()
        photo_arguments = {
            "source_image": source_image,
            "source_mime_type": source_mime_type,
            "profile": profile,
            "options": options,
        }
        if additional_source_images:
            photo_arguments["additional_source_images"] = additional_source_images
        return self.photo_generator.generate(**photo_arguments)

    def _render_page(
        self,
        *,
        source_image: bytes,
        source_mime_type: str,
        profile: ProductProfileDto,
        options: GenerationOptions,
        photo_set: ProductPhotoSet,
    ) -> GeneratedImage:
        return self.renderer.render(
            source_image=source_image,
            source_mime_type=source_mime_type,
            template_image=self.template_image,
            profile=profile,
            options=options,
            photo_set=photo_set,
        )

    def _produce_page(
        self,
        *,
        source_image: bytes,
        source_mime_type: str,
        additional_source_images: tuple[tuple[bytes, str], ...],
        profile: ProductProfileDto,
        options: GenerationOptions,
        status_callback: Callable[[str, int], None],
    ) -> tuple[ProductPhotoSet, GeneratedImage]:
        """Make the photos and render the page. DetailPageAgent overrides this step."""
        status_callback("GENERATING_BACKGROUNDS", 45)
        photo_set = self._generate_photos(
            source_image=source_image,
            source_mime_type=source_mime_type,
            additional_source_images=additional_source_images,
            profile=profile,
            options=options,
        )
        status_callback("COMPOSING", 55)
        status_callback("VERIFYING", 65)
        source_images = ((source_image, source_mime_type),) + additional_source_images
        photo_set = self._validated_photo_set(photo_set, source_images)
        status_callback("RENDERING", 75)
        return photo_set, self._render_page(
            source_image=source_image,
            source_mime_type=source_mime_type,
            profile=profile,
            options=options,
            photo_set=photo_set,
        )

    def _should_research_craft(
        self,
        profile: ProductProfileDto,
        user_hints: UserHintsDto | None = None,
    ) -> bool:
        if user_hints and user_hints.model_dump(exclude_none=True):
            return True
        if profile.is_traditional_craft:
            return True
        if profile.craft_confidence >= self.craft_confidence_threshold:
            return True
        craft_terms = ("나전", "칠기", "한지", "도자", "옻칠", "자개", "공예")
        candidates = [
            profile.product_type,
            profile.craft_type or "",
            *profile.candidate_types,
            *profile.keywords,
        ]
        return any(term in candidate for term in craft_terms for candidate in candidates)
