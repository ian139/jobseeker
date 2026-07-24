from __future__ import annotations

import ipaddress
import json
import re
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from importlib import resources
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import parse_qsl, unquote, urlsplit


class DescriptorSafety(str, Enum):
    SAFE = "safe"
    SENSITIVE = "sensitive"


class DescriptorLimitError(ValueError):
    pass


@dataclass(frozen=True)
class SafetyPolicy:
    version: str
    terms: tuple[str, ...]
    compact_aliases: tuple[str, ...]
    sensitive_field_kinds: tuple[str, ...]
    ascii_codepoint_ranges: tuple[tuple[int, int], ...]
    max_descriptors: int
    max_descriptor_bytes: int
    max_descriptor_aggregate_bytes: int
    max_options: int
    max_option_bytes: int
    max_option_aggregate_bytes: int
    greenhouse_route_policy: Mapping[str, Any]
    lever_route_policy: Mapping[str, Any]
    route_parity_vectors: tuple[Mapping[str, Any], ...]
    route_policies: Mapping[str, Mapping[str, Any]]
    @property
    def route_policy(self) -> Mapping[str, Any]:
        return self.greenhouse_route_policy

    @property
    def greenhouse_routes(self) -> Mapping[str, Any]:
        return self.greenhouse_route_policy

    @property
    def route_vectors(self) -> tuple[Mapping[str, Any], ...]:
        return self.route_parity_vectors
    @property
    def lever_routes(self) -> Mapping[str, Any]:
        return self.lever_route_policy
    @property
    def ats_route_policies(self) -> Mapping[str, Mapping[str, Any]]:
        return self.route_policies


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError("policy contains a non-JSON value")


def load_safety_policy() -> SafetyPolicy:
    raw = json.loads(resources.files("jobs_assistant").joinpath("safety_policy.json").read_text(encoding="utf-8"))
    caps = raw["caps"]
    raw_routes = {
        "greenhouse": raw["greenhouse_route_policy"],
        "lever": raw["lever_route_policy"],
    }
    frozen_routes: dict[str, Mapping[str, Any]] = {}
    vectors_by_policy: dict[str, tuple[Mapping[str, Any], ...]] = {}
    expected_versions = {
        "greenhouse": "2026-07-10.greenhouse-routes.v1",
        "lever": "2026-07-10.lever-routes.v1",
    }
    for name, route_policy in raw_routes.items():
        if not isinstance(route_policy, dict) or route_policy.get("version") != expected_versions[name]:
            raise ValueError(f"unsupported {name.title()} route policy")
        vectors = route_policy.get("parity_vectors", ())
        if not isinstance(vectors, list) or not all(isinstance(vector, dict) for vector in vectors):
            raise ValueError(f"invalid {name.title()} route parity vectors")
        frozen = _freeze_json(route_policy)
        frozen_routes[name] = frozen
        vectors_by_policy[name] = tuple(frozen["parity_vectors"])
    greenhouse = frozen_routes["greenhouse"]
    lever = frozen_routes["lever"]
    return SafetyPolicy(
        version=str(raw["version"]),
        terms=tuple(str(term) for term in raw["terms"]),
        compact_aliases=tuple(str(alias) for alias in raw["compact_aliases"]),
        sensitive_field_kinds=tuple(str(kind) for kind in raw["sensitive_field_kinds"]),
        ascii_codepoint_ranges=tuple((int(start), int(end)) for start, end in raw["ascii_codepoint_ranges"]),
        max_descriptors=int(caps["max_descriptors"]),
        max_descriptor_bytes=int(caps["max_descriptor_bytes"]),
        max_descriptor_aggregate_bytes=int(caps["max_descriptor_aggregate_bytes"]),
        max_options=int(caps["max_options"]),
        max_option_bytes=int(caps["max_option_bytes"]),
        max_option_aggregate_bytes=int(caps["max_option_aggregate_bytes"]),
        greenhouse_route_policy=greenhouse,
        lever_route_policy=lever,
        route_parity_vectors=vectors_by_policy["greenhouse"],
        route_policies=MappingProxyType(frozen_routes),
    )


