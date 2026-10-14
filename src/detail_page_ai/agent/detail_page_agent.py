"""A DetailPagePipeline whose steps are chosen by one tool-calling agent."""

import logging
from dataclasses import asdict
from typing import Any, Callable

from ..dto import ProductProfileDto, UserHintsDto
from ..pipeline import DetailPagePipeline
from ..validation import ensure_editorial_page_plan
from . import prompts
from .loop import FALLBACKS, AgentState, LoopOutcome, Tool, run_agent_loop
from .tools import draft_tools

logger = logging.getLogger(__name__)


class DetailPageAgent(DetailPagePipeline):
    """Overrides the compose/produce hooks; everything after them is unchanged."""

    def __init__(
        self,
        *,
        chat_client,
        max_tool_calls: int = 15,
        timeout_seconds: float = 600.0,
        edit_lock_timeout_seconds: float = 10.0,
        **pipeline_kwargs,
    ):
        super().__init__(**pipeline_kwargs)
        self.chat_client = chat_client
        self.max_tool_calls = max_tool_calls
        self.timeout_seconds = timeout_seconds
        self.edit_lock_timeout_seconds = edit_lock_timeout_seconds
        # ponytail: one slot per mode, valid because jobs run one at a time
        self.last_runs: dict[str, dict[str, Any]] = {}

    def _run_loop(
        self,
        state: AgentState,
        tools: dict[str, Tool],
        system_prompt: str,
        user_prompt: str,
        status_callback: Callable[[str, int], None] | None = None,
    ) -> LoopOutcome:
        outcome = run_agent_loop(
            client=self.chat_client,
            tools=tools,
            state=state,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tool_calls=self.max_tool_calls,
            timeout_seconds=self.timeout_seconds,
            on_status=status_callback,
        )
        self.last_runs[state.mode] = {
            "reason": outcome.reason,
            "trace": [asdict(entry) for entry in state.trace],
        }
        if not outcome.finished:
            FALLBACKS.labels(state.mode, outcome.reason).inc()
            logger.warning("agent %s mode stopped: %s", state.mode, outcome.reason)
        return outcome

    def _compose_profile(
        self,
        *,
        source_image: bytes,
        source_mime_type: str,
        additional_source_images: tuple[tuple[bytes, str], ...],
        user_hints: UserHintsDto | None,
        status_callback: Callable[[str, int], None],
    ) -> ProductProfileDto:
        state = AgentState(
            mode="draft",
            source_images=((source_image, source_mime_type),) + additional_source_images,
            user_hints=user_hints,
        )
        outcome = self._run_loop(
            state,
            draft_tools(analyze=self._analyze, chat_client=self.chat_client),
            prompts.DRAFT_SYSTEM_PROMPT,
            prompts.draft_user_prompt(len(state.source_images), user_hints),
            status_callback,
        )
        if outcome.finished and (state.profile is None or not state.plan_ready):
            FALLBACKS.labels("draft", "incomplete").inc()
        profile = state.profile or self._analyze(source_image, source_mime_type, user_hints)
        return profile if state.plan_ready else ensure_editorial_page_plan(profile)
