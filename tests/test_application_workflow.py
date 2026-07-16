from __future__ import annotations

import asyncio
import hashlib
import json
import pytest
from pathlib import Path
import jobs_assistant.application as app
from jobs_assistant.ats import ApplicationProfile, LeverAdapter
from jobs_assistant.application_preferences import ApplicationPreferences, PreferenceMapping
from jobs_assistant.contracts import ApplicationClaim, ApplicationContext
from jobs_assistant.artifacts import ArtifactRoot


def _payload():
    return {
        "observation_id": "obs-1", "url": "https://boards.greenhouse.io/fixture/jobs/123", "title": "Apply",
        "site_markers": ["greenhouse"], "fields": [], "buttons": [], "final_submit_target_ids": ["obs-1:frame-0:button-0"],
        "errors": [], "blockers": [],
    }


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("min_length", True),
        ("min_length", -1),
        ("min_length", "1"),
        ("max_length", 1.5),
        ("max_length", -1),
        ("pattern", 1),
        ("min_value", 1),
        ("max_value", False),
        ("step", 1),
        ("step", ["1"]),
    ),
)
def test_observation_rejects_malformed_constraint_types(key, value):
    payload = _payload()
    payload["fields"] = [{
        "target_id": "field",
        "field_key": "field",
        "kind": "text",
        key: value,
    }]
    with pytest.raises(app.BrowserAdapterError, match="protocol_invalid_response"):
        app._observation_from_payload(payload)


def test_observation_accepts_typed_constraints():
    payload = _payload()
    payload["fields"] = [{
        "target_id": "field",
        "field_key": "field",
        "kind": "number",
        "min_length": 0,
        "max_length": 20,
        "pattern": r"[0-9]+",
        "min_value": "1",
        "max_value": "10",
        "step": "1",
    }]
    field = app._observation_from_payload(payload).fields[0]
    assert (field.min_length, field.max_length, field.pattern, field.min_value, field.max_value, field.step) == (
        0, 20, r"[0-9]+", "1", "10", "1"
    )


class FakeSession:
    starts = 0
    releases = 0
    closes = 0
    tokens: list[str] = []

    def __init__(self, manifest):
        self.owner_pid = 1
        self.browser_pid = 2
        self.owner_identity = {"pid": 1, "pgid": 1, "birth": "fake"}
        self.browser_identity = {"pid": 2, "pgid": 2, "birth": "fake"}
        self.manifest = Path(manifest)

    @classmethod
    def start(cls, **kwargs):
        starting = json.loads(Path(kwargs["session_manifest"]).read_text(encoding="utf-8"))
        assert starting["state"] == "starting"
        assert starting["spawn_attempted"] is True
        cls.starts += 1
        if cls.starts == 2:
            raise RuntimeError("browser_start_error")
        return cls(kwargs["session_manifest"])

    def goto(self, url, *, ats_policy=None):
        return {"url": url, "ats_policy": ats_policy}

    def observe(self):
        return _payload()

    def prepare_handoff(self, **kwargs):
        return {"state": "prepared"}

    def commit_handoff(self, token):
        self.tokens.append(token)
        self.manifest.write_text(json.dumps({"state": "open_guarded", "commit_token_sha256": hashlib.sha256(token.encode()).hexdigest()}))
        return {"state": "open_guarded"}

    def release_handoff(self):
        type(self).releases += 1
        return {"state": "open_guarded", "released": True}

    def close(self):
        type(self).closes += 1


def test_handoff_manifest_accepts_only_single_sha256_token_hash(tmp_path):
    manifest = tmp_path / "review_session.json"
    token = "trusted-review-token"
    manifest.write_text(json.dumps({"commit_token": token}))
    assert app._manifest_token_hash(manifest) is None
    manifest.write_text(json.dumps({"commit_token_sha256": hashlib.sha256(token.encode()).hexdigest()}))
    assert app._manifest_token_hash(manifest) == hashlib.sha256(token.encode()).hexdigest()


def test_limit_two_claims_independent_handoffs_and_later_launch_failure(monkeypatch, tmp_path):
    claims = [
        ApplicationClaim(11, {"id": 1, "canonical_url": "https://boards.greenhouse.io/a/jobs/1", "title": "A", "description": "Exact listing description"}),
        ApplicationClaim(12, {"id": 2, "canonical_url": "https://boards.greenhouse.io/b/jobs/2", "title": "B"}),
        ApplicationClaim(13, {"id": 3, "canonical_url": "https://boards.greenhouse.io/c/jobs/3", "title": "C"}),
    ]
    finished = []
    monkeypatch.setattr(app, "PuppeteerSession", FakeSession)
    monkeypatch.setattr(app, "claim_next_application_job", lambda conn, owner: claims.pop(0) if claims else None)
    monkeypatch.setattr(app, "register_application_artifact", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "register_application_session", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "register_application_owner_process", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "register_application_browser_process", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "finish_application_run", lambda *args, **kwargs: finished.append(kwargs))
    resume = tmp_path / "resume.txt"
    resume.write_text("A resume")
    root = tmp_path / "artifacts"
    result = asyncio.run(app.run_application_workflow(object(), limit=2, resume_file=resume, artifact_root=root, headed=True))
    assert [entry["run_id"] for entry in result] == [11, 12]
    assert result[0]["window_state"] == "open"
    assert result[1]["reason_code"] == "browser_error"
    assert FakeSession.releases == 1
    assert finished[0]["reason_code"] == "draft_ready"
    assert (root / "run-11" / "run.json").exists()
    assert (root / "run-12" / "run.json").exists()
    manifest = json.loads((root / "run-11" / "run.json").read_text(encoding="utf-8"))
    assert manifest["latest"] == {"iteration": 1, "stage": "prepared"}
    assert manifest["latest_iteration"] == 1
    assert manifest["latest_stage"] == "prepared"
    for key in ("claim", "input", "observation", "plan", "actions", "filled_state"):
        indexed = manifest["artifacts"][key]
        artifact_path = root / "run-11" / indexed["path"]
        assert indexed["sha256"] == hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        assert indexed["iteration"] == (0 if key in {"claim", "input"} else 1)
        assert indexed["stage"] == ("claimed" if key in {"claim", "input"} else "prepared")
    assert "job" not in manifest
    assert manifest["artifacts"]["claim"]["path"] == "claim.json"
    description_path = root / "run-11" / "job_description.txt"
    assert description_path.read_text(encoding="utf-8") == "Exact listing description"
    assert manifest["artifacts"]["job_description"]["path"] == "job_description.txt"
    assert manifest["artifacts"]["job_description"]["sha256"] == hashlib.sha256(
        b"Exact listing description"
    ).hexdigest()
    second_manifest = json.loads((root / "run-12" / "run.json").read_text(encoding="utf-8"))
    assert "job_description" not in second_manifest["artifacts"]
    assert not (root / "run-12" / "job_description.txt").exists()
    assert finished[0]["artifact_dir"] != finished[1]["artifact_dir"]
    assert all(entry["ats"] == "greenhouse" for entry in result)
    assert [claim.run_id for claim in claims] == [13]

