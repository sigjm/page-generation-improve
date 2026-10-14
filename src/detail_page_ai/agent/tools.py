"""Tools the agent can call, grouped by mode. Each wraps existing pipeline code."""

from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..dto import LayoutId, ProductProfileDto, UserHintsDto
from ..validation import ensure_editorial_page_plan, sanitize_page_plan
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
    evidence: Literal["image-visible", "inferred", "unknown"]


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


def grounding_report(profile: ProductProfileDto) -> dict[str, Any]:
    """What sanitize_profile_for_render would drop from the customer page."""
    ungrounded = [
        {"target": f"features.{index}", "title": item.title, "description": item.description, "evidence": item.evidence}
        for index, item in enumerate(profile.features)
        if item.evidence != "image-visible"
    ] + [
        {"target": f"copy_sections.{index}", "title": item.title, "description": item.description, "evidence": item.evidence}
        for index, item in enumerate(profile.copy_sections)
        if item.evidence != "image-visible"
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
        return grounding_report(_require_profile(state))

    def revise_copy(state: AgentState, args: ReviseCopyArgs) -> dict[str, Any]:
        profile = _require_profile(state)
        report = {item["target"]: item for item in grounding_report(profile)["ungrounded"]}
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
                item["target"] for item in grounding_report(state.profile)["ungrounded"]
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
        Tool("analyze_photos", "대표 사진과 장인 입력으로 제품 정보와 문구 초안을 만든다.", NoArgs, analyze_photos, status=("ANALYZING", 15)),
        inspect_photo_tool(chat_client),
        Tool("check_copy_grounding", "근거가 부족해 고객용 페이지에서 빠질 문구를 보여준다.", NoArgs, check_copy_grounding),
        Tool("revise_copy", "빠질 문구를 사진이나 장인 입력 근거 안에서 다시 쓴다.", ReviseCopyArgs, revise_copy, status=("ANALYZING", 25), max_calls=2),
        Tool("plan_page", "레이아웃과 블록 구성을 확정한다.", PlanPageArgs, plan_page, status=("EXTRACTING", 40)),
        Tool("finish", "초안 작업을 끝낸다.", DraftFinishArgs, finish),
    )
