import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO

from PIL import Image

from detail_page_ai.agent.detail_page_agent import DetailPageAgent
from detail_page_ai.dto import AiBePersistAck, ProductProfileDto
from detail_page_ai.models import (
    GeneratedImage,
    GeneratedSection,
    PhotoGenerationFailure,
    ProductPhoto,
    ProductPhotoSet,
)
from detail_page_ai.pipeline import DetailPagePipeline


def png(color) -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 32), color).save(output, format="PNG")
    return output.getvalue()


SOURCE = png((200, 200, 200))
SOURCE_HASH = hashlib.sha256(SOURCE).hexdigest()
PROFILE = ProductProfileDto.minimal("보관함")


def call(name, arguments=None):
    return {"id": f"id-{name}", "name": name, "arguments": json.dumps(arguments or {}, ensure_ascii=False)}


def turn(*calls, content=""):
    return {"content": content, "tool_calls": list(calls)}


class ScriptedClient:
    def __init__(self, turns=(), json_replies=()):
        self.turns = list(turns)
        self.json_replies = list(json_replies)

    def chat_with_tools(self, messages, tools):
        if not self.turns:
            raise AssertionError("more model turns than scripted")
        return self.turns.pop(0)

    def generate_json(self, prompt, *, image=None, mime_type=None, json_schema=None):
        return self.json_replies.pop(0)


HERO = ProductPhoto(
    photo_id="hero",
    order=1,
    label="Hero",
    data=SOURCE,
    mime_type="image/png",
    asset_mode="source_original",
    source_sha256=SOURCE_HASH,
    product_generated=False,
    fidelity_status="VERIFIED",
)
SCENE = ProductPhoto(
    photo_id="lifestyle-02",
    order=2,
    label="AI 장면",
    data=png((1, 1, 1)),
    mime_type="image/png",
    asset_mode="generated_scene",
    source_sha256=SOURCE_HASH,
    background_generated=True,
    product_generated=True,
    fidelity_status="GENERATED",
)
FORGED = ProductPhoto(
    photo_id="packshot",
    order=3,
    label="Packshot",
    data=png((9, 9, 9)),
    mime_type="image/png",
    asset_mode="source",
    source_sha256=SOURCE_HASH,
    product_generated=False,
    fidelity_status="VERIFIED",
)
NEW_SCENE_DATA = png((10, 120, 10))


class FakePhotoGenerator:
    def __init__(self, photos=(HERO, SCENE), regenerate_fails=False):
        self.photos = photos
        self.regenerate_fails = regenerate_fails
        self.regenerate_calls = []

    def generate(self, **kwargs):
        return ProductPhotoSet(photos=self.photos)

    def regenerate_role(self, *, photo, source_image, source_mime_type, profile, direction=None):
        self.regenerate_calls.append((photo.photo_id, direction))
        if self.regenerate_fails:
            return None, (PhotoGenerationFailure(photo.photo_id, "image server returned 500"),)
        return replace(photo, data=NEW_SCENE_DATA), ()


class FakeRenderer:
    def __init__(self):
        self.photo_sets = []

    def render(self, **kwargs):
        self.photo_sets.append(kwargs["photo_set"])
        return GeneratedImage(
            data=png((255, 255, 255)),
            mime_type="image/png",
            sections=(
                GeneratedSection(section_id="hero", order=1, label="소개", data=png((250, 250, 250)), mime_type="image/png"),
            ),
            photos=kwargs["photo_set"].photos,
        )


class SavingBackend:
    def persist(self, request, image):
        return AiBePersistAck(
            generation_id=request.generation_id,
            product_id=None,
            status="SAVED",
            saved_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
        )


class NeverCalled:
    def __getattr__(self, name):
        raise AssertionError(f"{name} must not be called")


def build(cls, client=None, generator=None, renderer=None):
    kwargs = dict(
        analyzer=NeverCalled(),
        photo_generator=generator or FakePhotoGenerator(),
        renderer=renderer or FakeRenderer(),
        backend=SavingBackend(),
        id_factory=lambda: "gen-1",
    )
    if cls is DetailPageAgent:
        kwargs["chat_client"] = client
    return cls(**kwargs)


def render(pipeline):
    return pipeline.run("job-1", "req-1", SOURCE, "image/png", profile_override=PROFILE)


def photo_data(result):
    import base64

    return {photo.photo_id: base64.b64decode(photo.image_base64) for photo in result.fe_result.detail_page.photos}