def _is_ascii_allowed(value: str, policy: SafetyPolicy) -> bool:
    for char in value:
        codepoint = ord(char)
        if not any(start <= codepoint <= end for start, end in policy.ascii_codepoint_ranges):
            return False
    return True


def normalize_descriptor(value: str) -> str:
    split = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])", " ", value)
    normalized = re.sub(r"[^0-9A-Za-z]+", " ", split).lower()
    return " ".join(normalized.split())


def _compact(value: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", value.lower())


def _check_text_limits(values: tuple[str, ...], *, count_cap: int, item_cap: int, aggregate_cap: int, kind: str) -> None:
    if len(values) > count_cap:
        raise DescriptorLimitError(f"too many {kind}")
    total = 0
    for value in values:
        size = len(value.encode("utf-8"))
        if size > item_cap:
            raise DescriptorLimitError(f"{kind} too large")
        total += size
    if total > aggregate_cap:
        raise DescriptorLimitError(f"{kind} aggregate too large")


def _contains_sensitive(value: str, policy: SafetyPolicy) -> bool:
    normalized = normalize_descriptor(value)
    compacted = _compact(value)
    padded = f" {normalized} "
    for term in policy.terms:
        if f" {normalize_descriptor(term)} " in padded:
            return True
    return any(alias in compacted for alias in policy.compact_aliases)


def classify_descriptors(
    descriptors: tuple[str, ...],
    *,
    field_kind: str | None = None,
    options: tuple[tuple[str, str], ...] = (),
    policy: SafetyPolicy | None = None,
) -> DescriptorSafety:
    active_policy = policy or load_safety_policy()
    _check_text_limits(
        descriptors,
        count_cap=active_policy.max_descriptors,
        item_cap=active_policy.max_descriptor_bytes,
        aggregate_cap=active_policy.max_descriptor_aggregate_bytes,
        kind="descriptors",
    )
    option_texts = tuple(text for option in options for text in option)
    if len(options) > active_policy.max_options:
        raise DescriptorLimitError("too many options")
    _check_text_limits(
        option_texts,
        count_cap=active_policy.max_options * 2,
        item_cap=active_policy.max_option_bytes,
        aggregate_cap=active_policy.max_option_aggregate_bytes,
        kind="option text",
    )
    if field_kind is not None:
        _check_text_limits(
            (field_kind,),
            count_cap=1,
            item_cap=active_policy.max_descriptor_bytes,
            aggregate_cap=active_policy.max_descriptor_bytes,
            kind="field kind",
        )
        if not _is_ascii_allowed(field_kind, active_policy):
            return DescriptorSafety.SENSITIVE
        if _compact(field_kind) in {_compact(kind) for kind in active_policy.sensitive_field_kinds}:
            return DescriptorSafety.SENSITIVE
    for value in (*descriptors, *option_texts):
        if not _is_ascii_allowed(value, active_policy):
            return DescriptorSafety.SENSITIVE
    for value in descriptors:
        if _contains_sensitive(value, active_policy):
            return DescriptorSafety.SENSITIVE
    for value, label in options:
        if not label.strip():
            return DescriptorSafety.SENSITIVE
        if _contains_sensitive(value, active_policy) or _contains_sensitive(label, active_policy):
            return DescriptorSafety.SENSITIVE
    return DescriptorSafety.SAFE


class _RouteReject(ValueError):
    pass


@dataclass(frozen=True)
class GreenhouseRouteDecision:
    """Deterministic route result shared by ATS and browser policy consumers.

    ``field_ownership`` is deliberately independent from ``allowed``: a trusted
    human-only form can establish which fields belong to a job, but it never
    grants an automated request permit.
    """

    allowed: bool
    route_class: str | None
    reason: str
    method: str
    automation: bool = False
    human_only: bool = False
    field_ownership: bool = False
    permit_required: bool = False
    same_board_job: bool = False

    @property
    def permitted(self) -> bool:
        return self.allowed

    @property
    def classification(self) -> str | None:
        return self.route_class

    @property
    def route(self) -> str | None:
        return self.route_class

    @property
    def code(self) -> str:
        return self.reason


@dataclass(frozen=True)
class _ParsedGreenhouseRoute:
    route_class: str
    host: str
    path: str
    query: tuple[tuple[str, str], ...]
    identity: tuple[str, ...] | None


def load_greenhouse_route_policy(policy: SafetyPolicy | None = None) -> Mapping[str, Any]:
    """Return the immutable versioned route graph used by both runtimes."""

    active_policy = policy or load_safety_policy()
    return active_policy.greenhouse_route_policy


def greenhouse_route_parity_vectors(policy: SafetyPolicy | None = None) -> tuple[Mapping[str, Any], ...]:
    active_policy = policy or load_safety_policy()
    return active_policy.route_parity_vectors


def _deny(reason: str, method: str, *, route_class: str | None = None, **flags: bool) -> GreenhouseRouteDecision:
    return GreenhouseRouteDecision(False, route_class, reason, method, **flags)


SUPPORTED_ATS_POLICIES = ("greenhouse", "lever")


def validate_ats_policy_name(name: str) -> str:
    if type(name) is not str or name not in SUPPORTED_ATS_POLICIES:
        raise ValueError("unsupported_ats")
    return name


def load_ats_route_policy(
    ats_policy: str = "greenhouse",
    policy: SafetyPolicy | None = None,
) -> Mapping[str, Any]:
    active_policy = policy or load_safety_policy()
    name = validate_ats_policy_name(ats_policy)
    return active_policy.route_policies[name]


def ats_route_parity_vectors(
    ats_policy: str = "greenhouse",
    policy: SafetyPolicy | None = None,
) -> tuple[Mapping[str, Any], ...]:
    active_policy = policy or load_safety_policy()
    name = validate_ats_policy_name(ats_policy)
    return tuple(active_policy.route_policies[name]["parity_vectors"])


def _policy_for_ats(ats_policy: str, policy: SafetyPolicy | None) -> SafetyPolicy:
    active_policy = policy or load_safety_policy()
    name = validate_ats_policy_name(ats_policy)
    graph = active_policy.route_policies[name]
    return replace(
        active_policy,
        greenhouse_route_policy=graph,
        route_parity_vectors=tuple(graph["parity_vectors"]),
    )


def _ats_reason(decision: GreenhouseRouteDecision, ats_policy: str) -> GreenhouseRouteDecision:
    if ats_policy == "lever" and decision.reason == "same_board_job_required":
        return replace(decision, reason="same_company_job_required")
    return decision


def classify_ats_url(
    url: str,
    *,
    ats_policy: str = "greenhouse",
    method: str = "GET",
    policy: SafetyPolicy | None = None,
) -> GreenhouseRouteDecision:
    return _ats_reason(classify_greenhouse_url(url, method=method, policy=_policy_for_ats(ats_policy, policy)), ats_policy)


def classify_ats_form_action(
    url: str,
    *,
    ats_policy: str = "greenhouse",
    method: str = "POST",
    page_url: str | None = None,
    policy: SafetyPolicy | None = None,
) -> GreenhouseRouteDecision:
    return _ats_reason(classify_greenhouse_form_action(
        url,
        method=method,
        page_url=page_url,
        policy=_policy_for_ats(ats_policy, policy),
    ), ats_policy)


def classify_ats_request(
    url: str,
    *,
    ats_policy: str = "greenhouse",
    method: str = "GET",
    request_class: str = "initial",
    page_url: str | None = None,
    human_permit: bool = False,
    resource_type: str | None = None,
    redirect_count: int = 0,
    policy: SafetyPolicy | None = None,
) -> GreenhouseRouteDecision:
    return _ats_reason(classify_greenhouse_request(
        url,
        method=method,
        request_class=request_class,
        page_url=page_url,
        human_permit=human_permit,
        resource_type=resource_type,
        redirect_count=redirect_count,
        policy=_policy_for_ats(ats_policy, policy),
    ), ats_policy)

def classify_ats_route_vector(
    vector: Mapping[str, Any],
    *,
    ats_policy: str = "greenhouse",
    policy: SafetyPolicy | None = None,
) -> GreenhouseRouteDecision:
    if not isinstance(vector, Mapping):
        return _deny("invalid_vector", "")
    operation = vector.get("operation")
    if operation == "initial":
        return classify_ats_url(vector.get("url", ""), ats_policy=ats_policy, method=vector.get("method", "GET"), policy=policy)
    if operation == "form":
        return classify_ats_form_action(
            vector.get("url", ""),
            ats_policy=ats_policy,
            method=vector.get("method", "POST"),
            page_url=vector.get("page_url"),
            policy=policy,
        )
    if operation == "request":
        return classify_ats_request(
            vector.get("url", ""),
            ats_policy=ats_policy,
            method=vector.get("method", "GET"),
            request_class=vector.get("request_class", "initial"),
            page_url=vector.get("page_url"),
            human_permit=vector.get("human_permit", False),
            resource_type=vector.get("resource_type"),
            redirect_count=vector.get("redirect_count", 0),
            policy=policy,
        )
    return _deny("invalid_vector", _method(vector.get("method", "")))


def is_ats_interactive_origin(
    origin: str,
    *,
    ats_policy: str = "greenhouse",
    policy: SafetyPolicy | None = None,
) -> bool:
    return is_greenhouse_interactive_origin(origin, _policy_for_ats(ats_policy, policy))


def classify_ats_frame_origin(
    origin: str,
    *,
    ats_policy: str = "greenhouse",
    policy: SafetyPolicy | None = None,
) -> GreenhouseRouteDecision:
    allowed = is_ats_interactive_origin(origin, ats_policy=ats_policy, policy=policy)
    return _allow("interactive_frame", "interactive_frame_origin", "", automation=True) if allowed else _deny("untrusted_frame_origin", "")
def _allow(
    route_class: str,
    reason: str,
    method: str,
    *,
    automation: bool = False,
    human_only: bool = False,
    field_ownership: bool = False,
    permit_required: bool = False,
    same_board_job: bool = False,
) -> GreenhouseRouteDecision:
    return GreenhouseRouteDecision(
        True,
        route_class,
        reason,
        method,
        automation=automation,
        human_only=human_only,
        field_ownership=field_ownership,
        permit_required=permit_required,
        same_board_job=same_board_job,
    )


def _route_hosts(policy: SafetyPolicy, section: str) -> tuple[str, ...]:
    value = policy.greenhouse_route_policy.get(section, ())
    if isinstance(value, Mapping):
        value = value.get("allowed_hosts", ())
    return tuple(str(host).lower() for host in value)


def _is_private_host(host: str) -> bool:
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_unspecified)


