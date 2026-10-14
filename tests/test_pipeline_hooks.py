import hashlib
from datetime import datetime, timezone
from io import BytesIO

from PIL import Image

from detail_page_ai.dto import AiBePersistAck, CopySectionDto, ProductProfileDto
from detail_page_ai.models import GeneratedImage, ProductPhoto, ProductPhotoSet
from detail_page_ai.persistence import MemoryJobRepository
from detail_page_ai.pipeline import DetailPagePipeline
from detail_page_ai.service import DetailPageJobService


def png(color=(238, 236, 232)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (16, 16), color).save(output, format="PNG")
    return output.getvalue()


class NeverCalled:
    def __getattr__(self, name):
        raise AssertionError(f"{name} must not be called")


class ControlledExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, function, *args, **kwargs):
        self.jobs.append((function, args, kwargs))

    def run_all(self):
        jobs, self.jobs = self.jobs, []
        for function, args, kwargs in jobs:
            function(*args, **kwargs)


class SavingBackend:
    def persist(self, request, image):
        return AiBePersistAck(
            generation_id=request.generation_id,
            product_id=None,
            status="SAVED",
            saved_at=datetime.now(timezone.utc),
        )


def test_create_draft_routes_through_the_compose_hook_and_still_sanitizes():
    seen = {}

    class HookedPipeline(DetailPagePipeline):
        def _compose_profile(self, **kwargs):
            seen["additional"] = kwargs["additional_source_images"]
            return ProductProfileDto(
                product_type="보관함",
                summary="자개 보관함",
                copy_sections=[
                    CopySectionDto(
                        section_type="hero",
                        title="자개 보관함",
                        description="자개 문양이 보입니다.",
                        evidence="image-visible",
                    ),
                    CopySectionDto(
                        section_type="feature",
                        title="조선 왕실 진품",
                        description="왕실에서 쓰던 물건입니다.",
                        evidence="inferred",
                    ),
                ],
            )

    pipeline = HookedPipeline(
        analyzer=NeverCalled(),
        renderer=NeverCalled(),
        backend=object(),
        id_factory=lambda: "gen-1",
    )
    extra = ((png((10, 10, 10)), "image/png"),)

    result = pipeline.create_draft(
        "job-1", "req-1", png(), "image/png", additional_source_images=extra
    )

    assert seen["additional"] == extra
    assert [section.title for section in result.profile.copy_sections] == ["자개 보관함"]


def test_run_routes_through_the_produce_hook_and_still_drops_unverified_photos():
    source = png()
    source_hash = hashlib.sha256(source).hexdigest()
    hero = ProductPhoto(
        photo_id="hero",
        order=1,
        label="Hero",
        data=source,
        mime_type="image/png",
        asset_mode="source",
        source_sha256=source_hash,
        product_generated=False,
        fidelity_status="VERIFIED",
    )
    forged = ProductPhoto(
        photo_id="packshot",
        order=2,
        label="Packshot",
        data=png((1, 2, 3)),
        mime_type="image/png",
        asset_mode="source",
        source_sha256=source_hash,
        product_generated=False,
        fidelity_status="VERIFIED",
    )

    class HookedPipeline(DetailPagePipeline):
        def _produce_page(self, **kwargs):
            photo_set = ProductPhotoSet(photos=(hero, forged))
            return photo_set, GeneratedImage(
                data=png(), mime_type="image/png", photos=photo_set.photos
            )

    pipeline = HookedPipeline(
        analyzer=NeverCalled(),
        renderer=NeverCalled(),
        backend=SavingBackend(),
        id_factory=lambda: "gen-2",
    )

    result = pipeline.run(
        "job-2",
        "req-2",
        source,
        "image/png",
        profile_override=ProductProfileDto.minimal("보관함"),
    )

    assert [photo.photo_id for photo in result.fe_result.detail_page.photos] == ["hero"]


def test_service_hands_every_uploaded_photo_to_the_draft_step():
    seen = {}

    class HookedPipeline(DetailPagePipeline):
        def _compose_profile(self, **kwargs):
            seen["count"] = 1 + len(kwargs["additional_source_images"])
            return ProductProfileDto.minimal("보관함")

    executor = ControlledExecutor()
    service = DetailPageJobService(
        pipeline=HookedPipeline(
            analyzer=NeverCalled(), renderer=NeverCalled(), backend=object()
        ),
        executor=executor,
        repository=MemoryJobRepository(),
    )

    accepted = service.submit(
        png(),
        "image/png",
        additional_source_images=(
            (png((5, 5, 5)), "image/png"),
            (png((9, 9, 9)), "image/png"),
        ),
    )
    executor.run_all()

    assert seen["count"] == 3
    assert service.get(accepted.job_id).status == "DRAFT_READY"
