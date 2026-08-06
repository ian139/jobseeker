const SUPPORTED_PLATFORMS = Object.freeze(['greenhouse', 'ashby', 'employer_hosted']);
const SUPPORTED_PLATFORM_SET = new Set(SUPPORTED_PLATFORMS);
const ANSWER_SOURCES = new Set(['memory', 'profile', 'resume', 'agent_inference', 'user']);
const CANDIDATE_CLASSES = new Set(['field', 'non_final_navigation', 'final_candidate', 'unknown']);
const TEXT_INPUT_TYPES = new Set(['text', 'email', 'tel', 'url', 'number', 'date', 'password']);
const NON_TEXT_INPUT_TYPES = new Set(['radio', 'checkbox', 'select', 'file', 'submit', 'button']);
const TRUE_CHECKBOX_ANSWERS = new Set(['true', 'yes', '1', 'on', 'checked']);
const FALSE_CHECKBOX_ANSWERS = new Set(['false', 'no', '0', 'off', 'unchecked']);
const SOURCE_MAX = 128;
const ID_MAX = 512;
const TEXT_MAX = 16 * 1024;
const URL_MAX = 8192;
const HTML_MAX = 4 * 1024 * 1024;
const GREENHOUSE_HOSTS = new Set([
  'job-boards.greenhouse.io',
  'boards.greenhouse.io',
  'job-boards.eu.greenhouse.io',
  'boards.eu.greenhouse.io',
]);
const ASHBY_HOST = 'jobs.ashbyhq.com';
const GREENHOUSE_PATH = /^\/([A-Za-z0-9][A-Za-z0-9_-]*)\/jobs\/([1-9][0-9]*)$/u;
const ASHBY_PATH = /^\/([A-Za-z0-9][A-Za-z0-9_-]*)\/([0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$/iu;
const EMPLOYER_FIRST_SEGMENTS = new Set(['jobs', 'careers', 'apply', 'positions', 'opportunities']);
const EMPLOYER_SEGMENT_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._~-]*$/u;
const EMPLOYER_HOST_MAX = 253;
const EMPLOYER_PATH_MAX = 1024;
const UNSAFE_HTML_ELEMENTS = new Set(['script', 'style', 'noscript', 'template']);
const BLOCK_HTML_ELEMENTS = new Set([
  'address', 'article', 'aside', 'blockquote', 'br', 'dd', 'div', 'dl', 'dt',
  'fieldset', 'figcaption', 'figure', 'footer', 'form', 'h1', 'h2', 'h3', 'h4',
  'h5', 'h6', 'header', 'hr', 'li', 'main', 'nav', 'ol', 'p', 'pre', 'section',
  'table', 'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'ul',
]);
const ENTITY_NAMES = Object.freeze({
  amp: '&',
  apos: "'",
  bull: '•',
  copy: '©',
  deg: '°',
  hellip: '…',
  laquo: '«',
  ldquo: '“',
  lsaquo: '‹',
  lsquo: '‘',
  mdash: '—',
  middot: '·',
  nbsp: ' ',
  ndash: '–',
  not: '¬',
  raquo: '»',
  rdquo: '”',
  rsaquo: '›',
  rsquo: '’',
  reg: '®',
  trade: '™',
  lt: '<',
  gt: '>',
  quot: '"',
});

class PlatformRegistryError extends TypeError {
  constructor(code, message = code) {
    super(message);
    this.name = 'PlatformRegistryError';
    this.code = code;
  }
}

function fail(code, message = code) {
  throw new PlatformRegistryError(code, message);
}

function isRecord(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function assertRecord(value, code) {
  if (!isRecord(value)) fail(code);
  return value;
}

function assertNoSymbols(value, code) {
  if (Reflect.ownKeys(value).some((key) => typeof key === 'symbol')) fail(code);
}

function ownDescriptor(value, key, code) {
  const descriptor = Object.getOwnPropertyDescriptor(value, key);
  if (descriptor && !Object.hasOwn(descriptor, 'value')) fail(code);
  return descriptor;
}

function hasOwn(value, key) {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function ownValue(value, key, code) {
  const descriptor = ownDescriptor(value, key, code);
  return descriptor ? descriptor.value : undefined;
}

function assertSafeString(value, code, {
  allowEmpty = false,
  max = TEXT_MAX,
  identifier = false,
  trim = false,
} = {}) {
  if (typeof value !== 'string' || value.length > max || (!allowEmpty && value.length === 0)) fail(code);
  if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(value)) fail(code);
  if (trim && value.trim() !== value) fail(code);
  if (identifier && value.trim() !== value) fail(code);
  return value;
}

function assertExactKeys(value, allowed, code) {
  assertNoSymbols(value, code);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) fail(code);
    ownDescriptor(value, key, code);
  }
}

function deepClone(value, path = '$', seen = new Set()) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) fail('UNSAFE_DATA', `${path} must contain finite numbers`);
    return value;
  }
  if (value === undefined) return undefined;
  if (typeof value !== 'object') fail('UNSAFE_DATA', `${path} must contain plain data`);
  if (seen.has(value)) fail('UNSAFE_DATA', `${path} must not be cyclic`);
  seen.add(value);
  let result;
  if (Array.isArray(value)) {
    result = new Array(value.length);
    for (const key of Reflect.ownKeys(value)) {
      if (typeof key === 'symbol' || (typeof key === 'string' && key !== String(Number(key)))) {
        if (key !== 'length') fail('UNSAFE_DATA', `${path} has unsupported keys`);
      }
    }
    for (let index = 0; index < value.length; index += 1) {
      const descriptor = ownDescriptor(value, String(index), 'UNSAFE_DATA');
      if (!descriptor) fail('UNSAFE_DATA', `${path}[${index}] is sparse`);
      result[index] = deepClone(descriptor.value, `${path}[${index}]`, seen);
    }
  } else {
    if (!isRecord(value)) fail('UNSAFE_DATA', `${path} must be a plain object`);
    assertNoSymbols(value, 'UNSAFE_DATA');
    result = {};
    for (const key of Object.keys(value)) {
      if (key === '__proto__' || key === 'constructor' || key === 'prototype') {
        fail('UNSAFE_DATA', `${path} has an unsafe key`);
      }
      const descriptor = ownDescriptor(value, key, 'UNSAFE_DATA');
      result[key] = deepClone(descriptor.value, `${path}.${key}`, seen);
    }
  }
  seen.delete(value);
  return result;
}

