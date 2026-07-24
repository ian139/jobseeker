from __future__ import annotations

import asyncio
import json
import os
import textwrap
from pathlib import Path
from typing import Any

import pytest
import jobs_assistant.omp_rpc as omp_rpc_module

from jobs_assistant.application_rpc_contracts import (
    BROWSER_HOST_TOOL_DEFINITIONS,
    build_application_response,
    build_rejected_application_response,
)
from jobs_assistant.omp_rpc import (
    FIXED_GUARDED_SYSTEM_PROMPT,
    OmpHostInvocation,
    OmpHostDurabilityError,
    OmpPromptOutcome,
    OmpRpcError,
    OmpRpcCleanupError,
    OmpRpcLaunchConfig,
    OmpRpcProcess,
    OmpRejection,
)


CONTEXT = {
    "protocol_version": 1,
    "run_id": 42,
    "request_id": "123e4567-e89b-12d3-a456-426614174000",
    "deadline_unix_ms": int(__import__("time").time() * 1000) + 240_000,
}


FAKE_TEMPLATE = r'''#!/usr/bin/env python3
import json
import os
import signal
import sys
import time

TOOLS = __TOOLS__
MODE = __MODE__
DUPLICATE_FRAME = None
DUPLICATE_RESULTS = 0
PROMPT_COUNT = 0
LOG = os.path.join(os.environ["HOME"], "child-log.json")

def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()

def log(value):
    try:
        with open(LOG, "w", encoding="utf-8") as f:
            json.dump(value, f, separators=(",", ":"))
    except Exception:
        pass

log({"pid": os.getpid(), "pgid": os.getpgid(0), "cwd": os.getcwd(), "argv": sys.argv, "env": dict(os.environ)})
if MODE == "delayed-readiness":
    time.sleep(30)
emit({"type": "ready"})
for line in sys.stdin:
    try:
        command = json.loads(line)
    except Exception:
        emit({"type": "response", "command": "parse", "success": False, "error": "bad"})
        continue
    kind = command.get("type")
    cid = command.get("id")
    if kind == "set_host_tools":
        if MODE == "delayed-verification":
            time.sleep(30)
        names = [item["name"] for item in TOOLS]
        emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"toolNames": names}})
    elif kind == "get_state":
        state = {
            "model": {"provider": "openai-codex", "id": "gpt-5.6-terra"},
            "thinkingLevel": "xhigh", "isStreaming": False,
            "systemPrompt": ["project wrapper", __SYSTEM_PROMPT__, "footer"],
            "messageCount": 0, "queuedMessageCount": 0, "todoPhases": [], "isCompacting": False,
            "sessionId": "fake-session-42", "dumpTools": TOOLS,
        }
        if MODE == "state-missing-system":
            state.pop("systemPrompt")
        elif MODE == "state-altered-system":
            state["systemPrompt"] = ["project wrapper", "altered prompt", "footer"]
        elif MODE == "state-session-file":
            state["sessionFile"] = "/private/native/session"
        elif MODE == "state-message-count":
            state["messageCount"] = 1
        elif MODE == "state-queue":
            state["queuedMessageCount"] = 1
        elif MODE == "state-todo":
            state["todoPhases"] = ["phase"]
        elif MODE == "state-compacting":
            state["isCompacting"] = True
        emit({
            "type": "response", "id": cid, "command": kind, "success": True,
            "data": state,
        })
        if MODE == "leader-exits-helper":
            helper = os.fork()
            if helper == 0:
                time.sleep(30)
                os._exit(0)
            with open(os.path.join(os.environ["HOME"], "helper.pid"), "w", encoding="utf-8") as f:
                f.write(str(helper))
            os._exit(0)
    elif kind == "prompt":
        if MODE == "local":
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": False}})
        elif MODE == "late-failure":
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": False}})
            emit({"type": "response", "id": cid, "command": kind, "success": False, "error": "late"})
        elif MODE == "malformed-host":
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": True}})
            emit({"type": "host_tool_call", "id": "host-1", "toolCallId": "tool-1", "toolName": "browser.observe", "arguments": {"extra": 1}})
        elif MODE == "two-observes":
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": True}})
            frame = {"type": "host_tool_call", "id": "host-1", "toolCallId": "tool-1", "toolName": "browser.observe", "arguments": {}}
            emit(frame)
            emit({**frame, "id": "host-2", "toolCallId": "tool-2"})
        elif MODE == "two-observes-valid":
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": True}})
            emit({"type": "agent_start"})
            frame = {"type": "host_tool_call", "id": "host-1", "toolCallId": "tool-1", "toolName": "browser.observe", "arguments": {}}
            emit(frame)
            emit({**frame, "id": "host-2", "toolCallId": "tool-2"})
        elif MODE == "action-without-observe":
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": True}})
            emit({"type": "host_tool_call", "id": "host-1", "toolCallId": "tool-1", "toolName": "browser.fill_field", "arguments": {"observation_sha256": "a" * 64, "element_id": "field-1", "value": "Ada", "confidence": 1.0, "reason": "configured"}})
        elif MODE == "early-host":
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": True}})
            observe = {"type": "host_tool_call", "id": "host-early", "toolCallId": "tool-early", "toolName": "browser.observe", "arguments": {}}
            action = {"type": "host_tool_call", "id": "action-early", "toolCallId": "action-early", "toolName": "browser.fill_field", "arguments": {"observation_sha256": "a" * 64, "element_id": "field-1", "value": "Ada", "confidence": 1.0, "reason": "configured"}}
            emit(observe)
            emit(action)
        elif MODE == "tool-invalid":
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": True}})
            emit({"type": "agent_start"})
            emit({"type": "tool_execution_start", "toolCallId": "tool-invalid", "toolName": "evil.native", "args": {}})
        elif MODE == "config-invalid":
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": True}})
            emit({"type": "config_update", "model": {"provider": "evil", "id": "wrong"}, "thinkingLevel": "xhigh"})
        elif MODE == "config-valid":
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": True}})
            emit({"type": "config_update", "model": {"provider": "openai-codex", "id": "gpt-5.6-terra"}, "thinkingLevel": "xhigh"})
            emit({"type": "agent_start"})
            emit({"type": "agent_end", "messages": []})
        elif MODE == "agent-reordered":
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": True}})
            emit({"type": "agent_end", "messages": []})
        elif MODE == "agent-duplicate":
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": True}})
            emit({"type": "agent_start"})
            emit({"type": "agent_start"})
        elif MODE == "tool-events":
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": True}})
            emit({"type": "agent_start"})
            emit({"type": "notice", "level": "info", "message": "benign native notice"})
            emit({"type": "tool_execution_start", "toolCallId": "tool-events-1", "toolName": "browser.observe", "args": {}})
            emit({"type": "host_tool_call", "id": "host-events-1", "toolCallId": "tool-events-1", "toolName": "browser.observe", "arguments": {}})
        elif MODE == "observe-false-action":
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": True}})
            emit({"type": "agent_start"})
            emit({"type": "host_tool_call", "id": "host-false", "toolCallId": "tool-false", "toolName": "browser.observe", "arguments": {}})

        elif MODE == "duplicate-host":
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": True}})
            emit({"type": "agent_start"})
            DUPLICATE_FRAME = {"type": "host_tool_call", "id": "host-duplicate", "toolCallId": "tool-duplicate", "toolName": "browser.observe", "arguments": {}}
            emit(DUPLICATE_FRAME)
        elif MODE == "late-cancel":
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": True}})
            emit({"type": "agent_start"})
            emit({"type": "host_tool_call", "id": "host-late-cancel", "toolCallId": "tool-late-cancel", "toolName": "browser.observe", "arguments": {}})
        elif MODE == "cancel":
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": True}})
            emit({"type": "agent_start"})
            emit({"type": "host_tool_call", "id": "host-1", "toolCallId": "tool-1", "toolName": "browser.observe", "arguments": {}})
            time.sleep(0.15)
            emit({"type": "host_tool_cancel", "id": "cancel-1", "targetId": "host-1"})
        elif MODE in {"cancel-retry", "hung"}:
            PROMPT_COUNT += 1
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": True}})
            emit({"type": "agent_start"})
            if MODE == "cancel-retry" and PROMPT_COUNT > 1:
                emit({"type": "agent_end", "messages": []})
        elif MODE == "secret-output":
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": True}})
            emit({"type": "agent_start"})
            emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "AUTH_SENTINEL"}})
            emit({"type": "agent_end", "messages": [{"role": "assistant", "content": "AUTH_SENTINEL"}]})
        else:
            emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {"agentInvoked": True}})
            emit({"type": "agent_start"})
            emit({"type": "agent_end", "messages": [{"role": "assistant", "content": "private"}]})
    elif kind == "abort":
        emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {}})
        emit({"type": "agent_end", "messages": []})
    elif kind == "host_tool_result":
        if MODE == "tool-events":
            emit({"type": "tool_execution_update", "toolCallId": "tool-events-1", "toolName": "browser.observe", "args": {}, "partialResult": {"content": []}})
            emit({"type": "tool_execution_end", "toolCallId": "tool-events-1", "toolName": "browser.observe", "result": {"content": []}, "isError": False})
            emit({"type": "agent_end", "messages": []})
            continue
        if MODE == "late-cancel":
            emit({"type": "host_tool_cancel", "id": "cancel-late", "targetId": "host-late-cancel"})
            emit({"type": "agent_end", "messages": []})
            continue
        if MODE == "observe-false-action":
            emit({"type": "host_tool_call", "id": "action-after-false", "toolCallId": "action-after-false", "toolName": "browser.fill_field", "arguments": {"observation_sha256": "a" * 64, "element_id": "field-1", "value": "Ada", "confidence": 1.0, "reason": "configured"}})
            continue
        if MODE == "duplicate-host":
            DUPLICATE_RESULTS += 1
            if DUPLICATE_RESULTS == 1:
                emit(DUPLICATE_FRAME)
            else:
                emit({"type": "agent_end", "messages": []})
    else:
        emit({"type": "response", "id": cid, "command": kind, "success": True, "data": {}})


'''
def fake_omp(tmp_path: Path, mode: str = "normal") -> Path:
    path = tmp_path / f"fake-{mode}.py"
    text = (
        FAKE_TEMPLATE
        .replace("__TOOLS__", repr([dict(item) for item in BROWSER_HOST_TOOL_DEFINITIONS]))
        .replace("__MODE__", repr(mode))
        .replace("__SYSTEM_PROMPT__", repr(FIXED_GUARDED_SYSTEM_PROMPT))
    )
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    path.chmod(0o700)
    return path


