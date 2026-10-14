import json

import pytest
from prometheus_client import REGISTRY
from pydantic import BaseModel, ConfigDict, Field

from detail_page_ai.agent.loop import (
    NUDGE,
    AgentState,
    LoopOutcome,
    Tool,
    run_agent_loop,
)


class ScriptedClient:
    """Returns queued assistant turns in order and records every request."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.requests = []

    def chat_with_tools(self, messages, tools):
        self.requests.append(([dict(message) for message in messages], tools))
        if not self.turns:
            raise AssertionError("the loop asked for more turns than scripted")
        return self.turns.pop(0)


def call(name, arguments="{}", call_id=None):
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {"id": call_id or f"id-{name}", "name": name, "arguments": arguments}


def turn(*calls, content=""):
    return {"content": content, "tool_calls": list(calls)}


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EchoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)


def make_tools(**overrides):
    log = []

    def echo(state, args):
        log.append(args.text)
        return {"echo": args.text}

    def boom(state, args):
        raise RuntimeError("tool exploded")

    def finish(state, args):
        state.summary = "done"
        return {"ok": True}

    tools = {
        "echo": Tool("echo", "echo text", EchoArgs, echo, status=("ANALYZING", 15)),
        "boom": Tool("boom", "always fails", NoArgs, boom),
        "finish": Tool("finish", "stop", NoArgs, finish),
    }
    tools.update(overrides)
    return tools, log


def run(client, tools, **kwargs):
    state = AgentState(mode="draft", source_images=((b"img", "image/png"),))
    outcome = run_agent_loop(
        client=client,
        tools=tools,
        state=state,
        system_prompt="sys",
        user_prompt="user",
        **kwargs,
    )
    return outcome, state


def test_loop_runs_tools_until_finish_and_feeds_results_back():
    tools, log = make_tools()
    client = ScriptedClient(turn(call("echo", {"text": "a"})), turn(call("finish")))

    outcome, state = run(client, tools)

    assert outcome == LoopOutcome(True, "finished")
    assert log == ["a"]
    assert state.summary == "done"
    assert [entry.outcome for entry in state.trace] == ["ok", "ok"]
    second_request_messages = client.requests[1][0]
    assert second_request_messages[2]["tool_calls"][0]["function"]["name"] == "echo"
    assert second_request_messages[3] == {
        "role": "tool",
        "tool_call_id": "id-echo",
        "content": '{"echo": "a"}',
    }
    assert {schema["function"]["name"] for schema in client.requests[0][1]} == {
        "echo",
        "boom",
        "finish",
    }


def test_loop_reports_the_tool_status_before_running_it():
    tools, _ = make_tools()
    statuses = []
    client = ScriptedClient(turn(call("echo", {"text": "a"})), turn(call("finish")))

    run(client, tools, on_status=lambda status, progress: statuses.append((status, progress)))

    assert statuses == [("ANALYZING", 15)]


def test_status_callback_errors_propagate():
    class LeaseLost(Exception):
        pass

    def lose_lease(status, progress):
        raise LeaseLost()

    tools, log = make_tools()
    client = ScriptedClient(turn(call("echo", {"text": "a"})))

    with pytest.raises(LeaseLost):
        run(client, tools, on_status=lose_lease)
    assert log == []


def test_unknown_tool_is_rejected_and_the_model_can_recover():
    tools, _ = make_tools()
    client = ScriptedClient(turn(call("delete_everything")), turn(call("finish")))

    outcome, state = run(client, tools)

    assert outcome.finished
    assert state.trace[0].outcome == "rejected"
    assert "unknown tool" in client.requests[1][0][-1]["content"]


def test_invalid_arguments_are_reported_and_cost_budget():
    tools, log = make_tools()
    client = ScriptedClient(
        turn(call("echo", {"text": ""})),
        turn(call("echo", "not json")),
        turn(call("finish")),
    )

    outcome, state = run(client, tools, max_tool_calls=2)

    assert outcome == LoopOutcome(False, "budget")
    assert [entry.outcome for entry in state.trace] == ["invalid", "invalid"]
    assert log == []


def test_one_turn_cannot_exceed_budget():
    tools, log = make_tools()
    client = ScriptedClient(
        turn(
            call("echo", {"text": "a"}, "c1"),
            call("echo", {"text": "b"}, "c2"),
            call("echo", {"text": "c"}, "c3"),
        )
    )

    outcome, _ = run(client, tools, max_tool_calls=2)

    assert outcome == LoopOutcome(False, "budget")
    assert log == ["a", "b"]


def test_text_only_reply_gets_one_nudge_then_stops():
    tools, _ = make_tools()
    client = ScriptedClient(turn(content="생각 중"), turn(content="또 생각 중"))

    outcome, _ = run(client, tools)

    assert outcome == LoopOutcome(False, "no_tool_call")
    assert client.requests[1][0][-1] == {"role": "user", "content": NUDGE}


def test_nudged_model_can_still_finish():
    tools, _ = make_tools()
    client = ScriptedClient(turn(content="생각 중"), turn(call("finish")))

    outcome, _ = run(client, tools)

    assert outcome.finished


def test_same_tool_failing_three_times_in_a_row_stops_the_loop():
    tools, _ = make_tools()
    client = ScriptedClient(
        turn(call("boom")), turn(call("boom")), turn(call("boom"))
    )

    outcome, state = run(client, tools)

    assert outcome == LoopOutcome(False, "repeated_failure")
    assert state.trace[0].result == {"error": "RuntimeError", "detail": "tool exploded"}


def test_a_success_in_between_resets_the_failure_streak():
    tools, _ = make_tools()
    client = ScriptedClient(
        turn(call("boom")),
        turn(call("boom")),
        turn(call("echo", {"text": "a"})),
        turn(call("boom")),
        turn(call("finish")),
    )

    outcome, _ = run(client, tools)

    assert outcome.finished


def test_per_tool_call_limit_rejects_extra_calls():
    tools, log = make_tools()
    tools["echo"].max_calls = 1
    client = ScriptedClient(
        turn(call("echo", {"text": "a"})),
        turn(call("echo", {"text": "b"})),
        turn(call("finish")),
    )

    outcome, state = run(client, tools)

    assert outcome.finished
    assert log == ["a"]
    assert state.trace[1].outcome == "rejected"


def test_timeout_stops_before_the_next_model_call():
    tools, _ = make_tools()
    ticks = iter([0.0, 0.0, 100.0])
    client = ScriptedClient(turn(call("echo", {"text": "a"})))

    outcome, _ = run(client, tools, timeout_seconds=50, clock=lambda: next(ticks))

    assert outcome == LoopOutcome(False, "timeout")


def test_model_errors_propagate():
    class DownClient:
        def chat_with_tools(self, messages, tools):
            raise RuntimeError("model server is down")

    tools, _ = make_tools()

    with pytest.raises(RuntimeError, match="model server is down"):
        run(DownClient(), tools)


def test_tool_calls_are_counted_in_metrics():
    labels = {"mode": "draft", "tool": "echo", "outcome": "ok"}
    before = REGISTRY.get_sample_value("agent_tool_calls_total", labels) or 0
    tools, _ = make_tools()

    run(ScriptedClient(turn(call("echo", {"text": "a"})), turn(call("finish"))), tools)

    assert REGISTRY.get_sample_value("agent_tool_calls_total", labels) == before + 1


def test_model_errors_inside_a_tool_stop_the_loop_at_once():
    from detail_page_ai.ports import ModelCallError

    calls = []

    def model_down(state, args):
        calls.append(1)
        raise ModelCallError("vision model timed out")

    tools, _ = make_tools(down=Tool("down", "calls the model", NoArgs, model_down))
    client = ScriptedClient(turn(call("down")), turn(call("down")), turn(call("down")))

    with pytest.raises(ModelCallError):
        run(client, tools)
    assert calls == [1]


def test_deadline_is_checked_before_every_tool_in_a_turn():
    tools, log = make_tools()
    ticks = iter([0.0, 0.0, 0.0, 100.0])
    client = ScriptedClient(
        turn(call("echo", {"text": "a"}, "c1"), call("echo", {"text": "b"}, "c2"))
    )

    outcome, _ = run(client, tools, timeout_seconds=50, clock=lambda: next(ticks))

    assert outcome == LoopOutcome(False, "timeout")
    assert log == ["a"]


def test_exhausted_tools_are_no_longer_offered():
    tools, _ = make_tools()
    tools["echo"].max_calls = 1
    client = ScriptedClient(turn(call("echo", {"text": "a"})), turn(call("finish")))

    run(client, tools)

    offered = [{schema["function"]["name"] for schema in request[1]} for request in client.requests]
    assert offered == [{"echo", "boom", "finish"}, {"boom", "finish"}]


def test_capped_tools_state_their_limit_up_front():
    # Live MLX run: the model asked for 8 inspect_render calls in one turn
    # because nothing told it about the 2-call cap before it planned.
    tool = Tool("inspect", "검수한다.", NoArgs, lambda state, args: {}, max_calls=2)

    assert tool.schema()["function"]["description"] == "검수한다. 최대 2회까지 부를 수 있다."
