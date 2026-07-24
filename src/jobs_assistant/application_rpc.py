"""Persistent coordinator for guarded application RPC runs.

The coordinator is intentionally boring at the transport edge: the native OMP
process owns one prompt at a time, while this module owns durable request
reservation, child proposal handoff, and lifecycle projection. Private source
values never cross this boundary; the application workflow remains the only
component allowed to mutate the browser.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import db as _db
from .application import (
    DEFAULT_APPLICATION_PROFILE_SHA256,
    ApplicationWorkflowControl,
    run_application_workflow,
)
from .application_profiles import load_application_profile_preset
from .application_preferences import ApplicationPreferences, load_application_preferences
from .application_rpc_contracts import (
    APPLICATION_RPC_PROTOCOL_VERSION,
    BROWSER_OPERATIONS,
    BrowserToolProposal,
    HostToolContext,
    ApplicationRpcError,
    ApplicationRpcRequest,
    build_application_response,
    build_rejected_application_response,
    parse_application_request,
    parse_application_response,
    validate_public_result,
)
from .artifacts import ArtifactRoot
from .ats import (
    load_application_profile_snapshot,
    load_applicant_description,
    load_resume_context,
)
from .omp_rpc import (
    OmpHostInvocation,
    OmpHostDurabilityError,
    OmpRpcCleanupError,
    OmpRpcLaunchConfig,
    OmpRpcProcess,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_DEADLINE_SECONDS = 300.0

_RUNTIME_LEASE_LOCK = threading.Lock()
_RUNTIME_LEASES: set[tuple[int, str]] = set()


class ApplicationRpcServiceError(RuntimeError):
    """A fixed-message service failure safe to expose to an RPC caller."""

    _MESSAGES = {
        "invalid_config": "Application coordinator configuration is invalid",
        "unavailable": "Application coordinator is unavailable",
        "internal_error": "Internal application error",
        "request_conflict": "Request identifier conflicts with prior intent",
        "request_incomplete": "Prior request outcome is incomplete",
        "run_not_found": "Application run was not found",
        "run_not_owned": "Application run is not owned by this coordinator",
        "run_not_active": "Application run is not active",
        "deadline_exceeded": "Request deadline exceeded",
        "cancelled": "Application run was cancelled",
    }

    def __init__(self, code: str = "internal_error") -> None:
        if code not in self._MESSAGES:
            code = "internal_error"
        self.code = code
        super().__init__(self._MESSAGES[code])


class ApplicationRpcDurabilityError(RuntimeError):
    """The transport must close because no durable RPC response exists."""


_LAUNCH_CANCEL_GRACE_SECONDS = 1.0
_CLEANUP_DRAIN_SECONDS = 5.0
_CANCELLED_TASK_DRAIN_SECONDS = 1.0


def _coordinator_instance_identity(
    db_path: str | os.PathLike[str],
    artifact_root: str | os.PathLike[str],
) -> str:
    """Derive a stable opaque ID for one database/artifact instance."""
    def component(value: str | os.PathLike[str], label: str) -> str:
        raw = str(value)
        if raw == ":memory:":
            return f"{label}:memory"
        try:
            canonical = Path(value).expanduser().resolve(strict=False)
        except Exception:
            canonical = Path(raw)
        return f"{label}:canonical:{canonical}"

    payload = "\0".join(
        (
            "application-rpc-instance-v1",
            component(db_path, "db"),
            component(artifact_root, "artifact"),
        )
    ).encode("utf-8", "surrogatepass")
    return "rpc-instance-" + hashlib.sha256(payload).hexdigest()

def _runtime_lease_key(
    db_path: str | os.PathLike[str],
    artifact_root: str | os.PathLike[str],
) -> tuple[int, str]:
    return (os.getpid(), _coordinator_instance_identity(db_path, artifact_root))


def _acquire_runtime_lease(
    db_path: str | os.PathLike[str],
    artifact_root: str | os.PathLike[str],
) -> tuple[int, str] | None:
    key = _runtime_lease_key(db_path, artifact_root)
    with _RUNTIME_LEASE_LOCK:
        if key in _RUNTIME_LEASES:
            return None
        _RUNTIME_LEASES.add(key)
    return key


def _release_runtime_lease(key: tuple[int, str] | None) -> None:
    if key is None:
        return
    with _RUNTIME_LEASE_LOCK:
        _RUNTIME_LEASES.discard(key)


@dataclass(frozen=True, slots=True)
class _ResolvedIdentity:
    configured_resume_id: str
    candidate_profile_id: str


@dataclass(frozen=True, slots=True, init=False)
class ApplicationRpcServiceConfig:
    """Validated immutable seams for one coordinator service.

    Path values are stored only in private slots and are intentionally omitted
    from ``repr``.  The public request carries retained-source hashes; this
    configuration resolves those hashes from the exact loader snapshots before
    any database claim is attempted.
    """

    _db_path: str | Path
    _artifact_root: str | Path
    _resume_file: str | Path
    _application_profile_json: str | Path | None
    _application_profile_preset: str | None
    _application_profile_dir: str | Path | None
    _application_preferences: str | Path | None
    _applicant_description_file: str | Path | None
    _ats: str
    _headed: bool
    _omp_launch_config: OmpRpcLaunchConfig | None
    _omp_launch_config_factory: Callable[..., Any] | None
    _omp_process_factory: Callable[..., Any] | None
    _workflow: Callable[..., Any] | None
    _connection_factory: Callable[..., Any] | None
    _coordinator_id: str
    _owner: str | None
    _event_callback: Callable[[Mapping[str, object]], Any] | None

    def __init__(
        self,
        db_path: str | os.PathLike[str] = "data/jobs.sqlite3",
        artifact_root: str | os.PathLike[str] = "data/application-runs",
        *,
        resume_file: str | os.PathLike[str] = "resume/Main_Resume.pdf",
        application_profile_json: str | os.PathLike[str] | None = None,
        application_profile_preset: str | None = None,
        application_profile_dir: str | os.PathLike[str] | None = None,
        application_preferences: str | os.PathLike[str] | None = None,
        applicant_description_file: str | os.PathLike[str] | None = None,
        ats: str = "auto",
        headed: bool = True,
        omp_launch_config: OmpRpcLaunchConfig | None = None,
        omp_launch_config_factory: Callable[..., Any] | None = None,
        omp_process_factory: Callable[..., Any] | None = None,
        workflow: Callable[..., Any] | None = None,
        workflow_factory: Callable[..., Any] | None = None,
        connection_factory: Callable[..., Any] | None = None,
        coordinator_id: str | None = None,
        owner: str | None = None,
        event_callback: Callable[[Mapping[str, object]], Any] | None = None,
    ) -> None:
        if application_profile_json is not None and application_profile_preset is not None:
            raise ApplicationRpcServiceError("invalid_config")
        if application_profile_dir is not None and application_profile_preset is None:
            raise ApplicationRpcServiceError("invalid_config")
        if omp_launch_config is not None and omp_launch_config_factory is not None:
            raise ApplicationRpcServiceError("invalid_config")
        if workflow is not None and workflow_factory is not None:
            raise ApplicationRpcServiceError("invalid_config")
        if type(ats) is not str or ats not in {"auto", "greenhouse", "lever"}:
            raise ApplicationRpcServiceError("invalid_config")
        if type(headed) is not bool or headed is not True:
            raise ApplicationRpcServiceError("invalid_config")
        if coordinator_id is None:
            try:
                coordinator_id = _coordinator_instance_identity(
                    db_path,
                    artifact_root,
                )
            except Exception:
                raise ApplicationRpcServiceError("invalid_config") from None
        if type(coordinator_id) is not str or not coordinator_id or len(coordinator_id) > 256:
            raise ApplicationRpcServiceError("invalid_config")
        if owner is not None and (type(owner) is not str or not owner.strip()):
            raise ApplicationRpcServiceError("invalid_config")
        if event_callback is not None and not callable(event_callback):
            raise ApplicationRpcServiceError("invalid_config")
        if connection_factory is not None and not callable(connection_factory):
            raise ApplicationRpcServiceError("invalid_config")
        if omp_launch_config_factory is not None and not callable(omp_launch_config_factory):
            raise ApplicationRpcServiceError("invalid_config")
        if omp_process_factory is not None and not callable(omp_process_factory):
            raise ApplicationRpcServiceError("invalid_config")
        selected_workflow = workflow if workflow is not None else workflow_factory
        if selected_workflow is not None and not callable(selected_workflow):
            raise ApplicationRpcServiceError("invalid_config")
        object.__setattr__(self, "_db_path", db_path)
        object.__setattr__(self, "_artifact_root", artifact_root)
        object.__setattr__(self, "_resume_file", resume_file)
        object.__setattr__(self, "_application_profile_json", application_profile_json)
        object.__setattr__(self, "_application_profile_preset", application_profile_preset)
        object.__setattr__(self, "_application_profile_dir", application_profile_dir)
        object.__setattr__(self, "_application_preferences", application_preferences)
        object.__setattr__(self, "_applicant_description_file", applicant_description_file)
        object.__setattr__(self, "_ats", ats)
        object.__setattr__(self, "_headed", headed)
        object.__setattr__(self, "_omp_launch_config", omp_launch_config)
        object.__setattr__(self, "_omp_launch_config_factory", omp_launch_config_factory)
        object.__setattr__(self, "_omp_process_factory", omp_process_factory)
        object.__setattr__(self, "_workflow", selected_workflow)
        object.__setattr__(self, "_connection_factory", connection_factory)
        object.__setattr__(self, "_coordinator_id", coordinator_id)
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_event_callback", event_callback)

    def __repr__(self) -> str:
        return (
            "ApplicationRpcServiceConfig(<private paths>, "
            f"ats={self._ats!r}, coordinator_id={self._coordinator_id!r})"
        )

    @property
    def ats(self) -> str:
        return self._ats

    @property
    def headed(self) -> bool:
        return self._headed

    @property
    def coordinator_id(self) -> str:
        return self._coordinator_id

    @property
    def event_callback(self) -> Callable[[Mapping[str, object]], Any] | None:
        return self._event_callback


# The function is deliberately usable both with a config and with explicit
# loader arguments, which keeps it useful in startup validation tests without
# exposing a path-bearing identity object.
def resolve_application_rpc_identity(
    config: ApplicationRpcServiceConfig | None = None,
    *,
    resume_file: str | os.PathLike[str] | None = None,
    application_profile_json: str | os.PathLike[str] | None = None,
    application_profile_preset: str | None = None,
    application_profile_dir: str | os.PathLike[str] | None = None,
) -> Mapping[str, str]:
    if config is not None:
        if not isinstance(config, ApplicationRpcServiceConfig):
            raise ApplicationRpcServiceError("invalid_config")
        resume_file = config._resume_file
        application_profile_json = config._application_profile_json
        application_profile_preset = config._application_profile_preset
        application_profile_dir = config._application_profile_dir
    if resume_file is None:
        resume_file = "resume/Main_Resume.pdf"
    if application_profile_json is not None and application_profile_preset is not None:
        raise ApplicationRpcServiceError("invalid_config")
    if application_profile_dir is not None and application_profile_preset is None:
        raise ApplicationRpcServiceError("invalid_config")
    try:
        with load_resume_context(resume_file) as resume:
            resume_id = resume.sha256
        if application_profile_preset is not None:
            if application_profile_dir is None:
                raise ValueError("profile directory")
            profile_id = load_application_profile_preset(
                application_profile_dir,
                application_profile_preset,
                cwd=Path.cwd(),
            ).source_sha256
        else:
            loaded = load_application_profile_snapshot(application_profile_json)
            profile_id = loaded.source_sha256 or DEFAULT_APPLICATION_PROFILE_SHA256
    except Exception:
        raise ApplicationRpcServiceError("invalid_config") from None
    if not _SHA256_RE.fullmatch(resume_id) or not _SHA256_RE.fullmatch(profile_id):
        raise ApplicationRpcServiceError("invalid_config")
    return MappingProxyType(
        {
            "configured_resume_id": resume_id,
            "candidate_profile_id": profile_id,
        }
    )


def public_rpc_event(event: _db.RpcEventInfo | Mapping[str, object]) -> Mapping[str, object]:
    """Project one durable event without exposing private request/artifact data."""
    if isinstance(event, _db.RpcEventInfo):
        value = {
            "run_id": event.run_id,
            "sequence": event.sequence,
            "request_id": event.request_id,
            "action_sequence": event.action_sequence,
            "timestamp": event.timestamp,
            "event_type": event.event_type,
            "summary_code": event.summary_code,
            "observation_sha256": event.observation_sha256,
        }
    elif isinstance(event, Mapping):
        allowed = {
            "run_id",
            "sequence",
            "request_id",
            "action_sequence",
            "timestamp",
            "event_type",
            "summary_code",
            "observation_sha256",
        }
        if set(event) != allowed:
            raise ApplicationRpcServiceError("internal_error")
        value = {key: event[key] for key in allowed}
    else:
        raise ApplicationRpcServiceError("internal_error")
    if type(value["run_id"]) is not int or value["run_id"] <= 0:
        raise ApplicationRpcServiceError("internal_error")
    if type(value["sequence"]) is not int or value["sequence"] <= 0:
        raise ApplicationRpcServiceError("internal_error")
    if type(value["action_sequence"]) is not int or value["action_sequence"] < 0:
        raise ApplicationRpcServiceError("internal_error")
    for key in ("request_id", "timestamp", "event_type", "summary_code"):
        if type(value[key]) is not str or not value[key]:
            raise ApplicationRpcServiceError("internal_error")
    observation = value["observation_sha256"]
    if observation is not None and (type(observation) is not str or not _SHA256_RE.fullmatch(observation)):
        raise ApplicationRpcServiceError("internal_error")
    return MappingProxyType(dict(value))

def _build_error_response(
    request: ApplicationRpcRequest,
    *,
    error: str,
    run_id: int | None = None,
    state: str = "failed",
    action_sequence: int = 0,
    event_sequence: int = 0,
) -> Mapping[str, object]:
    response = build_application_response(
        request,
        ok=False,
        state=state,
        action_sequence=max(0, action_sequence),
        event_sequence=max(0, event_sequence),
        error=error,
        run_id=run_id,
    )
    parse_application_response(response, request=request)
    return response


@dataclass(slots=True)
class _PendingProposal:
    proposal: BrowserToolProposal
    workflow_sequence: int
    future: asyncio.Future[object]
    invocation: Any
    mode: str
    dispatched: bool = False

@dataclass(slots=True)
class _ChildFlight:
    semantic_sha256: str
    future: asyncio.Future[object]

@dataclass(slots=True)
class _LifecycleFlight:
    semantic_sha256: str
    future: asyncio.Future[Mapping[str, object]]
    owner_task: asyncio.Task[Any] | None = None


class _LifecycleDeadlineExceeded(asyncio.TimeoutError):
    """One lifecycle wait crossed its request deadline."""



@dataclass(slots=True)
class _ActiveRun:
    run_id: int
    claim: _db.ApplicationClaim
    start_request: ApplicationRpcRequest
    control: "RpcApplicationControl"
    process: Any
    preferences: ApplicationPreferences
    applicant_description: str
    workflow_task: asyncio.Task[Any] | None = None


class RpcApplicationControl(ApplicationWorkflowControl):
    """Workflow control bridge for one durable RPC run."""

    def __init__(
        self,
        *,
        db_path: str | os.PathLike[str] | None = None,
        artifact_root: str | os.PathLike[str] | None = None,
        coordinator_id: str,
        connection_factory: Callable[..., Any] | None = None,
        event_callback: Callable[[Mapping[str, object]], Any] | None = None,
        process: Any | None = None,
        run_id: int | None = None,
        parent_request: ApplicationRpcRequest | None = None,
    ) -> None:
        if type(coordinator_id) is not str or not coordinator_id:
            raise ApplicationRpcServiceError("invalid_config")
        self._db_path = db_path
        self._artifact_root = artifact_root
        self._coordinator_id = coordinator_id
        self._connection_factory = connection_factory or _db.connect
        self._event_callback = event_callback
        self._process = process
        self._run_id = run_id
        self._parent_request_id = parent_request.request_id if parent_request is not None else None
        self._deadline_unix_ms = parent_request.deadline_unix_ms if parent_request is not None else 0
        self._start_request_id = self._parent_request_id
        self._proposal_surface_future: asyncio.Future[object] | None = None
        self._job_id: int | None = None
        self._application_url: str | None = None
        self._ats_policy: str | None = None
        self._session_id: str | None = None
        self._action_sequence = 0
        self._event_sequence = 0
        self._last_observation_sha256: str | None = None
        self._public_observation: Mapping[str, object] | None = None
        self._pending: _PendingProposal | None = None
        self._finished: dict[str, Mapping[str, object]] = {}
        self._child_flights: dict[str, _ChildFlight] = {}
        self._invocations: dict[str, Any] = {}
        self._db_lock = asyncio.Lock()
        self._prompt_lock = asyncio.Lock()
        self._cancel_event = asyncio.Event()
        self._resume_event = asyncio.Event()
        self._awaiting_resume = False
        self._prompt_mode = "action"
        self._prompt_observed = False
        self._prompt_workflow_sequence: int | None = None
        self._workflow_sequence = 0
        self._prompt_action = False
        self._prompt_task: asyncio.Task[Any] | None = None
        self._workflow_task: asyncio.Task[Any] | None = None
        self._prompt_failed = False
        self._coordinator_state = "starting"
        self._browser_state = "not_started"
        self._post_commit_guard = False
        self._handoff_intent: Mapping[str, Any] | None = None
        self._pending_handoff_result: Mapping[str, Any] | None = None
        self._pending_handoff_finalization: Mapping[str, Any] | None = None
        self._pending_handoff_state: str | None = None
        self._handoff_committed = False
        self._closed = False

    @property
    def run_id(self) -> int | None:
        return self._run_id

    @property
    def awaiting_resume(self) -> bool:
        return self._awaiting_resume

    def set_workflow_task(self, task: asyncio.Task[Any]) -> None:
        if not isinstance(task, asyncio.Task):
            raise ApplicationRpcServiceError("internal_error")
        self._workflow_task = task

    @property
    def post_commit_guard(self) -> bool:
        return self._post_commit_guard

    @property
    def released_pending_finalization(self) -> bool:
        return self._post_commit_guard and not self._handoff_committed

    def set_session_id(self, session_id: str) -> None:
        if type(session_id) is not str or not session_id:
            raise ApplicationRpcServiceError("internal_error")
        self._session_id = session_id

    async def reconcile_postcommit_handoff_failure(
        self,
        run_id: int,
        *,
        session_id: str | None,
        artifact_root: ArtifactRoot,
    ) -> bool:
        if (
            self._run_id != run_id
            or not self._post_commit_guard
            or not self._handoff_committed
            or not isinstance(artifact_root, ArtifactRoot)
            or (
                session_id is not None
                and self._session_id is not None
                and session_id != self._session_id
            )
        ):
            return False
        async with self._db_lock:
            connection = self._connection()
            try:
                reconciled = bool(
                    _db.reconcile_committed_handoff_failure(
                        connection,
                        run_id=run_id,
                        coordinator_id=self._coordinator_id,
                        artifact_root=artifact_root,
                    )
                )
            except Exception:
                return False
            finally:
                connection.close()
        if reconciled:
            self._handoff_committed = True
            self._browser_state = "failed"
            self._coordinator_state = "terminal"
        return reconciled

    def mark_handoff_committed(self) -> None:
        self._post_commit_guard = True
        self._browser_state = "open_guarded"
    @property
    def requires_handoff_intent(self) -> bool:
        return True
    @property
    def handoff_committed(self) -> bool:
        return self._handoff_committed

    @property
    def browser_state(self) -> str:
        return self._browser_state

    @property
    def coordinator_state(self) -> str:
        return self._coordinator_state

    @property
    def process(self) -> Any | None:
        return self._process

    def set_process(self, process: Any) -> None:
        self._process = process
        if process is not None:
            self._browser_state = "owned"

    def _connection(self) -> Any:
        factory = self._connection_factory
        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError):
            # Inspection failure chooses one shape; invocation errors must
            # propagate without a retry that could duplicate side effects.
            return factory(self._db_path)
        parameters = tuple(signature.parameters.values())
        positional = tuple(
            parameter
            for parameter in parameters
            if parameter.kind
            in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        )
        if positional or any(parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
            return factory(self._db_path)
        return factory()
    async def _read_status(self, run_id: int) -> _db.RpcRunStatus | None:
        """Read one durable run status through this control's DB seam."""
        connection = self._connection()
        try:
            return _db.get_rpc_run_status(connection, run_id)
        except ApplicationRpcDurabilityError:
            raise
        except Exception as exc:
            raise ApplicationRpcDurabilityError(
                "RPC failure status could not be read"
            ) from exc
        finally:
            connection.close()


    async def _emit(self, event: _db.RpcEventInfo | None) -> None:
        if event is None or self._event_callback is None:
            return
        try:
            value = self._event_callback(public_rpc_event(event))
            if inspect.isawaitable(value):
                await value
        except Exception:
            # A progress subscriber is not allowed to poison a workflow.
            return

    def _error_response(
        self,
        request: ApplicationRpcRequest,
        *,
        error: str,
        run_id: int | None = None,
        state: str = "failed",
        action_sequence: int = 0,
        event_sequence: int = 0,
    ) -> Mapping[str, object]:
        return _build_error_response(
            request,
            error=error,
            run_id=run_id,
            state=state,
            action_sequence=action_sequence,
            event_sequence=event_sequence,
        )

    def _next_sequence_locked(self) -> int:
        self._action_sequence = max(self._action_sequence, 0) + 1
        return self._action_sequence

    def _refresh_sequences_locked(self, connection: Any) -> None:
        if self._run_id is None:
            return
        status = _db.get_rpc_run_status(connection, self._run_id)
        if status is not None:
            self._action_sequence = max(self._action_sequence, status.action_sequence)
            self._event_sequence = max(self._event_sequence, status.latest_event_sequence)
            self._last_observation_sha256 = status.last_observation_sha256
            self._handoff_committed = status.handoff_committed

    def _event_request_id(self, request_id: str | None = None) -> str:
        candidate = request_id or self._parent_request_id or self._start_request_id
        if candidate is None:
            raise ApplicationRpcServiceError("internal_error")
        return candidate

    async def on_claimed(self, run_id: int, job_id: int, ats_policy: str, application_url: str) -> None:
        if self._run_id is not None and self._run_id != run_id:
            raise ApplicationRpcServiceError("internal_error")
        self._run_id = run_id
        self._job_id = job_id
        self._ats_policy = ats_policy
        self._application_url = application_url
        self._coordinator_state = "executing"
        event: _db.RpcEventInfo | None = None
        async with self._db_lock:
            connection = self._connection()
            try:
                self._refresh_sequences_locked(connection)
                sequence = self._next_sequence_locked()
                event = _db.commit_rpc_run_transition(
                    connection,
                    _db.RpcRunTransition(
                        run_id=run_id,
                        coordinator_id=self._coordinator_id,
                        request_id=self._event_request_id(),
                        action_sequence=sequence,
                        event_type="run_started",
                        summary_code="started",
                        state="running",
                        ats_policy=ats_policy,
                    ),
                )
                self._event_sequence = event.sequence
            finally:
                connection.close()
    async def cancellation_requested(self, run_id: int) -> bool:
        if self._cancel_event.is_set():
            return True
        connection = self._connection()
        try:
            return bool(_db.read_rpc_cancellation(connection, run_id))
        except Exception:
            return self._cancel_event.is_set()
        finally:
            connection.close()

    async def record_progress(
        self,
        run_id: int,
        event_type: str,
        summary_code: str,
        action_sequence: int,
        observation_sha256: str | None = None,
        request_id: str | None = None,
    ) -> None:
        if self._run_id != run_id or self._handoff_committed or self._post_commit_guard:
            return
        event: _db.RpcEventInfo | None = None
        async with self._db_lock:
            connection = self._connection()
            try:
                self._refresh_sequences_locked(connection)
                if self._handoff_committed or self._post_commit_guard:
                    return
                status = _db.get_rpc_run_status(connection, run_id)
                if status is None:
                    raise ApplicationRpcServiceError("internal_error")
                if observation_sha256 is not None and not _SHA256_RE.fullmatch(observation_sha256):
                    raise ApplicationRpcServiceError("internal_error")
                changed_observation = (
                    observation_sha256 is not None
                    and observation_sha256 != status.last_observation_sha256
                )
                if event_type == "page_observed" and observation_sha256 is None:
                    raise ApplicationRpcServiceError("internal_error")
                if changed_observation:
                    sequence = self._next_sequence_locked()
                    event = _db.commit_rpc_run_transition(
                        connection,
                        _db.RpcRunTransition(
                            run_id=run_id,
                            coordinator_id=self._coordinator_id,
                            request_id=self._event_request_id(request_id),
                            action_sequence=sequence,
                            event_type=str(event_type),
                            summary_code=getattr(summary_code, "value", str(summary_code)),
                            observation_sha256=observation_sha256,
                        ),
                    )
                    self._last_observation_sha256 = observation_sha256
                else:
                    sequence = status.action_sequence
                    event_sequence = _db.append_rpc_event(
                        connection,
                        run_id=run_id,
                        event_type=str(event_type),
                        summary_code=getattr(summary_code, "value", str(summary_code)),
                        request_id=self._event_request_id(request_id),
                        action_sequence=sequence,
                        observation_sha256=observation_sha256,
                    )
                    self._event_sequence = event_sequence
                    event = _db.latest_rpc_event(connection, run_id)
                if event is not None:
                    self._event_sequence = event.sequence
            finally:
                connection.close()
        await self._emit(event)

    def _remaining_seconds(self) -> float:
        if self._deadline_unix_ms <= 0:
            return _MAX_DEADLINE_SECONDS
        remaining = (self._deadline_unix_ms - int(time.time() * 1000)) / 1000.0
        return min(_MAX_DEADLINE_SECONDS, remaining)

    def _prompt_message(
        self,
        public_observation: Mapping[str, object],
        inference_request: Mapping[str, object] | None,
        deterministic_plan: Mapping[str, object],
        *,
        handoff: bool = False,
    ) -> str:
        # Values are already public projections from application.py.  Explicit
        # labels make page/job text untrusted model input rather than policy.
        payload = {
            "instruction": (
                "Propose at most one guarded browser action. Never submit an application."
                if not handoff
                else "Propose only browser.prepare_human_handoff after reviewing this draft."
            ),
            "untrusted_page_observation": dict(public_observation),
            "untrusted_inference_context": dict(inference_request) if inference_request is not None else None,
            "deterministic_availability": dict(deterministic_plan),
            "handoff_only": handoff,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def _context(self) -> HostToolContext:
        if self._run_id is None or self._parent_request_id is None or self._deadline_unix_ms <= 0:
            raise ApplicationRpcServiceError("internal_error")
        return HostToolContext(
            protocol_version=APPLICATION_RPC_PROTOCOL_VERSION,
            run_id=self._run_id,
            request_id=self._parent_request_id,
            deadline_unix_ms=self._deadline_unix_ms,
        )

    async def propose_action(
        self,
        run_id: int,
        iteration: int,
        observation_sha256: str,
        public_observation: dict[str, Any],
        inference_request: dict[str, Any] | None,
        deterministic_plan: dict[str, Any],
    ) -> BrowserToolProposal | None:
        if self._run_id != run_id or self._cancel_event.is_set() or self._handoff_committed or self._post_commit_guard:
            return None
        self._public_observation = MappingProxyType(dict(public_observation))
        self._last_observation_sha256 = observation_sha256
        return await self._run_prompt(
            self._prompt_message(public_observation, inference_request, deterministic_plan),
            mode="action",
            workflow_sequence=iteration,
        )
    async def _cancel_native_prompt(self) -> None:
        process = self._process
        cancel = getattr(process, "cancel_prompt", None) if process is not None else None
        if not callable(cancel):
            return
        try:
            value = cancel()
            if inspect.isawaitable(value):
                timeout = max(0.05, min(1.0, max(0.0, self._remaining_seconds())))
                await asyncio.wait_for(value, timeout=timeout)
        except Exception:
            return

    async def _drain_prompt_task(self) -> bool:
        """Drain the exact prior native prompt before opening another."""
        task = self._prompt_task
        if task is None:
            return not self._prompt_failed
        if task.done():
            try:
                task.result()
            except asyncio.CancelledError:
                self._prompt_failed = True
            except Exception:
                self._prompt_failed = True
            finally:
                if self._prompt_task is task:
                    self._prompt_task = None
            return not self._prompt_failed
        remaining = self._remaining_seconds()
        if remaining <= 0:
            await self._cancel_native_prompt()
            self._prompt_failed = True
            return False
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except asyncio.TimeoutError:
            await self._cancel_native_prompt()
            self._prompt_failed = True
            return False
        except asyncio.CancelledError:
            self._prompt_failed = True
            raise
        except Exception:
            self._prompt_failed = True
            return False
        finally:
            if task.done() and self._prompt_task is task:
                self._prompt_task = None
        return not self._prompt_failed


    async def authorize_handoff(
        self,
        run_id: int,
        iteration: int,
        observation_sha256: str,
        public_observation: dict[str, Any],
    ) -> BrowserToolProposal | None:
        if self._run_id != run_id or self._cancel_event.is_set() or self._handoff_committed or self._post_commit_guard:
            return None
        self._public_observation = MappingProxyType(dict(public_observation))
        return await self._run_prompt(
            self._prompt_message(public_observation, None, {"status": "draft_ready"}, handoff=True),
            mode="handoff",
            workflow_sequence=iteration,
        )

    async def _run_prompt(self, message: str, *, mode: str, workflow_sequence: int | None = None) -> BrowserToolProposal | None:
        if self._process is None or self._closed or self._prompt_failed:
            return None
        async with self._prompt_lock:
            while True:
                if self._cancel_event.is_set() or self._closed or self._prompt_failed:
                    return None
                if self._prompt_task is not None and not await self._drain_prompt_task():
                    return None
                remaining = self._remaining_seconds()
                if remaining <= 0:
                    await self._deadline_stop()
                    return None
                self._prompt_mode = mode
                self._prompt_observed = False
                self._prompt_action = False
                self._prompt_workflow_sequence = workflow_sequence
                loop = asyncio.get_running_loop()
                proposal_future: asyncio.Future[object] = loop.create_future()
                self._proposal_surface_future = proposal_future
                context = self._context()
                process = self._process
                prompt_call = process.prompt(message, context, timeout=remaining)
                prompt_task = asyncio.create_task(prompt_call, name="application-rpc-omp-prompt")
                self._prompt_task = prompt_task
                surfaced = False
                park = False
                try:
                    done, _ = await asyncio.wait(
                        (proposal_future, prompt_task),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if proposal_future in done:
                        value = proposal_future.result()
                        if isinstance(value, BrowserToolProposal):
                            surfaced = True
                            return value
                        try:
                            await asyncio.shield(prompt_task)
                        except Exception:
                            self._prompt_failed = True
                        return None
                    try:
                        await asyncio.shield(prompt_task)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self._prompt_failed = True
                        return None
                    if proposal_future.done() and isinstance(proposal_future.result(), BrowserToolProposal):
                        return proposal_future.result()  # type: ignore[return-value]
                    if self._cancel_event.is_set():
                        return None
                    park = True
                finally:
                    if self._prompt_task is prompt_task and (not surfaced or prompt_task.done()):
                        self._prompt_task = None
                    if self._proposal_surface_future is proposal_future:
                        self._proposal_surface_future = None
                if park:
                    await self._park_for_resume()
                    if self._cancel_event.is_set():
                        return None

    async def _park_for_resume(self) -> None:
        if self._run_id is None or self._handoff_committed or self._post_commit_guard:
            return
        self._awaiting_resume = True
        self._coordinator_state = "awaiting_resume"
        event: _db.RpcEventInfo | None = None
        async with self._db_lock:
            connection = self._connection()
            try:
                self._refresh_sequences_locked(connection)
                if not self._handoff_committed and not self._post_commit_guard:
                    status = _db.get_rpc_run_status(connection, self._run_id)
                    if status is None:
                        raise ApplicationRpcServiceError("internal_error")
                    if status.state == "running":
                        sequence = self._next_sequence_locked()
                        event = _db.commit_rpc_run_transition(
                            connection,
                            _db.RpcRunTransition(
                                run_id=self._run_id,
                                coordinator_id=self._coordinator_id,
                                request_id=self._event_request_id(),
                                action_sequence=sequence,
                                event_type="awaiting_resume",
                                summary_code="awaiting_resume",
                                state="manual",
                                ats_policy=self._ats_policy,
                            ),
                        )
                    else:
                        _db.append_rpc_event(
                            connection,
                            run_id=self._run_id,
                            event_type="awaiting_resume",
                            summary_code="awaiting_resume",
                            request_id=self._event_request_id(),
                            action_sequence=status.action_sequence,
                        )
                        event = _db.latest_rpc_event(connection, self._run_id)
                    if event is not None:
                        self._event_sequence = event.sequence
            finally:
                connection.close()
        await self._emit(event)
        await self._resume_event.wait()
        self._resume_event.clear()
        self._awaiting_resume = False
        if not self._cancel_event.is_set():
            self._coordinator_state = "prompting"

    async def _deadline_stop(self) -> None:
        self._cancel_event.set()
        self._coordinator_state = "cancelling"
        if self._pending is not None and not self._pending.dispatched:
            await self._finish_pending_error("deadline_exceeded")

    async def before_action_dispatch(self, proposal: BrowserToolProposal, action_sequence: int) -> bool:
        # Deliberately no await: cancellation/deadline and invocation CAS are
        pending = self._pending
        # checked in one event-loop turn immediately before browser mutation.
        if (
            pending is None
            or pending.proposal.request.request_id != proposal.request.request_id
            or type(action_sequence) is not int
            or action_sequence != pending.workflow_sequence
        ):
            return False
        if self._handoff_committed or self._post_commit_guard or self._cancel_event.is_set() or self._remaining_seconds() <= 0:
            return False
        if self._run_id is None:
            return False
        try:
            connection = self._connection()
            try:
                if _db.read_rpc_cancellation(connection, self._run_id):
                    self._cancel_event.set()
                    return False
            finally:
                connection.close()
        except Exception:
            return False
        if self._remaining_seconds() <= 0:
            return False
        invocation = pending.invocation
        mark = getattr(invocation, "mark_dispatched", None)
        if not callable(mark):
            return False
        allowed = bool(mark())
        if allowed:
            pending.dispatched = True
        return allowed

    async def _finish_pending_error(self, error_code: str) -> Mapping[str, object] | None:
        if self._post_commit_guard or self._handoff_intent is not None:
            return None
        pending = self._pending
        if pending is None:
            return None
        if pending.dispatched:
            return None
        response = await self._commit_pending(
            pending,
            ok=False,
            state="manual",
            result=None,
            error_code=error_code,
            event_type="action_rejected",
            summary_code="rejected",
        )
        if not pending.future.done():
            pending.future.set_result(None)
        return response

    @staticmethod
    def _validated_handoff_result(
        proposal: BrowserToolProposal,
        result: object,
        *,
        state: str,
        observation_sha256: object,
        reason_code: object,
    ) -> Mapping[str, Any] | None:
        try:
            validated = validate_public_result(
                result,
                request=proposal.request,
                envelope_state=state,
            )
        except Exception:
            return None
        if not isinstance(validated, Mapping):
            return None
        if (
            set(validated)
            != {
                "outcome",
                "reason_code",
                "observation_sha256",
                "unresolved_required_count",
                "automated_submission",
            }
            or validated.get("outcome") != "committed"
            or validated.get("reason_code") != reason_code
            or validated.get("observation_sha256") != observation_sha256
            or type(validated.get("unresolved_required_count")) is not int
            or validated["unresolved_required_count"] < 0
            or validated.get("automated_submission") is not False
        ):
            return None
        return dict(validated)

    async def prepare_handoff_finalization(
        self,
        proposal: BrowserToolProposal,
        *,
        action_sequence: int,
        intent: Mapping[str, Any],
    ) -> bool:
        pending = self._pending
        if (
            self._run_id is None
            or self._post_commit_guard
            or pending is None
            or pending.proposal.request.request_id != proposal.request.request_id
            or type(action_sequence) is not int
            or action_sequence != pending.workflow_sequence
            or proposal.request.run_id != self._run_id
            or proposal.request.operation != "browser.prepare_human_handoff"
        ):
            return False
        bound: Mapping[str, Any] | None = None
        last_error: BaseException | None = None
        prior_browser_state = self._browser_state
        if self._remaining_seconds() <= 0:
            await self._deadline_stop()
            raise RuntimeError("deadline_exceeded")
        if self._cancel_event.is_set():
            self._coordinator_state = "cancelling"
            raise RuntimeError("abandoned_running_attempt")
        for attempt in range(2):
            try:
                async with self._db_lock:
                    connection = self._connection()
                    try:
                        bound = _db.bind_rpc_handoff_intent(
                            connection,
                            request=proposal.request,
                            coordinator_id=self._coordinator_id,
                            intent=intent,
                        )
                    finally:
                        connection.close()
                break
            except Exception as exc:
                cancelled = self._cancel_event.is_set()
                if not cancelled and self._run_id is not None:
                    cancellation_connection = self._connection()
                    try:
                        cancelled = bool(
                            _db.read_rpc_cancellation(
                                cancellation_connection,
                                self._run_id,
                            )
                        )
                    except Exception:
                        cancelled = False
                    finally:
                        cancellation_connection.close()
                if cancelled:
                    self._cancel_event.set()
                    self._coordinator_state = "cancelling"
                    self._browser_state = prior_browser_state
                    raise RuntimeError("abandoned_running_attempt") from exc
                last_error = exc
                self._post_commit_guard = True
                self._browser_state = "unknown"
            if attempt == 0:
                try:
                    await asyncio.sleep(0)
                except asyncio.CancelledError:
                    error = ApplicationRpcDurabilityError(
                        "Handoff intent acknowledgement was cancelled"
                    )
                    self._fail_child_flight(
                        proposal.request.request_id,
                        error,
                    )
                    raise
        if bound is None:
            self._post_commit_guard = True
            self._browser_state = "unknown"
            error = ApplicationRpcDurabilityError(
                "Handoff intent outcome is indeterminate"
            )
            self._fail_child_flight(proposal.request.request_id, error)
            raise error from last_error
        finalization = bound.get("application_finalization")
        next_state = (
            finalization.get("status")
            if isinstance(finalization, Mapping)
            else None
        )
        observation_sha256 = bound.get("observation_sha256")
        cached_result = self._validated_handoff_result(
            proposal,
            bound.get("proposal_result"),
            state=str(next_state),
            observation_sha256=observation_sha256,
            reason_code=(
                finalization.get("reason_code")
                if isinstance(finalization, Mapping)
                else None
            ),
        )
        if (
            cached_result is None
            or not isinstance(finalization, Mapping)
            or type(next_state) is not str
            or next_state not in {"review_ready", "manual", "blocked"}
            or type(observation_sha256) is not str
            or not _SHA256_RE.fullmatch(observation_sha256)
        ):
            self._post_commit_guard = True
            self._browser_state = "unknown"
            error = ApplicationRpcDurabilityError(
                "Persisted handoff intent is invalid"
            )
            self._fail_child_flight(proposal.request.request_id, error)
            raise error
        self._post_commit_guard = False
        self._browser_state = prior_browser_state
        self._handoff_intent = MappingProxyType(dict(bound))
        self._pending_handoff_result = MappingProxyType(dict(cached_result))
        self._pending_handoff_finalization = MappingProxyType(
            dict(finalization)
        )
        self._pending_handoff_state = next_state
        return True

    async def proposal_finished(
        self,
        proposal: BrowserToolProposal,
        action_sequence: int,
        ok: bool,
        state: str,
        result: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        application_finalization: Mapping[str, Any] | None = None,
    ) -> bool:
        pending = self._pending
        if pending is None or pending.proposal.request.request_id != proposal.request.request_id:
            if proposal.request.request_id in self._finished:
                return False
            return False
        if type(action_sequence) is not int or action_sequence != pending.workflow_sequence:
            return False
        if self._post_commit_guard and proposal.request.operation == "browser.prepare_human_handoff" and not ok:
            error = ApplicationRpcDurabilityError(
                "Handoff outcome requires restart recovery"
            )
            self._fail_child_flight(proposal.request.request_id, error)
            raise error
        if pending.dispatched is False and self._cancel_event.is_set() and not ok:
            error_code = error_code or "cancelled"
        if not ok and state == "failed":
            finalization = (
                dict(application_finalization)
                if isinstance(application_finalization, Mapping)
                else {
                    "status": "failed",
                    "reason_code": "abandoned_running_attempt",
                    "observation_summary": {},
                    "plan_summary": {},
                    "artifact_dir": None,
                }
            )
            if finalization.get("status") != "failed":
                original_status = finalization.get("status")
                original_reason = finalization.get("reason_code")
                evidence = (
                    dict(finalization.get("observation_summary", {}))
                    if isinstance(finalization.get("observation_summary"), Mapping)
                    else {}
                )
                evidence["terminal_status"] = original_status
                evidence["terminal_reason_code"] = original_reason
                finalization["status"] = "failed"
                finalization["reason_code"] = "abandoned_running_attempt"
                finalization["observation_summary"] = evidence
            await self.finalize_failure(
                self._run_id or 0,
                status=str(finalization.get("status", "failed")),
                reason_code=str(
                    finalization.get(
                        "reason_code",
                        "abandoned_running_attempt",
                    )
                ),
                observation_summary=(
                    finalization.get("observation_summary", {})
                    if isinstance(finalization.get("observation_summary", {}), Mapping)
                    else {}
                ),
                plan_summary=(
                    finalization.get("plan_summary", {})
                    if isinstance(finalization.get("plan_summary", {}), Mapping)
                    else {}
                ),
                artifact_dir=(
                    finalization.get("artifact_dir")
                    if isinstance(finalization.get("artifact_dir"), str)
                    else None
                ),
                pending_proposal=proposal,
                action_sequence=action_sequence,
                error_code=error_code or "workflow_failed",
                observation_sha256=self._last_observation_sha256,
            )
            return True
        if proposal.request.operation == "browser.prepare_human_handoff" and ok:
            cached_result = self._pending_handoff_result
            cached_finalization = self._pending_handoff_finalization
            if cached_result is None or cached_finalization is None or self._pending_handoff_state is None:
                return False
            try:
                if (
                    application_finalization is not None
                    and dict(application_finalization) != dict(cached_finalization)
                ):
                    return False
                if result is not None and dict(result) != dict(cached_result):
                    return False
            except Exception:
                return False
            response_result = dict(cached_result)
            finalization = dict(cached_finalization)
            next_state = self._pending_handoff_state
            manifest_sha = (
                self._handoff_intent.get("artifact_manifest_sha256")
                if self._handoff_intent is not None
                else None
            )
            if type(manifest_sha) is not str or not _SHA256_RE.fullmatch(manifest_sha):
                return False
            await self._commit_pending(
                pending,
                ok=True,
                state=next_state,
                result=response_result,
                error_code=None,
                event_type="browser_handed_off",
                summary_code="handed_off",
                manifest_sha256=manifest_sha,
                human_review_ready=next_state == "review_ready",
                handoff_committed=True,
                application_finalization=finalization,
            )
            self._pending_handoff_result = None
            self._pending_handoff_finalization = None
            self._pending_handoff_state = None
            self._handoff_committed = True
            self._browser_state = "handed_off"
            self._coordinator_state = "terminal"
            return True
        event_type = "action_allowed" if ok else "action_rejected"
        summary_code = "allowed" if ok else "rejected"
        next_state = state if state in {"running", "manual", "blocked", "failed"} else "manual"
        if not ok:
            next_state = "manual" if next_state not in {"failed", "blocked"} else next_state
        await self._commit_pending(
            pending,
            ok=bool(ok),
            state=next_state,
            result=dict(result) if result is not None else None,
            error_code=error_code if not ok else None,
            event_type=event_type,
            summary_code=summary_code,
        )
        return False

    def _manifest_sha256(self, run_id: int | None) -> str | None:
        if run_id is None or self._artifact_root is None:
            return None
        root: ArtifactRoot | None = None
        run: Any | None = None
        try:
            root = ArtifactRoot.open_existing(self._artifact_root, cwd=Path.cwd())
            run = root.open_run_dir(run_id)
            payload = run.read_bytes("run.json", max_bytes=8 * 1024 * 1024)
            return hashlib.sha256(payload).hexdigest()
        except Exception:
            return None
        finally:
            try:
                if run is not None:
                    run.close()
            finally:
                if root is not None:
                    root.close()

    async def _commit_pending(
        self,
        pending: _PendingProposal,
        *,
        ok: bool,
        state: str,
        result: Mapping[str, Any] | None,
        error_code: str | None,
        event_type: str,
        summary_code: str,
        manifest_sha256: str | None = None,
        human_review_ready: bool = False,
        handoff_committed: bool = False,
        application_finalization: Mapping[str, Any] | None = None,
    ) -> Mapping[str, object]:
        last_error: BaseException | None = None
        parsed: Mapping[str, object] | None = None
        event: _db.RpcEventInfo | None = None
        commit_sequence = 0
        for attempt in range(2):
            try:
                async with self._db_lock:
                    connection = self._connection()
                    try:
                        existing = _db.get_rpc_request(
                            connection,
                            pending.proposal.request.request_id,
                        )
                        if (
                            existing is not None
                            and existing.state == "completed"
                            and existing.response_json is not None
                        ):
                            if (
                                existing.semantic_sha256
                                != pending.proposal.request.semantic_sha256
                            ):
                                raise ApplicationRpcDurabilityError(
                                    "Completed proposal semantic binding mismatch"
                                )
                            parsed = parse_application_response(
                                existing.response_json,
                                request=pending.proposal.request,
                            )
                            commit_sequence = int(
                                parsed["action_sequence"]
                            )
                        else:
                            status = _db.get_rpc_run_status(
                                connection,
                                pending.proposal.request.run_id or 0,
                            )
                            current_action = (
                                status.action_sequence
                                if status is not None
                                else self._action_sequence
                            )
                            commit_sequence = (
                                max(self._action_sequence, current_action) + 1
                            )
                            event_sequence = (
                                status.latest_event_sequence
                                if status is not None
                                else self._event_sequence
                            ) + 1
                            response = build_application_response(
                                pending.proposal.request,
                                ok=ok,
                                state=state,
                                action_sequence=commit_sequence,
                                event_sequence=event_sequence,
                                result=result,
                                error=error_code,
                            )
                            completed = _db.commit_rpc_proposal_result(
                                connection,
                                request=pending.proposal.request,
                                response=response,
                                coordinator_id=self._coordinator_id,
                                action_sequence=commit_sequence,
                                event_type=event_type,
                                summary_code=summary_code,
                                observation_sha256=self._last_observation_sha256,
                                manifest_sha256=manifest_sha256,
                                run_state=state,
                                ats_policy=self._ats_policy,
                                human_review_ready=human_review_ready,
                                handoff_committed=handoff_committed,
                                application_finalization=application_finalization,
                                parent_request_id=pending.proposal.parent_request_id,
                            )
                            parsed = parse_application_response(
                                completed.response_json,
                                request=pending.proposal.request,
                            )
                        event = _db.latest_rpc_event(
                            connection,
                            pending.proposal.request.run_id,
                        )
                    finally:
                        connection.close()
                break
            except Exception as exc:
                last_error = exc
            if attempt == 0:
                try:
                    await asyncio.sleep(0)
                except asyncio.CancelledError:
                    error = ApplicationRpcDurabilityError(
                        "Proposal completion was cancelled before durable acknowledgement"
                    )
                    self._fail_child_flight(
                        pending.proposal.request.request_id,
                        error,
                    )
                    raise
        if parsed is None:
            error = ApplicationRpcDurabilityError(
                "Proposal response could not be persisted"
            )
            self._fail_child_flight(
                pending.proposal.request.request_id,
                error,
            )
            raise error from last_error
        self._action_sequence = max(
            self._action_sequence,
            commit_sequence,
        )
        self._finished[pending.proposal.request.request_id] = parsed
        self._pending = None
        if not pending.future.done():
            pending.future.set_result(parsed)
        await self._emit(event)
        return parsed

    async def finalize_failure(
        self,
        run_id: int,
        *,
        status: str,
        reason_code: str,
        observation_summary: Mapping[str, Any],
        plan_summary: Mapping[str, Any],
        artifact_dir: str | None,
        pending_proposal: BrowserToolProposal | None = None,
        action_sequence: int = 0,
        error_code: str | None = None,
        observation_sha256: str | None = None,
        manifest_sha256: str | None = None,
    ) -> bool:
        """Atomically finalize application and RPC failure state."""
        if self._run_id != run_id or self._post_commit_guard or self._handoff_committed:
            return False
        if status != "failed":
            return False
        if not isinstance(reason_code, str) or not reason_code:
            return False
        if pending_proposal is not None:
            pending = self._pending
            if (
                pending is None
                or pending.proposal.request.request_id != pending_proposal.request.request_id
            ):
                return False
        elif self._pending is not None:
            pending = self._pending
            pending_proposal = pending.proposal
        else:
            pending = None
        durable_observation = observation_sha256 or self._last_observation_sha256
        durable_manifest = manifest_sha256 or self._manifest_sha256(run_id)
        durable_error = error_code or (
            "cancelled"
            if reason_code == "abandoned_running_attempt"
            else "workflow_failed"
        )
        finalization = {
            "status": status,
            "reason_code": reason_code,
            "observation_summary": dict(observation_summary),
            "plan_summary": dict(plan_summary),
            "artifact_dir": artifact_dir,
        }
        last_error: BaseException | None = None
        parsed: Mapping[str, object] | None = None
        event: _db.RpcEventInfo | None = None
        commit_sequence = 0
        for attempt in range(2):
            try:
                async with self._db_lock:
                    connection = self._connection()
                    try:
                        durable_status = _db.get_rpc_run_status(connection, run_id)
                        if durable_status is None:
                            raise ApplicationRpcDurabilityError(
                                "RPC failure run is missing"
                            )
                        commit_sequence = (
                            max(self._action_sequence, durable_status.action_sequence) + 1
                        )
                        if pending is not None and pending_proposal is not None:
                            event_sequence = durable_status.latest_event_sequence + 1
                            response = build_application_response(
                                pending_proposal.request,
                                ok=False,
                                state="failed",
                                action_sequence=commit_sequence,
                                event_sequence=event_sequence,
                                error=durable_error,
                            )
                            completed = _db.commit_rpc_proposal_failure(
                                connection,
                                request=pending_proposal.request,
                                response=response,
                                coordinator_id=self._coordinator_id,
                                action_sequence=commit_sequence,
                                application_finalization=finalization,
                                observation_sha256=durable_observation,
                                manifest_sha256=durable_manifest,
                                ats_policy=self._ats_policy,
                                parent_request_id=pending_proposal.parent_request_id,
                            )
                            if completed.response_json is None:
                                raise ApplicationRpcDurabilityError(
                                    "Completed failure proposal response is missing"
                                )
                            parsed = parse_application_response(
                                completed.response_json,
                                request=pending_proposal.request,
                            )
                        else:
                            request_id = self._event_request_id()
                            event = _db.commit_rpc_failure(
                                connection,
                                run_id=run_id,
                                coordinator_id=self._coordinator_id,
                                request_id=request_id,
                                action_sequence=commit_sequence,
                                application_finalization=finalization,
                                observation_sha256=durable_observation,
                                manifest_sha256=durable_manifest,
                                ats_policy=self._ats_policy,
                            )
                        if event is None:
                            event = _db.latest_rpc_event(connection, run_id)
                    finally:
                        connection.close()
                break
            except Exception as exc:
                last_error = exc
            if attempt == 0:
                try:
                    await asyncio.sleep(0)
                except asyncio.CancelledError:
                    error = ApplicationRpcDurabilityError(
                        "Failure finalization was cancelled before durable acknowledgement"
                    )
                    if pending_proposal is not None:
                        self._fail_child_flight(
                            pending_proposal.request.request_id,
                            error,
                        )
                    raise
        if event is None:
            error = ApplicationRpcDurabilityError(
                "Application failure could not be persisted"
            )
            if pending_proposal is not None:
                self._fail_child_flight(pending_proposal.request.request_id, error)
            raise error from last_error
        self._action_sequence = max(self._action_sequence, commit_sequence)
        self._event_sequence = event.sequence
        if parsed is not None and pending_proposal is not None:
            self._finished[pending_proposal.request.request_id] = parsed
            self._pending = None
            if pending is not None and not pending.future.done():
                pending.future.set_result(parsed)
        self._browser_state = "failed"
        self._coordinator_state = "terminal"
        await self._emit(event)
        return True

    def _fail_child_flight(
        self,
        request_id: str,
        error: BaseException,
    ) -> None:
        flight = self._child_flights.get(request_id)
        if flight is not None and not flight.future.done():
            flight.future.set_exception(error)
            flight.future.exception()

    async def _complete_child_request(
        self,
        proposal: BrowserToolProposal,
        response: Mapping[str, object],
    ) -> Mapping[str, object]:
        last_error: BaseException | None = None
        for attempt in range(2):
            try:
                async with self._db_lock:
                    connection = self._connection()
                    try:
                        completed = _db.complete_rpc_request(
                            connection,
                            request=proposal.request,
                            response=response,
                            parent_request_id=proposal.parent_request_id,
                            coordinator_id=self._coordinator_id,
                        )
                    finally:
                        connection.close()
                if completed.response_json is None:
                    raise ApplicationRpcDurabilityError(
                        "Completed child RPC response is missing"
                    )
                parsed = parse_application_response(
                    completed.response_json,
                    request=proposal.request,
                )
                flight = self._child_flights.get(proposal.request.request_id)
                if flight is not None and not flight.future.done():
                    flight.future.set_result(parsed)
                return parsed
            except BaseException as exc:
                last_error = exc
            if attempt == 0:
                await asyncio.sleep(0)
        error = ApplicationRpcDurabilityError(
            "Child RPC response could not be persisted"
        )
        self._fail_child_flight(proposal.request.request_id, error)
        raise error from last_error

    async def handle_host_tool(self, invocation: OmpHostInvocation | Any) -> Mapping[str, object] | None:
        proposal = getattr(invocation, "proposal", None)
        if not isinstance(proposal, BrowserToolProposal):
            return None
        if self._run_id != proposal.request.run_id or self._handoff_committed or self._post_commit_guard:
            raise OmpHostDurabilityError(
                "Host callback cannot persist against an inactive run"
            )
        request_id = proposal.request.request_id
        existing_flight = self._child_flights.get(request_id)
        if existing_flight is not None:
            if existing_flight.semantic_sha256 != proposal.request.semantic_sha256:
                raise OmpHostDurabilityError(
                    "Conflicting host callback has no durable response"
                )
            try:
                value = await asyncio.shield(existing_flight.future)
            except ApplicationRpcDurabilityError as exc:
                raise OmpHostDurabilityError(
                    "Host callback has no durable response"
                ) from exc
            if isinstance(value, Mapping):
                return dict(value)
            raise OmpHostDurabilityError(
                "Host callback has no durable response"
            )
        loop = asyncio.get_running_loop()
        child_future: asyncio.Future[object] = loop.create_future()
        child_flight = _ChildFlight(proposal.request.semantic_sha256, child_future)
        self._child_flights[request_id] = child_flight
        self._invocations[request_id] = invocation
        reserved: _db.RpcRequestInfo | None = None
        transport_rejection_code = getattr(
            invocation,
            "transport_rejection_code",
            None,
        )
        try:
            async with self._db_lock:
                connection = self._connection()
                try:
                    reserved = _db.reserve_rpc_request(
                        connection,
                        request=proposal.request,
                        parent_request_id=proposal.parent_request_id,
                        run_id=self._run_id,
                    )
                finally:
                    connection.close()
            if not reserved.created:
                if reserved.state == "completed" and reserved.response_json is not None:
                    parsed = parse_application_response(
                        reserved.response_json,
                        request=proposal.request,
                    )
                    if not child_future.done():
                        child_future.set_result(parsed)
                    return parsed
                pending = self._pending
                if pending is not None and pending.proposal.request.request_id == request_id:
                    value = await asyncio.shield(pending.future)
                    if isinstance(value, Mapping):
                        return dict(value)
                error = ApplicationRpcDurabilityError(
                    "Child RPC request is pending without a durable response"
                )
                self._fail_child_flight(request_id, error)
                raise error

            if transport_rejection_code is not None:
                response = self._error_response(
                    proposal.request,
                    error=(
                        "cancelled"
                        if transport_rejection_code
                        == "prompt_cancelled"
                        else "action_rejected"
                    ),
                    run_id=self._run_id,
                    state="manual",
                )
                return await self._complete_child_request(
                    proposal,
                    response,
                )
            if proposal.tool_name == "browser.observe":
                accepted_observation = False
                if self._prompt_observed:
                    response = self._error_response(
                        proposal.request,
                        error="action_rejected",
                        run_id=self._run_id,
                    )
                elif self._public_observation is None:
                    response = self._error_response(
                        proposal.request,
                        error="unavailable",
                        run_id=self._run_id,
                    )
                else:
                    connection = self._connection()
                    try:
                        status = _db.get_rpc_run_status(connection, self._run_id)
                        action_sequence = status.action_sequence if status is not None else self._action_sequence
                        event_sequence = status.latest_event_sequence if status is not None else self._event_sequence
                    finally:
                        connection.close()
                    result = validate_public_result(
                        self._public_observation,
                        request=proposal.request,
                        operation="browser.observe",
                    )
                    response = build_application_response(
                        proposal.request,
                        ok=True,
                        state="running",
                        action_sequence=action_sequence,
                        event_sequence=event_sequence,
                        result=result,
                    )
                    accepted_observation = True
                completed_response = await self._complete_child_request(proposal, response)
                if accepted_observation:
                    self._prompt_observed = True
                return completed_response

            if not self._prompt_observed or self._prompt_action:
                response = self._error_response(
                    proposal.request,
                    error="action_rejected",
                    run_id=self._run_id,
                )
                return await self._complete_child_request(proposal, response)
            if self._prompt_mode == "handoff":
                allowed = proposal.tool_name == "browser.prepare_human_handoff"
            else:
                allowed = proposal.tool_name != "browser.prepare_human_handoff"
            if not allowed:
                response = self._error_response(
                    proposal.request,
                    error="action_rejected",
                    run_id=self._run_id,
                )
                return await self._complete_child_request(proposal, response)
            self._prompt_action = True
            future = child_future
            rejected: Mapping[str, object] | None = None
            async with self._db_lock:
                connection = self._connection()
                try:
                    self._refresh_sequences_locked(connection)
                    status = _db.get_rpc_run_status(connection, self._run_id)
                    if status is None or status.state not in {"starting", "running"}:
                        rejected = self._error_response(
                            proposal.request,
                            error="action_rejected",
                            run_id=self._run_id,
                            state="manual",
                            action_sequence=max(self._action_sequence, status.action_sequence if status is not None else 0),
                            event_sequence=status.latest_event_sequence if status is not None else self._event_sequence,
                        )
                finally:
                    connection.close()
            if rejected is not None:
                return await self._complete_child_request(proposal, rejected)
            workflow_sequence = self._prompt_workflow_sequence
            if type(workflow_sequence) is not int:
                response = self._error_response(
                    proposal.request,
                    error="internal_error",
                    run_id=self._run_id,
                )
                return await self._complete_child_request(proposal, response)
            self._workflow_sequence += 1
            self._pending = _PendingProposal(
                proposal,
                self._workflow_sequence,
                future,
                invocation,
                self._prompt_mode,
            )
            if self._proposal_surface_future is not None and not self._proposal_surface_future.done():
                self._proposal_surface_future.set_result(proposal)
            remaining = self._remaining_seconds()
            if remaining <= 0:
                await self._finish_pending_error("deadline_exceeded")
                return await asyncio.shield(future)
            value = await asyncio.shield(future)
            if isinstance(value, Mapping):
                return dict(value)
            return build_rejected_application_response(
                proposal.request.to_mapping(),
                error="cancelled",
            )
        except asyncio.CancelledError:
            pending = self._pending
            if pending is not None and pending.proposal.request.request_id == request_id:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                try:
                    await asyncio.shield(pending.future)
                except BaseException:
                    pass
                raise
            if reserved is not None and reserved.created:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                try:
                    response = self._error_response(
                        proposal.request,
                        error="cancelled",
                        run_id=self._run_id,
                        state="manual",
                    )
                    completion = asyncio.create_task(
                        self._complete_child_request(proposal, response)
                    )
                    await asyncio.shield(completion)
                except BaseException:
                    self._fail_child_flight(
                        request_id,
                        ApplicationRpcDurabilityError(
                            "Cancelled child RPC response was not persisted"
                        ),
                    )
            if reserved is None or not reserved.created:
                self._fail_child_flight(
                    request_id,
                    ApplicationRpcDurabilityError(
                        "Cancelled child RPC reservation has no durable response"
                    ),
                )
            raise
        except ApplicationRpcDurabilityError as exc:
            self._fail_child_flight(request_id, exc)
            raise OmpHostDurabilityError(
                "Host callback has no durable response"
            ) from exc
        except Exception as exc:
            if reserved is None and "conflicting request binding" in str(exc):
                error = ApplicationRpcDurabilityError(
                    "Conflicting child RPC request has no durable response"
                )
                self._fail_child_flight(request_id, error)
                raise OmpHostDurabilityError(
                    "Host callback has no durable response"
                ) from exc
            if reserved is not None and reserved.created:
                response = self._error_response(
                    proposal.request,
                    error="internal_error",
                    run_id=self._run_id,
                )
                try:
                    return await self._complete_child_request(
                        proposal,
                        response,
                    )
                except ApplicationRpcDurabilityError as durability_error:
                    self._fail_child_flight(
                        request_id,
                        durability_error,
                    )
                    raise OmpHostDurabilityError(
                        "Host callback has no durable response"
                    ) from durability_error
            if reserved is not None:
                error = ApplicationRpcDurabilityError(
                    "Reserved child RPC request has no durable response"
                )
                self._fail_child_flight(request_id, error)
                raise OmpHostDurabilityError(
                    "Host callback has no durable response"
                ) from error
            error = ApplicationRpcDurabilityError(
                "Child RPC reservation is indeterminate"
            )
            self._fail_child_flight(request_id, error)
            raise OmpHostDurabilityError(
                "Host callback has no durable response"
            ) from exc
        finally:
            self._invocations.pop(request_id, None)
            if self._child_flights.get(request_id) is child_flight:
                self._child_flights.pop(request_id, None)

    __call__ = handle_host_tool

    async def fail(self, reason_code: str = "workflow_failed") -> None:
        """Durably fail an RPC workflow and its application row together."""
        if self._run_id is None or self._post_commit_guard:
            return
        current = await self._read_status(self._run_id)
        if current is not None and current.state == "failed":
            self._browser_state = "failed"
            self._coordinator_state = "terminal"
            return
        application_reason = (
            reason_code
            if reason_code in _db.PUBLIC_REASON_CODES
            else "browser_error"
        )
        await self.finalize_failure(
            self._run_id,
            status="failed",
            reason_code=application_reason,
            observation_summary={"error_code": reason_code},
            plan_summary={},
            artifact_dir=None,
            pending_proposal=(
                self._pending.proposal
                if self._pending is not None
                else None
            ),
            action_sequence=(
                self._pending.workflow_sequence
                if self._pending is not None
                else 0
            ),
            error_code=(
                reason_code
                if reason_code in {
                    "cancelled",
                    "workflow_failed",
                    "deadline_exceeded",
                    "internal_error",
                }
                else "workflow_failed"
            ),
        )

    async def resume(self, request: ApplicationRpcRequest) -> bool:
        if self._run_id is None or request.operation != "run.resume" or request.run_id != self._run_id:
            return False
        if (
            self._cancel_event.is_set()
            or self._handoff_committed
            or self._handoff_intent is not None
            or self._post_commit_guard
            or self._closed
            or not self._awaiting_resume
        ):
            return False
        event: _db.RpcEventInfo | None = None
        accepted = False
        async with self._db_lock:
            connection = self._connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._refresh_sequences_locked(connection)
                status = _db.get_rpc_run_status(connection, self._run_id)
                if (
                    status is None
                    or status.state not in {"manual", "blocked"}
                    or not status.resume_eligible
                    or status.cancellation_requested
                    or status.handoff_committed
                    or self._cancel_event.is_set()
                    or self._handoff_committed
                    or self._handoff_intent is not None
                    or self._post_commit_guard
                    or self._closed
                    or (
                        self._workflow_task is not None
                        and self._workflow_task.done()
                    )
                ):
                    connection.rollback()
                    return False
                sequence = self._next_sequence_locked()
                try:
                    event = _db.commit_rpc_run_transition(
                        connection,
                        _db.RpcRunTransition(
                            run_id=self._run_id,
                            coordinator_id=self._coordinator_id,
                            request_id=request.request_id,
                            action_sequence=sequence,
                            event_type="resume_requested",
                            summary_code="resume_requested",
                            state="running",
                            ats_policy=self._ats_policy,
                        ),
                        deadline_unix_ms=request.deadline_unix_ms,
                    )
                except _db.RpcDeadlineExceeded:
                    connection.rollback()
                    return False
                latest = _db.get_rpc_run_status(connection, self._run_id)
                accepted = bool(
                    latest is not None
                    and latest.state == "running"
                    and not latest.cancellation_requested
                    and not latest.handoff_committed
                )
                self._event_sequence = event.sequence
                if accepted:
                    self._parent_request_id = request.request_id
                    self._deadline_unix_ms = request.deadline_unix_ms
                elif latest is not None and latest.cancellation_requested:
                    self._cancel_event.set()
            finally:
                connection.close()
        await self._emit(event)
        if not accepted:
            return False
        self._coordinator_state = "prompting"
        self._resume_event.set()
        return True

    async def cancel(self, deadline_unix_ms: int | None = None) -> bool:
        if self._run_id is None:
            return False
        async with self._db_lock:
            if self._post_commit_guard or self._handoff_intent is not None:
                return False
            connection = self._connection()
            try:
                requested = _db.request_rpc_cancellation(
                    connection,
                    run_id=self._run_id,
                    coordinator_id=self._coordinator_id,
                    deadline_unix_ms=deadline_unix_ms,
                )
            except _db.RpcDeadlineExceeded:
                requested = False
            finally:
                connection.close()
            if not requested:
                return False
            self._cancel_event.set()
            self._coordinator_state = "cancelling"
        if self._pending is not None and not self._pending.dispatched:
            try:
                await self._finish_pending_error("cancelled")
            except Exception:
                pass
        await self._cancel_native_prompt()
        if self._prompt_task is not None:
            try:
                await asyncio.wait_for(
                    self._drain_prompt_task(),
                    timeout=max(0.05, min(5.0, max(0.0, self._remaining_seconds()))),
                )
            except asyncio.TimeoutError:
                self._prompt_failed = True
            except asyncio.CancelledError:
                self._prompt_failed = True
                raise
            except Exception:
                self._prompt_failed = True
        self._resume_event.set()
        return True

    async def close(self) -> None:
        self._closed = True
        if not self._post_commit_guard:
            await self.cancel()
        else:
            self._cancel_event.set()
            self._resume_event.set()
            await self._cancel_native_prompt()
            if self._prompt_task is not None:
                try:
                    await asyncio.wait_for(
                        self._drain_prompt_task(),
                        timeout=max(0.05, min(5.0, max(0.0, self._remaining_seconds()))),
                    )
                except Exception:
                    self._prompt_failed = True
            if (
                not self._handoff_committed
                and self._pending is not None
                and self._pending_handoff_result is not None
                and self._pending_handoff_finalization is not None
                and self._pending_handoff_state is not None
            ):
                try:
                    await self.proposal_finished(
                        self._pending.proposal,
                        self._pending.workflow_sequence,
                        True,
                        self._pending_handoff_state,
                        self._pending_handoff_result,
                        None,
                        self._pending_handoff_finalization,
                    )
                except Exception:
                    # Durable intent remains for startup recovery; never
                    # turn a post-commit uncertainty into failure/cancel.
                    pass


class ApplicationRpcCoordinator:
    """One service dispatching lifecycle requests for multiple isolated runs."""

    def __init__(self, config: ApplicationRpcServiceConfig) -> None:
        if not isinstance(config, ApplicationRpcServiceConfig):
            raise ApplicationRpcServiceError("invalid_config")
        self.config = config
        self._started = False
        self._closed = False
        self._identity: Mapping[str, str] | None = None
        self._runs: dict[int, _ActiveRun] = {}
        self._inflight_starts: set[asyncio.Task[Any]] = set()
        self._inflight_lifecycles: set[asyncio.Task[Any]] = set()
        self._dispatch_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._lifecycle_flights: dict[str, _LifecycleFlight] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._inflight_lifecycle_recovery: set[asyncio.Task[Any]] = set()
        self._late_launch_cleanups: set[asyncio.Task[Any]] = set()
        self._runtime_lease_key = _runtime_lease_key(
            config._db_path,
            config._artifact_root,
        )
        self._runtime_lease_held = False

    def _connection(self) -> Any:
        factory = self.config._connection_factory or _db.connect
        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError):
            return factory(self.config._db_path)
        parameters = tuple(signature.parameters.values())
        positional = tuple(
            parameter
            for parameter in parameters
            if parameter.kind
            in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        )
        if positional or any(parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
            return factory(self.config._db_path)
        return factory()
    async def start(self) -> None:
        async with self._dispatch_lock:
            if self._closed:
                raise ApplicationRpcServiceError("unavailable")
            if self._started:
                return
            if not self._runtime_lease_held:
                if _acquire_runtime_lease(
                    self.config._db_path,
                    self.config._artifact_root,
                ) is None:
                    raise ApplicationRpcServiceError("unavailable")
                self._runtime_lease_held = True
            try:
                self.config  # force type validation above
                self._identity = resolve_application_rpc_identity(self.config)
                root = ArtifactRoot.open(self.config._artifact_root, cwd=Path.cwd())
                connection = self._connection()
                try:
                    _db.initialize_database(
                        connection,
                        migration_artifact_root=root,
                        expected_coordinator_id=None,
                    )
                finally:
                    connection.close()
                    root.close()
            except ApplicationRpcServiceError:
                _release_runtime_lease(self._runtime_lease_key)
                self._runtime_lease_held = False
                raise
            except Exception:
                _release_runtime_lease(self._runtime_lease_key)
                self._runtime_lease_held = False
                raise ApplicationRpcServiceError("unavailable") from None
            self._started = True

    async def __aenter__(self) -> "ApplicationRpcCoordinator":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    def _owner(self) -> str:
        return self.config._owner or f"{self.config._coordinator_id}:{os.getpid()}"

    def _error_response(
        self,
        request: ApplicationRpcRequest,
        *,
        error: str,
        run_id: int | None = None,
        state: str = "failed",
        action_sequence: int = 0,
        event_sequence: int = 0,
    ) -> Mapping[str, object]:
        return _build_error_response(
            request,
            error=error,
            run_id=run_id,
            state=state,
            action_sequence=action_sequence,
            event_sequence=event_sequence,
        )

    def _stored_response(self, info: _db.RpcRequestInfo, request: ApplicationRpcRequest) -> Mapping[str, object] | None:
        if info.state != "completed" or info.response_json is None:
            return None
        try:
            return parse_application_response(info.response_json, request=request)
        except Exception:
            raise ApplicationRpcServiceError("internal_error") from None

    def _status_result(
        self,
        status: _db.RpcRunStatus,
        control: RpcApplicationControl | None = None,
        *,
        job_url: str | None = None,
    ) -> dict[str, object]:
        selected_url = (
            job_url
            or (control._application_url if control is not None else None)
            or getattr(status, "job_url", None)
            or status.apply_url
        )
        browser_state = control.browser_state if control is not None else ("closed" if status.state == "failed" else "not_started")
        coordinator_state = (
            "terminal"
            if status.handoff_committed
            else (
                control.coordinator_state
                if control is not None
                else ("terminal" if status.state in {"failed", "review_ready"} else "starting")
            )
        )
        if status.handoff_committed:
            browser_state = (
                "failed"
                if status.reason_code == "page_not_stable" and not status.human_review_ready
                else "handed_off"
            )
        elif status.state == "failed" and browser_state not in {"closed", "failed"}:
            browser_state = "failed"
        if browser_state in {"unknown", "open_guarded"}:
            raise ApplicationRpcServiceError("unavailable")
        reason_code = status.reason_code
        if status.state in {"starting", "running"}:
            reason_code = None
        elif reason_code is None:
            reason_code = "no_deterministic_next_step"
        human = bool(status.human_review_ready)
        if status.state == "review_ready":
            reason_code = "draft_ready"
            human = True
        return {
            "ats": status.ats_policy,
            "job_url": selected_url,
            "reason_code": reason_code,
            "current_step": status.current_form_step,
            "coordinator_state": coordinator_state,
            "browser_state": browser_state,
            "last_observation_sha256": status.last_observation_sha256,
            "artifact_manifest_sha256": status.artifact_manifest_sha256,
            "human_review_ready": human,
            "handoff_committed": bool(status.handoff_committed),
            "automated_submission": False,
        }

    def _status_response(
        self,
        request: ApplicationRpcRequest,
        status: _db.RpcRunStatus,
        control: RpcApplicationControl | None = None,
        *,
        job_url: str | None = None,
    ) -> Mapping[str, object]:
        try:
            result = self._status_result(status, control, job_url=job_url)
        except ApplicationRpcServiceError as exc:
            return self._error_response(
                request,
                error=exc.code,
                run_id=status.run_id,
                action_sequence=status.action_sequence,
                event_sequence=status.latest_event_sequence,
            )
        return build_application_response(
            request,
            ok=True,
            state=status.state,
            action_sequence=status.action_sequence,
            event_sequence=status.latest_event_sequence,
            result=result,
            run_id=status.run_id,
        )

    def _finish_lifecycle_flight_now(
        self,
        request_id: str,
        response: Mapping[str, object],
    ) -> None:
        flight = self._lifecycle_flights.pop(request_id, None)
        if flight is not None and flight.owner_task is not None:
            self._inflight_lifecycles.discard(flight.owner_task)
        if flight is not None and not flight.future.done():
            flight.future.set_result(response)

    async def _finish_lifecycle_flight(
        self,
        request_id: str,
        response: Mapping[str, object],
    ) -> None:
        async with self._lifecycle_lock:
            self._finish_lifecycle_flight_now(request_id, response)
    async def _fail_lifecycle_flight(
        self,
        request_id: str,
        error: BaseException,
    ) -> None:
        async with self._lifecycle_lock:
            flight = self._lifecycle_flights.pop(request_id, None)
            if flight is not None and flight.owner_task is not None:
                self._inflight_lifecycles.discard(flight.owner_task)
            if flight is not None and not flight.future.done():
                flight.future.set_exception(error)
                flight.future.exception()


    async def _complete_abandoned_lifecycle(
        self,
        request: ApplicationRpcRequest,
        error: str,
    ) -> None:
        response = self._error_response(request, error=error, run_id=request.run_id)
        durable_transition_reconstructed = False
        connection: Any | None = None
        info: _db.RpcRequestInfo | None = None
        lookup_error: BaseException | None = None
        try:
            connection = self._connection()
            info = _db.get_rpc_request(connection, request.request_id)
            if info is not None and info.state == "completed":
                replay = self._stored_response(info, request)
                if replay is not None:
                    await self._finish_lifecycle_flight(request.request_id, replay)
                    return
            status = (
                _db.get_rpc_run_status(connection, request.run_id)
                if request.run_id is not None
                else None
            )
            operation_committed = request.operation == "run.status"
            if status is not None and request.operation == "run.resume":
                operation_committed = any(
                    event.request_id == request.request_id
                    and event.event_type == "resume_requested"
                    for event in _db.replay_rpc_events(connection, request.run_id)
                )
            elif status is not None and request.operation == "run.cancel":
                operation_committed = _db.read_rpc_cancellation(
                    connection,
                    request.run_id,
                )
            if status is not None and operation_committed:
                durable_transition_reconstructed = True
                active = self._run_for(request.run_id or 0)
                response = self._status_response(
                    request,
                    status,
                    active.control if active is not None else None,
                    job_url=status.job_url,
                )
        except BaseException as exc:
            lookup_error = exc
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        if lookup_error is not None:
            await self._fail_lifecycle_flight(
                request.request_id,
                ApplicationRpcDurabilityError(
                    "RPC lifecycle recovery could not read durable state"
                ),
            )
            return
        if info is None:
            await self._finish_lifecycle_flight(request.request_id, response)
            return
        try:
            if durable_transition_reconstructed and self._remaining_for(request) <= 0:
                await self._complete_lifecycle(
                    request,
                    response,
                    enforce_deadline=False,
                )
            else:
                await self._complete_lifecycle(request, response)
        except Exception:
            await self._fail_lifecycle_flight(
                request.request_id,
                ApplicationRpcDurabilityError(
                    "RPC lifecycle recovery could not persist a response"
                ),
            )

    def _lifecycle_owner_done(
        self,
        request: ApplicationRpcRequest,
        owner: asyncio.Task[Any],
    ) -> None:
        self._inflight_lifecycles.discard(owner)
        flight = self._lifecycle_flights.get(request.request_id)
        if flight is None or flight.owner_task is not owner:
            return
        error = "cancelled" if owner.cancelled() else "internal_error"
        recovery = asyncio.create_task(
            self._complete_abandoned_lifecycle(request, error),
            name=f"application-rpc-lifecycle-recover-{request.request_id}",
        )
        self._inflight_lifecycle_recovery.add(recovery)
        recovery.add_done_callback(self._inflight_lifecycle_recovery.discard)

    async def _complete_lifecycle(
        self,
        request: ApplicationRpcRequest,
        response: Mapping[str, object],
        *,
        parent_request_id: str | None = None,
        enforce_deadline: bool = True,
    ) -> Mapping[str, object]:
        last_error: BaseException | None = None
        for attempt in range(2):
            connection = None
            try:
                connection = self._connection()
                info = _db.complete_rpc_request(
                    connection,
                    request=request,
                    response=response,
                    parent_request_id=parent_request_id,
                    coordinator_id=self.config._coordinator_id,
                    allow_terminal_handoff_read=True,
                    deadline_unix_ms=(
                        request.deadline_unix_ms if enforce_deadline else None
                    ),
                )
                if info.response_json is None:
                    raise ApplicationRpcDurabilityError(
                        "Completed RPC lifecycle response is missing"
                    )
                parsed = parse_application_response(
                    info.response_json,
                    request=request,
                )
                self._finish_lifecycle_flight_now(request.request_id, parsed)
                return parsed
            except _db.RpcDeadlineExceeded:
                raise
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                last_error = exc
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
            if attempt == 0:
                await asyncio.sleep(0)
        raise ApplicationRpcDurabilityError(
            "RPC lifecycle response could not be persisted"
        ) from last_error

    async def _reserve_lifecycle(
        self,
        request: ApplicationRpcRequest,
    ) -> tuple[_db.RpcRequestInfo | None, Mapping[str, object] | None]:
        waiter: asyncio.Future[Mapping[str, object]] | None = None
        async with self._lifecycle_lock:
            flight = self._lifecycle_flights.get(request.request_id)
            if flight is None:
                loop = asyncio.get_running_loop()
                owner = asyncio.current_task()
                flight = _LifecycleFlight(
                    request.semantic_sha256,
                    loop.create_future(),
                    owner,
                )
                self._lifecycle_flights[request.request_id] = flight
                if owner is not None:
                    self._inflight_lifecycles.add(owner)
                    owner.add_done_callback(
                        lambda task: self._lifecycle_owner_done(request, task)
                    )
            elif flight.semantic_sha256 != request.semantic_sha256:
                return None, self._error_response(
                    request,
                    error="request_conflict",
                    run_id=request.run_id,
                )
            else:
                waiter = flight.future
        if waiter is not None:
            remaining = self._remaining_for(request)
            if remaining <= 0:
                raise ApplicationRpcDurabilityError(
                    "RPC lifecycle flight outlived its request deadline"
                )
            try:
                replay = await asyncio.wait_for(
                    asyncio.shield(waiter),
                    timeout=remaining,
                )
            except asyncio.TimeoutError as exc:
                raise ApplicationRpcDurabilityError(
                    "RPC lifecycle flight outlived its request deadline"
                ) from exc
            if not isinstance(replay, Mapping):
                raise ApplicationRpcDurabilityError(
                    "RPC lifecycle flight has no durable response"
                )
            return None, replay
        connection: Any | None = None
        try:
            connection = self._connection()
            info = _db.reserve_rpc_request(connection, request=request, run_id=request.run_id)
        except Exception as exc:
            if "conflict" in str(exc).casefold():
                response = self._error_response(
                    request,
                    error="request_conflict",
                    run_id=request.run_id,
                )
                await self._finish_lifecycle_flight(request.request_id, response)
                return None, response
            raise ApplicationRpcDurabilityError(
                "RPC lifecycle reservation is indeterminate"
            ) from exc
        finally:
            if connection is not None:
                connection.close()
        replay = self._stored_response(info, request)
        if replay is not None:
            await self._finish_lifecycle_flight(request.request_id, replay)
            return info, replay
        if not info.created:
            if request.operation == "run.status":
                return info, None
            raise ApplicationRpcDurabilityError(
                "RPC lifecycle request is pending without a durable response"
            )
        return info, None

    def _reconcile_handoff_failure(self, connection: Any, run_id: int) -> None:
        root: ArtifactRoot | None = None
        try:
            root = ArtifactRoot.open_existing(self.config._artifact_root, cwd=Path.cwd())
            _db.reconcile_committed_handoff_failure(
                connection,
                run_id=run_id,
                coordinator_id=self.config._coordinator_id,
                artifact_root=root,
            )
        except Exception as exc:
            raise ApplicationRpcServiceError("unavailable") from exc
        finally:
            if root is not None:
                root.close()

    async def _read_status(self, run_id: int) -> _db.RpcRunStatus | None:
        connection = self._connection()
        try:
            self._reconcile_handoff_failure(connection, run_id)
            return _db.get_rpc_run_status(connection, run_id)
        finally:
            connection.close()
    async def _read_status_after_reservation(
        self,
        run_id: int,
    ) -> _db.RpcRunStatus | None:
        try:
            return await self._read_status(run_id)
        except ApplicationRpcDurabilityError:
            raise
        except BaseException as exc:
            raise ApplicationRpcDurabilityError(
                "RPC lifecycle status could not be reconciled"
            ) from exc


    async def _canonical_job_url(self, run_id: int) -> str | None:
        status = await self._read_status(run_id)
        return status.job_url if status is not None else None

    def _run_for(self, run_id: int) -> _ActiveRun | None:
        return self._runs.get(run_id)

    @staticmethod
    def _remaining_for(request: ApplicationRpcRequest) -> float:
        return (request.deadline_unix_ms - int(time.time() * 1000)) / 1000.0

    def _deadline_response(
        self,
        request: ApplicationRpcRequest,
        *,
        run_id: int | None = None,
    ) -> Mapping[str, object]:
        return self._error_response(
            request,
            error="deadline_exceeded",
            run_id=run_id if run_id is not None else request.run_id,
        )

    async def _await_lifecycle_work(
        self,
        request: ApplicationRpcRequest,
        operation: Callable[[], Any],
    ) -> Any:
        remaining = self._remaining_for(request)
        if remaining <= 0:
            raise _LifecycleDeadlineExceeded

        async def guarded_operation() -> Any:
            if self._remaining_for(request) <= 0:
                raise _LifecycleDeadlineExceeded
            return await operation()

        try:
            return await asyncio.wait_for(guarded_operation(), timeout=remaining)
        except _LifecycleDeadlineExceeded:
            raise
        except asyncio.TimeoutError as exc:
            raise _LifecycleDeadlineExceeded from exc


    def _lifecycle_transition_committed(
        self,
        request: ApplicationRpcRequest,
        *,
        cancellation_was_requested: bool = False,
    ) -> bool:
        """Read whether a lifecycle transition for ``request`` is durable."""
        if request.run_id is None:
            return False
        connection = self._connection()
        try:
            status = _db.get_rpc_run_status(connection, request.run_id)
            if status is None:
                return False
            if request.operation == "run.resume":
                return any(
                    event.request_id == request.request_id
                    and event.event_type == "resume_requested"
                    for event in _db.replay_rpc_events(connection, request.run_id)
                )
            if request.operation == "run.cancel":
                return (
                    not cancellation_was_requested
                    and status.cancellation_requested
                )
            return False
        finally:
            connection.close()

    async def _invoke_lifecycle_transition(
        self,
        request: ApplicationRpcRequest,
        operation: Callable[[], Any],
        *,
        cancellation_was_requested: bool = False,
        result_indicates_transition: Callable[[Any], bool] | None = None,
    ) -> Any:
        """Run one transition only while its request deadline remains live."""
        try:
            result = await self._await_lifecycle_work(request, operation)
        except _LifecycleDeadlineExceeded:
            try:
                committed = self._lifecycle_transition_committed(
                    request,
                    cancellation_was_requested=cancellation_was_requested,
                )
            except BaseException as exc:
                raise ApplicationRpcDurabilityError(
                    "RPC lifecycle transition could not be reconciled"
                ) from exc
            if committed:
                raise ApplicationRpcDurabilityError(
                    "RPC lifecycle transition is indeterminate"
                )
            raise
        if self._remaining_for(request) <= 0:
            committed = (
                bool(result)
                if result_indicates_transition is None
                else bool(result_indicates_transition(result))
            )
            if not committed:
                try:
                    committed = self._lifecycle_transition_committed(
                        request,
                        cancellation_was_requested=cancellation_was_requested,
                    )
                except BaseException as exc:
                    raise ApplicationRpcDurabilityError(
                        "RPC lifecycle transition could not be reconciled"
                    ) from exc
            if committed:
                raise ApplicationRpcDurabilityError(
                    "RPC lifecycle transition is indeterminate"
                )
            raise _LifecycleDeadlineExceeded
        return result

    async def _complete_lifecycle_response(
        self,
        request: ApplicationRpcRequest,
        response: Mapping[str, object],
        *,
        transition_linearized: bool = False,
    ) -> Mapping[str, object]:
        """Persist a lifecycle response, replacing it on an uncommitted timeout."""
        if self._remaining_for(request) <= 0:
            if transition_linearized:
                raise ApplicationRpcDurabilityError(
                    "RPC lifecycle response is indeterminate after transition"
                )
            response = self._deadline_response(request)
            return await self._complete_lifecycle(
                request,
                response,
                enforce_deadline=False,
            )
        try:
            return await self._await_lifecycle_work(
                request,
                lambda: self._complete_lifecycle(request, response),
            )
        except (_db.RpcDeadlineExceeded, _LifecycleDeadlineExceeded) as exc:
            if transition_linearized:
                raise ApplicationRpcDurabilityError(
                    "RPC lifecycle response is indeterminate after transition"
                ) from exc
            # A synchronous database commit may have won the race with the
            # timeout.  Replay it rather than attempting a conflicting result.
            connection = self._connection()
            try:
                info = _db.get_rpc_request(connection, request.request_id)
            finally:
                connection.close()
            if info is not None:
                replay = self._stored_response(info, request)
                if replay is not None:
                    return replay
            return await self._complete_lifecycle(
                request,
                self._deadline_response(request),
                enforce_deadline=False,
            )

    async def _complete_deadline_lifecycle(
        self,
        request: ApplicationRpcRequest,
    ) -> Mapping[str, object]:
        """Reserve and durably finish a lifecycle request that missed its deadline."""
        info, replay = await self._reserve_lifecycle(request)
        if replay is not None:
            return replay
        if info is None:
            raise ApplicationRpcDurabilityError(
                "Expired lifecycle reservation is incomplete"
            )
        return await self._complete_lifecycle_response(
            request,
            self._deadline_response(request),
        )

    def _track_cleanup_task(self, task: asyncio.Task[Any]) -> None:
        self._late_launch_cleanups.add(task)
        task.add_done_callback(self._late_launch_cleanups.discard)

    @staticmethod
    async def _invoke_process_close(process: Any) -> bool:
        close = getattr(process, "close", None)
        if not callable(close):
            return False
        exact_identity: Mapping[str, object] | None = None
        if isinstance(process, OmpRpcProcess):
            identity = process.process_identity
            if not isinstance(identity, Mapping):
                return False
            exact_identity = dict(identity)
        try:
            value = await asyncio.to_thread(close)
            if inspect.isawaitable(value):
                await value
        except BaseException:
            return False
        if exact_identity is None:
            return True
        state = await asyncio.to_thread(
            _db._exact_process_identity_state,
            exact_identity,
        )
        return bool(process.closed) and state == "absent"

    async def _cleanup_late_launch(self, task: asyncio.Task[Any]) -> bool:
        try:
            process = await asyncio.shield(task)
        except asyncio.CancelledError:
            return task.cancelled()
        except Exception:
            return False
        return await self._invoke_process_close(process)

    async def _release_start_after_cleanup(
        self,
        run_id: int,
        cleanup_task: asyncio.Task[bool],
    ) -> None:
        try:
            cleaned = bool(await asyncio.shield(cleanup_task))
        except BaseException:
            return
        if not cleaned:
            return
        connection = self._connection()
        try:
            _db.release_quarantined_rpc_start(
                connection,
                run_id=run_id,
                coordinator_id=self.config._coordinator_id,
            )
        except Exception:
            return
        finally:
            connection.close()

    def _committed_start_response(
        self,
        request: ApplicationRpcRequest,
        run_id: int,
    ) -> Mapping[str, object] | None:
        connection = self._connection()
        try:
            info = _db.get_rpc_request(connection, request.request_id)
        finally:
            connection.close()
        if info is None or info.state != "completed" or info.response_json is None:
            return None
        try:
            response = parse_application_response(
                info.response_json,
                request=request,
            )
        except Exception:
            return None
        if (
            response.get("ok") is True
            and response.get("run_id") == run_id
        ):
            return response
        return None


    async def _abort_start(
        self,
        request: ApplicationRpcRequest,
        run_id: int,
        control: RpcApplicationControl,
        process: Any | None,
        *,
        error_code: str,
        late_launch_task: asyncio.Task[Any] | None = None,
        cleanup_unverified: bool = False,
    ) -> Mapping[str, object]:
        control._cancel_event.set()
        control._resume_event.set()
        control._closed = True
        committed_response = self._committed_start_response(request, run_id)
        cleanup_task: asyncio.Task[bool] | None = None
        if late_launch_task is not None:
            cleanup_task = asyncio.create_task(
                self._cleanup_late_launch(late_launch_task),
                name=f"application-rpc-late-launch-cleanup-{run_id}",
            )
        elif process is not None:
            cleanup_task = asyncio.create_task(
                self._invoke_process_close(process),
                name=f"application-rpc-process-close-{run_id}",
            )
        cleanup_complete = cleanup_task is None and not cleanup_unverified
        if cleanup_task is not None:
            self._track_cleanup_task(cleanup_task)
            try:
                if committed_response is not None:
                    cleanup_complete = bool(await asyncio.shield(cleanup_task))
                else:
                    cleanup_complete = bool(
                        await asyncio.wait_for(
                            asyncio.shield(cleanup_task),
                            timeout=_CLEANUP_DRAIN_SECONDS,
                        )
                    )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                cleanup_complete = False
        if committed_response is not None:
            if not cleanup_complete:
                raise ApplicationRpcDurabilityError(
                    "Committed RPC start process cleanup was not verified"
                )
            await control.finalize_failure(
                run_id,
                status="failed",
                reason_code=(
                    "abandoned_running_attempt"
                    if error_code == "cancelled"
                    else "browser_error"
                ),
                observation_summary={"error_code": error_code},
                plan_summary={},
                artifact_dir=None,
                observation_sha256=control._last_observation_sha256,
                manifest_sha256=control._manifest_sha256(run_id),
                error_code=(
                    "cancelled"
                    if error_code == "cancelled"
                    else "workflow_failed"
                ),
            )
            return committed_response
        info: _db.RpcRequestInfo | None = None
        last_error: BaseException | None = None
        for attempt in range(2):
            connection = None
            try:
                connection = self._connection()
                info = _db.abort_rpc_start(
                    connection,
                    request=request,
                    coordinator_id=self.config._coordinator_id,
                    error_code=error_code,
                    release_claim=cleanup_complete,
                )
            except BaseException as exc:
                last_error = exc
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
            if info is not None:
                break
            if attempt == 0:
                await asyncio.sleep(0)
        if info is None or info.response_json is None:
            raise ApplicationRpcDurabilityError(
                "RPC start abort could not be persisted"
            ) from last_error
        try:
            response = parse_application_response(
                info.response_json,
                request=request,
            )
        except Exception as exc:
            raise ApplicationRpcDurabilityError(
                "Persisted RPC start abort is unreadable"
            ) from exc
        if not cleanup_complete and cleanup_task is not None:
            release_task = asyncio.create_task(
                self._release_start_after_cleanup(run_id, cleanup_task),
                name=f"application-rpc-quarantine-release-{run_id}",
            )
            self._track_cleanup_task(release_task)
        return response

    async def _cancel_launch_task(
        self,
        task: asyncio.Task[Any],
    ) -> tuple[Any | None, asyncio.Task[Any] | None, bool]:
        if not task.done():
            task.cancel()
        try:
            process = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=_LAUNCH_CANCEL_GRACE_SECONDS,
            )
            return process, None, False
        except asyncio.TimeoutError:
            self._track_cleanup_task(task)
            return None, task, False
        except asyncio.CancelledError:
            if task.done():
                try:
                    return task.result(), None, False
                except OmpRpcCleanupError:
                    return None, None, True
                except BaseException:
                    return None, None, False
            self._track_cleanup_task(task)
            return None, task, False
        except OmpRpcCleanupError:
            return None, None, True
        except Exception:
            return None, None, False


    async def _read_owned_status(
        self, run_id: int
    ) -> tuple[_db.RpcRunStatus | None, bool]:
        connection = self._connection()
        try:
            self._reconcile_handoff_failure(connection, run_id)
            status = _db.get_rpc_run_status(connection, run_id)
            owned = bool(
                status is not None
                and _db.rpc_run_owner_matches(
                    connection,
                    run_id=run_id,
                    coordinator_id=self.config._coordinator_id,
                )
            )
            return status, owned
        finally:
            connection.close()

    def _load_start_inputs(self) -> tuple[ApplicationPreferences, str]:
        if self.config._application_profile_preset is not None:
            if self.config._application_profile_dir is None:
                raise ApplicationRpcServiceError("invalid_config")
            preset = load_application_profile_preset(
                self.config._application_profile_dir,
                self.config._application_profile_preset,
                cwd=Path.cwd(),
            )
            profile = preset.profile
            profile_sha256 = preset.source_sha256
        else:
            loaded_profile = load_application_profile_snapshot(
                self.config._application_profile_json
            )
            profile = loaded_profile.profile
            profile_sha256 = (
                loaded_profile.source_sha256 or DEFAULT_APPLICATION_PROFILE_SHA256
            )
        if (
            self._identity is not None
            and not hmac.compare_digest(
                profile_sha256,
                self._identity["candidate_profile_id"],
            )
        ):
            raise ApplicationRpcServiceError("request_conflict")
        preferences = load_application_preferences(
            self.config._application_preferences,
            cwd=Path.cwd(),
        )
        applicant_description = load_applicant_description(
            self.config._applicant_description_file,
            profile,
        )
        return preferences, applicant_description

    async def _handle_start(self, request: ApplicationRpcRequest) -> Mapping[str, object]:
        if self._identity is None:
            raise ApplicationRpcServiceError("unavailable")
        if self._remaining_for(request) <= 0:
            return build_rejected_application_response(
                request.to_mapping(), error="deadline_exceeded"
            )
        connection: Any | None = None
        try:
            connection = self._connection()
            existing_request = _db.get_rpc_request(connection, request.request_id)
        except Exception as exc:
            raise ApplicationRpcDurabilityError(
                "RPC start request ledger probe is unreadable"
            ) from exc
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        if existing_request is not None and (
            existing_request.semantic_sha256 != request.semantic_sha256
            or existing_request.protocol_version != request.protocol_version
            or existing_request.operation != request.operation
            or existing_request.parent_request_id is not None
        ):
            return build_rejected_application_response(
                request.to_mapping(),
                error="request_conflict",
            )
        if existing_request is not None:
            if existing_request.run_id is not None:
                owner_connection: Any | None = None
                try:
                    owner_connection = self._connection()
                    owned = _db.rpc_run_owner_matches(
                        owner_connection,
                        run_id=existing_request.run_id,
                        coordinator_id=self.config._coordinator_id,
                    )
                except Exception as exc:
                    raise ApplicationRpcDurabilityError(
                        "RPC start run ownership probe is unreadable"
                    ) from exc
                finally:
                    if owner_connection is not None:
                        try:
                            owner_connection.close()
                        except Exception:
                            pass
                if not owned:
                    return self._error_response(
                        request,
                        error="run_not_owned",
                        run_id=existing_request.run_id,
                    )
            replay = self._stored_response(existing_request, request)
            if replay is not None:
                return replay
        payload = request.payload
        if existing_request is None:
            try:
                refreshed_identity = resolve_application_rpc_identity(self.config)
            except ApplicationRpcServiceError as exc:
                return build_rejected_application_response(request.to_mapping(), error=exc.code)
            except Exception:
                return build_rejected_application_response(request.to_mapping(), error="unavailable")
            if (
                not hmac.compare_digest(
                    refreshed_identity["configured_resume_id"],
                    self._identity["configured_resume_id"],
                )
                or not hmac.compare_digest(
                    refreshed_identity["candidate_profile_id"],
                    self._identity["candidate_profile_id"],
                )
            ):
                return build_rejected_application_response(request.to_mapping(), error="request_conflict")
        if not hmac.compare_digest(str(payload.get("configured_resume_id", "")), self._identity["configured_resume_id"]):
            return build_rejected_application_response(request.to_mapping(), error="request_conflict")
        if not hmac.compare_digest(str(payload.get("candidate_profile_id", "")), self._identity["candidate_profile_id"]):
            return build_rejected_application_response(request.to_mapping(), error="request_conflict")
        start_preferences: ApplicationPreferences | None = None
        start_applicant_description: str | None = None
        if existing_request is None:
            try:
                start_preferences, start_applicant_description = self._load_start_inputs()
            except ApplicationRpcServiceError as exc:
                return build_rejected_application_response(
                    request.to_mapping(),
                    error=exc.code,
                )
            except Exception:
                return build_rejected_application_response(
                    request.to_mapping(),
                    error="invalid_config",
                )
        connection: Any | None = None
        deadline_during_claim = False
        try:
            connection = self._connection()
            outcome = _db.claim_application_job_for_rpc(
                connection,
                owner=self._owner(),
                request=request,
                coordinator_id=self.config._coordinator_id,
            )
        except _db.RpcDeadlineExceeded:
            deadline_during_claim = True
        except Exception as exc:
            raise ApplicationRpcDurabilityError(
                "RPC start claim outcome is indeterminate"
            ) from exc
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        if deadline_during_claim:
            return await self._complete_deadline_lifecycle(request)
        try:
            outcome_name = outcome.outcome
            outcome_run_id = outcome.run_id
            outcome_claim = outcome.claim
        except Exception as exc:
            raise ApplicationRpcDurabilityError(
                "RPC start claim acknowledgement is invalid"
            ) from exc
        if outcome_name == "conflict":
            return build_rejected_application_response(request.to_mapping(), error="request_conflict")
        if outcome_name == "pending":
            if outcome_run_id is None:
                response = self._error_response(request, error="unavailable", run_id=None)
                return await self._complete_lifecycle(request, response)
            raise ApplicationRpcDurabilityError(
                "RPC start request is pending without a durable response"
            )
        if outcome_name == "completed":
            connection: Any | None = None
            info: _db.RpcRequestInfo | None = None
            try:
                connection = self._connection()
                info = _db.get_rpc_request(connection, request.request_id)
            except Exception as exc:
                raise ApplicationRpcDurabilityError(
                    "Completed RPC start response probe is unreadable"
                ) from exc
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
            if info is not None:
                if (
                    info.semantic_sha256 != request.semantic_sha256
                    or info.protocol_version != request.protocol_version
                    or info.operation != request.operation
                    or info.parent_request_id is not None
                ):
                    return build_rejected_application_response(
                        request.to_mapping(),
                        error="request_conflict",
                    )
                replay = self._stored_response(info, request)
                if replay is not None:
                    return replay
            raise ApplicationRpcDurabilityError(
                "Completed RPC start response is unreadable"
            )
        if outcome_name == "unavailable":
            pending_request: _db.RpcRequestInfo | None = None
            connection: Any | None = None
            try:
                connection = self._connection()
                pending_request = _db.get_rpc_request(
                    connection,
                    request.request_id,
                )
            except Exception as exc:
                raise ApplicationRpcDurabilityError(
                    "RPC start request ledger probe is unreadable"
                ) from exc
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
            if pending_request is None:
                # Prior-process and coordinator-identity conflicts roll back
                # with no ledger row, so this is the only retryable path.
                return build_rejected_application_response(
                    request.to_mapping(),
                    error="unavailable",
                )
            if (
                pending_request.semantic_sha256 != request.semantic_sha256
                or pending_request.protocol_version != request.protocol_version
                or pending_request.operation != request.operation
                or pending_request.parent_request_id is not None
            ):
                return build_rejected_application_response(
                    request.to_mapping(),
                    error="request_conflict",
                )
            if pending_request.state == "completed":
                replay = self._stored_response(pending_request, request)
                if replay is not None:
                    return replay
                raise ApplicationRpcDurabilityError(
                    "Completed RPC start response is unreadable"
                )
            if pending_request.state == "pending" and pending_request.run_id is None:
                # Only the no-job claim deliberately leaves a pending row.
                response = self._error_response(request, error="unavailable", run_id=None)
                return await self._complete_lifecycle(request, response)
            raise ApplicationRpcDurabilityError(
                "RPC start unavailable outcome has an unexpected durable request row"
            )
        if (
            outcome_name != "new"
            or outcome_claim is None
            or type(outcome_run_id) is not int
            or outcome_run_id <= 0
        ):
            raise ApplicationRpcDurabilityError(
                "RPC start claim acknowledgement is invalid"
            )
        claim = outcome_claim
        control = RpcApplicationControl(
            db_path=self.config._db_path,
            artifact_root=self.config._artifact_root,
            coordinator_id=self.config._coordinator_id,
            connection_factory=self.config._connection_factory,
            event_callback=self.config._event_callback,
            run_id=outcome_run_id,
            parent_request=request,
        )
        process: Any | None = None
        launch_task: asyncio.Task[Any] | None = None
        late_launch_task: asyncio.Task[Any] | None = None
        cleanup_unverified = False
        try:
            remaining = self._remaining_for(request)
            if remaining <= 0:
                raise ApplicationRpcServiceError("deadline_exceeded")
            launch_task = asyncio.create_task(
                self._launch_omp(outcome_run_id, control),
                name=f"application-rpc-launch-{outcome_run_id}",
            )
            try:
                process = await asyncio.wait_for(asyncio.shield(launch_task), timeout=remaining)
            except asyncio.TimeoutError:
                process, late_launch_task, cleanup_unverified = (
                    await self._cancel_launch_task(launch_task)
                )
                raise ApplicationRpcServiceError("deadline_exceeded")
            except OmpRpcCleanupError:
                cleanup_unverified = True
                raise ApplicationRpcServiceError("unavailable")
            except asyncio.CancelledError:
                process, late_launch_task, cleanup_unverified = (
                    await self._cancel_launch_task(launch_task)
                )
                raise
            if self._remaining_for(request) <= 0:
                raise ApplicationRpcServiceError("deadline_exceeded")
            if not self._verified_process(process):
                raise ApplicationRpcServiceError("unavailable")
            control.set_process(process)
            connection = self._connection()
            try:
                pid = getattr(process, "pid", None)
                session_sha = getattr(process, "session_identity_sha256", None)
                if (
                    type(pid) is not int
                    or pid <= 0
                    or type(session_sha) is not str
                    or not _SHA256_RE.fullmatch(session_sha)
                ):
                    raise ApplicationRpcServiceError("unavailable")
                registered = _db.update_rpc_run_process(
                    connection,
                    run_id=outcome_run_id,
                    coordinator_id=self.config._coordinator_id,
                    pid=pid,
                    session_sha256=session_sha,
                )
                if not registered:
                    raise ApplicationRpcServiceError("unavailable")
                status = _db.get_rpc_run_status(connection, outcome_run_id)
            finally:
                connection.close()
            if self._remaining_for(request) <= 0:
                raise ApplicationRpcServiceError("deadline_exceeded")
            if status is None:
                raise ApplicationRpcServiceError("unavailable")
            result = self._status_result(status, control, job_url=str(request.payload["job_url"]))
            response = build_application_response(
                request,
                ok=True,
                state="starting",
                action_sequence=status.action_sequence,
                event_sequence=status.latest_event_sequence,
                result=result,
                run_id=outcome_run_id,
            )
            response = await self._complete_lifecycle(request, response)
        except asyncio.CancelledError:
            if (
                launch_task is not None
                and not launch_task.done()
                and late_launch_task is None
            ):
                process, late_launch_task, cleanup_unverified = (
                    await self._cancel_launch_task(launch_task)
                )
            try:
                await self._abort_start(
                    request,
                    outcome_run_id,
                    control,
                    process,
                    error_code="cancelled",
                    late_launch_task=late_launch_task,
                    cleanup_unverified=cleanup_unverified,
                )
            finally:
                raise
        except ApplicationRpcServiceError as exc:
            return await self._abort_start(
                request,
                outcome_run_id,
                control,
                process,
                error_code=exc.code,
                late_launch_task=late_launch_task,
                cleanup_unverified=cleanup_unverified,
            )
        except Exception:
            return await self._abort_start(
                request,
                outcome_run_id,
                control,
                process,
                error_code="unavailable",
                late_launch_task=late_launch_task,
                cleanup_unverified=cleanup_unverified,
            )
        active = _ActiveRun(
            outcome_run_id,
            claim,
            request,
            control,
            process,
            start_preferences
            if start_preferences is not None
            else ApplicationPreferences(1, (), (), ()),
            start_applicant_description if start_applicant_description is not None else "",
        )
        self._runs[outcome_run_id] = active
        active.workflow_task = asyncio.create_task(
            self._run_workflow(active), name=f"application-rpc-{outcome_run_id}"
        )
        control.set_workflow_task(active.workflow_task)
        return response

    def _mark_omp_spawn_attempted(self, run_id: int) -> None:
        connection = self._connection()
        try:
            marked = _db.mark_rpc_omp_spawn_attempted(
                connection,
                run_id=run_id,
                coordinator_id=self.config._coordinator_id,
            )
        finally:
            connection.close()
        if not marked:
            raise ApplicationRpcDurabilityError(
                "OMP spawn attempt could not be persisted"
            )


    def _register_omp_spawn(
        self,
        run_id: int,
        identity: Mapping[str, object],
    ) -> None:
        if set(identity) != {"pid", "pgid", "birth"}:
            raise ApplicationRpcDurabilityError(
                "OMP spawn identity is invalid"
            )
        pid = identity.get("pid")
        pgid = identity.get("pgid")
        birth = identity.get("birth")
        if (
            type(pid) is not int
            or pid <= 0
            or type(pgid) is not int
            or pgid <= 0
            or type(birth) is not str
            or not birth
        ):
            raise ApplicationRpcDurabilityError(
                "OMP spawn identity is invalid"
            )
        provisional = _db.rpc_provisional_session_sha256(
            pid,
            pgid,
            birth,
        )
        connection = self._connection()
        try:
            registered = _db.update_rpc_run_process(
                connection,
                run_id=run_id,
                coordinator_id=self.config._coordinator_id,
                pid=pid,
                session_sha256=provisional,
                process_identity=identity,
            )
        finally:
            connection.close()
        if not registered:
            raise ApplicationRpcDurabilityError(
                "OMP spawn identity could not be persisted"
            )

    async def _launch_omp(self, run_id: int, control: RpcApplicationControl) -> Any:
        launch_config = self.config._omp_launch_config
        factory = self.config._omp_launch_config_factory
        if factory is not None:
            launch_config = await self._call_launch_factory(factory, run_id)
        process_factory = self.config._omp_process_factory
        callback = control.handle_host_tool
        if process_factory is None:
            if launch_config is None:
                raise ApplicationRpcServiceError("invalid_config")
            return await OmpRpcProcess.launch(
                launch_config,
                host_tool_callback=callback,
                on_spawn_attempt=lambda: self._mark_omp_spawn_attempted(
                    run_id
                ),
                on_spawn=lambda identity: self._register_omp_spawn(
                    run_id,
                    identity,
                ),
            )
        self._mark_omp_spawn_attempted(run_id)
        value = self._call_process_factory(process_factory, launch_config, callback)
        if inspect.isawaitable(value):
            value = await value
        return value

    async def _call_launch_factory(self, factory: Callable[..., Any], run_id: int) -> Any:
        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError):
            value = factory(run_id)
        else:
            parameters = tuple(signature.parameters.values())
            if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
                value = factory(run_id=run_id, coordinator=self)
            else:
                run_parameter = next(
                    (parameter for parameter in parameters if parameter.name == "run_id"),
                    None,
                )
                positional = tuple(
                    parameter
                    for parameter in parameters
                    if parameter.kind
                    in {
                        inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    }
                )
                if run_parameter is not None and run_parameter.kind is not inspect.Parameter.POSITIONAL_ONLY:
                    value = factory(run_id=run_id)
                elif not positional:
                    value = factory()
                else:
                    value = factory(run_id)
        if inspect.isawaitable(value):
            return await value
        return value


    def _call_process_factory(self, factory: Callable[..., Any], launch_config: Any, callback: Any) -> Any:
        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError):
            return factory(launch_config, callback)
        parameters = tuple(signature.parameters.values())
        if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
            return factory(config=launch_config, host_tool_callback=callback)
        positional = tuple(
            parameter
            for parameter in parameters
            if parameter.kind
            in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        )
        names = {parameter.name for parameter in parameters}
        if "host_tool_callback" in names:
            if len(positional) >= 2 or (
                positional and positional[0].name in {"config", "launch_config"}
            ):
                return factory(launch_config, host_tool_callback=callback)
            return factory(host_tool_callback=callback)
        if len(positional) >= 2:
            return factory(launch_config, callback)
        if len(positional) == 1:
            return factory(launch_config)
        return factory()

    @staticmethod
    def _verified_process(process: Any) -> bool:
        if process is None:
            return False
        verified = getattr(process, "verified", None)
        if verified is None:
            verification = getattr(process, "verification", None)
            verified = bool(getattr(verification, "all_verified", False))
        return bool(verified) and not bool(getattr(process, "poisoned", False))

    async def _close_process(
        self,
        process: Any,
        *,
        timeout: float = _CLEANUP_DRAIN_SECONDS,
    ) -> bool:
        if process is None:
            return True
        cleanup_task = asyncio.create_task(
            self._invoke_process_close(process)
        )
        self._track_cleanup_task(cleanup_task)
        try:
            return bool(
                await asyncio.wait_for(
                    asyncio.shield(cleanup_task),
                    timeout=max(0.05, timeout),
                )
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return False

    def _abort_active_for_shutdown(self, active: _ActiveRun) -> bool:
        connection = self._connection()
        try:
            return bool(
                _db.abort_rpc_run_for_shutdown(
                    connection,
                    run_id=active.run_id,
                    coordinator_id=self.config._coordinator_id,
                )
            )
        except Exception:
            return False
        finally:
            connection.close()

    async def _run_workflow(self, active: _ActiveRun) -> None:
        workflow = self.config._workflow or run_application_workflow
        consumed = False
        claim = active.claim

        def claim_provider(_connection: Any, **_kwargs: Any) -> _db.ApplicationClaim | None:
            nonlocal consumed
            if consumed:
                return None
            consumed = True
            return claim

        connection = self._connection()
        try:
            kwargs: dict[str, Any] = {
                "limit": 1,
                "resume_file": self.config._resume_file,
                "application_profile_json": self.config._application_profile_json,
                "application_profile_preset": self.config._application_profile_preset,
                "application_profile_dir": self.config._application_profile_dir,
                "application_preferences": self.config._application_preferences,
                "applicant_description_file": self.config._applicant_description_file,
                "application_preferences_snapshot": active.preferences,
                "applicant_description_snapshot": active.applicant_description,
                "artifact_root": self.config._artifact_root,
                "headed": self.config._headed,
                "ats": self.config._ats,
                "claim_provider": claim_provider,
                "control": active.control,
                "expected_resume_sha256": self._identity["configured_resume_id"] if self._identity is not None else None,
                "expected_profile_sha256": self._identity["candidate_profile_id"] if self._identity is not None else None,
            }
            value = self._invoke_workflow(workflow, connection, kwargs)
            if inspect.isawaitable(value):
                value = await value
            await self._workflow_finished(active, value)
        except asyncio.CancelledError:
            if not active.control.handoff_committed:
                await active.control.fail("abandoned_running_attempt")
            raise
        except Exception:
            await active.control.fail("workflow_failed")
        finally:
            connection.close()
            if active.control.post_commit_guard and not active.control.handoff_committed:
                try:
                    await active.control.close()
                except Exception:
                    pass
            cleanup_complete = await self._close_process(active.process)
            if cleanup_complete:
                self._runs.pop(active.run_id, None)

    @staticmethod
    def _invoke_workflow(workflow: Callable[..., Any], connection: Any, kwargs: dict[str, Any]) -> Any:
        try:
            signature = inspect.signature(workflow)
        except (TypeError, ValueError):
            return workflow(connection, **kwargs)
        parameters = signature.parameters
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        filtered = kwargs if accepts_kwargs else {
            key: value for key, value in kwargs.items() if key in parameters
        }
        return workflow(connection, **filtered)

    async def _workflow_finished(self, active: _ActiveRun, value: Any) -> None:
        if active.control.handoff_committed:
            return
        item: Mapping[str, Any] | None = None
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) and value:
            first = value[0]
            if isinstance(first, Mapping):
                item = first
        elif isinstance(value, Mapping):
            item = value
        status_name = str(item.get("status")) if item is not None else None
        reason = str(item.get("reason_code")) if item is not None and item.get("reason_code") is not None else None
        if status_name not in {"manual", "blocked", "review_ready", "failed"}:
            current = await self._read_status(active.run_id)
            status_name = current.state if current is not None else "failed"
            reason = reason or (current.reason_code if current is not None else None)
        if status_name == "review_ready":
            # A review-ready result is only valid after the durable handoff.
            status_name = "manual"
            reason = reason or "no_deterministic_next_step"
        if status_name not in {"manual", "blocked", "failed"}:
            status_name = "failed"
        await self._terminalize(active.control, status_name, reason)

    async def _terminalize(self, control: RpcApplicationControl, state: str, reason: str | None) -> None:
        if control.run_id is None or control.handoff_committed:
            return
        if state == "failed":
            current = await self._read_status(control.run_id)
            if current is not None and current.state == "failed":
                control._browser_state = "failed"
                control._coordinator_state = "terminal"
                return
            application_reason = (
                reason
                if reason in _db.PUBLIC_REASON_CODES
                else "browser_error"
            )
            await control.finalize_failure(
                control.run_id,
                status="failed",
                reason_code=application_reason,
                observation_summary={"error_code": reason or "workflow_failed"},
                plan_summary={},
                artifact_dir=None,
                observation_sha256=control._last_observation_sha256,
                manifest_sha256=control._manifest_sha256(control.run_id),
                error_code=(
                    reason
                    if reason in {
                        "cancelled",
                        "workflow_failed",
                        "deadline_exceeded",
                        "internal_error",
                    }
                    else "workflow_failed"
                ),
            )
            return
        event_type = "manual_intervention_required"
        summary = "manual_required"
        manifest_sha = control._manifest_sha256(control.run_id)
        event: _db.RpcEventInfo | None = None
        async with control._db_lock:
            connection = control._connection()
            try:
                control._refresh_sequences_locked(connection)
                status_row = _db.get_rpc_run_status(connection, control.run_id)
                if status_row is None or status_row.state in {"failed", "review_ready"}:
                    return
                sequence = control._next_sequence_locked()
                event = _db.commit_rpc_run_transition(
                    connection,
                    _db.RpcRunTransition(
                        run_id=control.run_id,
                        coordinator_id=control._coordinator_id,
                        request_id=control._event_request_id(),
                        action_sequence=sequence,
                        event_type=event_type,
                        summary_code=summary,
                        state=state,
                        ats_policy=control._ats_policy,
                        observation_sha256=control._last_observation_sha256,
                        manifest_sha256=manifest_sha,
                    ),
                )
                control._event_sequence = event.sequence
            finally:
                connection.close()
        control._coordinator_state = "terminal"
        await control._emit(event)

    async def handle(self, value: object) -> Mapping[str, object]:
        request_task = asyncio.create_task(
            self._handle_request(value),
            name="application-rpc-request",
        )
        return await request_task

    async def _handle_request(self, value: object) -> Mapping[str, object]:
        if self._closed:
            return build_rejected_application_response(value, error="unavailable")
        try:
            request = parse_application_request(value)
        except ApplicationRpcError as exc:
            return build_rejected_application_response(value, error=exc.code)
        if request.operation in BROWSER_OPERATIONS:
            return build_rejected_application_response(
                request.to_mapping(),
                error="unsupported_operation",
            )
        if request.operation == "run.start":
            task = asyncio.current_task()
            if task is not None:
                self._inflight_starts.add(task)
            try:
                try:
                    await self.start()
                except ApplicationRpcServiceError as exc:
                    return build_rejected_application_response(
                        request.to_mapping(),
                        error=exc.code,
                    )
                async with self._dispatch_lock:
                    if self._closed:
                        return build_rejected_application_response(
                            request.to_mapping(),
                            error="unavailable",
                        )
                    return await self._handle_start(request)
            finally:
                if task is not None:
                    self._inflight_starts.discard(task)
        try:
            await self.start()
        except ApplicationRpcServiceError as exc:
            return build_rejected_application_response(
                request.to_mapping(),
                error=exc.code,
            )
        run_id = request.run_id
        if run_id is None:
            return build_rejected_application_response(request.to_mapping(), error="invalid_request")
        connection = self._connection()
        try:
            existing_request = _db.get_rpc_request(
                connection,
                request.request_id,
            )
        finally:
            connection.close()
        if existing_request is not None and existing_request.run_id is None:
            info, replay = await self._reserve_lifecycle(request)
            if replay is not None:
                return replay
            raise ApplicationRpcDurabilityError(
                "Unbound RPC lifecycle request has no durable response"
            )
        try:
            status, owned = await self._await_lifecycle_work(
                request,
                lambda: self._read_owned_status(run_id),
            )
        except _LifecycleDeadlineExceeded:
            return await self._complete_deadline_lifecycle(request)
        if status is None:
            info, replay = await self._reserve_lifecycle(request)
            if replay is not None:
                return replay
            if info is None:
                raise ApplicationRpcDurabilityError(
                    "Unknown-run lifecycle reservation is incomplete"
                )
            response = self._error_response(
                request,
                error="run_not_found",
                run_id=run_id,
            )
            return await self._complete_lifecycle_response(request, response)
        if not owned:
            return build_rejected_application_response(request.to_mapping(), error="run_not_owned")
        if self._remaining_for(request) <= 0:
            return await self._complete_deadline_lifecycle(request)
        active = self._run_for(run_id)
        if request.operation == "run.status":
            info, replay = await self._reserve_lifecycle(request)
            if replay is not None:
                if info is None or info.state == "completed":
                    return replay
            if info is None:
                raise ApplicationRpcDurabilityError(
                    "Status lifecycle reservation is incomplete"
                )
            if self._remaining_for(request) <= 0:
                return await self._complete_lifecycle_response(
                    request,
                    self._deadline_response(request, run_id=run_id),
                )
            try:
                latest_status = await self._await_lifecycle_work(
                    request,
                    lambda: self._read_status_after_reservation(run_id),
                )
            except _LifecycleDeadlineExceeded:
                return await self._complete_lifecycle_response(
                    request,
                    self._deadline_response(request, run_id=run_id),
                )
            if latest_status is None:
                response = self._error_response(
                    request,
                    error="run_not_found",
                    run_id=run_id,
                )
                return await self._complete_lifecycle_response(request, response)
            status = latest_status
            response = self._status_response(
                request,
                status,
                active.control if active else None,
                job_url=status.job_url,
            )
            return await self._complete_lifecycle_response(request, response)
        if request.operation == "run.resume":
            info, replay = await self._reserve_lifecycle(request)
            if replay is not None:
                if info is None or info.state == "completed":
                    return replay
            if info is None:
                raise ApplicationRpcDurabilityError(
                    "Resume lifecycle reservation is incomplete"
                )
            if self._remaining_for(request) <= 0:
                return await self._complete_lifecycle_response(
                    request,
                    self._deadline_response(request, run_id=run_id),
                )
            try:
                latest_status = await self._await_lifecycle_work(
                    request,
                    lambda: self._read_status_after_reservation(run_id),
                )
            except _LifecycleDeadlineExceeded:
                return await self._complete_lifecycle_response(
                    request,
                    self._deadline_response(request, run_id=run_id),
                )
            if latest_status is None:
                response = self._error_response(
                    request,
                    error="run_not_found",
                    run_id=run_id,
                )
                return await self._complete_lifecycle_response(request, response)
            status = latest_status
            active = self._run_for(run_id)
            if (
                active is None
                or not active.control.awaiting_resume
                or active.workflow_task is None
                or active.workflow_task.done()
            ):
                response = self._error_response(
                    request,
                    error="run_not_active",
                    run_id=run_id,
                    state=status.state,
                    action_sequence=status.action_sequence,
                    event_sequence=status.latest_event_sequence,
                )
                return await self._complete_lifecycle_response(request, response)
            try:
                resumed = await self._invoke_lifecycle_transition(
                    request,
                    lambda: active.control.resume(request),
                )
            except _LifecycleDeadlineExceeded:
                return await self._complete_lifecycle_response(
                    request,
                    self._deadline_response(request, run_id=run_id),
                )
            except ApplicationRpcDurabilityError:
                raise
            except BaseException as exc:
                raise ApplicationRpcDurabilityError(
                    "Resume transition is indeterminate"
                ) from exc
            try:
                latest = await self._await_lifecycle_work(
                    request,
                    lambda: self._read_status_after_reservation(run_id),
                )
            except _LifecycleDeadlineExceeded as exc:
                resume_transition_linearized = bool(resumed)
                if not resume_transition_linearized:
                    try:
                        resume_transition_linearized = self._lifecycle_transition_committed(
                            request,
                        )
                    except BaseException as probe_exc:
                        raise ApplicationRpcDurabilityError(
                            "Resume transition response could not be reconciled"
                        ) from probe_exc
                if resume_transition_linearized:
                    raise ApplicationRpcDurabilityError(
                        "Resume transition response is indeterminate"
                    ) from exc
                return await self._complete_lifecycle_response(
                    request,
                    self._deadline_response(request, run_id=run_id),
                )
            resume_transition_linearized = bool(resumed)
            if latest is None:
                response = self._error_response(
                    request,
                    error="run_not_found",
                    run_id=run_id,
                )
            else:
                if not resume_transition_linearized:
                    try:
                        resume_transition_linearized = self._lifecycle_transition_committed(
                            request,
                        )
                    except BaseException as exc:
                        raise ApplicationRpcDurabilityError(
                            "Resume transition could not be reconciled"
                        ) from exc
                if (
                    not resumed
                    or latest.cancellation_requested
                    or latest.state != "running"
                    or latest.handoff_committed
                ):
                    response = self._error_response(
                        request,
                        error="run_not_active",
                        run_id=run_id,
                        state=latest.state,
                        action_sequence=latest.action_sequence,
                        event_sequence=latest.latest_event_sequence,
                    )
                else:
                    response = self._status_response(
                        request,
                        latest,
                        active.control,
                        job_url=latest.job_url,
                    )
            return await self._complete_lifecycle_response(
                request,
                response,
                transition_linearized=resume_transition_linearized,
            )
        if request.operation == "run.cancel":
            info, replay = await self._reserve_lifecycle(request)
            if replay is not None:
                if info is None or info.state == "completed":
                    return replay
            if info is None:
                raise ApplicationRpcDurabilityError(
                    "Cancel lifecycle reservation is incomplete"
                )
            if self._remaining_for(request) <= 0:
                return await self._complete_lifecycle_response(
                    request,
                    self._deadline_response(request, run_id=run_id),
                )
            try:
                latest_status = await self._await_lifecycle_work(
                    request,
                    lambda: self._read_status_after_reservation(run_id),
                )
            except _LifecycleDeadlineExceeded:
                return await self._complete_lifecycle_response(
                    request,
                    self._deadline_response(request, run_id=run_id),
                )
            if latest_status is None:
                response = self._error_response(
                    request,
                    error="run_not_found",
                    run_id=run_id,
                )
                return await self._complete_lifecycle_response(request, response)
            status = latest_status
            if status.state in {"failed", "review_ready"} or status.handoff_committed:
                response = self._error_response(
                    request,
                    error="run_not_active",
                    run_id=run_id,
                    state=status.state,
                    action_sequence=status.action_sequence,
                    event_sequence=status.latest_event_sequence,
                )
                return await self._complete_lifecycle_response(request, response)
            cancellation_was_requested = status.cancellation_requested

            async def request_cancel_without_active() -> bool:
                connection = self._connection()
                try:
                    return _db.request_rpc_cancellation(
                        connection,
                        run_id=run_id,
                        coordinator_id=self.config._coordinator_id,
                        deadline_unix_ms=request.deadline_unix_ms,
                    )
                except _db.RpcDeadlineExceeded:
                    return False
                finally:
                    connection.close()

            async def request_cancel_active() -> bool:
                if active is None:
                    return False
                cancel = active.control.cancel
                try:
                    signature = inspect.signature(cancel)
                except (TypeError, ValueError):
                    value = cancel()
                else:
                    parameters = tuple(signature.parameters.values())
                    if (
                        "deadline_unix_ms" in signature.parameters
                        or any(
                            parameter.kind is inspect.Parameter.VAR_KEYWORD
                            for parameter in parameters
                        )
                    ):
                        value = cancel(deadline_unix_ms=request.deadline_unix_ms)
                    else:
                        value = cancel()
                if inspect.isawaitable(value):
                    value = await value
                return bool(value)

            try:
                cancellation_accepted = await self._invoke_lifecycle_transition(
                    request,
                    (
                        request_cancel_active
                        if active is not None
                        else request_cancel_without_active
                    ),
                    cancellation_was_requested=cancellation_was_requested,
                    result_indicates_transition=lambda result: (
                        bool(result) and not cancellation_was_requested
                    ),
                )
            except _LifecycleDeadlineExceeded:
                return await self._complete_lifecycle_response(
                    request,
                    self._deadline_response(request, run_id=run_id),
                )
            except ApplicationRpcDurabilityError:
                raise
            except BaseException as exc:
                raise ApplicationRpcDurabilityError(
                    "Cancellation transition is indeterminate"
                ) from exc
            transition_linearized = (
                bool(cancellation_accepted) and not cancellation_was_requested
            )
            try:
                latest = await self._await_lifecycle_work(
                    request,
                    lambda: self._read_status_after_reservation(run_id),
                )
            except _LifecycleDeadlineExceeded as exc:
                if not transition_linearized:
                    try:
                        transition_linearized = self._lifecycle_transition_committed(
                            request,
                            cancellation_was_requested=cancellation_was_requested,
                        )
                    except BaseException as probe_exc:
                        raise ApplicationRpcDurabilityError(
                            "Cancellation transition response could not be reconciled"
                        ) from probe_exc
                if transition_linearized:
                    raise ApplicationRpcDurabilityError(
                        "Cancellation transition response is indeterminate"
                    ) from exc
                return await self._complete_lifecycle_response(
                    request,
                    self._deadline_response(request, run_id=run_id),
                )
            if not transition_linearized and not cancellation_was_requested:
                try:
                    transition_linearized = self._lifecycle_transition_committed(
                        request,
                        cancellation_was_requested=cancellation_was_requested,
                    )
                except BaseException as exc:
                    raise ApplicationRpcDurabilityError(
                        "Cancellation transition could not be reconciled"
                    ) from exc
            if not cancellation_accepted:
                response = self._error_response(
                    request,
                    error="run_not_active",
                    run_id=run_id,
                    state=latest.state if latest is not None else status.state,
                    action_sequence=(
                        latest.action_sequence
                        if latest is not None
                        else status.action_sequence
                    ),
                    event_sequence=(
                        latest.latest_event_sequence
                        if latest is not None
                        else status.latest_event_sequence
                    ),
                )
                return await self._complete_lifecycle_response(request, response)
            if latest is None:
                response = self._error_response(request, error="run_not_found", run_id=run_id)
            else:
                response = self._status_response(
                    request,
                    latest,
                    active.control if active else None,
                    job_url=latest.job_url,
                )
            return await self._complete_lifecycle_response(
                request,
                response,
                transition_linearized=transition_linearized,
            )
        return build_rejected_application_response(request.to_mapping(), error="unsupported_operation")

    async def replay_progress(self, run_id: int, after_sequence: int = 0) -> tuple[Mapping[str, object], ...]:
        if type(run_id) is not int or run_id <= 0:
            raise TypeError("run_id must be a positive integer")
        if type(after_sequence) is not int or after_sequence < 0:
            raise TypeError("after_sequence must be a non-negative integer")
        if not self._started:
            await self.start()
        connection = self._connection()
        try:
            status = _db.get_rpc_run_status(connection, run_id)
            if status is None or not _db.rpc_run_owner_matches(
                connection,
                run_id=run_id,
                coordinator_id=self.config._coordinator_id,
            ):
                return ()
            events = _db.replay_rpc_events(connection, run_id, after_sequence=after_sequence)
        finally:
            connection.close()
        output: list[Mapping[str, object]] = []
        for event in events:
            public = public_rpc_event(event)
            output.append(public)
            if self.config._event_callback is not None:
                try:
                    value = self.config._event_callback(public)
                    if inspect.isawaitable(value):
                        await value
                except Exception:
                    pass
        return tuple(output)

    async def close(self) -> None:
        async with self._close_lock:
            outstanding = bool(self._runs) or any(
                not task.done()
                for task in (
                    *self._inflight_starts,
                    *self._inflight_lifecycles,
                    *self._inflight_lifecycle_recovery,
                    *self._late_launch_cleanups,
                )
            )
            if self._closed and not outstanding:
                if self._runtime_lease_held:
                    _release_runtime_lease(self._runtime_lease_key)
                    self._runtime_lease_held = False
                return
            # Mark shutdown admission closed before touching the dispatch lock.
            # A run.start may hold that lock while launch is draining; waiting
            # behind it would prevent cancellation and make close unbounded.
            self._closed = True

            async def drain(
                tasks: tuple[asyncio.Task[Any], ...],
                *,
                timeout: float,
                cancel_on_timeout: bool,
            ) -> bool:
                if not tasks:
                    return True
                try:
                    _done, pending = await asyncio.wait(
                        tasks,
                        timeout=timeout,
                    )
                except asyncio.CancelledError:
                    return False
                if not pending:
                    return True
                if not cancel_on_timeout:
                    return False
                for task in pending:
                    if not task.done():
                        task.cancel()
                try:
                    _done, pending = await asyncio.wait(
                        tasks,
                        timeout=_CANCELLED_TASK_DRAIN_SECONDS,
                    )
                except asyncio.CancelledError:
                    return False
                return not pending

            cleanup_failed = False
            inflight = tuple(self._inflight_starts)
            for task in inflight:
                if not task.done():
                    task.cancel()
            if not await drain(
                inflight,
                timeout=_CLEANUP_DRAIN_SECONDS,
                cancel_on_timeout=True,
            ):
                cleanup_failed = True

            runs = tuple(self._runs.values())
            for active in runs:
                try:
                    await asyncio.wait_for(
                        active.control.cancel(),
                        timeout=_CLEANUP_DRAIN_SECONDS,
                    )
                except (Exception, asyncio.CancelledError):
                    pass
            workflow_tasks = tuple(
                active.workflow_task
                for active in runs
                if active.workflow_task is not None
            )
            await drain(
                workflow_tasks,
                timeout=_CLEANUP_DRAIN_SECONDS,
                cancel_on_timeout=True,
            )
            if not await drain(
                tuple(self._inflight_lifecycles),
                timeout=_CLEANUP_DRAIN_SECONDS,
                cancel_on_timeout=True,
            ):
                cleanup_failed = True
            if not await drain(
                tuple(self._inflight_lifecycle_recovery),
                timeout=_CLEANUP_DRAIN_SECONDS,
                cancel_on_timeout=True,
            ):
                cleanup_failed = True

            for active in runs:
                workflow_task = active.workflow_task
                workflow_done = (
                    workflow_task is None
                    or workflow_task.done()
                )
                process_closed = await self._close_process(
                    active.process,
                    timeout=_CLEANUP_DRAIN_SECONDS,
                )
                if not workflow_done or not process_closed:
                    aborted = self._abort_active_for_shutdown(active)
                    if active.control.handoff_committed:
                        connection = self._connection()
                        try:
                            self._reconcile_handoff_failure(
                                connection,
                                active.run_id,
                            )
                        except Exception:
                            pass
                        finally:
                            connection.close()
                    if aborted and workflow_task is not None:
                        if not workflow_task.done():
                            workflow_task.cancel()
                        workflow_done = await drain(
                            (workflow_task,),
                            timeout=_CANCELLED_TASK_DRAIN_SECONDS,
                            cancel_on_timeout=True,
                        )
                        if not process_closed:
                            process_closed = await self._close_process(
                                active.process,
                                timeout=_CANCELLED_TASK_DRAIN_SECONDS,
                            )
                if workflow_done and process_closed:
                    self._runs.pop(active.run_id, None)
                else:
                    cleanup_failed = True

            if not await drain(
                tuple(self._late_launch_cleanups),
                timeout=_CLEANUP_DRAIN_SECONDS,
                cancel_on_timeout=False,
            ):
                cleanup_failed = True
            if cleanup_failed:
                raise ApplicationRpcDurabilityError(
                    "RPC shutdown could not prove process absence"
                )
            self._runs.clear()
            if self._runtime_lease_held:
                _release_runtime_lease(self._runtime_lease_key)
                self._runtime_lease_held = False


__all__ = (
    "ApplicationRpcServiceConfig",
    "ApplicationRpcCoordinator",
    "RpcApplicationControl",
    "ApplicationRpcServiceError",
    "ApplicationRpcDurabilityError",
    "resolve_application_rpc_identity",
    "public_rpc_event",
)