def config(tmp_path: Path, executable: Path, **overrides: Any) -> OmpRpcLaunchConfig:
    values: dict[str, Any] = {
        "executable": executable,
        "runtime_root": tmp_path / "rpc-root",
        "ready_timeout": 2.0,
        "command_timeout": 0.8,
        "close_timeout": 0.4,
        "max_frame_bytes": 32 * 1024,
        "max_buffer_bytes": 128 * 1024,
    }
    values.update(overrides)
    return OmpRpcLaunchConfig(**values)


def run(coro):
    return asyncio.run(coro)


def _response_for(invocation: OmpHostInvocation, *, ok: bool = True) -> dict[str, object]:
    request = invocation.request
    return build_application_response(
        request,
        ok=ok,
        state="starting" if invocation.tool_name == "browser.observe" else "executing",
        action_sequence=0,
        event_sequence=0,
        result={
            "observation_sha256": "a" * 64,
            "observation_sequence": 1,
            "observed_at": "2026-01-01T00:00:00Z",
            "url": "https://boards.greenhouse.io/acme/jobs/1",
            "ats": "greenhouse",
            "page_type": "application",
            "frame_id": "frame-1",
            "fields": [],
            "controls": [],
            "validation_errors": [],
            "progress": {"step_index": 0, "step_count": 1},
            "blocker_codes": [],
        }
        if invocation.tool_name == "browser.observe"
        else {"outcome": "allowed", "reason_code": None, "observation_sha256": "a" * 64, "changed": False},
        error=None if ok else "action_rejected",
        run_id=request.run_id,
    )