def test_workflow_keeps_listing_description_separate_from_profile_summary(monkeypatch, tmp_path: Path):
    listing_description = "LISTING_SENTINEL"
    applicant_description = "APPLICANT_SENTINEL"
    claims = [ApplicationClaim(
        14,
        {
            "id": 14,
            "canonical_url": "https://boards.greenhouse.io/a/jobs/14",
            "title": "A",
            "description": listing_description,
        },
    )]
    calls: list[dict[str, object]] = []

    class DescriptionSession(FakeSession):
        starts = 0

        def observe(self):
            payload = _payload()
            payload["fields"] = [{
                "target_id": "field-infer",
                "field_key": "question",
                "kind": "text",
                "visible": True,
                "enabled": True,
                "value": None,
                "valid": True,
                "will_validate": True,
            }]
            return payload

    monkeypatch.setattr(app, "PuppeteerSession", DescriptionSession)
    monkeypatch.setattr(app, "claim_next_application_job", lambda conn, owner: claims.pop(0) if claims else None)
    for name in ("register_application_artifact", "register_application_session", "register_application_owner_process", "register_application_browser_process"):
        monkeypatch.setattr(app, name, lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "finish_application_run", lambda *args, **kwargs: None)

    def resolve(*args, **kwargs):
        calls.append(kwargs)
        return app.AutofillPlan()

    monkeypatch.setattr(app, "resolve_with_llm", resolve)
    resume = tmp_path / "resume.txt"
    resume.write_text("A resume")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"resume_summary": applicant_description}))

    result = asyncio.run(app.run_application_workflow(
        object(),
        resume_file=resume,
        application_profile_json=profile,
        artifact_root=tmp_path / "artifacts",
    ))

    assert result[0]["reason_code"] == "draft_ready"
    assert calls
    assert calls[0]["job_description"] == listing_description
    assert calls[0]["applicant_description"] == applicant_description
    assert calls[0]["job_description"] != calls[0]["applicant_description"]

def test_lever_auto_workflow_persists_policy_and_no_final_submit(monkeypatch, tmp_path: Path) -> None:
    url = "https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000"
    claims = [ApplicationClaim(71, {"id": 71, "canonical_url": url, "title": "Lever"})]
    calls: list[dict[str, object]] = []

    class LeverSession(FakeSession):
        @classmethod
        def start(cls, **kwargs):
            calls.append(kwargs)
            return cls(kwargs["session_manifest"])

        def goto(self, value, **kwargs):
            assert value == url
            assert kwargs["ats_policy"] == "lever"

        def observe(self):
            payload = _payload()
            payload["url"] = url
            payload["site_markers"] = ["lever"]
            return payload

    monkeypatch.setattr(app, "PuppeteerSession", LeverSession)
    monkeypatch.setattr(app, "claim_next_application_job", lambda conn, owner: claims.pop(0) if claims else None)
    for name in ("register_application_artifact", "register_application_session", "register_application_owner_process", "register_application_browser_process"):
        monkeypatch.setattr(app, name, lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "finish_application_run", lambda *args, **kwargs: None)
    resume = tmp_path / "resume.txt"
    resume.write_text("A resume")
    root = tmp_path / "artifacts"
    result = asyncio.run(app.run_application_workflow(object(), resume_file=resume, artifact_root=root, ats="auto"))
    assert result[0]["ats"] == "lever"
    manifest = json.loads((root / "run-71" / "run.json").read_text(encoding="utf-8"))
    assert manifest["ats_policy"] == "lever"
    assert manifest["no_final_submit"] is True
    assert calls[0]["ats_policy"] == "lever"


