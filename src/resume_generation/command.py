from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, NoReturn

from .generator import ResumeJob, generate_resume

_MAX_STDIN_BYTES = 4 * 1024 * 1024
_INPUT_KEYS = frozenset({
    "schema",
    "id",
    "title",
    "company",
    "description",
    "location",
    "posted_at",
})
_ERROR_MESSAGES = {
    "invalid_input": "resume generation input was rejected",
    "generation_error": "resume generation failed",
}


class _InputError(Exception):
    pass


class _QuietArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _InputError(message)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _emit_error(code: str) -> int:
    payload = {"error": {"code": code, "message": _ERROR_MESSAGES[code]}}
    sys.stderr.buffer.write(_canonical_json(payload) + b"\n")
    return 1


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _InputError("duplicate JSON key")
        result[key] = value
    return result


def _read_job() -> ResumeJob:
    raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
    if len(raw) > _MAX_STDIN_BYTES:
        raise _InputError("stdin exceeds limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _InputError("stdin is not UTF-8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicates)
    except (json.JSONDecodeError, _InputError) as exc:
        raise _InputError("stdin is not valid JSON") from exc
    if type(value) is not dict or set(value) != _INPUT_KEYS:
        raise _InputError("job snapshot has invalid keys")
    if raw != _canonical_json(value):
        raise _InputError("job snapshot is not canonical JSON")
    if value["schema"] != "resume-job-v1":
        raise _InputError("job snapshot has invalid schema")
    if type(value["id"]) is not int or value["id"] <= 0:
        raise _InputError("job id must be positive")
    for key in ("title", "company", "description"):
        if type(value[key]) is not str or not value[key].strip():
            raise _InputError(f"{key} must be nonblank")
    for key in ("location", "posted_at"):
        if value[key] is not None and type(value[key]) is not str:
            raise _InputError(f"{key} must be a string or null")
    try:
        return ResumeJob(
            id=value["id"],
            title=value["title"],
            company=value["company"],
            description=value["description"],
            location=value["location"],
            posted_at=value["posted_at"],
        )
    except (TypeError, ValueError) as exc:
        raise _InputError("job snapshot is invalid") from exc


def _absolute_path(value: str, label: str) -> Path:
    if not value or "\x00" in value:
        raise _InputError(f"{label} path is invalid")
    try:
        return Path(os.path.abspath(value))
    except OSError as exc:
        raise _InputError(f"{label} path is invalid") from exc


def _reject_symlink_components(path: Path, label: str, *, leaf_may_not_exist: bool) -> None:
    current = Path(path.anchor)
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if leaf_may_not_exist or index < len(parts) - 1:
                continue
            raise _InputError(f"{label} path does not exist") from None
        except OSError as exc:
            raise _InputError(f"{label} path is unsafe") from exc
        if stat.S_ISLNK(info.st_mode):
            raise _InputError(f"{label} path is a symlink")


def _regular_input(value: str, label: str) -> Path:
    path = _absolute_path(value, label)
    _reject_symlink_components(path, label, leaf_may_not_exist=False)
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise _InputError(f"{label} path is unsafe") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise _InputError(f"{label} path is unsafe")
    if label == "profile" and info.st_mode & 0o077:
        raise _InputError("profile path is unsafe")
    return path


def _output_root(value: str) -> Path:
    path = _absolute_path(value, "output root")
    _reject_symlink_components(path, "output root", leaf_may_not_exist=True)
    existing = path
    try:
        while not existing.exists():
            if existing == existing.parent:
                raise _InputError("output root has no existing parent")
            existing = existing.parent
        info = os.stat(existing, follow_symlinks=False)
    except _InputError:
        raise
    except OSError as exc:
        raise _InputError("output root parent is unsafe") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise _InputError("output root parent is unsafe")
    return path


def _compiler(value: str | None) -> str | None:
    if value is None:
        return None
    if not value or "\x00" in value or any(character.isspace() for character in value):
        raise _InputError("compiler must be one executable")
    try:
        resolved = shutil.which(value)
        if resolved is None:
            raise _InputError("compiler was not found")
        path = _absolute_path(os.path.realpath(resolved), "compiler")
        _reject_symlink_components(path, "compiler", leaf_may_not_exist=False)
        info = os.stat(path, follow_symlinks=False)
        executable = os.access(path, os.X_OK)
    except _InputError:
        raise
    except OSError as exc:
        raise _InputError("compiler is unsafe") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or not executable:
        raise _InputError("compiler is unsafe")
    return str(path)


def _parser() -> argparse.ArgumentParser:
    parser = _QuietArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--skill", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--compiler")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        profile = _regular_input(args.profile, "profile")
        template = _regular_input(args.template, "template")
        skill = _regular_input(args.skill, "skill")
        output_root = _output_root(args.output_root)
        compiler = _compiler(args.compiler)
        job = _read_job()
    except _InputError:
        return _emit_error("invalid_input")

    try:
        generated = generate_resume(
            job,
            profile_path=profile,
            template_path=template,
            skill_path=skill,
            output_root=output_root,
            compiler=compiler,
        )
        artifact_dir = generated.pdf_path.parent
        payload = {
            "schema": "generated-resume-v1",
            "job_id": generated.job_id,
            "artifact_ref": generated.artifact_ref,
            "tex_path": str(generated.tex_path.resolve(strict=True)),
            "pdf_path": str(generated.pdf_path.resolve(strict=True)),
            "report_path": str(generated.report_path.resolve(strict=True)),
            "manifest_path": str((artifact_dir / "manifest.json").resolve(strict=True)),
            "pages": generated.pages,
            "field": generated.field,
            "graduation_date": generated.graduation_date,
            "matched_keywords": list(generated.matched_keywords),
        }
        sys.stdout.buffer.write(_canonical_json(payload) + b"\n")
        return 0
    except Exception:
        return _emit_error("generation_error")


if __name__ == "__main__":
    raise SystemExit(main())
