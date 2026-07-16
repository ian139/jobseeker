from __future__ import annotations

import json
import ipaddress
import sys
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .safety import validate_ats_policy_name

RUNNER = Path(__file__).with_name("puppeteer_runner.js").resolve(strict=True)
MAX_IN_FRAME = 256 * 1024
MAX_OUT_FRAME = 2 * 1024 * 1024
MAX_OUT_FRAME_DIGITS = len(str(MAX_OUT_FRAME))
# Keep observations and plans comfortably below the protocol frame ceiling.
MAX_OBSERVATION_BYTES = 1_900_000
FINAL_ROUTE_TOKENS = ("submit", "complete", "confirm", "finish", "send", "final")
GREENHOUSE_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io"}
SAFE_BROWSER_ERROR_CODES = {
    "unsupported_ats",
    "invalid_ats_policy",
    "invalid_application_url",
    "final_like_route",
    "unsafe_navigation_target",
    "unsafe_network_attempt",
    "redirect_limit_exceeded",
    "invalid_path_encoding",
    "unsafe_path",
    "resolver_address_count",
    "resolver_address_rejected",
    "resolver_hostname_required",
    "local_transport_not_numeric",
    "proxy_authorization_revoked",
    "response_body_too_large",
    "puppeteer_version_mismatch",
    "chromium_executable_missing",
    "browser_process_missing",
    "browser_disconnected",
    "ats_policy_mismatch",
    "page_not_stable",
    "query_selector_unavailable",
    "observation_too_large",
    "artifact_error",
    "startup_identity_required",
    "startup_identity_mismatch",
    "browser_handshake_failed",
    "browser_identity_mismatch",
    "process_identity_mismatch",
    "headless_handoff_forbidden",
    "handoff_not_eligible",
    "handoff_state_conflict",
    "stale_generation",
    "generation_already_consumed",
    "field_identity_collision",
    "sensitive_field",
    "target_not_actionable",
    "invalid_field_value",
    "invalid_boolean_value",
    "invalid_select_value",
    "upload_path_forbidden",
    "upload_accept_mismatch",
    "staged_input_mismatch",
    "staged_input_changed",
    "field_value_not_retained",
    "final_or_anchor_not_automated",
    "button_not_hit_tested",
    "prototype_poisoned",
    "artifact_budget",
    "file_budget",
    "input_frame_too_large",
    "invalid_frame_prefix",
    "invalid_json_frame",
    "invalid_command_frame",
    "browser_launch_timeout",
    "browser_launch_failed",
    "navigation_timeout",
    "navigation_dns_failed",
    "navigation_connection_failed",
    "navigation_tls_failed",
    "observation_timeout",
    "browser_command_failed",
    "browser_preflight_error",
    "browser_start_error",
    "browser_handshake_identity_mismatch",
    "owner_identity_registration_failed",
    "browser_identity_registration_failed",
    "protocol_timeout",
    "protocol_eof",
    "protocol_bad_length",
    "protocol_frame_too_large",
    "protocol_invalid_json",
    "protocol_non_object",
    "protocol_invalid_response",
    "output_frame_too_large",
    "adapter_stdout_missing",
    "adapter_stdin_missing",
}


def normalize_browser_error_code(value: object) -> str:
    """Return only a privacy-safe, allowlisted browser diagnostic code."""
    return value if isinstance(value, str) and value in SAFE_BROWSER_ERROR_CODES else "browser_command_failed"
def _parse_frame_length(prefix: bytes) -> int:
    """Parse one canonical non-negative decimal protocol length."""
    if re.fullmatch(rb"(?:0|[1-9][0-9]*)", prefix) is None:
        raise BrowserAdapterError("protocol_bad_length")
    if len(prefix) > MAX_OUT_FRAME_DIGITS:
        raise BrowserAdapterError("protocol_frame_too_large")
    length = int(prefix)
    if length > MAX_OUT_FRAME:
        raise BrowserAdapterError("protocol_frame_too_large")
    return length

def _positive_protocol_int(value: object) -> bool:
    return type(value) is int and value > 0


def _protocol_identity(value: object) -> bool:
    return isinstance(value, dict) and all(
        key in value for key in ("pid", "pgid", "birth")
    )


def _protocol_counters(value: object) -> bool:
    return isinstance(value, dict)