def test_iteration_action_evidence_is_durable_before_mutation(monkeypatch, tmp_path: Path) -> None:
    claims = [ApplicationClaim(73, {"id": 73, "canonical_url": "https://boards.greenhouse.io/a/jobs/73", "title": "Evidence"})]
    deterministic_plan = app.AutofillPlan(
        answers=(
            app.FieldAnswer("safe-field", "Ada", 1.0, "configured", "profile"),
            app.FieldAnswer("rejected-field", "ignored", 1.0, "configured", "profile"),
        ),
        status="ready",
        reason_code=app.PublicReasonCode.draft_ready,
    )

    class EvidenceSession(FakeSession):
        @classmethod
        def start(cls, **kwargs):
            return cls(kwargs["session_manifest"])

        def __init__(self, manifest):
            super().__init__(manifest)
            self.observations = 0
            self.filled = False

        def observe(self):
            self.observations += 1
            payload = _payload()
            payload["fields"] = [
                {
                    "target_id": "safe-field",
                    "field_key": "safe",
                    "kind": "text",
                    "visible": True,
                    "enabled": True,
                    "value": "Ada" if self.filled else None,
                    "valid": True,
                    "will_validate": True,
                },
                {
                    "target_id": "rejected-field",
                    "field_key": "rejected",
                    "kind": "text",
                    "visible": False,
                    "enabled": True,
                    "value": None,
                    "valid": True,
                    "will_validate": True,
                },
            ]
            return payload

        def fill(self, target_id, value):
            assert target_id == "safe-field"
            evidence_path = self.manifest.parent / "iterations" / "0001" / "action_evidence.json"
            assert evidence_path.exists()
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            assert evidence["ats_policy"] == "greenhouse"
            assert evidence["observation_id"] == "obs-1"
            assert evidence["no_final_submit"] is True
            assert evidence["planned"] == [{
                "target_id": "safe-field",
                "action": "fill",
                "kind": "text",
                "source": "profile",
                "value_length": 3,
            }]
            assert evidence["rejected"] == [{
                "target_id": "rejected-field",
                "action": "fill",
                "reason": "ineligible_field",
            }]
            manifest = json.loads((self.manifest.parent / "run.json").read_text(encoding="utf-8"))
            indexed = manifest["iterations"]["1"]["artifacts"]["action_evidence"]
            assert indexed["sha256"] == hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            observation_path = self.manifest.parent / "iterations" / "0001" / "observation.json"
            assert observation_path.exists()
            snapshot = json.loads(observation_path.read_text(encoding="utf-8"))
            assert snapshot["observation_id"] == "obs-1"
            assert snapshot["fields"][0]["target_id"] == "safe-field"
            assert snapshot["fields"][0]["selector"] == ""
            assert snapshot["fields"][0]["frame_id"] == ""
            observation_sha256 = hashlib.sha256(observation_path.read_bytes()).hexdigest()
            assert observation_sha256 == app._observation_snapshot_sha256(app._observation_from_payload(snapshot))
            assert evidence["observation_artifact"] == "iterations/0001/observation.json"
            assert evidence["observation_sha256"] == observation_sha256
            indexed_observation = manifest["iterations"]["1"]["artifacts"]["observation"]
            assert indexed_observation["path"] == "iterations/0001/observation.json"
            assert indexed_observation["sha256"] == observation_sha256
            assert indexed_observation["sha256"] == evidence["observation_sha256"]
            assert manifest["no_final_submit"] is True
            self.filled = True

    monkeypatch.setattr(app, "PuppeteerSession", EvidenceSession)
    monkeypatch.setattr(app, "claim_next_application_job", lambda conn, owner: claims.pop(0) if claims else None)
    for name in ("register_application_artifact", "register_application_session", "register_application_owner_process", "register_application_browser_process"):
        monkeypatch.setattr(app, name, lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "finish_application_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "_configured_and_profile_plan", lambda *args, **kwargs: deterministic_plan)
    monkeypatch.setattr(app, "resolve_with_llm", lambda *args, **kwargs: app.AutofillPlan())

    resume = tmp_path / "resume.txt"
    resume.write_text("resume")
    root = tmp_path / "artifacts"
    result = asyncio.run(app.run_application_workflow(object(), resume_file=resume, artifact_root=root))

    assert result[0]["status"] == "review_ready"
    run_dir = root / "run-73"
    evidence_path = run_dir / "iterations" / "0001" / "action_evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["iteration"] == 1
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    indexed = manifest["iterations"]["1"]["artifacts"]["action_evidence"]
    assert indexed["sha256"] == hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    assert manifest["no_final_submit"] is True
    actions = json.loads((run_dir / "actions.json").read_text(encoding="utf-8"))
    assert actions["final_submit_calls"] == 0

def test_configured_resume_upload_is_retained_on_reobserve_without_submit(monkeypatch, tmp_path: Path):
    claims = [ApplicationClaim(74, {"id": 74, "canonical_url": "https://boards.greenhouse.io/a/jobs/74", "title": "Resume"})]

    class ResumeContinuationSession(FakeSession):
        starts = 0
        instances = []

        @classmethod
        def start(cls, **kwargs):
            cls.starts += 1
            session = cls(kwargs["session_manifest"])
            cls.instances.append(session)
            return session

        def __init__(self, manifest):
            super().__init__(manifest)
            self.observations = 0
            self.upload_calls = 0
            self.uploaded = False

        def observe(self):
            self.observations += 1
            payload = _payload()
            payload["fields"] = [{
                "target_id": "resume-field",
                "field_key": "resume",
                "kind": "file",
                "name": "resume",
                "label": "Resume",
                "required": True,
                "visible": True,
                "enabled": True,
                "readonly": False,
                "value": None,
                "will_validate": True,
                "valid": True,
                "file_count": 1 if self.uploaded else 0,
                "file_basenames": ["resume.txt"] if self.uploaded else [],
                "accept": [".txt"],
            }]
            return payload

        def upload(self, target_id):
            assert target_id == "resume-field"
            self.upload_calls += 1
            self.uploaded = True

    monkeypatch.setattr(app, "PuppeteerSession", ResumeContinuationSession)
    monkeypatch.setattr(app, "claim_next_application_job", lambda conn, owner: claims.pop(0) if claims else None)
    for name in ("register_application_artifact", "register_application_session", "register_application_owner_process", "register_application_browser_process"):
        monkeypatch.setattr(app, name, lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "finish_application_run", lambda *args, **kwargs: None)

    resume = tmp_path / "resume.txt"
    resume.write_text("resume")
    root = tmp_path / "artifacts"
    result = asyncio.run(app.run_application_workflow(object(), resume_file=resume, artifact_root=root))

    assert result[0]["status"] == "review_ready"
    session = ResumeContinuationSession.instances[0]
    assert session.upload_calls == 1
    assert session.observations == 3
    run_dir = root / "run-74"
    retained_snapshot = json.loads((run_dir / "iterations" / "0002" / "observation.json").read_text(encoding="utf-8"))
    assert retained_snapshot["fields"][0]["file_count"] == 1
    actions = json.loads((run_dir / "actions.json").read_text(encoding="utf-8"))
    assert actions["mutation_count"] == 1
    assert [item["action"] for item in actions["actions"]] == ["upload"]
    assert actions["final_submit_calls"] == 0



def test_lever_button_only_workflow_clicks_policy_aware_continue(monkeypatch, tmp_path: Path) -> None:
    url = "https://jobs.eu.lever.co/acme/123e4567-e89b-12d3-a456-426614174000/apply"
    claims = [ApplicationClaim(72, {"id": 72, "canonical_url": url, "title": "Lever"})]
    resolve_calls: list[dict[str, object]] = []
    evidence_policies: list[str | None] = []

    class LeverButtonSession(FakeSession):
        observes = 0
        clicks: list[str] = []

        @classmethod
        def start(cls, **kwargs):
            return cls(kwargs["session_manifest"])

        def observe(self):
            type(self).observes += 1
            payload = _payload()
            payload["url"] = url
            payload["site_markers"] = ["lever"]
            if self.observes == 1:
                payload["buttons"] = [{
                    "target_id": "continue",
                    "frame_id": "frame-0",
                    "frame_url": url,
                    "click_key": "continue-key",
                    "element_kind": "button",
                    "button_type": "button",
                    "text": "Continue",
                    "visible": True,
                    "enabled": True,
                    "safety_descriptors": [],
                }]
            else:
                payload["buttons"] = []
            return payload

        def click_offline(self, target_id, continuation=False):
            type(self).clicks.append(target_id)
            assert continuation is False
            return {"clicked": True, "counters": {}}

    monkeypatch.setattr(app, "PuppeteerSession", LeverButtonSession)
    monkeypatch.setattr(app, "claim_next_application_job", lambda conn, owner: claims.pop(0) if claims else None)
    for name in ("register_application_artifact", "register_application_session", "register_application_owner_process", "register_application_browser_process"):
        monkeypatch.setattr(app, name, lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "finish_application_run", lambda *args, **kwargs: None)

    def resolve(*args, **kwargs):
        resolve_calls.append(kwargs)
        return app.AutofillPlan(
            safe_click_target_id="continue",
            status="ready",
            reason_code=app.PublicReasonCode.draft_ready,
        )

    original_evidence = app.plan_action_evidence
    def evidence(*args, **kwargs):
        evidence_policies.append(kwargs.get("ats_policy"))
        return original_evidence(*args, **kwargs)

    monkeypatch.setattr(app, "resolve_with_llm", resolve)
    monkeypatch.setattr(app, "plan_action_evidence", evidence)
    resume = tmp_path / "resume.txt"
    resume.write_text("A resume")
    root = tmp_path / "artifacts"
    result = asyncio.run(app.run_application_workflow(object(), resume_file=resume, artifact_root=root, ats="auto"))

    assert result[0]["status"] == "review_ready"
    assert result[0]["reason_code"] == "draft_ready"
    assert LeverButtonSession.clicks == ["continue"]
    assert resolve_calls and resolve_calls[0]["ats_policy"] == "lever"
    assert evidence_policies and evidence_policies[0] == "lever"
    actions = json.loads((root / "run-72" / "actions.json").read_text(encoding="utf-8"))
    assert actions["actions"][0]["action"] == "click"
    assert actions["final_submit_calls"] == 0
def test_lever_eu_safe_action_origin_is_allowed() -> None:
    payload = _payload()
    button = {
        "target_id": "obs-1:frame-0:button-0",
        "frame_id": "frame-0",
        "frame_url": "https://jobs.eu.lever.co/acme/123e4567-e89b-12d3-a456-426614174000/apply",
        "click_key": "click-safe",
        "element_kind": "button",
        "button_type": "button",
        "enabled": True,
        "visible": True,
    }
    payload["url"] = button["frame_url"]
    payload["site_markers"] = ["lever"]
    payload["buttons"] = [button]
    observation = app._observation_from_payload(payload)
    assert app._safe_click_is_eligible(observation.buttons[0], ats_policy="lever")

    api_button = dict(button, frame_url="https://api.lever.co/acme/123")
    api_observation = app._observation_from_payload({**payload, "buttons": [api_button]})
    assert not app._safe_click_is_eligible(api_observation.buttons[0], ats_policy="lever")

    for port in (444, 8443):
        blocked = dict(button, frame_url=f"https://jobs.eu.lever.co:{port}/acme/123e4567-e89b-12d3-a456-426614174000/apply")
        blocked_observation = app._observation_from_payload({**payload, "buttons": [blocked]})
        assert not app._safe_click_is_eligible(blocked_observation.buttons[0], ats_policy="lever")
    default_port = dict(button, frame_url="https://jobs.eu.lever.co:443/acme/123e4567-e89b-12d3-a456-426614174000/apply")
    default_observation = app._observation_from_payload({**payload, "buttons": [default_port]})
    assert app._safe_click_is_eligible(default_observation.buttons[0], ats_policy="lever")

def test_safe_click_allows_nonfinal_submit_continuation_and_denies_unsafe_controls() -> None:
    page_url = "https://boards.greenhouse.io/acme/jobs/123"
    base = {
        "target_id": "continue",
        "frame_id": "frame-0",
        "frame_url": page_url,
        "click_key": "click-safe",
        "element_kind": "button",
        "button_type": "button",
        "text": "Continue",
        "visible": True,
        "enabled": True,
        "safety_descriptors": [],
    }

    def eligible(button: dict[str, object], final_ids: list[str] | None = None) -> bool:
        payload = {
            **_payload(),
            "url": page_url,
            "buttons": [button],
            "final_submit_target_ids": final_ids or [],
        }
        observation = app._observation_from_payload(payload)
        return app._safe_click_is_eligible(
            observation.buttons[0],
            observation.final_submit_target_ids,
            ats_policy="greenhouse",
            page_url=observation.url,
        )

    assert eligible(base)
    submit = dict(base, button_type="submit")
    assert eligible(submit)
    assert not eligible(dict(base, target="_blank"))
    assert not eligible(dict(submit, effective_action_url=page_url, effective_method="post"))
    assert not eligible(dict(submit, frame_url="https://evil.example/acme/jobs/123"))
    assert not eligible(dict(submit, download=True))
    assert not eligible(dict(submit, safety_descriptors=["ssn"]))
    assert not eligible(dict(submit, target_id="final"), final_ids=["final"])


def test_workflow_dispatches_submit_continuation_through_offline_protocol(monkeypatch, tmp_path: Path) -> None:
    claims = [ApplicationClaim(73, {"id": 73, "canonical_url": "https://boards.greenhouse.io/a/jobs/73", "title": "Continuation"})]

    class SubmitContinuationSession(FakeSession):
        observes = 0
        clicks: list[tuple[str, bool]] = []

        @classmethod
        def start(cls, **kwargs):
            return cls(kwargs["session_manifest"])

        def observe(self):
            type(self).observes += 1
            payload = _payload()
            if self.observes == 1:
                payload["buttons"] = [{
                    "target_id": "continue",
                    "frame_id": "frame-0",
                    "frame_url": payload["url"],
                    "click_key": "continue-key",
                    "element_kind": "button",
                    "button_type": "submit",
                    "text": "Continue",
                    "visible": True,
                    "enabled": True,
                    "safety_descriptors": [],
                }]
            else:
                payload["buttons"] = []
            return payload

        def click_offline(self, target_id, continuation=False):
            type(self).clicks.append((target_id, continuation))
            return {"clicked": True, "counters": {}}

    monkeypatch.setattr(app, "PuppeteerSession", SubmitContinuationSession)
    monkeypatch.setattr(app, "claim_next_application_job", lambda conn, owner: claims.pop(0) if claims else None)
    for name in ("register_application_artifact", "register_application_session", "register_application_owner_process", "register_application_browser_process"):
        monkeypatch.setattr(app, name, lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "finish_application_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        app,
        "resolve_with_llm",
        lambda *args, **kwargs: app.AutofillPlan(
            safe_click_target_id="continue",
            status="ready",
            reason_code=app.PublicReasonCode.draft_ready,
        ),
    )

    resume = tmp_path / "resume.txt"
    resume.write_text("A resume")
    root = tmp_path / "artifacts"
    result = asyncio.run(app.run_application_workflow(object(), resume_file=resume, artifact_root=root))

    assert result[0]["status"] == "review_ready"
    assert SubmitContinuationSession.clicks == [("continue", True)]
    actions = json.loads((root / "run-73" / "actions.json").read_text(encoding="utf-8"))
    assert actions["final_submit_calls"] == 0
def test_frame_origin_unknown_ats_policy_denies_greenhouse_origin() -> None:
    assert app._frame_origin_allowed(
        "https://boards.greenhouse.io/acme/jobs/123",
        ats_policy="greenhosue",
    ) is False

def test_lever_preferences_fill_only_unanswered_safe_field(tmp_path: Path) -> None:
    url = "https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000"
    payload = _payload()
    payload["url"] = url
    payload["site_markers"] = ["lever"]
    payload["fields"] = [{
        "target_id": "email",
        "field_key": "email",
        "frame_id": "frame-0",
        "frame_url": url,
        "kind": "email",
        "name": "email",
        "label": "Email",
        "visible": True,
        "enabled": True,
        "required": True,
        "valid": True,
        "will_validate": True,
    }]
    observation = app._observation_from_payload(payload)
    preferences = ApplicationPreferences(
        1,
        (PreferenceMapping("lever", "email", None, "email", "ada@example.test"),),
        (),
        (),
    )
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("resume")
    with app.load_resume_context(resume_path) as resume:
        plan = app._configured_and_profile_plan(
            observation,
            adapter=LeverAdapter(),
            context=ApplicationContext(profile_facts={}, resume_available=True),
            profile=ApplicationProfile(),
            resume=resume,
            preferences=preferences,
        )
    assert [(answer.target_id, answer.value) for answer in plan.answers] == [("email", "ada@example.test")]

def test_profile_and_preference_provenance_uses_loaded_snapshot_after_replacement(monkeypatch, tmp_path: Path) -> None:
    preset_dir = tmp_path / "presets"
    preset_dir.mkdir()
    preset_path = preset_dir / "default.json"
    preset_path.write_text(json.dumps({
        "schema_version": 1,
        "name": "default",
        "profile": {"first_name": "Ada"},
    }))
    original_preset_raw = preset_path.read_bytes()
    replacement_preset_raw = json.dumps({
        "schema_version": 1,
        "name": "default",
        "profile": {"first_name": "Replacement"},
    }).encode()
    preferences_path = tmp_path / "preferences.json"
    preferences_path.write_text(json.dumps({
        "schema_version": 1,
        "mappings": [],
        "opt_outs": [],
        "review_order": [],
    }))
    original_preferences_raw = preferences_path.read_bytes()
    replacement_preferences_raw = b'{"schema_version":1,"mappings":[],"opt_outs":[],"review_order":[]}'
    claims = [ApplicationClaim(72, {"id": 72, "canonical_url": "https://boards.greenhouse.io/a/jobs/72", "title": "Preset"})]
    claimed = {"value": False}

    class PresetSession(FakeSession):
        @classmethod
        def start(cls, **kwargs):
            return cls(kwargs["session_manifest"])

    monkeypatch.setattr(app, "PuppeteerSession", PresetSession)
    original_profile_loader = app.load_application_profile_preset
    def load_profile_then_replace(*args, **kwargs):
        loaded = original_profile_loader(*args, **kwargs)
        preset_path.write_bytes(replacement_preset_raw)
        return loaded
    monkeypatch.setattr(app, "load_application_profile_preset", load_profile_then_replace)
    original_preferences_loader = app.load_application_preferences
    def load_preferences_then_replace(path, *, cwd):
        loaded = original_preferences_loader(path, cwd=tmp_path)
        preferences_path.write_bytes(replacement_preferences_raw)
        return loaded
    monkeypatch.setattr(app, "load_application_preferences", load_preferences_then_replace)
    def claim(conn, owner):
        claimed["value"] = True
        return claims.pop(0) if claims else None
    monkeypatch.setattr(app, "claim_next_application_job", claim)
    for name in ("register_application_artifact", "register_application_session", "register_application_owner_process", "register_application_browser_process"):
        monkeypatch.setattr(app, name, lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "finish_application_run", lambda *args, **kwargs: None)
    resume = tmp_path / "resume.txt"
    resume.write_text("resume")
    root = tmp_path / "artifacts"
    result = asyncio.run(app.run_application_workflow(
        object(),
        resume_file=resume,
        artifact_root=root,
        application_profile_preset="default",
        application_profile_dir=preset_dir,
        application_preferences=preferences_path,
    ))
    assert claimed["value"] is True
    assert result[0]["run_id"] == 72
    manifest = json.loads((root / "run-72" / "run.json").read_text())
    provenance = manifest["inputs"]["application_profile_preset"]
    assert provenance["name"] == "default"
    assert provenance["content_sha256"] == hashlib.sha256(original_preset_raw).hexdigest()
    assert provenance["content_sha256"] != hashlib.sha256(replacement_preset_raw).hexdigest()
    preference_provenance = manifest["inputs"]["application_preferences"]
    assert preference_provenance["sha256"] == hashlib.sha256(original_preferences_raw).hexdigest()
    assert preference_provenance["sha256"] != hashlib.sha256(replacement_preferences_raw).hexdigest()
    assert "Ada" not in json.dumps(manifest)


def test_job_failure_still_claims_later_jobs_until_limit(monkeypatch, tmp_path):
    FakeSession.starts = 0
    claims = [
        ApplicationClaim(index, {"id": index, "canonical_url": f"https://boards.greenhouse.io/a/jobs/{index}", "title": "A"})
        for index in (41, 42, 43)
    ]
    finished = []
    monkeypatch.setattr(app, "PuppeteerSession", FakeSession)
    monkeypatch.setattr(app, "claim_next_application_job", lambda conn, owner: claims.pop(0) if claims else None)
    monkeypatch.setattr(app, "register_application_artifact", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "register_application_session", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "register_application_owner_process", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "register_application_browser_process", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "finish_application_run", lambda *args, **kwargs: finished.append(kwargs))
    resume = tmp_path / "resume.txt"
    resume.write_text("A resume")
    result = asyncio.run(app.run_application_workflow(object(), limit=3, resume_file=resume, artifact_root=tmp_path / "artifacts"))
    assert [entry["run_id"] for entry in result] == [41, 42, 43]
    assert [entry["reason_code"] for entry in result] == ["draft_ready", "browser_error", "draft_ready"]
    assert claims == []
    assert len(finished) == 3


def test_invalid_optional_prefill_cannot_be_draft_ready(monkeypatch, tmp_path):
    class InvalidSession(FakeSession):
        starts = 0
        @classmethod
        def start(cls, **kwargs):
            return cls(kwargs["session_manifest"])
        def observe(self):
            payload = _payload()
            payload["fields"] = [{
                "target_id": "field-invalid",
                "field_key": "optional",
                "kind": "text",
                "visible": True,
                "enabled": True,
                "value": "bad",
                "valid": False,
                "will_validate": True,
            }]
            return payload

    claims = [ApplicationClaim(51, {"id": 51, "canonical_url": "https://boards.greenhouse.io/a/jobs/51", "title": "A"})]
    monkeypatch.setattr(app, "PuppeteerSession", InvalidSession)
    monkeypatch.setattr(app, "claim_next_application_job", lambda conn, owner: claims.pop(0) if claims else None)
    monkeypatch.setattr(app, "register_application_artifact", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "register_application_session", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "register_application_owner_process", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "register_application_browser_process", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "finish_application_run", lambda *args, **kwargs: None)
    resume = tmp_path / "resume.txt"
    resume.write_text("A resume")
    result = asyncio.run(app.run_application_workflow(object(), limit=1, resume_file=resume, artifact_root=tmp_path / "artifacts"))
    assert result[0]["reason_code"] == "page_validation_error"
    assert result[0]["status"] == "manual"


def test_retention_requires_exact_string_value() -> None:
    from jobs_assistant.application import _retained_value_equal
    from jobs_assistant.contracts import ObservedField

    field = ObservedField(
        target_id="f", field_key="f", frame_id="frame", frame_url="https://boards.greenhouse.io/a/jobs/1",
        form_action_url=None, kind="text", name="question", label="", group_id=None, option_value=None,
        safety_descriptors=(), selector="#f", required=False, visible=True, enabled=True, readonly=False,
        value="Ada-Lovelace", will_validate=True, valid=True, validity_flags=(), file_count=0,
        file_basenames=(), accept=(), min_length=None, max_length=None, pattern=None, min_value=None,
        max_value=None, step=None, options=(),
    )
    assert not _retained_value_equal(field, "Ada Lovelace")



def test_cached_safe_click_disappearance_fails_manual(monkeypatch, tmp_path):
    class RebindSession(FakeSession):
        starts = 0
        observes = 0
        @classmethod
        def start(cls, **kwargs):
            return cls(kwargs["session_manifest"])
        def observe(self):
            type(self).observes += 1
            payload = _payload()
            payload["fields"] = [{
                "target_id": "field-infer",
                "field_key": "question",
                "kind": "text",
                "visible": True,
                "enabled": True,
                "value": None if self.observes == 1 else "Ada",
                "valid": True,
                "will_validate": True,
            }]
            if self.observes == 1:
                payload["buttons"] = [{
                    "target_id": "safe-button",
                    "frame_id": "frame-0",
                    "frame_url": payload["url"],
                    "click_key": "click-key",
                    "element_kind": "button",
                    "button_type": "button",
                    "visible": True,
                    "enabled": True,
                    "safety_descriptors": [],
                }]
            return payload
        def fill(self, target_id, value):
            return None

    monkeypatch.setattr(app, "PuppeteerSession", RebindSession)
    monkeypatch.setattr(
        app,
        "resolve_with_llm",
        lambda *args, **kwargs: app.AutofillPlan(
            answers=(app.FieldAnswer("field-infer", "Ada", 0.9, "answer", "inference"),),
            safe_click_target_id="safe-button",
            status="ready",
            reason_code=app.PublicReasonCode.draft_ready,
        ),
    )
    claims = [ApplicationClaim(61, {"id": 61, "canonical_url": "https://boards.greenhouse.io/a/jobs/61", "title": "A"})]
    monkeypatch.setattr(app, "claim_next_application_job", lambda conn, owner: claims.pop(0) if claims else None)
    monkeypatch.setattr(app, "register_application_artifact", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "register_application_session", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "register_application_owner_process", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "register_application_browser_process", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "finish_application_run", lambda *args, **kwargs: None)
    resume = tmp_path / "resume.txt"
    resume.write_text("A resume")
    result = asyncio.run(app.run_application_workflow(object(), limit=1, resume_file=resume, artifact_root=tmp_path / "artifacts"))
    assert result[0]["reason_code"] == "safe_click_no_progress"
    assert result[0]["status"] == "manual"

def test_resume_context_is_closed_on_workflow_failure(monkeypatch, tmp_path):
    closed = []
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("x")
    original = app.load_resume_context(resume_path)

    class Tracking:
        text = original.text
        basename = original.basename
        media_type = original.media_type
        sha256 = original.sha256
        facts = original.facts
        def __enter__(self):
            return self
        def __exit__(self, *args):
            closed.append(True)
            original.close()
        def fileno(self):
            return original.fileno()

    monkeypatch.setattr(app, "load_resume_context", lambda path: Tracking())
    monkeypatch.setattr(app, "claim_next_application_job", lambda conn, owner: (_ for _ in ()).throw(RuntimeError("db")))
    try:
        asyncio.run(app.run_application_workflow(object(), limit=1, resume_file=resume_path, artifact_root=tmp_path / "artifacts"))
    except RuntimeError:
        pass
    assert closed


def test_release_failure_does_not_rewrite_committed_handoff(monkeypatch, tmp_path):
    class ReleaseFailureSession(FakeSession):
        starts = 0
        @classmethod
        def start(cls, **kwargs):
            cls.starts += 1
            return cls(kwargs["session_manifest"])
        def release_handoff(self):
            raise RuntimeError("reaper delayed")

    claims = [ApplicationClaim(21, {"id": 21, "canonical_url": "https://boards.greenhouse.io/a/jobs/21", "title": "A"})]
    finished = []
    monkeypatch.setattr(app, "PuppeteerSession", ReleaseFailureSession)
    monkeypatch.setattr(app, "claim_next_application_job", lambda conn, owner: claims.pop(0) if claims else None)
    monkeypatch.setattr(app, "register_application_artifact", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "register_application_session", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "register_application_owner_process", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "register_application_browser_process", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "finish_application_run", lambda *args, **kwargs: finished.append(kwargs))
    resume = tmp_path / "resume.txt"
    resume.write_text("A resume")
    result = asyncio.run(app.run_application_workflow(object(), limit=1, resume_file=resume, artifact_root=tmp_path / "artifacts", headed=True))
    assert result[0]["window_state"] == "unknown"
    assert ReleaseFailureSession.closes >= 1
    assert result[0]["status"] == "review_ready"
    assert finished[0]["reason_code"] == "draft_ready"


def test_review_annotation_persists_indexes_and_deduplicates(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("review note", encoding="utf-8")
    with ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path) as root:
        with root.create_run_dir(31) as run:
            run.write_json("run.json", {"run_id": 31, "job_id": 9, "stage": "finished"})
            first = app.persist_review_annotation(run, source)
            second = app.persist_review_annotation(run, source)
            assert second == first
            manifest = run.read_json("run.json")
            assert manifest["annotations"] == [{
                "artifact_ref": first["artifact_ref"],
                "sha256": hashlib.sha256(b"review note").hexdigest(),
                "chars": len("review note"),
            }]


def test_review_annotation_quota_allows_verified_duplicate_but_rejects_eleventh(tmp_path):
    sources = []
    for index in range(11):
        source = tmp_path / f"note-{index}.txt"
        source.write_text(f"note-{index}", encoding="utf-8")
        sources.append(source)
    with ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path) as root:
        with root.create_run_dir(32) as run:
            run.write_json("run.json", {"run_id": 32, "job_id": 9, "stage": "finished"})
            first = app.persist_review_annotation(run, sources[0])
            for source in sources[1:10]:
                app.persist_review_annotation(run, source)
            assert app.persist_review_annotation(run, sources[0]) == first
            try:
                app.persist_review_annotation(run, sources[10])
            except app.AnnotationError as exc:
                assert exc.code == "annotation_error"
            else:
                raise AssertionError("eleventh annotation must be rejected")


