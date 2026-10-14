import json
from io import BytesIO

import pytest
from PIL import Image
from prometheus_client import REGISTRY

from detail_page_ai.agent.detail_page_agent import DetailPageAgent
from detail_page_ai.agent.tools import draft_tools
from detail_page_ai.dto import (
    CopySectionDto,
    PageBlockDto,
    ProductFeatureDto,
    ProductProfileDto,
    UserHintsDto,
)
from detail_page_ai.pipeline import DetailPagePipeline


def png(color=(238, 236, 232)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (16, 16), color).save(output, format="PNG")
    return output.getvalue()


PRIMARY = png()
SECOND = png((20, 20, 20))
HINTS = UserHintsDto(product_name="나전 보관함", care_guide="마른 천으로 닦아 주세요.")


def call(name, arguments=None, call_id=None):
    return {
        "id": call_id or f"id-{name}",
        "name": name,
        "arguments": json.dumps(arguments or {}, ensure_ascii=False),
    }


def turn(*calls, content=""):
    return {"content": content, "tool_calls": list(calls)}


class ScriptedClient:
    def __init__(self, turns=(), json_replies=()):
        self.turns = list(turns)
        self.json_replies = list(json_replies)
        self.json_calls = []

    def chat_with_tools(self, messages, tools):
        if not self.turns:
            raise AssertionError("more model turns than scripted")
        return self.turns.pop(0)

    def generate_json(self, prompt, *, image=None, mime_type=None, json_schema=None):
        self.json_calls.append({"prompt": prompt, "image": image})
        return self.json_replies.pop(0)


class CountingAnalyzer:
    def __init__(self):
        self.calls = 0

    def analyze(self, image, mime_type, user_hints=None):
        self.calls += 1
        return ProductProfileDto(
            product_type="나전 보관함",
            display_name="나전 보관함",
            summary="자개 문양이 있는 검은 보관함입니다.",
            features=[
                ProductFeatureDto(
                    title="자개 문양", description="뚜껑에 자개가 보입니다.", evidence="image-visible"
                ),
                ProductFeatureDto(
                    title="100년 된 옻칠", description="오래된 옻칠입니다.", evidence="inferred"
                ),
            ],
            copy_sections=[
                CopySectionDto(
                    section_type="hero",
                    title="빛나는 자개 보관함",
                    description="자개 문양이 은은하게 빛납니다.",
                    evidence="image-visible",
                ),
                CopySectionDto(
                    section_type="feature",
                    title="왕실 장인의 작품",
                    description="왕실 장인이 만들었습니다.",
                    evidence="inferred",
                ),
            ],
            page_plan=[
                PageBlockDto(section_id="statement", block_type="statement", title="자개의 빛")
            ],
        )


class NeverCalled:
    def __getattr__(self, name):
        raise AssertionError(f"{name} must not be called")


def make_agent(client, analyzer=None, **kwargs):
    return DetailPageAgent(
        chat_client=client,
        analyzer=analyzer or CountingAnalyzer(),
        renderer=NeverCalled(),
        backend=object(),
        id_factory=lambda: "gen-1",
        **kwargs,
    )


def draft(agent, **kwargs):
    return agent.create_draft(
        "job-1",
        "req-1",
        PRIMARY,
        "image/png",
        additional_source_images=((SECOND, "image/png"),),
        user_hints=HINTS,
        **kwargs,
    )


def test_agent_analyzes_inspects_revises_and_plans():
    client = ScriptedClient(
        turns=[
            turn(call("analyze_photos")),
            turn(call("inspect_photo", {"index": 1, "question": "바닥에 표시가 있나?"})),
            turn(call("check_copy_grounding")),
            turn(call("revise_copy", {"targets": ["copy_sections.1"]})),
            turn(call("plan_page")),
            turn(call("finish", {"summary": "초안 완료"})),
        ],
        json_replies=[
            {"answer": "바닥에 낙관이 보입니다.", "evidence": "image-visible"},
            {
                "items": [
                    {
                        "target": "copy_sections.1",
                        "title": "자개로 채운 뚜껑",
                        "description": "뚜껑 전체에 자개 조각이 보입니다.",
                        "evidence": "image-visible",
                    }
                ]
            },
        ],
    )
    agent = make_agent(client)

    result = draft(agent)

    assert [section.title for section in result.profile.copy_sections] == [
        "빛나는 자개 보관함",
        "자개로 채운 뚜껑",
    ]
    assert result.profile.page_plan[0].block_type == "hero"
    assert result.profile.page_plan[-1].block_type == "closing"
    assert client.json_calls[0]["image"] == SECOND
    assert "바닥에 낙관이 보입니다." in client.json_calls[1]["prompt"]
    assert [entry["tool"] for entry in agent.last_runs["draft"]["trace"]] == [
        "analyze_photos",
        "inspect_photo",
        "check_copy_grounding",
        "revise_copy",
        "plan_page",
        "finish",
    ]
    assert agent.last_runs["draft"]["reason"] == "finished"


def test_copy_the_model_leaves_inferred_never_ships():
    client = ScriptedClient(
        turns=[
            turn(call("analyze_photos")),
            turn(call("revise_copy", {"targets": ["copy_sections.1"]})),
            turn(call("plan_page")),
            turn(call("finish")),
        ],
        json_replies=[
            {
                "items": [
                    {
                        "target": "copy_sections.1",
                        "title": "왕실 장인의 작품",
                        "description": "근거는 없지만 왕실 장인이 만들었습니다.",
                        "evidence": "inferred",
                    }
                ]
            }
        ],
    )

    result = draft(make_agent(client))

    assert all(section.evidence == "image-visible" for section in result.profile.copy_sections)
    assert all(feature.evidence == "image-visible" for feature in result.profile.features)
    assert any("왕실 장인의 작품" in item for item in result.profile.uncertain_information)


