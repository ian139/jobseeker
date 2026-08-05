import { createHash } from 'node:crypto';

import { normalizeAction } from './browser-adapter.mjs';
import { validateApplicationDecision } from './decision.mjs';
import {
  digestObservedValue,
  digestPrivateValue,
  validateLedger,
  validateObservation,
} from './ledger.mjs';

export const ACTION_PLAN_SCHEMA = 'phase1-browser-action-plan-v2';
export const LEGACY_ACTION_PLAN_SCHEMA = 'phase1-browser-action-plan-v1';
export const ACTION_RESULT_SCHEMA = 'phase1-browser-action-result-v1';

const DRIVERS = Object.freeze(['omp_browser', 'playwright_cli', 'computer']);
const DRIVER_SET = new Set(DRIVERS);
const FALLBACK_ORDER = Object.freeze(['omp_browser', 'playwright_cli', 'computer']);
const ACTIONS = new Set(['fill_text', 'clear', 'select_option', 'toggle', 'upload_file']);
const ACTION_RESULT_OUTCOMES = new Set(['succeeded', 'failed', 'blocked', 'retry', 'stale']);
const HELPERS = new Set(['fill', 'select', 'click', 'uploadFile', 'click_exact_option']);
const RETENTION_KINDS = new Set([
  'exact_value',
  'normalized_option',
  'greenhouse_phone_country',
  'upload_file',
  'semantic_blank',
]);
const MAX_IDENTIFIER = 512;
const MAX_STRING = 16 * 1024;
const MAX_ACTIONS = 3;
const MAX_STEPS = 3;
const CUSTOM_SELECT_WAIT_MS = 15_000;
const OPTION_VISIBLE_STATE = 'exact_option_visible';
const SELECTION_STABLE_STATE = 'custom_select_committed_menu_closed';
const MAX_ERROR_CODE = 128;
const SHA256 = /^[a-f0-9]{64}$/u;
const ISO_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/u;
const BASENAME = /^(?!\.{1,2}$)(?!\s)[^/\\\u0000-\u001f\u007f]{1,255}$/u;

export class ActionPlanError extends TypeError {
  constructor(code, location = '$', message = code) {
    super(`${location}: ${message}`);
    this.name = 'ActionPlanError';
    this.code = code;
    this.location = location;
  }
}

function fail(code, location, message = code) {
  throw new ActionPlanError(code, location, message);
}

function isObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function hasOwn(value, key) {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function exactKeys(value, keys, location) {
  if (!isObject(value)) fail('INVALID_OBJECT', location);
  const allowed = new Set(keys);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) fail('UNKNOWN_KEY', `${location}.${key}`);
  }
}

function requiredKeys(value, keys, location) {
  for (const key of keys) {
    if (!hasOwn(value, key)) fail('MISSING_KEY', `${location}.${key}`);
  }
}

function safeString(value, location, {
  max = null,
  nullable = false,
  identifier = false,
  pattern = null,
  allowEmpty = false,
} = {}) {
  if (nullable && value === null) return value;
  const limit = max ?? (identifier ? MAX_IDENTIFIER : MAX_STRING);
  if (typeof value !== 'string' || (!allowEmpty && value.length === 0) || value.length > limit) {
    fail('INVALID_STRING', location);
  }
  if (/[\u0000-\u001f\u007f]/u.test(value)) fail('INVALID_STRING', location);
  if (identifier && value.trim() !== value) fail('INVALID_STRING', location);
  if (pattern !== null && !pattern.test(value)) fail('INVALID_STRING', location);
  return value;
}

function nullableString(value, location, options = {}) {
  return safeString(value, location, { ...options, nullable: true });
}

function boundedArray(value, location, { min = 0, max = 64 } = {}) {
  if (!Array.isArray(value) || value.length < min || value.length > max) fail('INVALID_ARRAY', location);
  return value;
}

function boundedInteger(value, location, minimum = 0) {
  if (!Number.isSafeInteger(value) || value < minimum) fail('INVALID_INTEGER', location);
  return value;
}

function digest(value) {
  return digestPrivateValue(value);
}

function sha256(value, location, { nullable = false } = {}) {
  if (nullable && value === null) return null;
  if (typeof value !== 'string' || !SHA256.test(value)) fail('INVALID_DIGEST', location);
  return value;
}

function timestamp(value, location) {
  safeString(value, location, { max: 32 });
  if (!ISO_TIMESTAMP.test(value) || Number.isNaN(Date.parse(value))) fail('INVALID_TIMESTAMP', location);
  return value;
}

function clone(value) {
  try {
    return structuredClone(value);
  } catch (error) {
    fail('UNSAFE_VALUE', '$', error instanceof Error ? error.message : 'value cannot be copied');
  }
}

function freeze(value, seen = new Set()) {
  if (value === null || typeof value !== 'object' || seen.has(value)) return value;
  seen.add(value);
  for (const child of Object.values(value)) freeze(child, seen);
  return Object.freeze(value);
}

function immutable(value) {
  return freeze(clone(value));
}

function deepEqual(left, right) {
  if (Object.is(left, right)) return true;
  if (typeof left !== typeof right || left === null || right === null) return false;
  if (Array.isArray(left)) {
    return Array.isArray(right)
      && left.length === right.length
      && left.every((item, index) => deepEqual(item, right[index]));
  }
  if (typeof left !== 'object') return false;
  const leftKeys = Object.keys(left);
  const rightKeys = Object.keys(right);
  if (leftKeys.length !== rightKeys.length) return false;
  return leftKeys.every((key) => hasOwn(right, key) && deepEqual(left[key], right[key]));
}

function normalizeBrowserAction(input, location) {
  try {
    return normalizeAction(input);
  } catch (error) {
    fail('INVALID_NORMALIZED_ACTION', location, error instanceof Error ? error.message : 'invalid browser action');
  }
}


function currentFieldId(decision) {
  return decision?.fieldId ?? decision?.field_id ?? null;
}

function currentControlReference(decision) {
  return decision?.controlReference ?? decision?.control_reference ?? null;
}


function controlKinds(control) {
  return [control?.kind, control?.tag, control?.type, control?.role]
    .filter((value) => typeof value === 'string')
    .map((value) => value.toLowerCase());
}

function isNativeSelect(control) {
  const kinds = controlKinds(control);
  return String(control?.tag ?? '').toLowerCase() === 'select'
    || (kinds.includes('select') && !kinds.includes('combobox') && !kinds.includes('custom'));
}

function isCustomSelect(control) {
  const kinds = controlKinds(control);
  return String(control?.tag ?? '').toLowerCase() !== 'select'
    && (kinds.includes('combobox')
      || kinds.includes('listbox')
      || kinds.some((kind) => kind.includes('custom') || kind.includes('react')));
}


function isTextControl(control) {
  const kinds = controlKinds(control);
  if (kinds.some((kind) => ['checkbox', 'radio', 'switch', 'file', 'select', 'button'].includes(kind))) return false;
  return kinds.some((kind) => [
    'input',
    'textarea',
    'text',
    'textbox',
    'searchbox',
    'search',
    'email',
    'tel',
    'url',
    'number',
    'password',
    'combobox',
  ].includes(kind));
}

