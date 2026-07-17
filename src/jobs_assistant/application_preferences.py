from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from .ats import SUPPORTED_ATS, _canonicalize_select_value, validate_answer_value
from .contracts import FieldAnswer, FieldValue, ObservedField, ObservedOption
from .safety import DescriptorSafety, classify_descriptors

APPLICATION_PREFERENCES_SCHEMA_VERSION = 1
MAX_PREFERENCES_BYTES = 256 * 1024
MAX_PREFERENCES_DEPTH = 8
MAX_PREFERENCES_NODES = 2_000
MAX_PREFERENCES_STRING_CHARS = 20_000
MAX_PREFERENCES_MAPPINGS = 500
MAX_PREFERENCES_OPTOUTS = 500
MAX_PREFERENCES_REVIEW_ORDER = 64

SAFE_FIELD_KINDS = frozenset(
    {"text", "email", "tel", "url", "number", "date", "textarea", "select", "checkbox", "radio"}
)
_BLOCKED_KINDS = frozenset({"file", "password", "hidden", "button", "submit", "reset", "final", "opaque"})
_SAFE_ATS = frozenset(("*", *SUPPORTED_ATS))
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class PreferenceValidationError(ValueError):
    """Raised when a preference document or observed action is unsafe."""


@dataclass(frozen=True)
class PreferenceMatcher:
    """An exact, safe field matcher.

    ``ats='*'`` is a safe ATS wildcard, but a matcher must still carry an exact
    name or label.  Kind is always required and is never a wildcard.
    """

    ats: str
    name: str | None
    label: str | None
    kind: str

    def __post_init__(self) -> None:
        ats, name, label, kind = _validate_matcher(self.ats, self.name, self.label, self.kind)
        object.__setattr__(self, "ats", ats)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "kind", kind)

    @property
    def matcher_id(self) -> str:
        """Return a stable identity for this exact matcher."""

        return _matcher_id(self)


@dataclass(frozen=True)
class PreferenceMapping:
    """A validated safe value mapping, shaped like ``ConfiguredFieldAnswer``."""

    ats: str
    name: str | None
    label: str | None
    kind: str
    value: FieldValue

    def __post_init__(self) -> None:
        ats, name, label, kind = _validate_matcher(self.ats, self.name, self.label, self.kind)
        _validate_scalar_value(self.value, kind)
        value: FieldValue = self.value
        if isinstance(value, (list, tuple)):
            value = tuple(value)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "ats", ats)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "kind", kind)

    @property
    def matcher(self) -> PreferenceMatcher:
        return PreferenceMatcher(self.ats, self.name, self.label, self.kind)

    @property
    def matcher_id(self) -> str:
        return self.matcher.matcher_id


@dataclass(frozen=True)
class PreferenceOptOut:
    """An exact safe matcher whose field is intentionally not automated."""

    ats: str
    name: str | None
    label: str | None
    kind: str

    def __post_init__(self) -> None:
        ats, name, label, kind = _validate_matcher(self.ats, self.name, self.label, self.kind)
        object.__setattr__(self, "ats", ats)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "kind", kind)

    @property
    def matcher(self) -> PreferenceMatcher:
        return PreferenceMatcher(self.ats, self.name, self.label, self.kind)

    @property
    def matcher_id(self) -> str:
        return self.matcher.matcher_id