def test_launch_argv_and_environment_are_selective(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path)

    async def exercise() -> None:
        process = await OmpRpcProcess.launch(config(tmp_path, executable))
        try:
            assert process._run_cwd is not None
            assert process._service_home is not None
            assert process._profile_cache is not None
            argv = process._build_argv(process._run_cwd, process._profile_cache)
            env = process._build_child_env(
                process.config.trusted_path,
                process._run_cwd,
                process._service_home,
                process._profile_cache,
            )
            assert argv[:7] == (
                str(executable),
                "--mode",
                "rpc",
                "--model",
                "openai-codex/gpt-5.6-terra",
                "--thinking",
                "xhigh",
            )
            assert "--no-tools" in argv and "--no-session" in argv
            assert "--no-extensions" in argv and "--no-skills" in argv
            assert "--no-rules" in argv and "--no-lsp" in argv
            assert "--auto-approve" in argv
            assert argv[argv.index("--system-prompt") + 1] == FIXED_GUARDED_SYSTEM_PROMPT
            assert set(env) == {
                "PATH",
                "HOME",
                "PI_CONFIG_DIR",
                "XDG_CONFIG_HOME",
                "XDG_CACHE_HOME",
                "XDG_DATA_HOME",
                "XDG_STATE_HOME",
                "TMPDIR",
                "LANG",
                "OMP_AUTH_BROKER_SNAPSHOT_CACHE",
                "OMP_AUTH_BROKER_SNAPSHOT_TTL_SECONDS",
            }
        finally:
            await process.close()

    run(exercise())


def test_explicit_auth_allowlist_is_redacted_and_not_inherited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "AUTH_SENTINEL"
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    monkeypatch.setenv("UNSAFE_SECRET", "ambient-unrelated")
    executable = fake_omp(tmp_path)
    plain = config(tmp_path, executable)
    assert "ambient-secret" not in repr(plain)

    async def exercise() -> None:
        process = await OmpRpcProcess.launch(
            config(
                tmp_path,
                executable,
                auth_env={
                    "OPENAI_API_KEY": sentinel,
                    "OMP_AUTH_BROKER_URL": "https://broker.example.test/token",
                    "OMP_AUTH_BROKER_TOKEN": "broker-token",
                },
            )
        )
        try:
            assert "AUTH_SENTINEL" not in repr(process.config)
            assert process._run_cwd is not None
            assert process._service_home is not None
            assert process._profile_cache is not None
            env = process._build_child_env(
                process.config.trusted_path,
                process._run_cwd,
                process._service_home,
                process._profile_cache,
            )
            assert env["OPENAI_API_KEY"] == sentinel
            assert env["OMP_AUTH_BROKER_TOKEN"] == "broker-token"
            child_log = json.loads(
                (process._service_home / "child-log.json").read_text(encoding="utf-8")
            )
            child_env = child_log["env"]
            assert child_env["OPENAI_API_KEY"] == sentinel
            assert child_env["OMP_AUTH_BROKER_TOKEN"] == "broker-token"
            assert "ambient-secret" not in child_env.values()
            assert "UNSAFE_SECRET" not in child_env
            assert plain.auth_env == {}
            assert "ambient-secret" not in repr(plain)
        finally:
            await process.close()

    run(exercise())


