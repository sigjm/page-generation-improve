import asyncio
import json
import threading
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image

import detail_page_ai.app as app_module
from detail_page_ai.agent.detail_page_agent import DetailPageAgent
from detail_page_ai.ai_dto import BeToAiEditDraftRequestDto
from detail_page_ai.dto import PageBlockDto, ProductProfileDto
from detail_page_ai.fe_dto import AiFeDraftEditResponseDto
from detail_page_ai.persistence import MemoryJobRepository
from detail_page_ai.pipeline import DetailPagePipeline, ModelBusyError
from detail_page_ai.service import (
    DetailPageJobService,
    DraftVersionConflictError,
    EditingDisabledError,
)


def png(color=(238, 236, 232)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (16, 16), color).save(output, format="PNG")
    return output.getvalue()


def call(name, arguments=None):
    return {"id": f"id-{name}", "name": name, "arguments": json.dumps(arguments or {}, ensure_ascii=False)}


def turn(*calls, content=""):
    return {"content": content, "tool_calls": list(calls)}


class ScriptedClient:
    def __init__(self):
        self.turns = []
        self.on_turn = None

    def chat_with_tools(self, messages, tools):
        if self.on_turn:
            self.on_turn(len(self.turns))
        if not self.turns:
            raise AssertionError("more model turns than scripted")
        return self.turns.pop(0)

    def generate_json(self, prompt, *, image=None, mime_type=None, json_schema=None):
        raise AssertionError("not used")


class FixedAnalyzer:
    def analyze(self, image, mime_type, user_hints=None):
        return ProductProfileDto(
            product_type="나전 보관함",
            display_name="나전 보관함",
            summary="자개 문양이 있는 검은 보관함입니다.",
            page_plan=[
                PageBlockDto(section_id="hero", block_type="hero", title="나전 보관함", photo_id="hero"),
                PageBlockDto(section_id="statement", block_type="statement", title="자개의 빛", body="빛을 받으면 자개가 반짝입니다."),
                PageBlockDto(section_id="closing", block_type="closing", title="오래 곁에"),
            ],
        )


class NeverCalled:
    def __getattr__(self, name):
        raise AssertionError(f"{name} must not be called")


class ControlledExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, function, *args, **kwargs):
        self.jobs.append((function, args, kwargs))

    def run_all(self):
        jobs, self.jobs = self.jobs, []
        for function, args, kwargs in jobs:
            function(*args, **kwargs)


def ready_service(pipeline_cls=DetailPageAgent, **agent_kwargs):
    """A service holding one DRAFT_READY job; the draft step itself used the fixed fallback."""
    client = ScriptedClient()
    kwargs = dict(analyzer=FixedAnalyzer(), renderer=NeverCalled(), backend=object())
    if pipeline_cls is DetailPageAgent:
        kwargs.update(chat_client=client, **agent_kwargs)
        client.turns = [turn(content="…"), turn(content="…")]  # draft step falls back
    executor = ControlledExecutor()
    service = DetailPageJobService(
        pipeline=pipeline_cls(**kwargs), executor=executor, repository=MemoryJobRepository()
    )
    job_id = service.submit(png(), "image/png").job_id
    executor.run_all()
    assert service.get(job_id).status == "DRAFT_READY"
    return service, client, job_id


def test_edit_applies_grounded_changes_and_saves_a_new_version():
    service, client, job_id = ready_service()
    client.turns = [
        turn(call("update_copy", {"path": "hero_headline", "text": "자개빛 보관함", "evidence": "creator-instruction"})),
        turn(call("update_block", {"section_id": "closing", "variant": "dark"})),
        turn(call("finish", {"reply": "제목과 마무리 배경을 바꿨어요."})),
    ]

    outcome = service.edit_draft(job_id, "제목을 자개빛 보관함으로, 마지막은 어둡게", expected_version=1)

    assert outcome.draft.version == 2
    assert outcome.draft.draft.hero_headline == "자개빛 보관함"
    assert outcome.draft.draft.page_plan[-1].variant == "dark"
    assert outcome.reply == "제목과 마무리 배경을 바꿨어요."
    assert [change["target"] for change in outcome.changes] == [
        "hero_headline",
        "blocks.closing.variant",
    ]


def test_inferred_copy_is_never_applied():
    service, client, job_id = ready_service()
    before = service.get(job_id).draft
    client.turns = [
        turn(call("update_copy", {"path": "summary", "text": "조선 후기 장인이 만든 진품", "evidence": "inferred"})),
        turn(call("finish", {"reply": "확인할 수 없는 내용은 넣지 않았어요."})),
    ]

    outcome = service.edit_draft(job_id, "진품이라고 써줘", expected_version=1)

    assert outcome.draft.draft.summary == before.draft.summary
    assert outcome.draft.version == 1
    assert outcome.changes == []
    assert "반영하지 않았어요" in outcome.warnings[0]


def test_unfinished_edit_changes_nothing():
    service, client, job_id = ready_service()
    client.turns = [
        turn(call("update_copy", {"path": "summary", "text": "짧은 요약", "evidence": "rephrase"})),
        turn(content="…"),
        turn(content="…"),
    ]

    outcome = service.edit_draft(job_id, "요약 줄여줘", expected_version=1)

    assert outcome.draft.version == 1
    assert outcome.changes == []
    assert "다시 알려 주세요" in outcome.reply


