from pathlib import Path

from detail_page_ai.agent.detail_page_agent import DetailPageAgent
from detail_page_ai.assets import LocalFileAssetStore
from detail_page_ai.backend_client import (
    DEFAULT_BACKEND_CALLBACK_PATH,
    BackendDeliveryError,
    BackendProductClient,
)
from detail_page_ai.dto import GenerationMetadataDto
from detail_page_ai.html_renderer import HtmlDetailPageRenderer
from detail_page_ai.persistence import SQLiteDeliveryOutbox, SQLiteJobRepository
from detail_page_ai.pipeline import DetailPagePipeline
from detail_page_ai.service import DetailPageJobService
from detail_page_ai.source_photos import SourcePreservingProductPhotoGenerator

from .adapters import LocalProductAnalyzer
from .clients import (
    MlxServeChatClient,
    MlxServeImageClient,
    OllamaChatClient,
    SglangImageClient,
)
from .runner import (
    MlxServeBackgroundGenerator,
    MlxServeDetailViewGenerator,
    MlxServeUsageSceneGenerator,
)


class _UnconfiguredBackend:
    def persist(self, request, image):
        raise BackendDeliveryError(
            "BACKEND_URL is not configured", retryable=True
        )


def build_service(settings) -> DetailPageJobService:
    """Assemble the production service from the local model adapters."""
    template_image = None
    if settings.detail_page_template_path:
        template_image = Path(settings.detail_page_template_path).read_bytes()

    if settings.analysis_provider != "local":
        raise ValueError("ANALYSIS_PROVIDER must be local")
    if settings.local_text_provider in {"mlx", "sglang"}:
        chat_client = MlxServeChatClient(
            base_url=settings.local_text_url,
            model=settings.local_text_model,
            timeout=settings.local_text_timeout_seconds,
        )
    elif settings.local_text_provider == "ollama":
        chat_client = OllamaChatClient(
            base_url=settings.local_text_url,
            model=settings.local_text_model,
            timeout=settings.local_text_timeout_seconds,
        )
    else:
        raise ValueError("LOCAL_TEXT_PROVIDER must be 'mlx', 'ollama', or 'sglang'")
    image_generation_enabled = (
        getattr(settings, "local_image_provider", "none") != "none"
        and getattr(settings, "background_provider", "none") != "none"
        and getattr(settings, "max_generated_photos", 5) > 0
    )
    analyzer = LocalProductAnalyzer(
        chat_client=chat_client,
        image_generation_enabled=image_generation_enabled,
    )
    analysis_model = settings.local_text_model
    asset_store = LocalFileAssetStore(
        getattr(settings, "asset_store_dir", ".local/detail-page-ai/assets")
    )
    sqlite_path = getattr(
        settings, "sqlite_path", ".local/detail-page-ai/state.sqlite3"
    )
    outbox = SQLiteDeliveryOutbox(sqlite_path)
    repository = SQLiteJobRepository(sqlite_path)
    photo_provider = getattr(settings, "product_photo_generation", "source")
    if photo_provider != "source":
        raise ValueError(
            "Original product pixels are immutable; set "
            "PRODUCT_PHOTO_GENERATION=source. Local Flux may generate backgrounds only."
        )
    configured_shots = tuple(
        shot.strip()
        for shot in getattr(
            settings,
            "product_photo_shots",
            "hero,packshot,detail,lifestyle",
        ).split(",")
        if shot.strip()
    )
    if not configured_shots or any(
        shot not in {"hero", "packshot", "detail", "lifestyle", "scale"}
        for shot in configured_shots
    ):
        raise ValueError(
            "PRODUCT_PHOTO_SHOTS must contain only hero, packshot, detail, lifestyle, scale"
        )
    background_generator = None
    usage_scene_generator = None
    detail_view_generator = None
    image_model = "source-preserving-pillow-compositor"
    if settings.background_provider in {"mlx", "sglang"}:
        if settings.local_image_provider != settings.background_provider:
            raise ValueError(
                "BACKGROUND_PROVIDER must match LOCAL_IMAGE_PROVIDER when image "
                "generation is enabled"
            )
        if settings.local_image_provider == "mlx":
            image_client = MlxServeImageClient(
                base_url=settings.local_image_url,
                model=settings.local_image_model,
                timeout=settings.local_image_timeout_seconds,
            )
        else:
            image_client = SglangImageClient(
                base_url=settings.local_image_url,
                model=settings.local_image_model,
                timeout=settings.local_image_timeout_seconds,
            )
        background_generator = MlxServeBackgroundGenerator(image_client)
        usage_scene_generator = MlxServeUsageSceneGenerator(image_client)
        detail_view_generator = MlxServeDetailViewGenerator(image_client)
        image_model = settings.local_image_model
    elif settings.local_image_provider not in {"none", "mlx", "sglang"}:
        raise ValueError("LOCAL_IMAGE_PROVIDER must be 'none', 'mlx', or 'sglang'")
    photo_generator = SourcePreservingProductPhotoGenerator(
        asset_store=asset_store,
        background_generator=background_generator,
        usage_scene_generator=usage_scene_generator,
        detail_view_generator=detail_view_generator,
        include_scale="scale" in configured_shots,
        photo_roles=configured_shots,
        max_generated_photos=getattr(
            settings, "max_generated_photos", 5
        ),
    )
    if settings.detail_page_renderer != "html":
        raise ValueError(
            "Original product pixels are immutable; set DETAIL_PAGE_RENDERER=html."
        )
    renderer = HtmlDetailPageRenderer()
    backend = (
        BackendProductClient(
            url=settings.backend_url,
            token=settings.backend_auth_token,
            timeout=settings.backend_timeout_seconds,
            callback_path=getattr(
                settings,
                "backend_callback_path",
                DEFAULT_BACKEND_CALLBACK_PATH,
            ),
        )
        if settings.backend_url
        else _UnconfiguredBackend()
    )
    agent_mode = getattr(settings, "agent_mode", "pipeline")
    if agent_mode == "agent" and not hasattr(chat_client, "chat_with_tools"):
        raise ValueError(
            "AGENT_MODE=agent needs LOCAL_TEXT_PROVIDER mlx or sglang; "
            "set AGENT_MODE=pipeline for ollama"
        )
    pipeline_kwargs = dict(
        analyzer=analyzer,
        photo_generator=photo_generator,
        renderer=renderer,
        backend=backend,
        template_image=template_image,
        generation_metadata=GenerationMetadataDto(
            provider=settings.analysis_provider,
            analysis_model=analysis_model,
            image_model=image_model,
            prompt_version=settings.prompt_version,
        ),
        outbox=outbox,
        asset_store=asset_store,
        response_asset_mode=getattr(settings, "response_asset_mode", "base64"),
        craft_confidence_threshold=getattr(
            settings, "craft_confidence_threshold", 0.65
        ),
        max_image_bytes=getattr(settings, "max_image_bytes", 10 * 1024 * 1024),
        max_source_images=getattr(settings, "max_source_images", 12),
        max_total_input_bytes=getattr(
            settings, "max_request_bytes", 120 * 1024 * 1024
        ),
        max_pending_generations=getattr(settings, "max_pending_generations", 100),
        require_decodable_images=getattr(
            settings, "require_decodable_images", True
        ),
    )
    pipeline = (
        DetailPageAgent(
            chat_client=chat_client,
            max_tool_calls=getattr(settings, "agent_max_tool_calls", 15),
            timeout_seconds=getattr(settings, "agent_timeout_seconds", 600.0),
            **pipeline_kwargs,
        )
        if agent_mode == "agent"
        else DetailPagePipeline(**pipeline_kwargs)
    )
    return DetailPageJobService(
        pipeline=pipeline,
        repository=repository,
        max_pending_generations=getattr(settings, "max_pending_generations", 100),
        max_image_bytes=getattr(settings, "max_image_bytes", 10 * 1024 * 1024),
        max_source_images=getattr(settings, "max_source_images", 12),
        max_total_input_bytes=getattr(
            settings, "max_request_bytes", 120 * 1024 * 1024
        ),
        max_delivery_attempts=getattr(settings, "max_delivery_attempts", 8),
        require_decodable_images=getattr(
            settings, "require_decodable_images", True
        ),
    )
