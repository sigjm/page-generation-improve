import hashlib
from io import BytesIO

import pytest
from PIL import Image

from detail_page_ai.dto import ProductProfileDto
from detail_page_ai.models import ProductPhoto
from detail_page_ai.source_photos import (
    SolidBackgroundCutoutExtractor,
    SourcePreservingProductPhotoGenerator,
)


def png(color) -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 32), color).save(output, format="PNG")
    return output.getvalue()


SOURCE = png((200, 200, 200))
PROFILE = ProductProfileDto.minimal("보관함")


class RecordingGenerator:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("image server returned 500")
        return png((10, 120, 10))


def generated(photo_id, asset_mode):
    return ProductPhoto(
        photo_id=photo_id,
        order=7,
        label="AI",
        data=png((1, 1, 1)),
        mime_type="image/png",
        asset_mode=asset_mode,
        source_sha256=hashlib.sha256(SOURCE).hexdigest(),
        background_generated=True,
        product_generated=True,
        fidelity_status="GENERATED",
    )


def make_generator(**kwargs):
    return SourcePreservingProductPhotoGenerator(
        extractor=SolidBackgroundCutoutExtractor(), **kwargs
    )


def test_regenerate_scene_keeps_the_slot_and_passes_direction():
    scenes = RecordingGenerator()
    generator = make_generator(usage_scene_generator=scenes)

    photo, failures = generator.regenerate_role(
        photo=generated("lifestyle-02", "generated_scene"),
        source_image=SOURCE,
        source_mime_type="image/png",
        profile=PROFILE,
        direction="밝은 원목 테이블 위",
    )

    assert failures == ()
    assert photo.photo_id == "lifestyle-02"
    assert photo.order == 7
    assert photo.asset_mode == "generated_scene"
    assert photo.data == png((10, 120, 10))
    assert scenes.calls[0]["direction"] == "밝은 원목 테이블 위"


def test_regenerate_detail_view_uses_the_detail_generator():
    views = RecordingGenerator()
    generator = make_generator(detail_view_generator=views)

    photo, _ = generator.regenerate_role(
        photo=generated("detail-03", "generated_view"),
        source_image=SOURCE,
        source_mime_type="image/png",
        profile=PROFILE,
    )

    assert photo.photo_id == "detail-03"
    assert views.calls[0]["role"] == "detail-03"
    assert "direction" not in views.calls[0]


def test_regenerate_refuses_source_preserved_photos():
    generator = make_generator(usage_scene_generator=RecordingGenerator())
    hero = ProductPhoto(
        photo_id="hero",
        order=1,
        label="Hero",
        data=SOURCE,
        mime_type="image/png",
        asset_mode="source_original",
        source_sha256=hashlib.sha256(SOURCE).hexdigest(),
        product_generated=False,
        fidelity_status="VERIFIED",
    )

    with pytest.raises(ValueError):
        generator.regenerate_role(
            photo=hero, source_image=SOURCE, source_mime_type="image/png", profile=PROFILE
        )


def test_regenerate_reports_generation_failures():
    generator = make_generator(usage_scene_generator=RecordingGenerator(fail=True))

    photo, failures = generator.regenerate_role(
        photo=generated("lifestyle-02", "generated_scene"),
        source_image=SOURCE,
        source_mime_type="image/png",
        profile=PROFILE,
    )

    assert photo is None
    assert failures[0].photo_id == "lifestyle-02"


def test_local_scene_generator_appends_direction_to_the_prompt():
    from local_detail_page_ai.runner import MlxServeUsageSceneGenerator

    class RecordingImageClient:
        def edit(self, prompt, **kwargs):
            self.prompt = prompt
            return b"image"

    client = RecordingImageClient()
    MlxServeUsageSceneGenerator(client).generate(
        profile=PROFILE,
        role="lifestyle",
        source_image=SOURCE,
        source_mime_type="image/png",
        width=8,
        height=8,
        direction="밝은 원목 테이블 위",
    )

    assert client.prompt.endswith("밝은 원목 테이블 위")