def _valid_response_data(action: object, data: dict[str, Any]) -> bool:
    """Check the minimum response shape needed by each adapter operation."""
    if not isinstance(action, str):
        return True
    if action == "startup_identity":
        return (
            data.get("hello") is True
            and data.get("protocol") == "length-prefixed-json-v1"
            and _protocol_identity(data.get("identity"))
        )
    if action == "launch":
        return (
            _positive_protocol_int(data.get("owner_pid"))
            and _positive_protocol_int(data.get("browser_pid"))
            and data.get("pipe") is True
        )
    if action == "register_browser_identity":
        return _protocol_identity(data.get("browser_identity")) and _protocol_identity(data.get("identity"))
    if action == "resolvePinnedAddress":
        return isinstance(data.get("address"), str) and data.get("family") in (4, 6)
    if action == "classifyResolverResult":
        return isinstance(data.get("address"), str) and data.get("family") in (4, 6)
    if action == "goto":
        return (
            isinstance(data.get("url"), str)
            and isinstance(data.get("title"), str)
            and (data.get("status") is None or type(data.get("status")) is int)
            and isinstance(data.get("mode"), str)
        )
    if action == "observe":
        return (
            isinstance(data.get("observation_id"), str)
            and isinstance(data.get("url"), str)
            and isinstance(data.get("title"), str)
            and all(isinstance(data.get(key), list) for key in (
                "site_markers",
                "fields",
                "buttons",
                "final_submit_target_ids",
                "errors",
                "blockers",
            ))
            and _protocol_counters(data.get("counters"))
            and (data.get("terminal_reason") is None or isinstance(data.get("terminal_reason"), str))
        )
    if action in {"fill", "select", "check", "upload"}:
        return data.get("retained") is True and _protocol_counters(data.get("counters"))
    if action == "click":
        return data.get("clicked") is True and _protocol_counters(data.get("counters"))
    if action == "screenshot":
        return (
            isinstance(data.get("path"), str)
            and isinstance(data.get("reference"), str)
            and type(data.get("bytes")) is int
            and data.get("bytes") >= 0
            and isinstance(data.get("sha256"), str)
            and type(data.get("full_page")) is bool
            and type(data.get("truncated")) is bool
            and type(data.get("pixel_width")) is int
            and type(data.get("pixel_height")) is int
        )
    if action == "webrtcStatus":
        return type(data.get("available")) is bool and data.get("policy") == "disable_non_proxied_udp"
    if action == "prepare_handoff":
        return data.get("state") == "prepared" and _protocol_identity(data.get("identity"))
    if action == "commit_handoff":
        return data.get("state") == "open_guarded" and _protocol_identity(data.get("identity"))
    if action == "release_handoff":
        return data.get("state") == "open_guarded" and data.get("released") is True
    if action == "networkCounters":
        return _protocol_counters(data) and isinstance(data.get("review_state"), str)
    if action == "test_proxy_setup":
        return _positive_protocol_int(data.get("proxy_port"))
    if action == "test_proxy_freeze":
        return _protocol_counters(data) and (
            data.get("terminal_reason") is None or isinstance(data.get("terminal_reason"), str)
        )
    if action == "close":
        return not data
    return True

IDENTITY_FIELDS = ("pid", "pgid", "birth")
MANIFEST_VERSION = 1


