---
name: live-proof-routing
description: >-
  Use when a task involves browser/UI/live workflow proof: local web app UI,
  screenshots, visual verification, embedded browser panes, desktop
  computer-use, CDP/remote-debugging, logged-in Chrome profiles, companion
  services, LinkedIn/Gmail/third-party account state, queues, DB state, or any
  case where tests/API output may be proxy proof. Routes to the lowest-coupling
  method that can prove the real target and applies strict CDP/profile fallback
  rules.
---

# Live Proof Routing

Use this skill when the user asks to verify, inspect, fix, or automate a live UI/browser/external workflow. The goal is to choose the cheapest method that proves the real target without substituting a different browser, profile, runtime, account, or proof class.

Core rule:

> Lowest coupling for gates. Exact target for proof.

## First: Classify The Target

Before acting, identify these four fields. If the answer is knowable from the prompt or repo, do not ask.

```text
Target surface: static URL/API | local web app | embedded browser pane | logged-in Chrome/CDP | desktop UI | app-specific bridge
Identity requirement: none | test auth | real app auth | exact browser profile/session | third-party account
Final proof: response body | DOM state | screenshot | visible UI text | DB row | queue delta | external account/app state
Forbidden substitutes: wrong browser/profile/runtime/account/mock/API-only/test-only proof
```

If the target surface, identity, or final proof changes mid-task, stop and report the change instead of silently adapting.

## Method Ladder

Choose the lowest level that can produce the final proof.

| Level | Method class | Examples | Use when | Avoid when |
|---:|---|---|---|---|
| 0 | Static fetch | `read` URL, `web_search`, docs | static content/API answer is enough | JS/session/visual state matters |
| 1 | Service/API gate | health endpoint, config endpoint, DB query | checking readiness/backend state | claiming UI works |
| 2 | DOM browser automation | OMP `browser`, Puppeteer against local app | local web UI behavior matters | exact user browser profile matters |
| 3 | Embedded browser control | supported embedded browser or pane tooling | target is that embedded browser/pane | target is normal Chrome/user profile |
| 4 | Accessibility/computer-use | desktop UI tree, screenshot, click/type | no DOM/CDP path, visible desktop matters | structured DOM/API proof exists |
| 5 | Identity-bound remote control | CDP to existing Chrome/profile | exact user browser/session/profile matters | a fresh browser can prove it |
| 6 | App-specific bridge | CDP + companion + app + DB/queue | the bridge/integration itself is the product | only UI/API behavior is being checked |

Never jump to level 5/6 unless the acceptance criterion depends on the user's real session/profile or a cross-runtime bridge.

## Gates Versus Final Proof

Cheap checks are gates, not completion proof.

| Check | Gate | Final proof |
|---|---:|---:|
| build/test passes | yes | only for test-only/code-only tasks |
| endpoint returns 200 | yes | no for UI workflows |
| app health/config responds | yes | no for UI workflows |
| CDP responds | yes | no |
| companion responds | yes | no |
| page loads | yes | no |
| DOM state after real interaction | sometimes | often |
| screenshot/visible UI after action | sometimes | yes for visual/UI |
| DB row or queue delta | sometimes | yes for data workflow |
| exact user browser/profile state changed | sometimes | yes for identity-bound workflow |

Do not report a gate as done unless the user explicitly asked only for that gate.

## Standard Workflow

```text
1. Run cheap gates that avoid wasted browser work.
2. Preflight the target surface/session/profile.
3. Perform the smallest real interaction that exercises the request.
4. Capture final proof from the same surface/session/profile/runtime.
5. If the required target is unreachable, stop with the exact failed edge.
```

Examples of cheap gates:

- app health endpoint;
- static asset/API response;
- DB exists or queue count;
- companion health;
- CDP `/json/version`;
- embedded browser runtime available.

## Routing Rules