def _public_url(url: str, policy: SafetyPolicy) -> tuple[Any, str]:
    if not isinstance(url, str) or not url or len(url.encode("utf-8", "surrogatepass")) > 8192:
        raise _RouteReject("invalid_url")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except (TypeError, ValueError):
        raise _RouteReject("invalid_url") from None
    if parts.scheme != "https":
        raise _RouteReject("https_required")
    if parts.username is not None or parts.password is not None:
        raise _RouteReject("userinfo_rejected")
    if parts.fragment or "#" in url:
        raise _RouteReject("fragment_rejected")
    if "?" in url and not parts.query:
        raise _RouteReject("invalid_query")
    if not host:
        raise _RouteReject("host_required")
    host = host.lower().rstrip(".")
    if port not in (None, 443):
        raise _RouteReject("port_rejected")
    allowed_hosts = _route_hosts(policy, "public_host_constraints")[0:]
    if host not in allowed_hosts:
        if _is_private_host(host):
            raise _RouteReject("private_host")
        raise _RouteReject("unsupported_host")
    if _is_private_host(host):
        raise _RouteReject("private_host")
    return parts, host


def _final_like(path: str, query: str, policy: SafetyPolicy) -> bool:
    route_policy = policy.greenhouse_route_policy
    tokens = tuple(str(token).lower() for token in route_policy.get("final_like_tokens", ()))
    text = f"{path}?{query}"
    for _ in range(3):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    mode = route_policy.get("final_like_match")
    lowered = text.lower()
    if mode == "substring_case_insensitive":
        return any(token in lowered for token in tokens)
    if mode != "ascii_word_boundary":
        return True
    words = set(re.findall(r"[a-z0-9]+", lowered))
    return any(token in words for token in tokens)