@pytest.mark.parametrize(
    "auth_env",
    [
        {"UNSAFE_SECRET": "x"},
        {"OMP_AUTH_BROKER_URL": "http://remote.example.test"},
        {"OMP_AUTH_BROKER_URL": "https://broker.example.test\nsecret"},
        {"OPENAI_API_KEY": ""},
    ],
)
def test_auth_allowlist_rejects_invalid_values(tmp_path: Path, auth_env: dict[str, str]) -> None:
    executable = fake_omp(tmp_path)
    with pytest.raises(OmpRpcError):
        run(OmpRpcProcess.launch(config(tmp_path, executable, auth_env=auth_env)))


def test_native_output_and_errors_never_echo_model_text_or_auth(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path, "secret-output")
    async def exercise() -> OmpPromptOutcome:
        process = await OmpRpcProcess.launch(config(tmp_path, executable, auth_env={"OPENAI_API_KEY": "AUTH_SENTINEL"}))
        try:
            return await process.prompt("safe", CONTEXT)
        finally:
            await process.close()
    outcome = run(exercise())
    assert "AUTH_SENTINEL" not in repr(outcome)


def test_prompt_requires_explicit_host_context(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path)
    async def exercise() -> None:
        process = await OmpRpcProcess.launch(config(tmp_path, executable))
        try:
            with pytest.raises(OmpRpcError):
                await process.prompt("missing context")
            assert not process._pending
        finally:
            await process.close()
    run(exercise())


def test_sequential_prompts_use_distinct_monotonic_ids(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path, "normal")
    async def exercise() -> tuple[OmpPromptOutcome, OmpPromptOutcome]:
        process = await OmpRpcProcess.launch(config(tmp_path, executable))
        try:
            first = await process.prompt("first", CONTEXT)
            second = await process.prompt("second", CONTEXT)
            return first, second
        finally:
            await process.close()
    first, second = run(exercise())
    assert first.child_request_id != second.child_request_id


def test_permissions_and_symlink_rejection(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path)
    bad = tmp_path / "bad-root"
    bad.mkdir()
    bad.chmod(0o755)
    with pytest.raises(OmpRpcError):
        run(OmpRpcProcess.launch(config(tmp_path, executable, runtime_root=bad)))
    link = tmp_path / "link"
    link.symlink_to(executable)
    with pytest.raises(OmpRpcError):
        run(OmpRpcProcess.launch(config(tmp_path, link)))

def test_trusted_path_rejects_foreign_owner_via_narrow_uid_patch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    trusted.chmod(0o700)
    real_uid = os.getuid()
    monkeypatch.setattr(omp_rpc_module.os, "getuid", lambda: real_uid + 1)
    with pytest.raises(OmpRpcError):
        omp_rpc_module._validate_trusted_path((trusted,))


def test_executable_rejects_writable_ancestor_chain(tmp_path: Path) -> None:
    parent = tmp_path / "writable-parent"
    parent.mkdir()
    executable = fake_omp(parent)
    parent.chmod(0o777)
    try:
        with pytest.raises(OmpRpcError):
            run(OmpRpcProcess.launch(config(tmp_path, executable)))
    finally:
        parent.chmod(0o700)


def test_close_removes_explicit_profile_and_all_transient_directories(
    tmp_path: Path,
) -> None:
    executable = fake_omp(tmp_path)
    runtime_root = tmp_path / "rpc-root"
    profile_parent = runtime_root / "profiles"

    async def exercise() -> tuple[Path, Path, Path]:
        process = await OmpRpcProcess.launch(
            config(
                tmp_path,
                executable,
                runtime_root=runtime_root,
                profile_cache=profile_parent,
            )
        )
        run_cwd = process._run_cwd
        service_home = process._service_home
        profile_cache = process._profile_cache
        assert run_cwd is not None and service_home is not None
        assert profile_cache is not None
        await process.close()
        return run_cwd, service_home, profile_cache

    run_cwd, service_home, profile_cache = run(exercise())
    assert not run_cwd.exists()
    assert not service_home.exists()
    assert not profile_cache.exists()
    assert profile_parent.is_dir() and not tuple(profile_parent.iterdir())


def test_failed_launch_removes_transient_private_directories(tmp_path: Path) -> None:
    executable = tmp_path / "fake-no-ready.py"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o700)
    runtime_root = tmp_path / "rpc-root"
    profile_parent = runtime_root / "profiles"
    with pytest.raises(OmpRpcError):
        run(
            OmpRpcProcess.launch(
                config(
                    tmp_path,
                    executable,
                    runtime_root=runtime_root,
                    profile_cache=profile_parent,
                )
            )
        )

    service_parent = runtime_root / "service-home"
    assert profile_parent.is_dir() and not tuple(profile_parent.iterdir())
    assert service_parent.is_dir() and not tuple(service_parent.iterdir())
    assert not tuple(path for path in runtime_root.iterdir() if path.name.startswith(".run-"))

