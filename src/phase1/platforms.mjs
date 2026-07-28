const SUPPORTED_PLATFORMS = Object.freeze(['greenhouse', 'ashby']);
const SUPPORTED_PLATFORM_SET = new Set(SUPPORTED_PLATFORMS);
const ANSWER_SOURCES = new Set(['memory', 'profile', 'resume', 'agent_inference', 'user']);
const SOURCE_MAX = 128;
const ID_MAX = 512;
const TEXT_MAX = 16 * 1024;
const URL_MAX = 8192;
const HTML_MAX = 4 * 1024 * 1024;
const GREENHOUSE_HOSTS = new Set(['job-boards.greenhouse.io', 'boards.greenhouse.io']);
const ASHBY_HOST = 'jobs.ashbyhq.com';
const GREENHOUSE_PATH = /^\/([A-Za-z0-9][A-Za-z0-9_-]*)\/jobs\/([1-9][0-9]*)$/u;
const ASHBY_PATH = /^\/([A-Za-z0-9][A-Za-z0-9_-]*)\/([0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$/iu;
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
  return deepFreeze(value);
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

function parseApplicationUrl(value) {
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
  if (raw === null || raw.authority.length === 0 || raw.authority.toLowerCase() !== parsed.hostname) return null;
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
  return null;
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

function snapshotForGreenhouse(applicationUrl, route, payload) {
  const externalJobId = normalizeExternalId(ownValue(payload, 'id', 'INVALID_PAYLOAD_ID'), 'greenhouse');
  if (externalJobId !== route.externalJobId) fail('PAYLOAD_ID_MISMATCH');
  const absoluteUrl = requirePayloadString(payload, 'absolute_url');
  const absoluteRoute = parseApplicationUrl(absoluteUrl);
  if (absoluteRoute?.platform !== 'greenhouse' || absoluteRoute.externalJobId !== route.externalJobId) {
    fail('PAYLOAD_URL_MISMATCH');
  }
  const location = requirePayloadRecord(payload, 'location');
  const locationName = normalizeText(requirePayloadString(location, 'name'), 'INVALID_PAYLOAD');
  const content = requirePayloadString(payload, 'content');
  const description = htmlToText(content);
  if (description.length === 0) fail('INVALID_PAYLOAD_DESCRIPTION');
  return immutable({
    schema: 'platform-job-snapshot-v1',
    platform: 'greenhouse',
    applicationUrl,
    externalJobId,
    title: normalizeText(requirePayloadString(payload, 'title'), 'INVALID_PAYLOAD'),
    company: normalizeText(requirePayloadString(payload, 'company_name'), 'INVALID_PAYLOAD'),
    location: locationName,
    description,
  });
}

function snapshotForAshby(applicationUrl, route, payload) {
  const posting = requirePayloadRecord(payload, 'jobPosting');
  const externalJobId = normalizeExternalId(ownValue(posting, 'id', 'INVALID_PAYLOAD_ID'), 'ashby');
  if (externalJobId.toLowerCase() !== route.externalJobId.toLowerCase()) fail('PAYLOAD_ID_MISMATCH');
  const postingUrl = requirePayloadString(posting, 'applicationUrl');
  const postingRoute = parseApplicationUrl(postingUrl);
  if (postingRoute?.platform !== 'ashby'
      || postingRoute.externalJobId.toLowerCase() !== route.externalJobId.toLowerCase()) {
    fail('PAYLOAD_URL_MISMATCH');
  }
  const descriptionHtml = requirePayloadString(posting, 'descriptionHtml');
  const description = htmlToText(descriptionHtml);
  if (description.length === 0) fail('INVALID_PAYLOAD_DESCRIPTION');
  return immutable({
    schema: 'platform-job-snapshot-v1',
    platform: 'ashby',
    applicationUrl,
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

function normalizeControl(value, index) {
  const code = 'INVALID_OBSERVATION';
  assertRecord(value, code);
  const fieldId = aliasValue(value, ['fieldId', 'field_id', 'stableId', 'stable_id'], code);
  if (fieldId !== null && fieldId !== undefined) assertSafeString(fieldId, code, { identifier: true, max: ID_MAX });
  const controlReference = aliasValue(value, ['controlReference', 'control_reference', 'ref', 'reference'], code);
  if (controlReference !== null && controlReference !== undefined) {
    assertSafeString(controlReference, code, { identifier: true, max: ID_MAX });
  }
  const name = aliasValue(value, ['name'], code);
  const label = aliasValue(value, ['label'], code);
  const kind = aliasValue(value, ['kind'], code);
  const type = aliasValue(value, ['type', 'inputType', 'input_type'], code);
  const role = aliasValue(value, ['role'], code);
  for (const [candidate, allowNull] of [[name, true], [label, true], [kind, true], [type, true], [role, true]]) {
    if (candidate !== undefined && candidate !== null && typeof candidate !== 'string') fail(code);
    if (!allowNull && candidate === null) fail(code);
  }
  const options = aliasValue(value, ['options'], code);
  if (options !== undefined && !Array.isArray(options)) fail(code);
  const normalizedOptions = (options ?? []).map((option, optionIndex) => {
    assertRecord(option, code);
    const optionLabel = ownValue(option, 'label', code);
    const optionValue = ownValue(option, 'value', code);
    assertSafeString(optionLabel, code, { max: TEXT_MAX });
    assertSafeString(optionValue, code, { max: TEXT_MAX });
    return { label: optionLabel, value: optionValue, optionIndex };
  });
  const candidate = aliasValue(value, ['candidateClass', 'candidate_class', 'class'], code);
  const nestedCandidate = aliasValue(value, ['candidate'], code);
  let candidateClass = candidate;
  if (nestedCandidate !== undefined && nestedCandidate !== null) {
    assertRecord(nestedCandidate, code);
    const nestedClass = aliasValue(nestedCandidate, ['class', 'candidateClass', 'candidate_class'], code);
    if (nestedClass !== undefined) {
      if (candidateClass !== undefined && candidateClass !== null && candidateClass !== nestedClass) fail(code);
      candidateClass = nestedClass;
    }
  }
  if (candidateClass !== undefined && candidateClass !== null && typeof candidateClass !== 'string') fail(code);
  const required = aliasValue(value, ['required'], code);
  if (required !== undefined && typeof required !== 'boolean') fail(code);
  const visible = aliasValue(value, ['visible'], code);
  if (visible !== undefined && typeof visible !== 'boolean') fail(code);
  const enabled = aliasValue(value, ['enabled'], code);
  if (enabled !== undefined && typeof enabled !== 'boolean') fail(code);
  if (fieldId === undefined) fail(code, `control ${index} is missing fieldId`);
  return {
    fieldId: fieldId ?? null,
    controlReference: controlReference ?? null,
    name: typeof name === 'string' ? name : '',
    label: typeof label === 'string' ? label : '',
    kind: typeof kind === 'string' ? kind.toLowerCase() : '',
    type: typeof type === 'string' ? type.toLowerCase() : '',
    role: typeof role === 'string' ? role.toLowerCase() : '',
    options: normalizedOptions,
    candidateClass: candidateClass ?? null,
    required: required === undefined ? false : required,
    visible: visible !== false,
    enabled: enabled !== false,
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
  const byLabel = control.options.filter((option) => option.label === answerValue);
  const byValue = control.options.filter((option) => option.value === answerValue);
  const matches = [...new Map([...byLabel, ...byValue].map((option) => [option.optionIndex, option])).values()];
  if (matches.length !== 1) fail('INVALID_OPTION_MAPPING');
  return matches[0].value;
}

function controlPlanKind(platform, control) {
  if (control.type === 'file' || control.kind === 'file') return 'file';
  if (platform === 'greenhouse') {
    if (control.kind === 'select' || control.type === 'select') return 'select';
    if (control.kind === 'radio' || control.type === 'radio' || control.role === 'radio') return 'radio';
    if (control.kind === 'textarea' || control.type === 'textarea') return 'textarea';
    if (control.kind === 'input' && new Set(['', 'text', 'email', 'tel', 'url', 'number', 'date', 'password']).has(control.type)) {
      return 'input';
    }
    return null;
  }
  if (control.role === 'combobox') return 'combobox';
  if (control.role === 'radiogroup') return 'yes_no';
  if (control.kind === 'textarea' || control.type === 'textarea') return 'textarea';
  if (control.kind === 'input' && !new Set(['radio', 'checkbox', 'select', 'file']).has(control.type)) return 'input';
  return null;
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
  if (kind === 'select' || kind === 'combobox') {
    return {
      ...base,
      operation: 'select_option',
      mechanic: kind === 'select' ? 'greenhouse_native_select' : 'ashby_combobox_exact_option',
      value: optionValueFor(control, answer.value),
    };
  }
  if (kind === 'radio' || kind === 'yes_no') {
    return {
      ...base,
      operation: 'toggle',
      mechanic: kind === 'radio' ? 'greenhouse_native_radio' : 'ashby_yes_no',
      value: optionValueFor(control, answer.value),
    };
  }
  fail('UNSUPPORTED_CONTROL');
}

function normalizePlanInput(value) {
  const code = 'INVALID_PLAN_INPUT';
  assertRecord(value, code);
  assertExactKeys(value, new Set(['platform', 'observation', 'answers', 'resumeUploadPath']), code);
  const platform = ownValue(value, 'platform', code);
  if (typeof platform !== 'string' || !SUPPORTED_PLATFORM_SET.has(platform)) fail('INVALID_PLATFORM');
  const observation = assertRecord(ownValue(value, 'observation', code), 'INVALID_OBSERVATION');
  const observationId = aliasValue(observation, ['observationId', 'observation_id'], 'INVALID_OBSERVATION');
  assertSafeString(observationId, 'INVALID_OBSERVATION', { identifier: true, max: ID_MAX });
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
  return { platform, observationId, controls: normalizedControls, answers: normalizedAnswers, resumeUploadPath };
}

export function classifyApplicationUrl(url) {
  return parseApplicationUrl(url)?.platform ?? null;
}

export function canonicalizeApplicationUrl(url) {
  const route = parseApplicationUrl(url);
  if (route === null) return null;
  return `https://${route.host}${new URL(url).pathname}`;
}

export function filterSupportedJobs(rows) {
  if (!Array.isArray(rows)) fail('INVALID_JOB_ROWS');
  const filtered = [];
  for (let index = 0; index < rows.length; index += 1) {
    const row = assertRecord(rows[index], 'INVALID_JOB_ROWS');
    if (!hasOwn(row, 'applicationUrl')) fail('INVALID_JOB_ROWS');
    const applicationUrl = ownValue(row, 'applicationUrl', 'INVALID_JOB_ROWS');
    if (typeof applicationUrl !== 'string') fail('INVALID_JOB_ROWS');
    const platform = classifyApplicationUrl(applicationUrl);
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
  assertExactKeys(value, new Set(['applicationUrl', 'payload']), code);
  const applicationUrl = ownValue(value, 'applicationUrl', code);
  assertSafeString(applicationUrl, code, { max: URL_MAX, trim: true });
  const route = parseApplicationUrl(applicationUrl);
  if (route === null) fail('UNSUPPORTED_APPLICATION_URL');
  const payload = assertRecord(ownValue(value, 'payload', code), 'INVALID_PAYLOAD');
  return route.platform === 'greenhouse'
    ? snapshotForGreenhouse(applicationUrl, route, payload)
    : snapshotForAshby(applicationUrl, route, payload);
}

export function planPlatformApplication(value) {
  const normalized = normalizePlanInput(value);
  const actions = [];
  const unresolved = [];
  let finalCandidateRef = null;
  for (const control of normalized.controls) {
    const isFinal = control.candidateClass === 'final_candidate';
    if (isFinal) {
      if (finalCandidateRef === null && control.visible && control.enabled) {
        if (control.controlReference === null) fail('INVALID_OBSERVATION');
        finalCandidateRef = control.controlReference;
      }
      continue;
    }
    if (control.fieldId === null) continue;
    const kind = controlPlanKind(normalized.platform, control);
    if (kind === null) fail('UNSUPPORTED_CONTROL');
    const answer = normalized.answers.get(control.fieldId);
    if (answer === undefined) {
      if (control.required) {
        unresolved.push({
          fieldId: control.fieldId,
          reason: kind === 'textarea' ? 'inference_required' : 'answer_required',
        });
      }
      continue;
    }
    if (!control.visible || !control.enabled) {
      unresolved.push({ fieldId: control.fieldId, reason: 'control_unavailable' });
      continue;
    }
    actions.push(actionFor(normalized.platform, kind, control, answer, normalized.resumeUploadPath));
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
