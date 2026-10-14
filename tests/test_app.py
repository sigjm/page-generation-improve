import asyncio
import io
import json
from types import SimpleNamespace
import pytest

import detail_page_ai.app as app_module
import local_detail_page_ai.factory as factory_module
from fastapi import UploadFile
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from detail_page_ai.assets import LocalFileAssetStore
from detail_page_ai.persistence import SQLiteDeliveryOutbox, SQLiteJobRepository
from detail_page_ai.dto import (
    AiFeResultDto,
    AiFeProductSummaryDto,
    FeDetailPageAssetDto,
    ProductProfileDto,
    UserHintsDto,
)
from detail_page_ai.source_photos import SourcePreservingProductPhotoGenerator


def test_build_service_wires_only_local_mlx_and_flux(monkeypatch, tmp_path):
    class FakeChatClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeImageClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeHtmlRenderer:
        def __init__(self, **kwargs):
            pass

    settings = SimpleNamespace(
        detail_page_template_path=None,
        analysis_provider="local",
        local_text_provider="mlx",
        local_text_url="http://127.0.0.1:11234",
        local_text_model="ddalcu/Qwen3.8-27B-MLX-Serve-4bit",
        local_text_timeout_seconds=300.0,
        local_image_provider="mlx",
        local_image_url="http://127.0.0.1:11234",
        local_image_model="mlx-community/flux2-klein-9b-4bit",
        local_image_timeout_seconds=300.0,
        background_provider="mlx",
        product_photo_generation="source",
        product_photo_shots="hero,packshot,detail,lifestyle",
        max_generated_photos=5,
        detail_page_renderer="html",
        backend_url=None,
        backend_auth_token=None,
        backend_timeout_seconds=60.0,
        prompt_version="local-v1",
        asset_store_dir=str(tmp_path / "assets"),
        sqlite_path=str(tmp_path / "state.sqlite3"),
        response_asset_mode="base64",
        craft_confidence_threshold=0.65,
    )
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(factory_module, "MlxServeChatClient", FakeChatClient)
    monkeypatch.setattr(factory_module, "MlxServeImageClient", FakeImageClient)
    monkeypatch.setattr(factory_module, "HtmlDetailPageRenderer", FakeHtmlRenderer)

    service = app_module.build_service()

    assert service.pipeline.generation_metadata.provider == "local"
    assert service.pipeline.generation_metadata.analysis_model == (
        "ddalcu/Qwen3.8-27B-MLX-Serve-4bit"
    )
    assert service.pipeline.generation_metadata.image_model == (
        "mlx-community/flux2-klein-9b-4bit"
    )
    assert service.pipeline.analyzer.chat_client.kwargs["model"] == (
        "ddalcu/Qwen3.8-27B-MLX-Serve-4bit"
    )
    assert service.pipeline.analyzer.image_generation_enabled is True
    assert service.pipeline.photo_generator.background_generator.image_client.kwargs[
        "model"
    ] == "mlx-community/flux2-klein-9b-4bit"
    assert service.pipeline.photo_generator.max_generated_photos == 5


def test_build_service_wires_sglang_text_and_image_clients(monkeypatch, tmp_path):
    class FakeChatClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeImageClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeHtmlRenderer:
        def __init__(self, **kwargs):
            pass

    settings = SimpleNamespace(
        detail_page_template_path=None,
        analysis_provider="local",
        local_text_provider="sglang",
        local_text_url="http://sglang-text:30000",
        local_text_model="Qwen/Qwen3.8-27B",
        local_text_timeout_seconds=300.0,
        local_image_provider="sglang",
        local_image_url="http://sglang-image:30001",
        local_image_model="black-forest-labs/FLUX.2-klein-4B",
        local_image_timeout_seconds=300.0,
        background_provider="sglang",
        product_photo_generation="source",
        product_photo_shots="hero,packshot,detail,lifestyle",
        max_generated_photos=5,
        detail_page_renderer="html",
        backend_url=None,
        backend_auth_token=None,
        backend_timeout_seconds=60.0,
        prompt_version="sglang-v1",
        asset_store_dir=str(tmp_path / "assets"),
        sqlite_path=str(tmp_path / "state.sqlite3"),
        response_asset_mode="base64",
        craft_confidence_threshold=0.65,
    )
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(factory_module, "MlxServeChatClient", FakeChatClient)
    monkeypatch.setattr(factory_module, "SglangImageClient", FakeImageClient)
    monkeypatch.setattr(factory_module, "HtmlDetailPageRenderer", FakeHtmlRenderer)

    service = app_module.build_service()

    assert service.pipeline.analyzer.chat_client.kwargs == {
        "base_url": "http://sglang-text:30000",
        "model": "Qwen/Qwen3.8-27B",
        "timeout": 300.0,
    }
    assert service.pipeline.photo_generator.background_generator.image_client.kwargs == {
        "base_url": "http://sglang-image:30001",
        "model": "black-forest-labs/FLUX.2-klein-4B",
        "timeout": 300.0,
        "steps": 4,
    }


