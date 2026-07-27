from __future__ import annotations

import json
from pathlib import Path

import resume_generation.advisor as advisor
from resume_generation.advisor import ResumeAdvice, build_resume_advisory_request, request_resume_advice
from resume_generation.generator import ResumeJob, load_resume_profile, optimize_resume


class _Response:
    content = b'{"message":{"content":"{\\"version\\":1,\\"ranked_claim_ids\\":[],\\"ranked_coursework\\":[]}"}}'

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return json.loads(self.content)


class _Client:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.request = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, endpoint, *, headers, content):
        self.request = (endpoint, headers, json.loads(content))
        return _Response()


def _profile():
    repository = Path(__file__).resolve().parents[2]
    return load_resume_profile(repository / "private" / "resume" / "profile.json")


def _job() -> ResumeJob:
    return ResumeJob(
        id=999,
        title="Software Engineer",
        company="Example",
        description="Requirements:\n- semantic retrieval systems",
        location="Remote",
        posted_at="2026-01-01",
    )


def test_advisory_is_disabled_without_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("RESUME_ADVISORY_ENABLED", raising=False)
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "do-not-use")
    called = False

    def fail_client(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("disabled advisory must not call Ollama")

    monkeypatch.setattr(advisor.httpx, "Client", fail_client)
    assert request_resume_advice(_profile(), _job()) is None
    assert called is False


def test_advisory_accepts_only_existing_claim_ids(monkeypatch):
    profile = _profile()
    rows, allowed, coursework = advisor._claim_rows(profile)
    first_id = next(iter(allowed))
    payload = {"version": 1, "ranked_claim_ids": [first_id], "ranked_coursework": list(coursework[:1])}

    class ValidResponse(_Response):
        content = json.dumps({"message": {"content": json.dumps(payload)}}).encode()

    class ValidClient(_Client):
        def post(self, endpoint, *, headers, content):
            self.request = (endpoint, headers, json.loads(content))
            return ValidResponse()

    monkeypatch.setenv("RESUME_ADVISORY_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "test-token")
    monkeypatch.setattr(advisor.httpx, "Client", ValidClient)
    result = request_resume_advice(profile, _job())
    assert result == ResumeAdvice((first_id,), tuple(coursework[:1]))
    request = build_resume_advisory_request(profile, _job())
    assert request["job"]["title"] == "Software Engineer"
    assert "email" not in json.dumps(request).casefold()


def test_advisory_unknown_ids_fail_closed(monkeypatch):
    class InvalidResponse(_Response):
        content = json.dumps({
            "message": {
                "content": json.dumps({"version": 1, "ranked_claim_ids": ["invented"], "ranked_coursework": []})
            }
        }).encode()

    class InvalidClient(_Client):
        def post(self, endpoint, *, headers, content):
            return InvalidResponse()

    monkeypatch.setenv("RESUME_ADVISORY_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "test-token")
    monkeypatch.setattr(advisor.httpx, "Client", InvalidClient)
    assert request_resume_advice(_profile(), _job()) is None


def test_valid_advice_can_surface_source_backed_semantic_claim(monkeypatch):
    profile = _profile()
    project = next(project for project in profile.projects if project.enabled)
    job = ResumeJob(
        id=1000,
        title="Specialist",
        company="Example",
        description="Requirements:\n- never-before-seen-term",
    )
    deterministic = optimize_resume(profile, job)
    advised = optimize_resume(profile, job, advice=ResumeAdvice((project.id,), ()))
    assert deterministic.selection is not None
    assert advised.selection is not None
    assert project.id not in {entry_id for entry_id, _ in deterministic.selection.projects}
    assert project.id in {entry_id for entry_id, _ in advised.selection.projects}
