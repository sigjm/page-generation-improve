"""Tools the agent can call, grouped by mode. Each wraps existing pipeline code."""

import io
from dataclasses import replace
from typing import Any, Callable, Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from ..dto import (
    ApprovedDraftDto,
    LayoutId,
    PageBlockType,
    PageBlockVariant,
    ProductProfileDto,
    UserHintsDto,
)
from ..models import GeneratedImage, PhotoGenerationFailure, ProductPhoto, ProductPhotoSet
from ..react_document_builder import build_react_document_from_draft
from ..validation import ensure_editorial_page_plan, is_grounded_copy, sanitize_page_plan
from . import prompts
from .loop import AgentState, Tool


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(_Args):
    pass


class InspectPhotoArgs(_Args):
    index: int = Field(ge=0, description="0은 대표 사진, 1부터 추가 사진")
    question: str = Field(min_length=1, max_length=300)


class ReviseCopyArgs(_Args):
    targets: list[str] = Field(
        min_length=1, max_length=8, description="check_copy_grounding이 돌려준 target"
    )
    instruction: str = Field(default="", max_length=300)


class PlanPageArgs(_Args):
    layout_id: LayoutId | None = None


class DraftFinishArgs(_Args):
    summary: str = Field(default="", max_length=1000)


class _InspectAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=500)
    evidence: Literal["image-visible", "inferred"] = "inferred"


class _RevisedItem(BaseModel):
    target: str
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    # A missing label means nobody vouched for the text, so sanitize drops it.
    evidence: Literal["image-visible", "creator-provided", "inferred", "unknown"] = "inferred"


class _RevisedCopy(BaseModel):
    items: list[_RevisedItem] = Field(default_factory=list, max_length=8)


def _registry(*tools: Tool) -> dict[str, Tool]:
    return {tool.name: tool for tool in tools}


def _require_profile(state: AgentState) -> ProductProfileDto:
    if state.profile is None:
        raise ValueError("analyze_photos를 먼저 호출하세요")
    return state.profile


def profile_summary(profile: ProductProfileDto) -> dict[str, Any]:
    return {
        "product_type": profile.product_type,
        "display_name": profile.display_name,
        "craft_type": profile.craft_type,
        "layout_id": profile.layout_id,
        "features": [
            {"title": feature.title, "evidence": feature.evidence}
            for feature in profile.features
        ],
        "copy_sections": [
            {"section_type": section.section_type, "title": section.title, "evidence": section.evidence}
            for section in profile.copy_sections
        ],
        "page_plan": [block.block_type for block in profile.page_plan],
    }


def grounding_report(profile: ProductProfileDto, user_hints: UserHintsDto | None = None) -> dict[str, Any]:
    """What sanitize_profile_for_render would drop from the customer page."""
    ungrounded = [
        {"target": f"features.{index}", "title": item.title, "description": item.description, "evidence": item.evidence}
        for index, item in enumerate(profile.features)
        if not is_grounded_copy(item, user_hints)
    ] + [
        {"target": f"copy_sections.{index}", "title": item.title, "description": item.description, "evidence": item.evidence}
        for index, item in enumerate(profile.copy_sections)
        if not is_grounded_copy(item, user_hints)
    ]
    kept = {block.section_id for block in sanitize_page_plan(profile.page_plan)}
    removed_blocks = [
        block.section_id for block in profile.page_plan if block.section_id not in kept
    ]
    return {"ungrounded": ungrounded, "removed_blocks": removed_blocks}


def _apply_revisions(
    profile: ProductProfileDto,
    items: list[_RevisedItem],
    allowed: set[str],
) -> tuple[ProductProfileDto, list[dict[str, str]]]:
    features = list(profile.features)
    sections = list(profile.copy_sections)
    applied = []
    for item in items:
        if item.target not in allowed:
            continue
        kind, _, index = item.target.partition(".")
        update = {"title": item.title, "description": item.description, "evidence": item.evidence}
        if kind == "features":
            features[int(index)] = features[int(index)].model_copy(update=update)
        else:
            sections[int(index)] = sections[int(index)].model_copy(update=update)
        applied.append({"target": item.target, "evidence": item.evidence})
    return (
        profile.model_copy(update={"features": features, "copy_sections": sections}),
        applied,
    )