@pytest.mark.parametrize("mode", ["delayed-readiness", "delayed-verification"])
def test_cancelled_launch_kills_new_session_and_removes_private_dirs(
    tmp_path: Path,
    mode: str,
) -> None:
    executable = fake_omp(tmp_path, mode)
    runtime_root = tmp_path / "rpc-root"

    async def exercise() -> tuple[int, int, Path, Path, Path]:
        task = asyncio.create_task(
            OmpRpcProcess.launch(
                config(tmp_path, executable, runtime_root=runtime_root, ready_timeout=30.0),
            )
        )
        child_log: Path | None = None
        try:
            deadline = asyncio.get_running_loop().time() + 3.0
            while asyncio.get_running_loop().time() < deadline:
                candidates = tuple(runtime_root.rglob("child-log.json")) if runtime_root.exists() else ()
                if candidates:
                    child_log = candidates[0]
                    break
                await asyncio.sleep(0.01)
            assert child_log is not None
            record = json.loads(child_log.read_text(encoding="utf-8"))
            env = record["env"]
            pid = int(record["pid"])
            pgid = int(record["pgid"])
            run_cwd = Path(env["TMPDIR"])
            service_home = Path(env["HOME"])
            profile_cache = Path(env["PI_CONFIG_DIR"])
            assert run_cwd.exists() and service_home.exists() and profile_cache.exists()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
            return pid, pgid, run_cwd, service_home, profile_cache
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    pid, pgid, run_cwd, service_home, profile_cache = run(exercise())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)
    assert not run_cwd.exists()
    assert not service_home.exists()
    assert not profile_cache.exists()
    assert not tuple(path for path in runtime_root.iterdir() if path.name.startswith(".run-"))

def test_cancellation_during_subprocess_creation_kills_spawned_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = fake_omp(tmp_path, "delayed-readiness")
    runtime_root = tmp_path / "rpc-root"
    real_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    release = asyncio.Event()
    child: asyncio.subprocess.Process | None = None

    async def delayed_spawn(*args: object, **kwargs: object):
        nonlocal child
        child = await real_spawn(*args, **kwargs)
        spawned.set()
        await release.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)

    async def exercise() -> int:
        task = asyncio.create_task(
            OmpRpcProcess.launch(
                config(
                    tmp_path,
                    executable,
                    runtime_root=runtime_root,
                    ready_timeout=30.0,
                )
            )
        )
        await asyncio.wait_for(spawned.wait(), timeout=3.0)
        assert child is not None and child.pid is not None
        pid = int(child.pid)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
        return pid

    pid = run(exercise())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not tuple(
        path
        for path in runtime_root.iterdir()
        if path.name.startswith(".run-")
    )


def test_spawn_callback_is_durable_boundary_before_readiness(
    tmp_path: Path,
) -> None:
    executable = fake_omp(tmp_path, "delayed-readiness")
    captured: list[dict[str, object]] = []

    def reject_registration(identity) -> None:
        captured.append(dict(identity))
        raise RuntimeError("registration failed")

    with pytest.raises(RuntimeError, match="registration failed"):
        run(
            OmpRpcProcess.launch(
                config(tmp_path, executable, ready_timeout=30.0),
                on_spawn=reject_registration,
            )
        )
    assert len(captured) == 1
    assert set(captured[0]) == {"pid", "pgid", "birth"}
    with pytest.raises(ProcessLookupError):
        os.kill(int(captured[0]["pid"]), 0)


def test_launch_cleanup_failure_is_not_reported_as_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = fake_omp(tmp_path)

    async def fail_start(_self) -> None:
        raise RuntimeError("start failed")

    async def fail_close(_self) -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(OmpRpcProcess, "_start", fail_start)
    monkeypatch.setattr(OmpRpcProcess, "close", fail_close)
    with pytest.raises(OmpRpcCleanupError):
        run(OmpRpcProcess.launch(config(tmp_path, executable)))


def test_host_durability_failure_poison_closes_without_native_result(
    tmp_path: Path,
) -> None:
    executable = fake_omp(tmp_path, "tool-events")

    async def callback(_invocation):
        raise OmpHostDurabilityError("no durable response")

    async def exercise() -> list[dict[str, object]]:
        process = await OmpRpcProcess.launch(
            config(tmp_path, executable),
            host_tool_callback=callback,
        )
        writes: list[dict[str, object]] = []
        original_write = process._write

        async def capture(payload):
            writes.append(dict(payload))
            await original_write(payload)

        process._write = capture
        with pytest.raises(OmpRpcError):
            await process.prompt(
                "prepare draft",
                CONTEXT,
                timeout=1.0,
            )
        for _ in range(100):
            if process.closed:
                break
            await asyncio.sleep(0.01)
        if not process.closed:
            await process.close()
        assert process.closed
        return writes

    writes = run(exercise())
    assert not any(
        item.get("type") == "host_tool_result"
        for item in writes
    )


