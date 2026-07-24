"""Deterministic, local resume PDF rendering and private artifact persistence.

This module intentionally depends only on the standard library and :mod:`pypdf`.
The renderer accepts the small mapping returned by the resume validation layer;
it does not know about jobs, profiles, databases, HTML, or templates.  The
artifact store keeps its output in an owner-only, descriptor-confined run
directory and publishes each file atomically.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import textwrap
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping, Sequence

from pypdf import PdfReader


__all__ = (
    "ARTIFACT_FILENAMES",
    "DOCUMENT_FILENAMES",
    "PersistedResumeRun",
    "RenderedPdf",
    "ResumeArtifactError",
    "ResumeArtifactSecurityError",
    "ResumeRenderError",
    "ResumeValidationError",
    "RESUME_FAILURE_REASON_CODES",
    "ResumeArtifactStore",
    "render_resume_pdf",
)


# These are deliberately fixed.  A failure record never stores caller-provided
# messages or evidence values, only the code below and an evidence digest.
RESUME_FAILURE_REASON_CODES = frozenset(
    {
        "ARTIFACT_WRITE_FAILED",
        "ALTERED_FACT",
        "EXTRANEOUS_KEYS",
        "INVALID_PROVENANCE_HASH",
        "MALFORMED_JSON",
        "MISSING_CITATION",
        "OVERSIZED_JSON",
        "PAGE_LIMIT_EXCEEDED",
        "PDF_EXTRACTION_MISMATCH",
        "PRIVACY_VIOLATION",
        "PROMPT_INJECTION_DETECTED",
        "RENDERER_FAILURE",
        "SENSITIVE_INFERENCE_REJECTED",
        "UNSUPPORTED_CLAIM",
    }
)

DOCUMENT_FILENAMES = (
    "job_snapshot.json",
    "candidate_claims.json",
    "generation_request.json",
    "generation_response.json",
    "validation.json",
    "scoring.json",
)
ARTIFACT_FILENAMES = DOCUMENT_FILENAMES + (
    "resume.json",
    "resume.pdf",
    "manifest.json",
)

_RESUME_TOP_KEYS = frozenset(
    {
        "schema_version",
        "job_snapshot_sha256",
        "profile_sha256",
        "source_resume_sha256",
        "headline",
        "summary",
        "experience",
        "skills",
        "education",
        "omitted_claim_ids",
        "missing_fact_questions",
        "generation_notes",
    }
)
_RESUME_HASH_KEYS = frozenset(
    {
        "job_snapshot_sha256",
        "profile_sha256",
        "source_resume_sha256",
    }
)
_PROVENANCE_KEYS = _RESUME_HASH_KEYS | {"generation_config_sha256"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_TEXT_CHARS = 50_000
_MAX_ITEMS = 256
_MAX_RENDER_LINES = 54
_WRAP_WIDTH = 104

_OPEN_DIRECTORY_FLAGS = os.O_RDONLY
for _flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"):
    _OPEN_DIRECTORY_FLAGS |= getattr(os, _flag, 0)
_OPEN_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_WRITE_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


class ResumeArtifactError(RuntimeError):
    """Base error for rendering or persistence failures."""


class ResumeValidationError(ValueError, ResumeArtifactError):
    """The supplied mapping is not a validated resume contract."""


class ResumeRenderError(ResumeArtifactError):
    """A valid resume could not be rendered into a bounded PDF."""


class ResumeArtifactSecurityError(ResumeArtifactError):
    """An artifact root, run, or path failed a filesystem safety check."""


@dataclass(frozen=True)
class RenderedPdf:
    """The verified result of :func:`render_resume_pdf`.

    ``sha256`` is the SHA-256 digest of ``bytes_data``.  ``extracted_text`` is
    captured from the exact bytes with pypdf after rendering, not from the
    source mapping.
    """

    bytes_data: bytes
    sha256: str
    extracted_text: str
    page_count: int

    def __post_init__(self) -> None:
        if type(self.bytes_data) is not bytes:
            raise TypeError("bytes_data must be bytes")
        expected = _sha256(self.bytes_data)
        if self.sha256 != expected:
            raise ValueError("sha256 does not match bytes_data")
        if type(self.page_count) is not int or self.page_count < 1:
            raise ValueError("page_count must be positive")
        if type(self.extracted_text) is not str:
            raise TypeError("extracted_text must be a string")

    @property
    def pdf_sha256(self) -> str:
        """Alias useful to callers constructing a generated artifact record."""

        return self.sha256


@dataclass(frozen=True)
class PersistedResumeRun:
    """Private paths and hashes produced after a complete successful run."""

    private_pdf_path: Path
    manifest_sha256: str
    content_sha256: str

    @property
    def pdf_sha256(self) -> str:
        """Read the published PDF digest from the manifest-independent result."""

        with self.private_pdf_path.open("rb") as handle:
            return _sha256(handle.read())


# ---------------------------------------------------------------------------
# Mapping validation and deterministic display extraction


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ResumeValidationError("resume data is not canonical JSON") from exc
    if b"\x00" in encoded:
        raise ResumeValidationError("resume data contains a NUL byte")
    return encoded


def _normal_text(value: Any, *, field: str, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise ResumeValidationError(f"{field} must be a string")
    if len(value) > _MAX_TEXT_CHARS:
        raise ResumeValidationError(f"{field} exceeds the text limit")
    # Keep facts, but make line boundaries deterministic and reject control
    # characters that could alter a PDF content stream or an extracted-text
    # assertion.  cp1252 is the encoding declared by the built-in PDF font.
    if any(ord(char) < 0x20 and char not in "\t\r\n" for char in value):
        raise ResumeValidationError(f"{field} contains a control character")
    normalized = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    try:
        normalized.encode("cp1252")
    except UnicodeEncodeError as exc:
        raise ResumeValidationError(f"{field} contains unsupported characters") from exc
    if not normalized and not allow_empty:
        raise ResumeValidationError(f"{field} must not be empty")
    return normalized


def _mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResumeValidationError(f"{field} must be a mapping")
    return value


def _claim_ids(value: Any, *, field: str, allow_empty: bool = False) -> None:
    if type(value) is not list or len(value) > _MAX_ITEMS or (not allow_empty and not value):
        if allow_empty:
            raise ResumeValidationError(f"{field}.claim_ids must be a bounded list")
        raise ResumeValidationError(f"{field}.claim_ids must be a non-empty list")
    for index, claim_id in enumerate(value):
        _normal_text(claim_id, field=f"{field}.claim_ids[{index}]")


def _leaf_text(value: Any, *, field: str, allow_empty: bool = False) -> str:
    item = _mapping(value, field=field)
    if set(item.keys()) != {"text", "claim_ids"}:
        raise ResumeValidationError(f"{field} leaf keys are invalid")
    text = _normal_text(item["text"], field=f"{field}.text", allow_empty=allow_empty)
    claim_ids = item["claim_ids"]
    _claim_ids(claim_ids, field=field, allow_empty=allow_empty)
    if not text and claim_ids:
        raise ResumeValidationError(f"{field}.claim_ids must be empty when text is empty")
    if text and not claim_ids:
        raise ResumeValidationError(f"{field}.claim_ids must be non-empty when text is non-empty")
    return text


def _hash_provenance(value: Any) -> dict[str, str]:
    item = _mapping(value, field="provenance")
    if set(item.keys()) != _PROVENANCE_KEYS:
        raise ResumeValidationError("provenance keys are invalid")
    result: dict[str, str] = {}
    for key in sorted(_PROVENANCE_KEYS):
        candidate = item[key]
        if type(candidate) is not str or _SHA256_RE.fullmatch(candidate) is None:
            raise ResumeValidationError(f"provenance.{key} is not a SHA-256 digest")
        result[key] = candidate
    return result

def _resume_hashes(resume: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in sorted(_RESUME_HASH_KEYS):
        candidate = resume[key]
        if type(candidate) is not str or _SHA256_RE.fullmatch(candidate) is None:
            raise ResumeValidationError(f"{key} is not a SHA-256 digest")
        result[key] = candidate
    return result


def _text_list(value: Any, *, field: str) -> list[str]:
    if type(value) is not list or len(value) > _MAX_ITEMS:
        raise ResumeValidationError(f"{field} must be a bounded list")
    return [
        _normal_text(item, field=f"{field}[{index}]", allow_empty=True)
        for index, item in enumerate(value)
    ]


def _validate_resume(resume: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...], dict[str, str]]:
    if not isinstance(resume, Mapping):
        raise ResumeValidationError("resume must be a mapping")
    if set(resume.keys()) != _RESUME_TOP_KEYS:
        raise ResumeValidationError("resume top-level keys are invalid")

    provenance = _resume_hashes(resume)
    schema_version = resume["schema_version"]
    if type(schema_version) is not int or schema_version != 1:
        raise ResumeValidationError("schema_version must be integer 1")
    headline = _leaf_text(resume["headline"], field="headline", allow_empty=True)
    summary = _leaf_text(resume["summary"], field="summary", allow_empty=True)

    skills_raw = resume["skills"]
    if type(skills_raw) is not list or len(skills_raw) > _MAX_ITEMS:
        raise ResumeValidationError("skills must be a bounded list")
    skills: list[dict[str, Any]] = []
    display: list[str] = []
    if headline:
        display.append(headline)
    if summary:
        display.append(summary)
    for index, raw in enumerate(skills_raw):
        item = _mapping(raw, field=f"skills[{index}]")
        if set(item.keys()) != {"name", "claim_ids"}:
            raise ResumeValidationError(f"skills[{index}] keys are invalid")
        name = _normal_text(item["name"], field=f"skills[{index}].name")
        _claim_ids(item["claim_ids"], field=f"skills[{index}]")
        skills.append({"name": name, "claim_ids": list(item["claim_ids"])})
        display.append(name)

    experience = _validate_entries(resume["experience"], kind="experience")
    for item in experience:
        display.extend((item["organization"], item["role"], item["dates"]))
        display.extend(bullet["text"] for bullet in item["bullets"])

    education = _validate_entries(resume["education"], kind="education")
    for item in education:
        display.extend((item["institution"], item["degree"], item["dates"]))
        display.extend(bullet["text"] for bullet in item["bullets"])

    _text_list(resume["omitted_claim_ids"], field="omitted_claim_ids")
    _text_list(resume["missing_fact_questions"], field="missing_fact_questions")
    _text_list(resume["generation_notes"], field="generation_notes")

    normalized = {
        "schema_version": schema_version,
        **provenance,
        "headline": headline,
        "summary": summary,
        "experience": experience,
        "skills": skills,
        "education": education,
    }
    return normalized, tuple(display), provenance


def _validate_entries(value: Any, *, kind: str) -> list[dict[str, Any]]:
    if type(value) is not list or len(value) > _MAX_ITEMS:
        raise ResumeValidationError(f"{kind} must be a bounded list")
    if kind == "experience":
        expected = {"source_entry_id", "organization", "role", "dates", "bullets"}
        first_key, second_key = "organization", "role"
    else:
        expected = {"institution", "degree", "dates", "bullets"}
        first_key, second_key = "institution", "degree"
    entries: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        item = _mapping(raw, field=f"{kind}[{index}]")
        if set(item.keys()) != expected:
            raise ResumeValidationError(f"{kind}[{index}] keys are invalid")
        source_entry_id = (
            _normal_text(item["source_entry_id"], field=f"{kind}[{index}].source_entry_id")
            if kind == "experience"
            else None
        )
        first = _normal_text(item[first_key], field=f"{kind}[{index}].{first_key}")
        second = _normal_text(item[second_key], field=f"{kind}[{index}].{second_key}")
        dates = _normal_text(item["dates"], field=f"{kind}[{index}].dates")
        bullets_raw = item["bullets"]
        if type(bullets_raw) is not list or not bullets_raw or len(bullets_raw) > _MAX_ITEMS:
            raise ResumeValidationError(f"{kind}[{index}].bullets must be a non-empty bounded list")
        bullets: list[dict[str, Any]] = []
        for bullet_index, bullet_raw in enumerate(bullets_raw):
            bullet = _mapping(bullet_raw, field=f"{kind}[{index}].bullets[{bullet_index}]")
            if set(bullet.keys()) != {"text", "claim_ids"}:
                raise ResumeValidationError(f"{kind}[{index}].bullets[{bullet_index}] keys are invalid")
            text = _normal_text(
                bullet["text"],
                field=f"{kind}[{index}].bullets[{bullet_index}].text",
            )
            _claim_ids(bullet["claim_ids"], field=f"{kind}[{index}].bullets[{bullet_index}]")
            bullets.append({"text": text, "claim_ids": list(bullet["claim_ids"])})
        entry = {first_key: first, second_key: second, "dates": dates, "bullets": bullets}
        if source_entry_id is not None:
            entry = {"source_entry_id": source_entry_id, **entry}
        entries.append(entry)
    return entries




def _display_lines(normalized: Mapping[str, Any]) -> tuple[tuple[str, int], ...]:
    # Section order is intentionally literal and independent of mapping order.
    lines: list[tuple[str, int]] = [("Resume", 15)]
    if normalized.get("headline"):
        lines.append((normalized["headline"], 13))
    lines.append(("Summary", 11))
    if normalized["summary"]:
        lines.append((normalized["summary"], 9))
    lines.append(("Experience", 11))
    for item in normalized["experience"]:
        if item["organization"]:
            lines.append((item["organization"], 10))
        role_dates = " | ".join(part for part in (item["role"], item["dates"]) if part)
        if role_dates:
            lines.append((role_dates, 9))
        for bullet in item["bullets"]:
            lines.append((f"- {bullet['text']}", 9))
    lines.append(("Skills", 11))
    for skill in normalized["skills"]:
        lines.append((skill["name"], 9))
    lines.append(("Education", 11))
    for item in normalized["education"]:
        if item["institution"]:
            lines.append((item["institution"], 10))
        degree_dates = " | ".join(part for part in (item["degree"], item["dates"]) if part)
        if degree_dates:
            lines.append((degree_dates, 9))
        for bullet in item["bullets"]:
            lines.append((f"- {bullet['text']}", 9))
    return tuple(lines)


def _wrapped_display_lines(lines: Sequence[tuple[str, int]]) -> tuple[tuple[str, int], ...]:
    wrapped: list[tuple[str, int]] = []
    for text, size in lines:
        # Do not silently omit or truncate a fact.  textwrap only breaks at a
        # word boundary; a long token is split as a final bounded fallback.
        chunks = textwrap.wrap(
            text,
            width=_WRAP_WIDTH,
            break_long_words=True,
            break_on_hyphens=False,
            replace_whitespace=False,
            drop_whitespace=True,
        ) or [""]
        wrapped.extend((chunk, size) for chunk in chunks)
    return tuple(wrapped)


# ---------------------------------------------------------------------------
# Deterministic one-page PDF


def _pdf_literal(text: str) -> bytes:
    raw = text.encode("cp1252")
    output = bytearray(b"(")
    for byte in raw:
        if byte in (0x28, 0x29, 0x5C):  # (, ), \
            output.extend((0x5C, byte))
        elif byte in (0x09, 0x0A, 0x0D):
            output.extend((0x5C, {0x09: ord("t"), 0x0A: ord("n"), 0x0D: ord("r")} [byte]))
        elif byte < 0x20 or byte > 0x7E:
            output.extend(f"\\{byte:03o}".encode("ascii"))
        else:
            output.append(byte)
    output.append(0x29)
    return bytes(output)


def _pdf_bytes(lines: Sequence[tuple[str, int]]) -> bytes:
    content = bytearray()
    top_y = 748
    line_height = 13
    for index, (text, size) in enumerate(lines):
        y = top_y - index * line_height
        content.extend(b"BT\n")
        content.extend(f"/F1 {size} Tf\n1 0 0 1 48 {y} Tm\n".encode("ascii"))
        content.extend(_pdf_literal(text))
        content.extend(b" Tj\nET\n")
    stream = bytes(content)
    bodies = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"endstream",
    )
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_number, body in enumerate(bodies, start=1):
        offsets.append(len(output))
        output.extend(f"{object_number} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(bodies) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(bodies) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


def _collapsed(text: str) -> str:
    return " ".join(text.split())


def render_resume_pdf(resume: Mapping[str, Any], *, max_pages: int = 1) -> RenderedPdf:
    """Render a validated resume mapping into a deterministic one-page PDF.

    The function has no network, HTML, browser, font-file, or template input.
    ``max_pages`` may be greater than one for a caller's common bound API, but
    this renderer always emits exactly one page and rejects a resume that does
    not fit that page.
    """

    if type(max_pages) is not int or max_pages < 1:
        raise ValueError("max_pages must be a positive integer")
    normalized, factual_values, _ = _validate_resume(resume)
    lines = _wrapped_display_lines(_display_lines(normalized))
    if len(lines) > _MAX_RENDER_LINES:
        raise ResumeRenderError("resume exceeds the one-page limit")
    payload = _pdf_bytes(lines)
    try:
        reader = PdfReader(BytesIO(payload), strict=True)
        page_count = len(reader.pages)
        extracted = reader.pages[0].extract_text() if page_count else ""
    except Exception as exc:  # pypdf exposes several parser exception types
        raise ResumeRenderError("rendered PDF could not be parsed") from exc
    extracted = extracted or ""
    if page_count < 1 or page_count > max_pages or page_count != 1:
        raise ResumeRenderError("rendered PDF page count is outside the requested bound")
    expected_text = _collapsed(" ".join(text for text, _ in lines))
    actual_text = _collapsed(extracted)
    if actual_text != expected_text:
        raise ResumeRenderError("rendered PDF extracted text does not match content")
    for factual in factual_values:
        if factual and _collapsed(factual) not in actual_text:
            raise ResumeRenderError("rendered PDF omitted validated resume content")
    return RenderedPdf(bytes_data=payload, sha256=_sha256(payload), extracted_text=extracted, page_count=page_count)


# ---------------------------------------------------------------------------
# Descriptor-confined owner-private artifact store


def _security(message: str, exc: BaseException | None = None) -> ResumeArtifactSecurityError:
    error = ResumeArtifactSecurityError(message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _absolute_lexical(path: str | os.PathLike[str]) -> Path:
    try:
        raw = os.fspath(path)
    except TypeError as exc:
        raise _security("artifact root path is invalid", exc) from exc
    if not isinstance(raw, str) or not raw:
        raise _security("artifact root path is invalid")
    candidate = Path(raw)
    if any(part == ".." for part in candidate.parts):
        raise _security("artifact root path may not contain traversal")
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = Path(os.path.normpath(str(candidate)))
    if candidate == Path(candidate.anchor or "/"):
        raise _security("artifact root may not be filesystem root")
    return candidate


def _verify_directory(fd: int, *, final: bool) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise _security("artifact component is not a directory")
    if final and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise _security("artifact directory must be owner-private")


def _open_directory_tree(path: Path) -> int:
    parts = tuple(part for part in path.parts if part not in (path.anchor, ""))
    current_fd = os.open(path.anchor or "/", _OPEN_DIRECTORY_FLAGS)
    try:
        for index, part in enumerate(parts):
            is_final = index == len(parts) - 1
            child_fd: int | None = None
            try:
                try:
                    child_fd = os.open(part, _OPEN_DIRECTORY_FLAGS, dir_fd=current_fd)
                except FileNotFoundError:
                    try:
                        os.mkdir(part, 0o700, dir_fd=current_fd)
                    except FileExistsError:
                        # A concurrent creator is safe only after O_NOFOLLOW open
                        # and identity checks below.
                        pass
                    child_fd = os.open(part, _OPEN_DIRECTORY_FLAGS, dir_fd=current_fd)
                    os.fchmod(child_fd, 0o700)
                except OSError as exc:
                    raise _security("artifact directory is unavailable", exc) from exc
                assert child_fd is not None
                expected = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
                actual = os.fstat(child_fd)
                if (
                    expected.st_dev != actual.st_dev
                    or expected.st_ino != actual.st_ino
                    or not stat.S_ISDIR(expected.st_mode)
                ):
                    raise _security("artifact directory changed while opening")
                _verify_directory(child_fd, final=is_final)
                os.close(current_fd)
                current_fd = child_fd
                child_fd = None
            except Exception:
                if child_fd is not None:
                    try:
                        os.close(child_fd)
                    except OSError:
                        pass
                raise
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _validate_resume_id(value: str) -> str:
    if type(value) is not str:
        raise _security("resume_id must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise _security("resume_id must be a UUID string", exc) from exc
    canonical = str(parsed)
    if value != canonical:
        raise _security("resume_id must use canonical UUID spelling")
    return canonical


def _safe_filename(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise _security("artifact filename is unsafe")


def _write_atomic(directory_fd: int, name: str, payload: bytes) -> None:
    _safe_filename(name)
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    fd: int | None = None
    try:
        fd = os.open(temporary, _WRITE_FILE_FLAGS, 0o600, dir_fd=directory_fd)
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(fd, view[written:])
            if count <= 0:
                raise OSError("short write")
            written += count
        os.fsync(fd)
        os.close(fd)
        fd = None
        # link() is an atomic no-replace publication primitive: unlike rename,
        # it cannot overwrite an existing target or a target symlink.
        os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        os.unlink(temporary, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise


def _unlink_files(directory_fd: int, names: Sequence[str]) -> None:
    for name in names:
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            continue
        except OSError:
            continue
    try:
        os.fsync(directory_fd)
    except OSError:
        pass


def _document_payload(value: Any, *, name: str) -> bytes:
    if isinstance(value, bytes):
        payload = value
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ResumeValidationError(f"{name} is not UTF-8") from exc
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = _canonical_json_bytes(value)
    if not payload or b"\x00" in payload:
        raise ResumeValidationError(f"{name} is empty or contains NUL")
    return payload


class ResumeArtifactStore:
    """Owner-private, one-run-per-resume artifact storage.

    The configured root and each run directory are opened by descriptor,
    component-by-component with ``O_NOFOLLOW``.  File publication is atomic,
    no-replace, and fsyncs both file and directory metadata.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root_path = _absolute_lexical(root)
        root_fd = _open_directory_tree(self._root_path)
        try:
            _verify_directory(root_fd, final=True)
        except Exception:
            try:
                os.close(root_fd)
            except OSError:
                pass
            raise
        self._root_fd = root_fd
        self._closed = False

    @property
    def root(self) -> Path:
        return self._root_path

    def __enter__(self) -> "ResumeArtifactStore":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def _ensure_open(self) -> int:
        if self._closed:
            raise ResumeArtifactError("artifact store is closed")
        return self._root_fd

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self._root_fd)

    def _create_run(self, resume_id: str) -> tuple[str, int]:
        root_fd = self._ensure_open()
        canonical_id = _validate_resume_id(resume_id)
        try:
            os.mkdir(canonical_id, 0o700, dir_fd=root_fd)
        except FileExistsError as exc:
            raise _security("resume run already exists", exc) from exc
        except OSError as exc:
            raise _security("resume run could not be created", exc) from exc
        run_fd: int | None = None
        try:
            run_fd = os.open(canonical_id, _OPEN_DIRECTORY_FLAGS, dir_fd=root_fd)
            os.fchmod(run_fd, 0o700)
            _verify_directory(run_fd, final=True)
            os.fsync(root_fd)
            result_fd = run_fd
            run_fd = None
            return canonical_id, result_fd
        except Exception:
            if run_fd is not None:
                try:
                    os.close(run_fd)
                except OSError:
                    pass
            try:
                os.rmdir(canonical_id, dir_fd=root_fd)
            except OSError:
                pass
            raise

    def _cleanup_run(self, name: str, run_fd: int, names: Sequence[str]) -> None:
        root_fd = self._ensure_open()
        _unlink_files(run_fd, names)
        try:
            os.close(run_fd)
        finally:
            try:
                os.rmdir(name, dir_fd=root_fd)
                os.fsync(root_fd)
            except OSError:
                pass

    def persist_success(
        self,
        resume_id: str,
        *,
        documents: Mapping[str, Any],
        validated_resume: Mapping[str, Any],
        rendered: RenderedPdf,
        provenance: Mapping[str, str],
    ) -> PersistedResumeRun:
        """Atomically persist every named provenance artifact and the manifest."""

        if not isinstance(documents, Mapping):
            raise TypeError("documents must be a mapping")
        if not isinstance(provenance, Mapping):
            raise TypeError("provenance must be a mapping")
        if not isinstance(validated_resume, Mapping):
            raise ResumeValidationError("validated_resume must be a mapping")
        if not isinstance(rendered, RenderedPdf):
            raise ResumeRenderError("rendered must be a verified RenderedPdf")
        # Re-validate at the persistence boundary: callers cannot smuggle a
        # different mapping into resume.json after rendering.
        _, _, resume_provenance = _validate_resume(validated_resume)
        normalized_provenance = _hash_provenance(provenance)
        for key in _RESUME_HASH_KEYS:
            if normalized_provenance[key] != resume_provenance[key]:
                raise ResumeValidationError("provenance does not match validated resume")
        expected_names = set(DOCUMENT_FILENAMES)
        normalized_documents: dict[str, bytes] = {}
        for stem in DOCUMENT_FILENAMES:
            candidate = documents.get(stem, documents.get(stem.removesuffix(".json")))
            if candidate is None:
                raise ResumeValidationError(f"missing provenance artifact {stem}")
            normalized_documents[stem] = _document_payload(candidate, name=stem)
        accepted_keys = expected_names | {stem.removesuffix(".json") for stem in DOCUMENT_FILENAMES}
        if set(documents.keys()) - accepted_keys:
            raise ResumeValidationError("unknown provenance artifact name")

        # Preserve the validated JSON value exactly (canonical key ordering is
        # deterministic, while whitespace inside an already validated fact is
        # part of its content hash).
        resume_payload = _canonical_json_bytes(validated_resume)
        content_sha256 = _sha256(resume_payload)
        all_payloads = dict(normalized_documents)
        all_payloads["resume.json"] = resume_payload
        all_payloads["resume.pdf"] = rendered.bytes_data
        artifact_meta = {
            name: {"bytes": len(payload), "sha256": _sha256(payload)}
            for name, payload in sorted(all_payloads.items())
        }
        manifest = {
            "schema_version": 1,
            "resume_id": _validate_resume_id(resume_id),
            "provenance": normalized_provenance,
            "content_sha256": content_sha256,
            "pdf_sha256": rendered.sha256,
            "page_count": rendered.page_count,
            "extracted_text_sha256": _sha256(rendered.extracted_text.encode("utf-8")),
            "artifacts": artifact_meta,
        }
        manifest_payload = _canonical_json_bytes(manifest)
        run_name, run_fd = self._create_run(resume_id)
        published: list[str] = []
        try:
            for name in ARTIFACT_FILENAMES[:-1]:
                _write_atomic(run_fd, name, all_payloads[name])
                published.append(name)
            _write_atomic(run_fd, "manifest.json", manifest_payload)
            published.append("manifest.json")
            private_pdf_path = self._root_path / run_name / "resume.pdf"
            return PersistedResumeRun(
                private_pdf_path=private_pdf_path,
                manifest_sha256=_sha256(manifest_payload),
                content_sha256=content_sha256,
            )
        except Exception:
            self._cleanup_run(run_name, run_fd, published)
            raise
        finally:
            # Successful runs retain their directory but not its descriptor.
            if run_fd is not None:
                try:
                    os.close(run_fd)
                except OSError:
                    pass

    def persist_failure(
        self,
        resume_id: str,
        *,
        reason_code: str,
        evidence: Mapping[str, Any] | None = None,
    ) -> Path:
        """Retain only fixed failure metadata, never caller evidence values."""

        if type(reason_code) is not str or reason_code not in RESUME_FAILURE_REASON_CODES:
            raise ValueError("unknown resume failure reason code")
        if evidence is not None and not isinstance(evidence, Mapping):
            raise TypeError("evidence must be a mapping or None")
        evidence_count = len(evidence) if evidence is not None else 0
        evidence_payload = _canonical_json_bytes(evidence if evidence is not None else {})
        failure = {
            "reason_code": reason_code,
            "evidence_count": evidence_count,
            "evidence_sha256": _sha256(evidence_payload),
        }
        failure_payload = _canonical_json_bytes(failure)
        run_name, run_fd = self._create_run(resume_id)
        try:
            _write_atomic(run_fd, "failure.json", failure_payload)
            return self._root_path / run_name / "failure.json"
        except Exception:
            self._cleanup_run(run_name, run_fd, ("failure.json",))
            raise
        finally:
            try:
                os.close(run_fd)
            except OSError:
                pass

    def render_and_persist(
        self,
        resume_id: str,
        *,
        documents: Mapping[str, Any],
        validated_resume: Mapping[str, Any],
        provenance: Mapping[str, str],
        max_pages: int = 1,
    ) -> PersistedResumeRun:
        """Render and persist, retaining redacted evidence if rendering fails."""

        try:
            rendered = render_resume_pdf(validated_resume, max_pages=max_pages)
        except Exception as exc:
            # The exception message may contain facts or paths; retain only a
            # stable exception type marker and no message text.
            self.persist_failure(
                resume_id,
                reason_code="RENDERER_FAILURE",
                evidence={"exception_type": type(exc).__name__},
            )
            raise
        return self.persist_success(
            resume_id,
            documents=documents,
            validated_resume=validated_resume,
            rendered=rendered,
            provenance=provenance,
        )