def test_review_annotation_rejects_invalid_utf8_and_oversize_source(tmp_path):
    invalid = tmp_path / "invalid.txt"
    invalid.write_bytes(b"\xff")
    oversize = tmp_path / "oversize.txt"
    oversize.write_text("x" * 12_001, encoding="utf-8")
    with ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path) as root:
        with root.create_run_dir(33) as run:
            run.write_json("run.json", {"run_id": 33, "job_id": 9, "stage": "finished"})
            for source in (invalid, oversize):
                try:
                    app.persist_review_annotation(run, source)
                except app.AnnotationError as exc:
                    assert exc.code == "annotation_error"
                else:
                    raise AssertionError("invalid annotation source must be rejected")


def test_review_annotation_rejects_corrupt_indexed_duplicate(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("same", encoding="utf-8")
    digest = hashlib.sha256(b"same").hexdigest()
    with ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path) as root:
        with root.create_run_dir(34) as run:
            run.write_json("run.json", {
                "run_id": 34,
                "job_id": 9,
                "stage": "finished",
                "annotations": [{
                    "artifact_ref": "run-34/annotations/missing.txt",
                    "sha256": digest,
                    "chars": 4,
                }],
            })
            try:
                app.persist_review_annotation(run, source)
            except app.AnnotationUnavailable as exc:
                assert exc.code == "annotation_unavailable"
            else:
                raise AssertionError("unverified indexed annotation must be rejected")