@pytest.mark.parametrize(
    "mode",
    ["state-missing-system", "state-altered-system", "state-session-file", "state-message-count", "state-queue", "state-todo", "state-compacting"],
)
def test_startup_state_requires_guard_prompt_and_empty_native_queues(tmp_path: Path, mode: str) -> None:
    executable = fake_omp(tmp_path, mode)
    with pytest.raises(OmpRpcError):
        run(OmpRpcProcess.launch(config(tmp_path, executable)))


def test_exact_registry_model_and_hashed_state(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path)

    async def exercise() -> None:
        process = await OmpRpcProcess.launch(config(tmp_path, executable))
        try:
            assert process.verified
            assert process.verification is not None
            assert len(process.session_identity_sha256 or "") == 64
            assert "sessionFile" not in process.safe_state
            assert "systemPrompt" not in process.safe_state
            assert "dumpTools" not in process.safe_state
        finally:
            await process.close()

    run(exercise())


@pytest.mark.parametrize("mode", ["malformed-host", "two-observes", "action-without-observe"])
def test_host_rejection_is_safe_and_poisoned(tmp_path: Path, mode: str) -> None:
    executable = fake_omp(tmp_path, mode)
    evidence: list[OmpRejection] = []
    async def exercise() -> None:
        process = await OmpRpcProcess.launch(config(tmp_path, executable), on_rejection=evidence.append)
        with pytest.raises(OmpRpcError):
            await process.prompt("safe prompt", CONTEXT, timeout=0.6)
        await process.close()
    run(exercise())
    assert evidence
    assert all(len(item.raw_frame_sha256) == 64 for item in evidence)
    assert all(item.parent_request_id == CONTEXT["request_id"] for item in evidence)
    assert all(not hasattr(item, "arguments") for item in evidence)


def test_valid_transport_rejection_uses_durable_callback_response(
    tmp_path: Path,
) -> None:
    executable = fake_omp(tmp_path, "two-observes-valid")
    calls: list[OmpHostInvocation] = []

    async def callback(
        invocation: OmpHostInvocation,
    ) -> dict[str, object]:
        calls.append(invocation)
        if invocation.transport_rejection_code is not None:
            return build_rejected_application_response(
                invocation.request.to_mapping(),
                error=invocation.transport_rejection_code,
            )
        return _response_for(invocation)

    async def exercise() -> None:
        process = await OmpRpcProcess.launch(
            config(tmp_path, executable),
            host_tool_callback=callback,
        )
        with pytest.raises(OmpRpcError):
            await process.prompt(
                "reject duplicate observe",
                CONTEXT,
                timeout=0.6,
            )
        await process.close()

    run(exercise())
    assert len(calls) == 2
    assert sum(
        item.transport_rejection_code == "action_rejected"
        for item in calls
    ) == 1


@pytest.mark.parametrize("mode", ["early-host", "tool-invalid", "agent-reordered", "agent-duplicate"])
def test_out_of_turn_frames_poison_without_host_callbacks(tmp_path: Path, mode: str) -> None:
    executable = fake_omp(tmp_path, mode)
    calls: list[OmpHostInvocation] = []

    async def exercise() -> None:
        process = await OmpRpcProcess.launch(config(tmp_path, executable), host_tool_callback=calls.append)
        try:
            with pytest.raises(OmpRpcError):
                await process.prompt("safe", CONTEXT, timeout=0.6)
        finally:
            await process.close()

    run(exercise())
    assert calls == []


def test_failed_observe_cannot_authorize_action(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path, "observe-false-action")
    calls: list[OmpHostInvocation] = []

    async def callback(invocation: OmpHostInvocation) -> dict[str, object]:
        calls.append(invocation)
        return _response_for(invocation, ok=False)

    async def exercise() -> None:
        process = await OmpRpcProcess.launch(config(tmp_path, executable), host_tool_callback=callback)
        try:
            with pytest.raises(OmpRpcError):
                await process.prompt("observe", CONTEXT, timeout=0.8)
        finally:
            await process.close()

    run(exercise())
    assert [item.tool_name for item in calls] == ["browser.observe"]

def test_callback_receives_typed_invocation_and_local_completion(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path, "local")
    calls: list[OmpHostInvocation] = []
    async def exercise() -> OmpPromptOutcome:
        process = await OmpRpcProcess.launch(config(tmp_path, executable), host_tool_callback=lambda item: calls.append(item))
        try:
            return await process.prompt("slash command", CONTEXT)
        finally:
            await process.close()
    outcome = run(exercise())
    assert outcome.agent_invoked is False
    assert outcome.completed and not calls