function isToggleControl(control) {
  const kinds = controlKinds(control);
  return kinds.some((kind) => ['checkbox', 'radio', 'switch'].includes(kind))
    || (kinds.includes('input') && ['checkbox', 'radio'].includes(String(control.type).toLowerCase()));
}

function isUploadControl(control) {
  const kinds = controlKinds(control);
  return kinds.includes('file') || String(control.type ?? '').toLowerCase() === 'file';
}

function controlsOf(observation) {
  return Array.isArray(observation?.controls) ? observation.controls : [];
}

function selectorFromControl(control, controls, location) {
  const locator = control?.locator;
  if (!isObject(locator)) fail('UNSAFE_SELECTOR', `${location}.locator`);
  exactKeys(locator, ['strategy', 'value', 'role', 'name'], `${location}.locator`);
  requiredKeys(locator, ['strategy', 'value', 'role', 'name'], `${location}.locator`);
  const strategy = safeString(locator.strategy, `${location}.locator.strategy`, { max: 32, identifier: true });
  const value = safeString(locator.value, `${location}.locator.value`);
  if (!['test_id', 'id', 'name'].includes(strategy)) fail('UNSAFE_SELECTOR', `${location}.locator.strategy`);
  const matches = controls.filter((candidate) => (
    candidate?.locator?.strategy === strategy && candidate?.locator?.value === value
  ));
  if (matches.length !== 1) fail('NON_UNIQUE_SELECTOR', `${location}.locator`);
  if (strategy === 'test_id') return `[data-testid=${JSON.stringify(value)}]`;
  if (strategy === 'name') return `[name=${JSON.stringify(value)}]`;
  return `[id=${JSON.stringify(value)}]`;
}

function findCurrentBinding(observation, ledger, fieldId, controlReference, location) {
  if (!isObject(observation) || !isObject(ledger)) fail('INVALID_CONTEXT', location);
  const controls = controlsOf(observation);
  const controlsByRef = controls.filter((control) => control?.ref === controlReference);
  if (controlsByRef.length !== 1) fail('STALE_BINDING', `${location}.control_reference`);
  const control = controlsByRef[0];
  if (control.stable_id === undefined || control.stable_id === null) fail('STALE_BINDING', location);
  if (control.visible === false || control.enabled === false || control.disabled === true) {
    fail('DISABLED_CONTROL', location);
  }
  if (control.candidate?.class !== 'field') fail('NON_FIELD_CONTROL', location);
  const fields = Array.isArray(ledger.fields) ? ledger.fields : [];
  const field = fields.find((candidate) => candidate.field_id === fieldId);
  if (!field) fail('UNKNOWN_FIELD', `${location}.field_id`);
  if (ledger.latest_observation_id !== observation.observation_id
      || field.latest_observation_id !== observation.observation_id
      || field.latest_ref !== control.ref
      || field.present_in_latest_observation !== true
      || field.reachable !== true
      || !field.ref_history.some((item) => (
        item.observation_id === observation.observation_id && item.ref === control.ref
      ))) {
    fail('STALE_BINDING', location);
  }
  if (field.final === true) fail('FINAL_FIELD_REJECTED', location);
  return { control, field, controls };
}

function validateCurrentObservationAndLedger(observation, ledger, location = '$context') {
  try {
    validateObservation(observation);
    validateLedger(ledger);
  } catch (error) {
    fail('INVALID_CONTEXT', location, error instanceof Error ? error.message : 'invalid observation or ledger');
  }
  if (ledger.latest_observation_id !== observation.observation_id) fail('STALE_OBSERVATION', location);
  return true;
}

function validateAliasValue(value, location) {
  if (value !== null && typeof value !== 'string' && typeof value !== 'boolean') {
    fail('INVALID_ALIAS_VALUE', location);
  }
  if (typeof value === 'string') safeString(value, location);
  return value;
}

function validateAnswerAliases(value, fieldIds, location = 'answerAliases') {
  if (value === undefined) return {};
  exactKeys(value, Object.keys(value), location);
  const allowed = new Set(fieldIds);
  for (const fieldId of Object.keys(value)) {
    safeString(fieldId, `${location}.${fieldId}`, { identifier: true });
    if (!allowed.has(fieldId)) fail('UNKNOWN_FIELD', `${location}.${fieldId}`);
    const alias = value[fieldId];
    exactKeys(alias, ['alias', 'value'], `${location}.${fieldId}`);
    requiredKeys(alias, ['alias', 'value'], `${location}.${fieldId}`);
    safeString(alias.alias, `${location}.${fieldId}.alias`);
    validateAliasValue(alias.value, `${location}.${fieldId}.value`);
  }
  return value;
}

function validateOptionMatches(value, fieldIds, location = 'optionMatches') {
  if (value === undefined) return {};
  exactKeys(value, Object.keys(value), location);
  const allowed = new Set(fieldIds);
  for (const fieldId of Object.keys(value)) {
    safeString(fieldId, `${location}.${fieldId}`, { identifier: true });
    if (!allowed.has(fieldId)) fail('UNKNOWN_FIELD', `${location}.${fieldId}`);
    const match = value[fieldId];
    exactKeys(match, ['option_text', 'option_value'], `${location}.${fieldId}`);
    requiredKeys(match, ['option_text', 'option_value'], `${location}.${fieldId}`);
    safeString(match.option_text, `${location}.${fieldId}.option_text`);
    safeString(match.option_value, `${location}.${fieldId}.option_value`);
  }
  return value;
}

function validateRetryOf(value, fieldIds, location = 'retryOf') {
  if (value === undefined) return {};
  exactKeys(value, Object.keys(value), location);
  const allowed = new Set(fieldIds);
  for (const fieldId of Object.keys(value)) {
    safeString(fieldId, `${location}.${fieldId}`, { identifier: true });
    if (!allowed.has(fieldId)) fail('UNKNOWN_FIELD', `${location}.${fieldId}`);
    if (value[fieldId] !== null) boundedInteger(value[fieldId], `${location}.${fieldId}`);
  }
  return value;
}

function validateResumeUpload(value, location = 'resumeUpload') {
  if (value === undefined || value === null) return null;
  exactKeys(value, ['path', 'sha256'], location);
  requiredKeys(value, ['path', 'sha256'], location);
  safeString(value.path, `${location}.path`, { max: MAX_STRING });
  sha256(value.sha256, `${location}.sha256`);
  return value;
}

function fileBasename(path, location) {
  const name = path.split(/[\\/]/u).at(-1);
  if (!name || !BASENAME.test(name)) fail('UPLOAD_BASENAME_MISMATCH', location);
  return name;
}

function validateDecision(decision, observation, field, control, optionMatch, location) {
  const validationControl = decision.proposedAction === 'select_option'
    && !isNativeSelect(control)
    && optionMatch !== undefined
    ? {
      ...control,
      options: [{
        value: optionMatch.option_value,
        label: optionMatch.option_text,
        disabled: false,
        selected: false,
      }],
    }
    : control;
  try {
    return validateApplicationDecision(decision, {
      currentObservationId: observation.observation_id,
      currentFieldId: field.field_id,
      currentControlReference: control.ref,
      currentField: field,
      currentControl: validationControl,
      allowedActions: [decision.proposedAction],
      allowedSources: [decision.answerSource],
    });
  } catch (error) {
    fail('INVALID_DECISION', location, error instanceof Error ? error.message : 'invalid application decision');
  }
}