def _query(parts: Any) -> tuple[tuple[str, str], ...]:
    try:
        query = tuple(parse_qsl(parts.query, keep_blank_values=True, strict_parsing=False))
    except ValueError:
        raise _RouteReject("invalid_query") from None
    keys = tuple(key for key, _ in query)
    if len(keys) != len(set(keys)):
        raise _RouteReject("duplicate_query")
    return query


def _value_matches_pattern(value: str, pattern: str) -> bool:
    if pattern == "ascii_slug":
        return re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None
    if pattern == "digits":
        return re.fullmatch(r"[0-9]+", value) is not None
    if pattern == "uuid":
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, TypeError, ValueError):
            return False
        return value == str(parsed)
    return False


def _query_matches(
    query: tuple[tuple[str, str], ...],
    allowed: tuple[str, ...],
    required: tuple[str, ...] = (),
    patterns: Mapping[str, str] | None = None,
) -> dict[str, str]:
    values = dict(query)
    if any(key not in allowed for key in values):
        raise _RouteReject("unknown_query")
    if any(key not in values for key in required):
        raise _RouteReject("required_query")
    for key, pattern in (patterns or {}).items():
        if key in values and not _value_matches_pattern(values[key], str(pattern)):
            raise _RouteReject("invalid_route")
    return values


