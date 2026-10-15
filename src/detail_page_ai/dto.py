from datetime import datetime, timezone
from typing import Any, Literal, Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import (
    AssetMode,
    FidelityStatus,
    GenerationOptions as GenerationOptions,
    PhotoTransform,
)
from .react_document import ReactDetailPageDocumentDto


Status = Literal[
    "QUEUED",
    "ANALYZING",
    "EXTRACTING",
    "GENERATING_BACKGROUNDS",
    "COMPOSING",
    "VERIFYING",
    "RENDERING",
    "DELIVERING",
    "DRAFT_READY",
    "COMPLETED",
    "FAILED",
]
LayoutId = Literal["editorial-split", "image-first", "catalog-grid"]
# creator-provided: taken from the artisan's input (user_hints), not seen in the photo.
Evidence = Literal["image-visible", "creator-provided", "inferred", "unknown"]


class ProductFeatureDto(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    evidence: Evidence = "image-visible"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class CopySectionDto(BaseModel):
    section_type: Literal["hero", "benefit", "feature", "usage", "closing"]
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    evidence: Evidence = "image-visible"


PageBlockType = Literal[
    "hero",
    "statement",
    "feature_grid",
    "detail_split",
    "wide_image",
    "gallery",
    "usage_scene",
    "scale_reference",
    "palette",
    "recommendation",
    "info_table",
    "notice",
    "closing",
]
PageBlockVariant = Literal[
    "paper",
    "light",
    "sand",
    "dark",
    "image-left",
    "image-right",
    "full-bleed",
    "compact",
]


class PageBlockItemDto(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    value: str = Field(default="", max_length=180)
    description: str = Field(default="", max_length=300)
    evidence: Evidence = "image-visible"


class PageBlockDto(BaseModel):
    """Whitelisted JSON block used to compose an adaptive detail page."""

    section_id: str = Field(min_length=1, max_length=80)
    block_type: PageBlockType
    eyebrow: str = Field(default="", max_length=80)
    title: str = Field(default="", max_length=120)
    body: str = Field(default="", max_length=500)
    variant: PageBlockVariant = "paper"
    photo_id: str | None = Field(default=None, max_length=80)
    photo_ids: list[str] = Field(default_factory=list, max_length=8)
    items: list[PageBlockItemDto] = Field(default_factory=list, max_length=8)


class CraftResearchSourceDto(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)

    @field_validator("url")
    @classmethod
    def require_https_source(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("research sources must use an HTTPS URL")
        return value


class CraftCharacteristicDto(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=400)
    kind: Literal["technique", "material", "visual", "cultural"]
    source_urls: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("source_urls")
    @classmethod
    def require_https_sources(cls, values: list[str]) -> list[str]:
        for value in values:
            parsed = urlparse(value)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("research sources must use HTTPS URLs")
        return values


class UserHintsDto(BaseModel):
    """Creator-provided product data paired with the source image.

    These fields are the primary source for product-specific copy. They remain
    separate from image observations so the API can preserve provenance, while
    the supplied values take precedence over visual interpretation.
    """

    model_config = ConfigDict(extra="forbid")

    product_name: str | None = Field(default=None, max_length=120)
    making_method: str | None = Field(default=None, max_length=2000)
    care_guide: str | None = Field(default=None, max_length=1000)

    @field_validator("product_name", "making_method", "care_guide", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        return normalized or None


class CraftResearchDto(BaseModel):
    craft_type: str = Field(min_length=1, max_length=120)
    region: str | None = Field(default=None, max_length=120)
    overview: str = Field(min_length=1, max_length=500)
    characteristics: list[CraftCharacteristicDto] = Field(default_factory=list, max_length=6)
    techniques: list[str] = Field(default_factory=list, max_length=8)
    materials: list[str] = Field(default_factory=list, max_length=8)
    sources: list[CraftResearchSourceDto] = Field(default_factory=list, max_length=8)


class ProductProfileDto(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_type: str = Field(min_length=1, max_length=120)
    display_name: str | None = Field(default=None, max_length=120)
    is_traditional_craft: bool = False
    craft_type: str | None = Field(default=None, max_length=120)
    classification_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    craft_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    classification_reason: str = Field(default="", max_length=500)
    candidate_types: list[str] = Field(default_factory=list, max_length=8)
    layout_id: LayoutId = "editorial-split"
    page_plan: list[PageBlockDto] = Field(default_factory=list, max_length=14)
    summary: str = Field(min_length=1, max_length=500)
    keywords: list[str] = Field(default_factory=list, max_length=8)
    observations: dict[str, Any] = Field(default_factory=dict)
    features: list[ProductFeatureDto] = Field(default_factory=list, max_length=5)
    copy_sections: list[CopySectionDto] = Field(default_factory=list, max_length=8)
    usage_scene: str = Field(default="", max_length=300)
    uncertain_information: list[str] = Field(default_factory=list, max_length=12)
    safety_notes: list[str] = Field(default_factory=list, max_length=12)
    craft_research: CraftResearchDto | None = None

    @classmethod
    def minimal(cls, product_type: str) -> Self:
        return cls(
            product_type=product_type,
            display_name=None,
            summary=f"이미지에서 확인되는 {product_type} 제품",
            keywords=[],
            observations={"colors": [], "shape": "", "visible_components": []},
            features=[],
            copy_sections=[],
            usage_scene="제품이 자연스럽게 사용되는 일상적인 장면",
            uncertain_information=["이미지로 확인되지 않는 성능·수치·정확한 소재"],
            safety_notes=["이미지에서 확인되지 않는 정보를 사실처럼 표현하지 않음"],
        )


class ApprovedDraftDto(BaseModel):
    """Creator-approved copy and layout used for the final, non-AI rerender."""

    model_config = ConfigDict(extra="forbid")

    product_name: str = Field(min_length=1, max_length=120)
    product_type: str | None = Field(default=None, max_length=120)
    summary: str = Field(min_length=1, max_length=500)
    hero_headline: str = Field(min_length=1, max_length=80)
    hero_description: str = Field(min_length=1, max_length=300)
    usage_scene: str = Field(default="", max_length=300)
    features: list[ProductFeatureDto] = Field(default_factory=list, max_length=3)
    keywords: list[str] = Field(default_factory=list, max_length=8)
    layout_id: LayoutId = "editorial-split"
    page_plan: list[PageBlockDto] = Field(default_factory=list, max_length=14)

    @classmethod
    def from_profile(cls, profile: ProductProfileDto) -> Self:
        hero = next(
            (section for section in profile.copy_sections if section.section_type == "hero"),
            None,
        )
        return cls(
            product_name=profile.display_name or profile.product_type,
            product_type=profile.product_type,
            summary=profile.summary,
            hero_headline=hero.title if hero else "이미지에서 발견한 제품의 특징",
            hero_description=hero.description if hero else profile.summary,
            usage_scene=profile.usage_scene,
            features=[
                feature for feature in profile.features
                if feature.evidence in {"image-visible", "creator-provided"}
            ][:3],
            keywords=profile.keywords,
            layout_id=profile.layout_id,
            page_plan=profile.page_plan,
        )

    def to_profile(self, base_profile: ProductProfileDto | None = None) -> ProductProfileDto:
        """Merge creator edits into the analyzed profile without losing evidence."""
        base = base_profile or ProductProfileDto.minimal(self.product_type or self.product_name)
        existing_non_hero = [
            section for section in base.copy_sections if section.section_type != "hero"
        ]
        hero = CopySectionDto(
            section_type="hero",
            title=self.hero_headline,
            description=self.hero_description,
            evidence="image-visible",
        )
        return base.model_copy(
            update={
                "product_type": self.product_type or base.product_type,
                "display_name": self.product_name,
                "layout_id": self.layout_id,
                "summary": self.summary,
                "keywords": self.keywords,
                "features": self.features,
                "copy_sections": [hero, *existing_non_hero],
                "usage_scene": self.usage_scene or base.usage_scene,
                "page_plan": self.page_plan or base.page_plan,
            }
        )


class AiFeProductSummaryDto(BaseModel):
    product_type: str
    display_name: str | None
    is_traditional_craft: bool = False
    craft_type: str | None = None
    classification_confidence: float = 0.0
    craft_confidence: float = 0.0
    classification_reason: str = ""
    candidate_types: list[str] = Field(default_factory=list)
    layout_id: LayoutId = "editorial-split"
    page_plan: list[PageBlockDto] = Field(default_factory=list, max_length=14)
    observations: dict[str, Any] = Field(default_factory=dict)
    summary: str
    keywords: list[str]
    features: list[ProductFeatureDto]
    warnings: list[str]
    craft_research: CraftResearchDto | None = None

    @classmethod
    def from_profile(cls, profile: ProductProfileDto) -> Self:
        return cls(
            product_type=profile.product_type,
            display_name=profile.display_name,
            is_traditional_craft=profile.is_traditional_craft,
            craft_type=profile.craft_type,
            classification_confidence=profile.classification_confidence,
            craft_confidence=profile.craft_confidence,
            classification_reason=profile.classification_reason,
            candidate_types=profile.candidate_types,
            layout_id=profile.layout_id,
            page_plan=profile.page_plan,
            observations=profile.observations,
            summary=profile.summary,
            keywords=profile.keywords,
            features=profile.features,
            warnings=[*profile.uncertain_information, *profile.safety_notes],
            craft_research=profile.craft_research,
        )


class FeDetailPageSectionDto(BaseModel):
    section_id: str = Field(min_length=1, max_length=80)
    order: int = Field(ge=1)
    label: str = Field(min_length=1, max_length=120)
    image_url: str | None = None
    image_base64: str | None = None
    mime_type: str
    width: int | None = None
    height: int | None = None


class FeProductPhotoDto(BaseModel):
    photo_id: str = Field(min_length=1, max_length=80)
    order: int = Field(ge=1)
    label: str = Field(min_length=1, max_length=120)
    image_url: str | None = None
    image_base64: str | None = None
    mime_type: str
    width: int | None = None
    height: int | None = None
    asset_mode: AssetMode = "source"
    source_asset_id: str | None = None
    source_sha256: str | None = None
    cutout_sha256: str | None = None
    mask_sha256: str | None = None
    background_generated: bool = False
    product_generated: bool = False
    transform: PhotoTransform = Field(default_factory=PhotoTransform)
    fidelity_status: FidelityStatus = "VERIFIED"


class PhotoGenerationFailureDto(BaseModel):
    model_config = ConfigDict(extra="forbid")

    photo_id: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=200)


class FeDetailPageAssetDto(BaseModel):
    image_url: str | None = None
    image_base64: str | None = None
    mime_type: str
    width: int | None = None
    height: int | None = None
    sections: list[FeDetailPageSectionDto] = Field(default_factory=list)
    photos: list[FeProductPhotoDto] = Field(default_factory=list)
    photo_generation_failures: list[PhotoGenerationFailureDto] = Field(
        default_factory=list
    )
    unused_generated_photo_ids: list[str] = Field(default_factory=list)
    react_document: ReactDetailPageDocumentDto | None = None


class AiFeResultDto(BaseModel):
    generation_id: str
    product: AiFeProductSummaryDto
    detail_page: FeDetailPageAssetDto


class AiFeDraftPreviewDto(BaseModel):
    """Source image reference used by Product BE/FE to render a local preview.

    The preview contains data, not executable markup.  Product BE/FE owns the
    preview template and renders it from ``draft`` and this image reference.
    """

    model_config = ConfigDict(extra="forbid")

    source_asset_id: str | None = None
    source_sha256: str
    mime_type: str
    image_url: str | None = None
    image_base64: str | None = None


class AiFeDraftResultDto(BaseModel):
    """Structured editable draft returned before final PNG/photo generation."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    generation_id: str
    version: int = Field(default=1, ge=1)
    source_mime_type: str
    source_sha256: str
    source_asset_id: str | None = None
    product: AiFeProductSummaryDto
    draft: ApprovedDraftDto
    preview: AiFeDraftPreviewDto
    preview_photos: list[FeProductPhotoDto] = Field(default_factory=list)
    photo_generation_failures: list[PhotoGenerationFailureDto] = Field(
        default_factory=list
    )
    unused_generated_photo_ids: list[str] = Field(default_factory=list)
    react_document: ReactDetailPageDocumentDto | None = None


class ErrorDto(BaseModel):
    code: str
    message: str
    retryable: bool
    request_id: str | None = None
    generation_id: str | None = None
    details: list[dict[str, Any]] = Field(default_factory=list)


class AiFeAcceptedResponse(BaseModel):
    job_id: str
    request_id: str
    status: Literal["QUEUED"] = "QUEUED"
    status_url: str
    created_at: datetime


class AiFeStatusResponse(BaseModel):
    job_id: str
    request_id: str
    status: Status
    progress: int = Field(ge=0, le=100)
    draft: AiFeDraftResultDto | None = None
    result: AiFeResultDto | None = None
    error: ErrorDto | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def completed(
        cls,
        *,
        job_id: str,
        request_id: str,
        generation_id: str,
        profile: ProductProfileDto,
        image_base64: str,
        mime_type: str,
        image_url: str | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> Self:
        from .react_document_builder import build_react_document_from_draft

        react_document = build_react_document_from_draft(
            ApprovedDraftDto.from_profile(profile)
        )
        return cls(
            job_id=job_id,
            request_id=request_id,
            status="COMPLETED",
            progress=100,
            result=AiFeResultDto(
                generation_id=generation_id,
                product=AiFeProductSummaryDto.from_profile(profile),
                detail_page=FeDetailPageAssetDto(
                    image_url=image_url,
                    image_base64=image_base64,
                    mime_type=mime_type,
                    width=width,
                    height=height,
                    react_document=react_document,
                ),
            ),
        )


class SourceImageDto(BaseModel):
    type: Literal["image-only"] = "image-only"
    mime_type: str
    sha256: str
    asset_id: str | None = None


class GeneratedSectionMetadataDto(BaseModel):
    section_id: str = Field(min_length=1, max_length=80)
    order: int = Field(ge=1)
    label: str = Field(min_length=1, max_length=120)
    mime_type: str
    width: int | None = None
    height: int | None = None
    sha256: str
    asset_id: str | None = None


class GeneratedPhotoMetadataDto(BaseModel):
    photo_id: str = Field(min_length=1, max_length=80)
    order: int = Field(ge=1)
    label: str = Field(min_length=1, max_length=120)
    mime_type: str
    width: int | None = None
    height: int | None = None
    sha256: str
    asset_id: str | None = None
    asset_mode: AssetMode = "source"
    source_asset_id: str | None = None
    source_sha256: str | None = None
    cutout_sha256: str | None = None
    mask_sha256: str | None = None
    background_generated: bool = False
    product_generated: bool = False
    transform: PhotoTransform = Field(default_factory=PhotoTransform)
    fidelity_status: FidelityStatus = "VERIFIED"


class GeneratedAssetMetadataDto(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mime_type: str = Field(alias="mimeType")
    width: int | None = None
    height: int | None = None
    sha256: str
    asset_id: str | None = Field(default=None, alias="assetId")
    sections: list[GeneratedSectionMetadataDto] = Field(default_factory=list)
    photos: list[GeneratedPhotoMetadataDto] = Field(default_factory=list)
    photo_generation_failures: list[PhotoGenerationFailureDto] = Field(
        default_factory=list
    )
    unused_generated_photo_ids: list[str] = Field(default_factory=list)
    react_document: ReactDetailPageDocumentDto | None = Field(
        default=None, alias="reactDocument"
    )


class GenerationMetadataDto(BaseModel):
    provider: str = "local"
    analysis_model: str = "ddalcu/Qwen3.8-27B-MLX-Serve-4bit"
    image_model: str = "mlx-community/flux2-klein-9b-4bit"
    aspect_ratio: str = "1:4"
    image_size: str = "2K"
    layout_id: LayoutId = "editorial-split"
    prompt_version: str = "detail-page-v1"
    research_model: str | None = None
    research_used: bool = False
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AiBeProductPersistRequest(BaseModel):
    product_id: str | None = Field(default=None, min_length=1, max_length=120)
    generation_id: str
    job_id: str
    request_id: str
    idempotency_key: str
    source: SourceImageDto
    user_hints: UserHintsDto = Field(default_factory=UserHintsDto)
    product: ProductProfileDto
    detail_page: GeneratedAssetMetadataDto
    generation: GenerationMetadataDto

    @classmethod
    def from_profile(
        cls,
        *,
        generation_id: str,
        job_id: str,
        request_id: str,
        source_image_mime_type: str,
        source_image_sha256: str,
        source_asset_id: str | None = None,
        generated_image_mime_type: str,
        generated_width: int | None,
        generated_height: int | None,
        generated_image_sha256: str,
        generated_asset_id: str | None = None,
        user_hints: UserHintsDto | None = None,
        profile: ProductProfileDto,
        generation: dict[str, Any] | GenerationMetadataDto,
        generated_sections: list[GeneratedSectionMetadataDto] | None = None,
        generated_photos: list[GeneratedPhotoMetadataDto] | None = None,
        photo_generation_failures: list[PhotoGenerationFailureDto] | None = None,
        unused_generated_photo_ids: list[str] | None = None,
        react_document: ReactDetailPageDocumentDto | None = None,
        product_id: str | None = None,
    ) -> Self:
        generation_metadata = (
            generation
            if isinstance(generation, GenerationMetadataDto)
            else GenerationMetadataDto.model_validate(generation)
        )
        return cls(
            product_id=product_id,
            generation_id=generation_id,
            job_id=job_id,
            request_id=request_id,
            idempotency_key=generation_id,
            source=SourceImageDto(
                mime_type=source_image_mime_type,
                sha256=source_image_sha256,
                asset_id=source_asset_id,
            ),
            user_hints=user_hints or UserHintsDto(),
            product=profile,
            detail_page=GeneratedAssetMetadataDto(
                mime_type=generated_image_mime_type,
                width=generated_width,
                height=generated_height,
                sha256=generated_image_sha256,
                asset_id=generated_asset_id,
                sections=generated_sections or [],
                photos=generated_photos or [],
                photo_generation_failures=photo_generation_failures or [],
                unused_generated_photo_ids=unused_generated_photo_ids or [],
                react_document=react_document,
            ),
            generation=generation_metadata,
        )


class AiBePersistAck(BaseModel):
    generation_id: str
    product_id: str | None = None
    status: Literal["SAVED", "ALREADY_SAVED"]
    saved_at: datetime


class AiFeApprovedResponse(BaseModel):
    status: Literal["COMPLETED", "COMPLETED_WITH_BACKEND_PENDING"]
    result: AiFeResultDto
    backend_delivery_pending: bool = False
    warning: str | None = None
    be_ack: AiBePersistAck | None = None
