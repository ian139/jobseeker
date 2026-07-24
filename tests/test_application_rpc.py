from __future__ import annotations

import asyncio
from dataclasses import replace
import functools
import hashlib
import inspect
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping
from uuid import uuid4

import pytest

from jobs_assistant import application_rpc as rpc_module
from jobs_assistant import db
from jobs_assistant.application import DEFAULT_APPLICATION_PROFILE_SHA256
from jobs_assistant.application_rpc import (
    ApplicationRpcCoordinator,
    ApplicationRpcDurabilityError,
    ApplicationRpcServiceConfig,
    ApplicationRpcServiceError,
    RpcApplicationControl,
    public_rpc_event,
    resolve_application_rpc_identity,
)
from jobs_assistant.application_rpc_contracts import (
    APPLICATION_RPC_PROTOCOL_VERSION,
    HostToolContext,
    parse_application_request,
    build_application_response,
    parse_application_response,
    parse_host_tool_call,
)
from jobs_assistant.artifacts import ArtifactRoot
from jobs_assistant.omp_rpc import (
    OmpHostDurabilityError,
    OmpHostInvocation,
    OmpRpcProcess,
)

def pytest_pyfunc_call(pyfuncitem):
    testfunction = pyfuncitem.obj
    if inspect.iscoroutinefunction(testfunction):
        kwargs = {
            name: value
            for name, value in pyfuncitem.funcargs.items()
            if name in inspect.signature(testfunction).parameters
        }
        asyncio.run(testfunction(**kwargs))
        return True
    return None