@dataclass(frozen=True)
class ApplicationPreferences:
    """The immutable, versioned preference document used by an application run."""

    schema_version: int
    mappings: tuple[PreferenceMapping, ...]
    opt_outs: tuple[PreferenceOptOut, ...]
    review_order: tuple[PreferenceMatcher, ...]
    source_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.source_sha256 is not None and (
            type(self.source_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.source_sha256) is None
        ):
            raise PreferenceValidationError("application preferences source_sha256 is invalid")
        if type(self.schema_version) is not int or self.schema_version != APPLICATION_PREFERENCES_SCHEMA_VERSION:
            raise PreferenceValidationError("unsupported application preferences schema version")
        mappings = tuple(self.mappings)
        opt_outs = tuple(self.opt_outs)
        review_order = tuple(self.review_order)
        if len(mappings) > MAX_PREFERENCES_MAPPINGS:
            raise PreferenceValidationError("too many preference mappings")
        if len(opt_outs) > MAX_PREFERENCES_OPTOUTS:
            raise PreferenceValidationError("too many preference opt-outs")
        if len(review_order) > MAX_PREFERENCES_REVIEW_ORDER:
            raise PreferenceValidationError("review order exceeds cap")
        if not all(isinstance(item, PreferenceMapping) for item in mappings):
            raise PreferenceValidationError("mappings must contain PreferenceMapping values")
        if not all(isinstance(item, PreferenceOptOut) for item in opt_outs):
            raise PreferenceValidationError("opt_outs must contain PreferenceOptOut values")
        if not all(isinstance(item, PreferenceMatcher) for item in review_order):
            raise PreferenceValidationError("review_order must contain exact matcher objects")

        mapping_keys = [item.matcher_id for item in mappings]
        if len(set(mapping_keys)) != len(mapping_keys):
            raise PreferenceValidationError("duplicate or conflicting preference mappings")
        optout_keys = [item.matcher_id for item in opt_outs]
        if len(set(optout_keys)) != len(optout_keys):
            raise PreferenceValidationError("duplicate preference opt-outs")
        if set(mapping_keys) & set(optout_keys):
            raise PreferenceValidationError("mapping conflicts with opt-out")
        order_keys = [item.matcher_id for item in review_order]
        if len(set(order_keys)) != len(order_keys):
            raise PreferenceValidationError("duplicate review-order matcher")
        object.__setattr__(self, "mappings", mappings)
        object.__setattr__(self, "opt_outs", opt_outs)
        object.__setattr__(self, "review_order", review_order)

    @property
    def version(self) -> int:
        return self.schema_version


@dataclass(frozen=True)
class ObservedFieldDescriptor:
    """Bounded normalized view of an observed field used for preference matching."""

    target_id: str
    ats: str
    name: str | None
    label: str
    kind: str
    required: bool
    safety_descriptors: tuple[str, ...] = ()
    options: tuple[tuple[str, str, bool], ...] = ()
    multiple: bool = False

    def __post_init__(self) -> None:
        if type(self.target_id) is not str or not self.target_id or len(self.target_id) > 512:
            raise PreferenceValidationError("field target_id is invalid")
        ats, name, label, kind = _validate_observed_identity(self.ats, self.name, self.label, self.kind)
        if type(self.required) is not bool:
            raise PreferenceValidationError("field required flag is invalid")
        if type(self.multiple) is not bool:
            raise PreferenceValidationError("field multiple flag is invalid")
        if type(self.options) not in (tuple, list):
            raise PreferenceValidationError("field options are invalid")
        descriptors = tuple(self.safety_descriptors)
        normalized_options: list[tuple[str, str, bool]] = []
        for item in self.options:
            if isinstance(item, ObservedOption):
                if type(item.value) is not str or type(item.label) is not str or type(item.enabled) is not bool:
                    raise PreferenceValidationError("field options are invalid")
                normalized_options.append((item.value, item.label, item.enabled))
                continue
            if isinstance(item, (tuple, list)):
                if len(item) == 2:
                    if self.multiple:
                        raise PreferenceValidationError("multi-select descriptor requires option enabled triples")
                    value, option_label = item
                    if type(value) is str and type(option_label) is str:
                        normalized_options.append((value, option_label, True))
                        continue
                if len(item) == 3:
                    value, option_label, enabled = item
                    if type(value) is str and type(option_label) is str and type(enabled) is bool:
                        normalized_options.append((value, option_label, enabled))
                        continue
            raise PreferenceValidationError("field options are invalid")
        options = tuple(normalized_options)
        try:
            pair_options = tuple((option[0], option[1]) for option in options)
            safety = classify_descriptors(descriptors, field_kind=kind, options=pair_options)
        except Exception as exc:
            raise PreferenceValidationError("field descriptors exceed safety limits") from exc
        if safety is not DescriptorSafety.SAFE:
            raise PreferenceValidationError("field descriptors are sensitive")
        object.__setattr__(self, "ats", ats)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "safety_descriptors", descriptors)
        object.__setattr__(self, "options", options)
        object.__setattr__(self, "multiple", self.multiple)

    @property
    def matcher(self) -> PreferenceMatcher:
        return PreferenceMatcher(self.ats, self.name, self.label or None, self.kind)




@dataclass(frozen=True)
class OptOutStatus:
    target_id: str
    status: Literal["manual", "skipped"]


@dataclass(frozen=True)
class PreferencePriority:
    target_id: str
    priority: int


