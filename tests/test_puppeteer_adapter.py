from __future__ import annotations

import base64
import http.server
import hashlib
import json
import os
import signal
import selectors
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from jobs_assistant.browser_adapter import (
    BrowserAdapterError,
    PuppeteerSession,
    _read_one_frame,
    _capture_process_identity,
    normalize_browser_error_code,
    validate_ats_url,
    validate_greenhouse_url,
)




FIXTURE = Path(__file__).with_name("fixtures") / "puppeteer_adapter" / "greenhouse_guarded.html"
LEVER_FIXTURE = Path(__file__).with_name("fixtures") / "puppeteer_adapter" / "lever_guarded.html"
CLEAN_FIXTURE = b"""<!doctype html>
<html><head><title>Greenhouse Clean Fixture</title></head>
<body><form>
<label>First Name <input name="first_name" autocomplete="given-name" required></label>
<label>Resume <input name="resume" type="file" accept="application/pdf,.pdf"></label>
<button type="button" id="offline">Continue</button>
<a id="human-review-link" href="?gh_src=human-review">Continue to guarded local review</a>
<button type="submit" id="submit-final">Submit Application</button>
</form>
<script>window.addEventListener("input",()=>{fetch("/exfil-after-input").catch(()=>{});const image=new Image();image.src="http://attacker.invalid/leak"});</script>
</body></html>"""
SUBMIT_CONTINUATION_FIXTURE = b"""<!doctype html>
<html><head><title>Greenhouse Submit Continuation Fixture</title></head>
<body>
<button type="submit" id="continue-native">Continue</button>
<button type="submit" id="submit-final">Submit Application</button>
<script>document.getElementById("continue-native").addEventListener("click",()=>{document.body.dataset.continued="yes"});</script>
</body></html>"""
BLOCKER_FIXTURE = b"""<!doctype html><form>
<label>First Name <input name="first_name" required></label>
<button type="button" id="offline">Continue</button>
<div id="blocker">CAPTCHA authentication assessment required</div>
</form>"""

class FixtureHandler(http.server.SimpleHTTPRequestHandler):
    attacker_http_requests = 0
    final_like_requests = 0
    fixture_requests = 0
    benign_requests = 0
    logical_urls: list[str] = []

    def do_GET(self):  # noqa: N802
        type(self).fixture_requests += 1
        encoded_logical = self.headers.get("x-jobs-assistant-logical-url")
        if encoded_logical:
            try:
                padding = "=" * (-len(encoded_logical) % 4)
                logical_url = base64.urlsafe_b64decode(encoded_logical + padding).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                logical_url = ""
            type(self).logical_urls.append(logical_url)
        if self.path.startswith("/redirect-cross"):
            self.send_response(302)
            self.send_header("Location", "https://boards.greenhouse.io/other/jobs/999")
            self.end_headers()
            return
        if self.path.startswith("/redirect-loop"):
            self.send_response(302)
            self.send_header("Location", "https://boards.greenhouse.io/fixture/jobs/123")
            self.end_headers()
            return
        if self.path.startswith("/overflow-chunked") or self.path.startswith("/overflow-lying"):
            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("content-type", "text/html")
            if self.path.startswith("/overflow-chunked"):
                self.send_header("transfer-encoding", "chunked")
            else:
                self.send_header("content-length", "1")
            self.end_headers()
            remaining = 20 * 1024 * 1024 + 1
            chunk = b"x" * (64 * 1024)
            while remaining:
                piece = chunk[: min(len(chunk), remaining)]
                if self.path.startswith("/overflow-chunked"):
                    self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                else:
                    self.wfile.write(piece)
                remaining -= len(piece)
            if self.path.startswith("/overflow-chunked"):
                self.wfile.write(b"0\r\n\r\n")
            return
        if self.path.startswith("/captcha") or self.path.startswith("/auth") or self.path.startswith("/assessment") or self.path.startswith("/validation"):
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.end_headers()
            text = {
                "/captcha": "CAPTCHA required",
                "/auth": "Authentication required",
                "/assessment": "Assessment required",
                "/validation": "Please correct this field",
            }.get(self.path.split("?", 1)[0], "CAPTCHA required")
            body = (b'<!doctype html><form><input name="first_name"><div role="alert">Please correct this field</div></form>'
                    if self.path.startswith("/validation")
                    else BLOCKER_FIXTURE.replace(b"CAPTCHA authentication assessment required", text.encode()))
            self.wfile.write(body)
            return
        if "attacker" in self.path or "exfil" in self.path:
            type(self).attacker_http_requests += 1
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"attacker fixture path reached")
            return
        if any(token in self.path for token in ("submit", "complete", "confirm", "finish")):
            type(self).final_like_requests += 1
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"final fixture path reached")
            return
        type(self).benign_requests += 1
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.end_headers()
        logical = type(self).logical_urls[-1] if type(self).logical_urls else ""
        body = (
            LEVER_FIXTURE.read_bytes()
            if logical.startswith("https://jobs.lever.co/")
            else SUBMIT_CONTINUATION_FIXTURE if self.path.startswith("/continue-native")
            else CLEAN_FIXTURE if self.path.startswith("/clean") else FIXTURE.read_bytes()
        )
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        return

@pytest.fixture()
def fixture_server():
    FixtureHandler.attacker_http_requests = 0
    FixtureHandler.final_like_requests = 0
    FixtureHandler.fixture_requests = 0
    FixtureHandler.benign_requests = 0
    FixtureHandler.logical_urls = []
    with socketserver.TCPServer(("127.0.0.1", 0), FixtureHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}/clean"
        finally:
            server.shutdown()
            thread.join(timeout=5)


