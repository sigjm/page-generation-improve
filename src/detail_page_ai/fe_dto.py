"""Direction-specific DTOs for AI output consumed by the FE through Product BE.

The FE never calls the AI service directly.  Product BE owns authentication and
its public API, while these models define the JSON projection that Product BE
may expose to the creator UI:

    FE -> Product BE -> AI -> Product BE -> FE

The draft is structured data only.  It never contains executable HTML or CSS.
Final images are represented by validated asset metadata and optional URL or
Base64 fields according to the deployment's asset policy.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .dto import (
    AiBePersistAck,
    AiFeAcceptedResponse,
    AiFeApprovedResponse,
    AiFeDraftPreviewDto as SharedAiFeDraftPreviewDto,
    AiFeDraftResultDto,
    AiFeProductSummaryDto,
    AiFeResultDto,
    AiFeStatusResponse,
    ApprovedDraftDto,
    ErrorDto,
    FeDetailPageAssetDto,
    FeDetailPageSectionDto,
    FeProductPhotoDto,
    GenerationOptions,
    UserHintsDto,
)


class _StrictAiFeDto(BaseModel):
    """Base for the JSON projection owned by the AI content contract."""

    model_config = ConfigDict(extra="forbid")


class AiFeCreatorHintsDto(UserHintsDto):
    """Creator-provided product data used as the primary source for product-specific copy."""

    model_config = ConfigDict(extra="forbid")


class AiFeGenerationOptionsDto(GenerationOptions):
    """Generation options accepted by the AI detail-page contract."""

    model_config = ConfigDict(extra="forbid")


class AiFeDraftDto(ApprovedDraftDto):
    """Editable, JSON-only draft.  Executable HTML/CSS is not accepted."""

    model_config = ConfigDict(extra="forbid")


class AiFeCreateJobRequestDto(_StrictAiFeDto):
    """Non-file fields Product BE receives from the creator UI."""

    product_name: str | None = Field(default=None, max_length=120)
    making_method: str | None = Field(default=None, max_length=2000)
    care_guide: str | None = Field(default=None, max_length=1000)
    request_id: str | None = Field(default=None, max_length=200)
    template_id: Literal["default-long-detail-page"] = "default-long-detail-page"
    locale: Literal["ko-KR"] = "ko-KR"
    options: GenerationOptions = Field(default_factory=GenerationOptions)

    @property
    def user_hints(self) -> UserHintsDto:
        """Convert creator input to the internal product-data shape."""

        return UserHintsDto(
            product_name=self.product_name,
            making_method=self.making_method,
            care_guide=self.care_guide,
        )


class AiFeApprovalRequestDto(_StrictAiFeDto):
    """Creator-approved JSON draft sent to Product BE before AI approval."""

    draft: AiFeDraftDto
    request_id: str | None = Field(default=None, max_length=200)
    options: GenerationOptions = Field(default_factory=GenerationOptions)


class AiFeProductSummaryResponseDto(AiFeProductSummaryDto):
    """Validated product analysis and copy projection."""

    model_config = ConfigDict(extra="forbid")


class AiFeDetailPageSectionResponseDto(FeDetailPageSectionDto):
    """One ordered section PNG reference."""

    model_config = ConfigDict(extra="forbid")


class AiFeProductPhotoResponseDto(FeProductPhotoDto):
    """One source, crop, composite, or explicitly labelled generated photo."""

    model_config = ConfigDict(extra="forbid")


class AiFeDetailPageAssetResponseDto(FeDetailPageAssetDto):
    """Final full-page asset plus ordered section and product-photo metadata."""

    model_config = ConfigDict(extra="forbid")
    sections: list[AiFeDetailPageSectionResponseDto] = Field(default_factory=list)
    photos: list[AiFeProductPhotoResponseDto] = Field(default_factory=list)


class AiFeResultResponseDto(AiFeResultDto):
    """Final AI result projected to Product BE/FE."""

    model_config = ConfigDict(extra="forbid")
    product: AiFeProductSummaryResponseDto
    detail_page: AiFeDetailPageAssetResponseDto


class AiFeDraftResponseDto(AiFeDraftResultDto):
    """Editable draft and source-image preview returned before approval."""

    model_config = ConfigDict(extra="forbid")
    product: AiFeProductSummaryResponseDto
    draft: AiFeDraftDto
    preview: SharedAiFeDraftPreviewDto


class DraftEditChangeDto(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str
    before: str | None = None
    after: str | None = None
    evidence: str | None = None


class AiFeDraftEditResponseDto(BaseModel):
    """Draft after a conversational edit, with what changed and why not."""

    model_config = ConfigDict(extra="forbid")

    draft: AiFeDraftResponseDto
    reply: str
    changes: list[DraftEditChangeDto]
    warnings: list[str]


class AiFeErrorDto(ErrorDto):
    """Stable public error shape without provider internals."""

    model_config = ConfigDict(extra="forbid")


class AiFeJobAcceptedResponseDto(AiFeAcceptedResponse):
    """Accepted job response exposed through Product BE."""

    model_config = ConfigDict(extra="forbid")


class AiFeJobStatusResponseDto(AiFeStatusResponse):
    """Polling response containing either an editable draft or final result."""

    model_config = ConfigDict(extra="forbid")
    draft: AiFeDraftResponseDto | None = None
    result: AiFeResultResponseDto | None = None
    error: AiFeErrorDto | None = None


class AiFeApprovedResponseDto(AiFeApprovedResponse):
    """Final approval response; Product BE ACK fields remain optional."""

    model_config = ConfigDict(extra="forbid")
    result: AiFeResultResponseDto
    be_ack: AiBePersistAck | None = None


# Product BE may use these aliases when exposing its own public FE endpoints.
# They are intentionally separate from the AI-BE DTOs and contain no internal
# authentication or provider configuration.
FeToProductBeCreateDetailPageRequestDto = AiFeCreateJobRequestDto
FeToProductBeApprovalRequestDto = AiFeApprovalRequestDto
ProductBeToFeAcceptedResponseDto = AiFeJobAcceptedResponseDto
ProductBeToFeStatusResponseDto = AiFeJobStatusResponseDto
ProductBeToFeApprovedResponseDto = AiFeApprovedResponseDto


__all__ = [
    "AiFeApprovalRequestDto",
    "AiFeApprovedResponseDto",
    "AiFeCreateJobRequestDto",
    "AiFeCreatorHintsDto",
    "AiFeDetailPageAssetResponseDto",
    "AiFeDetailPageSectionResponseDto",
    "AiFeDraftDto",
    "AiFeDraftResponseDto",
    "AiFeErrorDto",
    "AiFeGenerationOptionsDto",
    "AiFeJobAcceptedResponseDto",
    "AiFeJobStatusResponseDto",
    "AiFeProductPhotoResponseDto",
    "AiFeProductSummaryResponseDto",
    "AiFeResultResponseDto",
    "FeToProductBeApprovalRequestDto",
    "FeToProductBeCreateDetailPageRequestDto",
    "ProductBeToFeAcceptedResponseDto",
    "ProductBeToFeApprovedResponseDto",
    "ProductBeToFeStatusResponseDto",
]