def test_build_service_rejects_product_pixel_generation_provider(monkeypatch):
    settings = SimpleNamespace(
        detail_page_template_path=None,
        analysis_provider="local",
        local_text_provider="mlx",
        local_text_url="http://127.0.0.1:11234",
        local_text_model="gemma",
        local_text_timeout_seconds=300.0,
        local_image_provider="none",
        local_image_url="http://127.0.0.1:11234",
        local_image_model="flux",
        local_image_timeout_seconds=300.0,
        background_provider="none",
        product_photo_generation="unsupported",
        product_photo_shots="hero,detail",
        detail_page_renderer="html",
        backend_url=None,
        backend_auth_token=None,
        backend_timeout_seconds=60.0,
        prompt_version="test-prompt",
    )
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)

    with pytest.raises(ValueError, match="PRODUCT_PHOTO_GENERATION=source"):
        app_module.build_service()


def test_build_service_uses_source_safe_generator_and_durable_local_adapters(
    monkeypatch, tmp_path
):
    class FakeChatClient:
        def __init__(self, **kwargs):
            pass

    class FakeImageClient:
        def __init__(self, **kwargs):
            pass

    class FakeHtmlRenderer:
        def __init__(self, **kwargs):
            pass

    settings = SimpleNamespace(
        detail_page_template_path=None,
        analysis_provider="local",
        local_text_provider="mlx",
        local_text_url="http://127.0.0.1:11234",
        local_text_model="gemma",
        local_text_timeout_seconds=300.0,
        local_image_provider="mlx",
        local_image_url="http://127.0.0.1:11234",
        local_image_model="flux",
        local_image_timeout_seconds=300.0,
        background_provider="mlx",
        product_photo_generation="source",
        product_photo_shots="hero,packshot,detail,lifestyle",
        detail_page_renderer="html",
        backend_url=None,
        backend_auth_token=None,
        backend_timeout_seconds=60.0,
        prompt_version="source-safe-v2",
        asset_store_dir=str(tmp_path / "assets"),
        sqlite_path=str(tmp_path / "state.sqlite3"),
        response_asset_mode="base64",
        craft_confidence_threshold=0.65,
    )
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(factory_module, "MlxServeChatClient", FakeChatClient)
    monkeypatch.setattr(factory_module, "MlxServeImageClient", FakeImageClient)
    monkeypatch.setattr(factory_module, "HtmlDetailPageRenderer", FakeHtmlRenderer)

    service = app_module.build_service()

    assert isinstance(
        service.pipeline.photo_generator, SourcePreservingProductPhotoGenerator
    )
    assert isinstance(service.pipeline.asset_store, LocalFileAssetStore)
    assert isinstance(service.pipeline.outbox, SQLiteDeliveryOutbox)
    assert isinstance(service.repository, SQLiteJobRepository)


def test_build_service_does_not_construct_local_background_in_none_mode(
    monkeypatch, tmp_path
):
    class FakeChatClient:
        def __init__(self, **kwargs):
            pass

    class UnexpectedBackground:
        def __init__(self, **kwargs):
            raise AssertionError("local image provider must be disabled")

    class FakeHtmlRenderer:
        def __init__(self, **kwargs):
            pass

    settings = SimpleNamespace(
        detail_page_template_path=None,
        analysis_provider="local",
        local_text_provider="mlx",
        local_text_url="http://127.0.0.1:11234",
        local_text_model="gemma",
        local_text_timeout_seconds=300.0,
        local_image_provider="none",
        local_image_url="http://127.0.0.1:11234",
        local_image_model="flux",
        local_image_timeout_seconds=300.0,
        background_provider="none",
        product_photo_generation="source",
        product_photo_shots="hero,packshot,detail,lifestyle",
        max_generated_photos=5,
        detail_page_renderer="html",
        backend_url=None,
        backend_auth_token=None,
        backend_timeout_seconds=60.0,
        prompt_version="test-prompt",
        asset_store_dir=str(tmp_path / "assets"),
        sqlite_path=str(tmp_path / "state.sqlite3"),
        response_asset_mode="base64",
        craft_confidence_threshold=0.65,
    )
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(factory_module, "MlxServeChatClient", FakeChatClient)
    monkeypatch.setattr(factory_module, "MlxServeImageClient", UnexpectedBackground)
    monkeypatch.setattr(factory_module, "HtmlDetailPageRenderer", FakeHtmlRenderer)

    service = app_module.build_service()

    assert service.pipeline.photo_generator.background_generator is None
    assert service.pipeline.analyzer.image_generation_enabled is False