def field_by_name(observation: dict, name: str) -> dict:
    return next(field for field in observation["fields"] if field.get("name") == name)

def _validated_emergency_cleanup(identities: dict, manifest: Path) -> None:
    """Kill only the released owner group after matching its recorded identities."""
    try:
        current = json.loads(manifest.read_text(encoding="utf-8"))
        owner = identities["owner"]
        browser = identities["browser"]
        fields = {"pid", "pgid", "birth"}
        for identity in (owner, browser):
            if not isinstance(identity, dict) or set(identity) != fields:
                return
            if type(identity["pid"]) is not int or identity["pid"] <= 0:
                return
            if type(identity["pgid"]) is not int or identity["pgid"] <= 0:
                return
            if type(identity["birth"]) is not str or not identity["birth"] or len(identity["birth"]) > 256:
                return
        if current.get("state") != "open_guarded":
            return
        if current.get("owner_identity") != owner or current.get("browser_identity") != browser:
            return
        if owner["pid"] == os.getpid() or owner["pgid"] == os.getpgrp() or owner["pgid"] != browser["pgid"]:
            return
        if _capture_process_identity(owner["pid"]) != owner:
            return
        if _capture_process_identity(browser["pid"]) != browser:
            return
        os.killpg(owner["pgid"], signal.SIGTERM)
        time.sleep(0.25)
        if _capture_process_identity(owner["pid"]) == owner:
            os.killpg(owner["pgid"], signal.SIGKILL)
    except (KeyError, TypeError, ValueError, OSError, AttributeError, json.JSONDecodeError):
        return

BROWSER_INTEGRATION_SKIP = pytest.mark.skipif(
    os.environ.get("RUN_PUPPETEER_INTEGRATION") != "1"
    and os.environ.get("RUN_PUPPETEER_HEADED_SMOKE") != "1",
    reason="set RUN_PUPPETEER_INTEGRATION=1 or RUN_PUPPETEER_HEADED_SMOKE=1",
)

def test_greenhouse_url_validation_accepts_only_structural_routes():
    assert validate_greenhouse_url("https://boards.greenhouse.io/acme/jobs/123").mode == "greenhouse_job"
    assert validate_greenhouse_url("https://job-boards.greenhouse.io/acme/jobs/123?gh_src=abc").mode == "greenhouse_job"
    assert validate_greenhouse_url("https://boards.greenhouse.io/embed/job_app?for=acme-team&token=123").mode == "greenhouse_embed"

    for url in [
        "http://boards.greenhouse.io/acme/jobs/123",
        "https://boards.greenhouse.io/acme/jobs/submit",
        "https://boards.greenhouse.io/acme/jobs/123?next=submit",
        "https://evil.example/acme/jobs/123",
        "https://boards.greenhouse.io/acme/jobs",
        "https://user:pass@boards.greenhouse.io/acme/jobs/123",
    ]:
        with pytest.raises(BrowserAdapterError):
            validate_greenhouse_url(url)


def test_lever_url_validation_is_exact_and_apply_scoped():
    uuid_url = "https://jobs.eu.lever.co/acme/123e4567-e89b-12d3-a456-426614174000"
    assert validate_ats_url(uuid_url, "lever").mode == "lever_job"
    assert validate_ats_url(uuid_url + "/apply", "lever").mode == "lever_apply"
    assert LEVER_FIXTURE.is_file()
    for url in [
        "http://jobs.eu.lever.co/acme/123e4567-e89b-12d3-a456-426614174000",
        uuid_url + "?next=confirm",
        "https://jobs.eu.lever.co/acme/job-123",
        "https://jobs.eu.lever.co/acme/123E4567-E89B-12D3-A456-426614174000",
        uuid_url + "/confirmation",
        "https://jobs.lever.co.evil.example/acme/123e4567-e89b-12d3-a456-426614174000",
        "https://user:pass@jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000",
    ]:
        with pytest.raises(BrowserAdapterError):
            validate_ats_url(url, "lever")


