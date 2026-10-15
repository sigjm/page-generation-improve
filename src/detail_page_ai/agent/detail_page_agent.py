"""A DetailPagePipeline whose steps are chosen by one tool-calling agent."""

import logging
from dataclasses import asdict, dataclass
from typing import Any, Callable

from ..dto import ApprovedDraftDto, ProductProfileDto, UserHintsDto
from ..models import GeneratedImage, GenerationOptions, ProductPhotoSet
from ..pipeline import DetailPagePipeline, ModelBusyError
from ..ports import ModelCallError
from ..validation import ensure_editorial_page_plan
from . import prompts
from .loop import FALLBACKS, AgentState, LoopOutcome, Tool, run_agent_loop
from .tools import draft_tools, edit_tools, render_tools

logger = logging.getLogger(__name__)


EDIT_FAILED_REPLY = "요청을 처리하지 못했어요. 바꿀 부분을 한 가지씩 구체적으로 다시 알려 주세요."


@dataclass(frozen=True, slots=True)
class DraftEditResult:
    draft: ApprovedDraftDto
    reply: str
    changes: list[dict]
    warnings: list[str]


class DetailPageAgent(DetailPagePipeline):
    """Overrides the compose/produce hooks; everything after them is unchanged."""

    def __init__(
        self,
        *,
        chat_client,
        max_tool_calls: int = 15,
        timeout_seconds: float = 600.0,
        edit_lock_timeout_seconds: float = 10.0,
        edit_max_tool_calls: int = 8,
        edit_timeout_seconds: float = 90.0,
        **pipeline_kwargs,
    ):
        super().__init__(**pipeline_kwargs)
        self.chat_client = chat_client
        self.max_tool_calls = max_tool_calls
        self.timeout_seconds = timeout_seconds
        self.edit_lock_timeout_seconds = edit_lock_timeout_seconds
        # An edit is a synchronous request holding the model lock: keep it short.
        self.edit_max_tool_calls = edit_max_tool_calls
        self.edit_timeout_seconds = edit_timeout_seconds
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
        editing = state.mode == "edit"
        try:
            outcome = run_agent_loop(
                client=self.chat_client,
                tools=tools,
                state=state,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tool_calls=self.edit_max_tool_calls if editing else self.max_tool_calls,
                timeout_seconds=self.edit_timeout_seconds if editing else self.timeout_seconds,
                on_status=status_callback,
            )
        except ModelCallError:
            if editing:
                raise
            # The fixed steps take over: rendering never needed the text model,
            # and a draft fails there as before if the model is really down.
            logger.warning("agent %s mode model call failed", state.mode, exc_info=True)
            outcome = LoopOutcome(False, "model_error")
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

    def _produce_page(
        self,
        *,
        source_image: bytes,
        source_mime_type: str,
        additional_source_images: tuple[tuple[bytes, str], ...],
        profile: ProductProfileDto,
        options: GenerationOptions,
        status_callback: Callable[[str, int], None],
    ) -> tuple[ProductPhotoSet, GeneratedImage]:
        source_images = ((source_image, source_mime_type),) + additional_source_images

        def generate_photos() -> ProductPhotoSet:
            return self._generate_photos(
                source_image=source_image,
                source_mime_type=source_mime_type,
                additional_source_images=additional_source_images,
                profile=profile,
                options=options,
            )

        def validate(photo_set: ProductPhotoSet) -> ProductPhotoSet:
            return self._validated_photo_set(photo_set, source_images)

        def render(photo_set: ProductPhotoSet) -> GeneratedImage:
            return self._render_page(
                source_image=source_image,
                source_mime_type=source_mime_type,
                profile=profile,
                options=options,
                photo_set=photo_set,
            )

        def regenerate(photo, direction):
            return self.photo_generator.regenerate_role(
                photo=photo,
                source_image=source_image,
                source_mime_type=source_mime_type,
                profile=profile,
                direction=direction,
            )

        state = AgentState(mode="render", source_images=source_images, profile=profile)
        outcome = self._run_loop(
            state,
            render_tools(
                generate_photos=generate_photos,
                validate=validate,
                photo_status=lambda photo: self.photo_validator.validate(
                    photo, source_images=source_images
                ),
                render=render,
                regenerate=(
                    regenerate if hasattr(self.photo_generator, "regenerate_role") else None
                ),
                chat_client=self.chat_client,
            ),
            prompts.RENDER_SYSTEM_PROMPT,
            prompts.render_user_prompt(profile, len(source_images)),
            status_callback,
        )
        # Always re-validate here: the agent may have skipped verify_photos.
        photo_set = validate(
            state.photo_set if state.photo_set is not None else generate_photos()
        )
        generated_image = state.generated_image
        if generated_image is None or state.rendered_photo_set != photo_set:
            if outcome.finished:
                FALLBACKS.labels("render", "incomplete").inc()
            generated_image = render(photo_set)
        if state.warnings:
            logger.warning("agent render warnings: %s", state.warnings)
        return photo_set, generated_image

    def edit_draft(
        self,
        *,
        draft: ApprovedDraftDto,
        instruction: str,
        source_images: tuple[tuple[bytes, str], ...],
        user_hints: UserHintsDto | None = None,
    ) -> DraftEditResult:
        """Apply one natural-language instruction; partial edits are discarded on failure."""
        if not self._model_lock.acquire(timeout=self.edit_lock_timeout_seconds):
            raise ModelBusyError("a generation job is using the model")
        try:
            state = AgentState(
                mode="edit", source_images=source_images, user_hints=user_hints, draft=draft
            )
            outcome = self._run_loop(
                state,
                edit_tools(chat_client=self.chat_client),
                prompts.EDIT_SYSTEM_PROMPT,
                prompts.edit_user_prompt(draft, instruction, len(source_images), user_hints),
            )
        finally:
            self._model_lock.release()
        if not outcome.finished:
            return DraftEditResult(draft, EDIT_FAILED_REPLY, [], state.warnings)
        return DraftEditResult(state.draft, state.summary, state.changes, state.warnings)