function deepFreeze(value, seen = new Set()) {
  if (value === null || typeof value !== 'object' || seen.has(value)) return value;
  seen.add(value);
  for (const child of Object.values(value)) deepFreeze(child, seen);
  return Object.freeze(value);
}

function immutable(value) {
  return deepFreeze(deepClone(value));
}

function rawUrlParts(value) {
  const schemeEnd = value.indexOf('://');
  if (schemeEnd < 0) return null;
  const authorityStart = schemeEnd + 3;
  const rest = value.slice(authorityStart);
  const boundary = rest.search(/[/?#]/u);
  const authority = boundary < 0 ? rest : rest.slice(0, boundary);
  const suffix = boundary < 0 ? '' : rest.slice(boundary);
  const pathEnd = suffix.search(/[?#]/u);
  const rawPath = pathEnd < 0 ? suffix : suffix.slice(0, pathEnd);
  return { authority, rawPath: rawPath || '/' };
}

function normalizeVerifiedEmployerHost(value, code = 'INVALID_HOST_BINDING') {
  assertSafeString(value, code, {
    allowEmpty: false,
    max: EMPLOYER_HOST_MAX,
    trim: true,
  });
  if (value !== value.toLowerCase() || value.includes('xn--')) fail(code);
  const labels = value.split('.');
  if (labels.length < 2 || labels.some((label) => (
    label.length === 0
      || label.length > 63
      || !/^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/u.test(label)
  ))) fail(code);
  if (/^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$/u.test(value)) fail(code);
  return value;
}

function normalizeEmployerPath(pathname) {
  if (pathname.length < 1 || pathname.length > EMPLOYER_PATH_MAX
      || pathname.endsWith('/') || pathname.includes('//')) return null;
  const segments = pathname.split('/');
  if (segments.length < 3 || segments.length > 9 || segments[0] !== '') return null;
  const routeSegments = segments.slice(1);
  if (!EMPLOYER_FIRST_SEGMENTS.has(routeSegments[0])) return null;
  if (routeSegments.some((segment) => !EMPLOYER_SEGMENT_PATTERN.test(segment)
      || segment === '.' || segment === '..')) return null;
  return routeSegments;
}

function parseApplicationUrl(value, verifiedEmployerHost = undefined, hostCode = 'INVALID_HOST_BINDING') {
  const employerHost = verifiedEmployerHost === undefined
    ? undefined
    : normalizeVerifiedEmployerHost(verifiedEmployerHost, hostCode);
  if (typeof value !== 'string' || value.length === 0 || value.length > URL_MAX || value.trim() !== value) return null;
  if (/[\u0000-\u001f\u007f]/u.test(value)) return null;
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }
  if (parsed.protocol !== 'https:' || parsed.username !== '' || parsed.password !== '' || parsed.hash !== '') return null;
  const raw = rawUrlParts(value);
  if (raw === null || raw.authority.length === 0 || raw.authority !== parsed.hostname) return null;
  if (raw.rawPath !== parsed.pathname || value.includes('#')) return null;
  if (GREENHOUSE_HOSTS.has(parsed.hostname)) {
    const match = GREENHOUSE_PATH.exec(parsed.pathname);
    if (!match) return null;
    return {
      platform: 'greenhouse',
      host: parsed.hostname,
      board: match[1],
      externalJobId: match[2],
    };
  }
  if (parsed.hostname === ASHBY_HOST) {
    const match = ASHBY_PATH.exec(parsed.pathname);
    if (!match) return null;
    return {
      platform: 'ashby',
      host: parsed.hostname,
      organization: match[1],
      externalJobId: match[2],
    };
  }
  if (employerHost !== undefined && parsed.hostname === employerHost) {
    const segments = normalizeEmployerPath(parsed.pathname);
    if (segments === null) return null;
    return {
      platform: 'employer_hosted',
      host: parsed.hostname,
      segments,
      externalJobId: segments.join('/'),
    };
  }
  return null;
}

function normalizeUrlOptions(options, code = 'INVALID_URL_OPTIONS') {
  if (options === undefined) return undefined;
  assertRecord(options, code);
  assertExactKeys(options, new Set(['verifiedEmployerHost']), code);
  if (!hasOwn(options, 'verifiedEmployerHost')) return undefined;
  const value = ownValue(options, 'verifiedEmployerHost', code);
  if (value === undefined) return undefined;
  return normalizeVerifiedEmployerHost(value, code);
}

function parseUrlWithOptions(value, options, code = 'INVALID_URL_OPTIONS') {
  const employerHost = normalizeUrlOptions(options, code);
  return parseApplicationUrl(value, employerHost, code);
}

function normalizeText(value, code) {
  assertSafeString(value, code, { max: TEXT_MAX });
  const normalized = value.replace(/\s+/gu, ' ').trim();
  if (normalized.length === 0) fail(code);
  return normalized;
}

function normalizeExternalId(value, platform) {
  if (platform === 'greenhouse') {
    if (typeof value === 'number') {
      if (!Number.isSafeInteger(value) || value < 1) fail('INVALID_PAYLOAD_ID');
      return String(value);
    }
    if (typeof value === 'string' && /^[1-9][0-9]*$/u.test(value)) return value;
    fail('INVALID_PAYLOAD_ID');
  }
  assertSafeString(value, 'INVALID_PAYLOAD_ID', { identifier: true, max: ID_MAX });
  if (!ASHBY_PATH.test(`/x/${value}`.replace('/x/', '/x/'))) fail('INVALID_PAYLOAD_ID');
  return value;
}

function decodeHtmlEntities(value) {
  return value.replace(/&(#x[0-9a-f]+|#[0-9]+|[A-Za-z][A-Za-z0-9]+);?/giu, (whole, entity) => {
    const lower = entity.toLowerCase();
    if (lower.startsWith('#x')) {
      const codePoint = Number.parseInt(lower.slice(2), 16);
      return Number.isFinite(codePoint) && codePoint > 0 && codePoint <= 0x10ffff
        ? String.fromCodePoint(codePoint)
        : '';
    }
    if (lower.startsWith('#')) {
      const codePoint = Number.parseInt(lower.slice(1), 10);
      return Number.isFinite(codePoint) && codePoint > 0 && codePoint <= 0x10ffff
        ? String.fromCodePoint(codePoint)
        : '';
    }
    return Object.hasOwn(ENTITY_NAMES, lower) ? ENTITY_NAMES[lower] : whole;
  });
}

function findTagEnd(value, start) {
  let quote = null;
  for (let index = start; index < value.length; index += 1) {
    const character = value[index];
    if (quote !== null) {
      if (character === quote) quote = null;
    } else if (character === '"' || character === "'") {
      quote = character;
    } else if (character === '>') {
      return index;
    }
  }
  return -1;
}

function skipUnsafeElement(value, contentStart, element) {
  const closePattern = new RegExp(`<\\/\\s*${element}\\b`, 'iu');
  const match = closePattern.exec(value.slice(contentStart));
  if (!match) return value.length;
  const closeStart = contentStart + match.index;
  const closeEnd = findTagEnd(value, closeStart);
  return closeEnd < 0 ? value.length : closeEnd + 1;
}

function htmlToText(value) {
  const withoutMarkup = [];
  let index = 0;
  while (index < value.length) {
    if (value.startsWith('<!--', index)) {
      const endComment = value.indexOf('-->', index + 4);
      index = endComment < 0 ? value.length : endComment + 3;
      withoutMarkup.push(' ');
      continue;
    }
    if (value[index] !== '<') {
      withoutMarkup.push(value[index]);
      index += 1;
      continue;
    }
    const end = findTagEnd(value, index + 1);
    if (end < 0) {
      withoutMarkup.push(' ');
      break;
    }
    const token = value.slice(index + 1, end);
    const nameMatch = /^\s*\/?\s*([A-Za-z][A-Za-z0-9:-]*)/u.exec(token);
    const name = nameMatch ? nameMatch[1].toLowerCase() : '';
    const closing = /^\s*\//u.test(token);
    if (!closing && UNSAFE_HTML_ELEMENTS.has(name)) {
      index = skipUnsafeElement(value, end + 1, name);
      withoutMarkup.push(' ');
      continue;
    }
    if (BLOCK_HTML_ELEMENTS.has(name)) withoutMarkup.push('\n');
    index = end + 1;
  }
  return decodeHtmlEntities(withoutMarkup.join('')).replace(/\s+/gu, ' ').trim();
}

function requirePayloadString(payload, key, code = 'INVALID_PAYLOAD') {
  if (!hasOwn(payload, key)) fail(code);
  return assertSafeString(ownValue(payload, key, code), code, { max: HTML_MAX });
}

function requirePayloadRecord(payload, key, code = 'INVALID_PAYLOAD') {
  if (!hasOwn(payload, key)) fail(code);
  return assertRecord(ownValue(payload, key, code), code);
}
function samePlatformRoute(first, second) {
  if (first?.platform !== second?.platform) return false;
  if (first.platform === 'greenhouse') {
    return first.board === second.board && first.externalJobId === second.externalJobId;
  }
  if (first.platform === 'ashby') {
    return first.organization === second.organization
      && first.externalJobId.toLowerCase() === second.externalJobId.toLowerCase();
  }
  return first.host === second.host && first.segments.join('/') === second.segments.join('/');
}

function snapshotForNormalizedSource(applicationUrl, applicationHost, route, payload) {
  const payloadRoute = parseApplicationUrl(
    requirePayloadString(payload, 'url'),
    route.platform === 'employer_hosted' ? applicationHost : undefined,
    'INVALID_PAYLOAD',
  );
  if (!samePlatformRoute(route, payloadRoute)) fail('PAYLOAD_URL_MISMATCH');
  const description = htmlToText(requirePayloadString(payload, 'description'));
  if (description.length === 0) fail('INVALID_PAYLOAD_DESCRIPTION');
  return immutable({
    schema: 'platform-job-snapshot-v1',
    platform: route.platform,
    applicationUrl,
    applicationHost,
    externalJobId: route.externalJobId ?? null,
    title: normalizeText(requirePayloadString(payload, 'job_title'), 'INVALID_PAYLOAD'),
    company: normalizeText(requirePayloadString(payload, 'company'), 'INVALID_PAYLOAD'),
    location: normalizeText(requirePayloadString(payload, 'location'), 'INVALID_PAYLOAD'),
    description,
  });
}

function snapshotForGreenhouse(applicationUrl, route, payload) {
  if (!hasOwn(payload, 'absolute_url')) {
    return snapshotForNormalizedSource(applicationUrl, route.host, route, payload);
  }
  const externalJobId = normalizeExternalId(ownValue(payload, 'id', 'INVALID_PAYLOAD_ID'), 'greenhouse');
  if (externalJobId !== route.externalJobId) fail('PAYLOAD_ID_MISMATCH');
  const absoluteRoute = parseApplicationUrl(requirePayloadString(payload, 'absolute_url'));
  if (!samePlatformRoute(route, absoluteRoute)) fail('PAYLOAD_URL_MISMATCH');
  const location = requirePayloadRecord(payload, 'location');
  const locationName = normalizeText(requirePayloadString(location, 'name'), 'INVALID_PAYLOAD');
  const description = htmlToText(requirePayloadString(payload, 'content'));
  if (description.length === 0) fail('INVALID_PAYLOAD_DESCRIPTION');
  return immutable({
    schema: 'platform-job-snapshot-v1',
    platform: 'greenhouse',
    applicationUrl,
    applicationHost: route.host,
    externalJobId,
    title: normalizeText(requirePayloadString(payload, 'title'), 'INVALID_PAYLOAD'),
    company: normalizeText(requirePayloadString(payload, 'company_name'), 'INVALID_PAYLOAD'),
    location: locationName,
    description,
  });
}

function snapshotForAshby(applicationUrl, route, payload) {
  if (!hasOwn(payload, 'jobPosting')) {
    return snapshotForNormalizedSource(applicationUrl, route.host, route, payload);
  }
  const posting = requirePayloadRecord(payload, 'jobPosting');
  const externalJobId = normalizeExternalId(ownValue(posting, 'id', 'INVALID_PAYLOAD_ID'), 'ashby');
  if (externalJobId.toLowerCase() !== route.externalJobId.toLowerCase()) fail('PAYLOAD_ID_MISMATCH');
  const postingRoute = parseApplicationUrl(requirePayloadString(posting, 'applicationUrl'));
  if (!samePlatformRoute(route, postingRoute)) fail('PAYLOAD_URL_MISMATCH');
  const description = htmlToText(requirePayloadString(posting, 'descriptionHtml'));
  if (description.length === 0) fail('INVALID_PAYLOAD_DESCRIPTION');
  return immutable({
    schema: 'platform-job-snapshot-v1',
    platform: 'ashby',
    applicationUrl,
    applicationHost: route.host,
    externalJobId,
    title: normalizeText(requirePayloadString(posting, 'title'), 'INVALID_PAYLOAD'),
    company: normalizeText(requirePayloadString(posting, 'organizationName'), 'INVALID_PAYLOAD'),
    location: normalizeText(requirePayloadString(posting, 'locationName'), 'INVALID_PAYLOAD'),
    description,
  });
}

function aliasValue(value, keys, code = 'INVALID_PLAN_INPUT') {
  let found = false;
  let selected;
  for (const key of keys) {
    if (!hasOwn(value, key)) continue;
    const candidate = ownValue(value, key, code);
    if (!found) {
      selected = candidate;
      found = true;
    } else if (!Object.is(selected, candidate)) {
      fail(code);
    }
  }
  return found ? selected : undefined;
}

function optionalSafeString(value, code, {
  allowEmpty = true,
  identifier = false,
  max = TEXT_MAX,
} = {}) {
  if (value === undefined || value === null) return value;
  return assertSafeString(value, code, { allowEmpty, identifier, max });
}

function optionalBoolean(value, code, fallback) {
  if (value === undefined) return fallback;
  if (typeof value !== 'boolean') fail(code);
  return value;
}

function normalizeValueState(value, code) {
  const hasValue = hasOwn(value, 'value');
  const rawValue = hasValue ? ownValue(value, 'value', code) : null;
  if (rawValue !== null && typeof rawValue !== 'string' && typeof rawValue !== 'boolean'
      && !(Array.isArray(rawValue) && rawValue.every((item) => typeof item === 'string'))) {
    fail(code);
  }
  if (Array.isArray(rawValue)) {
    for (const item of rawValue) assertSafeString(item, code, { allowEmpty: true, max: TEXT_MAX });
  } else if (typeof rawValue === 'string') {
    assertSafeString(rawValue, code, { allowEmpty: true, max: TEXT_MAX });
  }
  const rawPresent = aliasValue(value, ['valuePresent', 'value_present'], code);
  const valuePresent = optionalBoolean(rawPresent, code, rawValue !== null
    && (!Array.isArray(rawValue) || rawValue.length > 0)
    && (typeof rawValue !== 'string' || rawValue.length > 0));
  return { value: rawValue, valuePresent };
}

function normalizeOption(value, optionIndex, code) {
  assertRecord(value, code);
  const label = aliasValue(value, ['label'], code);
  const optionValue = aliasValue(value, ['value'], code);
  if (label === undefined || optionValue === undefined) fail(code);
  optionalSafeString(label, code, { allowEmpty: true, max: TEXT_MAX });
  optionalSafeString(optionValue, code, { allowEmpty: true, max: TEXT_MAX });
  const disabled = optionalBoolean(aliasValue(value, ['disabled'], code), code, false);
  const selected = optionalBoolean(aliasValue(value, ['selected'], code), code, false);
  return {
    label: label ?? null,
    value: optionValue ?? null,
    disabled,
    selected,
    optionIndex,
  };
}

function normalizeControl(value, index) {
  const code = 'INVALID_OBSERVATION';
  assertRecord(value, code);
  const fieldId = aliasValue(value, ['fieldId', 'field_id', 'stableId', 'stable_id'], code);
  if (fieldId !== null && fieldId !== undefined) {
    assertSafeString(fieldId, code, { identifier: true, max: ID_MAX });
  }
  const controlReference = aliasValue(value, ['controlReference', 'control_reference', 'ref', 'reference'], code);
  if (controlReference !== null && controlReference !== undefined) {
    assertSafeString(controlReference, code, { identifier: true, max: ID_MAX });
  }
  const name = aliasValue(value, ['name'], code);
  const label = aliasValue(value, ['label'], code);
  const kind = aliasValue(value, ['kind'], code);
  const tag = aliasValue(value, ['tag'], code);
  const type = aliasValue(value, ['type', 'inputType', 'input_type'], code);
  const role = aliasValue(value, ['role'], code);
  for (const candidate of [name, label]) {
    optionalSafeString(candidate, code, { allowEmpty: true, max: TEXT_MAX });
  }
  for (const candidate of [kind, tag, type, role]) {
    optionalSafeString(candidate, code, { allowEmpty: true, identifier: true, max: ID_MAX });
  }
  const options = aliasValue(value, ['options'], code);
  if (options !== undefined && !Array.isArray(options)) fail(code);
  const normalizedOptions = (options ?? []).map((option, optionIndex) =>
    normalizeOption(option, optionIndex, code));

  const candidateClass = aliasValue(value, ['candidateClass', 'candidate_class', 'class'], code);
  const nestedCandidate = aliasValue(value, ['candidate'], code);
  let normalizedCandidateClass = candidateClass;
  if (nestedCandidate !== undefined && nestedCandidate !== null) {
    assertRecord(nestedCandidate, code);
    const nestedClass = aliasValue(nestedCandidate, ['class', 'candidateClass', 'candidate_class'], code);
    if (nestedClass !== undefined) {
      if (normalizedCandidateClass !== undefined && !Object.is(normalizedCandidateClass, nestedClass)) {
        fail(code);
      }
      normalizedCandidateClass = nestedClass;
    }
  }
  optionalSafeString(normalizedCandidateClass, code, { allowEmpty: false, identifier: true, max: ID_MAX });
  if (normalizedCandidateClass !== undefined && normalizedCandidateClass !== null
      && !CANDIDATE_CLASSES.has(normalizedCandidateClass)) {
    fail(code);
  }

  const required = optionalBoolean(aliasValue(value, ['required'], code), code, false);
  const visible = optionalBoolean(aliasValue(value, ['visible'], code), code, true);
  const enabledAlias = aliasValue(value, ['enabled'], code);
  const disabledAlias = aliasValue(value, ['disabled'], code);
  const enabled = optionalBoolean(enabledAlias, code, disabledAlias === undefined ? true : !optionalBoolean(disabledAlias, code, false));
  const disabled = optionalBoolean(disabledAlias, code, !enabled);
  if (enabled !== !disabled) fail(code);
  const readonly = optionalBoolean(
    aliasValue(value, ['readonly', 'readOnly', 'read_only'], code),
    code,
    false,
  );
  const checked = aliasValue(value, ['checked'], code);
  if (checked !== undefined && checked !== null && typeof checked !== 'boolean') fail(code);
  const selected = aliasValue(value, ['selected'], code);
  if (selected !== undefined && selected !== null
      && !(Array.isArray(selected) && selected.every((item) => typeof item === 'string'))) {
    fail(code);
  }
  if (Array.isArray(selected)) {
    for (const item of selected) assertSafeString(item, code, { allowEmpty: true, max: TEXT_MAX });
  }
  const state = normalizeValueState(value, code);
  if (fieldId === undefined) fail(code, `control ${index} is missing fieldId`);
  return {
    fieldId: fieldId ?? null,
    controlReference: controlReference ?? null,
    name: typeof name === 'string' ? name : '',
    label: typeof label === 'string' ? label : '',
    kind: typeof kind === 'string' ? kind.toLowerCase() : '',
    tag: typeof tag === 'string' ? tag.toLowerCase() : '',
    type: typeof type === 'string' ? type.toLowerCase() : '',
    role: typeof role === 'string' ? role.toLowerCase() : '',
    options: normalizedOptions,
    candidateClass: normalizedCandidateClass ?? null,
    required,
    visible,
    enabled,
    disabled,
    readonly,
    value: state.value,
    valuePresent: state.valuePresent,
    checked: checked ?? null,
    selected: selected ?? null,
  };
}


function normalizeAnswer(value, fieldId) {
  const code = 'INVALID_ANSWERS';
  assertRecord(value, code);
  assertExactKeys(value, new Set(['source', 'value']), code);
  const source = ownValue(value, 'source', code);
  assertSafeString(source, code, { identifier: true, max: SOURCE_MAX });
  if (!ANSWER_SOURCES.has(source)) fail('INVALID_ANSWER_SOURCE');
  const answerValue = ownValue(value, 'value', code);
  if (typeof answerValue !== 'string' || answerValue.length > TEXT_MAX || /[\u0000-\u001f\u007f]/u.test(answerValue)) {
    fail(code, `answer for ${fieldId} must contain a safe string value`);
  }
  return { source, value: answerValue };
}

function optionValueFor(control, answerValue) {
  const matches = [];
  for (const option of control.options) {
    if (option.disabled) continue;
    if (option.label === answerValue || option.value === answerValue) {
      matches.push(option);
    }
  }
  if (matches.length > 1) fail('INVALID_OPTION_MAPPING');
  if (matches.length === 1) {
    if (matches[0].value === null) fail('INVALID_OPTION_MAPPING');
    return matches[0].value;
  }
  const choiceLike = control.kind === 'radio'
    || control.type === 'radio'
    || control.role === 'radio'
    || control.role === 'radiogroup';
  if (choiceLike && (typeof control.value === 'string' || control.value === null)) {
    if (control.label === answerValue || control.value === answerValue) return control.value ?? answerValue;
  }
  fail('INVALID_OPTION_MAPPING');
}
function checkboxValueFor(control, answerValue) {
  const normalized = answerValue.trim().toLowerCase();
  if (TRUE_CHECKBOX_ANSWERS.has(normalized)) return true;
  if (FALSE_CHECKBOX_ANSWERS.has(normalized)) return false;
  if (control.label === answerValue || control.value === answerValue) return true;
  fail('INVALID_CHECKBOX_MAPPING');
}


function readonlyMatches(kind, control, answerValue) {
  if (kind === 'checkbox') {
    return typeof control.checked === 'boolean'
      && control.checked === checkboxValueFor(control, answerValue);
  }
  if (!control.valuePresent) {
    return (kind === 'input' || kind === 'textarea')
      && answerValue === ''
      && (control.value === null || control.value === '');
  }
  if (kind === 'file') return true;
  if (kind === 'select' || kind === 'combobox' || kind === 'radio' || kind === 'yes_no') {
    const expected = optionValueFor(control, answerValue);
    const observed = control.value ?? (
      Array.isArray(control.selected) && control.selected.length === 1
        ? control.selected[0]
        : control.selected
    );
    if (Array.isArray(observed)) return observed.length === 1 && observed[0] === expected;
    if (kind === 'radio' || kind === 'yes_no') {
      return control.checked !== false && observed === expected;
    }
    return observed === expected;
  }
  return control.value === answerValue;
}

function isUnclassifiedNonField(control) {
  return control.kind === 'button'
    || control.kind === 'navigation'
    || control.kind === 'link'
    || control.tag === 'button'
    || control.tag === 'a'
    || control.role === 'button'
    || control.type === 'submit'
    || control.type === 'image';
}

function isNonFieldCandidate(control) {
  return control.candidateClass === 'non_final_navigation'
    || (control.candidateClass === null && isUnclassifiedNonField(control));
}

function planFinalCandidate(controls) {
  const current = controls.filter((control) =>
    control.candidateClass === 'final_candidate' && control.visible && control.enabled);
  if (current.length > 1) fail('AMBIGUOUS_FINAL_CANDIDATE');
  if (current.length === 0) return null;
  if (current[0].controlReference === null) fail('INVALID_OBSERVATION');
  return current[0].controlReference;
}

function normalizeMissingAnswer(control, kind, unresolved) {
  if (!control.required) return;
  unresolved.push({
    fieldId: control.fieldId,
    reason: kind === 'textarea' ? 'inference_required' : 'answer_required',
  });
}

function actionFor(platform, kind, control, answer, resumeUploadPath) {
  if (control.controlReference === null) fail('INVALID_OBSERVATION');
  const base = {
    fieldId: control.fieldId,
    operation: 'fill_text',
    mechanic: `${platform}_native_input`,
    value: answer.value,
    source: answer.source,
    controlReference: control.controlReference,
  };
  if (kind === 'file') {
    return { ...base, operation: 'upload_file', mechanic: `${platform}_file_input`, value: resumeUploadPath };
  }
  if (kind === 'input') return base;
  if (kind === 'textarea') return { ...base, mechanic: `${platform}_native_textarea` };
  if (kind === 'select') {
    return {
      ...base,
      operation: 'select_option',
      mechanic: `${platform}_native_select`,
      value: optionValueFor(control, answer.value),
    };
  }
  if (kind === 'combobox') {
    if (control.options.length === 0) {
      if (control.role === 'listbox') fail('INVALID_OPTION_MAPPING');
      return {
        ...base,
        operation: 'open_combobox',
        mechanic: `${platform}_combobox_open`,
        value: answer.value,
      };
    }
    return {
      ...base,
      operation: 'select_option',
      mechanic: `${platform}_combobox_exact_option`,
      value: optionValueFor(control, answer.value),
    };
  }
  if (kind === 'radio' || kind === 'yes_no') {
    return {
      ...base,
      operation: 'toggle',
      mechanic: platform === 'greenhouse'
        ? 'greenhouse_native_radio'
        : platform === 'ashby'
          ? 'ashby_yes_no'
          : 'employer_hosted_radio',
      value: optionValueFor(control, answer.value),
    };
  }
  if (kind === 'checkbox') {
    const desired = checkboxValueFor(control, answer.value);
    if (control.checked === desired) return null;
    if (typeof control.checked !== 'boolean') fail('INVALID_OBSERVATION');
    return {
      ...base,
      operation: 'toggle',
      mechanic: `${platform}_native_checkbox`,
      value: desired,
    };
  }
  fail('UNSUPPORTED_CONTROL');
}

function controlPlanKind(platform, control) {
  const kind = control.kind;
  const tag = control.tag;
  const type = control.type;
  const role = control.role;
  if (type === 'file' || kind === 'file' || tag === 'file') return 'file';
  if (tag === 'textarea' || kind === 'textarea' || type === 'textarea') return 'textarea';
  if (type === 'checkbox' || kind === 'checkbox' || role === 'checkbox' || role === 'switch') {
    return 'checkbox';
  }
  if (tag === 'select' || kind === 'select' || type === 'select' || kind === 'native_select') {
    return 'select';
  }
  if (platform === 'greenhouse' || platform === 'employer_hosted') {
    if (role === 'combobox' || role === 'listbox'
        || kind === 'combobox' || kind === 'autocomplete' || kind === 'custom_select') {
      return 'combobox';
    }
    if (type === 'radio' || kind === 'radio' || kind === 'radio_group' || kind === 'yes_no'
        || role === 'radio' || role === 'radiogroup') {
      return 'radio';
    }
    if ((tag === 'input' || kind === 'input' || kind === 'aria' || kind === 'contenteditable'
        || role === 'textbox' || role === 'searchbox' || TEXT_INPUT_TYPES.has(type))
        && !NON_TEXT_INPUT_TYPES.has(type)) {
      return 'input';
    }
    return null;
  }
  if (role === 'combobox' || role === 'listbox'
      || kind === 'combobox' || kind === 'autocomplete' || kind === 'custom_select') {
    return 'combobox';
  }
  if (role === 'radiogroup' || role === 'radio' || type === 'radio'
      || kind === 'radio' || kind === 'radio_group') {
    return 'yes_no';
  }
  if ((tag === 'input' || kind === 'input' || kind === 'aria' || kind === 'contenteditable'
      || role === 'textbox' || role === 'searchbox' || TEXT_INPUT_TYPES.has(type))
      && !NON_TEXT_INPUT_TYPES.has(type)) {
    return 'input';
  }
  return null;
}


function normalizePlanInput(value) {
  const code = 'INVALID_PLAN_INPUT';
  assertRecord(value, code);
  assertExactKeys(value, new Set([
    'platform',
    'observation',
    'answers',
    'resumeUploadPath',
    'applicationUrl',
    'applicationHost',
  ]), code);
  let platform = ownValue(value, 'platform', code);
  if (typeof platform !== 'string' || !SUPPORTED_PLATFORM_SET.has(platform)) fail('INVALID_PLATFORM');
  const observation = assertRecord(ownValue(value, 'observation', code), 'INVALID_OBSERVATION');
  const observationId = aliasValue(observation, ['observationId', 'observation_id'], 'INVALID_OBSERVATION');
  assertSafeString(observationId, 'INVALID_OBSERVATION', { identifier: true, max: ID_MAX });
  const observationUrl = hasOwn(observation, 'url')
    ? ownValue(observation, 'url', 'INVALID_OBSERVATION')
    : undefined;
  const applicationUrl = hasOwn(value, 'applicationUrl')
    ? ownValue(value, 'applicationUrl', code)
    : undefined;
  const applicationHost = hasOwn(value, 'applicationHost')
    ? ownValue(value, 'applicationHost', code)
    : undefined;
  let effectiveApplicationUrl = applicationUrl;
  let effectiveApplicationHost = applicationHost;
  if (platform === 'employer_hosted') {
    if (applicationUrl === undefined || applicationHost === undefined || observationUrl === undefined) {
      fail(code);
    }
    assertSafeString(applicationUrl, code, { max: URL_MAX, trim: true });
    assertSafeString(observationUrl, 'INVALID_OBSERVATION', { max: URL_MAX, trim: true });
    const normalizedHost = normalizeVerifiedEmployerHost(applicationHost, code);
    const sourceRoute = parseApplicationUrl(applicationUrl, normalizedHost, code);
    if (sourceRoute?.platform !== 'employer_hosted') fail(code);
    const redirect = reclassifyApplicationRedirect({
      applicationUrl,
      applicationHost: normalizedHost,
      finalUrl: observationUrl,
    });
    platform = redirect.platform;
    effectiveApplicationUrl = redirect.applicationUrl;
    effectiveApplicationHost = redirect.applicationHost;
  } else if (applicationUrl !== undefined || applicationHost !== undefined) {
    fail(code);
  }
  const controls = ownValue(observation, 'controls', 'INVALID_OBSERVATION');
  if (!Array.isArray(controls)) fail('INVALID_OBSERVATION');
  const normalizedControls = controls.map((control, index) => normalizeControl(control, index));
  const fieldIds = new Set();
  const references = new Set();
  for (const control of normalizedControls) {
    if (control.fieldId !== null) {
      if (fieldIds.has(control.fieldId)) fail('INVALID_OBSERVATION');
      fieldIds.add(control.fieldId);
    }
    if (control.controlReference !== null) {
      if (references.has(control.controlReference)) fail('INVALID_OBSERVATION');
      references.add(control.controlReference);
    }
  }
  const answers = assertRecord(ownValue(value, 'answers', code), 'INVALID_ANSWERS');
  assertNoSymbols(answers, 'INVALID_ANSWERS');
  const normalizedAnswers = new Map();
  for (const fieldId of Object.keys(answers)) {
    if (fieldId === '__proto__' || fieldId === 'constructor' || fieldId === 'prototype') fail('INVALID_ANSWERS');
    normalizedAnswers.set(fieldId, normalizeAnswer(ownValue(answers, fieldId, 'INVALID_ANSWERS'), fieldId));
  }
  const resumeUploadPath = ownValue(value, 'resumeUploadPath', code);
  assertSafeString(resumeUploadPath, code, { max: TEXT_MAX });
  return {
    platform,
    observationId,
    controls: normalizedControls,
    answers: normalizedAnswers,
    resumeUploadPath,
    applicationUrl: effectiveApplicationUrl,
    applicationHost: effectiveApplicationHost,
  };
}

export function classifyApplicationUrl(url, options = undefined) {
  return parseUrlWithOptions(url, options)?.platform ?? null;
}

function canonicalUrlForRoute(url, route) {
  return `https://${route.host}${new URL(url).pathname}`;
}

export function canonicalizeApplicationUrl(url, options = undefined) {
  const route = parseUrlWithOptions(url, options);
  if (route === null) return null;
  return canonicalUrlForRoute(url, route);
}

export function reclassifyApplicationRedirect(value) {
  const code = 'INVALID_REDIRECT_INPUT';
  assertRecord(value, code);
  assertExactKeys(value, new Set(['applicationUrl', 'applicationHost', 'finalUrl']), code);
  const applicationUrl = ownValue(value, 'applicationUrl', code);
  const applicationHost = ownValue(value, 'applicationHost', code);
  const finalUrl = ownValue(value, 'finalUrl', code);
  assertSafeString(applicationUrl, code, { max: URL_MAX, trim: true });
  const normalizedHost = normalizeVerifiedEmployerHost(applicationHost, code);
  assertSafeString(finalUrl, code, { max: URL_MAX, trim: true });
  const sourceRoute = parseApplicationUrl(applicationUrl, normalizedHost, code);
  if (sourceRoute?.platform !== 'employer_hosted') fail(code);
  const finalAtsRoute = parseApplicationUrl(finalUrl);
  const finalRoute = finalAtsRoute ?? parseApplicationUrl(finalUrl, normalizedHost, code);
  if (finalRoute === null) fail('UNSUPPORTED_REDIRECT');
  if (finalRoute.platform !== 'employer_hosted'
      && finalRoute.platform !== 'greenhouse'
      && finalRoute.platform !== 'ashby') {
    fail('UNSUPPORTED_REDIRECT');
  }
  return immutable({
    platform: finalRoute.platform,
    applicationUrl: canonicalUrlForRoute(finalUrl, finalRoute),
    applicationHost: finalRoute.host,
    reclassified: finalRoute.platform !== 'employer_hosted',
  });
}

export function filterSupportedJobs(rows, options = undefined) {
  if (!Array.isArray(rows)) fail('INVALID_JOB_ROWS');
  const sharedHost = normalizeUrlOptions(options, 'INVALID_JOB_ROWS');
  const filtered = [];
  for (let index = 0; index < rows.length; index += 1) {
    const row = assertRecord(rows[index], 'INVALID_JOB_ROWS');
    if (!hasOwn(row, 'applicationUrl')) fail('INVALID_JOB_ROWS');
    const applicationUrl = ownValue(row, 'applicationUrl', 'INVALID_JOB_ROWS');
    if (typeof applicationUrl !== 'string') fail('INVALID_JOB_ROWS');
    const rowHost = hasOwn(row, 'verifiedEmployerHost')
      ? ownValue(row, 'verifiedEmployerHost', 'INVALID_JOB_ROWS')
      : sharedHost;
    const platform = classifyApplicationUrl(
      applicationUrl,
      rowHost === undefined ? undefined : { verifiedEmployerHost: rowHost },
    );
    if (platform === null) continue;
    const cloned = deepClone(row, `rows[${index}]`);
    cloned.platform = platform;
    filtered.push(deepFreeze(cloned));
  }
  return deepFreeze(filtered);
}

export function extractPlatformJobSnapshot(value) {
  const code = 'INVALID_SNAPSHOT_INPUT';
  assertRecord(value, code);
  assertExactKeys(value, new Set(['applicationUrl', 'verifiedEmployerHost', 'payload']), code);
  const applicationUrl = ownValue(value, 'applicationUrl', code);
  assertSafeString(applicationUrl, code, { max: URL_MAX, trim: true });
  const verifiedEmployerHost = hasOwn(value, 'verifiedEmployerHost')
    ? ownValue(value, 'verifiedEmployerHost', code)
    : undefined;
  const normalizedHost = verifiedEmployerHost === undefined
    ? undefined
    : normalizeVerifiedEmployerHost(verifiedEmployerHost, code);
  const route = parseApplicationUrl(applicationUrl, normalizedHost, code);
  if (route === null) fail('UNSUPPORTED_APPLICATION_URL');
  const payload = assertRecord(ownValue(value, 'payload', code), 'INVALID_PAYLOAD');
  if (route.platform === 'greenhouse') return snapshotForGreenhouse(applicationUrl, route, payload);
  if (route.platform === 'ashby') return snapshotForAshby(applicationUrl, route, payload);
  return snapshotForNormalizedSource(applicationUrl, route.host, route, payload);
}
export function planPlatformApplication(value) {
  const normalized = normalizePlanInput(value);
  const actions = [];
  const unresolved = [];
  const finalCandidateRef = planFinalCandidate(normalized.controls);
  for (const control of normalized.controls) {
    if (control.candidateClass === 'final_candidate' || isNonFieldCandidate(control)) continue;
    if (control.fieldId === null) continue;
    if (control.candidateClass === 'unknown') {
      unresolved.push({ fieldId: control.fieldId, reason: 'unknown_control' });
      continue;
    }
    const kind = controlPlanKind(normalized.platform, control);
    if (kind === null) {
      unresolved.push({ fieldId: control.fieldId, reason: 'unsupported_widget' });
      continue;
    }
    const answer = normalized.answers.get(control.fieldId);
    if (!control.visible || !control.enabled) {
      if (answer !== undefined) {
        unresolved.push({ fieldId: control.fieldId, reason: 'control_unavailable' });
      }
      continue;
    }
    if (control.controlReference === null) fail('INVALID_OBSERVATION');
    if (control.readonly) {
      if (answer === undefined) {
        if (!control.valuePresent && control.required) {
          unresolved.push({ fieldId: control.fieldId, reason: 'control_unavailable' });
        }
        continue;
      }
      if (!readonlyMatches(kind, control, answer.value)) fail('READONLY_MISMATCH');
      continue;
    }
    if (answer === undefined) {
      normalizeMissingAnswer(control, kind, unresolved);
      continue;
    }
    const action = actionFor(normalized.platform, kind, control, answer, normalized.resumeUploadPath);
    if (action !== null) actions.push(action);
  }
  return immutable({
    schema: 'deterministic-platform-plan-v1',
    platform: normalized.platform,
    adapter: `${normalized.platform}_v1`,
    observationId: normalized.observationId,
    actions,
    unresolved,
    finalCandidateRef,
  });
}
