from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

from .contracts import FieldKind, ObservedButton, ObservedField, PageSnapshot
from .policy import is_final_submit_text

FIELD_TAGS = {"input", "textarea", "select"}
BUTTON_TAGS = {"button"}
ERROR_RE = re.compile(r"\b(error|required|invalid|captcha|sign in|login|expired|not\s*found|notfound|payment|assessment|identity|email verification|verify email)\b", re.IGNORECASE)
BLOCKER_RE = re.compile(r"\b(captcha|sign in|login|job no longer|expired|not\s*found|notfound|payment|assessment|identity verification|identity|email verification|verify email)\b", re.IGNORECASE)
ACTION_LINK_RE = re.compile(r"\b(apply|next|continue|submit|review)\b", re.IGNORECASE)


def normalize_space(value: str | None) -> str:
    return " ".join((value or "").split())


class _ApplyFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: list[dict[str, Any]] = []
        self.buttons: list[dict[str, Any]] = []
        self.labels_by_for: dict[str, str] = {}
        self.current_label_for: str | None = None
        self.in_label = False
        self.current_label_parts: list[str] = []
        self.current_label_field_ids: list[str] = []
        self.current_button: dict[str, Any] | None = None
        self.current_button_parts: list[str] = []
        self.current_select_id: str | None = None
        self.current_option_parts: list[str] = []
        self.text_parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self.ignored_depth += 1
            return
        if tag == "label":
            self.in_label = True
            self.current_label_for = attrs_dict.get("for") or None
            self.current_label_parts = []
            self.current_label_field_ids = []
        if tag == "button" or (tag == "input" and (attrs_dict.get("type") or "").lower() in {"button", "submit"}):
            text = attrs_dict.get("value") or attrs_dict.get("aria-label") or ""
            button_id = element_id(tag, attrs_dict, len(self.buttons))
            self.current_button = {
                "id": button_id,
                "text": text,
                "type": attrs_dict.get("type") or None,
                "disabled": "disabled" in attrs_dict or attrs_dict.get("aria-disabled") == "true",
                "selector": selector_from_attrs(tag, attrs_dict, button_id),
            }
            self.current_button_parts = []
            if tag == "input":
                self.buttons.append(self.current_button)
                self.current_button = None
        if tag == "a" and attrs_dict.get("href"):
            text = attrs_dict.get("aria-label") or attrs_dict.get("title") or ""
            button_id = element_id(tag, attrs_dict, len(self.buttons))
            self.current_button = {
                "id": button_id,
                "text": text,
                "type": "link",
                "disabled": attrs_dict.get("aria-disabled") == "true",
                "selector": selector_from_attrs(tag, attrs_dict, button_id),
            }
            self.current_button_parts = []
        if (tag in FIELD_TAGS and not (tag == "input" and (attrs_dict.get("type") or "").lower() in {"button", "submit"})) or attrs_dict.get("contenteditable", "").lower() == "true":
            field_id = element_id(tag, attrs_dict, len(self.fields))
            kind = field_kind(tag, attrs_dict)
            options: list[str] = []
            if kind == "select":
                self.current_select_id = field_id
            if self.in_label:
                self.current_label_field_ids.append(field_id)
            self.fields.append(
                {
                    "id": field_id,
                    "kind": kind,
                    "label": label_from_attrs(attrs_dict),
                    "required": "required" in attrs_dict or attrs_dict.get("aria-required") == "true",
                    "options": options,
                    "value": attrs_dict.get("value") or None,
                    "disabled": "disabled" in attrs_dict or attrs_dict.get("aria-disabled") == "true",
                    "selector": selector_from_attrs(tag, attrs_dict, field_id),
                }
            )
        if tag == "option" and self.current_select_id:
            self.current_option_parts = []
            value = attrs_dict.get("value")
            if value:
                self._append_option(value)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self.ignored_depth:
            self.ignored_depth -= 1
            return
        if tag == "label":
            label = normalize_space(" ".join(self.current_label_parts))
            if self.current_label_for:
                self.labels_by_for[self.current_label_for] = label
            for field_id in self.current_label_field_ids:
                self.labels_by_for.setdefault(field_id, label)
            self.current_label_for = None
            self.in_label = False
            self.current_label_parts = []
            self.current_label_field_ids = []
        if tag in {"button", "a"} and self.current_button is not None:
            text = normalize_space(" ".join(self.current_button_parts)) or self.current_button["text"]
            if tag == "a" and not ACTION_LINK_RE.search(text):
                self.current_button = None
                self.current_button_parts = []
                return
            self.current_button["text"] = text
            self.buttons.append(self.current_button)
            self.current_button = None
            self.current_button_parts = []
        if tag == "select":
            self.current_select_id = None
        if tag == "option" and self.current_select_id and self.current_option_parts:
            self._append_option(normalize_space(" ".join(self.current_option_parts)))
            self.current_option_parts = []

    def handle_data(self, data: str) -> None:
        if self.ignored_depth:
            return
        text = normalize_space(data)
        if not text:
            return
        self.text_parts.append(text)
        if self.in_label:
            self.current_label_parts.append(text)
        if self.current_button is not None:
            self.current_button_parts.append(text)
        if self.current_select_id:
            self.current_option_parts.append(text)

    def _append_option(self, option: str) -> None:
        for field in self.fields:
            if field["id"] == self.current_select_id:
                if option and option not in field["options"]:
                    field["options"].append(option)
                return