def inspect_photo_tool(chat_client) -> Tool:
    def inspect_photo(state: AgentState, args: InspectPhotoArgs) -> dict[str, Any]:
        count = len(state.source_images)
        if args.index >= count:
            raise ValueError(f"사진은 {count}장입니다 (index 0~{count - 1})")
        image, mime_type = state.source_images[args.index]
        answer = _InspectAnswer.model_validate(
            chat_client.generate_json(
                prompts.inspect_prompt(args.question), image=image, mime_type=mime_type
            )
        )
        if answer.evidence == "image-visible":
            state.observations.append(f"사진 {args.index}: {answer.answer}")
        return answer.model_dump()

    return Tool(
        "inspect_photo",
        "원본 사진 한 장을 골라 질문에 답한다.",
        InspectPhotoArgs,
        inspect_photo,
        status=("ANALYZING", 20),
    )


def draft_tools(
    *,
    analyze: Callable[[bytes, str, UserHintsDto | None], ProductProfileDto],
    chat_client,
) -> dict[str, Tool]:
    def analyze_photos(state: AgentState, args: NoArgs) -> dict[str, Any]:
        image, mime_type = state.source_images[0]
        state.profile = analyze(image, mime_type, state.user_hints)
        state.plan_ready = False
        return profile_summary(state.profile)

    def check_copy_grounding(state: AgentState, args: NoArgs) -> dict[str, Any]:
        return grounding_report(_require_profile(state), state.user_hints)

    def revise_copy(state: AgentState, args: ReviseCopyArgs) -> dict[str, Any]:
        profile = _require_profile(state)
        report = {item["target"]: item for item in grounding_report(profile, state.user_hints)["ungrounded"]}
        requested = [report[target] for target in args.targets if target in report]
        if not requested:
            raise ValueError("고칠 target이 없습니다. check_copy_grounding의 target 값을 쓰세요")
        image, mime_type = state.source_images[0]
        revised = _RevisedCopy.model_validate(
            chat_client.generate_json(
                prompts.revise_copy_prompt(
                    requested, profile, state.user_hints, state.observations, args.instruction
                ),
                image=image,
                mime_type=mime_type,
            )
        )
        state.profile, applied = _apply_revisions(
            profile, revised.items, {item["target"] for item in requested}
        )
        return {
            "applied": applied,
            "still_ungrounded": [
                item["target"] for item in grounding_report(state.profile, state.user_hints)["ungrounded"]
            ],
        }

    def plan_page(state: AgentState, args: PlanPageArgs) -> dict[str, Any]:
        profile = _require_profile(state)
        if args.layout_id:
            profile = profile.model_copy(update={"layout_id": args.layout_id})
        state.profile = ensure_editorial_page_plan(profile)
        state.plan_ready = True
        return {
            "layout_id": state.profile.layout_id,
            "blocks": [f"{block.section_id}:{block.block_type}" for block in state.profile.page_plan],
        }

    def finish(state: AgentState, args: DraftFinishArgs) -> dict[str, Any]:
        state.summary = args.summary
        return {"ok": True}

    return _registry(
        Tool("analyze_photos", "대표 사진과 장인 입력으로 제품 정보와 문구 초안을 만든다.", NoArgs, analyze_photos, status=("ANALYZING", 15), max_calls=2),
        inspect_photo_tool(chat_client),
        Tool("check_copy_grounding", "근거가 부족해 고객용 페이지에서 빠질 문구를 보여준다.", NoArgs, check_copy_grounding),
        Tool("revise_copy", "빠질 문구를 사진이나 장인 입력 근거 안에서 다시 쓴다.", ReviseCopyArgs, revise_copy, status=("ANALYZING", 25), max_calls=2),
        Tool("plan_page", "레이아웃과 블록 구성을 확정한다.", PlanPageArgs, plan_page, status=("EXTRACTING", 40)),
        Tool("finish", "초안 작업을 끝낸다.", DraftFinishArgs, finish),
    )


GENERATED_MODES = {"generated_scene", "generated_view"}


class InspectImageArgs(_Args):
    photo_id: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=300)


class RegeneratePhotoArgs(_Args):
    photo_id: str = Field(min_length=1, max_length=80)
    direction: str = Field(min_length=1, max_length=200)


class InspectRenderArgs(_Args):
    section_index: int = Field(ge=0)
    question: str = Field(
        default="글자 넘침, 빈 영역, 깨진 이미지가 있는가?", max_length=300
    )


class RenderFinishArgs(_Args):
    summary: str = Field(default="", max_length=1000)
    warnings: list[str] = Field(default_factory=list, max_length=12)


class _ImageReview(BaseModel):
    answer: str = Field(min_length=1, max_length=500)
    has_problem: bool