def test_build_service_disables_usage_layout_when_generated_photo_cap_is_zero(
    monkeypatch, tmp_path
):
    from detail_page_ai.config import Settings

    settings = Settings(
        _env_file=None,
        MAX_GENERATED_PHOTOS=0,
        ASSET_STORE_DIR=str(tmp_path / "assets"),
        SQLITE_PATH=str(tmp_path / "state.sqlite3"),
    )
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)

    service = app_module.build_service()

    assert service.pipeline.analyzer.image_generation_enabled is False


def test_create_job_accepts_repeated_additional_original_images(monkeypatch):
    class RecordingService:
        def __init__(self):
            self.additional_source_images = None

        def submit(self, source_image, source_mime_type, **kwargs):
            self.additional_source_images = kwargs["additional_source_images"]
            return {
                "job_id": "job-1",
                "request_id": "request-1",
                "status": "QUEUED",
                "status_url": "/api/v1/ai/detail-page-jobs/job-1",
                "created_at": "2026-08-27T00:00:00Z",
            }

    service = RecordingService()
    monkeypatch.setattr(app_module, "get_service", lambda: service)

    def upload(filename, data, mime_type):
        return UploadFile(
            file=io.BytesIO(data),
            filename=filename,
            headers=Headers({"content-type": mime_type}),
        )

    response = asyncio.run(
        app_module.create_detail_page_job(
            product_image=upload("front.png", b"front", "image/png"),
            product_images=[
                upload("side.png", b"side", "image/png"),
                upload("rear.jpg", b"rear", "image/jpeg"),
            ],
            request_id=None,
            template_id=None,
            locale="ko-KR",
            options=None,
        )
    )

    assert response["status"] == "QUEUED"
    assert service.additional_source_images == (
        (b"side", "image/png"),
        (b"rear", "image/jpeg"),
    )


def test_create_job_forwards_creator_notes_as_user_hints(monkeypatch):
    class RecordingService:
        def __init__(self):
            self.user_hints = None

        def submit(self, source_image, source_mime_type, **kwargs):
            self.user_hints = kwargs["user_hints"]
            return {
                "job_id": "job-1",
                "request_id": "request-1",
                "status": "QUEUED",
                "status_url": "/api/v1/ai/detail-page-jobs/job-1",
                "created_at": "2026-08-27T00:00:00Z",
            }

    service = RecordingService()
    monkeypatch.setattr(app_module, "get_service", lambda: service)
    upload = UploadFile(
        file=io.BytesIO(b"front"),
        filename="front.png",
        headers=Headers({"content-type": "image/png"}),
    )

    response = asyncio.run(
        app_module.create_detail_page_job(
            product_image=upload,
            product_images=None,
            product_name="나전 보관함",
            making_method="표면 장식으로 완성했습니다.",
            care_guide="마른 천으로 닦아 주세요.",
            request_id=None,
            template_id=None,
            locale="ko-KR",
            options=None,
        )
    )

    assert response["status"] == "QUEUED"
    assert service.user_hints == UserHintsDto(
        product_name="나전 보관함",
        making_method="표면 장식으로 완성했습니다.",
        care_guide="마른 천으로 닦아 주세요.",
    )


def test_create_job_does_not_expose_service_configuration_error(monkeypatch):
    def unavailable_service():
        raise ValueError("GEMINI_API_KEY secret provider detail")

    monkeypatch.setattr(app_module, "get_service", unavailable_service)
    upload = UploadFile(
        file=io.BytesIO(b"\x89PNG\r\n\x1a\nimage"),
        filename="product.png",
        headers=Headers({"content-type": "image/png"}),
    )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            app_module.create_detail_page_job(
                product_image=upload,
                product_images=None,
                request_id=None,
                template_id=None,
                locale="ko-KR",
                options=None,
            )
        )

    assert caught.value.status_code == 503
    assert caught.value.detail == "AI service is unavailable"