def _async_test(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return wrapper


JOB_URL = "https://boards.greenhouse.io/acme/jobs/123"
OBSERVATION_SHA = hashlib.sha256(b"observation").hexdigest()


def _observation() -> dict[str, object]:
    return {
        "observation_sha256": OBSERVATION_SHA,
        "observation_sequence": 1,
        "observed_at": "2025-01-01T00:00:00Z",
        "url": JOB_URL,
        "ats": "greenhouse",
        "page_type": "application",
        "frame_id": "frame-main",
        "fields": [
            {
                "element_id": "field-name",
                "frame_id": "frame-main",
                "label": "Name",
                "kind": "text",
                "required": True,
                "disabled": False,
                "readonly": False,
                "has_value": False,
                "multiple": False,
                "options": [],
                "accept": [],
                "safety_class": "safe",
            }
        ],
        "controls": [],
        "validation_errors": [],
        "progress": {"step_index": None, "step_count": None},
        "blocker_codes": [],
    }


def _request(operation: str, *, run_id: int | None = None, payload: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "protocol_version": APPLICATION_RPC_PROTOCOL_VERSION,
        "request_id": str(uuid4()),
        "operation": operation,
        "deadline_unix_ms": int(time.time() * 1000) + 60_000,
        "run_id": run_id,
        "payload": payload or {},
    }


class _FakeInvocation:
    def __init__(self, proposal: object) -> None:
        self.proposal = proposal
        self.dispatched = False

    def mark_dispatched(self) -> bool:
        if self.dispatched:
            return False
        self.dispatched = True
        return True


class _FakeOmp:
    def __init__(self, callback, *, mode: str) -> None:
        self.callback = callback
        self.mode = mode
        self.calls = 0
        self.closed = False
        self.verified = True
        self.poisoned = False
        self.pid = os.getpid()
        self.session_identity_sha256 = hashlib.sha256(b"fake-session").hexdigest()
        self.action_responses: list[dict[str, object]] = []

    async def prompt(self, _message: str, context: HostToolContext, *, timeout: float) -> object:
        assert timeout > 0
        self.calls += 1
        observe_frame = {
            "type": "host_tool_call",
            "id": f"observe-{self.calls}",
            "toolCallId": f"observe-call-{self.calls}",
            "toolName": "browser.observe",
            "arguments": {},
        }
        observe = parse_host_tool_call(observe_frame, context)
        await self.callback(_FakeInvocation(observe))
        if self.mode == "park" and self.calls == 1:
            return SimpleNamespace(agent_invoked=True)
        if self.mode == "park" and self.calls > 1:
            return SimpleNamespace(agent_invoked=True)
        if self.mode == "handoff":
            operation = "browser.prepare_human_handoff"
            arguments = {"observation_sha256": OBSERVATION_SHA}
        else:
            operation = "browser.fill_field"
            arguments = {
                "observation_sha256": OBSERVATION_SHA,
                "element_id": "field-name",
                "value": "answer",
                "confidence": 0.9,
                "reason": "safe deterministic test",
            }
        frame = {
            "type": "host_tool_call",
            "id": f"action-{self.calls}",
            "toolCallId": f"action-call-{self.calls}",
            "toolName": operation,
            "arguments": arguments,
        }
        proposal = parse_host_tool_call(frame, context)
        result = await self.callback(_FakeInvocation(proposal))
        assert isinstance(result, dict)
        self.action_responses.append(result)
        return SimpleNamespace(agent_invoked=True)

    async def cancel_prompt(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True



class _DelayedAgentEndOmp:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.calls = 0
        self.active = False
        self.closed = False
        self.verified = True
        self.poisoned = False
        self.pid = os.getpid()
        self.session_identity_sha256 = hashlib.sha256(b"delayed-session").hexdigest()
        self.release_first = asyncio.Event()
        self.started: list[int] = []
        self.finished: list[int] = []

    async def prompt(self, _message: str, context: HostToolContext, *, timeout: float) -> object:
        assert timeout > 0
        if self.active:
            raise RuntimeError("prompt_busy")
        self.active = True
        self.calls += 1
        call_number = self.calls
        self.started.append(call_number)
        try:
            observe = parse_host_tool_call(
                {
                    "type": "host_tool_call",
                    "id": f"observe-{call_number}",
                    "toolCallId": f"observe-call-{call_number}",
                    "toolName": "browser.observe",
                    "arguments": {},
                },
                context,
            )
            await self.callback(_FakeInvocation(observe))
            proposal = parse_host_tool_call(
                {
                    "type": "host_tool_call",
                    "id": f"action-{call_number}",
                    "toolCallId": f"action-call-{call_number}",
                    "toolName": "browser.fill_field",
                    "arguments": {
                        "observation_sha256": OBSERVATION_SHA,
                        "element_id": "field-name",
                        "value": "answer",
                        "confidence": 0.9,
                        "reason": "delayed agent end",
                    },
                },
                context,
            )
            result = await self.callback(_FakeInvocation(proposal))
            assert isinstance(result, dict)
            if call_number == 1:
                await self.release_first.wait()
            return SimpleNamespace(agent_invoked=True)
        finally:
            self.active = False
            self.finished.append(call_number)

    async def cancel_prompt(self) -> None:
        self.release_first.set()

    async def close(self) -> None:
        self.closed = True
        self.release_first.set()

def _prepare_db(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    db_path = tmp_path / "jobs.sqlite3"
    artifact_root = tmp_path / "application-runs"
    artifact_root.mkdir(mode=0o700)
    resume = tmp_path / "resume.txt"
    resume.write_text("Ada Lovelace\nada@example.test\n", encoding="utf-8")
    resume.chmod(0o600)
    connection = db.connect(db_path)
    try:
        db.init_db(connection)
        with ArtifactRoot.open(artifact_root, cwd=Path.cwd()) as root:
            db.initialize_database(connection, migration_artifact_root=root)
        now = db.utc_now()
        connection.execute(
            """
            INSERT INTO jobs
                (source, source_job_id, canonical_url, title, company, location,
                 remote, posted_at, discovered_at, description, status,
                 raw_json, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', '{}', ?, ?)
            """ ,
            ("test", "123", JOB_URL, "Engineer", "Acme", "Remote", 1,
             now, now, "Untrusted job text", now, now),
        )
        connection.commit()
    finally:
        connection.close()
    return db_path, artifact_root, resume, now

def _config(
    db_path: Path,
    artifact_root: Path,
    resume: Path,
    process_factory,
    workflow,
    *,
    event_callback=None,
    coordinator_id: str = "test-coordinator",
) -> ApplicationRpcServiceConfig:
    return ApplicationRpcServiceConfig(
        db_path=db_path,
        artifact_root=artifact_root,
        resume_file=resume,
        omp_process_factory=process_factory,
        workflow=workflow,
        coordinator_id=coordinator_id,
        event_callback=event_callback,
    )
async def _start_parked_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ApplicationRpcCoordinator, int, _FakeOmp, asyncio.Event, dict[str, object]]:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    monkeypatch.setattr(db, "update_rpc_run_process", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        db,
        "mark_rpc_omp_spawn_attempted",
        lambda *_args, **_kwargs: True,
    )
    process_box: list[_FakeOmp] = []
    stop = asyncio.Event()

    async def workflow(connection, *, claim_provider, control, **_kwargs):
        claim = claim_provider(connection)
        assert claim is not None
        await control.on_claimed(claim.run_id, int(claim.job["id"]), "greenhouse", JOB_URL)
        await control.record_progress(claim.run_id, "page_observed", "observed", 1, OBSERVATION_SHA)
        await control.propose_action(
            claim.run_id,
            1,
            OBSERVATION_SHA,
            _observation(),
            None,
            {"status": "ready"},
        )
        await stop.wait()

    def process_factory(_config, host_tool_callback):
        process = _FakeOmp(host_tool_callback, mode="park")
        process_box.append(process)
        return process

    config = _config(db_path, artifact_root, resume, process_factory, workflow)
    coordinator = ApplicationRpcCoordinator(config)
    identity = resolve_application_rpc_identity(config)
    request = _request(
        "run.start",
        payload={
            "goal": "prepare_application_draft",
            "job_url": JOB_URL,
            "candidate_profile_id": identity["candidate_profile_id"],
            "configured_resume_id": identity["configured_resume_id"],
            "headed": True,
        },
    )
    response = await coordinator.handle(request)
    assert response["ok"] is True
    await asyncio.sleep(0.05)
    assert process_box
    return coordinator, int(response["run_id"]), process_box[0], stop, request

def _new_direct_control(
    tmp_path: Path,
) -> tuple[Path, Path, RpcApplicationControl, int]:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    config = _config(
        db_path,
        artifact_root,
        resume,
        lambda *_args, **_kwargs: None,
        lambda *_args, **_kwargs: None,
    )
    identity = resolve_application_rpc_identity(config)
    parent = parse_application_request(
        _request(
            "run.start",
            payload={
                "goal": "prepare_application_draft",
                "job_url": JOB_URL,
                "candidate_profile_id": identity["candidate_profile_id"],
                "configured_resume_id": identity["configured_resume_id"],
                "headed": True,
            },
        )
    )
    connection = db.connect(db_path)
    try:
        claim = db.claim_application_job_for_rpc(
            connection,
            owner="rpc-owner",
            request=parent,
            coordinator_id=config.coordinator_id,
        )
    finally:
        connection.close()
    assert claim.run_id is not None
    control = RpcApplicationControl(
        db_path=db_path,
        artifact_root=artifact_root,
        coordinator_id=config.coordinator_id,
        connection_factory=lambda: db.connect(db_path),
        run_id=claim.run_id,
        parent_request=parent,
    )
    return db_path, artifact_root, control, int(claim.run_id)


@_async_test
async def test_committed_start_cancel_before_registration_fails_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    monkeypatch.setattr(db, "update_rpc_run_process", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        db,
        "mark_rpc_omp_spawn_attempted",
        lambda *_args, **_kwargs: True,
    )
    process_box: list[_FakeOmp] = []

    async def workflow(*_args, **_kwargs):
        raise AssertionError("workflow must not start before start response returns")

    def process_factory(_config, host_tool_callback):
        process = _FakeOmp(host_tool_callback, mode="park")
        process_box.append(process)
        return process

    coordinator = ApplicationRpcCoordinator(
        _config(db_path, artifact_root, resume, process_factory, workflow)
    )
    identity = resolve_application_rpc_identity(coordinator.config)
    request = _request(
        "run.start",
        payload={
            "goal": "prepare_application_draft",
            "job_url": JOB_URL,
            "candidate_profile_id": identity["candidate_profile_id"],
            "configured_resume_id": identity["configured_resume_id"],
            "headed": True,
        },
    )
    original_complete = coordinator._complete_lifecycle

    async def commit_then_cancel(request_obj, response):
        committed = await original_complete(request_obj, response)
        if request_obj.operation == "run.start":
            raise asyncio.CancelledError
        return committed

    monkeypatch.setattr(coordinator, "_complete_lifecycle", commit_then_cancel)
    with pytest.raises(asyncio.CancelledError):
        await coordinator.handle(request)
    assert process_box and process_box[0].closed is True
    assert coordinator._runs == {}
    replay = await coordinator.handle(dict(request))
    assert replay["ok"] is True
    connection = db.connect(db_path)
    try:
        row = connection.execute(
            """
            SELECT a.status, a.outcome, j.status AS job_status,
                   r.state
            FROM application_runs AS a
            JOIN jobs AS j ON j.id=a.job_id
            JOIN application_rpc_runs AS r ON r.run_id=a.id
            """
        ).fetchone()
        assert tuple(row) == ("failed", "retry", "queued", "failed")
        event_count = connection.execute(
            """
            SELECT COUNT(*) FROM application_progress_events
            WHERE run_id=? AND event_type='run_failed'
            """,
            (replay["run_id"],),
        ).fetchone()[0]
        assert event_count == 1
    finally:
        connection.close()
    await coordinator.close()


@_async_test
async def test_dispatched_child_failure_finalizes_application_and_rpc_atomically(
    tmp_path: Path,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    config = _config(
        db_path,
        artifact_root,
        resume,
        lambda *_args, **_kwargs: None,
        lambda *_args, **_kwargs: None,
    )
    identity = resolve_application_rpc_identity(config)
    parent = parse_application_request(
        _request(
            "run.start",
            payload={
                "goal": "prepare_application_draft",
                "job_url": JOB_URL,
                "candidate_profile_id": identity["candidate_profile_id"],
                "configured_resume_id": identity["configured_resume_id"],
                "headed": True,
            },
        )
    )
    connection = db.connect(db_path)
    try:
        claim = db.claim_application_job_for_rpc(
            connection,
            owner="rpc-owner",
            request=parent,
            coordinator_id=config.coordinator_id,
        )
        assert claim.run_id is not None
    finally:
        connection.close()
    proposal = parse_host_tool_call(
        {
            "type": "host_tool_call",
            "id": "dispatched-failure",
            "toolCallId": "dispatched-failure-call",
            "toolName": "browser.observe",
            "arguments": {},
        },
        HostToolContext(
            APPLICATION_RPC_PROTOCOL_VERSION,
            1,
            parent.request_id,
            parent.deadline_unix_ms,
        ),
    )
    connection = db.connect(db_path)
    try:
        db.reserve_rpc_request(
            connection,
            request=proposal.request,
            parent_request_id=parent.request_id,
        )
    finally:
        connection.close()
    control = RpcApplicationControl(
        db_path=db_path,
        artifact_root=artifact_root,
        coordinator_id=config.coordinator_id,
        connection_factory=lambda: db.connect(db_path),
        run_id=claim.run_id,
        parent_request=parent,
    )
    future = asyncio.get_running_loop().create_future()
    control._pending = SimpleNamespace(
        proposal=proposal,
        workflow_sequence=1,
        future=future,
        dispatched=True,
    )
    control._child_flights[proposal.request.request_id] = SimpleNamespace(
        semantic_sha256=proposal.request.semantic_sha256,
        future=future,
    )
    assert await control.finalize_failure(
        claim.run_id,
        status="failed",
        reason_code="browser_error",
        observation_summary={"error_code": "browser_error"},
        plan_summary={},
        artifact_dir=None,
        pending_proposal=proposal,
        error_code="workflow_failed",
    )
    response = future.result()
    assert response["ok"] is False
    assert control._pending is None
    connection = db.connect(db_path)
    try:
        app_state = connection.execute(
            """
            SELECT a.status, a.outcome, j.status AS job_status,
                   r.state
            FROM application_runs AS a
            JOIN jobs AS j ON j.id=a.job_id
            JOIN application_rpc_runs AS r ON r.run_id=a.id
            WHERE a.id=?
            """,
            (claim.run_id,),
        ).fetchone()
        assert tuple(app_state) == ("failed", "retry", "queued", "failed")
        child_info = db.get_rpc_request(connection, proposal.request.request_id)
        assert child_info is not None and child_info.state == "completed"
        assert json.loads(child_info.response_json or "{}")["ok"] is False
        assert connection.execute(
            """
            SELECT COUNT(*) FROM application_progress_events
            WHERE run_id=? AND event_type='run_failed'
            """,
            (claim.run_id,),
        ).fetchone()[0] == 1
    finally:
        connection.close()


@_async_test
async def test_control_fail_finalizes_live_run_without_unhandled_task(
    tmp_path: Path,
) -> None:
    db_path, _artifact_root, control, run_id = _new_direct_control(tmp_path)

    await control.fail()

    assert control.browser_state == "failed"
    assert control.coordinator_state == "terminal"
    connection = db.connect(db_path)
    try:
        row = connection.execute(
            """
            SELECT a.status, a.outcome, j.status AS job_status, r.state
            FROM application_runs AS a
            JOIN jobs AS j ON j.id=a.job_id
            JOIN application_rpc_runs AS r ON r.run_id=a.id
            WHERE a.id=?
            """,
            (run_id,),
        ).fetchone()
        assert tuple(row) == ("failed", "retry", "queued", "failed")
        assert connection.execute(
            """
            SELECT COUNT(*) FROM application_progress_events
            WHERE run_id=? AND event_type='run_failed'
            """,
            (run_id,),
        ).fetchone()[0] == 1
    finally:
        connection.close()


@_async_test
async def test_control_fail_is_idempotent_for_prefailed_run(
    tmp_path: Path,
) -> None:
    db_path, _artifact_root, control, run_id = _new_direct_control(tmp_path)

    await control.fail("browser_error")
    await control.fail("workflow_failed")

    assert control.browser_state == "failed"
    assert control.coordinator_state == "terminal"
    connection = db.connect(db_path)
    try:
        status = db.get_rpc_run_status(connection, run_id)
        assert status is not None and status.state == "failed"
        assert connection.execute(
            """
            SELECT COUNT(*) FROM application_progress_events
            WHERE run_id=? AND event_type='run_failed'
            """,
            (run_id,),
        ).fetchone()[0] == 1
    finally:
        connection.close()


@_async_test
async def test_control_fail_status_read_failure_is_durable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, _artifact_root, control, run_id = _new_direct_control(tmp_path)
    connections: list[SimpleNamespace] = []

    def factory() -> SimpleNamespace:
        connection = SimpleNamespace(closed=False)

        def close() -> None:
            connection.closed = True

        connection.close = close
        connections.append(connection)
        return connection

    def fail_status(*_args, **_kwargs):
        raise RuntimeError("status unavailable")

    control._connection_factory = factory
    monkeypatch.setattr(db, "get_rpc_run_status", fail_status)
    with pytest.raises(ApplicationRpcDurabilityError, match="status could not be read"):
        await control.fail()

    assert connections and connections[0].closed is True
    connection = db.connect(db_path)
    try:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM application_progress_events
            WHERE run_id=? AND event_type='run_failed'
            """,
            (run_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()



def test_config_is_immutable_private_and_identity_uses_retained_hashes(tmp_path: Path) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    config = ApplicationRpcServiceConfig(db_path=db_path, artifact_root=artifact_root, resume_file=resume)
    identity = resolve_application_rpc_identity(config)
    assert identity["configured_resume_id"] == hashlib.sha256(resume.read_bytes()).hexdigest()
    assert identity["candidate_profile_id"] == DEFAULT_APPLICATION_PROFILE_SHA256
    assert str(tmp_path) not in repr(config)
    with pytest.raises((AttributeError, FrozenInstanceError)):
        config._db_path = "changed"  # type: ignore[misc]

def test_default_coordinator_identity_is_stable_and_instance_scoped(tmp_path: Path) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    first = ApplicationRpcServiceConfig(
        db_path=db_path,
        artifact_root=artifact_root,
        resume_file=resume,
    )
    second = ApplicationRpcServiceConfig(
        db_path=db_path,
        artifact_root=artifact_root,
        resume_file=resume,
    )
    assert first.coordinator_id == second.coordinator_id
    assert first.coordinator_id.startswith("rpc-instance-")
    assert str(tmp_path) not in first.coordinator_id
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = ApplicationRpcServiceConfig(
        db_path=other_root / "jobs.sqlite3",
        artifact_root=other_root / "application-runs",
        resume_file=resume,
    )
    assert other.coordinator_id != first.coordinator_id
    explicit = ApplicationRpcServiceConfig(
        db_path=db_path,
        artifact_root=artifact_root,
        resume_file=resume,
        coordinator_id="explicit-owner",
    )
    assert explicit.coordinator_id == "explicit-owner"

@_async_test
async def test_default_coordinator_identity_restart_replays_completed_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    monkeypatch.setattr(db, "update_rpc_run_process", lambda *_args, **_kwargs: True)

    async def workflow(connection, *, claim_provider, control, **_kwargs):
        claim = claim_provider(connection)
        assert claim is not None
        await control.on_claimed(claim.run_id, int(claim.job["id"]), "greenhouse", JOB_URL)
        await control.record_progress(claim.run_id, "page_observed", "observed", 1, OBSERVATION_SHA)
        await control.propose_action(
            claim.run_id, 1, OBSERVATION_SHA, _observation(), None, {"status": "ready"}
        )

    def process_factory(_config, host_tool_callback):
        return _FakeOmp(host_tool_callback, mode="park")

    config = ApplicationRpcServiceConfig(
        db_path=db_path,
        artifact_root=artifact_root,
        resume_file=resume,
        omp_process_factory=process_factory,
        workflow=workflow,
    )
    coordinator = ApplicationRpcCoordinator(config)
    identity = resolve_application_rpc_identity(config)
    request = _request(
        "run.start",
        payload={
            "goal": "prepare_application_draft",
            "job_url": JOB_URL,
            "candidate_profile_id": identity["candidate_profile_id"],
            "configured_resume_id": identity["configured_resume_id"],
            "headed": True,
        },
    )
    first = await coordinator.handle(request)
    await asyncio.sleep(0.05)
    await coordinator.close()
    restarted_config = ApplicationRpcServiceConfig(
        db_path=db_path,
        artifact_root=artifact_root,
        resume_file=resume,
    )
    assert restarted_config.coordinator_id == config.coordinator_id
    restarted = ApplicationRpcCoordinator(restarted_config)
    try:
        assert await restarted.handle(dict(request)) == first
    finally:
        await restarted.close()

@_async_test
async def test_runtime_lease_denies_same_instance_until_owner_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    owner = ApplicationRpcCoordinator(
        ApplicationRpcServiceConfig(
            db_path=db_path,
            artifact_root=artifact_root,
            resume_file=resume,
            coordinator_id="owner-a",
        )
    )
    foreign = ApplicationRpcCoordinator(
        ApplicationRpcServiceConfig(
            db_path=db_path,
            artifact_root=artifact_root,
            resume_file=resume,
            coordinator_id="owner-b",
        )
    )
    third = ApplicationRpcCoordinator(
        ApplicationRpcServiceConfig(
            db_path=db_path,
            artifact_root=artifact_root,
            resume_file=resume,
            coordinator_id="owner-c",
        )
    )
    await owner.start()
    initialize_calls = 0
    original_initialize = db.initialize_database

    def count_initialize(*args, **kwargs):
        nonlocal initialize_calls
        initialize_calls += 1
        return original_initialize(*args, **kwargs)

    monkeypatch.setattr(db, "initialize_database", count_initialize)
    try:
        with pytest.raises(ApplicationRpcServiceError, match="unavailable"):
            await foreign.start()
        assert initialize_calls == 0
        await foreign.close()
        with pytest.raises(ApplicationRpcServiceError, match="unavailable"):
            await third.start()
        assert initialize_calls == 0
        await owner.close()
        await third.start()
    finally:
        await foreign.close()
        await third.close()
        await owner.close()


@_async_test
async def test_foreign_terminal_start_replay_is_denied_before_response_read(
    tmp_path: Path,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    owner_config = _config(
        db_path,
        artifact_root,
        resume,
        lambda *_args, **_kwargs: None,
        lambda *_args, **_kwargs: None,
        coordinator_id="owner-a",
    )
    foreign_config = _config(
        db_path,
        artifact_root,
        resume,
        lambda *_args, **_kwargs: None,
        lambda *_args, **_kwargs: None,
        coordinator_id="owner-b",
    )
    identity = resolve_application_rpc_identity(owner_config)
    raw = _request(
        "run.start",
        payload={
            "goal": "prepare_application_draft",
            "job_url": JOB_URL,
            "candidate_profile_id": identity["candidate_profile_id"],
            "configured_resume_id": identity["configured_resume_id"],
            "headed": True,
        },
    )
    request = parse_application_request(raw)
    connection = db.connect(db_path)
    try:
        claim = db.claim_application_job_for_rpc(
            connection,
            owner="owner",
            request=request,
            coordinator_id=owner_config.coordinator_id,
        )
        assert claim.run_id is not None
        db.commit_rpc_run_transition(
            connection,
            db.RpcRunTransition(
                run_id=claim.run_id,
                coordinator_id=owner_config.coordinator_id,
                request_id=request.request_id,
                action_sequence=1,
                event_type="run_failed",
                summary_code="failed",
                state="failed",
            ),
        )
        db.complete_rpc_request(
            connection,
            request=request,
            response=build_application_response(
                request,
                ok=False,
                state="failed",
                action_sequence=1,
                event_sequence=1,
                error="workflow_failed",
                run_id=claim.run_id,
            ),
            coordinator_id=owner_config.coordinator_id,
        )
        before = db.get_rpc_request(connection, request.request_id)
    finally:
        connection.close()
    foreign = ApplicationRpcCoordinator(foreign_config)
    foreign._identity = resolve_application_rpc_identity(foreign_config)
    try:
        response = await foreign._handle_start(request)
        assert response["ok"] is False
        assert response["error"]["code"] == "run_not_owned"  # type: ignore[index]
        connection = db.connect(db_path)
        try:
            after = db.get_rpc_request(connection, request.request_id)
        finally:
            connection.close()
        assert after == before
    finally:
        await foreign.close()

@_async_test
async def test_run_bound_start_owner_probe_failure_is_fail_closed_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _run_id, _process, stop, request = await _start_parked_run(
        tmp_path,
        monkeypatch,
    )
    connections: list[object] = []

    class TrackingConnection:
        def __init__(self, connection) -> None:
            self._connection = connection
            self.closed = False

        def close(self) -> None:
            self.closed = True
            self._connection.close()

        def __getattr__(self, name: str):
            return getattr(self._connection, name)

    def connection_factory() -> TrackingConnection:
        connection = TrackingConnection(db.connect(coordinator.config._db_path))
        connections.append(connection)
        return connection

    coordinator._connection = connection_factory  # type: ignore[method-assign]
    first = await coordinator.handle(dict(request))
    assert first["ok"] is True

    original_owner_matches = db.rpc_run_owner_matches
    failed = True

    def fail_owner_probe_once(*args, **kwargs):
        nonlocal failed
        if failed:
            failed = False
            raise RuntimeError("ownership unavailable")
        return original_owner_matches(*args, **kwargs)

    monkeypatch.setattr(db, "rpc_run_owner_matches", fail_owner_probe_once)
    try:
        with pytest.raises(ApplicationRpcDurabilityError, match="ownership probe"):
            await coordinator.handle(dict(request))
        assert connections and all(connection.closed for connection in connections)
        monkeypatch.setattr(db, "rpc_run_owner_matches", original_owner_matches)
        replay = await coordinator.handle(dict(request))
        assert replay == first
    finally:
        stop.set()
        await coordinator.close()


class FrozenInstanceError(Exception):
    pass


@_async_test
async def test_factory_typeerror_is_not_retried(tmp_path: Path) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    calls = {"connection": 0, "launch": 0, "process": 0, "workflow": 0}

    def connection_factory(path):
        calls["connection"] += 1
        raise TypeError("connection body")

    def launch_factory(run_id):
        calls["launch"] += 1
        raise TypeError("launch body")

    def process_factory(config, host_tool_callback):
        calls["process"] += 1
        raise TypeError("process body")

    def workflow(connection, **kwargs):
        calls["workflow"] += 1
        raise TypeError("workflow body")

    config = ApplicationRpcServiceConfig(
        db_path=db_path,
        artifact_root=artifact_root,
        resume_file=resume,
        connection_factory=connection_factory,
        omp_launch_config_factory=launch_factory,
        omp_process_factory=process_factory,
        workflow=workflow,
        coordinator_id="test-coordinator",
    )
    coordinator = ApplicationRpcCoordinator(config)
    control = RpcApplicationControl(
        db_path=db_path,
        artifact_root=artifact_root,
        coordinator_id="test-coordinator",
        connection_factory=connection_factory,
    )
    with pytest.raises(TypeError, match="connection body"):
        control._connection()
    with pytest.raises(TypeError, match="connection body"):
        coordinator._connection()
    with pytest.raises(TypeError, match="launch body"):
        await coordinator._call_launch_factory(launch_factory, 1)
    with pytest.raises(TypeError, match="process body"):
        coordinator._call_process_factory(process_factory, None, lambda *_: None)
    with pytest.raises(TypeError, match="workflow body"):
        coordinator._invoke_workflow(workflow, object(), {})
    assert calls == {"connection": 2, "launch": 1, "process": 1, "workflow": 1}

@_async_test
async def test_mismatched_source_ids_are_rejected_before_atomic_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    called = False

    def claim(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("claim must not run")

    monkeypatch.setattr(db, "claim_application_job_for_rpc", claim)
    config = _config(db_path, artifact_root, resume, lambda *_args, **_kwargs: None, lambda *_args, **_kwargs: None)
    coordinator = ApplicationRpcCoordinator(config)
    identity = resolve_application_rpc_identity(config)
    payload = {
        "goal": "prepare_application_draft",
        "job_url": JOB_URL,
        "candidate_profile_id": identity["candidate_profile_id"],
        "configured_resume_id": "0" * 64,
        "headed": True,
    }
    response = await coordinator.handle(_request("run.start", payload=payload))
    assert response["ok"] is False
    assert response["error"]["code"] == "request_conflict"  # type: ignore[index]
    assert not called
    await coordinator.close()


@_async_test
async def test_start_freezes_claim_and_public_url_then_replays_byte_semantically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    monkeypatch.setattr(db, "update_rpc_run_process", lambda *_args, **_kwargs: True)
    transitions: list[db.RpcRunTransition] = []
    original_transition = db.commit_rpc_run_transition

    def record_transition(connection, transition):
        transitions.append(transition)
        return original_transition(connection, transition)

    monkeypatch.setattr(db, "commit_rpc_run_transition", record_transition)
    seen: list[db.ApplicationClaim] = []

    async def workflow(connection, *, claim_provider, control, **_kwargs):
        claim = claim_provider(connection, owner="test")
        assert claim is not None
        seen.append(claim)
        await control.on_claimed(claim.run_id, int(claim.job["id"]), "greenhouse", JOB_URL)
        await control.record_progress(claim.run_id, "page_observed", "observed", 999, OBSERVATION_SHA)
        return [{"status": "manual", "reason_code": "no_deterministic_next_step"}]

    process_box: list[_FakeOmp] = []

    def process_factory(_config, host_tool_callback):
        process = _FakeOmp(host_tool_callback, mode="park")
        process_box.append(process)
        return process

    config = _config(db_path, artifact_root, resume, process_factory, workflow)
    coordinator = ApplicationRpcCoordinator(config)
    identity = resolve_application_rpc_identity(config)
    raw = _request(
        "run.start",
        payload={
            "goal": "prepare_application_draft",
            "job_url": JOB_URL,
            "candidate_profile_id": identity["candidate_profile_id"],
            "configured_resume_id": identity["configured_resume_id"],
            "headed": True,
        },
    )
    first = await coordinator.handle(raw)
    replay = await coordinator.handle(raw)
    assert first == replay
    assert first["ok"] is True
    assert first["result"]["job_url"] == JOB_URL  # type: ignore[index]
    await asyncio.sleep(0.05)
    assert len(seen) == 1
    assert seen[0].run_id == first["run_id"]
    status_request = _request("run.status", run_id=int(first["run_id"]))
    status = await coordinator.handle(status_request)
    assert status["result"]["job_url"] == JOB_URL  # type: ignore[index]
    events = await coordinator.replay_progress(int(first["run_id"]))
    assert [event["sequence"] for event in events] == sorted(event["sequence"] for event in events)
    assert all("Untrusted job text" not in repr(event) for event in events)
    assert {"run_started", "page_observed"} <= {item.event_type for item in transitions}
    assert all(isinstance(item, db.RpcRunTransition) for item in transitions)
    await coordinator.close()


@_async_test
async def test_completed_start_replay_precedes_refreshed_identity_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, _run_id, _process, stop, start_request = await _start_parked_run(
        tmp_path,
        monkeypatch,
    )
    first = await owner.handle(start_request)
    assert first["ok"] is True
    stop.set()
    await owner.close()

    resume_file = owner.config._resume_file
    resume_file.write_bytes(resume_file.read_bytes() + b"\nchanged\n")
    restarted = ApplicationRpcCoordinator(
        _config(
            owner.config._db_path,
            owner.config._artifact_root,
            resume_file,
            lambda *_args, **_kwargs: None,
            lambda *_args, **_kwargs: None,
        )
    )
    replay = await restarted.handle(dict(start_request))
    assert replay == first

    new_request = dict(start_request)
    new_request["request_id"] = str(uuid4())
    conflict = await restarted.handle(new_request)
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == "request_conflict"  # type: ignore[index]
    await restarted.close()


@_async_test
async def test_replay_requires_exact_coordinator_ownership_without_event_leakage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    owner_events: list[object] = []
    foreign_events: list[object] = []
    owner_config = _config(
        db_path,
        artifact_root,
        resume,
        lambda *_args, **_kwargs: None,
        lambda *_args, **_kwargs: None,
        coordinator_id="owner-coordinator",
        event_callback=owner_events.append,
    )
    foreign_config = _config(
        db_path,
        artifact_root,
        resume,
        lambda *_args, **_kwargs: None,
        lambda *_args, **_kwargs: None,
        coordinator_id="foreign-coordinator",
        event_callback=foreign_events.append,
    )
    owner = ApplicationRpcCoordinator(owner_config)
    foreign = ApplicationRpcCoordinator(foreign_config)
    identity = resolve_application_rpc_identity(owner_config)
    request = parse_application_request(
        _request(
            "run.start",
            payload={
                "goal": "prepare_application_draft",
                "job_url": JOB_URL,
                "candidate_profile_id": identity["candidate_profile_id"],
                "configured_resume_id": identity["configured_resume_id"],
                "headed": True,
            },
        )
    )
    connection = db.connect(db_path)
    try:
        claim = db.claim_application_job_for_rpc(
            connection,
            owner="owner",
            request=request,
            coordinator_id=owner_config.coordinator_id,
        )
        assert claim.outcome == "new" and claim.run_id is not None
        event = db.commit_rpc_run_transition(
            connection,
            db.RpcRunTransition(
                run_id=claim.run_id,
                coordinator_id=owner_config.coordinator_id,
                request_id=request.request_id,
                action_sequence=1,
                event_type="run_started",
                summary_code="started",
                state="failed",
            ),
        )
        assert event.sequence == 1
    finally:
        connection.close()

    replay_calls: list[int] = []
    original_replay = db.replay_rpc_events

    def record_replay(connection, run_id: int, *, after_sequence: int = 0):
        replay_calls.append(run_id)
        return original_replay(connection, run_id, after_sequence=after_sequence)

    monkeypatch.setattr(db, "replay_rpc_events", record_replay)
    try:
        run_id = int(claim.run_id)
        for invalid_after_sequence in (-1, True, "invalid"):
            with pytest.raises(TypeError, match="after_sequence must be a non-negative integer"):
                await foreign.replay_progress(run_id, after_sequence=invalid_after_sequence)  # type: ignore[arg-type]
        assert await foreign.replay_progress(run_id) == ()
        assert await foreign.replay_progress(run_id + 10_000) == ()
        assert foreign_events == []
        assert replay_calls == []
        await foreign.close()

        owned_replay = await owner.replay_progress(run_id)
        assert len(owned_replay) == 1
        assert owned_replay[0]["run_id"] == run_id
        assert owner_events == list(owned_replay)
        assert all("path" not in event and "payload" not in event for event in owner_events)
        assert replay_calls == [run_id]
    finally:
        await foreign.close()
        await owner.close()


@_async_test
async def test_control_observe_action_commit_and_wrong_handoff_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    monkeypatch.setattr(db, "update_rpc_run_process", lambda *_args, **_kwargs: True)
    process_box: list[_FakeOmp] = []
    holder: dict[str, object] = {}

    async def workflow(connection, *, claim_provider, control, **_kwargs):
        claim = claim_provider(connection)
        assert claim is not None
        await control.on_claimed(claim.run_id, int(claim.job["id"]), "greenhouse", JOB_URL)
        await control.record_progress(claim.run_id, "page_observed", "observed", 1, OBSERVATION_SHA)
        proposal = await control.propose_action(claim.run_id, 1, OBSERVATION_SHA, _observation(), None, {"status": "ready"})
        assert proposal is not None
        assert await control.before_action_dispatch(proposal, 1)
        await control.proposal_finished(
            proposal,
            1,
            True,
            "running",
            {"outcome": "allowed", "reason_code": None, "observation_sha256": OBSERVATION_SHA, "changed": True},
        )
        holder["proposal"] = proposal
        return [{"status": "manual", "reason_code": "no_deterministic_next_step"}]

    def process_factory(_config, host_tool_callback):
        process = _FakeOmp(host_tool_callback, mode="action")
        process_box.append(process)
        return process

    config = _config(db_path, artifact_root, resume, process_factory, workflow)
    coordinator = ApplicationRpcCoordinator(config)
    identity = resolve_application_rpc_identity(config)
    first = await coordinator.handle(
        _request(
            "run.start",
            payload={
                "goal": "prepare_application_draft",
                "job_url": JOB_URL,
                "candidate_profile_id": identity["candidate_profile_id"],
                "configured_resume_id": identity["configured_resume_id"],
                "headed": True,
            },
        )
    )
    await asyncio.sleep(0.1)
    assert process_box[0].action_responses
    assert parse_application_response(process_box[0].action_responses[0])["ok"] is True
    events = await coordinator.replay_progress(int(first["run_id"]))
    action_sequences = [int(event["action_sequence"]) for event in events]
    assert action_sequences == sorted(action_sequences)
    unique_action_sequences = sorted(set(action_sequences))
    assert unique_action_sequences == list(range(1, max(unique_action_sequences) + 1))
    assert any(event["event_type"] == "action_allowed" for event in events)
    await coordinator.close()


@_async_test
async def test_prompt_two_waits_for_prior_agent_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    monkeypatch.setattr(db, "update_rpc_run_process", lambda *_args, **_kwargs: True)
    process_box: list[_DelayedAgentEndOmp] = []

    async def workflow(connection, *, claim_provider, control, **_kwargs):
        claim = claim_provider(connection)
        assert claim is not None
        await control.on_claimed(claim.run_id, int(claim.job["id"]), "greenhouse", JOB_URL)
        await control.record_progress(claim.run_id, "page_observed", "observed", 1, OBSERVATION_SHA)
        first = await control.propose_action(
            claim.run_id, 1, OBSERVATION_SHA, _observation(), None, {"status": "ready"}
        )
        assert first is not None
        assert await control.before_action_dispatch(first, 1)
        await control.proposal_finished(
            first,
            1,
            True,
            "running",
            {"outcome": "allowed", "reason_code": None, "observation_sha256": OBSERVATION_SHA, "changed": True},
        )
        second_task = asyncio.create_task(
            control.propose_action(
                claim.run_id, 2, OBSERVATION_SHA, _observation(), None, {"status": "ready"}
            )
        )
        await asyncio.sleep(0.02)
        assert process_box[0].calls == 1
        assert process_box[0].active is True
        process_box[0].release_first.set()
        second = await second_task
        assert second is not None
        assert await control.before_action_dispatch(second, 2)
        await control.proposal_finished(
            second,
            2,
            True,
            "running",
            {"outcome": "allowed", "reason_code": None, "observation_sha256": OBSERVATION_SHA, "changed": True},
        )
        return [{"status": "manual", "reason_code": "no_deterministic_next_step"}]

    def process_factory(_config, host_tool_callback):
        process = _DelayedAgentEndOmp(host_tool_callback)
        process_box.append(process)
        return process

    config = _config(db_path, artifact_root, resume, process_factory, workflow)
    coordinator = ApplicationRpcCoordinator(config)
    identity = resolve_application_rpc_identity(config)
    response = await coordinator.handle(
        _request(
            "run.start",
            payload={
                "goal": "prepare_application_draft",
                "job_url": JOB_URL,
                "candidate_profile_id": identity["candidate_profile_id"],
                "configured_resume_id": identity["configured_resume_id"],
                "headed": True,
            },
        )
    )
    assert response["ok"] is True
    await asyncio.sleep(0.15)
    assert process_box[0].started == [1, 2]


@_async_test
async def test_no_proposal_parks_until_resume_and_cancel_closes_omp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    monkeypatch.setattr(db, "update_rpc_run_process", lambda *_args, **_kwargs: True)
    process_box: list[_FakeOmp] = []

    async def workflow(connection, *, claim_provider, control, **_kwargs):
        claim = claim_provider(connection)
        await control.on_claimed(claim.run_id, int(claim.job["id"]), "greenhouse", JOB_URL)
        await control.record_progress(claim.run_id, "page_observed", "observed", 1, OBSERVATION_SHA)
        assert await control.propose_action(claim.run_id, 1, OBSERVATION_SHA, _observation(), None, {"status": "ready"}) is None
        return [{"status": "manual", "reason_code": "no_deterministic_next_step"}]

    def process_factory(_config, host_tool_callback):
        process = _FakeOmp(host_tool_callback, mode="park")
        process_box.append(process)
        return process

    config = _config(db_path, artifact_root, resume, process_factory, workflow)
    coordinator = ApplicationRpcCoordinator(config)
    identity = resolve_application_rpc_identity(config)
    first = await coordinator.handle(
        _request(
            "run.start",
            payload={
                "goal": "prepare_application_draft",
                "job_url": JOB_URL,
                "candidate_profile_id": identity["candidate_profile_id"],
                "configured_resume_id": identity["configured_resume_id"],
                "headed": True,
            },
        )
    )
    await asyncio.sleep(0.05)
    status = await coordinator.handle(_request("run.status", run_id=int(first["run_id"])))
    assert status["state"] in {"manual", "running"}
    assert process_box[0].closed is False
    cancel = await coordinator.handle(_request("run.cancel", run_id=int(first["run_id"])))
    assert cancel["ok"] is True
    await asyncio.sleep(0.1)
    connection = db.connect(db_path)
    try:
        row = connection.execute(
            """
            SELECT a.status, a.reason_code, a.outcome, j.status AS job_status,
                   r.state
            FROM application_runs AS a
            JOIN jobs AS j ON j.id=a.job_id
            JOIN application_rpc_runs AS r ON r.run_id=a.id
            WHERE a.id=?
            """,
            (int(first["run_id"]),),
        ).fetchone()
        events = db.replay_rpc_events(connection, int(first["run_id"]))
    finally:
        connection.close()
    assert row is not None
    assert tuple(row) == (
        "failed",
        "abandoned_running_attempt",
        "retry",
        "queued",
        "failed",
    )
    assert [event.event_type for event in events].count("run_failed") == 1
    assert process_box[0].closed is True
    await coordinator.close()


@_async_test
async def test_identical_status_requests_coalesce_and_conflicts_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, run_id, process, stop, _ = await _start_parked_run(tmp_path, monkeypatch)
    request = _request("run.status", run_id=run_id)
    connection = db.connect(coordinator.config._db_path)
    try:
        db.reserve_rpc_request(
            connection,
            request=parse_application_request(request),
            run_id=run_id,
        )
    finally:
        connection.close()
    entered = asyncio.Event()
    release = asyncio.Event()
    original_complete = coordinator._complete_lifecycle

    async def delayed_complete(request, response, *, parent_request_id=None):
        entered.set()
        await release.wait()
        return await original_complete(request, response, parent_request_id=parent_request_id)

    monkeypatch.setattr(coordinator, "_complete_lifecycle", delayed_complete)
    leader = asyncio.create_task(coordinator.handle(request))
    await entered.wait()
    follower = asyncio.create_task(coordinator.handle(dict(request)))
    await asyncio.sleep(0.01)
    assert not follower.done()
    release.set()
    first, second = await asyncio.gather(leader, follower)
    assert first == second
    connection = db.connect(coordinator.config._db_path)
    try:
        info = db.get_rpc_request(connection, request["request_id"])
        assert info is not None and info.state == "completed"
        assert info.response_json is not None
    finally:
        connection.close()

    connection = db.connect(coordinator.config._db_path)
    try:
        before_requests = int(
            connection.execute("SELECT COUNT(*) FROM application_rpc_requests").fetchone()[0]
        )
        before_events = int(
            connection.execute(
                "SELECT COUNT(*) FROM application_progress_events WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()
    conflict = dict(request)
    conflict["operation"] = "run.cancel"
    response = await coordinator.handle(conflict)
    assert response["ok"] is False
    assert response["error"]["code"] == "request_conflict"  # type: ignore[index]
    connection = db.connect(coordinator.config._db_path)
    try:
        assert int(connection.execute("SELECT COUNT(*) FROM application_rpc_requests").fetchone()[0]) == before_requests
        assert int(
            connection.execute(
                "SELECT COUNT(*) FROM application_progress_events WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        ) == before_events
    finally:
        connection.close()
    stop.set()
    await coordinator.close()
    assert process.closed is True


@_async_test
async def test_status_expiry_during_read_persists_fixed_deadline_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, run_id, _process, stop, _ = await _start_parked_run(tmp_path, monkeypatch)
    expired = False
    original_read = coordinator._read_status_after_reservation

    async def expire_during_read(request_run_id: int):
        nonlocal expired
        expired = True
        return await original_read(request_run_id)

    monkeypatch.setattr(coordinator, "_read_status_after_reservation", expire_during_read)
    monkeypatch.setattr(
        coordinator,
        "_remaining_for",
        lambda _request: 0.0 if expired else 10.0,
    )
    request = _request("run.status", run_id=run_id)
    response = await coordinator.handle(request)
    assert response["ok"] is False
    assert response["error"]["code"] == "deadline_exceeded"  # type: ignore[index]
    assert await coordinator.handle(dict(request)) == response
    connection = db.connect(coordinator.config._db_path)
    try:
        info = db.get_rpc_request(connection, request["request_id"])
        assert info is not None and info.state == "completed"
        status = db.get_rpc_run_status(connection, run_id)
        assert status is not None and status.cancellation_requested is False
    finally:
        connection.close()
    stop.set()
    await coordinator.close()


@_async_test
async def test_resume_expiry_before_transition_persists_fixed_deadline_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, run_id, _process, stop, _ = await _start_parked_run(tmp_path, monkeypatch)
    active = coordinator._run_for(run_id)
    assert active is not None
    expired = False

    async def expire_before_resume(_request):
        nonlocal expired
        expired = True
        return False

    monkeypatch.setattr(active.control, "resume", expire_before_resume)
    monkeypatch.setattr(
        coordinator,
        "_remaining_for",
        lambda _request: 0.0 if expired else 10.0,
    )
    request = _request("run.resume", run_id=run_id)
    response = await coordinator.handle(request)
    assert response["ok"] is False
    assert response["error"]["code"] == "deadline_exceeded"  # type: ignore[index]
    assert await coordinator.handle(dict(request)) == response
    connection = db.connect(coordinator.config._db_path)
    try:
        info = db.get_rpc_request(connection, request["request_id"])
        assert info is not None and info.state == "completed"
        events = db.replay_rpc_events(connection, run_id)
        assert not any(
            event.request_id == request["request_id"]
            and event.event_type == "resume_requested"
            for event in events
        )
    finally:
        connection.close()
    stop.set()
    await coordinator.close()


@_async_test
async def test_cancel_expiry_before_transition_persists_fixed_deadline_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, run_id, _process, stop, _ = await _start_parked_run(tmp_path, monkeypatch)
    active = coordinator._run_for(run_id)
    assert active is not None
    expired = False

    async def expire_before_cancel():
        nonlocal expired
        expired = True
        return False

    monkeypatch.setattr(active.control, "cancel", expire_before_cancel)
    monkeypatch.setattr(
        coordinator,
        "_remaining_for",
        lambda _request: 0.0 if expired else 10.0,
    )
    request = _request("run.cancel", run_id=run_id)
    response = await coordinator.handle(request)
    assert response["ok"] is False
    assert response["error"]["code"] == "deadline_exceeded"  # type: ignore[index]
    assert await coordinator.handle(dict(request)) == response
    connection = db.connect(coordinator.config._db_path)
    try:
        info = db.get_rpc_request(connection, request["request_id"])
        assert info is not None and info.state == "completed"
        status = db.get_rpc_run_status(connection, run_id)
        assert status is not None and status.cancellation_requested is False
    finally:
        connection.close()
    stop.set()
    await coordinator.close()


@_async_test
async def test_cancel_expiry_after_transition_preserves_durability_uncertainty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, run_id, _process, stop, _ = await _start_parked_run(tmp_path, monkeypatch)
    active = coordinator._run_for(run_id)
    assert active is not None
    original_cancel = active.control.cancel
    expired = False

    async def transition_then_expire():
        nonlocal expired
        result = await original_cancel()
        expired = True
        return result

    monkeypatch.setattr(active.control, "cancel", transition_then_expire)
    monkeypatch.setattr(
        coordinator,
        "_remaining_for",
        lambda _request: 0.0 if expired else 10.0,
    )
    request = _request("run.cancel", run_id=run_id)
    with pytest.raises(ApplicationRpcDurabilityError):
        await coordinator.handle(request)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    replay = await coordinator.handle(dict(request))
    assert replay["ok"] is True
    connection = db.connect(coordinator.config._db_path)
    try:
        info = db.get_rpc_request(connection, request["request_id"])
        assert info is not None and info.state == "completed"
        status = db.get_rpc_run_status(connection, run_id)
        assert status is not None and status.cancellation_requested is True
    finally:
        connection.close()
    stop.set()
    await coordinator.close()

async def _assert_identical_lifecycle_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    coordinator, run_id, _process, stop, _ = await _start_parked_run(tmp_path, monkeypatch)
    active = coordinator._run_for(run_id)
    assert active is not None
    calls = 0
    if operation == "run.resume":
        original = active.control.resume

        async def counted(request):
            nonlocal calls
            calls += 1
            return await original(request)

        monkeypatch.setattr(active.control, "resume", counted)
    else:
        original = active.control.cancel

        async def counted():
            nonlocal calls
            calls += 1
            return await original()

        monkeypatch.setattr(active.control, "cancel", counted)
    request = _request(operation, run_id=run_id)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_complete = coordinator._complete_lifecycle

    async def delayed_complete(request, response, *, parent_request_id=None):
        entered.set()
        await release.wait()
        return await original_complete(request, response, parent_request_id=parent_request_id)

    monkeypatch.setattr(coordinator, "_complete_lifecycle", delayed_complete)
    leader = asyncio.create_task(coordinator.handle(request))
    await entered.wait()
    follower = asyncio.create_task(coordinator.handle(dict(request)))
    await asyncio.sleep(0.01)
    assert not follower.done()
    release.set()
    first, second = await asyncio.gather(leader, follower)
    assert first == second
    assert calls == 1
    connection = db.connect(coordinator.config._db_path)
    try:
        info = db.get_rpc_request(connection, request["request_id"])
        assert info is not None and info.state == "completed"
    finally:
        connection.close()
    stop.set()
    await coordinator.close()


@_async_test
async def test_identical_resume_requests_coalesce(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await _assert_identical_lifecycle_side_effect(tmp_path, monkeypatch, "run.resume")


@_async_test
async def test_identical_cancel_requests_coalesce(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await _assert_identical_lifecycle_side_effect(tmp_path, monkeypatch, "run.cancel")

@_async_test
async def test_foreign_start_replay_is_denied_without_claim_or_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, run_id, _process, stop, start_request = await _start_parked_run(tmp_path, monkeypatch)
    connection = db.connect(owner.config._db_path)
    try:
        before_runs = int(connection.execute("SELECT COUNT(*) FROM application_runs").fetchone()[0])
        before_events = int(
            connection.execute(
                "SELECT COUNT(*) FROM application_progress_events WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()
    foreign_config = _config(
        owner.config._db_path,
        owner.config._artifact_root,
        owner.config._resume_file,
        lambda *_args, **_kwargs: None,
        lambda *_args, **_kwargs: None,
        coordinator_id="foreign-coordinator",
    )
    foreign = ApplicationRpcCoordinator(foreign_config)
    response = await foreign.handle(dict(start_request))
    assert response["ok"] is False
    assert response["error"]["code"] == "unavailable"  # type: ignore[index]
    connection = db.connect(owner.config._db_path)
    try:
        assert int(connection.execute("SELECT COUNT(*) FROM application_runs").fetchone()[0]) == before_runs
        assert int(
            connection.execute(
                "SELECT COUNT(*) FROM application_progress_events WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        ) == before_events
    finally:
        connection.close()
    stop.set()
    await foreign.close()
    await owner.close()


@_async_test
async def test_restarted_terminal_handoff_lifecycle_records_are_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, run_id, _process, stop, _ = await _start_parked_run(tmp_path, monkeypatch)
    stop.set()
    await owner.close()
    connection = db.connect(owner.config._db_path)
    try:
        connection.execute(
            """
            UPDATE application_rpc_runs
            SET state='manual', handoff_committed=1,
                human_review_ready=0, version=version+1
            WHERE run_id=?
            """,
            (run_id,),
        )
        connection.execute(
            """
            UPDATE application_runs
            SET status='manual', reason_code='no_deterministic_next_step',
                finished_at=COALESCE(finished_at, ?)
            WHERE id=?
            """,
            (db.utc_now(), run_id),
        )
        connection.commit()
        before = db.get_rpc_run_status(connection, run_id)
        assert before is not None
        before_events = int(
            connection.execute(
                "SELECT COUNT(*) FROM application_progress_events WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()
    restarted = ApplicationRpcCoordinator(
        _config(
            owner.config._db_path,
            owner.config._artifact_root,
            owner.config._resume_file,
            lambda *_args, **_kwargs: None,
            lambda *_args, **_kwargs: None,
        )
    )
    status_request = _request("run.status", run_id=run_id)
    first = await restarted.handle(status_request)
    replay = await restarted.handle(dict(status_request))
    assert first == replay
    assert first["ok"] is True
    assert first["result"]["handoff_committed"] is True  # type: ignore[index]
    for operation in ("run.resume", "run.cancel"):
        response = await restarted.handle(_request(operation, run_id=run_id))
        assert response["ok"] is False
        assert response["error"]["code"] == "run_not_active"  # type: ignore[index]
    connection = db.connect(owner.config._db_path)
    try:
        after = db.get_rpc_run_status(connection, run_id)
        assert after is not None
        assert after.version == before.version
        assert after.state == before.state
        assert after.handoff_committed is True
        assert int(
            connection.execute(
                "SELECT COUNT(*) FROM application_progress_events WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        ) == before_events
        for request_id in (status_request["request_id"],):
            info = db.get_rpc_request(connection, request_id)
            assert info is not None and info.state == "completed"
    finally:
        connection.close()
    await restarted.close()


@_async_test
async def test_restarted_failed_status_replay_completes_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, run_id, _process, stop, _ = await _start_parked_run(tmp_path, monkeypatch)
    stop.set()
    await owner.close()
    connection = db.connect(owner.config._db_path)
    try:
        connection.execute(
            """
            UPDATE application_rpc_runs
            SET state='failed', handoff_committed=0,
                human_review_ready=0, version=version+1
            WHERE run_id=?
            """,
            (run_id,),
        )
        connection.execute(
            """
            UPDATE application_runs
            SET status='failed', reason_code='browser_error',
                finished_at=COALESCE(finished_at, ?)
            WHERE id=?
            """,
            (db.utc_now(), run_id),
        )
        connection.commit()
    finally:
        connection.close()
    restarted = ApplicationRpcCoordinator(
        _config(
            owner.config._db_path,
            owner.config._artifact_root,
            owner.config._resume_file,
            lambda *_args, **_kwargs: None,
            lambda *_args, **_kwargs: None,
        )
    )
    request = _request("run.status", run_id=run_id)
    first = await restarted.handle(request)
    replay = await restarted.handle(dict(request))
    assert first == replay
    assert first["ok"] is True
    info_connection = db.connect(owner.config._db_path)
    try:
        info = db.get_rpc_request(info_connection, request["request_id"])
        assert info is not None and info.state == "completed"
    finally:
        info_connection.close()
    await restarted.close()

@_async_test
async def test_handoff_cancel_after_commit_retries_cached_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    parent_raw = _request("run.status", run_id=1)
    parent = parse_application_request(parent_raw)
    context = HostToolContext(
        APPLICATION_RPC_PROTOCOL_VERSION,
        1,
        parent.request_id,
        parent.deadline_unix_ms,
    )
    proposal = parse_host_tool_call(
        {
            "type": "host_tool_call",
            "id": "handoff-host",
            "toolCallId": "handoff-call",
            "toolName": "browser.prepare_human_handoff",
            "arguments": {"observation_sha256": OBSERVATION_SHA},
        },
        context,
    )
    control = RpcApplicationControl(
        db_path=db_path,
        artifact_root=artifact_root,
        coordinator_id="test-coordinator",
        connection_factory=lambda: SimpleNamespace(close=lambda: None),
        run_id=1,
        parent_request=parent,
    )
    control._pending = SimpleNamespace(
        proposal=proposal,
        workflow_sequence=1,
        future=asyncio.get_running_loop().create_future(),
        invocation=SimpleNamespace(),
        mode="handoff",
        dispatched=True,
    )
    finalization = {
        "artifact_dir": None,
        "observation_summary": {},
        "plan_summary": {},
        "reason_code": "no_deterministic_next_step",
        "status": "manual",
    }
    proposal_result = {
        "outcome": "committed",
        "reason_code": "no_deterministic_next_step",
        "observation_sha256": OBSERVATION_SHA,
        "unresolved_required_count": 2,
        "automated_submission": False,
    }
    intent = {
        "application_finalization": finalization,
        "artifact_manifest_sha256": "0" * 64,
        "artifact_sha256": "1" * 64,
        "child_request_id": proposal.request.request_id,
        "commit_token_sha256": "2" * 64,
        "job_id": 1,
        "observation_sha256": OBSERVATION_SHA,
        "parent_request_id": proposal.parent_request_id,
        "session_id": "session-1",
    }
    bound = dict(intent)
    bound["proposal_result"] = proposal_result
    bind_calls = 0

    def bind_after_indeterminate_commit(*_args, **_kwargs):
        nonlocal bind_calls
        bind_calls += 1
        if bind_calls == 1:
            raise RuntimeError("commit acknowledgement lost")
        return bound

    monkeypatch.setattr(
        db,
        "bind_rpc_handoff_intent",
        bind_after_indeterminate_commit,
    )
    assert await control.prepare_handoff_finalization(
        proposal,
        action_sequence=1,
        intent=intent,
    )
    assert bind_calls == 2
    control.mark_handoff_committed()
    await control.cancel()
    calls: list[dict[str, object]] = []

    async def fake_commit(_pending, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(control, "_commit_pending", fake_commit)
    await control.close()
    assert calls
    assert calls[0]["result"] == proposal_result
    assert calls[0]["application_finalization"] == finalization
    assert control.handoff_committed is True


@_async_test
async def test_cancel_winning_handoff_intent_bind_stays_precommit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root, _resume, _ = _prepare_db(tmp_path)
    parent = parse_application_request(_request("run.status", run_id=1))
    proposal = parse_host_tool_call(
        {
            "type": "host_tool_call",
            "id": "cancel-before-handoff-host",
            "toolCallId": "cancel-before-handoff-call",
            "toolName": "browser.prepare_human_handoff",
            "arguments": {"observation_sha256": OBSERVATION_SHA},
        },
        HostToolContext(
            APPLICATION_RPC_PROTOCOL_VERSION,
            1,
            parent.request_id,
            parent.deadline_unix_ms,
        ),
    )
    control = RpcApplicationControl(
        db_path=db_path,
        artifact_root=artifact_root,
        coordinator_id="test-coordinator",
        connection_factory=lambda: SimpleNamespace(close=lambda: None),
        run_id=1,
        parent_request=parent,
    )
    control._pending = SimpleNamespace(
        proposal=proposal,
        workflow_sequence=1,
        future=asyncio.get_running_loop().create_future(),
        invocation=SimpleNamespace(),
        mode="handoff",
        dispatched=True,
    )
    prior_browser_state = control.browser_state
    bind_calls = 0

    def cancelled_bind(*_args, **_kwargs):
        nonlocal bind_calls
        bind_calls += 1
        raise RuntimeError("handoff intent provenance mismatch")

    monkeypatch.setattr(db, "bind_rpc_handoff_intent", cancelled_bind)
    monkeypatch.setattr(db, "read_rpc_cancellation", lambda *_args, **_kwargs: True)

    with pytest.raises(RuntimeError, match="abandoned_running_attempt"):
        await control.prepare_handoff_finalization(
            proposal,
            action_sequence=1,
            intent={},
        )

    assert bind_calls == 1
    assert control._cancel_event.is_set()
    assert control._post_commit_guard is False
    assert control.handoff_committed is False
    assert control.browser_state == prior_browser_state


@_async_test
async def test_close_waits_for_cancel_suppression_beyond_initial_launch_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    started = asyncio.Event()
    order: list[str] = []

    class Process:
        verified = True
        poisoned = False
        pid = os.getpid()
        session_identity_sha256 = hashlib.sha256(b"finite-session").hexdigest()

        async def close(self):
            order.append("closed")

    async def process_factory(_config, _callback):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(1.2)
            order.append("yielded")
            return Process()

    async def workflow(*_args, **_kwargs):
        return []

    config = _config(db_path, artifact_root, resume, process_factory, workflow)
    coordinator = ApplicationRpcCoordinator(config)
    original_abort = db.abort_rpc_start

    def abort(*args, **kwargs):
        order.append("aborted")
        return original_abort(*args, **kwargs)

    monkeypatch.setattr(db, "abort_rpc_start", abort)
    identity = resolve_application_rpc_identity(config)
    request = _request(
        "run.start",
        payload={
            "goal": "prepare_application_draft",
            "job_url": JOB_URL,
            "candidate_profile_id": identity["candidate_profile_id"],
            "configured_resume_id": identity["configured_resume_id"],
            "headed": True,
        },
    )
    task = asyncio.create_task(coordinator.handle(request))
    await started.wait()
    await coordinator.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert order == ["yielded", "closed", "aborted"]
    assert coordinator._runs == {}
    connection = db.connect(db_path)
    try:
        state = connection.execute(
            """
            SELECT a.status, a.outcome, j.status AS job_status
            FROM application_runs a JOIN jobs j ON j.id=a.job_id
            """
        ).fetchone()
        assert tuple(state) == ("failed", "retry", "queued")
    finally:
        connection.close()


@_async_test
async def test_slow_launch_survivor_stays_quarantined_until_late_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    process_closed = asyncio.Event()

    class Process:
        verified = True
        poisoned = False
        pid = os.getpid()
        session_identity_sha256 = hashlib.sha256(b"slow-session").hexdigest()

        async def close(self):
            process_closed.set()

    async def process_factory(_config, _callback):
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
        return Process()

    async def workflow(*_args, **_kwargs):
        return []

    monkeypatch.setattr(rpc_module, "_LAUNCH_CANCEL_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(rpc_module, "_CLEANUP_DRAIN_SECONDS", 0.03)
    monkeypatch.setattr(rpc_module, "_CANCELLED_TASK_DRAIN_SECONDS", 0.2)
    config = _config(db_path, artifact_root, resume, process_factory, workflow)
    coordinator = ApplicationRpcCoordinator(config)
    identity = resolve_application_rpc_identity(config)
    request = _request(
        "run.start",
        payload={
            "goal": "prepare_application_draft",
            "job_url": JOB_URL,
            "candidate_profile_id": identity["candidate_profile_id"],
            "configured_resume_id": identity["configured_resume_id"],
            "headed": True,
        },
    )
    start_task = asyncio.create_task(coordinator.handle(request))
    await started.wait()
    with pytest.raises(
        ApplicationRpcDurabilityError,
        match="RPC shutdown could not prove process absence",
    ):
        await coordinator.close()
    await asyncio.wait_for(cancelled.wait(), timeout=1.0)
    with pytest.raises(asyncio.CancelledError):
        await start_task
    connection = db.connect(db_path)
    try:
        quarantined = connection.execute(
            """
            SELECT a.status, a.reason_code, a.outcome, a.observation_json,
                   j.status AS job_status
            FROM application_runs a JOIN jobs j ON j.id=a.job_id
            """
        ).fetchone()
        assert tuple(quarantined)[:3] == ("manual", "page_not_stable", None)
        assert quarantined["job_status"] == "in_progress"
        assert "_launch_cleanup_quarantine" in json.loads(
            quarantined["observation_json"]
        )
    finally:
        connection.close()
    assert process_closed.is_set() is False
    assert coordinator._late_launch_cleanups
    release.set()
    await asyncio.wait_for(process_closed.wait(), timeout=1.0)
    for _ in range(100):
        if not coordinator._late_launch_cleanups:
            break
        await asyncio.sleep(0.01)
    assert not coordinator._late_launch_cleanups
    connection = db.connect(db_path)
    try:
        released = connection.execute(
            """
            SELECT a.status, a.outcome, j.status AS job_status
            FROM application_runs a JOIN jobs j ON j.id=a.job_id
            """
        ).fetchone()
        assert tuple(released) == ("failed", "retry", "queued")
    finally:
        connection.close()
    await coordinator.close()
    assert coordinator._runtime_lease_held is False


@_async_test
async def test_close_cancels_run_start_waiting_for_dispatch_lock(
    tmp_path: Path,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    launch_started = asyncio.Event()
    launch_calls = 0

    async def process_factory(_config, _callback):
        nonlocal launch_calls
        launch_calls += 1
        launch_started.set()
        await asyncio.Event().wait()

    async def workflow(*_args, **_kwargs):
        raise AssertionError("workflow must not start")

    config = _config(db_path, artifact_root, resume, process_factory, workflow)
    coordinator = ApplicationRpcCoordinator(config)
    identity = resolve_application_rpc_identity(config)

    def start_request() -> dict[str, object]:
        return _request(
            "run.start",
            payload={
                "goal": "prepare_application_draft",
                "job_url": JOB_URL,
                "candidate_profile_id": identity["candidate_profile_id"],
                "configured_resume_id": identity["configured_resume_id"],
                "headed": True,
            },
        )

    first = asyncio.create_task(coordinator.handle(start_request()))
    await launch_started.wait()
    queued = asyncio.create_task(coordinator.handle(start_request()))
    for _ in range(100):
        if len(coordinator._inflight_starts) == 2:
            break
        await asyncio.sleep(0.001)
    assert len(coordinator._inflight_starts) == 2

    await coordinator.close()

    results = await asyncio.gather(first, queued, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert launch_calls == 1
    assert coordinator._runs == {}
    connection = db.connect(db_path)
    try:
        state = connection.execute(
            """
            SELECT a.status, a.outcome, j.status AS job_status
            FROM application_runs a JOIN jobs j ON j.id=a.job_id
            """
        ).fetchone()
        assert tuple(state) == ("failed", "retry", "queued")
    finally:
        connection.close()


@_async_test
async def test_close_is_bounded_and_quarantines_slow_workflow_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    monkeypatch.setattr(
        db,
        "update_rpc_run_process",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        db,
        "mark_rpc_omp_spawn_attempted",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        rpc_module,
        "_CLEANUP_DRAIN_SECONDS",
        0.02,
    )
    monkeypatch.setattr(
        rpc_module,
        "_CANCELLED_TASK_DRAIN_SECONDS",
        0.02,
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def workflow(connection, *, claim_provider, control, **_kwargs):
        claim = claim_provider(connection)
        assert claim is not None
        await control.on_claimed(
            claim.run_id,
            int(claim.job["id"]),
            "greenhouse",
            JOB_URL,
        )
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()

    def process_factory(_config, host_tool_callback):
        return _FakeOmp(host_tool_callback, mode="park")

    config = _config(
        db_path,
        artifact_root,
        resume,
        process_factory,
        workflow,
    )
    coordinator = ApplicationRpcCoordinator(config)
    identity = resolve_application_rpc_identity(config)
    response = await coordinator.handle(
        _request(
            "run.start",
            payload={
                "goal": "prepare_application_draft",
                "job_url": JOB_URL,
                "candidate_profile_id": identity[
                    "candidate_profile_id"
                ],
                "configured_resume_id": identity[
                    "configured_resume_id"
                ],
                "headed": True,
            },
        )
    )
    assert response["ok"] is True
    await started.wait()
    before = asyncio.get_running_loop().time()
    with pytest.raises(ApplicationRpcDurabilityError):
        await coordinator.close()
    elapsed = asyncio.get_running_loop().time() - before
    assert elapsed < 0.5
    assert cancelled.is_set()
    connection = db.connect(db_path)
    try:
        state = connection.execute(
            """
            SELECT a.status, a.outcome, j.status AS job_status
            FROM application_runs a
            JOIN jobs j ON j.id=a.job_id
            WHERE a.id=?
            """,
            (response["run_id"],),
        ).fetchone()
        assert tuple(state) == ("failed", "retry", "queued")
    finally:
        connection.close()
    active = coordinator._runs[response["run_id"]]
    release.set()
    if active.workflow_task is not None:
        await asyncio.wait_for(
            asyncio.gather(
                active.workflow_task,
                return_exceptions=True,
            ),
            timeout=1.0,
        )
    await coordinator.close()
    assert coordinator._runs == {}


@_async_test
async def test_omp_close_requires_exact_process_group_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = {"pid": 991, "pgid": 991, "birth": "exact-birth"}

    class Process(OmpRpcProcess):
        def __init__(self):
            self._closed = False

        @property
        def process_identity(self):
            return identity

        async def close(self):
            self._closed = True

    process = Process()
    monkeypatch.setattr(
        db,
        "_exact_process_identity_state",
        lambda _identity: "live",
    )
    assert await ApplicationRpcCoordinator._invoke_process_close(process) is False

    process = Process()
    monkeypatch.setattr(
        db,
        "_exact_process_identity_state",
        lambda _identity: "absent",
    )
    assert await ApplicationRpcCoordinator._invoke_process_close(process) is True



@_async_test
async def test_start_abort_persistence_failure_emits_no_rpc_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    abort_calls = 0

    async def process_factory(_config, _callback):
        raise RuntimeError("launch failed")

    async def workflow(*_args, **_kwargs):
        raise AssertionError("workflow must not start")

    def fail_abort(*_args, **_kwargs):
        nonlocal abort_calls
        abort_calls += 1
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "abort_rpc_start", fail_abort)
    config = _config(db_path, artifact_root, resume, process_factory, workflow)
    coordinator = ApplicationRpcCoordinator(config)
    identity = resolve_application_rpc_identity(config)
    request = _request(
        "run.start",
        payload={
            "goal": "prepare_application_draft",
            "job_url": JOB_URL,
            "candidate_profile_id": identity["candidate_profile_id"],
            "configured_resume_id": identity["configured_resume_id"],
            "headed": True,
        },
    )

    with pytest.raises(ApplicationRpcDurabilityError):
        await coordinator.handle(request)
    assert abort_calls == 2
    with pytest.raises(ApplicationRpcDurabilityError):
        await coordinator.handle(request)

    connection = db.connect(db_path)
    try:
        info = db.get_rpc_request(connection, request["request_id"])
        assert info is not None
        assert info.state == "pending"
        assert info.response_json is None
    finally:
        connection.close()
    await coordinator.close()

def test_launch_cleanup_quarantine_blocks_restart_requeue_until_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    config = _config(
        db_path,
        artifact_root,
        resume,
        lambda *_args, **_kwargs: None,
        lambda *_args, **_kwargs: None,
    )
    identity = resolve_application_rpc_identity(config)
    request = parse_application_request(
        _request(
            "run.start",
            payload={
                "goal": "prepare_application_draft",
                "job_url": JOB_URL,
                "candidate_profile_id": identity["candidate_profile_id"],
                "configured_resume_id": identity["configured_resume_id"],
                "headed": True,
            },
        )
    )
    connection = db.connect(db_path)
    try:
        outcome = db.claim_application_job_for_rpc(
            connection,
            owner="rpc-owner",
            request=request,
            coordinator_id=config._coordinator_id,
        )
        assert outcome.outcome == "new" and outcome.run_id is not None
        info = db.abort_rpc_start(
            connection,
            request=request,
            coordinator_id=config._coordinator_id,
            error_code="deadline_exceeded",
            release_claim=False,
        )
        response = parse_application_response(info.response_json, request=request)
        assert response["state"] == "manual"
        row = connection.execute(
            """
            SELECT a.status, a.reason_code, a.outcome, a.observation_json,
                   j.status AS job_status
            FROM application_runs a JOIN jobs j ON j.id=a.job_id
            WHERE a.id=?
            """,
            (outcome.run_id,),
        ).fetchone()
        assert tuple(row)[:3] == ("manual", "page_not_stable", None)
        assert row["job_status"] == "in_progress"
        assert "_launch_cleanup_quarantine" in json.loads(row["observation_json"])
        monkeypatch.setattr(
            db,
            "_coordinator_identity_state",
            lambda _row: "absent",
        )
        restart = db.reconcile_abandoned_rpc_runs(connection)
        assert restart.status == "conflict"
        assert restart.conflict_run_ids == (outcome.run_id,)
        assert db.release_quarantined_rpc_start(
            connection,
            run_id=outcome.run_id,
            coordinator_id=config._coordinator_id,
        )
        released = connection.execute(
            """
            SELECT a.status, a.reason_code, a.outcome, j.status AS job_status
            FROM application_runs a JOIN jobs j ON j.id=a.job_id
            WHERE a.id=?
            """,
            (outcome.run_id,),
        ).fetchone()
        assert tuple(released) == (
            "failed",
            "abandoned_running_attempt",
            "retry",
            "queued",
        )
    finally:
        connection.close()


@_async_test
async def test_cancel_after_handoff_intent_is_rejected_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, run_id, _process, stop, _ = await _start_parked_run(
        tmp_path,
        monkeypatch,
    )
    active = coordinator._run_for(run_id)
    assert active is not None
    active.control._handoff_intent = {"child_request_id": str(uuid4())}
    request = _request("run.cancel", run_id=run_id)
    response = await coordinator.handle(request)
    replay = await coordinator.handle(dict(request))
    assert replay == response
    assert response["ok"] is False
    assert response["error"]["code"] == "run_not_active"
    assert active.control._cancel_event.is_set() is False
    connection = db.connect(coordinator.config._db_path)
    try:
        assert db.read_rpc_cancellation(connection, run_id) is False
        info = db.get_rpc_request(connection, request["request_id"])
        assert info is not None and info.state == "completed"
    finally:
        connection.close()
    active.control._handoff_intent = None
    stop.set()
    await coordinator.close()


@_async_test
async def test_child_flight_follower_cancellation_propagates_without_cancelling_owner(
    tmp_path: Path,
) -> None:
    db_path, artifact_root, _resume, _ = _prepare_db(tmp_path)
    parent = parse_application_request(_request("run.status", run_id=1))
    proposal = parse_host_tool_call(
        {
            "type": "host_tool_call",
            "id": "observe-follower",
            "toolCallId": "observe-follower-call",
            "toolName": "browser.observe",
            "arguments": {},
        },
        HostToolContext(
            APPLICATION_RPC_PROTOCOL_VERSION,
            1,
            parent.request_id,
            parent.deadline_unix_ms,
        ),
    )
    control = RpcApplicationControl(
        db_path=db_path,
        artifact_root=artifact_root,
        coordinator_id="test-coordinator",
        connection_factory=lambda: SimpleNamespace(close=lambda: None),
        run_id=1,
        parent_request=parent,
    )
    owner_result = asyncio.get_running_loop().create_future()
    control._child_flights[proposal.request.request_id] = SimpleNamespace(
        semantic_sha256=proposal.request.semantic_sha256,
        future=owner_result,
    )
    follower = asyncio.create_task(
        control.handle_host_tool(SimpleNamespace(proposal=proposal))
    )
    await asyncio.sleep(0)
    follower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await follower
    assert owner_result.cancelled() is False
    owner_result.set_result({"ok": True})


@pytest.mark.parametrize(
    "reservation_error",
    (
        RuntimeError("database unavailable"),
        RuntimeError("conflicting request binding"),
    ),
)
@_async_test
async def test_child_reservation_failure_has_no_ephemeral_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reservation_error: RuntimeError,
) -> None:
    db_path, artifact_root, _resume, _ = _prepare_db(tmp_path)
    parent = parse_application_request(
        _request("run.status", run_id=1)
    )
    proposal = parse_host_tool_call(
        {
            "type": "host_tool_call",
            "id": "observe-indeterminate",
            "toolCallId": "observe-indeterminate-call",
            "toolName": "browser.observe",
            "arguments": {},
        },
        HostToolContext(
            APPLICATION_RPC_PROTOCOL_VERSION,
            1,
            parent.request_id,
            parent.deadline_unix_ms,
        ),
    )
    control = RpcApplicationControl(
        db_path=db_path,
        artifact_root=artifact_root,
        coordinator_id="test-coordinator",
        connection_factory=lambda: SimpleNamespace(
            close=lambda: None
        ),
        run_id=1,
        parent_request=parent,
    )

    def fail_reservation(*_args, **_kwargs):
        raise reservation_error

    monkeypatch.setattr(
        db,
        "reserve_rpc_request",
        fail_reservation,
    )
    with pytest.raises(OmpHostDurabilityError):
        await control.handle_host_tool(
            SimpleNamespace(proposal=proposal)
        )
    assert proposal.request.request_id not in control._child_flights


@_async_test
async def test_child_owner_cancel_before_reservation_fails_follower(
    tmp_path: Path,
) -> None:
    db_path, artifact_root, _resume, _ = _prepare_db(tmp_path)
    parent = parse_application_request(
        _request("run.status", run_id=1)
    )
    proposal = parse_host_tool_call(
        {
            "type": "host_tool_call",
            "id": "observe-cancel-before-reserve",
            "toolCallId": "observe-cancel-before-reserve-call",
            "toolName": "browser.observe",
            "arguments": {},
        },
        HostToolContext(
            APPLICATION_RPC_PROTOCOL_VERSION,
            1,
            parent.request_id,
            parent.deadline_unix_ms,
        ),
    )
    control = RpcApplicationControl(
        db_path=db_path,
        artifact_root=artifact_root,
        coordinator_id="test-coordinator",
        connection_factory=lambda: SimpleNamespace(
            close=lambda: None
        ),
        run_id=1,
        parent_request=parent,
    )
    await control._db_lock.acquire()
    owner = asyncio.create_task(
        control.handle_host_tool(
            SimpleNamespace(proposal=proposal)
        )
    )
    for _ in range(100):
        if proposal.request.request_id in control._child_flights:
            break
        await asyncio.sleep(0)
    follower = asyncio.create_task(
        control.handle_host_tool(
            SimpleNamespace(proposal=proposal)
        )
    )
    await asyncio.sleep(0)
    owner.cancel()
    control._db_lock.release()
    with pytest.raises(asyncio.CancelledError):
        await owner
    with pytest.raises(OmpHostDurabilityError):
        await follower




@_async_test
async def test_proposal_commit_replays_after_durable_commit_ack_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root, _resume, _ = _prepare_db(tmp_path)
    parent = parse_application_request(
        _request("run.status", run_id=1)
    )
    proposal = parse_host_tool_call(
        {
            "type": "host_tool_call",
            "id": "observe-commit-ack-loss",
            "toolCallId": "observe-commit-ack-loss-call",
            "toolName": "browser.observe",
            "arguments": {},
        },
        HostToolContext(
            APPLICATION_RPC_PROTOCOL_VERSION,
            1,
            parent.request_id,
            parent.deadline_unix_ms,
        ),
    )
    control = RpcApplicationControl(
        db_path=db_path,
        artifact_root=artifact_root,
        coordinator_id="test-coordinator",
        connection_factory=lambda: SimpleNamespace(
            close=lambda: None
        ),
        run_id=1,
        parent_request=parent,
    )
    future = asyncio.get_running_loop().create_future()
    pending = SimpleNamespace(
        proposal=proposal,
        workflow_sequence=1,
        future=future,
        dispatched=True,
    )
    control._pending = pending
    control._child_flights[proposal.request.request_id] = SimpleNamespace(
        semantic_sha256=proposal.request.semantic_sha256,
        future=future,
    )
    stored_response: str | None = None
    commit_calls = 0

    def get_request(_connection, request_id):
        if stored_response is None:
            return None
        return db.RpcRequestInfo(
            request_id=request_id,
            protocol_version=APPLICATION_RPC_PROTOCOL_VERSION,
            operation=proposal.request.operation,
            semantic_sha256=proposal.request.semantic_sha256,
            request_json=json.dumps(
                proposal.request.to_mapping()
            ),
            run_id=1,
            parent_request_id=proposal.parent_request_id,
            state="completed",
            response_json=stored_response,
            created_at="2025-01-01T00:00:00Z",
            completed_at="2025-01-01T00:00:01Z",
        )

    def commit_after_write(_connection, **kwargs):
        nonlocal stored_response, commit_calls
        commit_calls += 1
        stored_response = json.dumps(kwargs["response"])
        raise RuntimeError("commit acknowledgement lost")

    monkeypatch.setattr(db, "get_rpc_request", get_request)
    monkeypatch.setattr(
        db,
        "get_rpc_run_status",
        lambda *_args, **_kwargs: SimpleNamespace(
            action_sequence=0,
            latest_event_sequence=0,
        ),
    )
    monkeypatch.setattr(
        db,
        "commit_rpc_proposal_result",
        commit_after_write,
    )
    monkeypatch.setattr(
        db,
        "latest_rpc_event",
        lambda *_args, **_kwargs: None,
    )
    result = await control._commit_pending(
        pending,
        ok=True,
        state="running",
        result=_observation(),
        error_code=None,
        event_type="page_observed",
        summary_code="observed",
    )
    assert result["ok"] is True
    assert result["action_sequence"] == 1
    assert commit_calls == 1
    assert future.result() == result
    assert control._pending is None
@_async_test
async def test_transport_rejection_is_completed_in_child_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root, _resume, _ = _prepare_db(tmp_path)
    parent = parse_application_request(
        _request("run.status", run_id=1)
    )
    context = HostToolContext(
        APPLICATION_RPC_PROTOCOL_VERSION,
        1,
        parent.request_id,
        parent.deadline_unix_ms,
    )
    proposal = parse_host_tool_call(
        {
            "type": "host_tool_call",
            "id": "observe-transport-rejected",
            "toolCallId": "observe-transport-rejected-call",
            "toolName": "browser.observe",
            "arguments": {},
        },
        context,
    )
    control = RpcApplicationControl(
        db_path=db_path,
        artifact_root=artifact_root,
        coordinator_id="test-coordinator",
        connection_factory=lambda: SimpleNamespace(
            close=lambda: None
        ),
        run_id=1,
        parent_request=parent,
    )
    completed_responses: list[Mapping[str, object]] = []

    def info(
        *,
        state: str,
        response_json: str | None,
        created: bool,
    ):
        return db.RpcRequestInfo(
            request_id=proposal.request.request_id,
            protocol_version=APPLICATION_RPC_PROTOCOL_VERSION,
            operation=proposal.request.operation,
            semantic_sha256=proposal.request.semantic_sha256,
            request_json=json.dumps(
                proposal.request.to_mapping()
            ),
            run_id=1,
            parent_request_id=proposal.parent_request_id,
            state=state,
            response_json=response_json,
            created_at="2025-01-01T00:00:00Z",
            completed_at=(
                "2025-01-01T00:00:01Z"
                if state == "completed"
                else None
            ),
            created=created,
        )

    monkeypatch.setattr(
        db,
        "reserve_rpc_request",
        lambda *_args, **_kwargs: info(
            state="pending",
            response_json=None,
            created=True,
        ),
    )

    def complete(_connection, *, response, **_kwargs):
        completed_responses.append(dict(response))
        return info(
            state="completed",
            response_json=json.dumps(response),
            created=False,
        )

    monkeypatch.setattr(
        db,
        "complete_rpc_request",
        complete,
    )
    response = await control.handle_host_tool(
        OmpHostInvocation(
            proposal,
            context,
            transport_rejection_code="action_rejected",
        )
    )
    assert response is not None
    assert response["ok"] is False
    assert response["error"]["code"] == "action_rejected"
    assert completed_responses == [response]
    assert control._prompt_observed is False




@_async_test
async def test_lifecycle_follower_replays_success_after_owner_crashes_postcommit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, run_id, _process, stop, _ = await _start_parked_run(
        tmp_path,
        monkeypatch,
    )
    request = _request("run.status", run_id=run_id)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_complete = coordinator._complete_lifecycle
    original_finish = coordinator._finish_lifecycle_flight_now
    first_complete = True
    first_finish = True

    async def delayed_complete(
        parsed_request,
        response,
        *,
        parent_request_id=None,
    ):
        nonlocal first_complete
        if parsed_request.request_id == request["request_id"] and first_complete:
            first_complete = False
            entered.set()
            await release.wait()
        return await original_complete(
            parsed_request,
            response,
            parent_request_id=parent_request_id,
        )

    def crash_after_durable_completion(request_id, response):
        nonlocal first_finish
        if request_id == request["request_id"] and first_finish:
            first_finish = False
            raise asyncio.CancelledError
        return original_finish(request_id, response)

    monkeypatch.setattr(coordinator, "_complete_lifecycle", delayed_complete)
    monkeypatch.setattr(
        coordinator,
        "_finish_lifecycle_flight_now",
        crash_after_durable_completion,
    )
    leader = asyncio.create_task(coordinator.handle(request))
    await entered.wait()
    follower = asyncio.create_task(coordinator.handle(dict(request)))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await leader
    response = await asyncio.wait_for(follower, timeout=2.0)
    assert response["ok"] is True
    assert await coordinator.handle(dict(request)) == response
    connection = db.connect(coordinator.config._db_path)
    try:
        info = db.get_rpc_request(connection, request["request_id"])
        assert info is not None and info.state == "completed"
        assert parse_application_response(
            info.response_json,
            request=parse_application_request(request),
        ) == response
    finally:
        connection.close()
    stop.set()
    await coordinator.close()


@_async_test
async def test_resume_follower_gets_success_when_owner_crashes_after_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, run_id, _process, stop, _ = await _start_parked_run(
        tmp_path,
        monkeypatch,
    )
    status = await coordinator._read_status(run_id)
    assert status is not None and status.resume_eligible
    request = _request("run.resume", run_id=run_id)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_complete = coordinator._complete_lifecycle
    first = True

    async def crash_before_request_completion(
        parsed_request,
        response,
        *,
        parent_request_id=None,
    ):
        nonlocal first
        if parsed_request.request_id == request["request_id"] and first:
            first = False
            entered.set()
            await release.wait()
            raise asyncio.CancelledError
        return await original_complete(
            parsed_request,
            response,
            parent_request_id=parent_request_id,
        )

    monkeypatch.setattr(
        coordinator,
        "_complete_lifecycle",
        crash_before_request_completion,
    )
    leader = asyncio.create_task(coordinator.handle(request))
    await entered.wait()
    follower = asyncio.create_task(coordinator.handle(dict(request)))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await leader
    response = await asyncio.wait_for(follower, timeout=2.0)
    assert response["ok"] is True
    connection = db.connect(coordinator.config._db_path)
    try:
        events = db.replay_rpc_events(connection, run_id)
        assert any(
            event.request_id == request["request_id"]
            and event.event_type == "resume_requested"
            for event in events
        )
        info = db.get_rpc_request(connection, request["request_id"])
        assert info is not None and info.state == "completed"
    finally:
        connection.close()
    stop.set()
    await coordinator.close()


@_async_test
async def test_unknown_run_response_is_durable_and_conflicts_replay(
    tmp_path: Path,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)

    async def no_process(*_args, **_kwargs):
        raise AssertionError("unknown status must not launch OMP")

    async def no_workflow(*_args, **_kwargs):
        raise AssertionError("unknown status must not run workflow")

    coordinator = ApplicationRpcCoordinator(
        _config(
            db_path,
            artifact_root,
            resume,
            no_process,
            no_workflow,
        )
    )
    request = _request("run.status", run_id=999)
    response = await coordinator.handle(request)
    assert response["ok"] is False
    assert response["error"]["code"] == "run_not_found"
    assert await coordinator.handle(dict(request)) == response
    conflict = dict(request)
    conflict["operation"] = "run.cancel"
    conflict_response = await coordinator.handle(conflict)
    assert conflict_response["ok"] is False
    assert conflict_response["error"]["code"] == "request_conflict"
    connection = db.connect(db_path)
    try:
        info = db.get_rpc_request(connection, request["request_id"])
        assert info is not None
        assert info.state == "completed"
        assert info.run_id is None
        assert parse_application_response(
            info.response_json,
            request=parse_application_request(request),
        ) == response
    finally:
        connection.close()
    await coordinator.close()


@_async_test
async def test_lifecycle_completion_retry_is_shared_and_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, run_id, _process, stop, _ = await _start_parked_run(
        tmp_path,
        monkeypatch,
    )
    request = _request("run.status", run_id=run_id)
    original = db.complete_rpc_request
    failures = 0

    def fail_once(*args, **kwargs):
        nonlocal failures
        parsed = kwargs.get("request")
        if (
            parsed is not None
            and parsed.request_id == request["request_id"]
            and failures == 0
        ):
            failures += 1
            raise RuntimeError("transient completion failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "complete_rpc_request", fail_once)
    first, second = await asyncio.gather(
        coordinator.handle(request),
        coordinator.handle(dict(request)),
    )
    assert first == second
    assert first["ok"] is True
    assert failures == 1
    connection = db.connect(coordinator.config._db_path)
    try:
        info = db.get_rpc_request(connection, request["request_id"])
        assert info is not None and info.state == "completed"
        assert parse_application_response(
            info.response_json,
            request=parse_application_request(request),
        ) == first
    finally:
        connection.close()
    stop.set()
    await coordinator.close()


@_async_test
async def test_handoff_reconciliation_failure_never_returns_stale_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, run_id, _process, stop, _ = await _start_parked_run(
        tmp_path,
        monkeypatch,
    )

    def fail_reconciliation(*_args, **_kwargs):
        raise RuntimeError("reconciliation failed")

    monkeypatch.setattr(
        db,
        "reconcile_committed_handoff_failure",
        fail_reconciliation,
    )
    with pytest.raises(ApplicationRpcServiceError) as caught:
        await coordinator._read_status(run_id)
    assert caught.value.code == "unavailable"
    stop.set()
    await coordinator.close()


@_async_test
async def test_committed_release_failure_uses_rpc_quarantine_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root, _resume, _ = _prepare_db(tmp_path)
    control = RpcApplicationControl(
        db_path=db_path,
        artifact_root=artifact_root,
        coordinator_id="test-coordinator",
        connection_factory=lambda: SimpleNamespace(close=lambda: None),
        run_id=1,
    )
    control._post_commit_guard = True
    control._handoff_committed = True
    calls = []
    monkeypatch.setattr(
        db,
        "reconcile_committed_handoff_failure",
        lambda *args, **kwargs: calls.append(kwargs) or True,
    )
    root = ArtifactRoot.open(artifact_root, cwd=tmp_path)
    try:
        assert await control.reconcile_postcommit_handoff_failure(
            1,
            session_id=None,
            artifact_root=root,
        )
    finally:
        root.close()
    assert len(calls) == 1
    assert calls[0]["run_id"] == 1

@_async_test
async def test_cancel_commits_while_resume_waits_and_blocks_resume_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, run_id, _process, stop, _ = await _start_parked_run(
        tmp_path,
        monkeypatch,
    )
    active = coordinator._runs[run_id]
    control = active.control
    status = None
    for _ in range(100):
        status = await coordinator._read_status(run_id)
        if status is not None and status.resume_eligible:
            break
        await asyncio.sleep(0.01)
    assert status is not None and status.resume_eligible

    resume_request = parse_application_request(_request("run.resume", run_id=run_id))
    cancellation_control = RpcApplicationControl(
        db_path=coordinator.config._db_path,
        artifact_root=coordinator.config._artifact_root,
        coordinator_id=coordinator.config._coordinator_id,
        run_id=run_id,
    )
    await control._db_lock.acquire()
    try:
        resume_task = asyncio.create_task(control.resume(resume_request))
        await asyncio.sleep(0)
        assert not resume_task.done()

        assert await cancellation_control.cancel()
        connection = db.connect(coordinator.config._db_path)
        try:
            cancelled = db.get_rpc_run_status(connection, run_id)
            assert cancelled is not None and cancelled.cancellation_requested
        finally:
            connection.close()
    finally:
        control._db_lock.release()

    assert await resume_task is False
    assert control._cancel_event.is_set() is False
    connection = db.connect(coordinator.config._db_path)
    try:
        final_status = db.get_rpc_run_status(connection, run_id)
        assert final_status is not None
        assert final_status.cancellation_requested
        assert final_status.state == "failed"
        events = db.replay_rpc_events(connection, run_id)
        assert not any(event.event_type == "resume_requested" for event in events)
    finally:
        connection.close()

    stop.set()
    await coordinator.close()


@_async_test
async def test_resume_commit_precedes_later_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, run_id, _process, stop, _ = await _start_parked_run(
        tmp_path,
        monkeypatch,
    )
    active = coordinator._runs[run_id]
    status = None
    for _ in range(100):
        status = await coordinator._read_status(run_id)
        if status is not None and status.resume_eligible:
            break
        await asyncio.sleep(0.01)
    assert status is not None and status.resume_eligible
    resume_request = parse_application_request(_request("run.resume", run_id=run_id))
    connection = db.connect(coordinator.config._db_path)
    try:
        db.reserve_rpc_request(
            connection,
            request=resume_request,
            run_id=run_id,
        )
    finally:
        connection.close()

    assert await active.control.resume(resume_request)
    cancellation_control = RpcApplicationControl(
        db_path=coordinator.config._db_path,
        artifact_root=coordinator.config._artifact_root,
        coordinator_id=coordinator.config._coordinator_id,
        run_id=run_id,
    )
    assert await cancellation_control.cancel()

    connection = db.connect(coordinator.config._db_path)
    try:
        final_status = db.get_rpc_run_status(connection, run_id)
        assert final_status is not None
        assert final_status.state == "running"
        assert final_status.cancellation_requested
        events = db.replay_rpc_events(connection, run_id)
        assert any(event.event_type == "resume_requested" for event in events)
    finally:
        connection.close()

    stop.set()
    await coordinator.close()

@_async_test
async def test_cancel_db_rejection_leaves_control_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    control = RpcApplicationControl(
        db_path=db_path,
        artifact_root=artifact_root,
        coordinator_id="test-coordinator",
        connection_factory=lambda: SimpleNamespace(close=lambda: None),
        run_id=1,
    )
    monkeypatch.setattr(db, "request_rpc_cancellation", lambda *_args, **_kwargs: False)
    await control.cancel()
    assert control._cancel_event.is_set() is False
    assert control.coordinator_state == "starting"

def test_public_event_projection_is_scalar_and_redacted() -> None:
    event = db.RpcEventInfo(7, 2, str(uuid4()), 3, "2025-01-01T00:00:00Z", "page_observed", "observed", OBSERVATION_SHA)
    projection = public_rpc_event(event)
    assert projection["run_id"] == 7
    assert projection["observation_sha256"] == OBSERVATION_SHA
    assert "path" not in projection
    with pytest.raises(ApplicationRpcServiceError):
        public_rpc_event({"secret": "not allowed"})


@_async_test
async def test_start_initial_ledger_read_failure_is_durability_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    coordinator = ApplicationRpcCoordinator(
        _config(
            db_path,
            artifact_root,
            resume,
            lambda *_args, **_kwargs: None,
            lambda *_args, **_kwargs: None,
        )
    )
    identity = resolve_application_rpc_identity(coordinator.config)
    request = _request(
        "run.start",
        payload={
            "goal": "prepare_application_draft",
            "job_url": JOB_URL.replace("/123", "/999"),
            "candidate_profile_id": identity["candidate_profile_id"],
            "configured_resume_id": identity["configured_resume_id"],
            "headed": True,
        },
    )
    original_get_request = db.get_rpc_request

    def fail_initial_probe(*_args, **_kwargs):
        raise RuntimeError("request ledger unavailable")

    monkeypatch.setattr(db, "get_rpc_request", fail_initial_probe)
    try:
        with pytest.raises(ApplicationRpcDurabilityError, match="ledger probe"):
            await coordinator.handle(request)
    finally:
        monkeypatch.setattr(db, "get_rpc_request", original_get_request)
        await coordinator.close()


@_async_test
async def test_start_claim_acknowledgement_loss_is_durability_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    coordinator = ApplicationRpcCoordinator(
        _config(
            db_path,
            artifact_root,
            resume,
            lambda *_args, **_kwargs: None,
            lambda *_args, **_kwargs: None,
        )
    )
    identity = resolve_application_rpc_identity(coordinator.config)
    request = _request(
        "run.start",
        payload={
            "goal": "prepare_application_draft",
            "job_url": JOB_URL,
            "candidate_profile_id": identity["candidate_profile_id"],
            "configured_resume_id": identity["configured_resume_id"],
            "headed": True,
        },
    )
    original_claim = db.claim_application_job_for_rpc

    def lose_claim_ack(*args, **kwargs):
        outcome = original_claim(*args, **kwargs)
        assert outcome.outcome == "new"
        raise RuntimeError("claim acknowledgement lost")

    monkeypatch.setattr(db, "claim_application_job_for_rpc", lose_claim_ack)
    try:
        with pytest.raises(ApplicationRpcDurabilityError, match="claim outcome"):
            await coordinator.handle(request)
        monkeypatch.setattr(db, "claim_application_job_for_rpc", original_claim)
        connection = db.connect(db_path)
        try:
            info = db.get_rpc_request(connection, request["request_id"])
            assert info is not None
            assert info.state == "pending"
            assert info.run_id is not None
        finally:
            connection.close()
    finally:
        await coordinator.close()


@_async_test
async def test_start_unavailable_unexpected_run_bound_probe_is_durability_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    coordinator = ApplicationRpcCoordinator(
        _config(
            db_path,
            artifact_root,
            resume,
            lambda *_args, **_kwargs: None,
            lambda *_args, **_kwargs: None,
        )
    )
    identity = resolve_application_rpc_identity(coordinator.config)
    request = _request(
        "run.start",
        payload={
            "goal": "prepare_application_draft",
            "job_url": JOB_URL.replace("/123", "/999"),
            "candidate_profile_id": identity["candidate_profile_id"],
            "configured_resume_id": identity["configured_resume_id"],
            "headed": True,
        },
    )
    original_get_request = db.get_rpc_request
    probes = 0

    def return_run_bound_probe(connection, request_id):
        nonlocal probes
        probes += 1
        info = original_get_request(connection, request_id)
        if probes == 2:
            assert info is not None
            return replace(info, run_id=123)
        return info

    monkeypatch.setattr(db, "get_rpc_request", return_run_bound_probe)
    try:
        with pytest.raises(ApplicationRpcDurabilityError, match="unexpected durable"):
            await coordinator.handle(request)
        assert probes == 2
        monkeypatch.setattr(db, "get_rpc_request", original_get_request)
        connection = db.connect(db_path)
        try:
            info = db.get_rpc_request(connection, request["request_id"])
            assert info is not None
            assert info.state == "pending"
            assert info.run_id is None
        finally:
            connection.close()
    finally:
        await coordinator.close()


@_async_test
async def test_status_internal_browser_states_are_fixed_errors_and_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, run_id, _process, stop, _ = await _start_parked_run(
        tmp_path,
        monkeypatch,
    )
    try:
        active = coordinator._run_for(run_id)
        assert active is not None
        for internal_state in ("unknown", "open_guarded"):
            active.control._browser_state = internal_state
            request = _request("run.status", run_id=run_id)
            first = await coordinator.handle(request)
            assert first["ok"] is False
            assert first["error"]["code"] == "unavailable"  # type: ignore[index]
            assert await coordinator.handle(dict(request)) == first
            assert (
                parse_application_response(
                    first,
                    request=parse_application_request(request),
                )
                == first
            )
    finally:
        stop.set()
        await coordinator.close()


@_async_test
async def test_start_unavailable_without_job_is_durably_replayable(
    tmp_path: Path,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    coordinator = ApplicationRpcCoordinator(
        _config(
            db_path,
            artifact_root,
            resume,
            lambda *_args, **_kwargs: None,
            lambda *_args, **_kwargs: None,
        )
    )
    identity = resolve_application_rpc_identity(coordinator.config)
    request = _request(
        "run.start",
        payload={
            "goal": "prepare_application_draft",
            "job_url": JOB_URL.replace("/123", "/999"),
            "candidate_profile_id": identity["candidate_profile_id"],
            "configured_resume_id": identity["configured_resume_id"],
            "headed": True,
        },
    )
    try:
        first = await coordinator.handle(request)
        second = await coordinator.handle(dict(request))
        assert first == second
        assert first["ok"] is False
        assert first["error"]["code"] == "unavailable"  # type: ignore[index]
        connection = db.connect(db_path)
        try:
            info = db.get_rpc_request(connection, request["request_id"])
            assert info is not None
            assert info.state == "completed"
            assert info.run_id is None
            assert info.response_json is not None
            assert connection.execute("SELECT COUNT(*) FROM application_runs").fetchone()[0] == 0
        finally:
            connection.close()
    finally:
        await coordinator.close()

@_async_test
async def test_start_unavailable_request_probe_failure_is_durability_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    coordinator = ApplicationRpcCoordinator(
        _config(
            db_path,
            artifact_root,
            resume,
            lambda *_args, **_kwargs: None,
            lambda *_args, **_kwargs: None,
        )
    )
    identity = resolve_application_rpc_identity(coordinator.config)
    request = _request(
        "run.start",
        payload={
            "goal": "prepare_application_draft",
            "job_url": JOB_URL.replace("/123", "/999"),
            "candidate_profile_id": identity["candidate_profile_id"],
            "configured_resume_id": identity["configured_resume_id"],
            "headed": True,
        },
    )
    original_get_request = db.get_rpc_request
    probes = 0

    def fail_post_claim_probe(connection, request_id):
        nonlocal probes
        probes += 1
        if probes == 2:
            raise RuntimeError("request ledger unavailable")
        return original_get_request(connection, request_id)

    monkeypatch.setattr(db, "get_rpc_request", fail_post_claim_probe)
    try:
        with pytest.raises(ApplicationRpcDurabilityError, match="ledger probe"):
            await coordinator.handle(request)
        assert probes == 2
        monkeypatch.setattr(db, "get_rpc_request", original_get_request)
        connection = db.connect(db_path)
        try:
            info = db.get_rpc_request(connection, request["request_id"])
            assert info is not None
            assert info.state == "pending"
            assert info.run_id is None
            assert info.response_json is None
        finally:
            connection.close()
    finally:
        await coordinator.close()


@_async_test
async def test_pending_no_job_retry_completes_after_interrupted_unavailable_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    coordinator = ApplicationRpcCoordinator(
        _config(
            db_path,
            artifact_root,
            resume,
            lambda *_args, **_kwargs: None,
            lambda *_args, **_kwargs: None,
        )
    )
    identity = resolve_application_rpc_identity(coordinator.config)
    request = _request(
        "run.start",
        payload={
            "goal": "prepare_application_draft",
            "job_url": JOB_URL.replace("/123", "/999"),
            "candidate_profile_id": identity["candidate_profile_id"],
            "configured_resume_id": identity["configured_resume_id"],
            "headed": True,
        },
    )
    original_complete = coordinator._complete_lifecycle
    interrupted = True

    async def interrupt_once(request_obj, response, **kwargs):
        nonlocal interrupted
        if interrupted:
            interrupted = False
            raise asyncio.CancelledError
        return await original_complete(request_obj, response, **kwargs)

    monkeypatch.setattr(coordinator, "_complete_lifecycle", interrupt_once)
    try:
        with pytest.raises(asyncio.CancelledError):
            await coordinator.handle(request)
        monkeypatch.setattr(coordinator, "_complete_lifecycle", original_complete)
        replay = await coordinator.handle(dict(request))
        assert replay == build_application_response(
            parse_application_request(request),
            ok=False,
            state="failed",
            action_sequence=0,
            event_sequence=0,
            error="unavailable",
        )
        assert await coordinator.handle(dict(request)) == replay
        connection = db.connect(db_path)
        try:
            info = db.get_rpc_request(connection, request["request_id"])
            assert info is not None
            assert info.state == "completed"
            assert info.run_id is None
            assert info.response_json is not None
        finally:
            connection.close()
    finally:
        await coordinator.close()


@_async_test
async def test_start_unavailable_prior_live_process_is_rowless_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    stop = asyncio.Event()
    process_box: list[_FakeOmp] = []

    async def workflow(connection, *, claim_provider, control, **_kwargs):
        claim = claim_provider(connection)
        assert claim is not None
        await control.on_claimed(claim.run_id, int(claim.job["id"]), "greenhouse", JOB_URL)
        await stop.wait()

    def process_factory(_config, host_tool_callback):
        process = _FakeOmp(host_tool_callback, mode="park")
        process_box.append(process)
        return process

    config = _config(db_path, artifact_root, resume, process_factory, workflow)
    coordinator = ApplicationRpcCoordinator(config)
    identity = resolve_application_rpc_identity(config)
    payload = {
        "goal": "prepare_application_draft",
        "job_url": JOB_URL,
        "candidate_profile_id": identity["candidate_profile_id"],
        "configured_resume_id": identity["configured_resume_id"],
        "headed": True,
    }
    prior_request = parse_application_request(
        _request("run.start", payload=payload)
    )
    connection = db.connect(db_path)
    try:
        prior_claim = db.claim_application_job_for_rpc(
            connection,
            owner="prior-owner",
            request=prior_request,
            coordinator_id="prior-coordinator",
        )
        assert prior_claim.run_id is not None
        now = db.utc_now()
        connection.execute(
            """
            UPDATE application_runs
            SET status='failed', reason_code='browser_error', outcome='retry',
                reviewed_at=?, finished_at=?
            WHERE id=?
            """,
            (now, now, prior_claim.run_id),
        )
        connection.execute(
            """
            UPDATE application_rpc_runs
            SET state='failed', omp_process_pid=301, omp_process_pgid=301,
                omp_process_birth='live-birth', omp_session_sha256=?
            WHERE run_id=?
            """,
            ("a" * 64, prior_claim.run_id),
        )
        job_id = connection.execute(
            "SELECT job_id FROM application_runs WHERE id=?",
            (prior_claim.run_id,),
        ).fetchone()[0]
        connection.execute("UPDATE jobs SET status='queued' WHERE id=?", (job_id,))
        connection.commit()
    finally:
        connection.close()

    states = {301: "live"}
    monkeypatch.setattr(
        db,
        "_process_group_state",
        lambda pid, *, expected=None: states.get(pid, "absent"),
    )
    monkeypatch.setattr(db, "mark_rpc_omp_spawn_attempted", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(db, "update_rpc_run_process", lambda *_args, **_kwargs: True)
    request = _request("run.start", payload=payload)
    try:
        first = await coordinator.handle(request)
        assert first == build_application_response(
            parse_application_request(request),
            ok=False,
            state="failed",
            action_sequence=0,
            event_sequence=0,
            error="unavailable",
        )
        connection = db.connect(db_path)
        try:
            assert db.get_rpc_request(connection, request["request_id"]) is None
            assert connection.execute("SELECT COUNT(*) FROM application_runs").fetchone()[0] == 1
        finally:
            connection.close()

        states[301] = "absent"
        replay = await coordinator.handle(dict(request))
        assert replay["ok"] is True
        assert replay["run_id"] != prior_claim.run_id
        assert process_box
    finally:
        stop.set()
        await coordinator.close()


@_async_test
async def test_start_unavailable_identity_capture_is_rowless_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    stop = asyncio.Event()
    process_box: list[_FakeOmp] = []

    async def workflow(connection, *, claim_provider, control, **_kwargs):
        claim = claim_provider(connection)
        assert claim is not None
        await control.on_claimed(claim.run_id, int(claim.job["id"]), "greenhouse", JOB_URL)
        await stop.wait()

    def process_factory(_config, host_tool_callback):
        process = _FakeOmp(host_tool_callback, mode="park")
        process_box.append(process)
        return process

    config = _config(db_path, artifact_root, resume, process_factory, workflow)
    coordinator = ApplicationRpcCoordinator(config)
    identity = resolve_application_rpc_identity(config)
    request = _request(
        "run.start",
        payload={
            "goal": "prepare_application_draft",
            "job_url": JOB_URL,
            "candidate_profile_id": identity["candidate_profile_id"],
            "configured_resume_id": identity["configured_resume_id"],
            "headed": True,
        },
    )
    original_identity_payload = db._identity_payload
    monkeypatch.setattr(db, "_identity_payload", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(db, "mark_rpc_omp_spawn_attempted", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(db, "update_rpc_run_process", lambda *_args, **_kwargs: True)
    try:
        first = await coordinator.handle(request)
        assert first == build_application_response(
            parse_application_request(request),
            ok=False,
            state="failed",
            action_sequence=0,
            event_sequence=0,
            error="unavailable",
        )
        connection = db.connect(db_path)
        try:
            assert db.get_rpc_request(connection, request["request_id"]) is None
            assert connection.execute("SELECT COUNT(*) FROM application_runs").fetchone()[0] == 0
            assert connection.execute("SELECT status FROM jobs").fetchone()[0] == "queued"
        finally:
            connection.close()

        monkeypatch.setattr(db, "_identity_payload", original_identity_payload)
        second = await coordinator.handle(dict(request))
        assert second["ok"] is True
        assert process_box
    finally:
        stop.set()
        await coordinator.close()



@_async_test
async def test_start_deadline_expiry_rolls_back_no_job_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, artifact_root, resume, _ = _prepare_db(tmp_path)
    coordinator = ApplicationRpcCoordinator(
        _config(
            db_path,
            artifact_root,
            resume,
            lambda *_args, **_kwargs: None,
            lambda *_args, **_kwargs: None,
        )
    )
    identity = resolve_application_rpc_identity(coordinator.config)
    request = _request(
        "run.start",
        payload={
            "goal": "prepare_application_draft",
            "job_url": JOB_URL.replace("/123", "/999"),
            "candidate_profile_id": identity["candidate_profile_id"],
            "configured_resume_id": identity["configured_resume_id"],
            "headed": True,
        },
    )
    original_require = db._require_rpc_deadline_live
    calls = 0

    def expire_before_commit(deadline_unix_ms: int | None) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise db.RpcDeadlineExceeded("expired before rowless commit")
        original_require(deadline_unix_ms)

    monkeypatch.setattr(db, "_require_rpc_deadline_live", expire_before_commit)
    response = await coordinator.handle(request)
    assert response["ok"] is False
    assert response["error"]["code"] == "deadline_exceeded"  # type: ignore[index]
    assert calls >= 2
    connection = db.connect(db_path)
    try:
        info = db.get_rpc_request(connection, request["request_id"])
        assert info is not None and info.state == "completed"
        assert info.response_json is not None
        assert db.parse_application_response(info.response_json, request=None)["error"]["code"] == "deadline_exceeded"  # type: ignore[index]
        assert connection.execute("SELECT COUNT(*) FROM application_runs").fetchone()[0] == 0
    finally:
        connection.close()
    await coordinator.close()

@_async_test
async def test_abandoned_lifecycle_recovery_completes_expired_durable_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, run_id, _process, stop, _ = await _start_parked_run(tmp_path, monkeypatch)
    request_raw = _request("run.cancel", run_id=run_id)
    request_raw["deadline_unix_ms"] = int(time.time() * 1000) + 30
    request = parse_application_request(request_raw)
    connection = db.connect(coordinator.config._db_path)
    try:
        reserved = db.reserve_rpc_request(connection, request=request, run_id=run_id)
        assert reserved.created and reserved.state == "pending"
        assert db.request_rpc_cancellation(
            connection,
            run_id=run_id,
            coordinator_id=coordinator.config._coordinator_id,
        )
    finally:
        connection.close()
    await asyncio.sleep(0.08)

    await coordinator._complete_abandoned_lifecycle(request, "internal_error")

    connection = db.connect(coordinator.config._db_path)
    try:
        info = db.get_rpc_request(connection, request.request_id)
        assert info is not None and info.state == "completed"
        assert info.response_json is not None
        durable = db.parse_application_response(info.response_json, request=request)
        assert durable["ok"] is True
        assert durable["run_id"] == run_id
    finally:
        connection.close()

    replay_request = dict(request_raw)
    replay_request["deadline_unix_ms"] = int(time.time() * 1000) + 60_000
    replay = await coordinator.handle(replay_request)
    assert replay == durable
    conflict_request = dict(replay_request)
    conflict_request["operation"] = "run.resume"
    conflict = await coordinator.handle(conflict_request)
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == "request_conflict"  # type: ignore[index]

    active = coordinator._run_for(run_id)
    assert active is not None
    active.control._cancel_event.set()
    active.control._resume_event.set()
    stop.set()
    await asyncio.sleep(0.05)
    await coordinator.close()