function assertSupportedAction(decision, location) {
  const action = decision?.proposedAction;
  if (action === 'final_submit' || action === 'submit' || action === 'click_submit') {
    fail('FINAL_SUBMIT_REJECTED', location);
  }
  if (!ACTIONS.has(action)) fail('UNSUPPORTED_ACTION', location);
}

function assertCompatibleControl(action, control, location) {
  if (control.readonly === true) fail('READONLY_CONTROL', location);
  if (action === 'fill_text' && !isTextControl(control)) fail('CONTROL_ACTION_MISMATCH', location);
  if (action === 'clear' && !isTextControl(control)) fail('CONTROL_ACTION_MISMATCH', location);
  if (action === 'select_option' && !isNativeSelect(control) && !isCustomSelect(control)) {
    fail('CONTROL_ACTION_MISMATCH', location);
  }
  if (action === 'toggle' && !isToggleControl(control)) fail('CONTROL_ACTION_MISMATCH', location);
  if (action === 'upload_file' && !isUploadControl(control)) fail('CONTROL_ACTION_MISMATCH', location);
}

function optionValue(option, location) {
  if (!isObject(option)) fail('INVALID_OPTION', location);
  const value = option.value;
  const label = option.label;
  if (typeof value !== 'string' || typeof label !== 'string') fail('INVALID_OPTION', location);
  return { value, label };
}

function findOption(control, match, custom, location) {
  const options = boundedArray(control.options, `${location}.options`, { max: 256 });
  if (custom && options.length === 0) {
    return optionValue(
      { value: match.option_value, label: match.option_text },
      location,
    );
  }

  const enabled = options.filter((option) => option?.disabled !== true);
  if (custom) {
    const labels = enabled.map((option, index) => optionValue(option, `${location}.options[${index}]`).label);
    if (new Set(labels).size !== labels.length) fail('NON_UNIQUE_OPTION', `${location}.options`);
    const target = labels.filter((label) => label === match.option_text);
    if (target.length !== 1) fail(target.length === 0 ? 'INVALID_OPTION' : 'NON_UNIQUE_OPTION', location);
    return optionValue(
      { value: match.option_value, label: match.option_text },
      location,
    );
  }

  const target = enabled.filter((option, index) => {
    const normalized = optionValue(option, `${location}.options[${index}]`);
    return normalized.value === match.option_value && normalized.label === match.option_text;
  });
  if (target.length !== 1) fail(target.length === 0 ? 'INVALID_OPTION' : 'NON_UNIQUE_OPTION', location);
  return optionValue(target[0], location);
}

function actionPrivateValue(action) {
  const first = action.steps[0]?.normalized_action;
  if (action.semantic_action === 'fill_text') return first?.value ?? null;
  if (action.semantic_action === 'clear') return action.steps[0]?.value ?? null;
  if (action.semantic_action === 'select_option') {
    const selectStep = action.steps.find((step) => step.normalized_action?.action === 'select_option');
    return selectStep?.option_value
      ?? action.steps.find((step) => step.option_value !== null)?.option_value
      ?? null;
  }
  if (action.semantic_action === 'toggle') return first?.checked ?? null;
  if (action.semantic_action === 'upload_file') return first?.filePath ?? null;
  return null;
}


function canonicalLedgerAction(action) {
  if (action.semantic_action === 'fill_text') return 'fill';
  if (action.semantic_action === 'clear') return 'clear';
  if (action.semantic_action === 'select_option') return 'select';
  if (action.semantic_action === 'toggle') {
    return action.steps[0].normalized_action.checked === true ? 'check' : 'uncheck';
  }
  if (action.semantic_action === 'upload_file') return 'upload';
  fail('UNSUPPORTED_ACTION', 'action.semantic_action');
}

function retention(kind, expectedValueDigest, optionText, fileName, artifactSha256 = null) {
  return {
    kind,
    expected_value_digest: expectedValueDigest,
    option_text_digest: optionText === null ? null : digest(optionText),
    file_name: fileName,
    artifact_sha256: artifactSha256,
  };
}

function makeStep({
  sequence,
  helper,
  selector,
  value = null,
  optionValue: selectedOptionValue = null,
  filePath = null,
  optionText = null,
  exact = null,
  normalizedAction,
  waitAfter = null,
  reobserveAfter = null,
}) {
  return {
    sequence,
    helper,
    selector,
    value,
    option_value: selectedOptionValue,
    file_path: filePath,
    option_text: optionText,
    exact,
    normalized_action: normalizedAction,
    wait_after: waitAfter,
    reobserve_after: reobserveAfter,
  };
}

function buildNormalizedAction(action, observation, fieldId, controlReference, payload, location) {
  return normalizeBrowserAction({
    action,
    observationId: observation.observation_id,
    fieldId,
    controlReference,
    ...payload,
  }, location);
}

function assertResolution(field, control, action, privateValue, location) {
  if (action === 'clear') {
    if (field.answer_state !== 'blank' || field.value_digest !== null || field.semantic_choice === null) {
      fail('RESOLUTION_MISMATCH', location);
    }
    return;
  }
  if (field.answer_state !== 'answered' || field.value_digest === null) fail('RESOLUTION_MISMATCH', location);
  if (digestObservedValue(control, privateValue) !== field.value_digest) fail('DIGEST_MISMATCH', location);
}