def _route_specs(policy: SafetyPolicy, section: str) -> tuple[Mapping[str, Any], ...]:
    value = policy.greenhouse_route_policy.get(section)
    if not isinstance(value, Mapping) or not isinstance(value.get("routes"), (tuple, list)):
        raise _RouteReject("route_policy_invalid")
    routes = tuple(route for route in value["routes"] if isinstance(route, Mapping))
    if len(routes) != len(value["routes"]):
        raise _RouteReject("route_policy_invalid")
    return routes


def _path_pattern_regex(pattern: str) -> str:
    if pattern == "ascii_slug":
        return r"[A-Za-z0-9_-]+"
    if pattern == "digits":
        return r"[0-9]+"
    if pattern == "uuid":
        return r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    raise _RouteReject("route_policy_invalid")


def _match_route_spec(
    spec: Mapping[str, Any],
    *,
    host: str,
    path: str,
    query: tuple[tuple[str, str], ...],
) -> tuple[str, tuple[str, ...] | None] | None:
    hosts = tuple(str(item).lower() for item in spec.get("hosts", ()))
    if host not in hosts:
        return None
    captures: dict[str, str] = {}
    template = spec.get("path_template")
    if template is not None:
        path_patterns = spec.get("path_patterns", {})
        if not isinstance(path_patterns, Mapping):
            raise _RouteReject("route_policy_invalid")
        escaped = re.escape(str(template))

        def replace_placeholder(match: re.Match[str]) -> str:
            name = match.group(1)
            pattern_name = path_patterns.get(name)
            if pattern_name is None:
                raise _RouteReject("route_policy_invalid")
            captures[name] = ""
            return rf"(?P<{name}>{_path_pattern_regex(str(pattern_name))})"

        pattern = re.sub(r"\\\{([A-Za-z0-9_]+)\\\}", replace_placeholder, escaped)
        match = re.fullmatch(pattern, path)
        if match is None:
            return None
        captures.update(match.groupdict())
    elif spec.get("path") is not None:
        if path != str(spec["path"]):
            return None
    else:
        raise _RouteReject("route_policy_invalid")
    allowed = tuple(str(item) for item in spec.get("query_keys", ()))
    required = tuple(str(item) for item in spec.get("required_query", ()))
    patterns = spec.get("query_patterns", {})
    if not isinstance(patterns, Mapping):
        raise _RouteReject("route_policy_invalid")
    values = _query_matches(query, allowed, required, {str(key): str(value) for key, value in patterns.items()})
    route_class = str(spec.get("class", spec.get("route", "")))
    identity_fields = spec.get("identity", ())
    if not isinstance(identity_fields, (tuple, list)) or len(identity_fields) not in {0, 2, 3}:
        raise _RouteReject("route_policy_invalid")
    identity: tuple[str, ...] | None = None
    if len(identity_fields):
        values_by_name = {**captures, **values, "host": host}
        identity_values = tuple(values_by_name.get(str(field)) for field in identity_fields)
        if any(not isinstance(value, str) or not value for value in identity_values):
            raise _RouteReject("route_policy_invalid")
        identity = identity_values
    if route_class.endswith("_form"):
        route_class = route_class[:-5]
    if not route_class:
        raise _RouteReject("route_policy_invalid")
    return route_class, identity


