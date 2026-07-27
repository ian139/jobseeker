import {
  ANSWER_SOURCES,
  isHoneypotControl,
  isReachableFieldControl,
  validateLedger,
  validateObservation,
} from './ledger.mjs';

export const AUDIT_SCHEMA = 'phase1-audit-v1';

const ANSWER_SOURCE_SET = new Set(ANSWER_SOURCES);
const OPTIONS = new Set(['final_review_boundary']);

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function clone(value) {
  if (Array.isArray(value)) return value.map(clone);
  if (isRecord(value)) {
    const result = {};
    for (const [key, item] of Object.entries(value)) result[key] = clone(item);
    return result;
  }
  return value;
}

function freeze(value) {
  if (isRecord(value) || Array.isArray(value)) {
    for (const item of Object.values(value)) freeze(item);
    Object.freeze(value);
  }
  return value;
}

function immutable(value) {
  return freeze(clone(value));
}

function blocker(code, message, fieldId = null, ref = null) {
  return { code, message, field_id: fieldId, ref };
}

function reachable(control) {
  return isReachableFieldControl(control) && !isHoneypotControl(control);
}

function currentFieldControls(observation) {
  return observation.controls.filter((control) => reachable(control));
}

function groupKey(control) {
  if (control.role !== 'radio') return null;
  if (control.group_id != null) return `radio-group:${control.group_id}`;
  if (control.name != null) return `radio-name:${control.frame_id || ''}:${control.name}`;
  return null;
}

function deliberate(field) {
  if (field.answer_state === 'answered') {
    return field.answer_source !== null && field.value_digest !== null;
  }
  if (field.answer_state === 'blank') {
    return field.answer_source !== null && field.semantic_choice !== null;
  }
  return false;
}

function validSource(field) {
  return field.answer_source === null || ANSWER_SOURCE_SET.has(field.answer_source);
}
function isFileField(field) {
  const file = isRecord(field.latest_state) && field.latest_state.file;
  return isRecord(file) && file.accept !== null;
}

function isObservedFileControl(control) {
  return control.type === 'file' && isRecord(control.file) && control.file.accept !== null;
}

function priorFieldIssueSet(field) {
  return {
    unresolved: !deliberate(field),
    invalid: !field.valid || !validSource(field),
    unretained: !field.retained,
    missingFile: isFileField(field) && field.answer_state === 'answered',
  };
}


function parseOptions(options) {
  if (options === undefined) return { final_review_boundary: false };
  if (!isRecord(options)) throw new TypeError('audit options must be an object');
  for (const key of Object.keys(options)) {
    if (!OPTIONS.has(key)) throw new TypeError(`audit options.${key}: unknown key`);
  }
  if (options.final_review_boundary !== undefined && typeof options.final_review_boundary !== 'boolean') {
    throw new TypeError('audit options.final_review_boundary: expected boolean');
  }
  return { final_review_boundary: options.final_review_boundary === true };
}

function fieldIssueSet(field, control, currentObservationId) {
  const unresolved = !deliberate(field);
  const invalid = !field.valid || !control.validity.valid || control.validity.aria_invalid;
  const unretained = !field.retained;
  const stale = field.latest_observation_id !== currentObservationId || field.latest_ref !== control.ref;
  return { unresolved, invalid, unretained, stale };
}

function actionEvidence(ledger) {
  const finalAttempts = ledger.action_attempts.filter((attempt) =>
    attempt.action === 'submit' || attempt.action === 'final_submit',
  );
  const staleAttempts = ledger.action_attempts.filter((attempt) => attempt.stale_ref);
  return { finalAttempts, staleAttempts };
}
function auditRadioGroup(group, unresolved, invalid, unretained, revealed, staleRefs) {
  if (group.length < 2) return;
  const selected = group.filter((field) =>
    field.latest_state.checked === true || field.latest_state.selected === true);
  if (selected.length !== 1) return;
  const selectedField = selected[0];
  const selectedStateValid = selectedField.latest_state.validity.valid &&
    selectedField.latest_state.validity.aria_invalid !== true;
  if (!deliberate(selectedField) || !selectedField.retained ||
      !selectedField.valid || !selectedStateValid) {
    return;
  }
  for (const field of group) {
    if (field.field_id === selectedField.field_id) continue;
    unresolved.delete(field.field_id);
    unretained.delete(field.field_id);
    revealed.delete(field.field_id);
    staleRefs.delete(field.field_id);
    if (field.latest_state.validity.valid &&
        field.latest_state.validity.aria_invalid !== true) {
      invalid.delete(field.field_id);
    }
  }
}