function buildAction({ decision, observation, ledger, aliases, optionMatches, resumeUpload, retryOf, location }) {
  assertSupportedAction(decision, `${location}.proposedAction`);
  const fieldId = decision.fieldId;
  const controlReference = decision.controlReference;
  const { control, field, controls } = findCurrentBinding(
    observation,
    ledger,
    fieldId,
    controlReference,
    location,
  );
  assertCompatibleControl(decision.proposedAction, control, location);
  const selector = selectorFromControl(control, controls, location);
  const alias = aliases[fieldId];
  if (decision.proposedAction !== 'upload_file') {
    if (!alias) fail('ANSWER_ALIAS_REQUIRED', `${location}.answerAliases`);
  }
  const answerAlias = alias?.alias ?? null;
  const privateValue = alias?.value ?? null;
  let steps;
  let actionRetention;

  if (decision.proposedAction === 'fill_text') {
    if (typeof privateValue !== 'string') fail('INVALID_ALIAS_VALUE', `${location}.answerAliases.${fieldId}.value`);
    assertResolution(field, control, decision.proposedAction, privateValue, location);
    const normalizedAction = buildNormalizedAction(
      'fill_text',
      observation,
      fieldId,
      controlReference,
      { value: privateValue },
      `${location}.steps[0]`,
    );
    steps = [makeStep({
      sequence: 1,
      helper: 'fill',
      selector,
      value: privateValue,
      exact: true,
      normalizedAction,
    })];
    actionRetention = retention('exact_value', field.value_digest, null, null);
  } else if (decision.proposedAction === 'clear') {
    if (privateValue !== null && privateValue !== '') fail('INVALID_ALIAS_VALUE', `${location}.answerAliases.${fieldId}.value`);
    assertResolution(field, control, decision.proposedAction, privateValue, location);
    const normalizedAction = buildNormalizedAction(
      'clear',
      observation,
      fieldId,
      controlReference,
      {},
      `${location}.steps[0]`,
    );
    steps = [makeStep({
      sequence: 1,
      helper: 'fill',
      selector,
      value: '',
      exact: true,
      normalizedAction,
    })];
    actionRetention = retention('semantic_blank', null, null, null);
  } else if (decision.proposedAction === 'select_option') {
    const match = optionMatches[fieldId];
    if (!match) fail('OPTION_MATCH_REQUIRED', `${location}.optionMatches`);
    const custom = !isNativeSelect(control);
    const selected = findOption(control, match, custom, location);
    if (typeof privateValue !== 'string' || privateValue !== selected.value) {
      fail('INVALID_ALIAS_VALUE', `${location}.answerAliases.${fieldId}.value`);
    }
    assertResolution(field, control, decision.proposedAction, privateValue, location);
    if (custom) {
      const open = buildNormalizedAction(
        'click',
        observation,
        fieldId,
        controlReference,
        {},
        `${location}.steps[0]`,
      );
      const query = buildNormalizedAction(
        'fill_text',
        observation,
        fieldId,
        controlReference,
        { value: selected.label },
        `${location}.steps[1]`,
      );
      const click = buildNormalizedAction(
        'click',
        observation,
        fieldId,
        controlReference,
        {},
        `${location}.steps[2]`,
      );
      const optionReady = buildNormalizedAction(
        'wait',
        observation,
        fieldId,
        controlReference,
        { state: OPTION_VISIBLE_STATE, timeoutMs: CUSTOM_SELECT_WAIT_MS },
        `${location}.steps[1].wait_after`,
      );
      const selectionStable = buildNormalizedAction(
        'wait',
        observation,
        fieldId,
        controlReference,
        { state: SELECTION_STABLE_STATE, timeoutMs: CUSTOM_SELECT_WAIT_MS },
        `${location}.steps[2].wait_after`,
      );
      const reobserve = buildNormalizedAction(
        'reobserve',
        observation,
        fieldId,
        controlReference,
        {},
        `${location}.steps[2].reobserve_after`,
      );
      steps = [
        makeStep({
          sequence: 1,
          helper: 'click',
          selector,
          exact: true,
          normalizedAction: open,
        }),
        makeStep({
          sequence: 2,
          helper: 'fill',
          selector,
          value: selected.label,
          optionText: null,
          exact: true,
          normalizedAction: query,
          waitAfter: optionReady,
        }),
        makeStep({
          sequence: 3,
          helper: 'click_exact_option',
          selector,
          optionValue: selected.value,
          optionText: selected.label,
          exact: true,
          normalizedAction: click,
          waitAfter: selectionStable,
          reobserveAfter: reobserve,
        }),
      ];
    } else {
      const normalizedAction = buildNormalizedAction(
        'select_option',
        observation,
        fieldId,
        controlReference,
        { optionValue: selected.value },
        `${location}.steps[0]`,
      );
      steps = [makeStep({
        sequence: 1,
        helper: 'select',
        selector,
        optionValue: selected.value,
        optionText: selected.label,
        exact: true,
        normalizedAction,
      })];
    }
    const phoneCountry = /phone.*country|country.*phone/iu.test(
      `${control.label ?? ''} ${control.description ?? ''} ${field.label ?? ''} ${field.description ?? ''}`,
    );
    actionRetention = retention(
      phoneCountry ? 'greenhouse_phone_country' : 'normalized_option',
      field.value_digest,
      selected.label,
      null,
    );
  } else if (decision.proposedAction === 'toggle') {
    if (typeof privateValue !== 'boolean') fail('INVALID_ALIAS_VALUE', `${location}.answerAliases.${fieldId}.value`);
    assertResolution(field, control, decision.proposedAction, privateValue, location);
    const normalizedAction = buildNormalizedAction(
      'toggle',
      observation,
      fieldId,
      controlReference,
      { checked: privateValue },
      `${location}.steps[0]`,
    );
    steps = [makeStep({
      sequence: 1,
      helper: 'click',
      selector,
      exact: true,
      normalizedAction,
    })];
    actionRetention = retention('exact_value', field.value_digest, null, null);
  } else if (decision.proposedAction === 'upload_file') {
    if (!resumeUpload) fail('UPLOAD_IDENTITY_REQUIRED', `${location}.resumeUpload`);
    const basename = fileBasename(resumeUpload.path, `${location}.resumeUpload.path`);
    if (control.file !== null && isObject(control.file)) {
      if (control.file.count > 1) fail('UPLOAD_COUNT_INVALID', location);
      if (Array.isArray(control.file.names) && control.file.names.length > 0
          && !control.file.names.includes(basename)) {
        fail('UPLOAD_BASENAME_MISMATCH', location);
      }
    }
    assertResolution(field, control, decision.proposedAction, resumeUpload.path, location);
    const normalizedAction = buildNormalizedAction(
      'upload_file',
      observation,
      fieldId,
      controlReference,
      { filePath: resumeUpload.path },
      `${location}.steps[0]`,
    );
    steps = [makeStep({
      sequence: 1,
      helper: 'uploadFile',
      selector,
      filePath: resumeUpload.path,
      exact: true,
      normalizedAction,
    })];
    actionRetention = retention(
      'upload_file',
      field.value_digest,
      null,
      basename,
      resumeUpload.sha256,
    );
  } else {
    fail('UNSUPPORTED_ACTION', location);
  }

  const actionId = `action-${createHash('sha256')
    .update(`${observation.observation_id}:${fieldId}:${controlReference}`, 'utf8')
    .digest('hex')
    .slice(0, 24)}`;
  return {
    action_id: actionId,
    field_id: fieldId,
    stable_id: control.stable_id,
    control_reference: control.ref,
    answer_alias: answerAlias,
    semantic_action: decision.proposedAction,
    retry_of: retryOf[fieldId] ?? null,
    decision: clone(decision),
    steps,
    retention: actionRetention,
  };
}

function validateRetention(value, action, location) {
  exactKeys(
    value,
    ['kind', 'expected_value_digest', 'option_text_digest', 'file_name', 'artifact_sha256'],
    location,
  );
  requiredKeys(value, ['kind', 'expected_value_digest', 'option_text_digest', 'file_name', 'artifact_sha256'], location);
  safeString(value.kind, `${location}.kind`, { max: 64, identifier: true });
  if (!RETENTION_KINDS.has(value.kind)) fail('INVALID_RETENTION', location);
  sha256(value.expected_value_digest, `${location}.expected_value_digest`, { nullable: true });
  sha256(value.option_text_digest, `${location}.option_text_digest`, { nullable: true });
  nullableString(value.file_name, `${location}.file_name`, { max: 255 });
  if (value.file_name !== null && !BASENAME.test(value.file_name)) fail('INVALID_BASENAME', `${location}.file_name`);
  sha256(value.artifact_sha256, `${location}.artifact_sha256`, { nullable: true });
  if (action.semantic_action === 'upload_file') {
    if (value.kind !== 'upload_file' || value.file_name === null || value.artifact_sha256 === null
        || value.expected_value_digest === null || value.option_text_digest !== null) {
      fail('INVALID_RETENTION', location);
    }
  } else if (action.semantic_action === 'clear') {
    if (value.kind !== 'semantic_blank' || value.expected_value_digest !== null
        || value.option_text_digest !== null || value.file_name !== null || value.artifact_sha256 !== null) {
      fail('INVALID_RETENTION', location);
    }
  } else if (action.semantic_action === 'select_option') {
    if (!['normalized_option', 'greenhouse_phone_country'].includes(value.kind)
        || value.expected_value_digest === null || value.option_text_digest === null
        || value.file_name !== null || value.artifact_sha256 !== null) {
      fail('INVALID_RETENTION', location);
    }
  } else if (value.kind !== 'exact_value' || value.expected_value_digest === null
      || value.option_text_digest !== null || value.file_name !== null || value.artifact_sha256 !== null) {
    fail('INVALID_RETENTION', location);
  }
}