def element_id(tag: str, attrs: dict[str, str], index: int) -> str:
    raw = attrs.get("id") or attrs.get("name") or attrs.get("aria-label") or attrs.get("placeholder") or f"{tag}_{index}"
    return re.sub(r"[^a-zA-Z0-9_:-]+", "_", raw.strip()).strip("_") or f"{tag}_{index}"

def selector_from_attrs(tag: str, attrs: dict[str, str], fallback_id: str) -> str:
    if attrs.get("id"):
        return f"#{attrs['id']}"
    if attrs.get("name"):
        return f"{tag}[name={attrs['name']!r}]"
    if attrs.get("aria-label"):
        return f"{tag}[aria-label={attrs['aria-label']!r}]"
    if attrs.get("placeholder"):
        return f"{tag}[placeholder={attrs['placeholder']!r}]"
    if attrs.get("href"):
        return f"{tag}[href={attrs['href']!r}]"
    return f"#{fallback_id}"


def label_from_attrs(attrs: dict[str, str]) -> str:
    return normalize_space(attrs.get("aria-label") or attrs.get("placeholder") or attrs.get("name") or attrs.get("id"))


def field_kind(tag: str, attrs: dict[str, str]) -> FieldKind:
    if attrs.get("contenteditable", "").lower() == "true":
        return "typeahead"
    if tag == "textarea":
        return "textarea"
    if tag == "select":
        return "select"
    input_type = (attrs.get("type") or "text").lower()
    if input_type == "file":
        return "file"
    if input_type == "checkbox":
        return "checkbox"
    if input_type == "radio":
        return "radio"
    return "text"


def observe_html(html: str, *, url: str = "about:blank") -> PageSnapshot:
    parser = _ApplyFormParser()
    parser.feed(html)
    fields: list[ObservedField] = []
    for raw in parser.fields:
        label = parser.labels_by_for.get(raw["id"]) or raw["label"] or raw["id"]
        fields.append(
            ObservedField(
                id=raw["id"],
                kind=raw["kind"],
                label=label,
                required=raw["required"],
                options=tuple(raw["options"]),
                value=raw["value"],
                disabled=raw["disabled"],
                selector=raw["selector"],
            )
        )
    buttons = tuple(
        ObservedButton(
            id=raw["id"],
            text=raw["text"] or raw["id"],
            type=raw["type"],
            disabled=raw["disabled"],
            final_submit_candidate=is_final_submit_text(raw["text"] or raw["id"]),
            selector=raw["selector"],
        )
        for raw in parser.buttons
    )
    page_text = tuple(dict.fromkeys(part for part in parser.text_parts if ERROR_RE.search(part)))
    blockers = tuple(part for part in page_text if BLOCKER_RE.search(part))
    metadata = {
        "observed_field_count": len(fields),
        "observed_button_count": len(buttons),
        "field_ids": [field.id for field in fields],
        "button_ids": [button.id for button in buttons],
    }
    return PageSnapshot(url=url, fields=tuple(fields), buttons=buttons, errors=page_text, blockers=blockers, metadata=metadata)