def _run_failure_case(monkeypatch, tmp_path, session_cls, *, headed=False, configure=None):
    claims = [ApplicationClaim(
        801,
        {
            "id": 801,
            "canonical_url": "https://boards.greenhouse.io/fixture/jobs/801",
            "title": "Fixture",
            "description": "JOB_DESCRIPTION_SENTINEL",
        },
    )]
    finished = []
    monkeypatch.setattr(app, "PuppeteerSession", session_cls)
    monkeypatch.setattr(app, "claim_next_application_job", lambda conn, owner: claims.pop(0) if claims else None)
    for name in (
        "register_application_artifact",
        "register_application_session",
        "register_application_owner_process",
        "register_application_browser_process",
    ):
        monkeypatch.setattr(app, name, lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "finish_application_run", lambda *args, **kwargs: finished.append(kwargs))
    monkeypatch.setattr(app, "reconcile_open_session_failure", lambda *args, **kwargs: False)
    if configure is not None:
        configure()
    resume = tmp_path / "resume.txt"
    resume.write_text("APPLICANT_SENTINEL", encoding="utf-8")
    root = tmp_path / "artifacts"
    result = asyncio.run(app.run_application_workflow(
        object(),
        resume_file=resume,
        artifact_root=root,
        headed=headed,
    ))
    return result[0], root / "run-801", finished