function validateFollowup(value, primary, expectedAction, location) {
  if (value === null) return null;
  const normalized = normalizeBrowserAction(value, location);
  if (!deepEqual(normalized, value) || normalized.action !== expectedAction) {
    fail('INVALID_STEP_FOLLOWUP', location);
  }
  if (normalized.observationId !== primary.observationId
      || normalized.fieldId !== primary.fieldId
      || normalized.controlReference !== primary.controlReference) {
    fail('STEP_BINDING_MISMATCH', location);
  }
  if (expectedAction === 'wait'
      && (!Number.isSafeInteger(normalized.timeoutMs) || normalized.timeoutMs <= 0)) {
    fail('INVALID_STEP_FOLLOWUP', location);
  }
  return normalized;
}

function validateStep(value, action, index, location, { legacy = false } = {}) {
  const keys = legacy
    ? [
      'sequence', 'helper', 'selector', 'value', 'option_value', 'file_path',
      'option_text', 'exact', 'normalized_action',
    ]
    : [
      'sequence', 'helper', 'selector', 'value', 'option_value', 'file_path',
      'option_text', 'exact', 'normalized_action', 'wait_after', 'reobserve_after',
    ];
  exactKeys(value, keys, location);
  requiredKeys(value, keys, location);
  boundedInteger(value.sequence, `${location}.sequence`, 1);
  safeString(value.helper, `${location}.helper`, { max: 64, identifier: true });
  if (!HELPERS.has(value.helper)) fail('INVALID_HELPER', `${location}.helper`);
  safeString(value.selector, `${location}.selector`);
  if (value.value !== null && typeof value.value !== 'string' && typeof value.value !== 'boolean') {
    fail('INVALID_STEP_ARGUMENT', `${location}.value`);
  }
  if (typeof value.value === 'string') {
    safeString(value.value, `${location}.value`, { allowEmpty: true });
  }
  nullableString(value.option_value, `${location}.option_value`);
  nullableString(value.file_path, `${location}.file_path`);
  nullableString(value.option_text, `${location}.option_text`);
  if (value.exact !== null && typeof value.exact !== 'boolean') fail('INVALID_STEP_ARGUMENT', `${location}.exact`);
  const legacyCustomEmptyFill = legacy
    && action.semantic_action === 'select_option'
    && value.helper === 'fill'
    && value.value === ''
    && value.normalized_action?.action === 'fill_text'
    && value.normalized_action?.value === '';
  const normalizationInput = legacyCustomEmptyFill
    ? { ...value.normalized_action, value: 'legacy-empty-query' }
    : value.normalized_action;
  let normalized = normalizeBrowserAction(normalizationInput, `${location}.normalized_action`);
  if (legacyCustomEmptyFill) normalized = { ...normalized, value: '' };
  if (!deepEqual(normalized, value.normalized_action)) fail('INVALID_NORMALIZED_ACTION', `${location}.normalized_action`);
  if (!legacy) {
    validateFollowup(value.wait_after, normalized, 'wait', `${location}.wait_after`);
    validateFollowup(value.reobserve_after, normalized, 'reobserve', `${location}.reobserve_after`);
  }
  if (value.value === '' && normalized.action !== 'clear' && !legacyCustomEmptyFill) {
    fail('INVALID_STEP_ARGUMENT', `${location}.value`);
  }
  if (normalized.observationId === undefined || normalized.fieldId === undefined
      || normalized.controlReference === undefined) {
    fail('INVALID_NORMALIZED_ACTION', `${location}.normalized_action`);
  }
  let expectedAction;
  if (action.semantic_action === 'fill_text' || action.semantic_action === 'clear') {
    expectedAction = action.semantic_action === 'clear' ? 'clear' : 'fill_text';
  } else if (action.semantic_action === 'select_option') {
    if (value.helper === 'select') expectedAction = 'select_option';
    else if (value.helper === 'click' || value.helper === 'click_exact_option') expectedAction = 'click';
    else expectedAction = value.value === '' && !legacyCustomEmptyFill ? 'clear' : 'fill_text';
  } else {
    expectedAction = action.semantic_action === 'toggle' ? 'toggle' : 'upload_file';
  }
  if (normalized.action !== expectedAction) fail('STEP_ACTION_MISMATCH', `${location}.normalized_action`);
  if (index === 0 && normalized.controlReference !== action.control_reference) {
    fail('STEP_BINDING_MISMATCH', `${location}.normalized_action.controlReference`);
  }
}
function validateActionShape(value, index, { legacy = false } = {}) {
  const location = `actions[${index}]`;
  exactKeys(
    value,
    ['action_id', 'field_id', 'stable_id', 'control_reference', 'answer_alias', 'semantic_action', 'retry_of', 'decision', 'steps', 'retention'],
    location,
  );
  requiredKeys(
    value,
    ['action_id', 'field_id', 'stable_id', 'control_reference', 'answer_alias', 'semantic_action', 'retry_of', 'decision', 'steps', 'retention'],
    location,
  );
  safeString(value.action_id, `${location}.action_id`, { identifier: true });
  safeString(value.field_id, `${location}.field_id`, { identifier: true });
  safeString(value.stable_id, `${location}.stable_id`, { identifier: true });
  safeString(value.control_reference, `${location}.control_reference`, { identifier: true });
  nullableString(value.answer_alias, `${location}.answer_alias`);
  safeString(value.semantic_action, `${location}.semantic_action`, { max: 64, identifier: true });
  if (!ACTIONS.has(value.semantic_action)) fail('UNSUPPORTED_ACTION', `${location}.semantic_action`);
  if (value.semantic_action !== 'upload_file' && value.answer_alias === null) {
    fail('ANSWER_ALIAS_REQUIRED', `${location}.answer_alias`);
  }
  if (value.retry_of !== null) boundedInteger(value.retry_of, `${location}.retry_of`);
  exactKeys(value.decision, [
    'observationId',
    'fieldId',
    'controlReference',
    'fieldPolicy',
    'proposedAnswer',
    'answerSource',
    'evidenceReferences',
    'inferenceRationaleDigest',
    'inferenceEvidenceDigests',
    'proposedAction',
    'expectedRetainedState',
    'modelTier',
    'confidence',
    'reasonCode',
    'reobservationRequired',
    'automaticSubmissionEligible',
  ], `${location}.decision`);
  let normalizedDecision;
  try {
    normalizedDecision = validateApplicationDecision(value.decision);
  } catch (error) {
    fail('INVALID_DECISION', `${location}.decision`, error instanceof Error ? error.message : 'invalid decision');
  }
  if (normalizedDecision.fieldId !== value.field_id
      || normalizedDecision.controlReference !== value.control_reference
      || normalizedDecision.proposedAction !== value.semantic_action) {
    fail('ACTION_DECISION_MISMATCH', location);
  }
  const steps = boundedArray(value.steps, `${location}.steps`, { min: 1, max: MAX_STEPS });
  const customSelectSteps = value.semantic_action === 'select_option'
    && (legacy ? (steps.length === 2 || steps.length === 3) : steps.length === 3);
  const expectedCount = customSelectSteps ? steps.length : 1;
  if (steps.length !== expectedCount) fail('INVALID_STEPS', `${location}.steps`);
  steps.forEach((step, stepIndex) => {
    validateStep(step, value, stepIndex, `${location}.steps[${stepIndex}]`, { legacy });
    if (step.sequence !== stepIndex + 1) fail('INVALID_STEP_SEQUENCE', `${location}.steps[${stepIndex}].sequence`);
    if (step.normalized_action.observationId !== value.decision.observationId
        || step.normalized_action.fieldId !== value.field_id
        || step.normalized_action.controlReference !== value.control_reference) {
      fail('STEP_BINDING_MISMATCH', `${location}.steps[${stepIndex}].normalized_action`);
    }
  });
  if (customSelectSteps && legacy && steps.length === 2) {
    if (steps[0].helper !== 'fill' || steps[1].helper !== 'click_exact_option') {
      fail('INVALID_STEPS', `${location}.steps`);
    }
  } else if (customSelectSteps && legacy && steps.length === 3) {
    const openFirst = steps[0].helper === 'click' && steps[1].helper === 'fill';
    if (!openFirst || steps[2].helper !== 'click_exact_option') {
      fail('INVALID_STEPS', `${location}.steps`);
    }
  } else if (customSelectSteps) {
    const openFirst = steps[0].helper === 'click' && steps[1].helper === 'fill';
    const optionWait = steps[1].wait_after?.state === OPTION_VISIBLE_STATE;
    const selectionWait = steps[2].wait_after?.state === SELECTION_STABLE_STATE;
    const freshObservation = steps[2].reobserve_after?.action === 'reobserve';
    if (!openFirst || steps[2].helper !== 'click_exact_option'
        || steps[0].wait_after !== null || steps[0].reobserve_after !== null
        || steps[1].reobserve_after !== null
        || !optionWait || !selectionWait || !freshObservation) {
      fail('INVALID_STEPS', `${location}.steps`);
    }
  } else {
    if (!legacy && steps.some((step) => step.wait_after !== null || step.reobserve_after !== null)) {
      fail('INVALID_STEPS', `${location}.steps`);
    }
    const helperByAction = {
      fill_text: 'fill',
      clear: 'fill',
      select_option: 'select',
      toggle: 'click',
      upload_file: 'uploadFile',
    };
    if (steps[0].helper !== helperByAction[value.semantic_action]) fail('INVALID_HELPER', `${location}.steps[0].helper`);
  }
  validateRetention(value.retention, value, `${location}.retention`);
}