def _parse_route(
    url: str,
    policy: SafetyPolicy,
    *,
    allow_shortlink: bool,
    section: str = "automated_initial_get",
) -> _ParsedGreenhouseRoute:
    parts, host = _public_url(url, policy)
    query = _query(parts)
    if _final_like(parts.path, parts.query, policy):
        raise _RouteReject("final_like_route")
    for spec in _route_specs(policy, section):
        if not allow_shortlink and str(spec.get("class")) == "shortlink":
            continue
        matched = _match_route_spec(spec, host=host, path=parts.path, query=query)
        if matched is not None:
            route_class, identity = matched
            return _ParsedGreenhouseRoute(route_class, host, parts.path, query, identity)
    raise _RouteReject("unsupported_route")


def _parse_confirmation_route(url: str, policy: SafetyPolicy) -> _ParsedGreenhouseRoute:
    parts, host = _public_url(url, policy)
    query = _query(parts)
    if _final_like(parts.path, parts.query, policy):
        raise _RouteReject("final_like_route")
    spec = policy.greenhouse_route_policy.get("post_human_confirmation")
    if not isinstance(spec, Mapping):
        raise _RouteReject("route_policy_invalid")
    matched = _match_route_spec(spec, host=host, path=parts.path, query=query)
    if matched is None:
        raise _RouteReject("unsupported_route")
    _, identity = matched
    return _ParsedGreenhouseRoute("confirmation", host, parts.path, query, identity)


def _method(value: str) -> str:
    if not isinstance(value, str):
        return ""
    return value.upper()


def classify_greenhouse_url(url: str, *, method: str = "GET", policy: SafetyPolicy | None = None) -> GreenhouseRouteDecision:
    """Classify only an automated initial document GET route."""

    active_policy = policy or load_safety_policy()
    verb = _method(method)
    methods = tuple(str(item).upper() for item in active_policy.greenhouse_route_policy["automated_initial_get"]["methods"])
    if verb not in methods:
        return _deny("method_mismatch", verb)
    try:
        route = _parse_route(url, active_policy, allow_shortlink=True)
    except _RouteReject as error:
        return _deny(str(error), verb)
    return _allow(route.route_class, "automated_initial_get", verb, automation=True)


def classify_greenhouse_form_action(
    url: str,
    *,
    method: str = "POST",
    page_url: str | None = None,
    policy: SafetyPolicy | None = None,
) -> GreenhouseRouteDecision:
    """Classify a trusted form action for ownership, never for automation."""

    active_policy = policy or load_safety_policy()
    verb = _method(method)
    allowed_methods = tuple(str(item).upper() for item in active_policy.greenhouse_route_policy["human_only_form_actions"]["methods"])
    if verb not in allowed_methods:
        return _deny("method_mismatch", verb, human_only=True)
    try:
        route = _parse_route(url, active_policy, allow_shortlink=False, section="human_only_form_actions")
    except _RouteReject as error:
        return _deny(str(error), verb, human_only=True)
    if page_url is not None:
        try:
            page = _parse_route(page_url, active_policy, allow_shortlink=False, section="automated_initial_get")
        except _RouteReject:
            return _deny("page_route_invalid", verb, route_class=route.route_class, human_only=True)
        if page.identity != route.identity:
            return _deny("same_board_job_required", verb, route_class=route.route_class, human_only=True)
    return _deny(
        "human_only_form_action",
        verb,
        route_class=route.route_class,
        human_only=True,
        field_ownership=True,
        permit_required=True,
        same_board_job=page_url is not None,
    )


