import json

import pytest

from detail_page_ai.agent.detail_page_agent import DetailPageAgent
from local_detail_page_ai.runner import build_local_pipeline, save_agent_trace


def test_local_pipeline_can_be_built_as_the_agent():
    pipeline = build_local_pipeline(image_provider="none", agent_mode="agent")

    assert isinstance(pipeline, DetailPageAgent)


def test_local_agent_needs_a_tool_calling_text_server():
    with pytest.raises(ValueError, match="agent_mode"):
        build_local_pipeline(image_provider="none", text_provider="ollama", agent_mode="agent")


def test_agent_trace_is_saved_next_to_the_case_output(tmp_path):
    pipeline = build_local_pipeline(image_provider="none", agent_mode="agent")
    pipeline.last_runs = {"draft": {"reason": "finished", "trace": [{"tool": "finish"}]}}

    path = save_agent_trace(pipeline, tmp_path)

    assert json.loads(path.read_text(encoding="utf-8")) == pipeline.last_runs


def test_fixed_pipeline_has_no_trace(tmp_path):
    assert save_agent_trace(build_local_pipeline(image_provider="none"), tmp_path) is None