def test_malformed_observation_response_fails_closed_and_is_durable(monkeypatch, tmp_path):
    class InvalidObservationSession(FakeSession):
        starts = 0

        @classmethod
        def start(cls, **kwargs):
            return cls(kwargs["session_manifest"])

        def observe(self):
            payload = _payload()
            payload["fields"] = [{
                "target_id": "field",
                "field_key": "field",
                "kind": "text",
                "visible": "false",
                "enabled": True,
                "value": None,
                "valid": True,
                "will_validate": True,
            }]
            return payload

    result, run_dir, finished = _run_failure_case(
        monkeypatch,
        tmp_path,
        InvalidObservationSession,
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "browser_error"
    failure = json.loads((run_dir / "browser_failure.json").read_text(encoding="utf-8"))
    assert failure["stage"] == "observation"
    assert failure["operation"] == "observe"
    assert failure["code"] == "protocol_invalid_response"
    assert failure["no_final_submit"] is True
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert manifest["stage"] == "failed"
    assert manifest["no_final_submit"] is True
    assert "prepared" not in manifest["stage"]
    assert finished[-1]["reason_code"] == "browser_error"


@pytest.mark.parametrize(
    ("failure", "stage", "operation"),
    (
        ("start", "startup", "start"),
        ("goto", "navigation", "goto"),
        ("observe", "observation", "observe"),
        ("stability", "observation", "observe"),
        ("prepare_handoff", "handoff", "prepare_handoff"),
        ("commit_handoff", "handoff", "commit_handoff"),
    ),
)
def test_browser_failure_diagnostic_schema_and_privacy(
    monkeypatch,
    tmp_path,
    failure,
    stage,
    operation,
):
    class FailingSession(FakeSession):
        starts = 0
        calls = {"start": 0, "goto": 0, "observe": 0, "prepare_handoff": 0, "commit_handoff": 0}

        @classmethod
        def start(cls, **kwargs):
            cls.calls["start"] += 1
            if failure == "start":
                raise RuntimeError("SECRET_SENTINEL https://private.example/path")
            return super().start(**kwargs)

        def goto(self, url, *, ats_policy=None):
            type(self).calls["goto"] += 1
            if failure == "goto":
                raise RuntimeError("SECRET_SENTINEL /private/path")
            return super().goto(url, ats_policy=ats_policy)

        def observe(self):
            type(self).calls["observe"] += 1
            if failure == "observe" or (failure == "stability" and self.calls["observe"] == 2):
                raise RuntimeError("SECRET_SENTINEL APPLICANT_SENTINEL")
            return super().observe()

        def prepare_handoff(self, **kwargs):
            type(self).calls["prepare_handoff"] += 1
            if failure == "prepare_handoff":
                raise RuntimeError("SECRET_SENTINEL")
            return super().prepare_handoff(**kwargs)

        def commit_handoff(self, token):
            type(self).calls["commit_handoff"] += 1
            if failure == "commit_handoff":
                raise RuntimeError("SECRET_SENTINEL")
            return super().commit_handoff(token)

    result, run_dir, finished = _run_failure_case(
        monkeypatch,
        tmp_path,
        FailingSession,
        headed=failure in {"prepare_handoff", "commit_handoff"},
    )
    assert result["status"] == "failed"
    assert result["reason_code"] == "browser_error"
    payload = json.loads((run_dir / "browser_failure.json").read_text(encoding="utf-8"))
    assert set(payload) == {
        "version",
        "stage",
        "operation",
        "code",
        "iteration",
        "ats_policy",
        "no_final_submit",
        "protocol",
    }
    assert payload["stage"] == stage
    assert payload["operation"] == operation
    assert payload["code"] == "browser_command_failed"
    assert payload["ats_policy"] == "greenhouse"
    assert payload["no_final_submit"] is True
    assert payload["protocol"] == "length-prefixed-json-v1"
    raw = (run_dir / "browser_failure.json").read_text(encoding="utf-8")
    assert all(secret not in raw for secret in (
        "SECRET_SENTINEL",
        "private.example",
        "/private/path",
        "APPLICANT_SENTINEL",
        "JOB_DESCRIPTION_SENTINEL",
    ))
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    indexed = manifest["artifacts"]["browser_failure"]
    assert indexed["path"] == "browser_failure.json"
    assert indexed["sha256"] == hashlib.sha256((run_dir / indexed["path"]).read_bytes()).hexdigest()
    assert indexed["iteration"] == payload["iteration"]
    assert indexed["stage"] == "failed"
    assert finished[-1]["reason_code"] == "browser_error"
    assert finished[-1]["observation_summary"] == {
        "error_code": "browser_error",
        "browser_failure": payload,
    }
    assert FailingSession.calls[operation] == 1 if operation != "observe" else (
        FailingSession.calls["observe"] == (2 if failure == "stability" else 1)
    )


@pytest.mark.parametrize("operation", ("upload", "select", "check", "fill", "click_offline"))
def test_each_mutation_failure_is_diagnostic_and_not_retried(monkeypatch, tmp_path, operation):
    class MutationFailureSession(FakeSession):
        starts = 0
        calls = {name: 0 for name in ("upload", "select", "check", "fill", "click_offline", "observe")}

        def observe(self):
            type(self).calls["observe"] += 1
            payload = _payload()
            if operation == "click_offline":
                payload["buttons"] = [{
                    "target_id": "button",
                    "frame_id": "frame-0",
                    "frame_url": payload["url"],
                    "click_key": "button-key",
                    "element_kind": "button",
                    "button_type": "button",
                    "visible": True,
                    "enabled": True,
                    "safety_descriptors": [],
                }]
            else:
                payload["fields"] = [{
                    "target_id": "field",
                    "field_key": "field",
                    "kind": "file" if operation == "upload" else "text",
                    "visible": True,
                    "enabled": True,
                    "value": None,
                    "valid": True,
                    "will_validate": True,
                }]
            return payload

        def _fail(self, name):
            type(self).calls[name] += 1
            raise RuntimeError("SECRET_SENTINEL https://private.example")

        def upload(self, target_id):
            return self._fail("upload")

        def select(self, target_id, value):
            return self._fail("select")

        def check(self, target_id, value):
            return self._fail("check")

        def fill(self, target_id, value):
            return self._fail("fill")

        def click_offline(self, target_id, continuation=False):
            assert continuation is False
            return self._fail("click_offline")

    answer = app.FieldAnswer("field", "Ada", 1.0, "configured", "profile")
    plan = app.AutofillPlan(
        answers=() if operation in {"upload", "click_offline"} else (answer,),
        safe_click_target_id="button" if operation == "click_offline" else None,
        status="ready",
        reason_code=app.PublicReasonCode.draft_ready,
    )
    def configure():
        monkeypatch.setattr(app, "_configured_and_profile_plan", lambda *args, **kwargs: plan)
        monkeypatch.setattr(app, "resolve_with_llm", lambda *args, **kwargs: app.AutofillPlan())
        monkeypatch.setattr(
            app,
            "plan_action_evidence",
            lambda *args, **kwargs: (
                [{"target_id": "button" if operation == "click_offline" else "field", "action": "click" if operation == "click_offline" else operation}],
                [],
            ),
        )
    result, run_dir, _ = _run_failure_case(monkeypatch, tmp_path, MutationFailureSession, configure=configure)
    assert result["status"] == "failed"
    assert result["reason_code"] == "browser_error"
    payload = json.loads((run_dir / "browser_failure.json").read_text())
    assert payload["stage"] == "mutation"
    assert payload["operation"] == operation
    assert payload["iteration"] == 1
    assert payload["code"] == "browser_command_failed"
    evidence_path = run_dir / "iterations" / "0001" / "action_evidence.json"
    evidence = json.loads(evidence_path.read_text())
    assert evidence["observation_id"] == "obs-1"
    observation_path = run_dir / "iterations" / "0001" / "observation.json"
    assert observation_path.exists()
    snapshot = json.loads(observation_path.read_text())
    assert snapshot["observation_id"] == evidence["observation_id"]
    observation_sha256 = hashlib.sha256(observation_path.read_bytes()).hexdigest()
    assert evidence["observation_artifact"] == "iterations/0001/observation.json"
    assert evidence["observation_sha256"] == observation_sha256
    manifest = json.loads((run_dir / "run.json").read_text())
    indexed = manifest["iterations"]["1"]["artifacts"]
    assert indexed["observation"]["sha256"] == observation_sha256
    assert indexed["action_evidence"]["sha256"] == hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    assert MutationFailureSession.calls[operation] == 1
    assert "SECRET_SENTINEL" not in json.dumps(payload)


def test_goto_type_error_is_not_retried(monkeypatch, tmp_path):
    class TypeErrorSession(FakeSession):
        starts = 0
        calls = 0

        def goto(self, url, *, ats_policy=None):
            type(self).calls += 1
            raise TypeError("SECRET_SENTINEL")

    result, run_dir, _ = _run_failure_case(monkeypatch, tmp_path, TypeErrorSession)
    assert result["reason_code"] == "browser_error"
    assert TypeErrorSession.calls == 1
    payload = json.loads((run_dir / "browser_failure.json").read_text())
    assert payload["stage"] == "navigation"
    assert payload["operation"] == "goto"
    assert payload["code"] == "browser_command_failed"
    assert "SECRET_SENTINEL" not in json.dumps(payload)


def test_cleanup_failure_indexes_private_evidence_and_next_claim_runs(monkeypatch, tmp_path):
    claims = [
        ApplicationClaim(811, {"id": 811, "canonical_url": "https://boards.greenhouse.io/a/jobs/811", "title": "A"}),
        ApplicationClaim(812, {"id": 812, "canonical_url": "https://boards.greenhouse.io/b/jobs/812", "title": "B"}),
    ]
    finished = []
    closed_runs = []

    class CloseFailureSession(FakeSession):
        starts = 0
        def __init__(self, manifest):
            super().__init__(manifest)
            self.run_id = int(self.manifest.parent.name.removeprefix("run-"))
        @classmethod
        def start(cls, **kwargs):
            cls.starts += 1
            return cls(kwargs["session_manifest"])

        def goto(self, url, *, ats_policy=None):
            if self.run_id == 811:
                raise RuntimeError("ORIGINAL_SECRET")
            return super().goto(url, ats_policy=ats_policy)

        def close(self):
            type(self).closes += 1
            if self.run_id == 811:
                raise RuntimeError("CLEANUP_SECRET /private/path")

    monkeypatch.setattr(app, "PuppeteerSession", CloseFailureSession)
    monkeypatch.setattr(app, "claim_next_application_job", lambda conn, owner: claims.pop(0) if claims else None)
    for name in (
        "register_application_artifact",
        "register_application_session",
        "register_application_owner_process",
        "register_application_browser_process",
    ):
        monkeypatch.setattr(app, name, lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "finish_application_run", lambda *args, **kwargs: finished.append(kwargs))
    monkeypatch.setattr(app, "reconcile_open_session_failure", lambda *args, **kwargs: False)
    original_close = app.ArtifactRun.close
    def tracking_close(run):
        closed_runs.append(run.public_ref)
        return original_close(run)
    monkeypatch.setattr(app.ArtifactRun, "close", tracking_close)
    resume = tmp_path / "resume.txt"
    resume.write_text("resume", encoding="utf-8")
    root = tmp_path / "artifacts"
    results = asyncio.run(app.run_application_workflow(object(), limit=2, resume_file=resume, artifact_root=root))
    assert [result["run_id"] for result in results] == [811, 812]
    assert results[0]["reason_code"] == "browser_error"
    assert results[0]["window_state"] == "unknown"
    assert results[1]["reason_code"] == "draft_ready"
    assert set(closed_runs) == {"run-811", "run-812"}
    cleanup = json.loads((root / "run-811" / "browser_cleanup_failure.json").read_text())
    assert set(cleanup) == {
        "version",
        "stage",
        "operation",
        "code",
        "iteration",
        "ats_policy",
        "no_final_submit",
        "protocol",
    }
    assert cleanup["stage"] == "cleanup"
    assert cleanup["operation"] == "close"
    assert cleanup["code"] == "browser_command_failed"
    assert "CLEANUP_SECRET" not in json.dumps(cleanup)
    assert "/private/path" not in json.dumps(cleanup)
    manifest = json.loads((root / "run-811" / "run.json").read_text())
    assert manifest["artifacts"]["browser_failure"]["path"] == "browser_failure.json"
    assert manifest["artifacts"]["browser_cleanup_failure"]["path"] == "browser_cleanup_failure.json"



@pytest.mark.parametrize("write_failure", ("browser_failure", "run"))
def test_browser_failure_write_failure_fails_closed_without_unverified_index(
    monkeypatch,
    tmp_path,
    write_failure,
):
    class NavigationFailureSession(FakeSession):
        starts = 0

        @classmethod
        def start(cls, **kwargs):
            cls.starts += 1
            return cls(kwargs["session_manifest"])

        def goto(self, url, *, ats_policy=None):
            raise RuntimeError("ORIGINAL_PRIVATE_BROWSER_CODE /private/path")

    def configure_write_failure():
        if write_failure == "browser_failure":
            original_write = app._write_json_verified

            def fail_browser_failure(run, relative_path, value):
                if relative_path == "browser_failure.json":
                    raise RuntimeError("WRITE_PRIVATE_SENTINEL")
                return original_write(run, relative_path, value)

            monkeypatch.setattr(app, "_write_json_verified", fail_browser_failure)
        else:
            original_manifest = app._write_run_manifest

            def fail_failed_manifest(run, payload):
                if payload.get("stage") == "failed":
                    raise RuntimeError("RUN_WRITE_PRIVATE_SENTINEL")
                return original_manifest(run, payload)

            monkeypatch.setattr(app, "_write_run_manifest", fail_failed_manifest)

    result, run_dir, finished = _run_failure_case(
        monkeypatch,
        tmp_path,
        NavigationFailureSession,
        configure=configure_write_failure,
    )
    assert result["status"] == "failed"
    assert result["reason_code"] == "browser_error"
    assert finished[-1]["reason_code"] == "browser_error"
    summary = finished[-1]["observation_summary"]
    assert summary["error_code"] == "browser_error"
    assert summary["browser_failure"]["code"] == "browser_command_failed"
    assert "ORIGINAL_PRIVATE_BROWSER_CODE" not in json.dumps(summary)
    assert "/private/path" not in json.dumps(summary)
    run_manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert "browser_failure" not in run_manifest.get("artifacts", {})
    if write_failure == "browser_failure":
        assert not (run_dir / "browser_failure.json").exists()
    else:
        assert (run_dir / "browser_failure.json").exists()
        payload = json.loads((run_dir / "browser_failure.json").read_text(encoding="utf-8"))
        assert payload["code"] == "browser_command_failed"
