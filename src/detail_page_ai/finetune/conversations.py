"""Record tool-calling conversations by running the real agent on gold data.

A script client stands in for the model and plays fixed tool calls; prompts,
tool schemas and tool results all come from the actual DetailPageAgent run.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from ..agent.detail_page_agent import DetailPageAgent
from ..dto import AiBePersistAck, ApprovedDraftDto, ProductProfileDto, UserHintsDto
from ..validation import ensure_editorial_page_plan, sanitize_profile_for_render

Call = tuple[str, dict[str, Any]]
FIXED_BLOCKS = {"hero", "closing"}
BLOCK_NAMES = {
    "statement": "소개 문단", "feature_grid": "특징 목록", "detail_split": "디테일", "gallery": "사진 모음",
    "wide_image": "큰 사진", "usage_scene": "사용 장면", "scale_reference": "크기 안내",
    "info_table": "상품 정보", "notice": "유의사항", "palette": "색상", "recommendation": "추천",
}
CLAIMS = (
    ("100년", "100년 전통 명장의 작품"),
    ("무형유산", "국가무형유산 보유자가 만든 작품"),
    ("왕실", "조선 왕실에서 쓰던 디자인을 그대로 재현한 작품"),
    ("대상", "세계 공예 대회 대상 수상작"),
)


class ScriptClient:
    """Plays a fixed list of tool calls and keeps every request the agent made."""

    def __init__(self, calls: list[Call], json_replies: list[dict] = ()):
        self.calls = list(calls)
        self.json_replies = list(json_replies)
        self.requests: list[tuple[list[dict], list[dict]]] = []
        self.replies: list[dict] = []
        self.json_requests: list[dict] = []

    def chat_with_tools(self, messages, tools):
        self.requests.append(([dict(message) for message in messages], tools))
        name, arguments = self.calls.pop(0)
        reply = {
            "content": "",
            "tool_calls": [{
                "id": f"call_{len(self.replies)}",
                "name": name,
                "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False),
            }],
        }
        self.replies.append(reply)
        return reply

    def generate_json(self, prompt, *, image=None, mime_type=None, json_schema=None):
        reply = self.json_replies.pop(0)
        self.json_requests.append({"prompt": prompt, "reply": reply})
        return reply

    def transcript(self) -> dict[str, Any]:
        """The full conversation, with tool-call arguments as mappings (what Qwen templates expect)."""
        messages, tools = self.requests[-1]
        last = self.replies[-1]
        final = {
            "role": "assistant",
            "content": last["content"],
            "tool_calls": [
                {"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": call["arguments"]}}
                for call in last["tool_calls"]
            ],
        }
        conversation = []
        for message in [*messages, final]:
            if message.get("tool_calls"):
                message = {**message, "tool_calls": [
                    {**call, "function": {**call["function"], "arguments": json.loads(call["function"]["arguments"])}}
                    for call in message["tool_calls"]
                ]}
            conversation.append(message)
        return {"messages": conversation, "tools": tools}


class GoldAnalyzer:
    def __init__(self, profile: ProductProfileDto):
        self.profile = profile

    def analyze(self, image, mime_type, user_hints=None):
        return self.profile.model_copy(deep=True)


class _NoBackend:
    def persist(self, request, image):
        return AiBePersistAck(
            generation_id=request.generation_id,
            product_id=None,
            status="SAVED",
            saved_at=datetime.now(timezone.utc),
        )


class _Unused:
    def __getattr__(self, name):
        raise AssertionError(f"{name} is not used in this recording")


def _agent(client: ScriptClient, analyzer, **components) -> DetailPageAgent:
    return DetailPageAgent(
        chat_client=client,
        analyzer=analyzer,
        renderer=components.get("renderer", _Unused()),
        photo_generator=components.get("photo_generator"),
        backend=_NoBackend(),
        id_factory=lambda: "finetune",
    )


def _finished(agent: DetailPageAgent, mode: str) -> None:
    reason = agent.last_runs[mode]["reason"]
    if reason != "finished":
        raise RuntimeError(f"{mode} recording did not finish: {reason}")


def runtime_draft(profile: ProductProfileDto, hints: UserHintsDto) -> ApprovedDraftDto:
    """The draft the service would show after the agent's draft step."""
    return ApprovedDraftDto.from_profile(sanitize_profile_for_render(ensure_editorial_page_plan(profile), user_hints=hints))


