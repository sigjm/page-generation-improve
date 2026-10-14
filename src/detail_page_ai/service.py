from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Timer
from typing import Protocol
import hashlib
import json
import uuid

from .backend_client import BackendDeliveryError
from .ai_dto import AiToBeStatusResponseDto
from .dto import (
    AiFeAcceptedResponse,
    AiFeDraftResultDto,
    AiFeStatusResponse,
    ErrorDto,
    GenerationOptions,
    UserHintsDto,
)
from .pipeline import DetailPagePipeline, DraftPipelineResult, PipelineResult
from .leases import LeaseHeartbeat
from .persistence import (
    IdempotencyKeyAlreadyExists,
    JobRecord,
    JobRepository,
    LeaseOwnershipError,
    MemoryJobRepository,
)
from .validation import InvalidImageError, ProfileValidationError, validate_source_image


class IdempotencyConflictError(ValueError):
    """The same idempotency key was reused for a different request."""


class ApprovalInProgressError(ValueError):
    """Another attempt currently owns approval of this draft."""


class CapacityExceededError(RuntimeError):
    """The AI worker has reached its configured active-job capacity."""


class DraftVersionConflictError(ValueError):
    """A stale editor tried to overwrite a newer draft revision."""


class JobExecutor(Protocol):
    def submit(self, function, *args, **kwargs):
        ...


class RetryScheduler(Protocol):
    def schedule(self, delay_seconds: float, function, *args) -> None:
        ...


class ThreadingRetryScheduler:
    def schedule(self, delay_seconds: float, function, *args) -> None:
        timer = Timer(delay_seconds, function, args=args)
        timer.daemon = True
        timer.start()


