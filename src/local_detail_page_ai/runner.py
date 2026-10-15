from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

from detail_page_ai.agent.detail_page_agent import DetailPageAgent
from detail_page_ai.dto import AiBePersistAck, GenerationMetadataDto, GenerationOptions
from detail_page_ai.html_renderer import HtmlDetailPageRenderer
from detail_page_ai.pipeline import DetailPagePipeline, PipelineResult
from detail_page_ai.prompts import (
    build_background_prompt,
    build_generated_detail_cut_prompt,
    build_generated_usage_scene_prompt,
    build_usage_context_background_prompt,
)
from detail_page_ai.source_photos import SourcePreservingProductPhotoGenerator

from .adapters import LocalProductAnalyzer
from .clients import (
    MlxServeChatClient,
    MlxServeImageClient,
    OllamaChatClient,
)


class LocalPreviewBackend:
    """Keeps local runs offline while exercising the real BE request builder."""

    def persist(self, request, image):
        del image
        return AiBePersistAck(
            generation_id=request.generation_id,
            product_id=None,
            status="SAVED",
            saved_at=datetime.now(timezone.utc),
        )


class MlxServeBackgroundGenerator:
    """Generate only a product-free background plate with the local Flux model."""

    def __init__(self, image_client: MlxServeImageClient):
        self.image_client = image_client

    def generate(self, *, profile, role: str, width: int, height: int) -> bytes:
        return self.image_client.generate(
            (
                build_usage_context_background_prompt(profile)
                if role == "lifestyle"
                else build_background_prompt(profile, role)
            ),
            negative_prompt=(
                "product, duplicate product, box, cabinet, vessel, container, person, "
                "hand, plate, dish, bowl, cup, cutlery, tray, text, logo, label, watermark, infographic"
            ),
            width=width,
            height=height,
        )


class MlxServeUsageSceneGenerator:
    """Generate a complete, reference-like lifestyle scene for set products."""

    def __init__(self, image_client: MlxServeImageClient):
        self.image_client = image_client

    def generate(
        self,
        *,
        profile,
        role: str,
        source_image: bytes,
        source_mime_type: str,
        width: int,
        height: int,
        direction: str | None = None,
    ) -> bytes:
        if role != "lifestyle":
            raise ValueError("generated usage scenes are restricted to lifestyle")
        prompt = build_generated_usage_scene_prompt(profile)
        if direction:
            prompt += f"\nArt direction (keep the exact product unchanged): {direction}"
        return self.image_client.edit(
            prompt,
            source_image=source_image,
            source_mime_type=source_mime_type,
            width=width,
            height=height,
            strength=0.22,
        )


class MlxServeDetailViewGenerator:
    """Generate the four non-original detail jobs when source photos are insufficient."""

    def __init__(self, image_client: MlxServeImageClient):
        self.image_client = image_client

    def generate(
        self,
        *,
        profile,
        role: str,
        source_image: bytes,
        source_mime_type: str,
        width: int,
        height: int,
        direction: str | None = None,
    ) -> bytes:
        if role not in {"detail-02", "detail-03", "detail-04", "detail-05"}:
            raise ValueError(
                "generated detail cuts are restricted to detail-02 through detail-05"
            )
        prompt = build_generated_detail_cut_prompt(profile, role)
        if direction:
            prompt += f"\nArt direction (keep the exact product unchanged): {direction}"
        return self.image_client.edit(
            prompt,
            source_image=source_image,
            source_mime_type=source_mime_type,
            width=width,
            height=height,
            strength=0.22,
        )