@dataclass(frozen=True)
class PreferenceApplicationResult:
    """Pure preference output containing no executable action payloads."""

    selected_answers: tuple[FieldAnswer, ...]
    opted_out: tuple[OptOutStatus, ...]
    ordered_target_ids: tuple[str, ...]
    priorities: tuple[PreferencePriority, ...]

    @property
    def manual_target_ids(self) -> tuple[str, ...]:
        return tuple(item.target_id for item in self.opted_out if item.status == "manual")

    @property
    def skipped_target_ids(self) -> tuple[str, ...]:
        return tuple(item.target_id for item in self.opted_out if item.status == "skipped")

    @property
    def answers(self) -> tuple[FieldAnswer, ...]:
        return self.selected_answers


def load_application_preferences(path: str | Path | None, *, cwd: str | Path) -> ApplicationPreferences:
    """Load one owned, regular, bounded JSON preference file beneath ``cwd``."""

    if path is None:
        return ApplicationPreferences(APPLICATION_PREFERENCES_SCHEMA_VERSION, (), (), ())

    root = _secure_root(cwd)
    target = _secure_target(path, root)
    fd = _open_preference_file(target)
    try:
        st = os.fstat(fd)
        if st.st_size > MAX_PREFERENCES_BYTES:
            raise PreferenceValidationError("application preferences exceeds its size cap")
        raw = _read_snapshot(fd, st.st_size)
        source_sha256 = hashlib.sha256(raw).hexdigest()
    finally:
        os.close(fd)
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PreferenceValidationError("malformed application preferences JSON") from exc
    _validate_json_caps(payload)
    if not isinstance(payload, dict):
        raise PreferenceValidationError("application preferences must contain an object")
    expected = {"schema_version", "mappings", "opt_outs", "review_order"}
    if set(payload) != expected:
        raise PreferenceValidationError("application preferences contain unknown or missing keys")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != APPLICATION_PREFERENCES_SCHEMA_VERSION:
        raise PreferenceValidationError("unsupported application preferences schema version")

    raw_mappings = payload["mappings"]
    raw_optouts = payload["opt_outs"]
    raw_order = payload["review_order"]
    if not isinstance(raw_mappings, list) or not isinstance(raw_optouts, list) or not isinstance(raw_order, list):
        raise PreferenceValidationError("mappings, opt_outs, and review_order must be arrays")
    if len(raw_mappings) > MAX_PREFERENCES_MAPPINGS:
        raise PreferenceValidationError("too many preference mappings")
    if len(raw_optouts) > MAX_PREFERENCES_OPTOUTS:
        raise PreferenceValidationError("too many preference opt-outs")
    if len(raw_order) > MAX_PREFERENCES_REVIEW_ORDER:
        raise PreferenceValidationError("review order exceeds cap")

    mappings = tuple(_parse_mapping(item) for item in raw_mappings)
    opt_outs = tuple(_parse_optout(item) for item in raw_optouts)
    review_order = tuple(_parse_review_matcher(item) for item in raw_order)
    return ApplicationPreferences(
        schema_version=APPLICATION_PREFERENCES_SCHEMA_VERSION,
        mappings=mappings,
        opt_outs=opt_outs,
        review_order=review_order,
        source_sha256=source_sha256,
    )


def matching_mapping(
    preferences: ApplicationPreferences,
    field: ObservedField | ObservedFieldDescriptor | Mapping[str, Any],
    *,
    ats: str = "greenhouse",
) -> PreferenceMapping | None:
    """Select one ATS-specific mapping and validate its value against ``field``.

    A value that fails the observed field's complete ATS validator is never
    returned.  ``None`` means no safe answer is authorized.
    """

    if not isinstance(preferences, ApplicationPreferences):
        raise PreferenceValidationError("invalid application preferences")
    descriptor = normalize_field_descriptor(field, ats=ats)
    candidates = [item for item in preferences.mappings if _matcher_matches(item.matcher, descriptor)]
    if not candidates:
        return None
    specific = [item for item in candidates if item.ats != "*"]
    selected = specific or candidates
    if len(selected) != 1:
        raise PreferenceValidationError("conflicting mappings match one observed field")
    mapping = selected[0]
    validation_field = field if isinstance(field, ObservedField) else _descriptor_as_observed_field(descriptor)
    if not validate_answer_value(validation_field, mapping.value, kind=mapping.kind):
        return None
    return mapping