def _observe_live_frame(frame: Any, *, frame_name: str) -> PageSnapshot | None:
    if not hasattr(frame, "evaluate"):
        return None
    try:
        raw = frame.evaluate(
            """
            () => {
              const visible = (el) => {
                const style = window.getComputedStyle(el);
                if (style.visibility === 'hidden' || style.display === 'none') return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
              };
              const text = (value) => (value || '').replace(/\\s+/g, ' ').trim();
              const cssEscape = (value) => {
                if (window.CSS && CSS.escape) return CSS.escape(value);
                return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\\\$&');
              };
              const selectorFor = (el, fallback) => {
                if (el.id) return `#${cssEscape(el.id)}`;
                if (el.name) return `${el.tagName.toLowerCase()}[name="${String(el.name).replace(/"/g, '\\\\"')}"]`;
                if (el.getAttribute('aria-label')) return `${el.tagName.toLowerCase()}[aria-label="${el.getAttribute('aria-label').replace(/"/g, '\\\\"')}"]`;
                if (el.getAttribute('placeholder')) return `${el.tagName.toLowerCase()}[placeholder="${el.getAttribute('placeholder').replace(/"/g, '\\\\"')}"]`;
                if (el.getAttribute('href')) return `${el.tagName.toLowerCase()}[href="${el.getAttribute('href').replace(/"/g, '\\\\"')}"]`;
                return `#${cssEscape(fallback)}`;
              };
              const stableId = (el, tag, index) => text(el.id || el.name || el.getAttribute('aria-label') || el.getAttribute('placeholder') || `${tag}_${index}`).replace(/[^a-zA-Z0-9_:-]+/g, '_').replace(/^_+|_+$/g, '') || `${tag}_${index}`;
              const labelledText = (ids) => text((ids || '').split(/\\s+/).map((id) => document.getElementById(id)?.innerText || '').join(' '));
              const contextualLabelFor = (el) => {
                const group = el.closest('[aria-labelledby]');
                if (group) return labelledText(group.getAttribute('aria-labelledby'));
                return '';
              };
              const labelFor = (el) => {
                const parentLabel = contextualLabelFor(el);
                const type = (el.getAttribute('type') || '').toLowerCase();
                const directLabel = () => {
                  if (!el.id) return '';
                  const label = document.querySelector(`label[for="${cssEscape(el.id)}"]`);
                  return label ? text(label.innerText) : '';
                };
                if (type === 'checkbox' || type === 'radio') {
                  const fieldset = el.closest('fieldset');
                  const legend = fieldset ? text(fieldset.querySelector('legend')?.innerText || '') : '';
                  const description = text(el.getAttribute('description') || '');
                  const direct = directLabel();
                  const full = text(`${legend || description} ${direct}`);
                  if (full) return full;
                }
                if (type === 'file' && parentLabel) return parentLabel;
                const direct = directLabel();
                if (direct && !/^(attach|select\\.\\.\\.)$/i.test(direct)) return direct;
                const wrapping = el.closest('label');
                if (wrapping) return text(wrapping.innerText);
                if (el.getAttribute('aria-label')) return text(el.getAttribute('aria-label'));
                if (el.getAttribute('aria-labelledby')) {
                  const direct = labelledText(el.getAttribute('aria-labelledby'));
                  if (direct) return direct;
                }
                const fallback = text(el.getAttribute('placeholder') || el.name || el.id);
                if (parentLabel && (!fallback || /^(attach|select\\.\\.\\.)$/i.test(fallback))) return parentLabel;
                return fallback;
              };
              const requiredFor = (el) => Boolean(el.required || el.getAttribute('aria-required') === 'true' || el.closest('[aria-required="true"]') || /\\*/.test(labelFor(el)));
              const kindFor = (el) => {
                const tag = el.tagName.toLowerCase();
                if (el.isContentEditable || el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') === 'list') return 'typeahead';
                if (tag === 'textarea') return 'textarea';
                if (tag === 'select') return 'select';
                const type = (el.getAttribute('type') || 'text').toLowerCase();
                if (type === 'file') return 'file';
                if (type === 'checkbox') return 'checkbox';
                if (type === 'radio') return 'radio';
                return 'text';
              };
              const fields = [];
              document.querySelectorAll('input, textarea, select, [contenteditable="true"]').forEach((el) => {
                const tag = el.tagName.toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                if (tag === 'input' && ['button', 'submit', 'hidden'].includes(type)) return;
                if (el.getAttribute('aria-hidden') === 'true') return;
                if (!visible(el) && type !== 'file') return;
                const id = stableId(el, tag, fields.length);
                fields.push({
                  id,
                  kind: kindFor(el),
                  label: labelFor(el) || id,
                  required: requiredFor(el),
                  options: tag === 'select' ? Array.from(el.options).map((option) => text(option.value || option.text)).filter(Boolean) : [],
                  value: typeof el.value === 'string' ? el.value : null,
                  disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
                  selector: selectorFor(el, id),
                });
              });
              const buttons = [];
              document.querySelectorAll('button, input[type="button"], input[type="submit"], a[href]').forEach((el) => {
                if (!visible(el)) return;
                const tag = el.tagName.toLowerCase();
                const type = (el.getAttribute('type') || (tag === 'a' ? 'link' : '') || null);
                const buttonText = text(el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '');
                if (tag === 'a' && !/\\b(apply|next|continue|submit|review)\\b/i.test(buttonText)) return;
                const id = stableId(el, tag, buttons.length);
                buttons.push({
                  id,
                  text: buttonText || id,
                  type,
                  disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
                  selector: selectorFor(el, id),
                });
              });
              const bodyText = text(document.body ? document.body.innerText : '');
              return { fields, buttons, bodyText };
            }
            """
        )
    except Exception:
        return None
    fields = tuple(
        ObservedField(
            id=str(item["id"]),
            kind=item["kind"],
            label=str(item["label"]),
            required=bool(item["required"]),
            options=tuple(str(option) for option in item.get("options", ())),
            value=item.get("value"),
            disabled=bool(item["disabled"]),
            visible=True,
            frame=frame_name,
            selector=str(item["selector"]),
        )
        for item in raw.get("fields", ())
    )
    buttons = tuple(
        ObservedButton(
            id=str(item["id"]),
            text=str(item["text"]),
            type=item.get("type"),
            disabled=bool(item["disabled"]),
            final_submit_candidate=is_final_submit_text(str(item["text"])),
            visible=True,
            frame=frame_name,
            selector=str(item["selector"]),
        )
        for item in raw.get("buttons", ())
    )
    body_parts = tuple(dict.fromkeys(part for part in (raw.get("bodyText") or "").split(". ") if ERROR_RE.search(part)))
    blockers = tuple(part for part in body_parts if BLOCKER_RE.search(part))
    return PageSnapshot(url=str(getattr(frame, "url", "about:blank")), fields=fields, buttons=buttons, errors=body_parts, blockers=blockers)


