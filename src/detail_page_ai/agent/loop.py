"""Tool-calling loop shared by the draft, render and edit modes.

The model only chooses which tool runs next. What the service guarantees
(grounded copy, untouched source pixels, delivery) is enforced by the tools
and by the pipeline code that runs after this loop, never by the prompt.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

from prometheus_client import Counter, Histogram
from pydantic import BaseModel, ValidationError

from ..dto import ApprovedDraftDto, ProductProfileDto, UserHintsDto
from ..models import GeneratedImage, ProductPhotoSet
from ..ports import ModelCallError

logger = logging.getLogger(__name__)

Mode = Literal["draft", "render", "edit"]
NUDGE = "도구를 호출해서 작업을 이어가거나, 끝났으면 finish를 호출하세요."
MAX_RESULT_CHARS = 2000

TOOL_CALLS = Counter(
    "agent_tool_calls_total", "Agent tool calls", ["mode", "tool", "outcome"]
)
TOOL_SECONDS = Histogram("agent_tool_seconds", "Agent tool run time", ["mode", "tool"])
FALLBACKS = Counter(
    "agent_fallback_total",
    "Agent runs completed by the fixed pipeline",
    ["mode", "reason"],
)


class ToolChatClient(Protocol):
    def chat_with_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        ...


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    run: Callable[["AgentState", Any], dict[str, Any]]
    status: tuple[str, int] | None = None
    max_calls: int | None = None

    def schema(self) -> dict[str, Any]:
        description = self.description
        if self.max_calls is not None:
            description += f" 최대 {self.max_calls}회까지 부를 수 있다."
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,
                "parameters": self.args_model.model_json_schema(),
            },
        }


@dataclass(frozen=True, slots=True)
class TraceEntry:
    tool: str
    arguments: str
    outcome: Literal["ok", "invalid", "error", "rejected"]
    result: dict[str, Any]
    elapsed_ms: int


@dataclass(slots=True)
class AgentState:
    mode: Mode
    source_images: tuple[tuple[bytes, str], ...]
    user_hints: UserHintsDto | None = None
    profile: ProductProfileDto | None = None
    plan_ready: bool = False
    observations: list[str] = field(default_factory=list)
    photo_set: ProductPhotoSet | None = None
    generated_image: GeneratedImage | None = None
    rendered_photo_set: ProductPhotoSet | None = None
    draft: ApprovedDraftDto | None = None
    changes: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary: str = ""
    trace: list[TraceEntry] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    finished: bool
    reason: Literal[
        "finished", "budget", "timeout", "no_tool_call", "repeated_failure", "model_error"
    ]


def run_agent_loop(
    *,
    client: ToolChatClient,
    tools: dict[str, Tool],
    state: AgentState,
    system_prompt: str,
    user_prompt: str,
    max_tool_calls: int = 15,
    timeout_seconds: float = 600.0,
    on_status: Callable[[str, int], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> LoopOutcome:
    """Run tools until the model calls `finish` or a limit stops the loop.

    Model errors (ModelCallError), from the loop's own call or inside a tool,
    propagate so the caller can decide. Other tool errors go back to the model
    as the tool result.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    deadline = clock() + timeout_seconds
    calls = 0
    turns = 0
    counts: dict[str, int] = {}
    nudged = False
    failing_tool, failures = "", 0
    while True:
        if clock() >= deadline:
            return LoopOutcome(False, "timeout")
        # Rejected calls are free, so cap the model turns too; otherwise a model
        # that keeps calling capped or unknown tools would loop until the timeout.
        if turns >= max_tool_calls:
            return LoopOutcome(False, "budget")
        turns += 1
        # Stop offering tools that hit their cap; the model tends to keep calling them.
        schemas = [
            tool.schema()
            for tool in tools.values()
            if tool.max_calls is None or counts.get(tool.name, 0) < tool.max_calls
        ]
        reply = client.chat_with_tools(messages, schemas)
        tool_calls = reply["tool_calls"]
        if not tool_calls:
            if nudged:
                return LoopOutcome(False, "no_tool_call")
            nudged = True
            messages.append({"role": "assistant", "content": reply["content"]})
            messages.append({"role": "user", "content": NUDGE})
            continue
        messages.append(
            {
                "role": "assistant",
                "content": reply["content"],
                "tool_calls": [
                    {
                        "id": tool_call["id"],
                        "type": "function",
                        "function": {
                            "name": tool_call["name"],
                            "arguments": tool_call["arguments"],
                        },
                    }
                    for tool_call in tool_calls
                ],
            }
        )
        for tool_call in tool_calls:
            if calls >= max_tool_calls:
                return LoopOutcome(False, "budget")
            if clock() >= deadline:
                return LoopOutcome(False, "timeout")
            entry = _execute(tool_call, tools, state, counts, on_status)
            if entry.outcome != "rejected":  # a rejected call ran nothing
                calls += 1
            state.trace.append(entry)
            metric_tool = entry.tool if entry.tool in tools else "unknown"
            TOOL_CALLS.labels(state.mode, metric_tool, entry.outcome).inc()
            logger.info(
                "agent tool mode=%s tool=%s outcome=%s elapsed_ms=%d result=%s",
                state.mode,
                entry.tool,
                entry.outcome,
                entry.elapsed_ms,
                json.dumps(entry.result, ensure_ascii=False)[:300],
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": json.dumps(entry.result, ensure_ascii=False)[
                        :MAX_RESULT_CHARS
                    ],
                }
            )
            if entry.outcome == "error":
                failures = failures + 1 if entry.tool == failing_tool else 1
                failing_tool = entry.tool
                if failures >= 3:
                    return LoopOutcome(False, "repeated_failure")
            else:
                failing_tool, failures = "", 0
            if entry.tool == "finish" and entry.outcome == "ok":
                return LoopOutcome(True, "finished")


def _execute(
    tool_call: dict[str, Any],
    tools: dict[str, Tool],
    state: AgentState,
    counts: dict[str, int],
    on_status: Callable[[str, int], None] | None,
) -> TraceEntry:
    started = time.monotonic()

    def entry(outcome, result: dict[str, Any]) -> TraceEntry:
        return TraceEntry(
            tool=tool_call["name"],
            arguments=tool_call["arguments"][:500],
            outcome=outcome,
            result=result,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )

    tool = tools.get(tool_call["name"])
    if tool is None:
        return entry(
            "rejected",
            {"error": f"unknown tool: {tool_call['name']}", "available": sorted(tools)},
        )
    if tool.max_calls is not None and counts.get(tool.name, 0) >= tool.max_calls:
        return entry(
            "rejected",
            {"error": f"{tool.name} can be called at most {tool.max_calls} times"},
        )
    try:
        args = tool.args_model.model_validate(json.loads(tool_call["arguments"] or "{}"))
    except (json.JSONDecodeError, ValidationError) as exc:
        return entry("invalid", {"error": "invalid arguments", "detail": str(exc)[:500]})
    counts[tool.name] = counts.get(tool.name, 0) + 1
    # Outside the try: a lost job lease must stop the run, not become a tool error.
    if tool.status is not None and on_status is not None:
        on_status(*tool.status)
    try:
        with TOOL_SECONDS.labels(state.mode, tool.name).time():
            result = tool.run(state, args)
    except ModelCallError:
        raise  # retrying a dead or confused model only multiplies the wait
    except Exception as exc:  # reported to the model, which decides what to do next
        logger.warning("agent tool %s failed", tool.name, exc_info=True)
        return entry("error", {"error": type(exc).__name__, "detail": str(exc)[:300]})
    return entry("ok", result)