def photo_summary(photo_set: ProductPhotoSet) -> dict[str, Any]:
    return {
        "photos": [
            {
                "photo_id": photo.photo_id,
                "asset_mode": photo.asset_mode,
                "fidelity_status": photo.fidelity_status,
                "product_generated": photo.product_generated,
            }
            for photo in photo_set.photos
        ],
        "failures": [
            {"photo_id": failure.photo_id, "reason": failure.reason[:200]}
            for failure in photo_set.photo_generation_failures
        ],
    }


def _downscaled_png(data: bytes, longest: int = 1024) -> bytes:
    image = Image.open(io.BytesIO(data)).convert("RGB")
    image.thumbnail((longest, longest))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def render_tools(
    *,
    generate_photos: Callable[[], ProductPhotoSet],
    validate: Callable[[ProductPhotoSet], ProductPhotoSet],
    photo_status: Callable[[ProductPhoto], str],
    render: Callable[[ProductPhotoSet], GeneratedImage],
    regenerate: Callable[
        [ProductPhoto, str], tuple[ProductPhoto | None, tuple[PhotoGenerationFailure, ...]]
    ]
    | None,
    chat_client,
) -> dict[str, Tool]:
    """No tool here takes copy or layout: the approved draft cannot change."""

    def require_photos(state: AgentState) -> ProductPhotoSet:
        if state.photo_set is None:
            raise ValueError("prepare_photos를 먼저 호출하세요")
        return state.photo_set

    def find(state: AgentState, photo_id: str) -> ProductPhoto:
        photos = require_photos(state).photos
        for photo in photos:
            if photo.photo_id == photo_id:
                return photo
        available = ", ".join(photo.photo_id for photo in photos)
        raise ValueError(f"없는 photo_id입니다: {photo_id}. 있는 사진: {available}")

    def review(image: bytes, question: str) -> dict[str, Any]:
        return _ImageReview.model_validate(
            chat_client.generate_json(
                prompts.image_review_prompt(question),
                image=_downscaled_png(image),
                mime_type="image/png",
            )
        ).model_dump()

    def prepare_photos(state: AgentState, args: NoArgs) -> dict[str, Any]:
        state.photo_set = generate_photos()
        return photo_summary(state.photo_set)

    def verify_photos(state: AgentState, args: NoArgs) -> dict[str, Any]:
        photo_set = require_photos(state)
        statuses = [
            {"photo_id": photo.photo_id, "status": photo_status(photo)}
            for photo in photo_set.photos
        ]
        state.photo_set = validate(photo_set)
        return {
            "photos": statuses,
            "removed": [item["photo_id"] for item in statuses if item["status"] == "REJECTED"],
        }

    def inspect_image(state: AgentState, args: InspectImageArgs) -> dict[str, Any]:
        return review(find(state, args.photo_id).data, args.question)

    def regenerate_photo(state: AgentState, args: RegeneratePhotoArgs) -> dict[str, Any]:
        photo = find(state, args.photo_id)
        if photo.asset_mode not in GENERATED_MODES:
            raise ValueError("원본을 보존한 사진은 다시 만들 수 없습니다")
        new_photo, failures = regenerate(photo, args.direction)
        if new_photo is None:
            return {"regenerated": False, "failures": [failure.reason[:200] for failure in failures]}
        state.photo_set = replace(
            state.photo_set,
            photos=tuple(
                new_photo if item.photo_id == photo.photo_id else item
                for item in state.photo_set.photos
            ),
        )
        return {"regenerated": True, "photo_id": new_photo.photo_id}

    def render_page(state: AgentState, args: NoArgs) -> dict[str, Any]:
        photo_set = validate(require_photos(state))
        state.photo_set = photo_set
        state.generated_image = render(photo_set)
        state.rendered_photo_set = photo_set
        return {
            "sections": [
                {"index": index, "section_id": section.section_id, "label": section.label}
                for index, section in enumerate(state.generated_image.sections)
            ]
        }

    def inspect_render(state: AgentState, args: InspectRenderArgs) -> dict[str, Any]:
        if state.generated_image is None:
            raise ValueError("render_page를 먼저 호출하세요")
        sections = state.generated_image.sections
        if args.section_index >= len(sections):
            raise ValueError(f"섹션은 {len(sections)}개입니다")
        section = sections[args.section_index]
        return {"section_id": section.section_id, **review(section.data, args.question)}

    def finish(state: AgentState, args: RenderFinishArgs) -> dict[str, Any]:
        state.summary = args.summary
        state.warnings.extend(args.warnings)
        return {"ok": True}

    tools = [
        Tool("prepare_photos", "원본 보존 사진과 AI 보조 사진을 준비한다.", NoArgs, prepare_photos, status=("GENERATING_BACKGROUNDS", 45), max_calls=1),
        Tool("verify_photos", "사진마다 원본 보존 검증 결과를 보여주고 통과하지 못한 사진을 뺀다.", NoArgs, verify_photos, status=("VERIFYING", 65)),
        Tool("inspect_image", "사진 한 장을 눈으로 검수한다.", InspectImageArgs, inspect_image, status=("VERIFYING", 65)),
        Tool("render_page", "페이지를 PNG로 렌더한다.", NoArgs, render_page, status=("RENDERING", 75), max_calls=2),
        Tool("inspect_render", "렌더된 섹션 하나를 검수한다.", InspectRenderArgs, inspect_render, status=("RENDERING", 80), max_calls=2),
        Tool("finish", "렌더 작업을 끝낸다. 고치지 못한 문제는 warnings에 적는다.", RenderFinishArgs, finish),
    ]
    if regenerate is not None:
        tools.append(
            Tool("regenerate_photo", "AI 생성 사진 한 장을 다른 방향으로 다시 만든다.", RegeneratePhotoArgs, regenerate_photo, status=("GENERATING_BACKGROUNDS", 50), max_calls=3)
        )
    return _registry(*tools)