def _canonical_static_path(path: str) -> str:
    text = path
    settled = False
    for _ in range(3):
        if re.search(r"%(?![0-9A-Fa-f]{2})", text):
            raise _RouteReject("invalid_path_encoding")
        decoded = unquote(text)
        if decoded == text:
            settled = True
            break
        text = decoded
    if not settled and re.search(r"%[0-9A-Fa-f]{2}", text):
        raise _RouteReject("invalid_path_encoding")
    if "\\" in text or any(segment in {".", ".."} for segment in text.split("/")):
        raise _RouteReject("unsafe_path")
    return text


def _classify_static(
    url: str,
    *,
    method: str,
    resource_type: str | None,
    policy: SafetyPolicy,
    redirect_count: int,
) -> GreenhouseRouteDecision:
    if method not in tuple(str(item).upper() for item in policy.greenhouse_route_policy["approved_static_get_head"]["methods"]):
        return _deny("method_mismatch", method)
    static = policy.greenhouse_route_policy["approved_static_get_head"]
    if resource_type not in tuple(str(item) for item in static["types"]):
        return _deny("unsupported_resource_type", method)
    cap = policy.greenhouse_route_policy["redirect_caps"]
    if redirect_count > int(cap["max_redirects"]):
        return _deny("redirect_cap", method)
    try:
        parts, _ = _public_url(url, policy)
        _query(parts)
        canonical_path = _canonical_static_path(parts.path)
    except _RouteReject as error:
        return _deny(str(error), method)
    if _final_like(canonical_path, parts.query, policy):
        return _deny("final_like_route", method)
    path_caps = static["path_caps"]
    if len(canonical_path.encode("utf-8", "surrogatepass")) > int(path_caps["max_path_bytes"]):
        return _deny("path_cap", method)
    if len(parts.query.encode("utf-8", "surrogatepass")) > int(path_caps["max_query_bytes"]):
        return _deny("query_cap", method)
    allowed_hosts = tuple(str(item).lower() for item in static["hosts"])
    if parts.hostname is None or parts.hostname.lower() not in allowed_hosts:
        return _deny("unsupported_static_host", method)
    path_prefixes = static.get("path_prefixes", {})
    allowed_prefixes = tuple(str(item) for item in path_prefixes.get(parts.hostname.lower(), ()))
    if not allowed_prefixes or not any(canonical_path.startswith(prefix) for prefix in allowed_prefixes):
        return _deny("unsupported_static_path", method)
    return _allow("static", "approved_static", method, automation=True)


def classify_greenhouse_request(
    url: str,
    *,
    method: str = "GET",
    request_class: str = "initial",
    page_url: str | None = None,
    human_permit: bool = False,
    resource_type: str | None = None,
    redirect_count: int = 0,
    policy: SafetyPolicy | None = None,
) -> GreenhouseRouteDecision:
    """Classify a request without treating human form routes as automated GETs."""

    active_policy = policy or load_safety_policy()
    verb = _method(method)
    kind = request_class.lower() if isinstance(request_class, str) else ""
    if kind in {"initial", "initial_get", "document"}:
        decision = classify_greenhouse_url(url, method=verb, policy=active_policy)
        if decision.allowed and redirect_count > int(active_policy.greenhouse_route_policy["redirect_caps"]["max_redirects"]):
            return _deny("redirect_cap", verb, route_class=decision.route_class)
        return decision
    if kind in {"static", "static_get", "static_head"}:
        return _classify_static(
            url,
            method=verb,
            resource_type=resource_type,
            policy=active_policy,
            redirect_count=redirect_count,
        )
    if kind in {"confirmation", "post_human_confirmation"}:
        confirmation_methods = tuple(
            str(item).upper() for item in active_policy.greenhouse_route_policy["post_human_confirmation"]["methods"]
        )
        if verb not in confirmation_methods:
            return _deny("method_mismatch", verb, human_only=True, permit_required=True)
        try:
            confirmation = _parse_confirmation_route(url, active_policy)
        except _RouteReject as error:
            return _deny(str(error), verb, human_only=True, permit_required=True)
        if human_permit is not True:
            return _deny(
                "human_permit_required",
                verb,
                route_class=confirmation.route_class,
                human_only=True,
                permit_required=True,
            )
        if page_url is None:
            return _deny(
                "same_board_job_required",
                verb,
                route_class=confirmation.route_class,
                human_only=True,
                permit_required=True,
            )
        try:
            page = _parse_route(page_url, active_policy, allow_shortlink=False)
        except _RouteReject:
            return _deny("page_route_invalid", verb, route_class=confirmation.route_class, human_only=True, permit_required=True)
        if page.identity != confirmation.identity:
            return _deny("same_board_job_required", verb, route_class=confirmation.route_class, human_only=True, permit_required=True)
        return _allow(
            "post_human_confirmation",
            "post_human_confirmation",
            verb,
            human_only=True,
            permit_required=True,
            same_board_job=True,
        )
    if kind in {"form", "human_form"}:
        ownership = classify_greenhouse_form_action(url, method=verb, page_url=page_url, policy=active_policy)
        if not ownership.field_ownership:
            return ownership
        return _deny(
            "human_only_form_action",
            verb,
            route_class=ownership.route_class,
            human_only=True,
            field_ownership=True,
            permit_required=True,
            same_board_job=page_url is not None,
        )
    return _deny("unknown_request_class", verb)