def mapping_answer(
    preferences: ApplicationPreferences,
    field: ObservedField | ObservedFieldDescriptor | Mapping[str, Any],
    *,
    ats: str = "greenhouse",
) -> FieldAnswer | None:
    """Return a validated configured answer, or no answer for an unsafe value."""

    descriptor = normalize_field_descriptor(field, ats=ats)
    mapping = matching_mapping(preferences, field, ats=descriptor.ats)
    if mapping is None:
        return None
    value: FieldValue = mapping.value
    if mapping.kind == "select":
        validation_field = field if isinstance(field, ObservedField) else _descriptor_as_observed_field(descriptor)
        canonical = _canonicalize_select_value(validation_field, mapping.value)
        if canonical is None:
            return None
        value = canonical
    return FieldAnswer(descriptor.target_id, value, 1.0, "application preference", "configured")


def normalize_field_descriptor(
    field: ObservedField | ObservedFieldDescriptor | Mapping[str, Any],
    *,
    ats: str = "greenhouse",
) -> ObservedFieldDescriptor:
    """Normalize an observed field without weakening its safety descriptors."""

    if isinstance(field, ObservedFieldDescriptor):
        return field
    if isinstance(field, ObservedField):
        options = tuple((option.value, option.label, option.enabled) for option in field.options)
        return ObservedFieldDescriptor(
            field.target_id,
            ats,
            field.name,
            field.label,
            field.kind,
            field.required,
            tuple(field.safety_descriptors),
            options,
            field.multiple,
        )
    if isinstance(field, Mapping):
        target_id = field.get("target_id")
        raw_options = field.get("options", ())
        if type(raw_options) not in (list, tuple):
            raise PreferenceValidationError("field options are invalid")
        normalized_options: list[Any] = []
        for item in raw_options:
            if isinstance(item, ObservedOption):
                normalized_options.append(item)
            elif isinstance(item, (tuple, list)):
                normalized_options.append(tuple(item))
            else:
                raise PreferenceValidationError("field options are invalid")
        return ObservedFieldDescriptor(
            target_id,
            field.get("ats", ats),
            field.get("name"),
            field.get("label", ""),
            field.get("kind", ""),
            field.get("required", False),
            tuple(field.get("safety_descriptors", ())),
            tuple(normalized_options),
            field.get("multiple", False),
        )
    raise PreferenceValidationError("unsupported observed field descriptor")

def _descriptor_as_observed_field(descriptor: ObservedFieldDescriptor) -> ObservedField:
    """Materialize a descriptor for the shared complete answer validator."""
    options: list[ObservedOption] = []
    for item in descriptor.options:
        if isinstance(item, ObservedOption):
            if type(item.value) is not str or type(item.label) is not str or type(item.enabled) is not bool:
                raise PreferenceValidationError("field options are invalid")
            options.append(item)
            continue
        if isinstance(item, (tuple, list)) and len(item) == 3:
            value, label, enabled = item
            if type(value) is str and type(label) is str and type(enabled) is bool:
                options.append(ObservedOption(value, label, enabled))
                continue
        if isinstance(item, (tuple, list)) and len(item) == 2:
            if descriptor.multiple:
                raise PreferenceValidationError("multi-select descriptor requires option enabled triples")
            value, label = item
            if type(value) is str and type(label) is str:
                options.append(ObservedOption(value, label, True))
                continue
        raise PreferenceValidationError("field options are invalid")
    return ObservedField(
        target_id=descriptor.target_id,
        field_key=descriptor.target_id,
        frame_id="",
        frame_url="",
        form_action_url=None,
        kind=descriptor.kind,
        name=descriptor.name,
        label=descriptor.label,
        group_id=None,
        option_value=None,
        safety_descriptors=descriptor.safety_descriptors,
        selector="",
        required=descriptor.required,
        visible=True,
        enabled=True,
        readonly=False,
        value=None,
        will_validate=True,
        valid=True,
        validity_flags=(),
        file_count=0,
        file_basenames=(),
        accept=(),
        min_length=None,
        max_length=None,
        pattern=None,
        min_value=None,
        max_value=None,
        step=None,
        options=tuple(options),
        multiple=descriptor.multiple,
    )