EditEvidence = Literal["creator-instruction", "rephrase", "image-visible", "user-hints", "inferred"]
TOP_LEVEL_COPY = ("product_name", "summary", "hero_headline", "hero_description", "usage_scene")
BLOCK_FIELDS = ("eyebrow", "title", "body")
FIXED_BLOCKS = {"hero", "closing"}


class UpdateCopyArgs(_Args):
    path: str = Field(
        min_length=1,
        max_length=120,
        description="product_name, summary, hero_headline, hero_description, usage_scene 또는 blocks.<section_id>.<eyebrow|title|body>",
    )
    text: str = Field(min_length=1, max_length=500)
    evidence: EditEvidence


class UpdateBlockArgs(_Args):
    section_id: str = Field(min_length=1, max_length=80)
    variant: PageBlockVariant


class MoveBlockArgs(_Args):
    section_id: str = Field(min_length=1, max_length=80)
    position: int = Field(ge=0, description="0부터 센 새 위치")


class RemoveBlockArgs(_Args):
    section_id: str = Field(min_length=1, max_length=80)


class AddBlockArgs(_Args):
    block_type: PageBlockType
    after_section_id: str = Field(min_length=1, max_length=80)
    title: str = Field(default="", max_length=120)
    body: str = Field(default="", max_length=500)
    evidence: EditEvidence


class SetLayoutArgs(_Args):
    layout_id: LayoutId


class EditFinishArgs(_Args):
    reply: str = Field(min_length=1, max_length=500)


def _require_draft(state: AgentState) -> ApprovedDraftDto:
    if state.draft is None:
        raise ValueError("편집할 초안이 없습니다")
    return state.draft


def _set_draft(state: AgentState, data: dict[str, Any]) -> None:
    """Re-check every limit and whitelist, and that the FE document still builds."""
    draft = ApprovedDraftDto.model_validate(data)
    if len(sanitize_page_plan(draft.page_plan)) != len(draft.page_plan):
        raise ValueError("빈 추천 블록이나 다른 상품 홍보 블록은 저장되지 않아 넣을 수 없습니다")
    build_react_document_from_draft(draft)
    state.draft = draft


def _block_index(blocks: list[dict[str, Any]], section_id: str) -> int:
    for index, block in enumerate(blocks):
        if block["section_id"] == section_id:
            return index
    raise ValueError(f"없는 section_id입니다: {section_id}")


def _rejected_as_inferred(state: AgentState, text: str, evidence: str) -> dict[str, Any] | None:
    if evidence != "inferred":
        return None
    state.warnings.append(
        f"'{text[:60]}'은(는) 사진이나 입력 정보로 확인할 수 없어 반영하지 않았어요."
    )
    return {"applied": False, "reason": "근거 없는 추정은 반영하지 않습니다"}


def _record(state: AgentState, target: str, before: Any, after: Any, evidence: str | None = None) -> None:
    state.changes.append(
        {
            "target": target,
            "before": None if before is None else str(before),
            "after": None if after is None else str(after),
            "evidence": evidence,
        }
    )