def test_model_that_skips_tools_falls_back_to_the_fixed_pipeline():
    labels = {"mode": "draft", "reason": "no_tool_call"}
    before = REGISTRY.get_sample_value("agent_fallback_total", labels) or 0
    client = ScriptedClient(turns=[turn(content="음…"), turn(content="글쎄요")])
    pipeline = DetailPagePipeline(
        analyzer=CountingAnalyzer(),
        renderer=NeverCalled(),
        backend=object(),
        id_factory=lambda: "gen-1",
    )

    agent_result = draft(make_agent(client))
    pipeline_result = draft(pipeline)

    assert agent_result.profile == pipeline_result.profile
    assert agent_result.fe_draft.draft == pipeline_result.fe_draft.draft
    assert REGISTRY.get_sample_value("agent_fallback_total", labels) == before + 1


def test_finish_without_work_falls_back():
    labels = {"mode": "draft", "reason": "incomplete"}
    before = REGISTRY.get_sample_value("agent_fallback_total", labels) or 0
    analyzer = CountingAnalyzer()

    result = draft(make_agent(ScriptedClient(turns=[turn(call("finish"))]), analyzer))

    assert analyzer.calls == 1
    assert result.profile.page_plan[0].block_type == "hero"
    assert REGISTRY.get_sample_value("agent_fallback_total", labels) == before + 1


def test_fallback_fills_only_the_missing_steps():
    analyzer = CountingAnalyzer()
    client = ScriptedClient(
        turns=[turn(call("analyze_photos")), turn(call("plan_page"))]
    )

    result = draft(make_agent(client, analyzer, max_tool_calls=1))

    assert analyzer.calls == 1
    assert result.profile.page_plan[0].block_type == "hero"


def test_model_errors_fail_the_draft_instead_of_falling_back():
    class DownClient:
        def chat_with_tools(self, messages, tools):
            raise RuntimeError("model server is down")

    analyzer = CountingAnalyzer()

    with pytest.raises(RuntimeError, match="model server is down"):
        draft(make_agent(DownClient(), analyzer))
    assert analyzer.calls == 0


def test_inspect_photo_rejects_an_index_out_of_range():
    client = ScriptedClient(
        turns=[
            turn(call("inspect_photo", {"index": 5, "question": "뒷면?"})),
            turn(call("finish")),
        ]
    )
    agent = make_agent(client)

    draft(agent)

    first = agent.last_runs["draft"]["trace"][0]
    assert first["outcome"] == "error"
    assert "2장" in first["result"]["detail"]


def test_draft_mode_cannot_call_render_tools():
    tools = draft_tools(analyze=lambda *args: None, chat_client=None)
    assert set(tools) == {
        "analyze_photos",
        "inspect_photo",
        "check_copy_grounding",
        "revise_copy",
        "plan_page",
        "finish",
    }
    client = ScriptedClient(turns=[turn(call("render_page")), turn(call("finish"))])
    agent = make_agent(client)

    draft(agent)

    assert agent.last_runs["draft"]["trace"][0]["outcome"] == "rejected"


def test_tool_calling_errors_fall_back_to_the_fixed_pipeline():
    from detail_page_ai.ports import ModelCallError

    class RejectsTools:
        def chat_with_tools(self, messages, tools):
            raise ModelCallError("HTTP Error 400: tools are not supported")

    pipeline = DetailPagePipeline(
        analyzer=CountingAnalyzer(),
        renderer=NeverCalled(),
        backend=object(),
        id_factory=lambda: "gen-1",
    )

    assert draft(make_agent(RejectsTools())).profile == draft(pipeline).profile


def test_a_dead_model_fails_the_draft_after_one_fallback_try():
    from detail_page_ai.ports import ModelCallError

    class DeadAnalyzer:
        calls = 0

        def analyze(self, image, mime_type, user_hints=None):
            DeadAnalyzer.calls += 1
            raise ModelCallError("vision model timed out")

    client = ScriptedClient(turns=[turn(call("analyze_photos"))] * 3)

    with pytest.raises(ModelCallError):
        draft(make_agent(client, DeadAnalyzer()))
    assert DeadAnalyzer.calls == 2


def test_rewrite_without_an_evidence_label_counts_as_inferred():
    client = ScriptedClient(
        turns=[
            turn(call("analyze_photos")),
            turn(call("revise_copy", {"targets": ["copy_sections.1"]})),
            turn(call("plan_page")),
            turn(call("finish")),
        ],
        # Qwen on MLX Serve left out "evidence" in the live run (2026-10-14).
        json_replies=[
            {"items": [{"target": "copy_sections.1", "title": "다양한 활용", "description": "여러 공간에 어울립니다."}]}
        ],
    )
    agent = make_agent(client)

    result = draft(agent)

    assert agent.last_runs["draft"]["trace"][1]["outcome"] == "ok"
    assert "다양한 활용" not in [section.title for section in result.profile.copy_sections]


def test_user_prompt_says_when_there_are_no_extra_photos():
    from detail_page_ai.agent.prompts import draft_user_prompt

    assert "추가 사진은 없다" in draft_user_prompt(1, None)
    assert "1~2번" in draft_user_prompt(3, None)