function validatePlanShape(plan, { historical = false } = {}) {
  exactKeys(
    plan,
    ['schema', 'plan_id', 'created_at', 'ats', 'observation_id', 'driver', 'screenshot_sha256', 'mode', 'actions', 'fallback_order', 'reobserve_after'],
    '$',
  );
  requiredKeys(
    plan,
    ['schema', 'plan_id', 'created_at', 'ats', 'observation_id', 'driver', 'screenshot_sha256', 'mode', 'actions', 'fallback_order', 'reobserve_after'],
    '$',
  );
  const legacy = historical && plan.schema === LEGACY_ACTION_PLAN_SCHEMA;
  if (plan.schema !== ACTION_PLAN_SCHEMA && !legacy) fail('INVALID_SCHEMA', 'schema');
  safeString(plan.plan_id, 'plan_id', { identifier: true });
  timestamp(plan.created_at, 'created_at');
  safeString(plan.ats, 'ats', { identifier: true });
  safeString(plan.observation_id, 'observation_id', { identifier: true });
  safeString(plan.driver, 'driver', { max: 64, identifier: true });
  if (!DRIVER_SET.has(plan.driver)) fail('INVALID_DRIVER', 'driver');
  sha256(plan.screenshot_sha256, 'screenshot_sha256', { nullable: true });
  if (plan.driver === 'computer' && plan.screenshot_sha256 === null) {
    fail('COMPUTER_SCREENSHOT_REQUIRED', 'screenshot_sha256');
  }
  if (!['single_action', 'fill_batch'].includes(plan.mode)) fail('INVALID_MODE', 'mode');
  const actions = boundedArray(plan.actions, 'actions', { min: 1, max: MAX_ACTIONS });
  if (plan.mode === 'single_action' && actions.length !== 1) fail('INVALID_BATCH', 'actions');
  if (plan.mode === 'fill_batch' && (actions.length < 2 || actions.length > MAX_ACTIONS)) fail('INVALID_BATCH', 'actions');
  if (plan.mode === 'fill_batch' && actions.some((action) => action.semantic_action !== 'fill_text')) {
    fail('INVALID_BATCH', 'actions');
  }
  boundedArray(plan.fallback_order, 'fallback_order', { min: DRIVERS.length, max: DRIVERS.length });
  if (!deepEqual(plan.fallback_order, FALLBACK_ORDER)) fail('INVALID_FALLBACK_ORDER', 'fallback_order');
  if (plan.fallback_order.some((driver) => !DRIVER_SET.has(driver))) fail('INVALID_DRIVER', 'fallback_order');
  if (new Set(plan.fallback_order).size !== plan.fallback_order.length) fail('DUPLICATE_DRIVER', 'fallback_order');
  if (plan.reobserve_after !== true) fail('REOBSERVATION_REQUIRED', 'reobserve_after');
  const actionIds = new Set();
  const fieldIds = new Set();
  actions.forEach((action, index) => {
    validateActionShape(action, index, { legacy });
    if (actionIds.has(action.action_id)) fail('DUPLICATE_ACTION', `actions[${index}].action_id`);
    if (fieldIds.has(action.field_id)) fail('DUPLICATE_FIELD', `actions[${index}].field_id`);
    actionIds.add(action.action_id);
    fieldIds.add(action.field_id);
  });
  return legacy;
}


function validateRetryBinding(action, ledger, location) {
  if (action.retry_of === null) return;
  if (action.retry_of >= ledger.action_attempts.length) fail('RETRY_MISMATCH', location);
  const prior = ledger.action_attempts[action.retry_of];
  if (!prior || prior.field_id !== action.field_id || prior.action !== canonicalLedgerAction(action)
      || !['failed', 'blocked', 'retry', 'stale'].includes(prior.outcome)) {
    fail('RETRY_MISMATCH', location);
  }
}