def apply_preferences(
    preferences: ApplicationPreferences,
    observed_fields: Sequence[ObservedField | ObservedFieldDescriptor | Mapping[str, Any]],
    answers: Sequence[FieldAnswer] | None = None,
    *,
    ats: str = "greenhouse",
) -> PreferenceApplicationResult:
    """Apply opt-outs/mappings while returning only IDs, statuses, and priorities."""

    if not isinstance(preferences, ApplicationPreferences):
        raise PreferenceValidationError("invalid application preferences")
    descriptors = tuple(normalize_field_descriptor(item, ats=ats) for item in observed_fields)
    _reject_duplicate_targets(descriptors)
    opted: list[OptOutStatus] = []
    retained: list[ObservedFieldDescriptor] = []
    opted_ids: set[str] = set()
    for descriptor in descriptors:
        if any(_matcher_matches(item.matcher, descriptor) for item in preferences.opt_outs):
            status: Literal["manual", "skipped"] = "manual" if descriptor.required else "skipped"
            opted.append(OptOutStatus(descriptor.target_id, status))
            opted_ids.add(descriptor.target_id)
        else:
            retained.append(descriptor)

    selected: list[FieldAnswer] = []
    broadcasted: set[str] = set()
    for mapping in preferences.mappings:
        matched = [descriptor for descriptor in retained if _matcher_matches(mapping.matcher, descriptor)]
        if len(matched) > 1:
            broadcasted.update(item.target_id for item in matched)
    for descriptor in retained:
        if descriptor.target_id in broadcasted:
            continue
        answer = mapping_answer(preferences, descriptor, ats=descriptor.ats)
        if answer is not None and answer.target_id not in opted_ids:
            selected.append(answer)

    existing = tuple(answers or ())
    if not all(isinstance(item, FieldAnswer) for item in existing):
        raise PreferenceValidationError("answers must contain FieldAnswer values")
    _reject_duplicate_targets(existing)
    existing = tuple(item for item in existing if item.target_id not in opted_ids)
    by_target = {item.target_id: item for item in existing}
    for answer in selected:
        by_target.setdefault(answer.target_id, answer)
    selected = list(by_target.values())
    order_input: Sequence[Any] = existing if answers is not None else retained
    ordered_ids = order_actions(preferences, order_input, descriptors=retained, ats=ats)
    priorities = _priorities(preferences, ordered_ids, retained)
    return PreferenceApplicationResult(tuple(selected), tuple(opted), ordered_ids, priorities)


def order_actions(
    preferences: ApplicationPreferences,
    actions: Sequence[ObservedField | ObservedFieldDescriptor | FieldAnswer | str | Mapping[str, Any]],
    *,
    descriptors: Sequence[ObservedField | ObservedFieldDescriptor | Mapping[str, Any]] | Mapping[str, Any] | None = None,
    ats: str = "greenhouse",
) -> tuple[str, ...]:
    """Stable-sort existing action references and return target IDs only."""

    if not isinstance(preferences, ApplicationPreferences):
        raise PreferenceValidationError("invalid application preferences")
    lookup = _descriptor_lookup(descriptors, ats=ats)
    entries: list[tuple[str, ObservedFieldDescriptor | None, int]] = []
    for index, item in enumerate(actions):
        if isinstance(item, ObservedField):
            descriptor = normalize_field_descriptor(item, ats=ats)
            target_id = descriptor.target_id
        elif isinstance(item, ObservedFieldDescriptor):
            descriptor = item
            target_id = item.target_id
        elif isinstance(item, FieldAnswer):
            target_id = item.target_id
            descriptor = lookup.get(target_id)
        elif isinstance(item, str):
            target_id = item
            descriptor = lookup.get(target_id)
        elif isinstance(item, Mapping):
            if "value" in item:
                raise PreferenceValidationError("action values must stay in AutofillPlan")
            target_id = item.get("target_id")
            if not isinstance(target_id, str):
                raise PreferenceValidationError("action target_id is invalid")
            descriptor = lookup.get(target_id)
            if {"kind", "name", "label"} & set(item):
                descriptor = normalize_field_descriptor(item, ats=ats)
        else:
            raise PreferenceValidationError("unsupported action reference")
        if not isinstance(target_id, str) or not target_id:
            raise PreferenceValidationError("action target_id is invalid")
        entries.append((target_id, descriptor, index))
    if len({item[0] for item in entries}) != len(entries):
        raise PreferenceValidationError("duplicate field target identity")
    fallback = len(preferences.review_order)
    ranked: list[tuple[int, int, str]] = []
    for target_id, descriptor, index in entries:
        rank = fallback
        if descriptor is not None:
            for order_index, matcher in enumerate(preferences.review_order):
                if _matcher_matches(matcher, descriptor):
                    rank = order_index
                    break
        ranked.append((rank, index, target_id))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in ranked)


