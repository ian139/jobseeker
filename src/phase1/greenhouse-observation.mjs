import { createHash } from 'node:crypto';

import { canonicalJson } from './evidence.mjs';
import {
  digestObservedValue,
  validateLedger,
  validateObservation,
} from './ledger.mjs';

const GREENHOUSE_HOSTS = Object.freeze([
  'job-boards.greenhouse.io',
  'boards.greenhouse.io',
]);
const GREENHOUSE_HOST_SET = new Set(GREENHOUSE_HOSTS);
const STALE_REQUIRED_MESSAGES = new Set([
  'This field is required.',
  'This field is required',
]);
const PHONE_COUNTRY_PATTERN = /phone.*country|country.*phone/iu;

function deepFreeze(value, seen = new Set()) {
  if (value === null || typeof value !== 'object' || seen.has(value)) return value;
  seen.add(value);
  for (const item of Object.values(value)) deepFreeze(item, seen);
  return Object.freeze(value);
}

function frozenClone(value) {
  return deepFreeze(structuredClone(value));
}

function observationHost(observation) {
  try {
    return new URL(observation.url).hostname;
  } catch {
    return null;
  }
}

function isNonNativeInputCombobox(control) {
  return control.role === 'combobox' && control.tag === 'input';
}

function successfulSelectProof(ledger, attempts, fieldId) {
  const candidates = [];
  if (ledger !== null && Array.isArray(ledger.action_attempts)) {
    for (const attempt of ledger.action_attempts) candidates.push(attempt);
  }
  if (Array.isArray(attempts)) {
    for (const attempt of attempts) candidates.push(attempt);
  }
  return candidates.some((attempt) =>
    attempt !== null && typeof attempt === 'object' &&
    attempt.action === 'select' &&
    attempt.outcome === 'succeeded' &&
    attempt.stale_ref !== true &&
    attempt.field_id === fieldId);
}

function isTextLikeControl(control) {
  if (isNonNativeInputCombobox(control)) return false;
  if (control.role === 'combobox' || control.role === 'listbox') return false;
  if (control.tag === 'select') return false;
  if (!Array.isArray(control.options) || control.options.length > 0) return false;
  if (control.file !== null && control.file !== undefined) return false;
  if (typeof control.value !== 'string') return false;
  if (control.value.length === 0) return false;
  if (control.type === 'checkbox' || control.type === 'radio' || control.type === 'file') return false;
  return true;
}

function textValueMatches(field, control) {
  return control.value_present === true &&
    typeof control.value === 'string' &&
    digestObservedValue(control, control.value) === field.value_digest;
}

function phoneCountryField(field, control) {
  const text = `${control.label ?? ''} ${control.description ?? ''} ${field.label ?? ''} ${field.description ?? ''}`;
  return PHONE_COUNTRY_PATTERN.test(text);
}

function committedSelectionMatches(field, control) {
  const selected = Array.isArray(control.selected) ? control.selected : [];
  const selectedOptions = control.options.filter((option) =>
    option !== null && typeof option === 'object' && option.selected === true);
  if (selected.length > 1 || selectedOptions.length > 1) return false;
  if (selected.length === 0 && selectedOptions.length === 0) return false;
  const candidates = [...selected];
  for (const option of selectedOptions) {
    if (typeof option.value === 'string') candidates.push(option.value);
    if (typeof option.label === 'string') candidates.push(option.label);
  }
  return candidates.some((value) =>
    typeof value === 'string' &&
    digestObservedValue(control, value) === field.value_digest);
}

const SELECT_PROMPT_PATTERN = /^select\b/iu;

function controlLabelText(control) {
  return (control.label ?? '').replace(/\s*\*+$/u, '').trim();
}

function isLabelDerivedRequiredMessage(control) {
  const message = control.validity?.message;
  if (typeof message !== 'string') return false;
  const label = controlLabelText(control);
  if (label.length === 0) return false;
  const lowerMessage = message.toLowerCase();
  const lowerLabel = label.toLowerCase();
  return lowerMessage === `${lowerLabel} is required.` || lowerMessage === `${lowerLabel} is required`;
}