function validatePlanContext(plan, observation, ledger) {
  validateCurrentObservationAndLedger(observation, ledger);
  if (plan.observation_id !== observation.observation_id
      || plan.observation_id !== ledger.latest_observation_id) {
    fail('STALE_OBSERVATION', 'observation_id');
  }
  const actionRefs = new Set();
  for (const [index, action] of plan.actions.entries()) {
    const location = `actions[${index}]`;
    const binding = findCurrentBinding(
      observation,
      ledger,
      action.field_id,
      action.control_reference,
      location,
    );
    if (binding.control.stable_id !== action.stable_id) fail('STALE_BINDING', `${location}.stable_id`);
    const expectedSelector = selectorFromControl(binding.control, binding.controls, location);
    if (action.steps.some((step) => step.selector !== expectedSelector)) {
      fail('SELECTOR_MISMATCH', `${location}.steps.selector`);
    }
    if (action.retention.expected_value_digest !== binding.field.value_digest) {
      fail('RESOLUTION_MISMATCH', `${location}.retention.expected_value_digest`);
    }
    if (action.decision.observationId !== observation.observation_id
        || action.decision.fieldId !== action.field_id
        || action.decision.controlReference !== action.control_reference) {
      fail('STALE_BINDING', `${location}.decision`);
    }
    if (actionRefs.has(action.control_reference)) fail('DUPLICATE_CONTROL_REFERENCE', location);
    actionRefs.add(action.control_reference);
    let optionMatch;
    if (action.semantic_action === 'select_option') {
      const optionStep = action.steps.find((step) => step.option_text !== null);
      const optionText = optionStep?.option_text;
      const optionValueText = optionStep?.option_value;
      if (optionText === null || optionText === undefined
          || optionValueText === null || optionValueText === undefined) {
        fail('INVALID_OPTION', location);
      }
      optionMatch = { option_text: optionText, option_value: optionValueText };
      const options = binding.control.options;
      const custom = !isNativeSelect(binding.control);
      const matched = options.filter((option) => (
        option.disabled !== true
          && option.label === optionText
          && (custom || option.value === optionValueText)
      ));
      if (!(matched.length === 1 || (custom && options.length === 0))) {
        fail('NON_UNIQUE_OPTION', location);
      }
      if (action.retention.option_text_digest !== digest(optionText)) {
        fail('INVALID_RETENTION', `${location}.retention.option_text_digest`);
      }
    }
    validateDecision(
      action.decision,
      observation,
      binding.field,
      binding.control,
      optionMatch,
      `${location}.decision`,
    );
    validateRetryBinding(action, ledger, `${location}.retry_of`);
    assertResolution(binding.field, binding.control, action.semantic_action, actionPrivateValue(action), location);
    if (action.semantic_action === 'upload_file') {
      const path = actionPrivateValue(action);
      const basename = fileBasename(path, `${location}.steps[0].file_path`);
      if (action.retention.file_name !== basename) fail('UPLOAD_BASENAME_MISMATCH', location);
      if (action.retention.artifact_sha256 === null) fail('UPLOAD_IDENTITY_REQUIRED', location);
    }
  }
  return true;
}

export function validateBrowserActionPlan(plan, context = {}) {
  if (context === undefined || context === null) {
    validatePlanShape(plan);
    return immutable(plan);
  }
  if (!isObject(context)) fail('INVALID_CONTEXT', '$context');
  exactKeys(context, ['observation', 'ledger', 'historical'], '$context');
  if (hasOwn(context, 'historical') && typeof context.historical !== 'boolean') {
    fail('INVALID_CONTEXT', '$context.historical');
  }
  const historical = context.historical === true;
  validatePlanShape(plan, { historical });
  if (hasOwn(context, 'observation') || hasOwn(context, 'ledger')) {
    if (!hasOwn(context, 'observation') || !hasOwn(context, 'ledger')) fail('INVALID_CONTEXT', '$context');
    validatePlanContext(plan, context.observation, context.ledger);
  }
  return immutable(plan);
}

export function createBrowserActionPlan(input) {
  exactKeys(
    input,
    ['observation', 'ledger', 'decisions', 'answerAliases', 'optionMatches', 'resumeUpload', 'driver', 'screenshotSha256', 'retryOf', 'createdAt', 'ats'],
    '$',
  );
  requiredKeys(input, ['observation', 'ledger', 'decisions', 'driver'], '$');
  try {
    validateCurrentObservationAndLedger(input.observation, input.ledger);
  } catch (error) {
    if (error instanceof ActionPlanError) throw error;
    throw error;
  }
  if (!DRIVER_SET.has(input.driver)) fail('INVALID_DRIVER', 'driver');
  if (input.driver === 'computer') sha256(input.screenshotSha256, 'screenshotSha256');
  else if (input.screenshotSha256 !== undefined && input.screenshotSha256 !== null) {
    sha256(input.screenshotSha256, 'screenshotSha256');
  }
  const decisions = boundedArray(input.decisions, 'decisions', { min: 1, max: MAX_ACTIONS });
  const fieldIds = decisions.map((decision, index) => {
    if (!isObject(decision)) fail('INVALID_DECISION', `decisions[${index}]`);
    const fieldId = currentFieldId(decision);
    if (typeof fieldId !== 'string' || fieldId.length === 0) fail('INVALID_DECISION', `decisions[${index}].fieldId`);
    return fieldId;
  });
  if (new Set(fieldIds).size !== fieldIds.length) fail('DUPLICATE_FIELD', 'decisions');
  const aliases = validateAnswerAliases(input.answerAliases, fieldIds);
  const optionMatches = validateOptionMatches(input.optionMatches, fieldIds);
  const retryOf = validateRetryOf(input.retryOf, fieldIds);
  const resumeUpload = validateResumeUpload(input.resumeUpload);
  const actions = decisions.map((decision, index) => {
    assertSupportedAction(decision, `decisions[${index}].proposedAction`);
    const fieldId = currentFieldId(decision);
    const controlReference = currentControlReference(decision);
    if (typeof controlReference !== 'string' || controlReference.length === 0) {
      fail('INVALID_DECISION', `decisions[${index}].controlReference`);
    }
    const { control, field } = findCurrentBinding(
      input.observation,
      input.ledger,
      fieldId,
      controlReference,
      `decisions[${index}]`,
    );
    const normalizedDecision = validateDecision(
      decision,
      input.observation,
      field,
      control,
      optionMatches[fieldId],
      `decisions[${index}]`,
    );
    return buildAction({
      decision: normalizedDecision,
      observation: input.observation,
      ledger: input.ledger,
      aliases,
      optionMatches,
      resumeUpload,
      retryOf,
      location: `decisions[${index}]`,
    });
  });
  const plan = {
    schema: ACTION_PLAN_SCHEMA,
    plan_id: `plan-${createHash('sha256')
      .update(`${input.observation.observation_id}:${actions.map((action) => action.action_id).join(',')}`, 'utf8')
      .digest('hex')
      .slice(0, 24)}`,
    created_at: input.createdAt ?? new Date().toISOString(),
    ats: input.ats ?? 'unknown',
    observation_id: input.observation.observation_id,
    driver: input.driver,
    screenshot_sha256: input.screenshotSha256 ?? null,
    mode: actions.length === 1 ? 'single_action' : 'fill_batch',
    actions,
    fallback_order: [...FALLBACK_ORDER],
    reobserve_after: true,
  };
  return validateBrowserActionPlan(plan, { observation: input.observation, ledger: input.ledger });
}