def _descriptor_lookup(
    descriptors: Sequence[ObservedField | ObservedFieldDescriptor | Mapping[str, Any]] | Mapping[str, Any] | None,
    *,
    ats: str = "greenhouse",
) -> dict[str, ObservedFieldDescriptor]:
    if descriptors is None:
        return {}
    values = descriptors.values() if isinstance(descriptors, Mapping) else descriptors
    result: dict[str, ObservedFieldDescriptor] = {}
    for item in values:
        descriptor = normalize_field_descriptor(item, ats=ats)
        if descriptor.target_id in result:
            raise PreferenceValidationError("duplicate field target identity")
        result[descriptor.target_id] = descriptor
    return result


def _priorities(
    preferences: ApplicationPreferences,
    target_ids: Sequence[str],
    descriptors: Sequence[ObservedFieldDescriptor],
) -> tuple[PreferencePriority, ...]:
    lookup = {item.target_id: item for item in descriptors}
    fallback = len(preferences.review_order)
    result: list[PreferencePriority] = []
    for target_id in target_ids:
        descriptor = lookup.get(target_id)
        rank = fallback
        if descriptor is not None:
            for index, matcher in enumerate(preferences.review_order):
                if _matcher_matches(matcher, descriptor):
                    rank = index
                    break
        result.append(PreferencePriority(target_id, rank))
    return tuple(result)




def _validate_matcher(
    ats: Any,
    name: Any,
    label: Any,
    kind: Any,
) -> tuple[str, str | None, str | None, str]:
    if type(ats) is not str or ats not in _SAFE_ATS:
        raise PreferenceValidationError("matcher ats must be greenhouse or *")
    if type(kind) is not str:
        raise PreferenceValidationError("matcher kind must be a string")
    normalized_kind = kind.strip().lower()
    if normalized_kind not in SAFE_FIELD_KINDS:
        if normalized_kind in _BLOCKED_KINDS:
            raise PreferenceValidationError("sensitive, final, or opaque field kind is forbidden")
        raise PreferenceValidationError("matcher kind is unsupported")
    normalized_name = _normalize_match_text(name, "name")
    normalized_label = _normalize_match_text(label, "label")
    if normalized_name is None and normalized_label is None:
        raise PreferenceValidationError("wildcard-only matcher is forbidden")
    if normalized_name is not None and _opaque_matcher_text("name", normalized_name):
        raise PreferenceValidationError("opaque matcher is forbidden")
    if normalized_label is not None and _opaque_matcher_text("label", normalized_label):
        raise PreferenceValidationError("opaque matcher is forbidden")
    try:
        safety = classify_descriptors(tuple(item for item in (normalized_name, normalized_label) if item), field_kind=normalized_kind)
    except Exception as exc:
        raise PreferenceValidationError("matcher descriptors exceed safety limits") from exc
    if safety is not DescriptorSafety.SAFE:
        raise PreferenceValidationError("sensitive matcher descriptor is forbidden")
    return ats, normalized_name, normalized_label, normalized_kind


def _validate_observed_identity(ats: Any, name: Any, label: Any, kind: Any) -> tuple[str, str | None, str, str]:
    if type(ats) is not str or ats not in _SAFE_ATS:
        raise PreferenceValidationError("observed field ats is unsupported")
    if type(kind) is not str:
        raise PreferenceValidationError("observed field kind is invalid")
    normalized_kind = kind.strip().lower()
    if normalized_kind not in SAFE_FIELD_KINDS:
        raise PreferenceValidationError("observed field is not a safe field kind")
    normalized_name = _normalize_match_text(name, "name")
    normalized_label = _normalize_match_text(label, "label") or ""
    if normalized_name is not None and _opaque_matcher_text("name", normalized_name):
        raise PreferenceValidationError("opaque observed field matcher")
    if normalized_label and _opaque_matcher_text("label", normalized_label):
        raise PreferenceValidationError("opaque observed field matcher")
    try:
        safety = classify_descriptors(
            tuple(item for item in (normalized_name, normalized_label) if item),
            field_kind=normalized_kind,
        )
    except Exception as exc:
        raise PreferenceValidationError("observed field descriptors exceed safety limits") from exc
    if safety is not DescriptorSafety.SAFE:
        raise PreferenceValidationError("sensitive observed field descriptor")
    return ats, normalized_name, normalized_label, normalized_kind