def observe_page(page: Any) -> PageSnapshot:
    frames = list(getattr(page, "frames", []) or [])
    if not frames:
        frames = [page]
    live_snapshots: list[PageSnapshot] = []
    for index, frame in enumerate(frames):
        snapshot = _observe_live_frame(frame, frame_name=f"frame_{index}")
        if snapshot is not None:
            live_snapshots.append(snapshot)
    if live_snapshots:
        fields = tuple(field for snapshot in live_snapshots for field in snapshot.fields)
        buttons = tuple(button for snapshot in live_snapshots for button in snapshot.buttons)
        errors = tuple(dict.fromkeys(error for snapshot in live_snapshots for error in snapshot.errors))
        blockers = tuple(dict.fromkeys(blocker for snapshot in live_snapshots for blocker in snapshot.blockers))
        metadata = {
            "observed_field_count": len(fields),
            "observed_button_count": len(buttons),
            "field_ids": [field.id for field in fields],
            "button_ids": [button.id for button in buttons],
            "frames_observed": len(live_snapshots),
        }
        return PageSnapshot(url=str(getattr(page, "url", "about:blank")), fields=fields, buttons=buttons, errors=errors, blockers=blockers, metadata=metadata)

    documents = [page.content()]
    main_frame = getattr(page, "main_frame", None)
    for frame in getattr(page, "frames", []) or []:
        if frame is page or frame is main_frame:
            continue
        try:
            documents.append(frame.content())
        except Exception:
            continue
    snapshots = [observe_html(document, url=str(getattr(page, "url", "about:blank"))) for document in documents]
    fields = tuple(field for snapshot in snapshots for field in snapshot.fields)
    buttons = tuple(button for snapshot in snapshots for button in snapshot.buttons)
    errors = tuple(dict.fromkeys(error for snapshot in snapshots for error in snapshot.errors))
    blockers = tuple(dict.fromkeys(blocker for snapshot in snapshots for blocker in snapshot.blockers))
    metadata = {
        "observed_field_count": len(fields),
        "observed_button_count": len(buttons),
        "field_ids": [field.id for field in fields],
        "button_ids": [button.id for button in buttons],
        "frames_observed": len(snapshots),
    }
    return PageSnapshot(url=str(getattr(page, "url", "about:blank")), fields=fields, buttons=buttons, errors=errors, blockers=blockers, metadata=metadata)