def build_local_pipeline(
    *,
    text_url: str = "http://127.0.0.1:11234",
    text_model: str = "ddalcu/Qwen3.8-27B-MLX-Serve-4bit",
    text_timeout: float = 300.0,
    generate_product_photos: bool = True,
    text_provider: str = "mlx",
    image_provider: str = "mlx",
    image_url: str = "http://127.0.0.1:11234",
    image_model: str = "mlx-community/flux2-klein-9b-4bit",
    image_timeout: float = 300.0,
    image_steps: int = 4,
    agent_mode: str = "pipeline",
) -> DetailPagePipeline:
    if text_provider == "mlx":
        chat_client = MlxServeChatClient(
            base_url=text_url,
            model=text_model,
            timeout=text_timeout,
        )
    elif text_provider == "ollama":
        chat_client = OllamaChatClient(
            base_url=text_url,
            model=text_model,
            timeout=text_timeout,
        )
    else:
        raise ValueError("text_provider must be 'ollama' or 'mlx'")

    if image_provider == "mlx":
        image_client = MlxServeImageClient(
            base_url=image_url,
            model=image_model,
            timeout=image_timeout,
            steps=image_steps,
        )
        background_generator = MlxServeBackgroundGenerator(image_client)
        usage_scene_generator = MlxServeUsageSceneGenerator(image_client)
        detail_view_generator = MlxServeDetailViewGenerator(image_client)
    elif image_provider == "none":
        background_generator = None
        usage_scene_generator = None
        detail_view_generator = None
    else:
        raise ValueError("image_provider must be 'none' or 'mlx'")

    pipeline_kwargs = dict(
        analyzer=LocalProductAnalyzer(
            chat_client=chat_client,
            image_generation_enabled=(
                generate_product_photos and image_provider != "none"
            ),
        ),
        photo_generator=(
            SourcePreservingProductPhotoGenerator(
                background_generator=background_generator,
                usage_scene_generator=usage_scene_generator,
                detail_view_generator=detail_view_generator,
            )
            if generate_product_photos
            else None
        ),
        renderer=HtmlDetailPageRenderer(),
        backend=LocalPreviewBackend(),
        options=GenerationOptions(),
        generation_metadata=GenerationMetadataDto(
            provider="local",
            analysis_model=text_model,
            image_model=(
                image_model
                if image_provider == "mlx"
                else "source-preserving-pillow-compositor"
            ),
            prompt_version=(
                "local-mlx-product-fit-v3-detail-guide"
                if text_provider == "mlx"
                else "local-source-safe-v2"
            ),
        ),
    )
    if agent_mode == "agent":
        if not hasattr(chat_client, "chat_with_tools"):
            raise ValueError("agent_mode='agent' needs text_provider='mlx'")
        return DetailPageAgent(chat_client=chat_client, **pipeline_kwargs)
    return DetailPagePipeline(**pipeline_kwargs)


def save_pipeline_result(result: PipelineResult, output_dir: str | Path) -> Path:
    """Save local-run page assets, React JSON, split sections, and a summary."""
    output = Path(output_dir)
    sections_dir = output / "sections"
    photos_dir = output / "photos"
    sections_dir.mkdir(parents=True, exist_ok=True)
    photos_dir.mkdir(parents=True, exist_ok=True)

    detail = result.fe_result.detail_page
    (output / "detail_page.png").write_bytes(base64.b64decode(detail.image_base64 or ""))
    for section in detail.sections:
        (sections_dir / f"{section.order:02d}-{section.section_id}.png").write_bytes(
            base64.b64decode(section.image_base64 or "")
        )
    for photo in detail.photos:
        extension = "jpg" if photo.mime_type == "image/jpeg" else "png"
        (photos_dir / f"{photo.order:02d}-{photo.photo_id}.{extension}").write_bytes(
            base64.b64decode(photo.image_base64 or "")
        )
    if detail.react_document is not None:
        (output / "react_document.json").write_text(
            json.dumps(
                detail.react_document.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                ),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    (output / "result_summary.json").write_text(
        json.dumps(
            {
                "generation_id": result.generation_id,
                "product": result.fe_result.product.model_dump(mode="json"),
                "detail_page": {
                    "mime_type": detail.mime_type,
                    "width": detail.width,
                    "height": detail.height,
                    "sections": [
                        {
                            "order": section.order,
                            "section_id": section.section_id,
                            "label": section.label,
                        }
                        for section in detail.sections
                    ],
                    "photos": [
                        {
                            "order": photo.order,
                            "photo_id": photo.photo_id,
                            "label": photo.label,
                            "asset_mode": photo.asset_mode,
                            "background_generated": photo.background_generated,
                            "product_generated": photo.product_generated,
                            "fidelity_status": photo.fidelity_status,
                        }
                        for photo in detail.photos
                    ],
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return output


def save_agent_trace(pipeline: DetailPagePipeline, output_dir: str | Path) -> Path | None:
    """Write the agent's per-mode tool trace for evaluation; no-op for the fixed pipeline."""
    last_runs = getattr(pipeline, "last_runs", None)
    if not last_runs:
        return None
    path = Path(output_dir) / "agent_trace.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(last_runs, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