def classify_greenhouse_redirect(
    url: str,
    *,
    redirect_count: int,
    policy: SafetyPolicy | None = None,
) -> GreenhouseRouteDecision:
    return classify_greenhouse_request(url, request_class="initial", redirect_count=redirect_count, policy=policy)




def classify_greenhouse_route_vector(
    vector: Mapping[str, Any],
    *,
    policy: SafetyPolicy | None = None,
) -> GreenhouseRouteDecision:
    """Evaluate one JSON parity vector without network, filesystem, or browser state."""

    if not isinstance(vector, Mapping):
        return _deny("invalid_vector", "")
    operation = vector.get("operation")
    method = vector.get("method", "GET")
    url = vector.get("url", "")
    if operation == "initial":
        return classify_greenhouse_url(url, method=method, policy=policy)
    if operation == "form":
        return classify_greenhouse_form_action(url, method=method, page_url=vector.get("page_url"), policy=policy)
    if operation == "request":
        return classify_greenhouse_request(
            url,
            method=method,
            request_class=vector.get("request_class", "initial"),
            page_url=vector.get("page_url"),
            human_permit=vector.get("human_permit", False),
            resource_type=vector.get("resource_type"),
            redirect_count=vector.get("redirect_count", 0),
            policy=policy,
        )
    return _deny("invalid_vector", _method(method))


def is_greenhouse_interactive_origin(origin: str, policy: SafetyPolicy | None = None) -> bool:
    """Return true only for an exact approved Greenhouse frame origin."""

    active_policy = policy or load_safety_policy()
    if not isinstance(origin, str):
        return False
    try:
        parts = urlsplit(origin)
        host = parts.hostname
        port = parts.port
    except (TypeError, ValueError):
        return False
    if parts.scheme != "https" or parts.username is not None or parts.password is not None or parts.path not in {"", "/"}:
        return False
    if parts.query or parts.fragment or port not in (None, 443) or not host:
        return False
    candidate = f"https://{host.lower().rstrip('.') }"
    return candidate in tuple(str(item).lower().rstrip("/") for item in active_policy.greenhouse_route_policy["interactive_frame_origins"])


def classify_greenhouse_frame_origin(origin: str, policy: SafetyPolicy | None = None) -> GreenhouseRouteDecision:
    allowed = is_greenhouse_interactive_origin(origin, policy)
    return _allow("interactive_frame", "interactive_frame_origin", "", automation=True) if allowed else _deny("untrusted_frame_origin", "")


def is_greenhouse_final_like(url: str, policy: SafetyPolicy | None = None) -> bool:
    active_policy = policy or load_safety_policy()
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    return _final_like(parts.path, parts.query, active_policy)


def classify_greenhouse_static(
    url: str,
    *,
    method: str = "GET",
    resource_type: str | None = None,
    redirect_count: int = 0,
    policy: SafetyPolicy | None = None,
) -> GreenhouseRouteDecision:
    active_policy = policy or load_safety_policy()
    return _classify_static(
        url,
        method=_method(method),
        resource_type=resource_type,
        policy=active_policy,
        redirect_count=redirect_count,
    )
