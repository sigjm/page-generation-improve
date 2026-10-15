from types import SimpleNamespace

import pytest

from detail_page_ai.agent.detail_page_agent import DetailPageAgent
from detail_page_ai.config import Settings
from detail_page_ai.pipeline import DetailPagePipeline
from local_detail_page_ai import factory as factory_module


def settings(tmp_path, **overrides):
    values = dict(
        detail_page_template_path=None,
        analysis_provider="local",
        local_text_provider="sglang",
        local_text_url="http://127.0.0.1:30000",
        local_text_model="qwen-text",
        local_text_timeout_seconds=300.0,
        local_image_provider="none",
        local_image_url="http://127.0.0.1:30001",
        local_image_model="flux-klein",
        local_image_timeout_seconds=300.0,
        background_provider="none",
        product_photo_generation="source",
        product_photo_shots="hero,packshot,detail,lifestyle",
        max_generated_photos=5,
        detail_page_renderer="html",
        backend_url=None,
        backend_auth_token=None,
        backend_timeout_seconds=60.0,
        prompt_version="local-v1",
        asset_store_dir=str(tmp_path / "assets"),
        sqlite_path=str(tmp_path / "state.sqlite3"),
        response_asset_mode="base64",
        craft_confidence_threshold=0.65,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def no_browser(monkeypatch):
    monkeypatch.setattr(factory_module, "HtmlDetailPageRenderer", lambda: object())


def test_settings_default_to_agent_mode(monkeypatch):
    for name in ("AGENT_MODE", "AGENT_MAX_TOOL_CALLS", "AGENT_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)

    loaded = Settings(_env_file=None)

    assert loaded.agent_mode == "agent"
    assert loaded.agent_max_tool_calls == 15
    assert loaded.agent_timeout_seconds == 600.0


def test_settings_read_agent_mode_from_the_environment(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "pipeline")

    assert Settings(_env_file=None).agent_mode == "pipeline"


def test_factory_builds_the_agent_with_its_limits(tmp_path):
    service = factory_module.build_service(
        settings(
            tmp_path,
            agent_mode="agent",
            agent_max_tool_calls=9,
            agent_timeout_seconds=120.0,
        )
    )

    assert isinstance(service.pipeline, DetailPageAgent)
    assert service.pipeline.max_tool_calls == 9
    assert service.pipeline.timeout_seconds == 120.0
    assert service.pipeline.chat_client is service.pipeline.analyzer.chat_client


def test_factory_keeps_the_fixed_pipeline_when_asked(tmp_path):
    service = factory_module.build_service(settings(tmp_path, agent_mode="pipeline"))

    assert type(service.pipeline) is DetailPagePipeline


def test_agent_mode_requires_a_tool_calling_text_server(tmp_path):
    with pytest.raises(ValueError, match="AGENT_MODE=agent"):
        factory_module.build_service(
            settings(tmp_path, agent_mode="agent", local_text_provider="ollama")
        )


def test_image_steps_reach_the_image_client(monkeypatch, tmp_path):
    # Qwen-Image 2.1 needs ~20 steps; FLUX.2 klein is distilled to 4.
    monkeypatch.setenv("LOCAL_IMAGE_STEPS", "20")
    assert Settings(_env_file=None).local_image_steps == 20
    monkeypatch.delenv("LOCAL_IMAGE_STEPS")
    assert Settings(_env_file=None).local_image_steps == 4

    seen = {}

    class Recorder:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(factory_module, "MlxServeImageClient", Recorder)
    factory_module.build_service(settings(
        tmp_path, local_text_provider="mlx", local_image_provider="mlx", background_provider="mlx", local_image_steps=20,
    ))

    assert seen["steps"] == 20