def test_hero_cannot_be_removed_but_blocks_can_be_added():
    service, client, job_id = ready_service()
    client.turns = [
        turn(call("remove_block", {"section_id": "hero"})),
        turn(call("add_block", {"block_type": "notice", "after_section_id": "statement", "title": "관리 안내", "body": "마른 천으로 닦아 주세요.", "evidence": "creator-instruction"})),
        turn(call("finish", {"reply": "관리 안내를 넣었어요."})),
    ]

    outcome = service.edit_draft(job_id, "관리 안내 넣고 맨 위는 빼줘", expected_version=1)

    ids = [block.section_id for block in outcome.draft.draft.page_plan]
    assert ids[0] == "hero"
    assert ids[-1] == "closing"
    assert ids[ids.index("statement") + 1] == "notice-1"


def test_stale_version_is_rejected_before_calling_the_model():
    service, _, job_id = ready_service()

    with pytest.raises(DraftVersionConflictError):
        service.edit_draft(job_id, "아무거나", expected_version=7)


def test_edit_loses_to_concurrent_manual_save():
    service, client, job_id = ready_service()
    client.turns = [
        turn(call("update_copy", {"path": "summary", "text": "에이전트 요약", "evidence": "rephrase"})),
        turn(call("finish", {"reply": "바꿨어요."})),
    ]

    def manual_save(remaining_turns):
        if remaining_turns == 1:  # just before the model's finish turn
            current = service.get(job_id).draft
            service.save_draft(
                job_id,
                current.draft.model_copy(update={"summary": "장인이 직접 쓴 요약"}),
                expected_version=current.version,
            )

    client.on_turn = manual_save

    with pytest.raises(DraftVersionConflictError):
        service.edit_draft(job_id, "요약 바꿔줘", expected_version=1)
    assert service.get(job_id).draft.draft.summary == "장인이 직접 쓴 요약"


def test_edit_fails_fast_when_a_job_holds_the_model():
    service, _, job_id = ready_service(edit_lock_timeout_seconds=0.05)
    held, release = threading.Event(), threading.Event()

    def hold_lock():
        with service.pipeline._model_lock:
            held.set()
            release.wait(5)

    worker = threading.Thread(target=hold_lock)
    worker.start()
    held.wait(5)
    try:
        with pytest.raises(ModelBusyError):
            service.edit_draft(job_id, "아무거나", expected_version=1)
    finally:
        release.set()
        worker.join()


def test_editing_is_disabled_in_pipeline_mode():
    service, _, job_id = ready_service(DetailPagePipeline)

    with pytest.raises(EditingDisabledError):
        service.edit_draft(job_id, "아무거나", expected_version=1)


def test_route_returns_the_documented_shape(monkeypatch):
    service, client, job_id = ready_service()
    client.turns = [
        turn(call("set_layout", {"layout_id": "image-first"})),
        turn(call("finish", {"reply": "이미지 중심으로 바꿨어요."})),
    ]
    monkeypatch.setattr(app_module, "get_service", lambda: service)
    monkeypatch.setattr(app_module, "get_settings", lambda: SimpleNamespace(ai_internal_auth_token="secret"))

    body = asyncio.run(
        app_module.edit_internal_detail_page_draft(
            job_id,
            BeToAiEditDraftRequestDto(instruction="이미지 위주로", version=1),
            x_ai_internal_token="secret",
        )
    )

    response = AiFeDraftEditResponseDto.model_validate(body)
    assert response.draft.version == 2
    assert response.changes[0].target == "layout_id"


@pytest.mark.parametrize(
    "error, status",
    [
        (KeyError("job"), 404),
        (EditingDisabledError("off"), 400),
        (ModelBusyError("busy"), 503),
        (DraftVersionConflictError("stale"), 409),
        (ValueError("Draft is not ready"), 409),
        (RuntimeError("model down"), 503),
    ],
)
def test_route_maps_errors_to_status_codes(monkeypatch, error, status):
    class FailingService:
        def edit_draft(self, *args, **kwargs):
            raise error

    monkeypatch.setattr(app_module, "get_service", lambda: FailingService())
    monkeypatch.setattr(app_module, "get_settings", lambda: SimpleNamespace(ai_internal_auth_token="secret"))

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            app_module.edit_internal_detail_page_draft(
                "job-x",
                BeToAiEditDraftRequestDto(instruction="아무거나", version=1),
                x_ai_internal_token="secret",
            )
        )
    assert caught.value.status_code == status


def test_edits_have_their_own_small_budget():
    service, client, job_id = ready_service(edit_max_tool_calls=2)
    variant = call("update_block", {"section_id": "closing", "variant": "dark"})
    client.turns = [turn(variant), turn(variant), turn(variant), turn(call("finish", {"reply": "끝"}))]

    outcome = service.edit_draft(job_id, "마지막 어둡게", expected_version=1)

    assert outcome.changes == []
    assert "다시 알려 주세요" in outcome.reply


def test_blocks_that_would_be_dropped_on_save_are_refused():
    service, client, job_id = ready_service()
    client.turns = [
        turn(call("add_block", {"block_type": "recommendation", "after_section_id": "statement", "title": "함께 보면 좋은 연출", "evidence": "creator-instruction"})),
        turn(call("finish", {"reply": "추천 블록을 넣었어요."})),
    ]
    agent = service.pipeline

    outcome = service.edit_draft(job_id, "추천 섹션 넣어줘", expected_version=1)

    assert outcome.changes == []
    assert agent.last_runs["edit"]["trace"][0]["outcome"] == "error"


def test_model_errors_during_an_edit_reach_the_caller():
    from detail_page_ai.ports import ModelCallError

    service, client, job_id = ready_service()

    def model_down(remaining_turns):
        raise ModelCallError("text model is down")

    client.on_turn = model_down

    with pytest.raises(ModelCallError):
        service.edit_draft(job_id, "아무거나", expected_version=1)
