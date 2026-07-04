from __future__ import annotations

import re
from hashlib import sha1
from typing import Iterable

from bs4 import BeautifulSoup, Tag

from .contracts import ButtonSnapshot, FieldKind, FieldSnapshot, PageSnapshot

_BLOCKER_PATTERNS = {
    "captcha": re.compile(r"\b(captcha|recaptcha|hcaptcha)\b", re.I),
    "sign_in": re.compile(r"\b(sign in|log in|login|create account)\b", re.I),
    "assessment": re.compile(r"\b(assessment|quiz|test required)\b", re.I),
    "payment": re.compile(r"\b(payment|credit card|billing)\b", re.I),
    "identity": re.compile(r"\b(passport|driver.?s license|ssn|social security|government id)\b", re.I),
}
_ERROR_SELECTOR = "[role='alert'], .error, .errors, .alert, .validation, [aria-invalid='true']"


def _visible(tag: Tag) -> bool:
    if tag.has_attr("hidden"):
        return False
    if tag.name == "input" and str(tag.get("type", "")).lower() == "hidden":
        return False
    style = str(tag.get("style", "")).replace(" ", "").lower()
    return "display:none" not in style and "visibility:hidden" not in style


def _text(tag: Tag) -> str:
    return " ".join(tag.get_text(" ", strip=True).split())


def _field_id(tag: Tag, index: int) -> str:
    raw = tag.get("id") or tag.get("name") or tag.get("aria-label") or f"field-{index}"
    return re.sub(r"[^a-zA-Z0-9_.:-]+", "-", str(raw)).strip("-") or f"field-{index}"


def _button_id(tag: Tag, index: int) -> str:
    raw = tag.get("id") or tag.get("name") or _text(tag) or tag.get("value") or f"button-{index}"
    slug = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", str(raw).lower()).strip("-")
    if not slug:
        slug = sha1(str(tag).encode()).hexdigest()[:8]
    return slug


def _selector(tag: Tag, fallback: str) -> str:
    if tag.get("id"):
        return f"#{tag['id']}"
    if tag.get("name"):
        return f"{tag.name}[name='{tag['name']}']"
    return fallback


def _label_for(tag: Tag, soup: BeautifulSoup) -> str:
    if tag.get("aria-label"):
        return str(tag["aria-label"]).strip()
    if tag.get("id"):
        label = soup.find("label", attrs={"for": tag["id"]})
        if isinstance(label, Tag):
            text = _text(label)
            if text:
                return text
    parent_label = tag.find_parent("label")
    if isinstance(parent_label, Tag):
        clone_text = _text(parent_label)
        own = str(tag.get("value") or "")
        return clone_text.replace(own, "").strip() or clone_text
    placeholder = tag.get("placeholder")
    if placeholder:
        return str(placeholder).strip()
    name = tag.get("name") or tag.get("id")
    return str(name or "").replace("_", " ").strip()


def _kind(tag: Tag) -> FieldKind:
    if tag.name == "textarea":
        return FieldKind.TEXTAREA
    if tag.name == "select":
        return FieldKind.SELECT
    input_type = str(tag.get("type", "text")).lower()
    return {
        "checkbox": FieldKind.CHECKBOX,
        "radio": FieldKind.RADIO,
        "file": FieldKind.FILE,
        "hidden": FieldKind.UNKNOWN,
    }.get(input_type, FieldKind.TEXT)


def _options(tag: Tag) -> tuple[str, ...]:
    if tag.name == "select":
        return tuple(_text(option) or str(option.get("value", "")) for option in tag.find_all("option"))
    if tag.name == "input" and str(tag.get("type", "")).lower() == "radio":
        name = tag.get("name")
        if name:
            root = tag.find_parent("form") or tag.parent or tag
            return tuple(str(item.get("value", "")) for item in root.find_all("input", attrs={"type": "radio", "name": name}))
    return ()


def _value(tag: Tag) -> str | bool | None:
    if tag.name == "textarea":
        return _text(tag)
    if tag.name == "select":
        selected = tag.find("option", selected=True)
        return str(selected.get("value") or _text(selected)) if isinstance(selected, Tag) else None
    if tag.name == "input" and str(tag.get("type", "")).lower() in {"checkbox", "radio"}:
        return tag.has_attr("checked")
    return None if tag.get("value") is None else str(tag.get("value"))


def _required(tag: Tag) -> bool:
    return tag.has_attr("required") or str(tag.get("aria-required", "")).lower() == "true"


def _final_submit_candidate(text: str, button_type: str) -> bool:
    lowered = text.lower()
    return button_type == "submit" and bool(re.search(r"\b(submit|send application|apply now|finish)\b", lowered)) and not re.search(r"\b(next|continue|start)\b", lowered)


def observe_static_html(html: str, *, url: str = "static://fixture", frame: str = "main") -> PageSnapshot:
    soup = BeautifulSoup(html, "html.parser")
    title = _text(soup.title) if soup.title else ""
    fields: list[FieldSnapshot] = []
    for index, tag in enumerate(soup.find_all(["input", "textarea", "select"]), start=1):
        if not isinstance(tag, Tag) or _kind(tag) == FieldKind.UNKNOWN:
            continue
        field_id = _field_id(tag, index)
        fields.append(FieldSnapshot(
            id=field_id,
            kind=_kind(tag),
            label=_label_for(tag, soup),
            required=_required(tag),
            options=_options(tag),
            value=_value(tag),
            visible=_visible(tag),
            frame=frame,
            selector=_selector(tag, f"{tag.name}:nth-of-type({index})"),
        ))
    buttons: list[ButtonSnapshot] = []
    button_tags: Iterable[Tag] = list(soup.find_all("button")) + list(soup.find_all("input", attrs={"type": re.compile("submit|button", re.I)}))
    for index, tag in enumerate(button_tags, start=1):
        text = _text(tag) or str(tag.get("value") or "")
        button_type = str(tag.get("type", "submit" if tag.name == "button" else "button")).lower()
        buttons.append(ButtonSnapshot(
            id=_button_id(tag, index),
            text=text,
            type=button_type,
            disabled=tag.has_attr("disabled") or str(tag.get("aria-disabled", "")).lower() == "true",
            final_submit_candidate=_final_submit_candidate(text, button_type),
            visible=_visible(tag),
            frame=frame,
            selector=_selector(tag, f"{tag.name}:nth-of-type({index})"),
        ))
    visible_text = _text(soup)
    blockers = tuple(name for name, pattern in _BLOCKER_PATTERNS.items() if pattern.search(visible_text))
    errors = tuple(dict.fromkeys(_text(tag) for tag in soup.select(_ERROR_SELECTOR) if isinstance(tag, Tag) and _text(tag)))
    return PageSnapshot(url=url, title=title, fields=tuple(fields), buttons=tuple(buttons), errors=errors, blockers=blockers)