def _normalize_match_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise PreferenceValidationError(f"matcher {field_name} must be a string or null")
    normalized = " ".join(unicodedata.normalize("NFKC", value).strip().lower().split())
    if not normalized:
        return None
    if len(normalized) > 512:
        raise PreferenceValidationError(f"matcher {field_name} is too long")
    if any(char in normalized for char in "\x00\r\n\t"):
        raise PreferenceValidationError(f"matcher {field_name} contains forbidden controls")
    return normalized


def _opaque_matcher_text(kind: str, raw: str) -> bool:
    if kind == "name":
        if re.fullmatch(r"(?:question|field|input|custom field)[ _-][0-9]+", raw):
            return True
        if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", raw):
            return True
        if re.fullmatch(r"job_application\[answers_attributes\]\[[0-9]+\]\[(?:text_value|answer_value|boolean_value)\]", raw):
            return True
    if kind == "label" and re.fullmatch(r"(?:question|field|input)(?: [0-9]+)?", raw):
        return True
    return False


def _validate_scalar_value(value: Any, kind: str) -> None:
    if kind in {"checkbox", "radio"}:
        if type(value) is not bool:
            raise PreferenceValidationError("checkbox/radio mappings require boolean values")
        return
    if kind == "select":
        if isinstance(value, (list, tuple)):
            for item in value:
                if type(item) is not str or not item:
                    raise PreferenceValidationError("select mapping list values must be non-empty strings")
                if len(item) > MAX_PREFERENCES_STRING_CHARS:
                    raise PreferenceValidationError("select mapping value is too long")
                if any(unicodedata.category(char) in {"Cc", "Cf"} for char in item):
                    raise PreferenceValidationError("select mapping value contains forbidden controls")
            try:
                safety = classify_descriptors(tuple(value), field_kind=kind)
            except Exception as exc:
                raise PreferenceValidationError("mapping value exceeds safety limits") from exc
            if safety is not DescriptorSafety.SAFE:
                raise PreferenceValidationError("sensitive mapping value is forbidden")
            return
    if type(value) is not str:
        raise PreferenceValidationError("safe mappings require string scalar values")
    if not value or len(value) > MAX_PREFERENCES_STRING_CHARS:
        raise PreferenceValidationError("mapping value is empty or too long")
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        raise PreferenceValidationError("mapping value contains forbidden controls")
    try:
        safety = classify_descriptors((value,), field_kind=kind)
    except Exception as exc:
        raise PreferenceValidationError("mapping value exceeds safety limits") from exc
    if safety is not DescriptorSafety.SAFE:
        raise PreferenceValidationError("sensitive mapping value is forbidden")




def _matcher_id(matcher: PreferenceMatcher) -> str:
    return "|".join(
        (
            matcher.ats,
            matcher.kind,
            f"name={matcher.name or ''}",
            f"label={matcher.label or ''}",
        )
    )


def _matcher_matches(matcher: PreferenceMatcher, field: ObservedFieldDescriptor) -> bool:
    if matcher.ats != "*" and matcher.ats != field.ats:
        return False
    if matcher.kind != field.kind:
        return False
    if matcher.name is not None and matcher.name != field.name:
        return False
    if matcher.label is not None and matcher.label != (field.label or None):
        return False
    return True




def _reject_duplicate_targets(items: Sequence[Any]) -> None:
    target_ids = [item.target_id for item in items]
    if len(set(target_ids)) != len(target_ids):
        raise PreferenceValidationError("duplicate field target identity")


def _parse_mapping(raw: Any) -> PreferenceMapping:
    if not isinstance(raw, dict):
        raise PreferenceValidationError("mapping entries must be objects")
    allowed = {"ats", "name", "label", "kind", "value"}
    if set(raw) - allowed or not {"ats", "kind", "value"} <= set(raw):
        raise PreferenceValidationError("mapping contains unknown or missing keys")
    return PreferenceMapping(raw["ats"], raw.get("name"), raw.get("label"), raw["kind"], raw["value"])


def _parse_optout(raw: Any) -> PreferenceOptOut:
    if not isinstance(raw, dict):
        raise PreferenceValidationError("opt_out entries must be objects")
    allowed = {"ats", "name", "label", "kind"}
    if set(raw) - allowed or not {"ats", "kind"} <= set(raw):
        raise PreferenceValidationError("opt_out contains unknown or missing keys")
    return PreferenceOptOut(raw["ats"], raw.get("name"), raw.get("label"), raw["kind"])