class DetailPageJobService:
    def __init__(
        self,
        *,
        pipeline: DetailPagePipeline,
        executor: JobExecutor | None = None,
        repository: JobRepository | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 900,
        heartbeat_interval_seconds: float = 60.0,
        retry_scheduler: RetryScheduler | None = None,
        max_pending_generations: int | None = None,
        max_image_bytes: int | None = None,
        max_source_images: int | None = None,
        max_total_input_bytes: int | None = None,
        max_delivery_attempts: int = 8,
        require_decodable_images: bool | None = None,
    ):
        self.pipeline = pipeline
        self.executor = executor or ThreadPoolExecutor(max_workers=2)
        self.repository = repository or MemoryJobRepository()
        self.worker_id = worker_id or f"job-worker-{uuid.uuid4()}"
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.retry_scheduler = retry_scheduler or ThreadingRetryScheduler()
        self.max_pending_generations = max_pending_generations or getattr(
            pipeline, "max_pending_generations", 100
        )
        self.max_image_bytes = max_image_bytes or getattr(
            pipeline, "max_image_bytes", 10 * 1024 * 1024
        )
        self.max_source_images = max_source_images or getattr(
            pipeline, "max_source_images", 12
        )
        self.max_total_input_bytes = max_total_input_bytes or getattr(
            pipeline, "max_total_input_bytes", 120 * 1024 * 1024
        )
        self.max_delivery_attempts = max_delivery_attempts
        if hasattr(self.pipeline.outbox, "configure_retry_limit"):
            self.pipeline.outbox.configure_retry_limit(max_delivery_attempts)
        self.require_decodable_images = (
            require_decodable_images
            if require_decodable_images is not None
            else getattr(pipeline, "require_decodable_images", False)
        )
        self.default_options = getattr(pipeline, "options", None) or GenerationOptions()
        # ponytail: assumes one process per database. A second process starting
        # against the same SQLite file would release the first one's live render
        # and allow a duplicate. Before scaling out, release only approvals whose
        # owner lease has expired instead of every IN_PROGRESS record.
        for approval_job_id in self.repository.list_in_progress_approvals():
            def release_interrupted(job: JobRecord) -> None:
                if job.approval_state == "IN_PROGRESS" and job.result is None:
                    job.approval_state = "FAILED"
                    job.approval_attempt_id = None
                    job.updated_at = datetime.now(timezone.utc)

            self.repository.update(approval_job_id, release_interrupted)
        for recovered_job_id in self.repository.claim_recoverable(
            self.worker_id, lease_seconds=self.lease_seconds
        ):
            recovered_job = self.repository.get(recovered_job_id)
            if recovered_job.generation_id:
                try:
                    recovered_job.result = self.pipeline.recover_fe_result(
                        recovered_job.generation_id
                    )
                    outbox_record = self.pipeline.outbox.get(
                        recovered_job.generation_id
                    )
                except KeyError:
                    self.executor.submit(
                        self._run_job, recovered_job_id, True, self.worker_id
                    )
                else:
                    def finish_recovered(job: JobRecord) -> None:
                        job.result = recovered_job.result
                        job.status = "COMPLETED"
                        job.progress = 100
                        job.error = (
                            None
                            if outbox_record.status == "DELIVERED"
                            else ErrorDto(
                                code="AI_BE_DELIVERY_FAILED",
                                message="Backend delivery is pending",
                                retryable=outbox_record.status != "PERMANENT_FAILED",
                                request_id=job.request_id,
                                generation_id=job.generation_id,
                            )
                        )
                        job.updated_at = datetime.now(timezone.utc)

                    self.repository.update(
                        recovered_job_id,
                        finish_recovered,
                        worker_id=self.worker_id,
                        lease_seconds=self.lease_seconds,
                    )
            else:
                self.executor.submit(
                    self._run_job, recovered_job_id, True, self.worker_id
                )
        for generation_id in self.pipeline.outbox.list_retryable():
            self.executor.submit(self._retry_delivery, generation_id)
        self._schedule_active_outbox_replay()

    def submit(
        self,
        source_image: bytes,
        source_mime_type: str,
        *,
        request_id: str | None = None,
        options: GenerationOptions | None = None,
        user_hints: UserHintsDto | None = None,
        additional_source_images: tuple[tuple[bytes, str], ...] = (),
        product_id: str | None = None,
        source_asset_id: str | None = None,
        idempotency_key: str | None = None,
        status_path_prefix: str = "/api/v1/ai/detail-page-jobs",
    ) -> AiFeAcceptedResponse:
        resolved_options = options or self.default_options
        all_images = ((source_image, source_mime_type),) + additional_source_images
        if len(all_images) > self.max_source_images:
            raise InvalidImageError("Too many source images")
        if sum(len(data) for data, _ in all_images) > self.max_total_input_bytes:
            raise InvalidImageError("Source images exceed the total request size")
        for additional_image, additional_mime_type in all_images:
            validate_source_image(
                additional_image,
                additional_mime_type,
                max_bytes=self.max_image_bytes,
                require_decodable=self.require_decodable_images,
            )
        resolved_request_id = request_id or str(uuid.uuid4())
        resolved_idempotency_key = idempotency_key or resolved_request_id
        request_fingerprint = self._request_fingerprint(
            source_image=source_image,
            source_mime_type=source_mime_type,
            additional_source_images=additional_source_images,
            options=resolved_options,
            user_hints=user_hints or UserHintsDto(),
            product_id=product_id,
            source_asset_id=source_asset_id,
        )
        existing = self.repository.find_by_idempotency(
            product_id, resolved_idempotency_key
        )
        if existing is not None:
            if existing.request_fingerprint != request_fingerprint:
                raise IdempotencyConflictError(
                    "Idempotency key is already used for a different request"
                )
            return AiFeAcceptedResponse(
                job_id=existing.job_id,
                request_id=existing.request_id,
                status="QUEUED",
                status_url=f"{status_path_prefix.rstrip('/')}/{existing.job_id}",
                created_at=existing.created_at,
            )
        if self.repository.count_active() >= self.max_pending_generations:
            raise CapacityExceededError("AI job capacity has been reached")
        job_id = str(uuid.uuid4())
        generation_id = (
            resolved_options.source_generation_id
            if product_id is not None and resolved_options.source_generation_id
            else str(uuid.uuid4())
        )
        job = JobRecord(
            job_id=job_id,
            request_id=resolved_request_id,
            generation_id=generation_id,
            source_image=source_image,
            source_mime_type=source_mime_type,
            options=resolved_options,
            product_id=product_id,
            source_asset_id=source_asset_id,
            user_hints=user_hints or UserHintsDto(),
            additional_source_images=additional_source_images,
            idempotency_key=resolved_idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        try:
            self.repository.create(job)
        except IdempotencyKeyAlreadyExists:
            existing = self.repository.find_by_idempotency(
                product_id, resolved_idempotency_key
            )
            if existing is None:
                raise
            if existing.request_fingerprint != request_fingerprint:
                raise IdempotencyConflictError(
                    "Idempotency key is already used for a different request"
                )
            return AiFeAcceptedResponse(
                job_id=existing.job_id,
                request_id=existing.request_id,
                status="QUEUED",
                status_url=f"{status_path_prefix.rstrip('/')}/{existing.job_id}",
                created_at=existing.created_at,
            )
        self.executor.submit(self._run_job, job_id)
        return AiFeAcceptedResponse(
            job_id=job_id,
            request_id=resolved_request_id,
            status="QUEUED",
            status_url=f"{status_path_prefix.rstrip('/')}/{job_id}",
            created_at=job.created_at,
        )

    def get(self, job_id: str) -> AiFeStatusResponse:
        job = self.repository.get(job_id)
        return AiFeStatusResponse(
            job_id=job.job_id,
            request_id=job.request_id,
            status=job.status,
            progress=job.progress,
            draft=job.draft,
            result=job.result,
            error=job.error,
            updated_at=job.updated_at,
        )

    def approve_draft(
        self,
        job_id: str,
        draft,
        *,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        options: GenerationOptions | None = None,
        product_id: str | None = None,
        source_asset_id: str | None = None,
    ) -> PipelineResult:
        """Render one approved draft and persist the idempotent final result."""
        resolved_key = idempotency_key or request_id or f"approval:{job_id}"
        attempt_id = str(uuid.uuid4())
        already_completed = False

        def reserve(job: JobRecord) -> None:
            nonlocal already_completed
            if job.draft is None or job.draft_profile is None:
                raise ValueError("Draft is not ready for approval")
            fingerprint = self._approval_fingerprint(draft, options or job.options)
            if job.approval_state in {"IN_PROGRESS", "COMPLETED"} or job.result is not None:
                if (
                    job.approval_idempotency_key != resolved_key
                    or job.approval_fingerprint != fingerprint
                ):
                    raise IdempotencyConflictError(
                        "Approval idempotency key is already used for a different request"
                    )
                if job.approval_state == "IN_PROGRESS":
                    raise ApprovalInProgressError("Approval is already in progress")
                already_completed = True
                return
            job.approval_idempotency_key = resolved_key
            job.approval_fingerprint = fingerprint
            job.approval_state = "IN_PROGRESS"
            job.approval_attempt_id = attempt_id
            job.approval_be_ack = None
            job.approval_backend_delivery_pending = False
            job.approval_warning = None
            job.updated_at = datetime.now(timezone.utc)

        job = self.repository.update(job_id, reserve)
        if already_completed:
            return PipelineResult(
                generation_id=job.result.generation_id,
                fe_result=job.result,
                be_ack=job.approval_be_ack,
                backend_delivery_pending=job.approval_backend_delivery_pending,
                warning=job.approval_warning,
            )

        try:
            resolved_request_id = request_id or str(uuid.uuid4())
            generation_id = (
                job.generation_id
                if job.product_id and job.generation_id
                else f"{job_id}-approval-{hashlib.sha256(resolved_key.encode()).hexdigest()[:16]}"
            )
            merged_profile = draft.to_profile(job.draft_profile)
            pipeline_result = self.pipeline.run(
                job_id=job_id,
                request_id=resolved_request_id,
                source_image=job.source_image,
                source_mime_type=job.source_mime_type,
                options=options or job.options,
                additional_source_images=job.additional_source_images,
                profile_override=merged_profile,
                user_hints=job.user_hints,
                generation_id=generation_id,
                product_id=product_id or job.product_id,
                source_asset_id=source_asset_id or job.source_asset_id,
            )

            def finish(current: JobRecord) -> None:
                if (
                    current.approval_state != "IN_PROGRESS"
                    or current.approval_attempt_id != attempt_id
                ):
                    raise ApprovalInProgressError("Approval is already in progress")
                if current.approval_be_ack is None:
                    current.approval_be_ack = pipeline_result.be_ack
                    current.approval_backend_delivery_pending = (
                        pipeline_result.backend_delivery_pending
                    )
                    current.approval_warning = pipeline_result.warning
                current.approval_state = "COMPLETED"
                current.approval_attempt_id = None
                current.result = pipeline_result.fe_result
                current.error = None
                current.status = "COMPLETED"
                current.progress = 100
                current.updated_at = datetime.now(timezone.utc)

            completed = self.repository.update(job_id, finish)
        except Exception:
            def release_failed(current: JobRecord) -> None:
                if (
                    current.approval_state == "IN_PROGRESS"
                    and current.approval_attempt_id == attempt_id
                ):
                    current.approval_state = "FAILED"
                    current.approval_attempt_id = None
                    current.updated_at = datetime.now(timezone.utc)

            self.repository.update(job_id, release_failed)
            raise

        if completed.approval_backend_delivery_pending:
            self._schedule_delivery_retry(pipeline_result.generation_id)
        return PipelineResult(
            generation_id=pipeline_result.generation_id,
            fe_result=completed.result,
            be_ack=completed.approval_be_ack,
            backend_delivery_pending=completed.approval_backend_delivery_pending,
            warning=completed.approval_warning,
        )

    def save_draft(
        self,
        job_id: str,
        draft,
        *,
        expected_version: int | None = None,
    ) -> AiFeDraftResultDto:
        """Persist creator edits and return only structured JSON for preview rendering."""
        snapshot = self.repository.get(job_id)
        if snapshot.draft_profile is None or snapshot.draft is None:
            raise ValueError("Draft is not ready")
        profile = draft.to_profile(snapshot.draft_profile)
        rebuilt = self.pipeline.build_editable_draft(
            job_id=snapshot.job_id,
            generation_id=snapshot.generation_id or self.pipeline.id_factory(),
            source_image=snapshot.source_image,
            source_mime_type=snapshot.source_mime_type,
            profile=profile,
            approved_draft=draft,
            source_asset_id=snapshot.source_asset_id or snapshot.draft.source_asset_id,
            user_hints=snapshot.user_hints,
        )

        def save(current: JobRecord) -> None:
            if current.draft_profile is None or current.draft is None:
                raise ValueError("Draft is not ready")
            if current.approval_state == "IN_PROGRESS":
                raise ValueError("Draft cannot be saved while approval is in progress")
            if current.approval_state == "COMPLETED" or current.result is not None:
                raise ValueError("Completed drafts cannot be edited")
            if (
                current.draft.version != snapshot.draft.version
                or (expected_version is not None and expected_version != current.draft.version)
            ):
                raise DraftVersionConflictError("Draft version is stale")
            current.draft = rebuilt.fe_draft.model_copy(
                update={"version": current.draft.version + 1}
            )
            current.draft_profile = rebuilt.profile
            current.updated_at = datetime.now(timezone.utc)

        return self.repository.update(job_id, save).draft

    def get_backend(self, job_id: str) -> AiToBeStatusResponseDto:
        job = self.repository.get(job_id)
        if not job.product_id:
            raise ValueError("Job is not associated with a Product BE product")
        return AiToBeStatusResponseDto.from_fe_status(
            product_id=job.product_id,
            response=self.get(job_id),
        )

    def _run_job(
        self,
        job_id: str,
        already_claimed: bool = False,
        claim_owner: str | None = None,
    ) -> None:
        owner = claim_owner or f"{self.worker_id}:{uuid.uuid4()}"
        job = (
            self.repository.get(job_id)
            if already_claimed
            else self.repository.claim(
                job_id, owner, lease_seconds=self.lease_seconds
            )
        )
        if job is None:
            return
        try:
            with LeaseHeartbeat(
                lambda: self.repository.renew_lease(
                    job_id, owner, lease_seconds=self.lease_seconds
                ),
                interval_seconds=self.heartbeat_interval_seconds,
            ) as heartbeat:
                pipeline_kwargs = {
                    "job_id": job.job_id,
                    "request_id": job.request_id,
                    "source_image": job.source_image,
                    "source_mime_type": job.source_mime_type,
                    "options": job.options,
                    "additional_source_images": job.additional_source_images,
                    "status_callback": lambda status, progress: self._update(
                        job_id, status, progress, worker_id=owner
                    ),
                    "generation_id": job.generation_id,
                }
                if job.user_hints.model_dump(exclude_none=True):
                    pipeline_kwargs["user_hints"] = job.user_hints
                if job.product_id is not None:
                    pipeline_kwargs["product_id"] = job.product_id
                if job.source_asset_id is not None:
                    pipeline_kwargs["source_asset_id"] = job.source_asset_id
                if hasattr(self.pipeline, "create_draft"):
                    draft_result: DraftPipelineResult = self.pipeline.create_draft(
                        job_id=job.job_id,
                        request_id=job.request_id,
                        source_image=job.source_image,
                        source_mime_type=job.source_mime_type,
                        additional_source_images=job.additional_source_images,
                        user_hints=job.user_hints,
                        source_asset_id=job.source_asset_id,
                        status_callback=pipeline_kwargs["status_callback"],
                        generation_id=job.generation_id,
                    )
                    heartbeat.ensure_active()
                    def finish_draft(current: JobRecord) -> None:
                        current.draft = draft_result.fe_draft
                        current.draft_profile = draft_result.profile
                        current.status = "DRAFT_READY"
                        current.progress = 100
                        current.error = None
                        current.updated_at = datetime.now(timezone.utc)

                    self.repository.update(
                        job_id,
                        finish_draft,
                        worker_id=owner,
                        lease_seconds=self.lease_seconds,
                    )
                    return
                pipeline_result: PipelineResult = self.pipeline.run(**pipeline_kwargs)
                heartbeat.ensure_active()
                warning = pipeline_result.warning
                error = None
                if warning:
                    error = ErrorDto(
                        code="AI_BE_DELIVERY_FAILED",
                        message="Backend delivery is pending.",
                        retryable=True,
                        request_id=job.request_id,
                        generation_id=pipeline_result.generation_id,
                    )
                def finish_result(current: JobRecord) -> None:
                    current.result = pipeline_result.fe_result
                    current.error = error
                    current.status = "COMPLETED"
                    current.progress = 100
                    current.updated_at = datetime.now(timezone.utc)

                self.repository.update(
                    job_id,
                    finish_result,
                    worker_id=owner,
                    lease_seconds=self.lease_seconds,
                )
                if warning:
                    self._schedule_delivery_retry(pipeline_result.generation_id)
        except LeaseOwnershipError:
            return
        except Exception as exc:
            retryable = getattr(exc, "retryable", None)
            if retryable is None:
                retryable = not isinstance(exc, (InvalidImageError, ProfileValidationError))
            code = self._error_code(exc)
            current = self.repository.get(job_id)
            try:
                self._update(
                    job_id,
                    "FAILED",
                    current.progress,
                    error=ErrorDto(
                        code=code,
                        message=self._public_error_message(exc),
                        retryable=retryable,
                        request_id=job.request_id,
                    ),
                    worker_id=owner,
                )
            except LeaseOwnershipError:
                return

    def _retry_delivery(self, generation_id: str) -> None:
        try:
            outbox_record = self.pipeline.outbox.get(generation_id)
            ack = self.pipeline.retry_backend_delivery(generation_id)
        except KeyError:
            return
        except BackendDeliveryError as exc:
            if exc.retryable:
                self._schedule_delivery_retry(generation_id)
            return
        try:
            def mark_delivered(job: JobRecord) -> None:
                job.error = None
                if job.approval_idempotency_key is not None:
                    job.approval_be_ack = ack
                    job.approval_backend_delivery_pending = False
                    job.approval_warning = None
                job.updated_at = datetime.now(timezone.utc)

            self.repository.update(outbox_record.request.job_id, mark_delivered)
        except KeyError:
            return

    def _schedule_active_outbox_replay(self) -> None:
        delay = self.pipeline.outbox.seconds_until_retryable()
        if delay is not None:
            self.retry_scheduler.schedule(
                delay + 0.05, self._replay_due_outbox_deliveries
            )

    def _schedule_delivery_retry(self, generation_id: str) -> None:
        try:
            record = self.pipeline.outbox.get(generation_id)
        except KeyError:
            return
        if record.status == "DELIVERING":
            self._schedule_active_outbox_replay()
            return
        if record.status != "FAILED" or record.attempts >= self.max_delivery_attempts:
            return
        delay = min(300.0, float(2 ** max(0, min(record.attempts - 1, 8))))
        self.retry_scheduler.schedule(delay, self._retry_delivery, generation_id)

    def _replay_due_outbox_deliveries(self) -> None:
        retryable = self.pipeline.outbox.list_retryable()
        for generation_id in retryable:
            self.executor.submit(self._retry_delivery, generation_id)
        self._schedule_active_outbox_replay()

    def _update(
        self,
        job_id: str,
        status: str,
        progress: int,
        *,
        error: ErrorDto | None = None,
        worker_id: str | None = None,
    ) -> None:
        def apply_status(job: JobRecord) -> None:
            job.status = status
            job.progress = progress
            job.error = error
            job.updated_at = datetime.now(timezone.utc)

        self.repository.update(
            job_id,
            apply_status,
            worker_id=worker_id,
            lease_seconds=self.lease_seconds,
        )

    @staticmethod
    def _error_code(exc: Exception) -> str:
        if isinstance(exc, InvalidImageError):
            return "INVALID_IMAGE"
        if isinstance(exc, ProfileValidationError):
            return "AI_RESPONSE_INVALID"
        if isinstance(exc, BackendDeliveryError):
            return "AI_BE_DELIVERY_FAILED"
        return "AI_JOB_FAILED"

    @staticmethod
    def _public_error_message(exc: Exception) -> str:
        if isinstance(exc, InvalidImageError):
            return "Uploaded image is invalid."
        if isinstance(exc, ProfileValidationError):
            return "AI response could not be validated. Please retry."
        if isinstance(exc, BackendDeliveryError):
            return "Backend delivery is pending."
        return "AI processing failed. Please retry."

    @staticmethod
    def _request_fingerprint(**values) -> str:
        normalized = {
            "source_image_sha256": hashlib.sha256(values.pop("source_image")).hexdigest(),
            "source_mime_type": values.pop("source_mime_type"),
            "additional_source_images": [
                {"sha256": hashlib.sha256(data).hexdigest(), "mime_type": mime}
                for data, mime in values.pop("additional_source_images")
            ],
            **{
                key: value.model_dump(mode="json") if hasattr(value, "model_dump") else value
                for key, value in values.items()
            },
        }
        encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _approval_fingerprint(draft, options: GenerationOptions) -> str:
        payload = {
            "draft": draft.model_dump(mode="json"),
            "options": options.model_dump(mode="json"),
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
