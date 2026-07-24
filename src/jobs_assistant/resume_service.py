"""Private orchestration service connecting backlog snapshots, applicant claims, grounded generation, and deterministic PDF artifacts."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping

from .artifacts import ArtifactRoot
from .ats import (
    ApplicationProfile,
    LoadedApplicationProfile,
    ResumeContext,
    load_applicant_description,
    load_application_profile_snapshot,
    load_resume_context,
    parse_application_profile,
)
from .db import (
    create_generated_resume,
    get_generated_resume,
    get_generated_resume_private,
    get_job_resume_snapshot,
    get_ready_generated_resume,
    list_generated_resumes,
    read_resume_job,
    transition_generated_resume_state,
)
from .resume import (
    JobResumeSnapshot,
    ResumeValidationError,
    canonical_json,
    compute_sha256,
    extract_candidate_claims,
    generate_grounded_tailored_resume,
)
from .resume_artifacts import (
    RESUME_FAILURE_REASON_CODES,
    ResumeArtifactSecurityError,
    ResumeArtifactStore,
    ResumeRenderError,
    render_resume_pdf,
)

_MAX_READY_PDF_BYTES = 10 * 1024 * 1024
_MAX_READY_JSON_BYTES = 2 * 1024 * 1024
_READY_REPLAY_FAILURE_REASON = "STALE_OR_CORRUPT_ARTIFACT"
_READY_ARTIFACT_FILENAMES = (
    "job_snapshot.json",
    "candidate_claims.json",
    "generation_request.json",
    "generation_response.json",
    "validation.json",
    "scoring.json",
    "resume.json",
    "resume.pdf",
)

__all__ = (
    "generate_resume",
    "resolve_generated_resume",
    "show_generated_resume",
    "list_public_generated_resumes",
)


def _ensure_no_active_transaction(connection: Any) -> None:
    if getattr(connection, "in_transaction", False):
        try:
            connection.commit()
        except Exception:
            pass


def _reason_code_to_failure_code(exc: Exception) -> str:
    if isinstance(exc, ResumeValidationError):
        code_val = str(getattr(exc, "code", ""))
        if code_val.startswith("ResumeReasonCode."):
            code_val = code_val.split(".", 1)[1]
        upper_code = code_val.upper()
        if upper_code in RESUME_FAILURE_REASON_CODES:
            return upper_code

    if isinstance(exc, ResumeRenderError):
        return "RENDERER_FAILURE"

    if isinstance(exc, ResumeArtifactSecurityError):
        return "ARTIFACT_WRITE_FAILED"

    return "RENDERER_FAILURE"

def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("artifact JSON is not canonical") from exc


def _reject_duplicate_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("artifact JSON contains duplicate keys")
        result[key] = value
    return result


def _read_json_document(payload: bytes, *, name: str) -> dict[str, Any]:
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{name} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{name} must contain an object")
    if _canonical_json_bytes(decoded) != payload:
        raise ValueError(f"{name} is not canonical JSON")
    return decoded


def _is_sha256(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and value == value.lower()
        and all(char in "0123456789abcdef" for char in value)
    )


def _verify_private_directory(fd: int, *, label: str) -> os.stat_result:
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode):
        raise ResumeArtifactSecurityError(f"{label} is not a directory")
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise ResumeArtifactSecurityError(f"{label} must be owner-private")
    return st


def _verify_opened_identity(parent_fd: int, name: str, fd: int) -> None:
    by_fd = os.fstat(fd)
    by_name = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if by_fd.st_dev != by_name.st_dev or by_fd.st_ino != by_name.st_ino:
        raise ResumeArtifactSecurityError("artifact changed while being opened")


def _read_fd_snapshot(fd: int, size: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(fd, min(remaining, 1024 * 1024))
        if not chunk:
            raise ValueError("artifact changed while being read")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(fd, 1):
        raise ValueError("artifact changed while being read")
    os.lseek(fd, 0, os.SEEK_SET)
    return b"".join(chunks)


def _read_private_file(run_fd: int, name: str, *, max_bytes: int) -> bytes:
    if Path(name).name != name or name in {".", ".."}:
        raise ResumeArtifactSecurityError("invalid artifact filename")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, dir_fd=run_fd)
    except FileNotFoundError as exc:
        raise ValueError(f"Missing private artifact {name}") from exc
    except OSError as exc:
        if getattr(exc, "errno", None) in {getattr(os, "ELOOP", 62), 40}:
            raise ResumeArtifactSecurityError(
                "Symlinks forbidden for private PDF artifact"
                if name == "resume.pdf"
                else "Symlinks forbidden for private provenance artifact"
            ) from exc
        raise ResumeArtifactSecurityError(f"Cannot open private artifact {name}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ResumeArtifactSecurityError(f"Private artifact {name} is not a regular file")
        if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o600:
            raise ResumeArtifactSecurityError(f"Private artifact {name} must be owner-private")
        if st.st_size > max_bytes:
            raise ValueError(f"Private artifact {name} exceeds its size cap")
        payload = _read_fd_snapshot(fd, st.st_size)
        _verify_opened_identity(run_fd, name, fd)
        return payload
    finally:
        os.close(fd)


def _secure_ready_artifact_files(
    record: Mapping[str, Any],
    artifact_root: ArtifactRoot,
) -> dict[str, Any]:
    """Read one ready run through descriptor-relative, no-follow handles."""
    if not isinstance(artifact_root, ArtifactRoot):
        raise TypeError("artifact_root must be an ArtifactRoot instance")
    if record.get("state") != "ready":
        raise ValueError(f"Resume state '{record.get('state')}' is not ready")

    pdf_path_value = record.get("private_pdf_path")
    run_name_value = record.get("artifact_dir")
    if not isinstance(pdf_path_value, str) or not pdf_path_value:
        raise ValueError("Missing private_pdf_path in generated resume record")
    if not isinstance(run_name_value, str) or not run_name_value:
        raise ResumeArtifactSecurityError("Missing artifact run directory")
    run_name = run_name_value
    run_parts = Path(run_name).parts
    if len(run_parts) != 1 or run_parts[0] != run_name or run_name in {".", ".."}:
        raise ResumeArtifactSecurityError("Invalid artifact run directory")

    # Keep a lexical pathlib confinement check in addition to descriptor
    # confinement.  ``str.startswith`` is deliberately insufficient here:
    # ``/root/artifacts-other`` is not inside ``/root/artifacts``.
    root_path = artifact_root._path.absolute()
    pdf_path = Path(pdf_path_value).absolute()
    try:
        if not pdf_path.is_relative_to(root_path):
            raise ResumeArtifactSecurityError("PDF path escapes artifact root confinement")
        relative_pdf = pdf_path.relative_to(root_path)
    except ValueError as exc:
        raise ResumeArtifactSecurityError("PDF path escapes artifact root confinement") from exc
    if relative_pdf.parts != (run_name, "resume.pdf"):
        raise ResumeArtifactSecurityError("PDF path does not match its artifact run")

    root_fd_value = getattr(artifact_root, "_fd", None)
    if type(root_fd_value) is not int or root_fd_value < 0:
        raise ResumeArtifactSecurityError("Artifact root is closed")
    root_fd = os.dup(root_fd_value)
    run_fd: int | None = None
    try:
        _verify_private_directory(root_fd, label="Artifact parent")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            run_fd = os.open(run_name, flags, dir_fd=root_fd)
        except OSError as exc:
            if getattr(exc, "errno", None) in {getattr(os, "ELOOP", 62), 40}:
                raise ResumeArtifactSecurityError("Symlinks forbidden for artifact run") from exc
            raise ResumeArtifactSecurityError("Artifact run is unavailable") from exc
        _verify_opened_identity(root_fd, run_name, run_fd)
        _verify_private_directory(run_fd, label="Artifact run")
        files: dict[str, bytes] = {}
        files["manifest.json"] = _read_private_file(
            run_fd,
            "manifest.json",
            max_bytes=_MAX_READY_JSON_BYTES,
        )
        manifest = _read_json_document(files["manifest.json"], name="manifest.json")
        expected_manifest_keys = {
            "schema_version",
            "resume_id",
            "provenance",
            "content_sha256",
            "pdf_sha256",
            "page_count",
            "extracted_text_sha256",
            "artifacts",
        }
        if set(manifest) != expected_manifest_keys or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
            raise ValueError("manifest.json schema mismatch")
        if manifest.get("resume_id") != str(record.get("resume_id")):
            raise ValueError("manifest resume_id mismatch")
        if manifest.get("content_sha256") != record.get("content_sha256"):
            raise ValueError("Manifest content SHA256 mismatch with DB")
        if manifest.get("pdf_sha256") != record.get("pdf_sha256"):
            raise ValueError("Manifest PDF SHA256 mismatch with DB")
        provenance = manifest.get("provenance")
        expected_provenance = {
            "job_snapshot_sha256": record.get("job_snapshot_sha256"),
            "profile_sha256": record.get("profile_sha256"),
            "source_resume_sha256": record.get("source_resume_sha256"),
            "generation_config_sha256": record.get("generation_config_sha256"),
        }
        if (
            not isinstance(provenance, dict)
            or set(provenance) != set(expected_provenance)
            or provenance != expected_provenance
            or any(not _is_sha256(value) for value in provenance.values())
        ):
            raise ValueError("Manifest provenance mismatch with DB")
        artifacts = manifest.get("artifacts")
        expected_names = set(_READY_ARTIFACT_FILENAMES)
        if not isinstance(artifacts, dict) or set(artifacts) != expected_names:
            raise ValueError("Manifest artifact list mismatch")
        for name in _READY_ARTIFACT_FILENAMES:
            metadata = artifacts.get(name)
            if (
                not isinstance(metadata, dict)
                or set(metadata) != {"bytes", "sha256"}
                or type(metadata.get("bytes")) is not int
                or metadata["bytes"] < 1
                or not _is_sha256(metadata.get("sha256"))
            ):
                raise ValueError(f"Manifest metadata mismatch for {name}")
            max_bytes = _MAX_READY_PDF_BYTES if name == "resume.pdf" else _MAX_READY_JSON_BYTES
            if metadata["bytes"] > max_bytes:
                raise ValueError(f"Manifest size cap exceeded for {name}")
            files[name] = _read_private_file(run_fd, name, max_bytes=max_bytes)
        snapshot_doc = _read_json_document(files["job_snapshot.json"], name="job_snapshot.json")
        expected_snapshot_keys = {
            "job_id",
            "title",
            "company",
            "description",
            "canonical_application_url",
            "location",
            "source_identifier",
            "requirements",
            "job_snapshot_sha256",
            "description_override",
        }
        if set(snapshot_doc) != expected_snapshot_keys:
            raise ValueError("job_snapshot.json schema mismatch")
        override = snapshot_doc.get("description_override")
        if override is not None and (type(override) is not str or not override.strip()):
            raise ValueError("job_snapshot.json description override is invalid")
        if snapshot_doc.get("job_snapshot_sha256") != record.get("job_snapshot_sha256"):
            raise ValueError("Persisted job snapshot hash mismatch with DB")
        if type(snapshot_doc.get("job_id")) is not int:
            raise ValueError("job_snapshot.json job_id is invalid")
        if type(snapshot_doc.get("requirements")) is not list:
            raise ValueError("job_snapshot.json requirements are invalid")
        return {
            "files": files,
            "manifest": manifest,
            "snapshot": snapshot_doc,
        }
    finally:
        if run_fd is not None:
            os.close(run_fd)
        os.close(root_fd)


def _verify_ready_file_hashes(
    record: Mapping[str, Any],
    artifact: Mapping[str, Any],
) -> None:
    files = artifact["files"]
    manifest = artifact["manifest"]
    metadata = manifest["artifacts"]
    actual_pdf_sha256 = compute_sha256(files["resume.pdf"])
    if actual_pdf_sha256 != record.get("pdf_sha256") or actual_pdf_sha256 != manifest.get("pdf_sha256"):
        raise ValueError("PDF content SHA256 digest mismatch")
    actual_content_sha256 = compute_sha256(files["resume.json"])
    if actual_content_sha256 != record.get("content_sha256") or actual_content_sha256 != manifest.get("content_sha256"):
        raise ValueError("Resume content SHA256 digest mismatch")
    for name in _READY_ARTIFACT_FILENAMES:
        if name in {"resume.pdf", "resume.json"}:
            continue
        payload = files[name]
        entry = metadata[name]
        if len(payload) != entry["bytes"] or compute_sha256(payload) != entry["sha256"]:
            raise ValueError(f"Persisted {name} hash mismatch")

def _description_override_from_artifact(
    record: Mapping[str, Any],
    artifact: Mapping[str, Any],
) -> str | None:
    """Validate the persisted request and return only an explicit override."""
    request_doc = _read_json_document(
        artifact["files"]["generation_request.json"],
        name="generation_request.json",
    )
    if "description_override_used" not in request_doc:
        raise ValueError("generation_request.json missing description_override_used")
    override_used = request_doc["description_override_used"]
    if type(override_used) is not bool:
        raise ValueError("generation_request.json description_override_used must be boolean")
    expected_keys = {
        "job_id",
        "job_snapshot_sha256",
        "profile_sha256",
        "source_resume_sha256",
        "generation_config_sha256",
        "description_override_used",
        "config",
    }
    if set(request_doc) != expected_keys:
        raise ValueError("generation_request.json schema mismatch")
    if type(request_doc["job_id"]) is not int or request_doc["job_id"] != int(record["job_id"]):
        raise ValueError("generation_request.json job_id mismatch")
    for key in (
        "job_snapshot_sha256",
        "profile_sha256",
        "source_resume_sha256",
        "generation_config_sha256",
    ):
        value = request_doc[key]
        if not _is_sha256(value) or value != record.get(key):
            raise ValueError(f"generation_request.json {key} mismatch")
    config = request_doc["config"]
    if type(config) is not dict:
        raise ValueError("generation_request.json config must be an object")
    if compute_sha256(canonical_json(config)) != request_doc["generation_config_sha256"]:
        raise ValueError("generation_request.json config hash mismatch")

    snapshot_doc = artifact["snapshot"]
    snapshot_override = snapshot_doc.get("description_override")
    if override_used:
        if type(snapshot_override) is not str or not snapshot_override.strip():
            raise ValueError("generation_request.json override provenance mismatch")
        description = snapshot_doc.get("description")
        if type(description) is not str or not description.strip():
            raise ValueError("job_snapshot.json description is unusable")
        return description
    if snapshot_override is not None:
        raise ValueError("generation_request.json override provenance mismatch")
    return None


def _current_snapshot_for_artifact(
    connection: Any,
    record: Mapping[str, Any],
    artifact: Mapping[str, Any],
) -> JobResumeSnapshot:
    snapshot_doc = artifact["snapshot"]
    try:
        current_snapshot = get_job_resume_snapshot(
            connection,
            record["job_id"],
            description_override=snapshot_doc["description_override"],
        )
    except Exception as exc:
        raise ValueError(f"{_READY_REPLAY_FAILURE_REASON}: persisted job snapshot is unusable") from exc
    if current_snapshot is None:
        raise ValueError(f"{_READY_REPLAY_FAILURE_REASON}: job no longer exists")
    expected_values = {
        "job_id": current_snapshot.job_id,
        "title": current_snapshot.title,
        "company": current_snapshot.company,
        "description": current_snapshot.description,
        "canonical_application_url": current_snapshot.canonical_application_url,
        "location": current_snapshot.location,
        "source_identifier": current_snapshot.source_identifier,
        "requirements": list(current_snapshot.requirements),
        "job_snapshot_sha256": current_snapshot.job_snapshot_sha256,
    }
    for key, expected in expected_values.items():
        if artifact["snapshot"].get(key) != expected:
            raise ValueError(f"{_READY_REPLAY_FAILURE_REASON}: Stale job snapshot metadata")
    if current_snapshot.job_snapshot_sha256 != record.get("job_snapshot_sha256"):
        raise ValueError(f"{_READY_REPLAY_FAILURE_REASON}: Stale job snapshot hash")
    return current_snapshot


def _verify_ready_artifact_on_disk(
    record: dict[str, Any],
    artifact_root: ArtifactRoot,
    snapshot: JobResumeSnapshot | None = None,
) -> dict[str, Any]:
    """Verify private run confinement, provenance, snapshot, and content hashes."""
    artifact = _secure_ready_artifact_files(record, artifact_root)
    if snapshot is not None:
        if int(record["job_id"]) != int(snapshot.job_id):
            raise ValueError(f"Job ID mismatch: record has {record['job_id']}, snapshot has {snapshot.job_id}")
        if record["job_snapshot_sha256"] != snapshot.job_snapshot_sha256:
            raise ValueError("Stale job snapshot hash")
    _verify_ready_file_hashes(record, artifact)
    return artifact


def generate_resume(
    connection: Any,
    job_id: int | str | None = None,
    profile_json: str | Path | Mapping[str, Any] | LoadedApplicationProfile | ApplicationProfile | None = None,
    source_resume: str | Path | ResumeContext | None = None,
    artifact_root: ArtifactRoot | None = None,
    description_file: str | Path | None = None,
    *,
    next_queued: bool = False,
    description_override: str | None = None,
    force: bool = False,
    config: dict[str, Any] | None = None,
    public_shaping: bool = True,
    raw_object: bool = False,
) -> Any:
    """Generate a grounded tailored resume artifact without mutating job state or claiming."""
    if not isinstance(artifact_root, ArtifactRoot):
        raise TypeError("artifact_root must be a valid ArtifactRoot instance")

    if job_id == "next":
        next_queued = True
        job_id = None
    if job_id is not None and next_queued:
        raise ValueError("Mutually exclusive job selection: specify job_id or next_queued, not both")
    if job_id is None and not next_queued:
        raise ValueError("Must specify job_id or next_queued=True")

    effective_description_override = description_override
    if description_file is not None:
        loaded_desc = load_applicant_description(description_file)
        if effective_description_override is None:
            effective_description_override = loaded_desc

    # Production generation requires explicit, byte-provenanced inputs.  The
    # object forms remain useful to deterministic tests, but are normalized to
    # the same ``explicit_json`` provenance contract as a file snapshot.
    if profile_json is None:
        raise ValueError("An explicit application profile is required")
    if isinstance(profile_json, LoadedApplicationProfile):
        loaded_profile = profile_json
        if loaded_profile.source_kind != "explicit_json" or not _is_sha256(loaded_profile.source_sha256):
            raise ValueError("Application profile must have explicit JSON provenance")
    elif isinstance(profile_json, ApplicationProfile):
        profile_payload: dict[str, Any] = dict(profile_json.facts)
        if profile_json.description:
            profile_payload["resume_summary"] = profile_json.description
        if profile_json.field_answers:
            profile_payload["field_answers"] = [
                {
                    "ats": answer.ats,
                    "name": answer.name,
                    "label": answer.label,
                    "kind": answer.kind,
                    "value": answer.value,
                }
                for answer in profile_json.field_answers
            ]
        loaded_profile = LoadedApplicationProfile(
            profile=profile_json,
            source_kind="explicit_json",
            source_sha256=compute_sha256(canonical_json(profile_payload)),
        )
    elif isinstance(profile_json, Mapping):
        parsed_profile = parse_application_profile(profile_json)
        loaded_profile = LoadedApplicationProfile(
            profile=parsed_profile,
            source_kind="explicit_json",
            source_sha256=compute_sha256(canonical_json(dict(profile_json))),
        )
    elif isinstance(profile_json, (str, Path)):
        loaded_profile = load_application_profile_snapshot(profile_json)
        if loaded_profile.source_kind != "explicit_json" or not _is_sha256(loaded_profile.source_sha256):
            raise ValueError("Application profile must have explicit JSON provenance")
    else:
        raise TypeError("profile_json must be an explicit JSON path or canonical profile")

    owns_resume_context = False
    resume_ctx: ResumeContext | None
    if isinstance(source_resume, ResumeContext):
        resume_ctx = source_resume
    elif isinstance(source_resume, (str, Path)):
        resume_ctx = load_resume_context(source_resume)
        owns_resume_context = True
    elif source_resume is None:
        raise ValueError("An explicit source resume is required")
    else:
        raise TypeError("source_resume must be a Path, string, or ResumeContext")
    if not _is_sha256(resume_ctx.sha256):
        if owns_resume_context:
            resume_ctx.close()
        raise ValueError("Source resume must have canonical provenance")

    store: ResumeArtifactStore | None = None
    try:
        profile_sha256 = str(loaded_profile.source_sha256)
        source_resume_sha256 = str(resume_ctx.sha256)
        generation_config_sha256 = compute_sha256(canonical_json(config or {}))

        snapshot_error: Exception | None = None
        try:
            snapshot = read_resume_job(
                connection,
                job_id=job_id,
                next_queued=next_queued,
                description_override=effective_description_override,
            )
        except ValueError as exc:
            # An override-derived artifact may be replayed even when the
            # current row has a blank description; its immutable override is
            # loaded from the persisted job snapshot below.
            if effective_description_override is not None:
                raise
            snapshot = None
            snapshot_error = exc

        if not force:
            existing_ready: dict[str, Any] | None = None
            if snapshot is not None:
                # A usable current snapshot is authoritative.  Replays must
                # match its complete immutable hash; an older ready artifact
                # must never be reused merely because the other inputs match.
                existing_ready = get_ready_generated_resume(
                    connection,
                    job_id=snapshot.job_id,
                    job_snapshot_sha256=snapshot.job_snapshot_sha256,
                    profile_sha256=profile_sha256,
                    source_resume_sha256=source_resume_sha256,
                    generation_config_sha256=generation_config_sha256,
                    public_shaping=False,
                    raw_object=False,
                )
            elif (
                job_id is not None
                and snapshot_error is not None
                and str(snapshot_error) == "Job description cannot be blank or unusable"
            ):
                # A blank database description cannot produce a current
                # snapshot.  In that one case only, look up a ready artifact
                # to recover its persisted explicit description override.
                existing_ready = get_ready_generated_resume(
                    connection,
                    job_id=job_id,
                    profile_sha256=profile_sha256,
                    source_resume_sha256=source_resume_sha256,
                    generation_config_sha256=generation_config_sha256,
                    public_shaping=False,
                    raw_object=False,
                )
            if existing_ready is not None:
                try:
                    persisted_artifact = _secure_ready_artifact_files(existing_ready, artifact_root)
                    persisted_override = persisted_artifact["snapshot"]["description_override"]
                    if snapshot is None and (
                        type(persisted_override) is not str or not persisted_override.strip()
                    ):
                        raise ValueError("Persisted ready artifact has no explicit description override")
                    _current_snapshot_for_artifact(connection, existing_ready, persisted_artifact)
                    _verify_ready_file_hashes(existing_ready, persisted_artifact)
                    _description_override_from_artifact(existing_ready, persisted_artifact)
                    return get_generated_resume(
                        connection,
                        existing_ready["resume_id"],
                        public_shaping=public_shaping,
                        raw_object=raw_object,
                    )
                except Exception as exc:
                    raise ValueError(
                        f"{_READY_REPLAY_FAILURE_REASON}: ready artifact verification failed"
                    ) from exc

        if snapshot is None:
            if snapshot_error is not None:
                raise snapshot_error
            raise ValueError(f"Job snapshot not found (job_id={job_id}, next_queued={next_queued})")
        job_snapshot_sha256 = snapshot.job_snapshot_sha256

        _ensure_no_active_transaction(connection)
        pending_record = create_generated_resume(
            connection,
            job_id=snapshot.job_id,
            job_snapshot_sha256=job_snapshot_sha256,
            profile_sha256=profile_sha256,
            source_resume_sha256=source_resume_sha256,
            generation_config_sha256=generation_config_sha256,
            state="pending",
            force=force,
            public_shaping=False,
            raw_object=False,
        )
        resume_id = pending_record["resume_id"]
        store = ResumeArtifactStore(artifact_root._path)
        try:
            _ensure_no_active_transaction(connection)
            transition_generated_resume_state(
                connection,
                resume_id=resume_id,
                from_state="pending",
                to_state="generating",
            )
            claims = extract_candidate_claims(loaded_profile, resume_ctx)
            _ensure_no_active_transaction(connection)
            transition_generated_resume_state(
                connection,
                resume_id=resume_id,
                from_state="generating",
                to_state="validating",
            )
            validated_resume, score_data = generate_grounded_tailored_resume(
                job_snapshot=snapshot,
                profile=loaded_profile,
                resume_context=resume_ctx,
            )
            if type(validated_resume.get("schema_version")) is not int or validated_resume["schema_version"] != 1:
                raise ResumeValidationError("schema_version must be integer 1")

            _ensure_no_active_transaction(connection)
            transition_generated_resume_state(
                connection,
                resume_id=resume_id,
                from_state="validating",
                to_state="rendering",
            )
            claims_data = [
                {
                    "claim_id": c.claim_id,
                    "category": c.category,
                    "text": c.text,
                    "source": c.source,
                    "source_sha256": c.source_sha256,
                    "sensitive": c.sensitive,
                    "metadata": dict(c.metadata),
                    "claim_sha256": c.claim_sha256,
                }
                for c in claims
            ]
            snapshot_data = {
                "job_id": snapshot.job_id,
                "title": snapshot.title,
                "company": snapshot.company,
                "description": snapshot.description,
                "canonical_application_url": snapshot.canonical_application_url,
                "location": snapshot.location,
                "source_identifier": snapshot.source_identifier,
                "requirements": list(snapshot.requirements),
                "job_snapshot_sha256": snapshot.job_snapshot_sha256,
                "description_override": effective_description_override,
            }
            generation_request_data = {
                "job_id": snapshot.job_id,
                "job_snapshot_sha256": job_snapshot_sha256,
                "profile_sha256": profile_sha256,
                "source_resume_sha256": source_resume_sha256,
                "generation_config_sha256": generation_config_sha256,
                "description_override_used": effective_description_override is not None,
                "config": config or {},
            }
            documents = {
                "job_snapshot.json": snapshot_data,
                "candidate_claims.json": claims_data,
                "generation_request.json": generation_request_data,
                "generation_response.json": {"validated_resume": validated_resume},
                "validation.json": {
                    "status": "validated",
                    "validation_decisions": list(score_data.validation_decisions),
                    "matched_requirements": list(score_data.matched_requirements),
                    "unsupported_requirements": list(score_data.unsupported_requirements),
                    "selected_claims": list(score_data.selected_claims),
                    "omitted_claims": list(score_data.omitted_claims),
                },
                "scoring.json": {
                    "matched_requirements": list(score_data.matched_requirements),
                    "unsupported_requirements": list(score_data.unsupported_requirements),
                    "selected_claims": list(score_data.selected_claims),
                    "omitted_claims": list(score_data.omitted_claims),
                    "validation_decisions": list(score_data.validation_decisions),
                },
            }
            provenance = {
                "job_snapshot_sha256": job_snapshot_sha256,
                "profile_sha256": profile_sha256,
                "source_resume_sha256": source_resume_sha256,
                "generation_config_sha256": generation_config_sha256,
            }
            rendered = render_resume_pdf(validated_resume)
            persisted = store.persist_success(
                resume_id,
                documents=documents,
                validated_resume=validated_resume,
                rendered=rendered,
                provenance=provenance,
            )
            _ensure_no_active_transaction(connection)
            transition_generated_resume_state(
                connection,
                resume_id=resume_id,
                from_state="rendering",
                to_state="ready",
                content_sha256=persisted.content_sha256,
                pdf_sha256=persisted.pdf_sha256,
                private_pdf_path=persisted.private_pdf_path,
                artifact_dir=resume_id,
                score=score_data.__dict__,
            )
            return get_generated_resume(
                connection,
                resume_id,
                public_shaping=public_shaping,
                raw_object=raw_object,
            )
        except Exception as exc:
            _ensure_no_active_transaction(connection)
            failure_code = _reason_code_to_failure_code(exc)
            try:
                store.persist_failure(
                    resume_id,
                    reason_code=failure_code,
                    evidence={"exception_type": type(exc).__name__},
                )
            except Exception:
                pass
            try:
                transition_generated_resume_state(
                    connection,
                    resume_id=resume_id,
                    to_state="failed",
                    reason_code=failure_code,
                    artifact_dir=resume_id,
                )
            except Exception:
                pass
            raise
        finally:
            store.close()
            store = None
    finally:
        if owns_resume_context:
            resume_ctx.close()


def resolve_generated_resume(
    connection: Any,
    resume_id: str,
    artifact_root: ArtifactRoot,
    *,
    expected_job_id: int | str | None = None,
    expected_job_snapshot_sha256: str | None = None,
    expected_profile_sha256: str | None = None,
    expected_source_resume_sha256: str | None = None,
    expected_generation_config_sha256: str | None = None,
) -> dict[str, Any]:
    """Resolve one ready artifact without exposing private filesystem evidence."""
    record = get_generated_resume_private(connection, resume_id)
    if record is None:
        raise KeyError(f"Generated resume '{resume_id}' not found")
    if record["state"] != "ready":
        raise ValueError(f"Generated resume state is '{record['state']}', expected 'ready'")

    if expected_job_id is not None and int(record["job_id"]) != int(expected_job_id):
        raise ValueError(f"Job ID mismatch: record has {record['job_id']}, expected {expected_job_id}")
    if expected_job_snapshot_sha256 is not None and record["job_snapshot_sha256"] != expected_job_snapshot_sha256:
        raise ValueError("Job snapshot SHA256 mismatch")
    if expected_profile_sha256 is not None and record["profile_sha256"] != expected_profile_sha256:
        raise ValueError("Profile SHA256 mismatch")
    if expected_source_resume_sha256 is not None and record["source_resume_sha256"] != expected_source_resume_sha256:
        raise ValueError("Source resume SHA256 mismatch")
    if expected_generation_config_sha256 is not None and record["generation_config_sha256"] != expected_generation_config_sha256:
        raise ValueError("Generation config SHA256 mismatch")

    # Read the manifest and job snapshot through secure descriptors before
    # consulting the mutable backlog.
    artifact = _secure_ready_artifact_files(record, artifact_root)
    current_snapshot = _current_snapshot_for_artifact(connection, record, artifact)
    if current_snapshot.job_id != int(record["job_id"]):
        raise ValueError(f"Job ID mismatch: record has {record['job_id']}, snapshot has {current_snapshot.job_id}")
    _verify_ready_file_hashes(record, artifact)
    description_override = _description_override_from_artifact(record, artifact)
    resolved_record = dict(record)
    resolved_record["_description_override"] = description_override
    return resolved_record


def show_generated_resume(
    connection: Any,
    resume_id: str,
) -> dict[str, Any] | None:
    """Return public shaped generated resume details for public/CLI view."""
    return get_generated_resume(connection, resume_id, public_shaping=True)


def list_public_generated_resumes(
    connection: Any,
    job_id: int | str | None = None,
    limit: int = 50,
    offset: int = 0,
    *,
    state: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """Return tuple of public shaped generated resume summaries."""
    return list_generated_resumes(
        connection,
        job_id=job_id,
        limit=limit,
        offset=offset,
        state=state,
        public_shaping=True,
    )