def _parse_review_matcher(raw: Any) -> PreferenceMatcher:
    if not isinstance(raw, dict):
        raise PreferenceValidationError("review_order entries must be exact matcher objects")
    allowed = {"ats", "name", "label", "kind"}
    if set(raw) - allowed or not {"ats", "kind"} <= set(raw):
        raise PreferenceValidationError("review_order matcher contains unknown or missing keys")
    return PreferenceMatcher(raw["ats"], raw.get("name"), raw.get("label"), raw["kind"])


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PreferenceValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise PreferenceValidationError(f"non-finite JSON number: {value}")


def _validate_json_caps(value: Any) -> None:
    nodes = 0

    def walk(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_PREFERENCES_NODES:
            raise PreferenceValidationError("preferences JSON exceeds node cap")
        if depth > MAX_PREFERENCES_DEPTH:
            raise PreferenceValidationError("preferences JSON exceeds depth cap")
        if isinstance(item, str):
            if len(item) > MAX_PREFERENCES_STRING_CHARS:
                raise PreferenceValidationError("preferences JSON string exceeds cap")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise PreferenceValidationError("preferences JSON contains non-finite number")
        elif isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > MAX_PREFERENCES_STRING_CHARS:
                    raise PreferenceValidationError("preferences JSON object key is invalid")
                walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
        elif item is None or isinstance(item, bool | int):
            return
        else:
            raise PreferenceValidationError("preferences JSON contains unsupported value")

    walk(value, 0)


def _secure_root(cwd: str | Path) -> Path:
    root = Path(cwd)
    if any(part in {"", ".", ".."} for part in root.parts if part not in {root.anchor}):
        raise PreferenceValidationError("unsafe preferences cwd")
    try:
        # Keep the caller's lexical spelling (notably /var vs /private/var on
        # macOS) so a target passed with the same spelling remains beneath cwd.
        root = root.absolute()
        st = root.stat()
    except OSError as exc:
        raise PreferenceValidationError("preferences cwd is unavailable") from exc
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        raise PreferenceValidationError("preferences cwd is not a private directory")
    return root


def _secure_target(path: str | Path, root: Path) -> Path:
    raw = os.fspath(path)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise PreferenceValidationError("invalid preferences path")
    parsed = Path(raw)
    if any(part in {"", ".", ".."} for part in parsed.parts if part not in {parsed.anchor}):
        raise PreferenceValidationError("preferences path traversal is forbidden")
    target = parsed if parsed.is_absolute() else root / parsed
    try:
        lexical = target.absolute()
        lexical.relative_to(root)
    except ValueError as exc:
        raise PreferenceValidationError("preferences path escapes cwd") from exc
    # Reject symlinked ancestors before opening the final component. O_NOFOLLOW
    # below closes the final-component symlink race as well as rejecting it.
    try:
        rel = lexical.relative_to(root)
    except ValueError as exc:
        raise PreferenceValidationError("preferences path escapes cwd") from exc
    current = root
    for component in rel.parts:
        current = current / component
        if current.is_symlink():
            raise PreferenceValidationError("preferences path contains a symlink")
    return lexical


def _open_preference_file(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise PreferenceValidationError("preferences file must not be a symlink") from exc
        raise PreferenceValidationError("unable to open preferences file") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PreferenceValidationError("preferences file must be regular")
        if st.st_uid != os.geteuid():
            raise PreferenceValidationError("preferences file must be owned by effective user")
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_snapshot(fd: int, size: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(fd, min(remaining, 1024 * 1024))
        if not chunk:
            raise PreferenceValidationError("preferences file changed while being read")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(fd, 1):
        raise PreferenceValidationError("preferences file changed while being read")
    return b"".join(chunks)


__all__ = (
    "APPLICATION_PREFERENCES_SCHEMA_VERSION",
    "SAFE_FIELD_KINDS",
    "ApplicationPreferences",
    "PreferenceValidationError",
    "PreferenceMatcher",
    "PreferenceMapping",
    "PreferenceOptOut",
    "ObservedFieldDescriptor",
    "OptOutStatus",
    "PreferencePriority",
    "PreferenceApplicationResult",
    "load_application_preferences",
    "normalize_field_descriptor",
    "matching_mapping",
    "mapping_answer",
    "apply_preferences",
    "order_actions",
)
