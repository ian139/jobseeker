from __future__ import annotations

from string import Formatter

from job_scraper.outreach.models import OutreachContact


def render_message(template: str, contact: OutreachContact) -> str:
    values = _template_values(contact)
    try:
        fields = [field_name for _, field_name, _, _ in Formatter().parse(template) if field_name is not None]
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    for field_name in fields:
        if not field_name or field_name not in values:
            raise ValueError(f"Unknown outreach template placeholder: {field_name}")
    try:
        return template.format(**values)
    except (KeyError, IndexError, AttributeError, ValueError) as exc:
        raise ValueError(str(exc)) from exc


def _template_values(contact: OutreachContact) -> dict[str, str]:
    return {
        "first_name": _first_name(contact.full_name),
        "full_name": contact.full_name or "",
        "company": contact.company or "",
        "role_title": contact.role_title or "",
        "company_domain": contact.company_domain or "",
        "job_id": contact.job_id or "",
        "job_title": contact.job_title or "",
        "linkedin_profile_url": contact.linkedin_profile_url or "",
        "notes": contact.notes or "",
    }


def _first_name(full_name: str) -> str:
    stripped = full_name.strip()
    if not stripped:
        return ""
    return stripped.split(maxsplit=1)[0]