def test_native_tool_execution_events_do_not_poison_host_tool_call(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path, "tool-events")
    calls: list[OmpHostInvocation] = []

    async def callback(invocation: OmpHostInvocation) -> dict[str, object]:
        calls.append(invocation)
        return _response_for(invocation)


    async def exercise() -> OmpPromptOutcome:
        process = await OmpRpcProcess.launch(
            config(tmp_path, executable),
            host_tool_callback=callback,
        )
        try:
            return await process.prompt("observe once", CONTEXT)
        finally:
            await process.close()

    outcome = run(exercise())
    assert outcome.completed
    assert [call.tool_name for call in calls] == ["browser.observe"]

def test_config_update_requires_pinned_model_and_thinking(tmp_path: Path) -> None:
    invalid = fake_omp(tmp_path, "config-invalid")

    async def invalid_exercise() -> None:
        process = await OmpRpcProcess.launch(config(tmp_path, invalid))
        try:
            with pytest.raises(OmpRpcError):
                await process.prompt("safe", CONTEXT)
        finally:
            await process.close()

    run(invalid_exercise())

    valid = fake_omp(tmp_path, "config-valid")

    async def valid_exercise() -> OmpPromptOutcome:
        process = await OmpRpcProcess.launch(config(tmp_path, valid))
        try:
            return await process.prompt("safe", CONTEXT)
        finally:
            await process.close()

    assert run(valid_exercise()).completed

def test_command_prompt_and_invalid_timeout_leave_no_state_and_safe_retry_works(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path)

    async def exercise() -> OmpPromptOutcome:
        process = await OmpRpcProcess.launch(config(tmp_path, executable))
        try:
            with pytest.raises(OmpRpcError):
                await process.prompt(" \n\t /local command", CONTEXT)
            assert process._prompt is None and not process._prompt_schedule_ids and not process._pending
            with pytest.raises(OmpRpcError):
                await process.prompt("safe", CONTEXT, timeout=0)
            assert process._prompt is None and not process._prompt_schedule_ids and not process._pending
            return await process.prompt("safe", CONTEXT)
        finally:
            await process.close()

    outcome = run(exercise())
    assert outcome.completed


def test_completed_host_call_replay_returns_cached_result_without_callback_reentry(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path, "duplicate-host")
    calls: list[OmpHostInvocation] = []

    async def callback(invocation: OmpHostInvocation) -> dict[str, object]:
        calls.append(invocation)
        return _response_for(invocation)

    async def exercise() -> OmpPromptOutcome:
        process = await OmpRpcProcess.launch(
            config(tmp_path, executable),
            host_tool_callback=callback,
        )
        try:
            return await process.prompt("observe once", CONTEXT)
        finally:
            await process.close()

    outcome = run(exercise())
    assert outcome.completed and len(calls) == 1


def test_late_cancel_for_completed_host_call_is_idempotent(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path, "late-cancel")
    calls: list[OmpHostInvocation] = []

    async def callback(invocation: OmpHostInvocation) -> dict[str, object]:
        calls.append(invocation)
        return _response_for(invocation)

    async def exercise() -> tuple[OmpPromptOutcome, bool]:
        process = await OmpRpcProcess.launch(
            config(tmp_path, executable),
            host_tool_callback=callback,
        )
        try:
            outcome = await process.prompt("observe once", CONTEXT)
            return outcome, process.poisoned
        finally:
            await process.close()

    outcome, poisoned = run(exercise())
    assert outcome.completed
    assert poisoned is False
    assert [call.tool_call_id for call in calls] == ["tool-late-cancel"]


def test_early_rejected_host_task_is_removed_from_registry(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path)
    frame = {
        "type": "host_tool_call",
        "id": "host-early",
        "toolCallId": "tool-early",
        "toolName": "browser.observe",
        "arguments": {},
    }

    async def exercise() -> bool:
        process = await OmpRpcProcess.launch(config(tmp_path, executable))
        try:
            process._spawn_host_call(frame)
            task = next(iter(process._host_tasks.values()))
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
            await asyncio.sleep(0)
            return not process._host_tasks
        finally:
            await process.close()

    assert run(exercise())


def test_close_drops_buffered_host_frame_after_host_drain(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path)
    frame = {
        "type": "host_tool_call",
        "id": "host-late",
        "toolCallId": "tool-late",
        "toolName": "browser.observe",
        "arguments": {},
    }

    async def exercise() -> tuple[bool, list[dict[str, object]]]:
        process = await OmpRpcProcess.launch(config(tmp_path, executable))
        started: list[dict[str, object]] = []

        async def fake_host_task(value: dict[str, object]) -> None:
            started.append(value)

        setattr(process, "_handle_host_call", fake_host_task)
        original_drain = process._drain_host_tasks

        async def drain_then_deliver(*, timeout: float | None = None) -> bool:
            drained = await original_drain(timeout=timeout)
            # A reader can deliver this buffered frame after its first drain
            # snapshot.  Shutdown must have closed admission before this point.
            process._spawn_host_call(frame)
            return drained

        setattr(process, "_drain_host_tasks", drain_then_deliver)
        try:
            await process.close()
            await asyncio.sleep(0)
            return process.closed and not process._host_tasks, started
        finally:
            await process.close()

    clean, started = run(exercise())
    assert clean
    assert started == []


