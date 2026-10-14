import hashlib
import json
from io import BytesIO

from PIL import Image

from detail_page_ai.finetune.conversations import (
    EDIT_TEMPLATES,
    draft_conversation,
    edit_conversation,
    pick_edit_templates,
)
from detail_page_ai.finetune.crawl import ProductPage
from detail_page_ai.finetune.export import analysis_row, group_products, split_for
from detail_page_ai.finetune.gold import Structured, build_gold_profile, build_user_hints
from detail_page_ai.dto import ApprovedDraftDto

PAGE = ProductPage(
    name="합성 나무 상자",
    main_image="https://example.invalid/main.jpg",
    spec={"소재": "원목", "규격": "W120 x D80 x H60mm", "용도": "소품 보관"},
    notice="원목 특성상 결이 조금씩 다릅니다. 직사광선을 피해 주세요.",
)
STRUCTURED = Structured(
    product_type="나무 상자",
    hero_title="결을 잇는 나무 상자",
    hero_description="서로 다른 나무를 이어 붙여 무늬를 만든 작은 상자입니다.",
    making=["자투리 목재를 손으로 깎아 이어 붙이고 옻칠로 마감했습니다."],
    features=[
        {"title": "미닫이 뚜껑", "description": "뚜껑은 미닫이 방식으로 열립니다."},
        {"title": "이어 붙인 무늬", "description": "서로 다른 나무를 이어 붙여 무늬를 만들었습니다."},
    ],
    closing_title="오래 곁에 두는 생활 오브제",
)
ARCHETYPE = {
    "name": "사양 확인형",
    "sequence": ["hero", "statement", "feature_grid", "info_table", "notice", "closing"],
    "variants": ["paper", "sand", "light", "light", "dark", "paper"],
}


def png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 32), (180, 150, 120)).save(output, format="PNG")
    return output.getvalue()


HINTS = build_user_hints(
    PAGE,
    [
        "결을 잇는 나무 상자",
        "서로 다른 나무를 이어 붙여 무늬를 만든 작은 상자입니다. 책상 위에 두기 좋습니다.",
        "자투리 목재를 손으로 깎아 이어 붙이고 옻칠로 마감했습니다.",
        "뚜껑은 미닫이 방식으로 열립니다.",
        "서로 다른 나무를 이어 붙여 무늬를 만들었습니다.",
        "오래 곁에 두는 생활 오브제",
    ],
)


def gold():
    return build_gold_profile(PAGE, "인테리어", STRUCTURED, ARCHETYPE, HINTS)


def tool_names(messages):
    return [
        call["function"]["name"]
        for message in messages
        if message["role"] == "assistant"
        for call in message.get("tool_calls", [])
    ]


def test_draft_conversation_records_the_real_prompts_and_tool_results():
    row = draft_conversation(gold(), HINTS, png(), "image/png")

    messages = row["messages"]
    assert messages[0]["role"] == "system" and "초안" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert tool_names(messages) == ["analyze_photos", "check_copy_grounding", "plan_page", "finish"]
    analyze_result = json.loads(messages[3]["content"])
    assert analyze_result["display_name"] == "합성 나무 상자"
    assert {tool["function"]["name"] for tool in row["tools"]} >= {"analyze_photos", "revise_copy"}


def test_revise_variant_rechecks_a_feature_against_seller_text():
    row = draft_conversation(gold(), HINTS, png(), "image/png", revise=True)

    names = tool_names(row["messages"])
    assert names == ["analyze_photos", "check_copy_grounding", "revise_copy", "plan_page", "finish"]
    revise_result = json.loads(row["messages"][7]["content"])
    assert revise_result["still_ungrounded"] == []
    example = row["revise_example"]
    assert example["messages"][0]["content"][0] == {"type": "image"}
    assert "<creator_data>" in example["messages"][0]["content"][1]["text"]
    assert json.loads(example["messages"][1]["content"][0]["text"])["items"][0]["evidence"] == "image-visible"