def test_approve_draft_renders_current_copy_without_another_ai_analysis(monkeypatch):
    class RecordingPipeline:
        def __init__(self):
            self.kwargs = None

        def run(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(
                fe_result=AiFeResultDto(
                    generation_id="generation-approved",
                    product=AiFeProductSummaryDto.from_profile(
                        ProductProfileDto.minimal("나전함")
                    ),
                    detail_page=FeDetailPageAssetDto(mime_type="image/png"),
                ),
                be_ack=None,
                backend_delivery_pending=False,
                warning=None,
            )

    class RecordingService:
        def __init__(self):
            self.pipeline = RecordingPipeline()

    service = RecordingService()
    monkeypatch.setattr(app_module, "get_service", lambda: service)
    upload = UploadFile(
        file=io.BytesIO(b"\x89PNG\r\n\x1a\nimage"),
        filename="front.png",
        headers=Headers({"content-type": "image/png"}),
    )
    draft = {
        "product_name": "수정한 나전함",
        "summary": "승인된 설명입니다.",
        "hero_headline": "승인된 핵심 문구",
        "hero_description": "승인된 핵심 설명입니다.",
        "usage_scene": "서재 선반 위",
        "features": [],
        "layout_id": "catalog-grid",
    }

    response = asyncio.run(
        app_module.approve_detail_page(
            product_image=upload,
            draft=json.dumps(draft),
            request_id="request-approved",
            options=None,
        )
    )

    assert response.status == "COMPLETED"
    assert service.pipeline.kwargs["profile_override"].display_name == "수정한 나전함"
    assert service.pipeline.kwargs["profile_override"].layout_id == "catalog-grid"


def test_health_returns_200_without_auth_token(monkeypatch):
    settings = SimpleNamespace(
        ai_internal_auth_token="strictly-required-for-internal-routes",
    )
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)

    client = TestClient(app_module.app)
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert app_module.health() == {"status": "ok"}


def test_health_ready_returns_200_when_all_servers_healthy(monkeypatch):
    settings = SimpleNamespace(
        local_text_provider="sglang",
        local_text_url="http://sglang-text:30000",
        local_image_provider="sglang",
        local_image_url="http://sglang-image:30001",
    )
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)

    called_urls = []

    def fake_get(url, timeout=None):
        called_urls.append(url)
        assert timeout == 2.0
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(app_module.httpx, "get", fake_get)

    client = TestClient(app_module.app)
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "components": {
            "text": "ok",
            "image": "ok",
        },
    }
    assert called_urls == [
        "http://sglang-text:30000/v1/models",
        "http://sglang-image:30001/v1/models",
    ]


def test_health_ready_returns_503_when_text_server_fails(monkeypatch):
    settings = SimpleNamespace(
        local_text_provider="sglang",
        local_text_url="http://sglang-text:30000",
        local_image_provider="sglang",
        local_image_url="http://sglang-image:30001",
    )
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)

    def fake_get(url, timeout=None):
        if "30000" in url:
            return SimpleNamespace(status_code=500)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(app_module.httpx, "get", fake_get)

    client = TestClient(app_module.app)
    response = client.get("/health/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert "text: HTTP 500" in payload["reason"]
    assert payload["components"]["text"] == "HTTP 500"
    assert payload["components"]["image"] == "ok"


def test_health_ready_returns_503_when_server_raises_network_error(monkeypatch):
    settings = SimpleNamespace(
        local_text_provider="sglang",
        local_text_url="http://sglang-text:30000",
        local_image_provider="sglang",
        local_image_url="http://sglang-image:30001",
    )
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)

    def fake_get(url, timeout=None):
        if "30001" in url:
            raise app_module.httpx.ConnectError("Connection refused")
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(app_module.httpx, "get", fake_get)

    client = TestClient(app_module.app)
    response = client.get("/health/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert "image: Connection refused" in payload["reason"]
    assert payload["components"]["image"] == "Connection refused"
    assert payload["components"]["text"] == "ok"


def test_health_ready_skips_image_server_when_provider_is_none(monkeypatch):
    settings = SimpleNamespace(
        local_text_provider="sglang",
        local_text_url="http://sglang-text:30000",
        local_image_provider="none",
        local_image_url="http://sglang-image:30001",
    )
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)

    called_urls = []

    def fake_get(url, timeout=None):
        called_urls.append(url)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(app_module.httpx, "get", fake_get)

    client = TestClient(app_module.app)
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "components": {
            "text": "ok",
        },
    }
    assert called_urls == ["http://sglang-text:30000/v1/models"]
    assert "http://sglang-image:30001/v1/models" not in called_urls