def test_agent_regenerates_a_flagged_ai_photo_and_renders_once():
    generator = FakePhotoGenerator()
    renderer = FakeRenderer()
    client = ScriptedClient(
        turns=[
            turn(call("prepare_photos")),
            turn(call("verify_photos")),
            turn(call("inspect_image", {"photo_id": "lifestyle-02", "question": "어색한가?"})),
            turn(call("regenerate_photo", {"photo_id": "lifestyle-02", "direction": "밝은 원목 테이블"})),
            turn(call("render_page")),
            turn(call("inspect_render", {"section_index": 0})),
            turn(call("finish", {"summary": "완료"})),
        ],
        json_replies=[
            {"answer": "제품이 녹아내린 듯 보입니다.", "has_problem": True},
            {"answer": "문제없습니다.", "has_problem": False},
        ],
    )

    result = render(build(DetailPageAgent, client, generator, renderer))

    assert generator.regenerate_calls == [("lifestyle-02", "밝은 원목 테이블")]
    assert photo_data(result)["lifestyle-02"] == NEW_SCENE_DATA
    assert len(renderer.photo_sets) == 1


def test_agent_cannot_regenerate_source_photos():
    client = ScriptedClient(
        turns=[
            turn(call("prepare_photos")),
            turn(call("regenerate_photo", {"photo_id": "hero", "direction": "다시"})),
            turn(call("render_page")),
            turn(call("finish")),
        ]
    )
    agent = build(DetailPageAgent, client)

    result = render(agent)

    assert agent.last_runs["render"]["trace"][1]["outcome"] == "error"
    assert photo_data(result)["hero"] == SOURCE


def test_changes_after_render_trigger_a_final_rerender():
    renderer = FakeRenderer()
    client = ScriptedClient(
        turns=[
            turn(call("prepare_photos")),
            turn(call("render_page")),
            turn(call("regenerate_photo", {"photo_id": "lifestyle-02", "direction": "다른 배경"})),
            turn(call("finish")),
        ]
    )

    result = render(build(DetailPageAgent, client, renderer=renderer))

    assert len(renderer.photo_sets) == 2
    assert photo_data(result)["lifestyle-02"] == NEW_SCENE_DATA


def test_unverified_photos_are_dropped_even_if_the_agent_skips_verify():
    generator = FakePhotoGenerator(photos=(HERO, FORGED))
    client = ScriptedClient(
        turns=[turn(call("prepare_photos")), turn(call("render_page")), turn(call("finish"))]
    )

    result = render(build(DetailPageAgent, client, generator))

    assert set(photo_data(result)) == {"hero"}


def test_failed_regeneration_keeps_previous_photo():
    generator = FakePhotoGenerator(regenerate_fails=True)
    client = ScriptedClient(
        turns=[
            turn(call("prepare_photos")),
            turn(call("regenerate_photo", {"photo_id": "lifestyle-02", "direction": "다시"})),
            turn(call("render_page")),
            turn(call("finish")),
        ]
    )

    result = render(build(DetailPageAgent, client, generator))

    assert photo_data(result)["lifestyle-02"] == SCENE.data


def test_render_fallback_matches_fixed_pipeline():
    client = ScriptedClient(turns=[turn(content="음…"), turn(content="글쎄요")])

    agent_result = render(build(DetailPageAgent, client))
    pipeline_result = render(build(DetailPagePipeline))

    assert agent_result.fe_result.model_dump() == pipeline_result.fe_result.model_dump()


def test_render_mode_has_no_copy_tools_and_keeps_the_approved_profile():
    client = ScriptedClient(
        turns=[
            turn(call("update_copy", {"path": "summary", "text": "바꿈", "evidence": "creator-instruction"})),
            turn(call("finish")),
        ]
    )
    agent = build(DetailPageAgent, client)

    result = render(agent)

    assert agent.last_runs["render"]["trace"][0]["outcome"] == "rejected"
    assert result.fe_result.product.summary == PROFILE.summary


def test_regenerate_is_capped_at_three_calls():
    generator = FakePhotoGenerator()
    regenerate = call("regenerate_photo", {"photo_id": "lifestyle-02", "direction": "다시"})
    client = ScriptedClient(
        turns=[turn(call("prepare_photos"))] + [turn(regenerate)] * 4 + [turn(call("finish"))]
    )

    render(build(DetailPageAgent, client, generator))

    assert len(generator.regenerate_calls) == 3