def test_recovery_variants_contain_real_tool_errors():
    first = draft_conversation(gold(), HINTS, png(), "image/png", recover="check_first")
    assert tool_names(first["messages"])[:2] == ["check_copy_grounding", "analyze_photos"]
    assert "analyze_photos를 먼저" in first["messages"][3]["content"]

    bad = draft_conversation(gold(), HINTS, png(), "image/png", recover="bad_layout")
    names = tool_names(bad["messages"])
    assert names[-3:] == ["plan_page", "plan_page", "finish"]
    assert "invalid arguments" in [m for m in bad["messages"] if m["role"] == "tool"][-2]["content"]


def test_edit_conversation_refuses_unsupported_claims():
    template = next(t for t in EDIT_TEMPLATES if t.name == "refuse_claim")
    row = edit_conversation(gold(), HINTS, png(), "image/png", template)

    assert tool_names(row["messages"]) == ["update_copy", "finish"]
    assert row["result"]["changes"] == []
    assert row["result"]["warnings"]


def test_edit_templates_are_picked_deterministically_and_each_one_runs():
    draft = ApprovedDraftDto.from_profile(gold())
    first = pick_edit_templates(draft, HINTS, "2457", count=2)
    assert first == pick_edit_templates(draft, HINTS, "2457", count=2)
    assert len({template.name for template in first}) == 2
    for template in EDIT_TEMPLATES:
        if template.applies(draft, HINTS):
            row = edit_conversation(gold(), HINTS, png(), "image/png", template)
            assert tool_names(row["messages"])[-1] == "finish", template.name


def test_products_sharing_a_text_image_are_grouped_and_banners_are_ignored():
    banner = hashlib.sha256(b"banner").hexdigest()
    bio = hashlib.sha256(b"bio").hexdigest()
    products = {
        "1": {bio: ["작가 소개 문장"], banner: []},
        "2": {bio: ["작가 소개 문장"], banner: []},
        "3": {banner: []},
    }

    groups = group_products(products)

    assert groups["1"] == groups["2"] != groups["3"]


def test_split_is_deterministic_by_group():
    assert split_for("group-a") == split_for("group-a")
    assert {split_for(f"group-{n}") for n in range(200)} == {"train", "val"}


def test_analysis_row_pairs_the_runtime_prompt_with_gold_json():
    profile = gold()
    row = analysis_row("images/2457/main.png", png(), profile, HINTS)

    assert row["images"] == ["images/2457/main.png"]
    user, assistant = row["messages"]
    assert user["content"][0] == {"type": "image"}
    assert "JSON Schema" in user["content"][1]["text"] and "합성 나무 상자" in user["content"][1]["text"]
    assert json.loads(assistant["content"][0]["text"])["display_name"] == "합성 나무 상자"


def test_render_conversation_runs_the_real_render_tools():
    from detail_page_ai.finetune.conversations import render_conversation
    from detail_page_ai.models import GeneratedImage, GeneratedSection, ProductPhoto, ProductPhotoSet

    image = png()

    class Photos:
        def generate(self, **kwargs):
            return ProductPhotoSet(photos=(ProductPhoto(
                photo_id="hero", order=1, label="원본", data=image, mime_type="image/png",
                asset_mode="source_original", source_sha256=hashlib.sha256(image).hexdigest(),
                product_generated=False, fidelity_status="VERIFIED",
            ),))

    class Renderer:
        def render(self, **kwargs):
            return GeneratedImage(
                data=image, mime_type="image/png", photos=kwargs["photo_set"].photos,
                sections=(GeneratedSection(section_id="hero", order=1, label="소개", data=image, mime_type="image/png"),),
            )

    row = render_conversation(gold(), HINTS, image, "image/png", photo_generator=Photos(), renderer=Renderer())

    assert tool_names(row["messages"]) == ["prepare_photos", "verify_photos", "render_page", "finish"]
    assert '"hero"' in [m for m in row["messages"] if m["role"] == "tool"][2]["content"]


def test_recorded_tool_call_arguments_are_mappings_for_the_qwen_template():
    # Qwen3.5/3.8 chat templates iterate `tool_call.arguments|items`; a JSON string breaks them.
    row = draft_conversation(gold(), HINTS, png(), "image/png", recover="bad_layout")

    calls = [call for m in row["messages"] if m["role"] == "assistant" for call in m.get("tool_calls", [])]
    assert all(isinstance(call["function"]["arguments"], dict) for call in calls)
    assert calls[-3]["function"]["arguments"] == {"layout_id": "bento-grid"}