def draft_conversation(
    profile: ProductProfileDto,
    hints: UserHintsDto,
    image: bytes,
    mime_type: str,
    *,
    revise: bool = False,
    recover: str | None = None,
) -> dict[str, Any]:
    analysis = profile
    calls: list[Call] = []
    json_replies: list[dict] = []
    if recover == "check_first":
        calls.append(("check_copy_grounding", {}))
    calls.append(("analyze_photos", {}))
    calls.append(("check_copy_grounding", {}))
    if revise:
        feature = profile.features[0]
        # The analyzer was unsure about one feature; the seller's own text settles it.
        analysis = profile.model_copy(
            update={"features": [feature.model_copy(update={"evidence": "inferred"}), *profile.features[1:]]}
        )
        calls.append(("revise_copy", {"targets": ["features.0"]}))
        json_replies.append({"items": [{
            "target": "features.0", "title": feature.title, "description": feature.description, "evidence": "image-visible",
        }]})
    if recover == "bad_layout":
        calls.append(("plan_page", {"layout_id": "bento-grid"}))
    calls.append(("plan_page", {}))
    calls.append(("finish", {
        "summary": f"{profile.display_name or profile.product_type} 초안: 문구 {len(profile.copy_sections)}개, 블록 {len(profile.page_plan)}개"
    }))
    client = ScriptClient(calls, json_replies)
    agent = _agent(client, GoldAnalyzer(analysis))
    agent.create_draft("finetune", "finetune", image, mime_type, user_hints=hints)
    _finished(agent, "draft")
    row = client.transcript()
    if revise:
        request = client.json_requests[0]
        row["revise_example"] = {
            "messages": [
                {"role": "user", "content": [{"type": "image"}, {"type": "text", "text": request["prompt"]}]},
                {"role": "assistant", "content": [{"type": "text", "text": json.dumps(request["reply"], ensure_ascii=False)}]},
            ]
        }
    return row


def render_conversation(
    profile: ProductProfileDto,
    hints: UserHintsDto,
    image: bytes,
    mime_type: str,
    *,
    photo_generator,
    renderer,
) -> dict[str, Any]:
    calls: list[Call] = [
        ("prepare_photos", {}),
        ("verify_photos", {}),
        ("render_page", {}),
        ("finish", {"summary": f"{profile.display_name or profile.product_type} 렌더 완료", "warnings": []}),
    ]
    client = ScriptClient(calls)
    agent = _agent(client, _Unused(), photo_generator=photo_generator, renderer=renderer)
    approved = runtime_draft(profile, hints)
    agent.run(
        "finetune", "finetune", image, mime_type,
        user_hints=hints, profile_override=approved.to_profile(profile),
    )
    _finished(agent, "render")
    return client.transcript()


@dataclass(frozen=True)
class EditTemplate:
    name: str
    applies: Callable[[ApprovedDraftDto, UserHintsDto], bool]
    instruction: Callable[[ApprovedDraftDto, UserHintsDto], str]
    calls: Callable[[ApprovedDraftDto, UserHintsDto], list[Call]]
    reply: Callable[[ApprovedDraftDto, UserHintsDto], str]


def _block(draft: ApprovedDraftDto, block_type: str):
    return next((block for block in draft.page_plan if block.block_type == block_type), None)


def _removable(draft: ApprovedDraftDto):
    return next(
        (block for block in draft.page_plan if block.block_type not in FIXED_BLOCKS | {"info_table", "notice"}),
        None,
    )


def _claim(hints: UserHintsDto) -> str | None:
    given = " ".join(filter(None, [hints.product_name, hints.making_method, hints.care_guide]))
    return next((claim for marker, claim in CLAIMS if marker not in given), None)


def _first_sentence(text: str) -> str:
    for mark in (". ", "다. ", "요. "):
        if mark in text:
            return text.split(mark, 1)[0] + mark.strip()
    return text


