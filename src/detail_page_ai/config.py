from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # The service is intentionally local-only.  Keeping the provider as a
    # one-value setting makes stale cloud configuration fail at startup instead
    # of silently selecting a remote model.
    analysis_provider: Literal["local"] = Field(
        default="local", alias="ANALYSIS_PROVIDER"
    )
    local_text_provider: Literal["mlx", "ollama", "sglang"] = Field(
        default="mlx", alias="LOCAL_TEXT_PROVIDER"
    )
    local_text_url: str = Field(
        default="http://127.0.0.1:11234", alias="LOCAL_TEXT_URL"
    )
    local_text_model: str = Field(
        default="ddalcu/Qwen3.8-27B-MLX-Serve-4bit", alias="LOCAL_TEXT_MODEL"
    )
    local_text_timeout_seconds: float = Field(
        default=300.0, alias="LOCAL_TEXT_TIMEOUT", gt=0
    )
    local_image_provider: Literal["none", "mlx", "sglang"] = Field(
        default="mlx", alias="LOCAL_IMAGE_PROVIDER"
    )
    local_image_url: str = Field(
        default="http://127.0.0.1:11234", alias="LOCAL_IMAGE_URL"
    )
    local_image_model: str = Field(
        default="mlx-community/flux2-klein-9b-4bit", alias="LOCAL_IMAGE_MODEL"
    )
    local_image_timeout_seconds: float = Field(
        default=300.0, alias="LOCAL_IMAGE_TIMEOUT", gt=0
    )
    # FLUX.2 klein is distilled to 4 steps; Qwen-Image 2.1 needs about 20.
    local_image_steps: int = Field(default=4, alias="LOCAL_IMAGE_STEPS", ge=1, le=100)
    product_photo_generation: Literal["source"] = Field(
        default="source", alias="PRODUCT_PHOTO_GENERATION"
    )
    background_provider: Literal["none", "mlx", "sglang"] = Field(
        default="mlx", alias="BACKGROUND_PROVIDER"
    )
    product_photo_shots: str = Field(
        default="hero,packshot,detail,lifestyle",
        alias="PRODUCT_PHOTO_SHOTS",
    )
    max_generated_photos: int = Field(
        default=5, alias="MAX_GENERATED_PHOTOS", ge=0, le=12
    )
    detail_page_renderer: Literal["html"] = Field(
        default="html", alias="DETAIL_PAGE_RENDERER"
    )
    backend_url: str | None = Field(
        default=None, alias="BACKEND_URL"
    )
    backend_callback_path: str = Field(
        default="/internal/generations/{generation_id}/completion",
        alias="BACKEND_CALLBACK_PATH",
    )
    backend_auth_token: str | None = Field(
        default=None, alias="BACKEND_AUTH_TOKEN"
    )
    backend_timeout_seconds: float = Field(
        default=60.0, alias="BACKEND_TIMEOUT_SECONDS", gt=0
    )
    ai_internal_auth_token: str | None = Field(
        default=None, alias="AI_INTERNAL_AUTH_TOKEN"
    )
    detail_page_template_path: str | None = Field(
        default=None, alias="DETAIL_PAGE_TEMPLATE_PATH"
    )
    max_image_bytes: int = Field(
        default=10 * 1024 * 1024, alias="MAX_IMAGE_BYTES", gt=0
    )
    require_decodable_images: bool = Field(
        default=True, alias="REQUIRE_DECODABLE_IMAGES"
    )
    max_source_images: int = Field(
        default=12, alias="MAX_SOURCE_IMAGES", ge=1, le=50
    )
    max_request_bytes: int = Field(
        default=120 * 1024 * 1024, alias="MAX_REQUEST_BYTES", gt=0
    )
    max_pending_generations: int = Field(
        default=100, alias="MAX_PENDING_GENERATIONS", ge=1
    )
    max_delivery_attempts: int = Field(
        default=8, alias="MAX_DELIVERY_ATTEMPTS", ge=1, le=100
    )
    enable_legacy_demo_api: bool = Field(
        default=False, alias="ENABLE_LEGACY_DEMO_API"
    )
    cors_origins: str = Field(
        default="http://127.0.0.1:4173,http://localhost:4173",
        alias="AI_CORS_ORIGINS",
    )
    prompt_version: str = Field(
        default="local-mlx-qwen-flux-v1", alias="PROMPT_VERSION"
    )
    asset_store_dir: str = Field(
        default=".local/detail-page-ai/assets", alias="ASSET_STORE_DIR"
    )
    sqlite_path: str = Field(
        default=".local/detail-page-ai/state.sqlite3", alias="SQLITE_PATH"
    )
    response_asset_mode: Literal["base64", "url", "both"] = Field(
        default="base64", alias="RESPONSE_ASSET_MODE"
    )
    craft_confidence_threshold: float = Field(
        default=0.65, alias="CRAFT_CONFIDENCE_THRESHOLD", ge=0, le=1
    )
    agent_mode: Literal["agent", "pipeline"] = Field(
        default="agent", alias="AGENT_MODE"
    )
    agent_max_tool_calls: int = Field(
        default=15, alias="AGENT_MAX_TOOL_CALLS", ge=1, le=50
    )
    agent_timeout_seconds: float = Field(
        default=600.0, alias="AGENT_TIMEOUT_SECONDS", gt=0
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
