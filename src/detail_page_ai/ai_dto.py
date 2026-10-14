"""Direction-specific DTOs for the Product BE ↔ AI boundary.

The FE-facing models intentionally stay in :mod:`detail_page_ai.fe_dto`.  These
models make the production ownership boundary explicit: Product BE sends work
to AI, AI reports work to Product BE, and AI sends the generated assets back to
Product BE for persistence.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .dto import (
    AiBePersistAck,
    AiBeProductPersistRequest,
    AiFeResultDto,
    AiFeDraftResultDto,
    ApprovedDraftDto,
    AiFeStatusResponse,
    ErrorDto,
    GeneratedAssetMetadataDto,
    GenerationOptions,
    UserHintsDto,
    Status,
)


class _StrictAiBeDto(BaseModel):
    """Base for the wire contract owned by the AI service."""

    model_config = ConfigDict(extra="forbid")


class BeToAiCreateJobRequestDto(_StrictAiBeDto):
    """JSON metadata sent by Product BE with the original image multipart."""

    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(min_length=1, max_length=120)
    source_asset_id: str | None = Field(default=None, max_length=200)
    request_id: str | None = Field(default=None, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    template_id: Literal["default-long-detail-page"] = "default-long-detail-page"
    locale: Literal["ko-KR"] = "ko-KR"
    user_hints: UserHintsDto = Field(default_factory=UserHintsDto)
    options: GenerationOptions = Field(default_factory=GenerationOptions)


class BeToAiApproveDraftRequestDto(_StrictAiBeDto):
    """Creator-approved draft metadata sent by Product BE for final PNG render."""

    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(min_length=1, max_length=120)
    source_asset_id: str | None = Field(default=None, max_length=200)
    request_id: str | None = Field(default=None, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    draft_id: str = Field(min_length=1, max_length=200)
    draft: ApprovedDraftDto
    options: GenerationOptions = Field(default_factory=GenerationOptions)


class BeToAiSaveDraftRequestDto(_StrictAiBeDto):
    """Creator edits saved without invoking analysis or image generation."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str | None = Field(default=None, max_length=200)
    version: int | None = Field(default=None, ge=1)
    draft: ApprovedDraftDto


class BeToAiEditDraftRequestDto(_StrictAiBeDto):
    """Creator's natural-language edit request for the current draft."""

    instruction: str = Field(min_length=1, max_length=1000)
    version: int = Field(ge=1)


class AiToBeAcceptedResponseDto(_StrictAiBeDto):
    """Accepted response returned to Product BE for an asynchronous job."""

    product_id: str = Field(min_length=1, max_length=120)
    job_id: str
    request_id: str
    status: Literal["QUEUED"] = "QUEUED"
    status_url: str
    created_at: datetime


class AiToBeStatusResponseDto(_StrictAiBeDto):
    """Internal status projection used by Product BE polling/recovery."""

    product_id: str = Field(min_length=1, max_length=120)
    job_id: str
    request_id: str
    status: Status
    progress: int = Field(ge=0, le=100)
    draft: AiFeDraftResultDto | None = None
    result: AiFeResultDto | None = None
    error: ErrorDto | None = None
    updated_at: datetime

    @classmethod
    def from_fe_status(
        cls, *, product_id: str, response: AiFeStatusResponse
    ) -> "AiToBeStatusResponseDto":
        return cls(
            product_id=product_id,
            job_id=response.job_id,
            request_id=response.request_id,
            status=response.status,
            progress=response.progress,
            draft=response.draft,
            result=response.result,
            error=response.error,
            updated_at=response.updated_at,
        )


class AiToBeApprovedResponseDto(_StrictAiBeDto):
    """Synchronous final-render response returned to Product BE."""

    product_id: str = Field(min_length=1, max_length=120)
    status: Literal["COMPLETED", "COMPLETED_WITH_BACKEND_PENDING"]
    result: AiFeResultDto
    backend_delivery_pending: bool = False
    warning: str | None = None
    be_ack: AiBePersistAck | None = None


# The canonical production request tightens the legacy shared model by making
# Product BE identity mandatory.  The direct demo still uses the legacy model
# when no product identity exists.
class AiToBePersistRequestDto(AiBeProductPersistRequest):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    generation_id: str = Field(alias="generationId")
    job_id: str = Field(alias="jobId")
    request_id: str = Field(alias="requestId")
    idempotency_key: str = Field(alias="idempotencyKey")
    product_id: str = Field(min_length=1, max_length=120, alias="productId")
    user_hints: UserHintsDto = Field(default_factory=UserHintsDto, alias="userHints")
    detail_page: GeneratedAssetMetadataDto = Field(alias="detailPage")


class BeToAiPersistAckDto(AiBePersistAck):
    """Strict ACK returned by Product BE after AI asset persistence."""

    model_config = ConfigDict(extra="forbid")


__all__ = [
    "AiToBeAcceptedResponseDto",
    "AiToBeApprovedResponseDto",
    "AiToBePersistRequestDto",
    "AiToBeStatusResponseDto",
    "BeToAiApproveDraftRequestDto",
    "BeToAiCreateJobRequestDto",
    "BeToAiSaveDraftRequestDto",
    "BeToAiPersistAckDto",
]