function radioGroupComponents(group) {
  const parents = group.map((_, index) => index);
  const find = (index) => {
    let root = index;
    while (parents[root] !== root) root = parents[root];
    while (parents[index] !== index) {
      const next = parents[index];
      parents[index] = root;
      index = next;
    }
    return root;
  };
  const union = (left, right) => {
    const leftRoot = find(left);
    const rightRoot = find(right);
    if (leftRoot !== rightRoot) parents[rightRoot] = leftRoot;
  };
  const firstByObservation = new Map();
  group.forEach((field, index) => {
    for (const ref of field.ref_history) {
      const first = firstByObservation.get(ref.observation_id);
      if (first === undefined) firstByObservation.set(ref.observation_id, index);
      else union(first, index);
    }
  });
  const components = new Map();
  group.forEach((field, index) => {
    const root = find(index);
    const component = components.get(root) ?? [];
    component.push(field);
    components.set(root, component);
  });
  return [...components.values()];
}

function auditRadioGroups(
  fields,
  unresolved,
  invalid,
  unretained,
  revealed,
  staleRefs,
) {
  const groups = new Map();
  for (const field of fields) {
    if (field.role !== 'radio') continue;
    const key = groupKey(field);
    if (key === null) continue;
    const group = groups.get(key) ?? [];
    group.push(field);
    groups.set(key, group);
  }
  for (const group of groups.values()) {
    for (const component of radioGroupComponents(group)) {
      auditRadioGroup(component, unresolved, invalid, unretained, revealed, staleRefs);
    }
  }
}


function sortedUnique(values) {
  return [...new Set(values)].sort();
}