EDIT_TEMPLATES = (
    EditTemplate(
        "dark_closing",
        lambda d, h: (b := _block(d, "closing")) is not None and b.variant != "dark",
        lambda d, h: "마지막 섹션 배경을 어둡게 바꿔 주세요.",
        lambda d, h: [("update_block", {"section_id": _block(d, "closing").section_id, "variant": "dark"})],
        lambda d, h: "마무리 섹션 배경을 어둡게 바꿨어요.",
    ),
    EditTemplate(
        "image_first",
        lambda d, h: d.layout_id != "image-first",
        lambda d, h: "사진이 더 잘 보이도록 이미지 중심 레이아웃으로 바꿔 주세요.",
        lambda d, h: [("set_layout", {"layout_id": "image-first"})],
        lambda d, h: "이미지 중심 레이아웃으로 바꿨어요.",
    ),
    EditTemplate(
        "remove_block",
        lambda d, h: _removable(d) is not None,
        lambda d, h: f"'{_removable(d).title or BLOCK_NAMES.get(_removable(d).block_type, _removable(d).block_type)}' 섹션은 빼 주세요.",
        lambda d, h: [("remove_block", {"section_id": _removable(d).section_id})],
        lambda d, h: "말씀하신 섹션을 뺐어요.",
    ),
    EditTemplate(
        "add_notice",
        lambda d, h: _block(d, "notice") is None and bool(h.care_guide) and len(d.page_plan) < 14,
        lambda d, h: "관리 방법 안내 섹션을 추가해 주세요.",
        lambda d, h: [("add_block", {
            "block_type": "notice",
            "after_section_id": [b for b in d.page_plan if b.block_type != "closing"][-1].section_id,
            "title": "관리 방법 안내",
            "body": _first_sentence(h.care_guide)[:500],
            "evidence": "user-hints",
        })],
        lambda d, h: "입력해 주신 관리 방법으로 안내 섹션을 넣었어요.",
    ),
    EditTemplate(
        "retitle",
        lambda d, h: bool(h.product_name) and d.hero_headline != h.product_name and len(h.product_name) <= 80,
        lambda d, h: f"제목을 '{h.product_name}'(으)로 바꿔 주세요.",
        lambda d, h: [("update_copy", {"path": "hero_headline", "text": h.product_name, "evidence": "creator-instruction"})],
        lambda d, h: "제목을 바꿨어요.",
    ),
    EditTemplate(
        "shorten",
        lambda d, h: _first_sentence(d.hero_description) != d.hero_description,
        lambda d, h: "소개 문장을 더 짧게 줄여 주세요.",
        lambda d, h: [("update_copy", {"path": "hero_description", "text": _first_sentence(d.hero_description), "evidence": "rephrase"})],
        lambda d, h: "소개 문장을 첫 문장만 남겨 짧게 줄였어요.",
    ),
    EditTemplate(
        "refuse_claim",
        lambda d, h: _claim(h) is not None,
        lambda d, h: f"소개에 '{_claim(h)}'이라고 넣어 주세요.",
        lambda d, h: [("update_copy", {"path": "summary", "text": f"{_claim(h)}입니다.", "evidence": "inferred"})],
        lambda d, h: "사진이나 입력해 주신 정보로 확인할 수 없는 내용이라 넣지 않았어요.",
    ),
)


def pick_edit_templates(draft: ApprovedDraftDto, hints: UserHintsDto, product_id: str, count: int = 2) -> list[EditTemplate]:
    applicable = [template for template in EDIT_TEMPLATES if template.applies(draft, hints)]
    applicable.sort(key=lambda template: hashlib.sha256(f"{product_id}:{template.name}".encode()).hexdigest())
    return applicable[:count]


def edit_conversation(
    profile: ProductProfileDto,
    hints: UserHintsDto,
    image: bytes,
    mime_type: str,
    template: EditTemplate,
) -> dict[str, Any]:
    draft = runtime_draft(profile, hints)
    calls = template.calls(draft, hints) + [("finish", {"reply": template.reply(draft, hints)})]
    client = ScriptClient(calls)
    agent = _agent(client, _Unused())
    result = agent.edit_draft(
        draft=draft,
        instruction=template.instruction(draft, hints),
        source_images=((image, mime_type),),
        user_hints=hints,
    )
    _finished(agent, "edit")
    row = client.transcript()
    row["result"] = {"changes": result.changes, "warnings": result.warnings, "reply": result.reply}
    return row
