import {
  ANSWER_SOURCES,
  isHoneypotTarget,
  isReachableTarget,
  validateLedger,
  validateObservation,
} from './ledger.mjs';

export const AUDIT_SCHEMA = 'phase1-audit-v2';

const SOURCES = new Set(ANSWER_SOURCES);
const OPTIONS = new Set(['final_review_boundary', 'final']);

function clone(value) {
  if (Array.isArray(value)) return value.map(clone);
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, clone(item)]));
  }
  return value;
}

function freeze(value) {
  if (value !== null && typeof value === 'object' && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}

function immutable(value) {
  return freeze(clone(value));
}

function optionsFor(input) {
  if (input === undefined || input === null) return { final_review_boundary: false, final: false };
  if (typeof input !== 'object' || Array.isArray(input)) throw new TypeError('audit options must be an object');
  for (const key of Object.keys(input)) {
    if (!OPTIONS.has(key)) throw new TypeError(`audit options.${key}: unknown key`);
  }
  if (input.final_review_boundary !== undefined && typeof input.final_review_boundary !== 'boolean') {
    throw new TypeError('audit options.final_review_boundary must be boolean');
  }
  if (input.final !== undefined && typeof input.final !== 'boolean') {
    throw new TypeError('audit options.final must be boolean');
  }
  return {
    final_review_boundary: input.final_review_boundary === true,
    final: input.final === true,
  };
}

function targetBlocker(code, message, target = null, fieldId = null) {
  return {
    code,
    message,
    field_id: fieldId ?? target?.field_id ?? null,
    target_id: target?.target_id ?? null,
  };
}

function sortedUnique(values) {
  return [...new Set(values)].sort();
}

function isReachableField(target) {
  return isReachableTarget(target) && target.field_id !== null;
}

function isFileTarget(target) {
  return target.kind === 'file_upload' || target.file !== null;
}

function validTargetState(target) {
  return target.latest_state.validation.valid === true
    && target.latest_state.validation.message_present !== true;
}

function deliberate(target) {
  return target.answer_state === 'answered' || target.answer_state === 'blank';
}

function currentTargetMap(observation) {
  return new Map(observation.targets.map((target) => [target.target_id, target]));
}

function groupKey(target) {
  if (target.kind !== 'radio' || target.group_id === null) return null;
  return target.group_id;
}

function auditRadioGroups(currentFields, byField, unresolved, invalid, unretained, revealed) {
  const groups = new Map();
  for (const target of currentFields) {
    const key = groupKey(target);
    if (key === null) continue;
    const group = groups.get(key) ?? [];
    group.push(target);
    groups.set(key, group);
  }
  for (const group of groups.values()) {
    const selected = group.filter((target) =>
      target.checked === true || target.selected !== null || target.value_state === 'selected');
    const selectedFields = selected
      .map((target) => byField.get(target.field_id))
      .filter((target) => target !== undefined);
    const complete = selectedFields.some((target) =>
      deliberate(target) && target.retained && target.valid && validTargetState(target));
    if (complete) {
      for (const target of group) {
        unresolved.delete(target.field_id);
        invalid.delete(target.field_id);
        unretained.delete(target.field_id);
        revealed.delete(target.field_id);
      }
    }
  }
}

function historicalIssue(target, currentTarget) {
  if (target.answer_state === 'unresolved' && target.revealed_observation_id !== null) return 'unresolved';
  if (!deliberate(target)) return null;
  if (!target.valid || !target.retained) return target.retained ? 'invalid' : 'unretained';
  if (isFileTarget(target) && currentTarget === undefined) return 'unresolved';
  return null;
}

export function auditCompletion(ledger, observation, options = undefined) {
  validateLedger(ledger);
  validateObservation(observation);
  const parsed = optionsFor(options);
  const byField = new Map(ledger.targets.map((target) => [target.field_id, target]));
  const currentByTarget = currentTargetMap(observation);
  const currentFields = observation.targets.filter(isReachableField);
  const currentFieldIds = new Set(currentFields.map((target) => target.field_id));
  const unresolved = new Set();
  const invalid = new Set();
  const unretained = new Set();
  const revealed = new Set();
  const staleTargets = new Set();
  const blockers = [];
  const finalCandidates = observation.targets.filter((target) =>
    target.candidate.class === 'final_candidate'
      && target.visible
      && target.enabled
      && !isHoneypotTarget(target),
  );

  if (ledger.latest_observation_id !== observation.observation_id) {
    blockers.push(targetBlocker('stale-observation', 'audit image is not the ledger latest observation'));
  }
  for (const item of observation.blockers) {
    const code = typeof item === 'string' ? item : item.code;
    blockers.push(targetBlocker(`observation-blocker:${code}`, 'observation reported a blocker'));
  }
  for (const target of observation.targets) {
    if (target.candidate.class === 'unknown' && target.visible && target.enabled && !isHoneypotTarget(target)) {
      blockers.push(targetBlocker('unknown-target', 'visible target has no safe candidate classification', target));
    }
  }

  if (finalCandidates.length === 0 && !parsed.final_review_boundary) {
    blockers.push(targetBlocker('no-final-boundary', 'a final candidate or explicit final-review boundary is required'));
  } else if (finalCandidates.length > 1 && !parsed.final_review_boundary) {
    blockers.push(targetBlocker('multiple-final-candidates', 'more than one final candidate is visible'));
  }

  for (const visual of currentFields) {
    const target = byField.get(visual.field_id);
    if (target === undefined) {
      unresolved.add(visual.field_id);
      revealed.add(visual.field_id);
      blockers.push(targetBlocker('revealed-field', 'reachable target is absent from the ledger', visual));
      continue;
    }
    if (target.latest_observation_id !== observation.observation_id
      || target.target_id !== visual.target_id
      || !target.present_in_latest_observation) {
      staleTargets.add(visual.target_id);
      blockers.push(targetBlocker('stale-target', 'target identity is not current for this image', visual, visual.field_id));
    }
    if (target.answer_state === 'unresolved') unresolved.add(target.field_id);
    if (!target.valid || !validTargetState(target)) invalid.add(target.field_id);
    if (!target.retained) unretained.add(target.field_id);
    if (target.revealed_observation_id === observation.observation_id
      && (target.answer_state === 'unresolved' || !target.valid || !target.retained)) {
      revealed.add(target.field_id);
    }
    if (target.answer_source !== null && !SOURCES.has(target.answer_source)) {
      invalid.add(target.field_id);
      blockers.push(targetBlocker('invalid-answer-source', 'target answer source is not allowed', visual, target.field_id));
    }
    if (isFileTarget(visual) && visual.file === null) {
      unresolved.add(target.field_id);
      blockers.push(targetBlocker('missing-file-target', 'file target is not observable in the current image', visual, target.field_id));
    }
  }

  for (const target of ledger.targets) {
    if (currentFieldIds.has(target.field_id)) continue;
    const current = target.target_id === undefined ? undefined : currentByTarget.get(target.target_id);
    const issue = historicalIssue(target, current);
    if (issue === 'unresolved') unresolved.add(target.field_id);
    if (issue === 'invalid') invalid.add(target.field_id);
    if (issue === 'unretained') unretained.add(target.field_id);
    if (issue !== null && isFileTarget(target) && current === undefined) {
      blockers.push(targetBlocker('missing-file-target', 'answered file target is absent from the current image', null, target.field_id));
    }
  }

  auditRadioGroups(currentFields, byField, unresolved, invalid, unretained, revealed);
  for (const action of ledger.action_attempts) {
    if (action.stale_target === true && action.target_id !== null) {
      staleTargets.add(action.target_id);
      blockers.push(targetBlocker('stale-action-target', 'action evidence contains a stale target', null, action.field_id));
    }
  }

  const unresolvedIds = sortedUnique([...unresolved]);
  const invalidIds = sortedUnique([...invalid]);
  const unretainedIds = sortedUnique([...unretained]);
  const revealedIds = sortedUnique([...revealed]);
  const staleIds = sortedUnique([...staleTargets]);
  const candidateIds = sortedUnique(finalCandidates.map((target) => target.target_id));
  const complete = blockers.length === 0
    && unresolvedIds.length === 0
    && invalidIds.length === 0
    && unretainedIds.length === 0
    && revealedIds.length === 0
    && staleIds.length === 0;

  return immutable({
    schema: AUDIT_SCHEMA,
    observation_id: observation.observation_id,
    passed: complete,
    complete,
    blockers,
    stale_target_ids: staleIds,
    unresolved_field_ids: unresolvedIds,
    invalid_field_ids: invalidIds,
    unretained_field_ids: unretainedIds,
    revealed_field_ids: revealedIds,
    final_candidate_target_ids: candidateIds,
    final_review_boundary: parsed.final_review_boundary,
    submit_action_count: ledger.submit_action_count,
    field_count: currentFields.length,
    target_count: currentFields.length,
    final: parsed.final,
  });
}

export const completionAudit = auditCompletion;