def _process_birth(pid: int) -> str | None:
    """Return a stable OS process-start token without trusting caller input."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            raw = proc_stat.read_text(encoding="ascii")
            tail = raw.rsplit(") ", 1)[1].split()
            if len(tail) > 19:
                return str(tail[19])
        except (OSError, UnicodeError, ValueError, IndexError):
            pass
    try:
        result = subprocess.run(
            ("ps", "-p", str(pid), "-o", "lstart="),
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = result.stdout.strip()
    return token if result.returncode == 0 and token and len(token) <= 256 else None


def _capture_process_identity(pid: int) -> dict[str, Any]:
    try:
        pgid = os.getpgid(pid)
    except (OSError, ProcessLookupError) as exc:
        raise BrowserAdapterError("process_identity_unavailable") from exc
    birth = _process_birth(pid)
    if pgid != pid or not birth:
        raise BrowserAdapterError("process_identity_unavailable")
    return {"pid": int(pid), "pgid": int(pgid), "birth": str(birth)}


def _validate_process_identity(value: Any, *, expected_pid: int | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(IDENTITY_FIELDS):
        raise BrowserAdapterError("invalid_process_identity")
    pid, pgid, birth = value["pid"], value["pgid"], value["birth"]
    if type(pid) is not int or pid <= 0 or type(pgid) is not int or pgid <= 0:
        raise BrowserAdapterError("invalid_process_identity")
    if pgid != pid or (expected_pid is not None and pid != expected_pid):
        raise BrowserAdapterError("process_identity_mismatch")
    if type(birth) is not str or not birth or len(birth) > 256 or not birth.isascii():
        raise BrowserAdapterError("invalid_process_identity")
    return {"pid": pid, "pgid": pgid, "birth": birth}




def _verified_group_absent(identity: dict[str, Any] | None) -> bool:
    if identity is None:
        return False
    try:
        _capture_process_identity(int(identity["pid"]))
    except BrowserAdapterError:
        try:
            os.killpg(int(identity["pgid"]), 0)
        except ProcessLookupError:
            return True
        except (KeyError, TypeError, PermissionError):
            return False
        return False
    return False

def _validated_group_live(identity: dict[str, Any] | None) -> bool:
    """Return true only while the registered leader identity is unchanged."""
    if identity is None:
        return False
    try:
        expected = _validate_process_identity(identity, expected_pid=int(identity["pid"]))
        return _capture_process_identity(expected["pid"]) == expected
    except (BrowserAdapterError, KeyError, TypeError, ValueError):
        return False

class BrowserAdapterError(RuntimeError):
    """Raised when the local browser adapter rejects or loses a guarded command."""

    def __init__(self, code: object, *, runner_originated: bool = False) -> None:
        super().__init__(str(code))
        self.runner_originated = runner_originated

@dataclass(frozen=True)
class GreenhouseRoute:
    url: str
    host: str
    path: str
    mode: str
    ats_policy: str = "greenhouse"

def _final_like(value: str) -> bool:
    # Match ASCII words, not substrings (``finalist`` is safe while ``submit`` is not).
    text = str(value or "")
    for _ in range(3):
        try:
            decoded = __import__("urllib.parse", fromlist=["unquote"]).unquote(text)
        except Exception:
            break
        if decoded == text:
            break
        text = decoded
    words = set(re.findall(r"[A-Za-z0-9]+", text.lower()))
    return any(token in words for token in FINAL_ROUTE_TOKENS)


def _ascii_slug(value: str) -> bool:
    return bool(value) and value.isascii() and bool(re.fullmatch(r"[A-Za-z0-9_-]+", value))


def _positive_job_id(value: str) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        return False
    significant = value.lstrip("0") or "0"
    return significant != "0" and (len(significant) < 16 or (len(significant) == 16 and significant <= "9007199254740991"))


def validate_greenhouse_url(url: str) -> GreenhouseRoute:
    """Validate and canonicalize one executable Greenhouse initial route."""

    if not isinstance(url, str) or len(url.encode("utf-8", "ignore")) > 8192:
        raise BrowserAdapterError("invalid_application_url")
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (ValueError, UnicodeError):
        raise BrowserAdapterError("invalid_application_url") from None
    if parsed.scheme != "https" or not host or port not in (None, 443):
        raise BrowserAdapterError("invalid_application_url")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise BrowserAdapterError("invalid_application_url")
    if not host.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in host):
        raise BrowserAdapterError("invalid_application_url")

    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise BrowserAdapterError("invalid_application_url") from None
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise BrowserAdapterError("invalid_application_url")

    def canonical(*, path: str, query_pairs: list[tuple[str, str]]) -> str:
        # gh_src is an attribution hint, never part of the executable identity.
        query = urlencode([(key, value) for key, value in query_pairs if key != "gh_src"])
        return urlunparse(("https", host, path, "", query, ""))

    if host == "grnh.se":
        if parsed.query or parsed.path.count("/") != 1 or not _ascii_slug(parsed.path.lstrip("/")):
            raise BrowserAdapterError("invalid_application_url")
        if _final_like(parsed.path):
            raise BrowserAdapterError("invalid_application_url")
        return GreenhouseRoute(url=canonical(path=parsed.path, query_pairs=[]), host=host, path=parsed.path, mode="greenhouse_short")
    if host not in GREENHOUSE_HOSTS:
        raise BrowserAdapterError("unsupported_ats")

    allowed = {"gh_src", "for", "token"}
    if any(key not in allowed for key in keys):
        raise BrowserAdapterError("invalid_application_url")
    if _final_like(parsed.path) or any(_final_like(value) for _, value in pairs):
        raise BrowserAdapterError("invalid_application_url")
    parts = parsed.path.split("/")
    if len(parts) == 4 and parts[0] == "" and parts[2] == "jobs" and _ascii_slug(parts[1]) and _positive_job_id(parts[3]):
        if any(key != "gh_src" for key in keys):
            raise BrowserAdapterError("invalid_application_url")
        return GreenhouseRoute(url=canonical(path=parsed.path, query_pairs=pairs), host=host, path=parsed.path, mode="greenhouse_job")
    if host == "boards.greenhouse.io" and parsed.path == "/embed/job_app":
        if any(key not in {"for", "token", "gh_src"} for key in keys):
            raise BrowserAdapterError("invalid_application_url")
        params = dict(pairs)
        slug, token = params.get("for", ""), params.get("token", "")
        if not _ascii_slug(slug) or not _positive_job_id(token):
            raise BrowserAdapterError("invalid_application_url")
        return GreenhouseRoute(url=canonical(path=parsed.path, query_pairs=pairs), host=host, path=parsed.path, mode="greenhouse_embed")
    raise BrowserAdapterError("invalid_application_url")
def validate_ats_url(url: str, ats_policy: str = "greenhouse") -> GreenhouseRoute:
    try:
        selected = validate_ats_policy_name(ats_policy)
    except ValueError as exc:
        raise BrowserAdapterError("unsupported_ats") from exc
    if selected == "greenhouse":
        return validate_greenhouse_url(url)
    if not isinstance(url, str) or len(url.encode("utf-8", "ignore")) > 8192:
        raise BrowserAdapterError("invalid_application_url")
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (ValueError, UnicodeError):
        raise BrowserAdapterError("invalid_application_url") from None
    if (
        parsed.scheme != "https"
        or host not in {"jobs.lever.co", "jobs.eu.lever.co"}
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or "?" in url
        or "%" in parsed.path
        or "\\" in parsed.path
    ):
        raise BrowserAdapterError("invalid_application_url")
    parts = parsed.path.split("/")
    if len(parts) not in (3, 4) or parts[0] != "" or (len(parts) == 4 and parts[3] != "apply"):
        raise BrowserAdapterError("invalid_application_url")
    company, job = parts[1], parts[2]
    try:
        canonical_job = str(uuid.UUID(job))
    except (AttributeError, TypeError, ValueError):
        raise BrowserAdapterError("invalid_application_url") from None
    if job != canonical_job or not _ascii_slug(company) or _final_like(parsed.path):
        raise BrowserAdapterError("invalid_application_url")
    mode = "lever_apply" if len(parts) == 4 else "lever_job"
    return GreenhouseRoute(
        url=urlunparse(("https", host, parsed.path, "", "", "")),
        host=host,
        path=parsed.path,
        mode=mode,
        ats_policy="lever",
    )

def validate_lever_url(url: str) -> GreenhouseRoute:
    return validate_ats_url(url, "lever")

def _is_public_hostname(host: str) -> bool:
    """Accept only DNS names or globally routable numeric addresses.

    DNS names are resolved and classified again by the Node proxy.  Numeric
    literals are checked here too so a local/special address cannot bypass the
    route guard by skipping DNS.
    """

    value = str(host or "").strip().lower().rstrip(".")
    if not value or value == "localhost" or value.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    return address.is_global


def _safe_child_env(root: Path, *, headed: bool = False) -> dict[str, str]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for sub in ("home", "tmp", "cache", "config", "data", "runtime"):
        (root / sub).mkdir(mode=0o700, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "TMP": str(root / "tmp"),
        "TEMP": str(root / "tmp"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_RUNTIME_DIR": str(root / "runtime"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NODE_OPTIONS": "",
    }
    if headed:
        for name in ("DISPLAY", "WAYLAND_DISPLAY"):
            value = os.environ.get(name)
            if value and value.isascii() and len(value) <= 256 and all(ord(char) >= 0x20 for char in value):
                env[name] = value
        xauthority = os.environ.get("XAUTHORITY")
        if xauthority:
            try:
                candidate = Path(xauthority).resolve(strict=True)
                if candidate.is_file() and candidate.stat().st_size <= 1 << 20:
                    env["XAUTHORITY"] = str(candidate)
            except OSError:
                pass
    configured = os.environ.get("PUPPETEER_EXECUTABLE_PATH")
    executable = None
    if configured:
        candidate = Path(configured)
        try:
            resolved = candidate.resolve(strict=True)
            if resolved.is_absolute() and resolved.is_file() and os.access(resolved, os.X_OK):
                executable = resolved
        except OSError:
            executable = None
    if executable is None:
        installed_cache = Path(os.environ.get("PUPPETEER_CACHE_DIR", Path.home() / ".cache" / "puppeteer")).resolve()
        executable = next(
            (
                candidate
                for pattern in (
                    "chrome/**/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                    "chrome/**/Google Chrome for Testing",
                    "chrome/**/chrome",
                    "chrome/**/chrome.exe",
                )
                for candidate in installed_cache.glob(pattern)
                if candidate.is_file() and os.access(candidate, os.X_OK)
            ),
            None,
        ) if installed_cache.is_dir() else None
    if executable is not None:
        env["JOBS_ASSISTANT_CHROMIUM_EXECUTABLE"] = str(executable.resolve())
    bundle_root = os.environ.get("JOBS_ASSISTANT_PUPPETEER_ROOT")
    if bundle_root:
        try:
            resolved_bundle_root = Path(bundle_root).resolve(strict=True)
            mode = stat.S_IMODE(resolved_bundle_root.stat().st_mode)
            if resolved_bundle_root.is_absolute() and resolved_bundle_root.is_dir() and not mode & 0o022:
                env["JOBS_ASSISTANT_PUPPETEER_ROOT"] = str(resolved_bundle_root)
        except OSError:
            pass
    if os.environ.get("JOBS_ASSISTANT_CONTAINER_NO_SANDBOX") == "1" and Path("/.dockerenv").is_file():
        env["JOBS_ASSISTANT_CONTAINER_NO_SANDBOX"] = "1"
    return env


def _absolute_node() -> Path:
    node = shutil.which("node")
    if not node:
        raise BrowserAdapterError("browser_preflight_error")
    resolved = Path(node).resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise BrowserAdapterError("browser_preflight_error")
    return resolved




class PuppeteerSession:
    """Pipe-connected, one-owner-per-job Node/Puppeteer session.

    The parent never receives a CDP endpoint and never reconnects. One protocol
    thread (the caller) owns each pipe; stderr is drained privately so a noisy
    browser cannot deadlock the owner.
    """

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        child_root: Path,
        nonce: str,
        owner_identity: dict[str, Any],
        session_id: str,
        run_id: int,
        job_id: int,
        ats_policy: str = "greenhouse",
        internal_transport_url: str | None = None,
    ) -> None:
        self.process = process
        self._internal_transport_url = internal_transport_url
        self._internal_transport_token: str | None = None
        self.owner_pid = int(process.pid)
        self.owner_identity = _validate_process_identity(owner_identity, expected_pid=self.owner_pid)
        self.owner_pgid = self.owner_identity["pgid"]
        self.browser_pid: int | None = None
        self.browser_pgid: int | None = None
        self.session_id = session_id
        self.run_id = run_id
        self.job_id = job_id
        self.ats_policy = ats_policy
        self._child_root = child_root
        self._nonce = nonce
        self._selector = selectors.DefaultSelector()
        self._poisoned = False
        self._closed = False
        self._detached = False
        self._committed_token: str | None = None
        self._write_lock = threading.Lock()
        self._stderr_bytes = 0
        self._stderr_cap = 2 * 1024 * 1024
        if process.stdout is None or process.stdin is None:
            self.close(force=True)
            raise BrowserAdapterError("adapter_pipes_missing")
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stdin.fileno(), False)
        self._selector.register(process.stdout, selectors.EVENT_READ)
        if process.stderr is not None:
            def drain() -> None:
                while True:
                    try:
                        chunk = os.read(process.stderr.fileno(), 65536)
                    except OSError:
                        return
                    if not chunk:
                        return
                    self._stderr_bytes = min(self._stderr_cap, self._stderr_bytes + len(chunk))
            self._stderr_thread = threading.Thread(target=drain, name="jobs-assistant-browser-stderr", daemon=True)
            self._stderr_thread.start()
        else:
            self._stderr_thread = None
        self._request_lock = threading.Lock()
        self._recv_buffer = bytearray()

    @classmethod
    def preflight(cls, *, headed: bool = False, timeout: float = 10.0) -> dict[str, Any]:
        """Verify Node, packaged Puppeteer, executable, and optional GUI prerequisites."""
        if headed and os.name != "nt" and sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            raise BrowserAdapterError("browser_preflight_error")

        node = _absolute_node()
        runner = RUNNER
        child_root = Path(tempfile.mkdtemp(prefix="jobs-assistant-preflight-"))
        env = _safe_child_env(child_root, headed=headed)
        nonce = uuid.uuid4().hex
        env["JOBS_ASSISTANT_HANDSHAKE"] = nonce
        process = subprocess.Popen(
            [str(node), str(runner), "--preflight"],
            cwd=str(runner.parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
            close_fds=True,
        )
        try:
            response = _read_one_frame(process, timeout=timeout)
            if not response.get("ok"):
                raise BrowserAdapterError("browser_preflight_error")
            data = response.get("data")
            if not isinstance(data, dict) or data.get("puppeteer") != "24.43.1":
                raise BrowserAdapterError("browser_preflight_error")
            return data
        except BrowserAdapterError:
            raise
        except Exception as exc:
            raise BrowserAdapterError("browser_preflight_error") from exc
        finally:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=timeout)
            shutil.rmtree(child_root, ignore_errors=True)

    @classmethod
    def start(
        cls,
        *,
        session_id: str | None = None,
        run_id: int | None = None,
        job_id: int | None = None,
        owner_identity: dict[str, Any] | None = None,
        ats_policy: str = "greenhouse",
        headless: bool = True,
        timeout: float = 15.0,
        run_cwd: str | os.PathLike[str] | None = None,
        screenshot_root: str | os.PathLike[str] | None = None,
        input_root: str | os.PathLike[str] | None = None,
        staged_input: str | None = None,
        staged_sha256: str | None = None,
        staged_media_type: str | None = None,
        session_manifest: str | os.PathLike[str] | None = None,
        internal_transport_url: str | None = None,
        on_owner_identity: Any | None = None,
        on_browser_identity: Any | None = None,
    ) -> "PuppeteerSession":
        try:
            selected_ats_policy = validate_ats_policy_name(ats_policy)
        except ValueError as exc:
            raise BrowserAdapterError("unsupported_ats") from exc
        if type(session_id) is not str or not session_id or len(session_id) > 256 or not session_id.isascii():
            raise BrowserAdapterError("startup_identity_required")
        if type(run_id) is not int or isinstance(run_id, bool) or run_id <= 0 or run_id > 9007199254740991:
            raise BrowserAdapterError("startup_identity_required")
        if type(job_id) is not int or isinstance(job_id, bool) or job_id <= 0 or job_id > 9007199254740991:
            raise BrowserAdapterError("startup_identity_required")
        if owner_identity is not None:
            _validate_process_identity(owner_identity)
        node = _absolute_node()
        child_root = Path(tempfile.mkdtemp(prefix="jobs-assistant-browser-owner-"))
        if not headless and os.name != "nt" and sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            shutil.rmtree(child_root, ignore_errors=True)
            raise BrowserAdapterError("browser_preflight_error")
        env = _safe_child_env(child_root, headed=not headless)
        nonce = uuid.uuid4().hex
        env["JOBS_ASSISTANT_HANDSHAKE"] = nonce
        internal_token: str | None = None
        if internal_transport_url is not None:
            parsed = urlparse(internal_transport_url)
            if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password:
                shutil.rmtree(child_root, ignore_errors=True)
                raise BrowserAdapterError("unsafe_navigation_target")
            internal_token = uuid.uuid4().hex
            env["JOBS_ASSISTANT_INTERNAL_TRANSPORT_TOKEN"] = internal_token
        if input_root is not None:
            input_root_path = Path(input_root).resolve()
            env["JOBS_ASSISTANT_INPUT_ROOT"] = str(input_root_path)
            if len({staged_input is None, staged_sha256 is None, staged_media_type is None}) != 1:
                shutil.rmtree(child_root, ignore_errors=True)
                raise BrowserAdapterError("staged_input_required")
            if staged_input is not None:
                if Path(staged_input).name != staged_input or not re.fullmatch(r"[0-9a-f]{64}", staged_sha256 or "") or staged_media_type not in {"application/pdf", "text/plain", "text/markdown"}:
                    shutil.rmtree(child_root, ignore_errors=True)
                    raise BrowserAdapterError("staged_input_required")
                env["JOBS_ASSISTANT_STAGED_INPUT_NAME"] = staged_input
                env["JOBS_ASSISTANT_STAGED_INPUT_SHA256"] = staged_sha256
                env["JOBS_ASSISTANT_STAGED_INPUT_MEDIA_TYPE"] = staged_media_type
        elif staged_input is not None or staged_sha256 is not None or staged_media_type is not None:
            shutil.rmtree(child_root, ignore_errors=True)
            raise BrowserAdapterError("staged_input_required")
        if session_manifest is not None:
            env["JOBS_ASSISTANT_SESSION_MANIFEST"] = str(Path(session_manifest).resolve())
        cwd = Path(run_cwd).resolve() if run_cwd is not None else RUNNER.parent
        shot_root = Path(screenshot_root).resolve() if screenshot_root is not None else cwd / "screenshots"
        shot_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        env["JOBS_ASSISTANT_SCREENSHOT_ROOT"] = str(shot_root)
        cwd.mkdir(mode=0o700, parents=True, exist_ok=True)
        process = subprocess.Popen(
            [str(node), str(RUNNER)],
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
            close_fds=True,
        )
        session: PuppeteerSession | None = None
        try:
            observed_owner = _capture_process_identity(int(process.pid))
            if owner_identity is not None and _validate_process_identity(owner_identity, expected_pid=process.pid) != observed_owner:
                raise BrowserAdapterError("process_identity_mismatch")
            session = cls(
                process,
                child_root=child_root,
                nonce=nonce,
                owner_identity=observed_owner,
                session_id=session_id,
                run_id=run_id,
                job_id=job_id,
                ats_policy=selected_ats_policy,
                internal_transport_url=internal_transport_url,
            )
            session._internal_transport_token = internal_token
            hello = session.read_response(timeout=timeout)
            data = hello.get("data") if hello.get("ok") else None
            if not hello.get("ok") or not isinstance(data, dict) or data.get("protocol") != "length-prefixed-json-v1":
                raise BrowserAdapterError("browser_handshake_failed")
            if data.get("identity") != nonce or int(data.get("owner_pid", -1)) != session.owner_pid:
                raise BrowserAdapterError("browser_handshake_identity_mismatch")
            if on_owner_identity is not None:
                try:
                    callback_result = on_owner_identity(dict(observed_owner))
                except Exception as exc:
                    raise BrowserAdapterError("owner_identity_registration_failed") from exc
                if callback_result is False:
                    raise BrowserAdapterError("owner_identity_registration_failed")
            startup = session.request(
                {
                    "action": "startup_identity",
                    "handshake": nonce,
                    "identity": {
                        "version": MANIFEST_VERSION,
                        "ats_policy": selected_ats_policy,
                        "run_id": run_id,
                        "job_id": job_id,
                        "session_id": session_id,
                        "owner_identity": observed_owner,
                        "browser_identity": None,
                    },
                },
                timeout=timeout,
            )
            if startup.get("identity", {}).get("owner_identity") != observed_owner:
                raise BrowserAdapterError("browser_handshake_identity_mismatch")
            launch_data = session.request({"action": "launch", "headless": bool(headless)}, timeout=timeout)
            browser_pid = launch_data.get("browser_pid")
            if type(browser_pid) is not int or browser_pid <= 0:
                raise BrowserAdapterError("browser_process_missing")
            browser_identity = _capture_process_identity(browser_pid)
            session.browser_pid = browser_identity["pid"]
            session.browser_pgid = browser_identity["pgid"]
            session.browser_identity = browser_identity
            registered = session.request({"action": "register_browser_identity", "identity": browser_identity}, timeout=timeout)
            if registered.get("browser_identity") != browser_identity:
                raise BrowserAdapterError("browser_identity_mismatch")
            if on_browser_identity is not None:
                try:
                    callback_result = on_browser_identity(dict(browser_identity))
                except Exception as exc:
                    raise BrowserAdapterError("browser_identity_registration_failed") from exc
                if callback_result is False:
                    raise BrowserAdapterError("browser_identity_registration_failed")
            return session
        except BrowserAdapterError as exc:
            if getattr(exc, "runner_originated", False):
                exc = BrowserAdapterError(normalize_browser_error_code(str(exc)))
            if session is not None:
                session.close(force=True)
            else:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            shutil.rmtree(child_root, ignore_errors=True)
            raise exc from None
        except Exception as exc:
            if session is not None:
                session.close(force=True)
            else:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            shutil.rmtree(child_root, ignore_errors=True)
            raise BrowserAdapterError("browser_start_error") from exc

    def request(self, payload: dict[str, Any], *, timeout: float = 15.0) -> dict[str, Any]:
        with self._request_lock:
            if self._poisoned:
                raise BrowserAdapterError("protocol_poisoned")
            if self._closed:
                raise BrowserAdapterError("browser_session_closed")
            self._write_frame(payload, timeout=timeout)
            response = self.read_response(timeout=timeout)
            if response.get("ok") is False:
                if set(response) != {"ok", "error"} or not isinstance(response.get("error"), str):
                    self._poisoned = True
                    raise BrowserAdapterError("protocol_invalid_response")
                raise BrowserAdapterError(
                    normalize_browser_error_code(response["error"]),
                    runner_originated=True,
                )
            if response.get("ok") is not True or set(response) != {"ok", "data"}:
                self._poisoned = True
                raise BrowserAdapterError("protocol_invalid_response")
            data = response["data"]
            if not isinstance(data, dict) or not _valid_response_data(payload.get("action"), data):
                self._poisoned = True
                raise BrowserAdapterError("protocol_invalid_response")
            return data

    def goto(
        self,
        url: str,
        *,
        timeout_ms: int = 10000,
        ats_policy: str | None = None,
    ) -> dict[str, Any]:
        if ats_policy is not None:
            try:
                requested = validate_ats_policy_name(ats_policy)
            except ValueError as exc:
                raise BrowserAdapterError("unsupported_ats") from exc
            if requested != self.ats_policy:
                raise BrowserAdapterError("ats_policy_mismatch")
        # The loopback fixture is an opaque transport capability, never a URL
        # accepted as an application destination.
        route = validate_ats_url(url, self.ats_policy)
        payload: dict[str, Any] = {
            "action": "goto",
            "ats_policy": self.ats_policy,
            "url": route.url,
            "timeoutMs": timeout_ms,
        }
        if self._internal_transport_url is not None:
            payload.update({
                "internal_url": self._internal_transport_url,
                "internal_token": self._internal_transport_token,
            })
        return self.request(payload, timeout=timeout_ms / 1000 + 5)

    def observe(self) -> dict[str, Any]:
        return self.request({"action": "observe"})

    def fill(self, target_id: str, value: str) -> dict[str, Any]:
        return self.request({"action": "fill", "target_id": str(target_id), "value": value})
    def check(self, target_id: str, value: bool) -> dict[str, Any]:
        if type(value) is not bool:
            raise BrowserAdapterError("invalid_boolean_value")
        return self.request({"action": "check", "target_id": str(target_id), "value": value})
    def select(self, target_id: str, value: str) -> dict[str, Any]:
        return self.request({"action": "select", "target_id": str(target_id), "value": value})

    def upload(self, target_id: str) -> dict[str, Any]:
        return self.request({"action": "upload", "target_id": str(target_id)})

    def click_offline(self, target_id: str) -> dict[str, Any]:
        return self.request({"action": "click", "target_id": str(target_id), "offline": True})

    def screenshot(self, slot: str = "final", *, full_page: bool = False) -> dict[str, Any]:
        return self.request({"action": "screenshot", "slot": slot, "fullPage": bool(full_page)})

    def prepare_handoff(self, *, run_id: int | None = None, job_id: int | None = None) -> dict[str, Any]:
        if run_id is not None and run_id != self.run_id or job_id is not None and job_id != self.job_id:
            raise BrowserAdapterError("startup_identity_mismatch")
        return self.request({
            "action": "prepare_handoff",
            "run_id": self.run_id,
            "job_id": self.job_id,
            "session_id": self.session_id,
        })

    def commit_handoff(self, commit_token: str) -> dict[str, Any]:
        token = str(commit_token)
        response = self.request({"action": "commit_handoff", "commit_token": token})
        if response.get("state") != "open_guarded":
            raise BrowserAdapterError("handoff_commit_failed")
        self._committed_token = token
        return response

    def release_handoff(self) -> dict[str, Any]:
        if self._detached:
            return {"state": "open_guarded", "released": True}
        if not self._committed_token or self._closed or self.process.poll() is not None:
            raise BrowserAdapterError("handoff_state_conflict")
        released = self.request({"action": "release_handoff"})
        if released.get("state") != "open_guarded" or released.get("released") is not True:
            raise BrowserAdapterError("handoff_release_failed")
        # Node's release ACK is the descriptor-ownership linearization point.
        # Only after it has detached from command transport may Python close
        # its pipe ends and return an independently owned review window.
        self._detached = True
        self._closed = True
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except OSError:
            pass
        try:
            self._selector.close()
        except Exception:
            pass
        for stream in (self.process.stdout, self.process.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=0.1)
        process = self.process
        threading.Thread(target=process.wait, name="jobs-assistant-browser-reaper", daemon=True).start()
        return {"state": "open_guarded", "released": True}

    def network_counters(self) -> dict[str, Any]:
        return self.request({"action": "networkCounters"})
    def _fill_receive_buffer(self, deadline: float) -> None:
        if self.process.stdout is None:
            raise BrowserAdapterError("adapter_stdout_missing")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._poisoned = True
                raise BrowserAdapterError("protocol_timeout")
            if not self._selector.select(remaining):
                continue
            try:
                chunk = os.read(self.process.stdout.fileno(), 65536)
            except BlockingIOError:
                continue
            if not chunk:
                self._poisoned = True
                raise BrowserAdapterError("protocol_eof")
            self._recv_buffer.extend(chunk)
            if len(self._recv_buffer) > MAX_OUT_FRAME + 64:
                self._poisoned = True
                raise BrowserAdapterError("output_frame_too_large")
            return

    def _take_exact(self, count: int, deadline: float) -> bytes:
        while len(self._recv_buffer) < count:
            self._fill_receive_buffer(deadline)
        data = bytes(self._recv_buffer[:count])
        del self._recv_buffer[:count]
        return data

    def read_response(self, *, timeout: float = 15.0) -> dict[str, Any]:
        if self.process.stdout is None:
            raise BrowserAdapterError("adapter_stdout_missing")
        deadline = time.monotonic() + timeout
        while b"\n" not in self._recv_buffer:
            self._fill_receive_buffer(deadline)
        newline = self._recv_buffer.index(10)
        prefix = bytes(self._recv_buffer[:newline])
        del self._recv_buffer[: newline + 1]
        try:
            length = _parse_frame_length(prefix)
        except BrowserAdapterError:
            self._poisoned = True
            raise
        body = self._take_exact(length, deadline)
        if length > MAX_OBSERVATION_BYTES:
            self._poisoned = True
            raise BrowserAdapterError("output_frame_too_large")
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._poisoned = True
            raise BrowserAdapterError("protocol_invalid_json") from exc
        if not isinstance(decoded, dict):
            self._poisoned = True
            raise BrowserAdapterError("protocol_non_object")
        return decoded

    def _write_frame(self, payload: dict[str, Any], *, timeout: float) -> None:
        if self.process.stdin is None:
            raise BrowserAdapterError("adapter_stdin_missing")
        try:
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise BrowserAdapterError("invalid_command") from exc
        if len(body) > MAX_IN_FRAME:
            raise BrowserAdapterError("input_frame_too_large")
        frame = f"{len(body)}\n".encode("ascii") + body
        deadline = time.monotonic() + max(0.0, timeout)
        with self._write_lock:
            writer = selectors.DefaultSelector()
            try:
                try:
                    writer.register(self.process.stdin, selectors.EVENT_WRITE)
                except (OSError, ValueError) as exc:
                    self._poisoned = True
                    raise BrowserAdapterError("protocol_eof") from exc
                offset = 0
                while offset < len(frame):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._poisoned = True
                        raise BrowserAdapterError("protocol_timeout")
                    try:
                        written = os.write(self.process.stdin.fileno(), frame[offset:])
                        if written <= 0:
                            self._poisoned = True
                            raise BrowserAdapterError("protocol_eof")
                        offset += written
                    except BlockingIOError:
                        if not writer.select(remaining):
                            self._poisoned = True
                            raise BrowserAdapterError("protocol_timeout")
                    except BrokenPipeError as exc:
                        self._poisoned = True
                        raise BrowserAdapterError("protocol_eof") from exc
                    except OSError as exc:
                        self._poisoned = True
                        raise BrowserAdapterError("protocol_eof") from exc
            finally:
                writer.close()
    def close(self, *, timeout: float = 5.0, force: bool = False) -> None:
        if self._closed:
            return
        if not force and self.process.poll() is None and not self._poisoned:
            try:
                self.request({"action": "close"}, timeout=timeout)
            except Exception:
                force = True
        self._closed = True
        browser_live = _validated_group_live(self.browser_identity)
        owner_live = _validated_group_live(self.owner_identity)
        if force:
            # A registered browser group is the only child group we may signal.
            # If registration never completed, the validated owner group is the
            # bounded fallback needed to reap a failed startup.
            if browser_live:
                try:
                    os.killpg(int(self.browser_identity["pgid"]), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                if self.process.poll() is None:
                    self.process.terminate()
            elif self.browser_identity is None and owner_live:
                try:
                    os.killpg(self.owner_pgid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if _validated_group_live(self.browser_identity):
                try:
                    os.killpg(int(self.browser_identity["pgid"]), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif self.browser_identity is None and _validated_group_live(self.owner_identity):
                try:
                    os.killpg(self.owner_pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if self.process.poll() is None and _validated_group_live(self.owner_identity):
                self.process.kill()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
        try:
            self._selector.close()
        except Exception:
            pass
        if _verified_group_absent(self.owner_identity) and _verified_group_absent(self.browser_identity):
            shutil.rmtree(self._child_root, ignore_errors=True)

    def __enter__(self) -> "PuppeteerSession":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def _read_one_frame(process: subprocess.Popen[bytes], *, timeout: float) -> dict[str, Any]:
    if process.stdout is None:
        raise BrowserAdapterError("adapter_stdout_missing")
    selector = selectors.DefaultSelector()
    try:
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        line = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserAdapterError("protocol_timeout")
            if not selector.select(remaining):
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise BrowserAdapterError("protocol_eof")
            newline = chunk.find(b"\n")
            if newline < 0:
                line.extend(chunk)
                if len(line) > 32:
                    raise BrowserAdapterError("protocol_bad_length")
                continue
            line.extend(chunk[:newline])
            length = _parse_frame_length(line)
            body = bytearray(chunk[newline + 1 :])
            while len(body) < length:
                if time.monotonic() >= deadline:
                    raise BrowserAdapterError("protocol_timeout")
                if not selector.select(max(0.001, deadline - time.monotonic())):
                    continue
                part = os.read(process.stdout.fileno(), length - len(body))
                if not part:
                    raise BrowserAdapterError("protocol_eof")
                body.extend(part)
            decoded = json.loads(bytes(body).decode("utf-8"))
            if not isinstance(decoded, dict):
                raise BrowserAdapterError("protocol_non_object")
            return decoded
    finally:
        selector.close()