function isSelectPromptAsValidityMessage(control) {
  const message = control.validity?.message;
  const description = control.description;
  if (typeof message !== 'string' || typeof description !== 'string' || description.length === 0) return false;
  if (message.toLowerCase() !== description.toLowerCase()) return false;
  return SELECT_PROMPT_PATTERN.test(description);
}

function staleRequiredValidity(control) {
  const validity = control.validity;
  return validity !== null && typeof validity === 'object' &&
    validity.valid === false &&
    validity.aria_invalid === true &&
    (STALE_REQUIRED_MESSAGES.has(validity.message) ||
     isLabelDerivedRequiredMessage(control) ||
     isSelectPromptAsValidityMessage(control));
}

function snapshotSha256(snapshot) {
  return createHash('sha256').update(canonicalJson(snapshot), 'utf8').digest('hex');
}

/**
 * Normalize only the proven stale "required" validity state that Greenhouse
 * leaves on a committed custom (input) combobox or ordinary text input after
 * a successful select or fill.
 *
 * The normalization is purely local and deterministic:
 * - only `job-boards.greenhouse.io` and `boards.greenhouse.io` observations;
 * - only fields the ledger (plus any supplied attempts) prove were answered by
 *   a successful `select` action or a deliberate `fill` resolution whose digest
 *   matches the observed value;
 * - for non-native input comboboxes, exactly one committed selection whose
 *   digest equals the answered field value digest;
 * - for ordinary text inputs (`input[type=text]`, `textarea`, `contenteditable`,
 *   etc.), a present string value whose digest equals the answered field value
 *   digest;
 * - only the stale required validity (`valid:false`, `aria_invalid:true`) with
 *   one of:
 *   - the exact messages `This field is required.` or `This field is required`;
 *   - a label-derived message such as `School is required.` from `School*` or
 *     `Email is required.` from `Email`;
 *   - a description prompt such as `Select a country` that matches the control
 *     description and begins with "Select";
 * - never observations with blockers, phone-country fields, or any other host.
 *
 * Only the matched control validity is rewritten to
 * `{valid:true, aria_invalid:false, message:null}`; `snapshot_sha256` is
 * recomputed over the canonical `{frames,controls,blockers,title,url}` snapshot.
 * The function never mutates its inputs, validates and recursively freezes a
 * clone of the result, and is idempotent.
 */
export function normalizeGreenhouseObservation(observation, ledger, attempts = []) {
  validateObservation(observation);
  if (ledger !== null) validateLedger(ledger);

  const host = observationHost(observation);
  if (host === null || !GREENHOUSE_HOST_SET.has(host)) return frozenClone(observation);
  if (!Array.isArray(observation.blockers) || observation.blockers.length > 0) {
    return frozenClone(observation);
  }

  const fieldsById = new Map();
  if (ledger !== null && Array.isArray(ledger.fields)) {
    for (const field of ledger.fields) fieldsById.set(field.field_id, field);
  }

  let changed = false;
  const controls = observation.controls.map((control) => {
    const field = fieldsById.get(control.stable_id) ?? null;
    if (field === null) return control;
    if (field.answer_state !== 'answered' || field.value_digest === null) return control;
    if (control.value_present !== true) return control;
    if (phoneCountryField(field, control)) return control;

    if (isNonNativeInputCombobox(control)) {
      if (!successfulSelectProof(ledger, attempts, control.stable_id)) return control;
      if (!committedSelectionMatches(field, control)) return control;
    } else if (isTextLikeControl(control)) {
      if (!textValueMatches(field, control)) return control;
    } else {
      return control;
    }

    if (!staleRequiredValidity(control)) return control;
    changed = true;
    return {
      ...control,
      validity: { valid: true, aria_invalid: false, message: null },
    };
  });

  if (!changed) return frozenClone(observation);

  const normalized = {
    ...observation,
    controls,
    snapshot_sha256: snapshotSha256({
      frames: observation.frames,
      controls,
      blockers: observation.blockers,
      title: observation.title,
      url: observation.url,
    }),
  };
  validateObservation(normalized);
  return frozenClone(normalized);
}