def test_native_agent_completion_does_not_expose_text(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path, "normal")
    async def exercise() -> OmpPromptOutcome:
        process = await OmpRpcProcess.launch(config(tmp_path, executable))
        try:
            return await process.prompt("draft", CONTEXT)
        finally:
            await process.close()
    outcome = run(exercise())
    assert outcome.agent_invoked is True
    assert not hasattr(outcome, "text")
    assert "private" not in repr(outcome)


def test_pre_dispatch_cancel_wins_and_post_dispatch_is_soft(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path, "cancel")
    seen: list[OmpHostInvocation] = []
    async def callback(invocation: OmpHostInvocation):
        seen.append(invocation)
        await invocation.cancellation_event.wait()
        return _response_for(invocation)
    async def exercise() -> OmpPromptOutcome:
        process = await OmpRpcProcess.launch(config(tmp_path, executable), host_tool_callback=callback)
        try:
            return await process.prompt("cancel", CONTEXT, timeout=0.7)
        finally:
            await process.close()
    outcome = run(exercise())
    assert outcome.cancelled
    assert seen and seen[0].cancelled and not seen[0].dispatched


def test_task_cancellation_cleans_prompt_and_allows_safe_retry(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path, "cancel-retry")

    async def exercise() -> OmpPromptOutcome:
        process = await OmpRpcProcess.launch(config(tmp_path, executable))
        try:
            task = asyncio.create_task(process.prompt("first", CONTEXT, timeout=5.0))
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert process._prompt is None and not process._prompt_schedule_ids
            return await process.prompt("safe retry", CONTEXT, timeout=1.0)
        finally:
            await process.close()

    outcome = run(exercise())
    assert outcome.completed


def test_close_settles_hung_prompt_without_waiting_for_original_timeout(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path, "hung")

    async def exercise() -> OmpPromptOutcome:
        process = await OmpRpcProcess.launch(config(tmp_path, executable, close_timeout=0.2))
        task = asyncio.create_task(process.prompt("hung", CONTEXT, timeout=30.0))
        try:
            await asyncio.sleep(0.1)
            await asyncio.wait_for(process.close(), timeout=1.0)
            return await asyncio.wait_for(task, timeout=0.5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await process.close()

    outcome = run(exercise())
    assert outcome.cancelled

def test_close_remains_bounded_when_background_task_suppresses_cancellation(
    tmp_path: Path,
) -> None:
    executable = fake_omp(tmp_path)

    async def exercise() -> None:
        process = await OmpRpcProcess.launch(
            config(tmp_path, executable, close_timeout=0.05)
        )
        original_reader = process._reader_task
        assert original_reader is not None
        original_reader.cancel()
        await asyncio.gather(original_reader, return_exceptions=True)
        release = asyncio.Event()

        async def stubborn_reader() -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()

        stubborn_task = asyncio.create_task(stubborn_reader())
        process._reader_task = stubborn_task
        try:
            await asyncio.wait_for(process.close(), timeout=1.0)
            assert process.closed
            assert not stubborn_task.done()
        finally:
            release.set()
            await asyncio.gather(stubborn_task, return_exceptions=True)

    run(exercise())


def test_group_cleanup_is_bounded_and_idempotent(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path)
    async def exercise() -> tuple[int | None, int | None, bool]:
        process = await OmpRpcProcess.launch(config(tmp_path, executable))
        pid, pgid = process.pid, process.pgid
        await process.close()
        await process.close()
        return pid, pgid, process.closed
    pid, pgid, closed = run(exercise())
    assert pid and pgid and pgid != os.getpgrp() and closed
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

def test_close_kills_same_group_helper_after_leader_exit(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path, "leader-exits-helper")

    async def exercise() -> int:
        process = await OmpRpcProcess.launch(config(tmp_path, executable))
        assert process._service_home is not None
        pid_file = process._service_home / "helper.pid"
        helper_pid: int | None = None
        for _ in range(100):
            if pid_file.exists():
                helper_pid = int(pid_file.read_text(encoding="utf-8"))
                break
            await asyncio.sleep(0.01)
        assert helper_pid is not None
        await process.close()
        return helper_pid

    helper_pid = run(exercise())
    for _ in range(100):
        try:
            os.kill(helper_pid, 0)
        except ProcessLookupError:
            break
        __import__("time").sleep(0.01)
    else:
        pytest.fail("same-process-group helper survived close")
    with pytest.raises(ProcessLookupError):
        os.kill(helper_pid, 0)


def test_invalid_profile_and_proxy_do_not_start_child(tmp_path: Path) -> None:
    executable = fake_omp(tmp_path)
    with pytest.raises(OmpRpcError):
        run(OmpRpcProcess.launch(config(tmp_path, executable, profile="../unsafe")))
    with pytest.raises(OmpRpcError):
        run(OmpRpcProcess.launch(config(tmp_path, executable, proxy_env={"HTTPS_PROXY": "https://user:secret@example.test"})))