export function auditCompletion(ledger, observation, options = undefined) {
  validateLedger(ledger);
  validateObservation(observation);
  const parsedOptions = parseOptions(options);
  const fieldsById = new Map(ledger.fields.map((field) => [field.field_id, field]));
  const activeControls = currentFieldControls(observation);
  const activeFieldIds = new Set(activeControls.map((control) => control.stable_id));
  const unresolved = new Set();
  const invalid = new Set();
  const unretained = new Set();
  const revealed = new Set();
  const staleRefs = new Set();
  const blockers = [];
  const finalCandidates = observation.controls.filter((control) =>
    control.candidate.class === 'final_candidate' && control.visible && control.enabled && !isHoneypotControl(control),
  );

  if (ledger.latest_observation_id !== observation.observation_id) {
    blockers.push(blocker(
      'stale-observation',
      'audit observation is not the ledger latest observation',
    ));
  }
  for (const item of observation.blockers) {
    blockers.push(blocker(`observation-blocker:${item.code}`, 'observation reported a blocker'));
  }
  for (const control of observation.controls) {
    if (control.candidate.class === 'unknown' && control.visible && control.enabled && !isHoneypotControl(control)) {
      blockers.push(blocker('unknown-control', 'visible control has no safe candidate classification', control.stable_id, control.ref));
    }
  }
  if (finalCandidates.length === 0 && !parsedOptions.final_review_boundary) {
    blockers.push(blocker('no-final-boundary', 'a final candidate or explicit final-review boundary is required'));
  }

  for (const control of activeControls) {
    const field = fieldsById.get(control.stable_id);
    if (!field) {
      unresolved.add(control.stable_id);
      revealed.add(control.stable_id);
      blockers.push(blocker('revealed-field', 'reachable field is absent from the ledger', control.stable_id, control.ref));
      continue;
    }
    const issues = fieldIssueSet(field, control, observation.observation_id);
    if (isFileField(field) && !isObservedFileControl(control)) {
      unresolved.add(field.field_id);
      blockers.push(blocker('missing-file-field', 'file field is not observable in the current observation', field.field_id, control.ref));
    }
    if (issues.unresolved) unresolved.add(field.field_id);
    if (issues.invalid) invalid.add(field.field_id);
    if (issues.unretained) unretained.add(field.field_id);
    if (issues.stale) staleRefs.add(field.field_id);
    if (field.last_revealed_observation_id === observation.observation_id &&
        (issues.unresolved || issues.invalid || issues.unretained || issues.stale)) {
      revealed.add(field.field_id);
    }
    if (!validSource(field)) {
      invalid.add(field.field_id);
      blockers.push(blocker('invalid-answer-source', 'field answer source is not allowed', field.field_id, control.ref));
    }
    if (issues.stale) {
      blockers.push(blocker('stale-ref', 'field reference is not current for this observation', field.field_id, control.ref));
    }
  }
  for (const field of ledger.fields) {
    if (activeFieldIds.has(field.field_id) || field.revealed_observation_id === null) continue;
    const issues = priorFieldIssueSet(field);
    if (issues.unresolved || issues.missingFile) unresolved.add(field.field_id);
    if (issues.invalid) invalid.add(field.field_id);
    if (issues.unretained) unretained.add(field.field_id);
    if (!validSource(field)) {
      blockers.push(blocker('invalid-answer-source', 'field answer source is not allowed', field.field_id, field.latest_ref ?? null));
    }
    if (issues.missingFile) {
      blockers.push(blocker('missing-file-field', 'answered file field is absent from the current observation', field.field_id, field.latest_ref ?? null));
    }
  }


  auditRadioGroups(
    ledger.fields,
    unresolved,
    invalid,
    unretained,
    revealed,
    staleRefs,
  );

  for (const fieldId of unresolved) {
    blockers.push(blocker('unresolved-field', 'reachable field has no deliberate answer or state', fieldId, fieldsById.get(fieldId)?.latest_ref ?? null));
  }
  for (const fieldId of invalid) {
    blockers.push(blocker('invalid-field', 'reachable field is invalid or has an invalid answer source', fieldId, fieldsById.get(fieldId)?.latest_ref ?? null));
  }
  for (const fieldId of unretained) {
    blockers.push(blocker('unretained-field', 'reachable field did not retain its deliberate value or state', fieldId, fieldsById.get(fieldId)?.latest_ref ?? null));
  }
  for (const fieldId of revealed) {
    blockers.push(blocker('revealed-field', 'newly revealed field is not verified complete', fieldId, fieldsById.get(fieldId)?.latest_ref ?? null));
  }

  const evidence = actionEvidence(ledger);
  for (const attempt of evidence.staleAttempts) {
    blockers.push(blocker('stale-action-ref', 'action evidence contains a stale control reference', attempt.field_id, attempt.ref));
  }
  const unresolvedIds = sortedUnique([...unresolved]);
  const invalidIds = sortedUnique([...invalid]);
  const unretainedIds = sortedUnique([...unretained]);
  const revealedIds = sortedUnique([...revealed]);
  const staleIds = sortedUnique([...staleRefs]);
  const pass = blockers.length === 0 && unresolvedIds.length === 0 && invalidIds.length === 0 &&
    unretainedIds.length === 0 && revealedIds.length === 0 && staleIds.length === 0;
  return immutable({
    schema: AUDIT_SCHEMA,
    observation_id: observation.observation_id,
    passed: pass,
    complete: pass,
    blockers,
    stale_refs: staleIds,
    unresolved_field_ids: unresolvedIds,
    invalid_field_ids: invalidIds,
    unretained_field_ids: unretainedIds,
    revealed_field_ids: revealedIds,
    final_candidate_refs: finalCandidates.map((control) => control.ref).sort(),
    final_review_boundary: parsedOptions.final_review_boundary,
    submit_action_count: ledger.submit_action_count,
    field_count: activeControls.length,
  });
}

export const completionAudit = auditCompletion;