function validateOutcome(value, action, location) {
  exactKeys(value, ['action_id', 'outcome', 'error_code', 'driver', 'selected_option_text'], location);
  requiredKeys(value, ['action_id', 'outcome', 'error_code', 'driver', 'selected_option_text'], location);
  safeString(value.action_id, `${location}.action_id`, { identifier: true });
  safeString(value.outcome, `${location}.outcome`, { max: 32, identifier: true });
  if (!ACTION_RESULT_OUTCOMES.has(value.outcome)) fail('INVALID_OUTCOME', `${location}.outcome`);
  nullableString(value.error_code, `${location}.error_code`, { max: MAX_ERROR_CODE, identifier: true });
  safeString(value.driver, `${location}.driver`, { max: 64, identifier: true });
  if (!DRIVER_SET.has(value.driver)) fail('INVALID_DRIVER', `${location}.driver`);
  if (value.error_code !== null && value.outcome === 'succeeded') fail('SUCCESS_ERROR_CODE', `${location}.error_code`);
  if (['failed', 'blocked', 'retry', 'stale'].includes(value.outcome)
      && (value.error_code === null || value.error_code.length === 0)) {
    fail('ERROR_CODE_REQUIRED', `${location}.error_code`);
  }
  if (action.semantic_action !== 'select_option') {
    if (value.selected_option_text !== null) fail('SELECTED_OPTION_INVALID', `${location}.selected_option_text`);
  } else if (value.outcome === 'succeeded') {
    safeString(value.selected_option_text, `${location}.selected_option_text`);
    const optionStep = action.steps.find((step) => step.option_text !== null);
    if (value.selected_option_text !== optionStep?.option_text) {
      fail('SELECTED_OPTION_MISMATCH', `${location}.selected_option_text`);
    }
  } else if (value.selected_option_text !== null) {
    fail('SELECTED_OPTION_INVALID', `${location}.selected_option_text`);
  }
}
function validateCommittedSelection(action, postObservation, location) {
  const controls = postObservation.controls.filter((control) => control.stable_id === action.field_id);
  if (controls.length !== 1) fail('POST_CONTROL_BINDING', location);
  const control = controls[0];
  const selectedOptions = control.options.filter((option) => option.selected);
  const selected = Array.isArray(control.selected) ? control.selected : [];
  if (control.options.length === 0 && selected.length === 0) return;
  if (control.options.length > 0 && selectedOptions.length !== 1) {
    fail('OPTION_SELECTION_UNCOMMITTED', location);
  }
  const optionStep = action.steps.find((step) => step.option_text !== null);
  const expected = new Set([optionStep?.option_text, optionStep?.option_value]);
  const actual = new Set(selected);
  for (const option of selectedOptions) actual.add(option.label).add(option.value);
  if (![...actual].some((value) => expected.has(value))) {
    fail('OPTION_SELECTION_MISMATCH', location);
  }
}


export function validateBrowserActionResult(result, plan, postObservation, context = {}) {
  let historical = false;
  if (context !== undefined && context !== null) {
    if (!isObject(context)) fail('INVALID_CONTEXT', '$context');
    exactKeys(context, ['historical'], '$context');
    if (hasOwn(context, 'historical') && typeof context.historical !== 'boolean') {
      fail('INVALID_CONTEXT', '$context.historical');
    }
    historical = context.historical === true;
  }
  const normalizedPlan = validateBrowserActionPlan(plan, { historical });
  exactKeys(result, ['schema', 'plan_id', 'post_observation_id', 'outcomes'], 'result');
  requiredKeys(result, ['schema', 'plan_id', 'post_observation_id', 'outcomes'], 'result');
  if (result.schema !== ACTION_RESULT_SCHEMA) fail('INVALID_SCHEMA', 'result.schema');
  safeString(result.plan_id, 'result.plan_id', { identifier: true });
  if (result.plan_id !== normalizedPlan.plan_id) fail('PLAN_MISMATCH', 'result.plan_id');
  safeString(result.post_observation_id, 'result.post_observation_id', { identifier: true });
  if (!isObject(postObservation)) fail('POST_OBSERVATION_REQUIRED', 'postObservation');
  try {
    validateObservation(postObservation);
  } catch (error) {
    fail('INVALID_POST_OBSERVATION', 'postObservation', error instanceof Error ? error.message : 'invalid observation');
  }
  if (result.post_observation_id !== postObservation.observation_id) {
    fail('POST_OBSERVATION_MISMATCH', 'result.post_observation_id');
  }
  if (postObservation.observation_id === normalizedPlan.observation_id
      || postObservation.previous_observation_id !== normalizedPlan.observation_id) {
    fail('POST_OBSERVATION_CHAIN', 'postObservation');
  }
  const maxOutcomes = normalizedPlan.actions.length;
  const outcomes = boundedArray(result.outcomes, 'result.outcomes', {
    min: 1,
    max: maxOutcomes,
  });
  if (normalizedPlan.mode === 'single_action' && outcomes.length !== 1) {
    fail('OUTCOME_COUNT', 'result.outcomes');
  }
  if (normalizedPlan.mode === 'fill_batch') {
    outcomes.forEach((outcome, index) => {
      if (index < outcomes.length - 1 && outcome.outcome !== 'succeeded') {
        fail('OUTCOME_PREFIX', `result.outcomes[${index}].outcome`);
      }
    });
    if (outcomes.length < normalizedPlan.actions.length
        && !['failed', 'blocked', 'retry', 'stale'].includes(outcomes.at(-1)?.outcome)) {
      fail('OUTCOME_PREFIX', `result.outcomes[${outcomes.length - 1}].outcome`);
    }
  }
  const attempts = [];
  const formattedValues = [];
  const uploadProofs = {};
  outcomes.forEach((outcome, index) => {
    const action = normalizedPlan.actions[index];
    validateOutcome(outcome, action, `result.outcomes[${index}]`);
    if (outcome.action_id !== action.action_id) fail('OUTCOME_ORDER', `result.outcomes[${index}].action_id`);
    if (!normalizedPlan.fallback_order.includes(outcome.driver)) fail('INVALID_DRIVER', `result.outcomes[${index}].driver`);
    if (outcome.outcome === 'succeeded'
        && action.semantic_action === 'select_option'
        && normalizedPlan.schema !== LEGACY_ACTION_PLAN_SCHEMA) {
      validateCommittedSelection(action, postObservation, `result.outcomes[${index}]`);
    }
    attempts.push({
      action_id: action.action_id,
      action: canonicalLedgerAction(action),
      field_id: action.field_id,
      observation_id: normalizedPlan.observation_id,
      ref: action.control_reference,
      outcome: outcome.outcome,
      retry_of: action.retry_of,
      error_code: outcome.error_code,
    });
    if (outcome.outcome === 'succeeded' && action.semantic_action !== 'upload_file') {
      formattedValues.push({
        field_id: action.field_id,
        answer_alias: action.answer_alias,
        value: actionPrivateValue(action),
      });
    }
    if (outcome.outcome === 'succeeded' && action.semantic_action === 'upload_file') {
      uploadProofs[action.field_id] = {
        action_id: action.action_id,
        value_digest: action.retention.expected_value_digest,
        file_name: action.retention.file_name,
      };
    }
  });
  return immutable({
    attempts,
    formatted_values: formattedValues,
    upload_proofs: uploadProofs,
  });
}
