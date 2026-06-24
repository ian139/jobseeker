from __future__ import annotations

from urllib.parse import urlparse


def normalize_linkedin_profile_url(value: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None
    if not candidate.startswith(("http://", "https://")):
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if host not in {"linkedin.com", "www.linkedin.com"}:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() != "in":
        return None

    slug = parts[1].strip().lower()
    if not slug:
        return None
    return f"https://www.linkedin.com/in/{slug}"