def edit_tools(*, chat_client) -> dict[str, Tool]:
    def update_copy(state: AgentState, args: UpdateCopyArgs) -> dict[str, Any]:
        rejected = _rejected_as_inferred(state, args.text, args.evidence)
        if rejected:
            return rejected
        data = _require_draft(state).model_dump()
        if args.path in TOP_LEVEL_COPY:
            target, field_name = data, args.path
        else:
            prefix, _, rest = args.path.partition(".")
            section_id, _, field_name = rest.rpartition(".")
            if prefix != "blocks" or not section_id or field_name not in BLOCK_FIELDS:
                raise ValueError("path는 blocks.<section_id>.<eyebrow|title|body> 형식이어야 합니다")
            target = data["page_plan"][_block_index(data["page_plan"], section_id)]
        before = target[field_name]
        target[field_name] = args.text
        _set_draft(state, data)
        _record(state, args.path, before, args.text, args.evidence)
        return {"applied": True}

    def update_block(state: AgentState, args: UpdateBlockArgs) -> dict[str, Any]:
        data = _require_draft(state).model_dump()
        block = data["page_plan"][_block_index(data["page_plan"], args.section_id)]
        before = block["variant"]
        block["variant"] = args.variant
        _set_draft(state, data)
        _record(state, f"blocks.{args.section_id}.variant", before, args.variant)
        return {"applied": True}

    def move_block(state: AgentState, args: MoveBlockArgs) -> dict[str, Any]:
        data = _require_draft(state).model_dump()
        blocks = data["page_plan"]
        index = _block_index(blocks, args.section_id)
        if blocks[index]["block_type"] in FIXED_BLOCKS:
            raise ValueError("hero와 closing은 옮길 수 없습니다")
        block = blocks.pop(index)
        low = 1 if blocks and blocks[0]["block_type"] == "hero" else 0
        high = len(blocks) - 1 if blocks and blocks[-1]["block_type"] == "closing" else len(blocks)
        position = min(max(args.position, low), high)
        blocks.insert(position, block)
        _set_draft(state, data)
        _record(state, f"blocks.{args.section_id}.position", index, position)
        return {"applied": True, "position": position}

    def remove_block(state: AgentState, args: RemoveBlockArgs) -> dict[str, Any]:
        data = _require_draft(state).model_dump()
        blocks = data["page_plan"]
        index = _block_index(blocks, args.section_id)
        if blocks[index]["block_type"] in FIXED_BLOCKS:
            raise ValueError("hero와 closing은 지울 수 없습니다")
        removed = blocks.pop(index)
        _set_draft(state, data)
        _record(state, f"blocks.{args.section_id}", removed["block_type"], None)
        return {"applied": True}

    def add_block(state: AgentState, args: AddBlockArgs) -> dict[str, Any]:
        rejected = _rejected_as_inferred(state, f"{args.title} {args.body}".strip(), args.evidence)
        if rejected:
            return rejected
        if args.block_type in FIXED_BLOCKS:
            raise ValueError("hero와 closing은 새로 넣을 수 없습니다")
        data = _require_draft(state).model_dump()
        blocks = data["page_plan"]
        index = _block_index(blocks, args.after_section_id)
        if blocks[index]["block_type"] == "closing":
            raise ValueError("closing 뒤에는 블록을 넣을 수 없습니다")
        existing = {block["section_id"] for block in blocks}
        number = 1
        while f"{args.block_type}-{number}" in existing:
            number += 1
        section_id = f"{args.block_type}-{number}"
        blocks.insert(
            index + 1,
            {"section_id": section_id, "block_type": args.block_type, "title": args.title, "body": args.body},
        )
        _set_draft(state, data)
        _record(state, f"blocks.{section_id}", None, args.block_type, args.evidence)
        return {"applied": True, "section_id": section_id}

    def set_layout(state: AgentState, args: SetLayoutArgs) -> dict[str, Any]:
        data = _require_draft(state).model_dump()
        before = data["layout_id"]
        data["layout_id"] = args.layout_id
        _set_draft(state, data)
        _record(state, "layout_id", before, args.layout_id)
        return {"applied": True}

    def finish(state: AgentState, args: EditFinishArgs) -> dict[str, Any]:
        state.summary = args.reply
        return {"ok": True}

    return _registry(
        Tool("update_copy", "문구 하나를 고친다. evidence를 정확히 적는다.", UpdateCopyArgs, update_copy),
        Tool("update_block", "블록의 배경 변형을 바꾼다.", UpdateBlockArgs, update_block),
        Tool("move_block", "블록 순서를 바꾼다.", MoveBlockArgs, move_block),
        Tool("remove_block", "블록을 지운다.", RemoveBlockArgs, remove_block),
        Tool("add_block", "블록을 하나 추가한다.", AddBlockArgs, add_block),
        Tool("set_layout", "레이아웃을 바꾼다.", SetLayoutArgs, set_layout),
        inspect_photo_tool(chat_client),
        Tool("finish", "편집을 끝내고 장인에게 답한다.", EditFinishArgs, finish),
    )