| Real requirement | First method | Final proof |
|---|---|---|
| Read docs/page/API | `read`/`web_search` | cited content/response |
| Check service alive | API/read/curl/DB | response/log row |
| Local web UI works | OMP `browser` | `tab.observe()`, DOM state, or screenshot after real interaction |
| Visual/component looks right | OMP `browser` screenshot | screenshot at required viewport/state |
| Embedded browser pane | its supported pane/browser commands | pane snapshot/wait/click result |
| Terminal pane/browser | its supported pane tooling | terminal tab/panel state |
| Logged-in Chrome profile | CDP preflight, then bounded CDP attach | target tab/profile verified plus requested state changed |
| Desktop app/browser outside CDP | computer-use/accessibility | visible UI tree/screenshot/action result |
| App-specific external bridge | bridge preflight + app/runtime proof | app UI/DB/queue/external state changed |

## CDP Policy

CDP is an identity-bound integration path, not the default browser. Use CDP only if this statement is true:

```text
The result is invalid unless it comes from an existing browser profile/session/tab.
```

If false, use static fetch, API/DB, OMP `browser`, supported embedded-browser control, or computer-use instead.

### CDP Preflight

Before any CDP workflow:

```text
1. GET the required CDP endpoint, usually /json/version. Timeout: 1s.
2. List targets and find the expected URL/title. Timeout: 2s.
3. Confirm profile/session assumption if visible from target metadata or page state.
4. If an app/container must reach CDP, test from that same runtime. Timeout: 2s.
5. If a companion service is involved, check companion health/reachability. Timeout: 1-2s.
```

If any step fails, stop. Do not launch a fresh Chromium, clone cookies, use a mock profile, or retry blindly.

### CDP Stop Rules

Stop instead of retrying when:

- endpoint is unreachable;
- expected target tab is absent;
- app/runtime cannot reach host CDP;
- companion is unreachable;
- auth/profile identity differs from the requested one;
- the same timeout happens twice without new information.

Preflight should fail in seconds or proceed with a verified target. It should not become a long discovery loop.

## App-Specific Bridge Rule

For flows like LinkedIn DM, Gmail capture, or any companion-mediated third-party workflow, split preconditions from proof.

Preconditions:

- CDP reachable;
- companion reachable;
- app/runtime can reach required endpoints;
- expected browser target/profile/session found.

Final proof must be the requested product state:

- UI changed;
- DB rows changed;
- queue count changed;
- outbox/send/capture state changed;
- external account/app state changed.

Do not treat CDP or companion reachability as final proof.

## Timeout Budgets

| Operation | Default budget |
|---|---:|
| static URL/API read | 1-5s |
| local health/config endpoint | 1-3s |
| open local browser page | 5-10s |
| wait for known selector/text | 5-10s |
| screenshot after loaded page | 5-15s |
| embedded-browser click/wait/snapshot | 3-10s |
| desktop accessibility snapshot | 5-15s |
| CDP `/json/version` | 1s |
| CDP target lookup | 2s |
| companion health | 1-2s |
| app/container to companion/CDP reachability | 2s |

Use shorter budgets for preflight and longer budgets only for a verified real workflow.

## Anti-Patterns

Do not do these:

- Use tests/build/API output as final proof for UI/live workflow tasks.
- Replace the requested browser with a fresh Chromium/profile.
- Clone cookies or mock auth without explicit approval.
- Check host reachability when the app runs in a container and needs container reachability.
- Count CDP/companion reachability as proof of sync/capture/send.
- Use headless Chromium proof for an embedded-pane or desktop-window target.
- Use embedded-pane proof for a required logged-in Chrome profile.
- Continue retrying a high-coupling path after bounded preflight failed.

## Prompt Contract To Emit When Useful

For complex live tasks, write this compact contract before tools:

```text
Target surface: <surface>
Identity requirement: <identity>
Final proof: <artifact/state change>
Method: <lowest-coupling method that can prove it>
Forbidden substitutes: <browser/profile/runtime/mock/proxy proof not allowed>
Stop rule: stop if <required surface/session/profile/runtime> is unreachable
```

Keep it factual and brief. Do not add ceremony for small static/API tasks.

## Next Action

Classify the target, choose the lowest-coupling valid method, run cheap gates, then capture final proof from the exact target surface/session/profile. If the exact target is unavailable, stop with the failed edge instead of substituting a cheaper proof.