def _resolver_frames(*, addresses: list[object]) -> dict:
    process = subprocess.Popen(
        ["node", "src/jobs_assistant/puppeteer_runner.js"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(Path.cwd()),
    )
    assert process.stdin is not None and process.stdout is not None
    hello_length = int(process.stdout.readline())
    process.stdout.read(hello_length)

    def send(payload: dict) -> dict:
        body = json.dumps(payload, separators=(",", ":")).encode()
        process.stdin.write(str(len(body)).encode() + b"\n" + body)
        process.stdin.flush()
        length = int(process.stdout.readline())
        return json.loads(process.stdout.read(length))

    response = send({"action": "classifyResolverResult", "addresses": addresses})
    send({"action": "close"})
    process.wait(timeout=5)
    return response
def test_global_unicast_resolver_classifier_rejects_special_and_accepts_safe() -> None:
    vectors = [
        ("127.0.0.1", 4, False),
        ("100.64.0.1", 4, False),
        ("0.1.2.3", 4, False),
        ("224.0.0.1", 4, False),
        ("192.0.2.1", 4, False),
        ("198.18.0.1", 4, False),
        ("203.0.113.1", 4, False),
        ("2001:db8::1", 6, False),
        ("::ffff:127.0.0.1", 6, False),
        ("fc00::1", 6, False),
        ("fe80::1", 6, False),
        ("ff02::1", 6, False),
        ("8.8.8.8", 4, True),
        ("2001:4860:4860::8888", 6, True),
    ]
    for address, family, accepted in vectors:
        response = _resolver_frames(addresses=[{"address": address, "family": family}])
        assert response["ok"] is accepted, (address, response)


def test_global_unicast_resolver_classifier_rejects_invalid_and_rebinding_second_answer() -> None:
    invalid = _resolver_frames(addresses=[{"address": "not-an-ip", "family": 4}])
    assert invalid == {"ok": False, "error": "resolver_address_rejected"}
    rebinding = _resolver_frames(
        addresses=[
            {"address": "8.8.8.8", "family": 4},
            {"address": "127.0.0.1", "family": 4},
        ]
    )
    assert rebinding == {"ok": False, "error": "resolver_address_rejected"}

def test_injected_resolver_is_called_once_and_pins_one_validated_result() -> None:
    env = os.environ.copy()
    env["JOBS_ASSISTANT_TEST_RESOLVER_JSON"] = json.dumps({
        "safe.example": [{"address": "8.8.8.8", "family": 4}],
        "rebind.example": [
            {"address": "8.8.8.8", "family": 4},
            {"address": "127.0.0.1", "family": 4},
        ],
    })
    process = subprocess.Popen(
        ["node", "src/jobs_assistant/puppeteer_runner.js"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(Path.cwd()),
        env=env,
    )
    assert process.stdin is not None and process.stdout is not None
    hello_length = int(process.stdout.readline())
    process.stdout.read(hello_length)

    def send(payload: dict) -> dict:
        body = json.dumps(payload, separators=(",", ":")).encode()
        process.stdin.write(str(len(body)).encode() + b"\n" + body)
        process.stdin.flush()
        length = int(process.stdout.readline())
        return json.loads(process.stdout.read(length))

    assert send({"action": "resolvePinnedAddress", "hostname": "safe.example"})["data"] == {"address": "8.8.8.8", "family": 4}
    assert send({"action": "networkCounters"})["data"]["dnsLookups"] == 1
    assert send({"action": "resolvePinnedAddress", "hostname": "rebind.example"})["ok"] is False
    assert send({"action": "networkCounters"})["data"]["dnsLookups"] == 2
    send({"action": "close"})
    process.wait(timeout=5)

def _start_test_proxy(*, logical_url: str, resolver: dict[str, list[dict[str, object]]], delay_ms: int = 0):
    env = os.environ.copy()
    env["JOBS_ASSISTANT_TEST_PROXY"] = "1"
    env["JOBS_ASSISTANT_TEST_RESOLVER_JSON"] = json.dumps(resolver)
    env["JOBS_ASSISTANT_TEST_RESOLVER_DELAY_MS"] = str(delay_ms)
    process = subprocess.Popen(
        ["node", "src/jobs_assistant/puppeteer_runner.js"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(Path.cwd()),
        env=env,
    )
    assert process.stdin is not None and process.stdout is not None
    hello_length = int(process.stdout.readline())
    hello = json.loads(process.stdout.read(hello_length))
    assert hello["ok"] is True

    def send(payload: dict) -> dict:
        body = json.dumps(payload, separators=(",", ":")).encode()
        process.stdin.write(str(len(body)).encode() + b"\n" + body)
        process.stdin.flush()
        length = int(process.stdout.readline())
        return json.loads(process.stdout.read(length))

    setup = send({"action": "test_proxy_setup", "logical_url": logical_url})
    assert setup["ok"] is True, setup
    return process, send, int(setup["data"]["proxy_port"])


def _connect_proxy(port: int, authority: str) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", port), timeout=2)
    client.sendall(
        f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
        "Proxy-Connection: keep-alive\r\n\r\n".encode()
    )
    return client
def _assert_proxy_client_closed(client: socket.socket, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    client.settimeout(0.1)
    while time.monotonic() < deadline:
        try:
            if client.recv(4096) == b"":
                return
        except socket.timeout:
            continue
    raise AssertionError("proxy CONNECT client remained open")

def _close_test_proxy(process, send) -> None:
    try:
        response = send({"action": "close"})
        assert response["ok"] is True, response
    finally:
        process.wait(timeout=5)

def test_http_rechecks_state_after_delayed_dns_before_upstream_request():
    process, send, port = _start_test_proxy(
        logical_url="https://boards.greenhouse.io/acme/jobs/123",
        resolver={"boards.greenhouse.io": [{"address": "8.8.8.8", "family": 4}]},
        delay_ms=300,
    )
    client = socket.create_connection(("127.0.0.1", port), timeout=2)
    client.sendall(
        b"GET https://boards.greenhouse.io/acme/jobs/123 HTTP/1.1\r\n"
        b"Host: boards.greenhouse.io\r\nConnection: close\r\n\r\n"
    )
    try:
        time.sleep(0.05)
        frozen = send({"action": "test_proxy_freeze", "mutate": True})
        assert frozen["ok"] is True, frozen
        client.settimeout(3)
        response = client.recv(4096)
        assert response.startswith(b"HTTP/1.1 403"), response
        counters = send({"action": "networkCounters"})
        assert counters["ok"] is True, counters
        assert counters["data"]["dnsLookups"] == 1
        assert counters["data"]["upstreamHttpAttempts"] == 0
    finally:
        _close_test_proxy(process, send)
        client.close()
def test_connect_rechecks_state_after_delayed_dns_before_upstream_socket():
    resolver = {"boards.greenhouse.io": [{"address": "8.8.8.8", "family": 4}]}
    process, send, port = _start_test_proxy(
        logical_url="https://boards.greenhouse.io/acme/jobs/123",
        resolver=resolver,
        delay_ms=300,
    )
    client = _connect_proxy(port, "boards.greenhouse.io:443")
    try:
        time.sleep(0.05)
        frozen = send({"action": "test_proxy_freeze", "mutate": True})
        assert frozen["ok"] is True, frozen
        _assert_proxy_client_closed(client)
        counters = send({"action": "networkCounters"})
        assert counters["ok"] is True, counters
        assert counters["data"]["dnsLookups"] == 1
        assert counters["data"]["upstreamConnectAttempts"] == 0
    finally:
        _close_test_proxy(process, send)
        client.close()


def test_connect_disallows_different_greenhouse_authority_before_dns():
    process, send, port = _start_test_proxy(
        logical_url="https://boards.greenhouse.io/acme/jobs/123",
        resolver={"grnh.se": [{"address": "8.8.8.8", "family": 4}]},
        delay_ms=300,
    )
    client = _connect_proxy(port, "grnh.se:443")
    try:
        _assert_proxy_client_closed(client)
        counters = send({"action": "networkCounters"})
        assert counters["ok"] is True, counters
        assert counters["data"]["dnsLookups"] == 0
        assert counters["data"]["upstreamConnectAttempts"] == 0
    finally:
        _close_test_proxy(process, send)
        client.close()


def test_connect_allows_exact_logical_and_static_authorities_before_freeze():
    process, send, port = _start_test_proxy(
        logical_url="https://boards.greenhouse.io/acme/jobs/123",
        resolver={
            "boards.greenhouse.io": [{"address": "8.8.8.8", "family": 4}],
            "job-boards.cdn.greenhouse.io": [{"address": "8.8.8.8", "family": 4}],
        },
    )
    logical_client = _connect_proxy(port, "boards.greenhouse.io:443")
    static_client = _connect_proxy(port, "job-boards.cdn.greenhouse.io:443")
    try:
        deadline = time.monotonic() + 3
        counters = None
        while time.monotonic() < deadline:
            response = send({"action": "networkCounters"})
            assert response["ok"] is True, response
            counters = response["data"]
            if counters["upstreamConnectAttempts"] >= 2:
                break
            time.sleep(0.05)
        assert counters is not None
        assert counters["dnsLookups"] == 2
        assert counters["upstreamConnectAttempts"] == 2
    finally:
        _close_test_proxy(process, send)
        logical_client.close()
        static_client.close()

def test_preflight_does_not_launch_browser():
    data = PuppeteerSession.preflight()
    assert data["node"].startswith("v")
    assert data["puppeteer"] == "24.43.1"
    assert data["executablePathBasename"]


@BROWSER_INTEGRATION_SKIP
def test_observe_generation_final_marker_network_denial_and_cleanup(fixture_server):
    with PuppeteerSession.start(headless=True, session_id="session-test", run_id=1, job_id=123, internal_transport_url=fixture_server) as session:
        launch_pid = session.owner_pid
        session.goto("https://boards.greenhouse.io/fixture/jobs/123")
        observation = session.observe()
        assert observation["observation_id"].startswith("obs-")
        assert observation["final_submit_target_ids"], observation
        first_shot = session.screenshot("initial")
        duplicate_shot = session.screenshot("blocker")
        assert first_shot["sha256"] == duplicate_shot["sha256"]
        assert first_shot["path"] == duplicate_shot["path"]
        assert duplicate_shot["deduplicated"] is True
        assert first_shot["reference"].startswith("screenshot:")
        first_name = field_by_name(observation, "first_name")
        resume = field_by_name(observation, "resume")
        assert first_name["field_key"]
        assert resume["kind"] == "file"
        assert "pdf" in ",".join(resume["accept"]).lower()

        session.fill(first_name["target_id"], "Ada")
        with pytest.raises(BrowserAdapterError, match="generation_already_consumed|stale_generation"):
            session.fill(first_name["target_id"], "Grace")

        counters = session.network_counters()
        assert counters["denied"] >= 1
        assert counters["attackerDnsLookups"] == 0
        assert counters["terminal_reason"] == "unsafe_network_attempt"
        assert FixtureHandler.final_like_requests == 0

    with pytest.raises(ProcessLookupError):
        os.kill(launch_pid, 0)

@BROWSER_INTEGRATION_SKIP
def test_submit_typed_nonfinal_continuation_click_uses_framed_offline_protocol(fixture_server):
    transport_url = fixture_server.replace("/clean", "/continue-native")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-submit-continuation",
        run_id=2,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        continuation = next(
            button
            for button in observation["buttons"]
            if button["button_type"] == "submit"
            and button["target_id"] not in observation["final_submit_target_ids"]
        )
        assert continuation["effective_action_url"] is None
        assert continuation["effective_method"] is None
        assert session.click_offline(continuation["target_id"], continuation=True)["clicked"] is True

        counters = session.network_counters()
        assert counters["finalLikeDenied"] == 0
        assert counters["terminal_reason"] in {None, "unsafe_network_attempt"}
        assert FixtureHandler.final_like_requests == 0
        assert FixtureHandler.attacker_http_requests == 0
@BROWSER_INTEGRATION_SKIP
def test_lever_session_uses_selected_policy_and_keeps_final_submit_manual(fixture_server):
    with PuppeteerSession.start(
        headless=True,
        ats_policy="lever",
        session_id="session-lever",
        run_id=101,
        job_id=123,
        internal_transport_url=fixture_server,
    ) as session:
        result = session.goto("https://jobs.lever.co/example/123e4567-e89b-12d3-a456-426614174000/apply")
        assert result["mode"] == "lever_apply"
        observation = session.observe()
        assert observation["final_submit_target_ids"]
        assert any(field.get("name") == "first_name" for field in observation["fields"])
        target = next(button for button in observation["buttons"] if button["target_id"] in observation["final_submit_target_ids"])
        with pytest.raises(BrowserAdapterError, match="final_or_anchor_not_automated"):
            session.click_offline(target["target_id"])



@pytest.mark.skipif(
    os.environ.get("RUN_PUPPETEER_INTEGRATION") != "1",
    reason="set RUN_PUPPETEER_INTEGRATION=1 for the normal-parent-exit survival smoke",
)
@pytest.mark.skipif(
    os.name != "nt" and sys.platform.startswith("linux") and not os.environ.get("DISPLAY"),
    reason="headed Chromium requires a Linux display",
)
@BROWSER_INTEGRATION_SKIP
def test_release_survives_normal_helper_exit_and_heartbeat(fixture_server, tmp_path):
    """A released owner survives normal interpreter shutdown without a guardian."""
    run_dir = tmp_path / "run"
    manifest = run_dir / "review_session.json"
    helper = f"""
import json, sys
from jobs_assistant.browser_adapter import PuppeteerSession

session = None
released = False
try:
    session = PuppeteerSession.start(
        headless=False,
        session_id="session-normal-exit",
        run_id=72,
        job_id=172,
        run_cwd={str(run_dir)!r},
        screenshot_root={str(run_dir / "screenshots")!r},
        session_manifest={str(manifest)!r},
        internal_transport_url={fixture_server!r},
    )
    session.goto("https://boards.greenhouse.io/fixture/jobs/123?gh_src=human-review")
    assert session.prepare_handoff()["state"] == "prepared"
    assert session.commit_handoff("normal-exit-token-" + "a" * 32)["state"] == "open_guarded"
    assert session.release_handoff()["released"] is True
    released = True
    print(json.dumps({{"owner": session.owner_identity, "browser": session.browser_identity}}), flush=True)
finally:
    if session is not None and not released:
        session.close(force=True)
"""
    identities: dict | None = None
    try:
        result = subprocess.run(
            [sys.executable, "-c", helper],
            cwd=str(Path.cwd()),
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        helper_lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert helper_lines, result.stdout
        identities = json.loads(helper_lines[-1])
        owner = identities["owner"]
        browser = identities["browser"]
        assert _capture_process_identity(owner["pid"]) == owner
        assert _capture_process_identity(browser["pid"]) == browser
        assert owner["pid"] == owner["pgid"]
        assert owner["pgid"] != os.getpgrp()
        process_name = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(owner["pid"])],
            text=True,
            capture_output=True,
            check=False,
        )
        assert process_name.returncode == 0
        assert "node" in process_name.stdout.lower()
        first_heartbeat = json.loads(manifest.read_text(encoding="utf-8"))["heartbeat"]
        heartbeat_values = {first_heartbeat}
        heartbeat_deadline = time.monotonic() + 16
        while time.monotonic() < heartbeat_deadline:
            current = json.loads(manifest.read_text(encoding="utf-8"))
            assert current["state"] == "open_guarded"
            assert current["detached"] is True
            profile_path = Path(current["profile_path"])
            assert profile_path.is_dir()
            assert any(entry.is_symlink() for entry in profile_path.rglob("*"))
            assert current["owner_identity"] == owner
            assert current["browser_identity"] == browser
            assert _capture_process_identity(owner["pid"]) == owner
            assert _capture_process_identity(browser["pid"]) == browser
            heartbeat_values.add(current["heartbeat"])
            if len(heartbeat_values) >= 2:
                break
            time.sleep(0.1)
        assert len(heartbeat_values) >= 2, "released owner did not emit a second heartbeat"
    finally:
        if identities is not None:
            _validated_emergency_cleanup(identities, manifest)

@pytest.mark.skipif(
    os.environ.get("RUN_PUPPETEER_HEADED_SMOKE") != "1",
    reason="set RUN_PUPPETEER_HEADED_SMOKE=1 for the automated headed local diagnostic",
)
@pytest.mark.skipif(
    os.name != "nt" and sys.platform.startswith("linux") and not os.environ.get("DISPLAY"),
    reason="headed Chromium requires a Linux display",
)
@BROWSER_INTEGRATION_SKIP
def test_headed_local_fixture_diagnostic_closes_cleanly(fixture_server, tmp_path):
    """Exercise headed browser startup and a benign local mutation without handoff."""
    preflight = PuppeteerSession.preflight(headed=True)
    assert preflight["puppeteer"] == "24.43.1"

    run_dir = tmp_path / "run"
    manifest = run_dir / "review_session.json"
    caller_staged = tmp_path / "caller-owned-staged.txt"
    caller_bytes = b"caller-owned staged fixture"
    caller_staged.write_bytes(caller_bytes)
    input_root = tmp_path / "input"
    input_root.mkdir(mode=0o700)
    staged = input_root / "resume.txt"
    staged.write_bytes(caller_bytes)
    staged_sha256 = hashlib.sha256(staged.read_bytes()).hexdigest()
    logical_url = validate_greenhouse_url("https://boards.greenhouse.io/fixture/jobs/123").url

    session = PuppeteerSession.start(
        headless=False,
        session_id="session-headed-local-diagnostic",
        run_id=73,
        job_id=173,
        run_cwd=run_dir,
        screenshot_root=run_dir / "screenshots",
        input_root=input_root,
        staged_input=staged.name,
        staged_sha256=staged_sha256,
        staged_media_type="text/plain",
        session_manifest=manifest,
        internal_transport_url=fixture_server,
    )
    owner = dict(session.owner_identity)
    browser = dict(session.browser_identity)
    adapter_root = Path(session._child_root)
    try:
        assert manifest.exists()
        session.goto(logical_url)
        observation = session.observe()
        first_name = field_by_name(observation, "first_name")
        session.fill(first_name["target_id"], "Ada")
    finally:
        session.close()

    terminal = json.loads(manifest.read_text(encoding="utf-8"))
    assert terminal["state"] == "closed"
    assert terminal["cleanup"] is True
    assert terminal["staged_input_removed"] is True
    assert terminal["owner_identity"] == owner
    assert terminal["browser_identity"] == browser
    assert terminal["owner_pgid"] == owner["pgid"]
    assert terminal["browser_pgid"] == browser["pgid"]
    assert terminal["profile_path"]
    assert not Path(terminal["profile_path"]).exists()
    assert not adapter_root.exists()
    for identity in (owner, browser):
        with pytest.raises(ProcessLookupError):
            os.killpg(identity["pgid"], 0)
    assert not staged.exists()

    assert FixtureHandler.attacker_http_requests == 0
    assert FixtureHandler.final_like_requests == 0
    assert caller_staged.read_bytes() == caller_bytes
    assert not (run_dir / "run.json").exists()

@pytest.mark.skipif(
    os.environ.get("RUN_PUPPETEER_HEADED_SMOKE") != "1",
    reason="set RUN_PUPPETEER_HEADED_SMOKE=1 for the manual headed survival smoke",
)
@BROWSER_INTEGRATION_SKIP
def test_headed_review_survives_parent_and_closes_from_tab(fixture_server, tmp_path):
    """Run handoff in a short-lived helper; only a physical click may continue it."""
    run_dir = tmp_path / "run"
    input_root = tmp_path / "input"
    input_root.mkdir(mode=0o700)
    staged = input_root / "resume.txt"
    staged.write_bytes(b"headed smoke resume")
    manifest = run_dir / "review_session.json"
    helper = f"""
import hashlib, json, sys
from jobs_assistant.browser_adapter import PuppeteerSession
import jobs_assistant.browser_adapter as adapter
print(adapter.__file__, adapter.RUNNER, file=sys.stderr)

session = None
released = False
try:
    staged = {str(staged)!r}
    session = PuppeteerSession.start(
        headless=False,
        session_id="session-headed-review",
        run_id=71,
        job_id=171,
        run_cwd={str(run_dir)!r},
        screenshot_root={str(run_dir / "screenshots")!r},
        input_root={str(input_root)!r},
        staged_input="resume.txt",
        staged_sha256=hashlib.sha256(open(staged, "rb").read()).hexdigest(),
        staged_media_type="text/plain",
        session_manifest={str(manifest)!r},
        internal_transport_url={fixture_server!r},
    )
    session.goto("https://boards.greenhouse.io/fixture/jobs/123?gh_src=human-review")
    observation = session.observe()
    assert any(button.get("text") == "Continue to guarded local review" for button in observation["buttons"])
    assert session.prepare_handoff()["state"] == "prepared"
    assert session.commit_handoff("headed-review-token-" + "a" * 32)["state"] == "open_guarded"
    assert session.release_handoff()["released"] is True
    released = True
    print(json.dumps({{"owner": session.owner_identity, "browser": session.browser_identity}}), flush=True)
finally:
    if session is not None and not released:
        session.close(force=True)
"""
    identities: dict | None = None
    try:
        result = subprocess.run(
            [sys.executable, "-c", helper],
            cwd=str(Path.cwd()),
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
            env={**os.environ, "DEBUG": "puppeteer:*"},
        )
        print("HELPER STDERR:", result.stderr, file=sys.stderr)
        assert result.returncode == 0, result.stderr
        helper_lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert helper_lines, result.stdout
        identities = json.loads(helper_lines[-1])
        owner = identities["owner"]
        browser = identities["browser"]
        first_heartbeat = json.loads(manifest.read_text(encoding="utf-8"))["heartbeat"]
        heartbeat_deadline = time.monotonic() + 16
        while time.monotonic() < heartbeat_deadline:
            current = json.loads(manifest.read_text(encoding="utf-8"))
            if current.get("state") == "open_guarded" and current.get("heartbeat") != first_heartbeat:
                break
            time.sleep(0.1)
        else:
            pytest.fail("headed owner did not survive release through a second heartbeat")

        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0
        before_click_requests = FixtureHandler.benign_requests
        before_click_logical_count = len(FixtureHandler.logical_urls)
        print(
            "\nHEADED SMOKE: in the isolated local Chromium window, physically click "
            "'Continue to guarded local review', then close that tab. "
            "Do not use the Submit Application control.",
            flush=True,
        )
        close_deadline = time.monotonic() + 120
        while time.monotonic() < close_deadline:
            current = json.loads(manifest.read_text(encoding="utf-8"))
            if current.get("state") == "closed" and current.get("cleanup") is True:
                break
            time.sleep(0.1)
        else:
            pytest.fail("headed review was not closed by a human tab close")

        terminal = json.loads(manifest.read_text(encoding="utf-8"))
        # The strict proxy is hosted by the owner process group.
        assert terminal["owner_pgid"] == owner["pgid"]
        review_urls = [
            url for url in FixtureHandler.logical_urls[before_click_logical_count:]
            if "gh_src=human-review" in url
        ]
        assert review_urls == [
            "https://boards.greenhouse.io/fixture/jobs/123?gh_src=human-review"
        ]
        assert 1 <= FixtureHandler.benign_requests - before_click_requests <= 2
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0
        assert not staged.exists()
        assert terminal.get("profile_path")
        assert not Path(terminal["profile_path"]).exists()
        for identity in (owner, browser):
            with pytest.raises(ProcessLookupError):
                os.killpg(identity["pgid"], 0)
    except BaseException:
        if identities is not None:
            _validated_emergency_cleanup(identities, manifest)
        raise


@BROWSER_INTEGRATION_SKIP
def test_headless_session_cannot_prepare_or_survive_release(fixture_server, tmp_path):
    manifest = tmp_path / "review_session.json"
    session = PuppeteerSession.start(
        headless=True,
        session_id="session-headless",
        run_id=7,
        job_id=123,
        session_manifest=manifest,
        internal_transport_url=fixture_server,
    )
    owner_pid = session.owner_pid
    starting = json.loads(manifest.read_text())
    assert starting["state"] == "starting"
    assert starting["browser_identity"]["pid"] == session.browser_pid
    try:
        session.goto("https://boards.greenhouse.io/fixture/jobs/123")
        with pytest.raises(BrowserAdapterError, match="headless_handoff_forbidden"):
            session.prepare_handoff()
    finally:
        session.close(force=True)
    terminal = json.loads(manifest.read_text())
    assert terminal["state"] == "closed"
    assert terminal["cleanup"] is True
    with pytest.raises(ProcessLookupError):
        os.kill(owner_pid, 0)


def test_start_rejects_missing_startup_identity():
    with pytest.raises(BrowserAdapterError, match="startup_identity_required"):
        PuppeteerSession.start(headless=True)



@BROWSER_INTEGRATION_SKIP
def test_upload_uses_registered_staged_file_without_caller_path(fixture_server, tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    staged = input_root / "resume.pdf"
    staged.write_bytes(b"fixture resume")
    session = PuppeteerSession.start(
        headless=True,
        session_id="session-upload",
        run_id=8,
        job_id=124,
        input_root=input_root,
        staged_input=staged.name,
        staged_sha256=hashlib.sha256(staged.read_bytes()).hexdigest(),
        staged_media_type="application/pdf",
        internal_transport_url=fixture_server,
    )
    try:
        session.goto("https://boards.greenhouse.io/fixture/jobs/123")
        resume = field_by_name(session.observe(), "resume")
        assert session.upload(resume["target_id"])["retained"] is True
    finally:
        session.close(force=True)


@BROWSER_INTEGRATION_SKIP
def test_upload_rejects_registered_staged_symlink(fixture_server, tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    original = tmp_path / "resume.pdf"
    original.write_bytes(b"fixture resume")
    staged = input_root / "resume.pdf"
    staged.symlink_to(original)
    session = PuppeteerSession.start(
        headless=True,
        session_id="session-upload-symlink",
        run_id=9,
        job_id=125,
        input_root=input_root,
        staged_input=staged.name,
        staged_sha256=hashlib.sha256(original.read_bytes()).hexdigest(),
        staged_media_type="application/pdf",
        internal_transport_url=fixture_server,
    )
    try:
        session.goto("https://boards.greenhouse.io/fixture/jobs/123")
        resume = field_by_name(session.observe(), "resume")
        with pytest.raises(BrowserAdapterError, match="browser_command_failed"):
            session.upload(resume["target_id"])
    finally:
        session.close(force=True)


@BROWSER_INTEGRATION_SKIP
def test_upload_rejects_registered_hash_mismatch(fixture_server, tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    staged = input_root / "resume.pdf"
    staged.write_bytes(b"fixture resume")
    session = PuppeteerSession.start(
        headless=True,
        session_id="session-upload-hash",
        run_id=10,
        job_id=126,
        input_root=input_root,
        staged_input=staged.name,
        staged_sha256="0" * 64,
        staged_media_type="application/pdf",
        internal_transport_url=fixture_server,
    )
    try:
        session.goto("https://boards.greenhouse.io/fixture/jobs/123")
        resume = field_by_name(session.observe(), "resume")
        with pytest.raises(BrowserAdapterError, match="staged_input_mismatch"):
            session.upload(resume["target_id"])
    finally:
        session.close(force=True)

def test_protocol_rejects_oversized_input_frame():
    process = subprocess.run(
        ["node", str(Path("src/jobs_assistant/puppeteer_runner.js"))],
        input=b"262145\n" + (b"{" * 262145),
        capture_output=True,
        cwd=str(Path.cwd()),
        timeout=10,
        check=False,
    )
    frames = _decode_frames(process.stdout)
    assert frames[0]["ok"] is True
    assert frames[-1]["ok"] is False
    assert frames[-1]["error"] == "input_frame_too_large"

def test_protocol_rejects_unframed_json_response() -> None:
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    try:
        os.write(write_fd, b'{"ok":true}\n')
    finally:
        os.close(write_fd)

    session = object.__new__(PuppeteerSession)
    session.process = type("FakeProcess", (), {"stdout": stream})()
    session._selector = selectors.DefaultSelector()
    session._selector.register(stream, selectors.EVENT_READ)
    session._recv_buffer = bytearray()
    session._poisoned = False
    try:
        with pytest.raises(BrowserAdapterError, match="protocol_bad_length"):
            session.read_response(timeout=1)
        assert session._poisoned is True
    finally:
        session._selector.close()
        stream.close()

@pytest.mark.parametrize(
    ("prefix", "error"),
    (
        (b"+1", "protocol_bad_length"),
        (b" 1", "protocol_bad_length"),
        (b"01", "protocol_bad_length"),
        (b"9" * 32, "protocol_frame_too_large"),
    ),
)
def test_protocol_rejects_noncanonical_response_length(prefix: bytes, error: str) -> None:
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    try:
        os.write(write_fd, prefix + b"\n{}")
    finally:
        os.close(write_fd)

    session = object.__new__(PuppeteerSession)
    session.process = type("FakeProcess", (), {"stdout": stream})()
    session._selector = selectors.DefaultSelector()
    session._selector.register(stream, selectors.EVENT_READ)
    session._recv_buffer = bytearray()
    session._poisoned = False
    try:
        with pytest.raises(BrowserAdapterError, match=error):
            session.read_response(timeout=1)
        assert session._poisoned is True
    finally:
        session._selector.close()
        stream.close()


@pytest.mark.parametrize(
    ("prefix", "error"),
    (
        (b"+1", "protocol_bad_length"),
        (b" 1", "protocol_bad_length"),
        (b"01", "protocol_bad_length"),
        (b"9" * 32, "protocol_frame_too_large"),
    ),
)
def test_preflight_protocol_rejects_noncanonical_response_length(prefix: bytes, error: str) -> None:
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    try:
        os.write(write_fd, prefix + b"\n{}")
    finally:
        os.close(write_fd)

    process = type("FakeProcess", (), {"stdout": stream})()
    try:
        with pytest.raises(BrowserAdapterError, match=error):
            _read_one_frame(process, timeout=1)
    finally:
        stream.close()


def test_preflight_protocol_accepts_canonical_response_length() -> None:
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    try:
        os.write(write_fd, b"2\n{}")
    finally:
        os.close(write_fd)

    process = type("FakeProcess", (), {"stdout": stream})()
    try:
        assert _read_one_frame(process, timeout=1) == {}
    finally:
        stream.close()



@BROWSER_INTEGRATION_SKIP
def test_npm_puppeteer_smoke_runs_real_headless_chromium():
    result = subprocess.run(
        ["npm", "run", "puppeteer-smoke", "--silent"],
        cwd=str(Path.cwd()),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    frames = _decode_frames(result.stdout.encode())
    assert frames[-1]["ok"] is True
    assert frames[-1]["data"]["smoke"] is True
    assert frames[-1]["data"]["counters"]["attackerDnsLookups"] == 0


def _decode_frames(data: bytes) -> list[dict]:
    frames = []
    while data:
        line, _, rest = data.partition(b"\n")
        if not line:
            break
        length = int(line)
        frames.append(json.loads(rest[:length]))
        data = rest[length:]
    return frames

def _session_with_response(response: dict) -> PuppeteerSession:
    session = object.__new__(PuppeteerSession)
    session._request_lock = threading.Lock()
    session._poisoned = False
    session._closed = False
    session._write_frame = lambda payload, timeout: None
    session.read_response = lambda timeout=15.0: response
    return session


def test_request_rejects_malformed_success_envelope_and_poison_session():
    session = _session_with_response({"ok": True})

    with pytest.raises(BrowserAdapterError, match="protocol_invalid_response"):
        session.request({"action": "unknown"})

    assert session._poisoned is True
    with pytest.raises(BrowserAdapterError, match="protocol_poisoned"):
        session.request({"action": "unknown"})


def test_request_rejects_malformed_observe_success_and_poison_session():
    session = _session_with_response({
        "ok": True,
        "data": {"observation_id": "obs-1", "fields": [], "buttons": []},
    })

    with pytest.raises(BrowserAdapterError, match="protocol_invalid_response"):
        session.request({"action": "observe"})

    assert session._poisoned is True


def test_request_rejects_malformed_mutation_ack_and_accepts_valid_shapes():
    malformed = _session_with_response({"ok": True, "data": {"counters": {}}})
    with pytest.raises(BrowserAdapterError, match="protocol_invalid_response"):
        malformed.request({"action": "fill"})
    assert malformed._poisoned is True

    valid_observation = {
        "observation_id": "obs-1",
        "url": "https://boards.greenhouse.io/acme/jobs/123",
        "title": "Apply",
        "site_markers": [],
        "fields": [],
        "buttons": [],
        "final_submit_target_ids": [],
        "errors": [],
        "blockers": [],
        "counters": {},
        "terminal_reason": None,
    }
    valid_observe = _session_with_response({"ok": True, "data": valid_observation})
    assert valid_observe.request({"action": "observe"}) == valid_observation

    valid_mutation = _session_with_response({
        "ok": True,
        "data": {"retained": True, "counters": {}},
    })
    assert valid_mutation.request({"action": "fill"}) == {"retained": True, "counters": {}}


def test_normalize_browser_error_code_collapses_raw_runner_messages():
    raw = "Error: page.goto failed at https://secret.example/app /private/tmp/jobs SECRET_SENTINEL"

    assert normalize_browser_error_code("navigation_timeout") == "navigation_timeout"
    assert normalize_browser_error_code(raw) == "browser_command_failed"
    assert normalize_browser_error_code({"error": raw}) == "browser_command_failed"
    assert normalize_browser_error_code(None) == "browser_command_failed"


def test_request_exposes_only_allowlisted_runner_error_codes():
    known = _session_with_response({"ok": False, "error": "navigation_dns_failed"})
    with pytest.raises(BrowserAdapterError) as known_error:
        known.request({"action": "goto"})
    assert str(known_error.value) == "navigation_dns_failed"

    raw = "PuppeteerError https://secret.example/app /private/tmp/jobs SECRET_SENTINEL"
    unknown = _session_with_response({"ok": False, "error": raw})
    with pytest.raises(BrowserAdapterError) as unknown_error:
        unknown.request({"action": "goto"})
    assert str(unknown_error.value) == "browser_command_failed"
    assert raw not in str(unknown_error.value)
    assert unknown_error.value.__cause__ is None

def test_runner_error_code_self_test():
    result = subprocess.run(
        ["node", "src/jobs_assistant/puppeteer_runner.js", "--error-code-self-test"],
        cwd=str(Path.cwd()),
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert _decode_frames(result.stdout) == [{"ok": True, "data": {"passed": 10}}]

def test_runner_request_guard_self_test() -> None:
    result = subprocess.run(
        ["node", "src/jobs_assistant/puppeteer_runner.js", "--request-guard-self-test"],
        cwd=str(Path.cwd()),
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert _decode_frames(result.stdout) == [{"ok": True, "data": {"passed": 6}}]
