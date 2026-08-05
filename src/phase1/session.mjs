import { createHash } from 'node:crypto';
import {
  MAX_RESUME_BYTES,
  MAX_ALIAS_LENGTH,
  appendAnswerRecord,
  approvalContextSha256,
  createAnswerRecord,
  ensurePrivateDirectory,
  loadRunInputs,
  loadRunContractSnapshot,
  loadAnswerMemory,
  resolveAnswer,
} from './contract.mjs';
import {
  createLedger,
  digestObservedValue,
  mergeObservation,
  recordActionAttempt,
  recordActionBatch as recordLedgerActionBatch,
  recordResolution,
  resolveFinalSubmitAttempt,
  requiresReobservation,
  semanticChoiceIsDeliberate,
  markFieldSensitive,
  validateLedger,
  validateObservation,
  verifyRetention as verifyLedgerRetention,
} from './ledger.mjs';
import { auditCompletion } from './audit.mjs';
import {
  canonicalJson,
  createEvidenceStore,
  SUBMISSION_AUTHORIZATION_SCHEMA,
} from './evidence.mjs';
import {
  validateBrowserActionPlan,
  validateBrowserActionResult,
} from './action-plan.mjs';

const SESSION_STATES = new WeakMap();
const DELIBERATE_BLANK_SOURCES = new Set(['memory', 'profile', 'agent_inference', 'user']);
const FINAL_SUBMIT_TERMINAL_OUTCOMES = new Set(['succeeded', 'failed', 'blocked']);
const MAX_FORMATTED_VALUE_LENGTH = 4096;

function assertRecord(value, name) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new TypeError(`${name} must be an object`);
  }
}

function assertExactKeys(value, allowed, name) {
  assertRecord(value, name);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) throw new TypeError(`${name}.${key}: unknown key`);
  }
}

function deepFreeze(value, seen = new Set()) {
  if (value === null || typeof value !== 'object' || seen.has(value)) return value;
  seen.add(value);
  for (const item of Object.values(value)) deepFreeze(item, seen);
  return Object.freeze(value);
}

function frozenClone(value) {
  return deepFreeze(structuredClone(value));
}

function snapshotValue(value, name, seen) {
  if (value === null || typeof value !== 'object') {
    if (typeof value === 'function') throw new TypeError(`${name} must contain plain data values`);
    return value;
  }
  if (seen.has(value)) throw new TypeError(`${name} must contain plain data values`);
  const array = Array.isArray(value);
  if (!array) {
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      throw new TypeError(`${name} must contain plain data values`);
    }
  }
  const descriptors = Object.getOwnPropertyDescriptors(value);
  if (Object.values(descriptors).some((descriptor) => descriptor.get || descriptor.set)) {
    throw new TypeError(`${name} properties must be plain data values`);
  }
  if (Object.getOwnPropertySymbols(value).length > 0
    || Object.entries(descriptors).some(([key, descriptor]) =>
      !descriptor.enumerable && !(array && key === 'length'))) {
    throw new TypeError(`${name} properties must be plain data values`);
  }
  seen.add(value);
  let snapshot;
  if (array) {
    snapshot = new Array(value.length);
    for (const [key, descriptor] of Object.entries(descriptors)) {
      if (!descriptor.enumerable) continue;
      const index = Number(key);
      if (!Number.isInteger(index) || index < 0 || index >= value.length || String(index) !== key) {
        throw new TypeError(`${name} must contain plain data values`);
      }
      snapshot[index] = snapshotValue(descriptor.value, `${name}[${index}]`, seen);
    }
  } else {
    snapshot = Object.fromEntries(
      Object.entries(descriptors)
        .filter(([, descriptor]) => descriptor.enumerable)
        .map(([key, descriptor]) => [
          key,
          snapshotValue(descriptor.value, `${name}.${key}`, seen),
        ]),
    );
  }
  seen.delete(value);
  return Object.freeze(snapshot);
}

function dataSnapshot(value, name) {
  assertRecord(value, name);
  return snapshotValue(value, name, new Set());
}

function boundCandidate(value, identity, name) {
  if (value === undefined) return undefined;
  if (identity === null) throw new TypeError(`${name} requires its configured source path`);
  assertExactKeys(value, new Set(['source_sha256', 'answers']), name);
  if (value.source_sha256 !== identity.sha256) {
    throw new TypeError(`${name}.source_sha256 must match the configured input`);
  }
  assertRecord(value.answers, `${name}.answers`);
  return frozenClone(value);
}

function boundAgentInference(value, sourceResumeIdentity, jobDescriptionIdentity, name) {
  if (value === undefined) return undefined;
  if (sourceResumeIdentity == null) {
    throw new TypeError(`${name} requires a configured source resume`);
  }
  if (jobDescriptionIdentity == null) {
    throw new TypeError(`${name} requires a configured job description`);
  }
  assertExactKeys(value, new Set(['source_resume_sha256', 'job_description_sha256', 'answers']), name);
  if (value.source_resume_sha256 !== sourceResumeIdentity.sha256) {
    throw new TypeError(`${name}.source_resume_sha256 must match the configured source resume`);
  }
  if (value.job_description_sha256 !== jobDescriptionIdentity.sha256) {
    throw new TypeError(`${name}.job_description_sha256 must match the configured job description`);
  }
  assertRecord(value.answers, `${name}.answers`);
  for (const alias of Object.keys(value.answers)) {
    const answer = resolveAnswer({ alias, agentInference: value });
    if (answer.inference_evidence_digests.resume_sha256 !== sourceResumeIdentity.sha256
      || answer.inference_evidence_digests.job_description_sha256 !== jobDescriptionIdentity.sha256) {
      throw new TypeError(`${name}.answers.${alias}.evidence must match the configured inputs`);
    }
  }
  return frozenClone(value);
}

const INFERENCE_SENSITIVE_PATTERNS = Object.freeze([
  /\b(?:identity|full|first|last|preferred|given|family|middle|maiden)\s*name\b|\bidentity\b|\bemail\b|\bphone\b|\bmobile\b|\baddress\b|\bstreet\b|\bcity\b|\bstate\b|\bzip(?:\s*code)?\b|\bpostal(?:\s*code)?\b|\bcountry\b|\bcontact\b/,
  /\b(?:authorization|work\s*authorization|authorized\s*to\s*work|right\s*to\s*work|sponsorship|sponsor|visa|citizenship|citizen|h[\s_-]*1[\s_-]*b|green\s*card|permanent\s*resident|work\s*permit|employment\s*eligibility|i[\s_-]*9|ead)\b/,
  /\b(?:protected\s*class|demographic|demographics|gender|sex|race|ethnicity|religion|marital\s*status|married|age|date\s*of\s*birth|dob|birth(?:day|date)?|veteran|disabilit(?:y|ies)|pronouns?|sexual\s*orientation|nationality|national\s*origin|children|pregnancy|family\s*status)\b/,
  /\b(?:salary|compensation|pay|wage|wages|hourly\s*rate|bonus|benefits|remuneration|expected\s*salary|desired\s*salary|current\s*salary|salary\s*expectation)\b/,
  /\b(?:date(?:s)?|start\s*date|end\s*date|graduation(?:\s*date)?|graduated|availability\s*date|available\s*date|hire\s*date|termination\s*date|expiration\s*date|effective\s*date|anniversary)\b/,
  /\b(?:degree|degrees|diploma|major|minor|gpa|university|college|school|education|license|certification|credential|credentials|accredited|professional\s*license|bar|cpa|mba|phd|md|jd|rn|license\s*number|certification\s*number)\b/,
  /\b(?:ssn|social\s*security(?:\s*number)?|national\s*id|passport|driver\s*s?\s*license|id\s*number|tax\s*id|taxpayer\s*id|ein|itin)\b/,
  /\b(?:bank|account|routing|credit\s*card|debit|paypal|venmo|financial|income|tax|net\s*worth|assets?)\b/,
  /\b(?:medical|disability|health|condition|insurance|diagnosis|accommodation|accommodations|sick|medication|mental\s*health|physical)\b/,
  /\b(?:criminal|felony|misdemeanor|background\s*check|security\s*clearance|clearance|legal|lawsuit|conviction|convictions|arrest|arrests|court|restraining|crime|dui|dwi|fingerprint|fingerprints|bonded)\b/,
]);
const IDENTITY_PROSE_PATTERN = /\b(?:identity|full\s*name|first\s*name|last\s*name|preferred\s*name|given\s*name|family\s*name|email(?:\s*address)?|phone(?:\s*number)?|mobile(?:\s*number)?|mailing\s*address|home\s*address|street\s*address|zip\s*code|postal\s*code|contact\s*information)\b/;
const EXACT_IDENTITY_PROSE = new Set(['name', 'address', 'city', 'state', 'country']);

function normalizeClassificationText(text) {
  if (typeof text !== 'string') return '';
  return text.toLowerCase().replace(/[^a-z0-9]+/g, ' ').replace(/\s+/g, ' ').trim();
}

function identityProseIsSensitive(field) {
  return [field.label, field.description].some((value) => {
    const text = normalizeClassificationText(value);
    return EXACT_IDENTITY_PROSE.has(text) || IDENTITY_PROSE_PATTERN.test(text);
  });
}

function isSensitiveInferenceField(alias, field) {
  const keyText = `${normalizeClassificationText(alias)} ${normalizeClassificationText(field.name)}`;
  if (INFERENCE_SENSITIVE_PATTERNS[0].test(keyText) || identityProseIsSensitive(field)) return true;
  const text = `${keyText} ${normalizeClassificationText(field.label)} ${normalizeClassificationText(field.description)}`;
  for (const pattern of INFERENCE_SENSITIVE_PATTERNS.slice(1)) {
    if (pattern.test(text)) return true;
  }
  return false;
}

function stateFor(session) {
  const state = SESSION_STATES.get(session);
  if (!state) throw new TypeError('session must be returned by startRun');
  if (state.faulted) throw new Error('session evidence publication previously failed');
  if (state.finalized) throw new Error('session is already finalized');
  return state;
}

async function transact(session, operation) {
  const state = stateFor(session);
  if (state.busy) throw new Error('session operation already in progress');
  state.busy = true;
  let published = false;
  try {
    return await operation(state, () => { published = true; });
  } catch (error) {
    if (published) {
      state.faulted = true;
      await state.evidence.close();
    }
    throw error;
  } finally {
    state.busy = false;
  }
}

function requireObservationState(state) {
  if (state.ledger === null || state.observation === null) {
    throw new Error('acceptObservation must initialize the session first');
  }
}
function hasPendingFinalSubmit(state) {
  return state.ledger?.action_attempts.some((action) =>
    action.action === 'final_submit' && action.outcome === 'attempted'
  ) ?? false;
}

function invalidateSubmissionPreparation(state) {
  if (state.submissionSucceeded) return;
  state.submissionAuthorized = false;
  state.authorizedFinalRef = null;
  state.authorizedObservationId = null;
  if (!hasPendingFinalSubmit(state)) state.preSubmitAuditRef = null;
}

function actionPlanRecords(state) {
  const names = typeof state.evidence.store.listActionPlans === 'function'
    ? state.evidence.store.listActionPlans()
    : state.evidence.store.listArtifacts().filter((name) => /^action-plan-\d+\.json$/.test(name));
  const plans = names.sort().map((name) => (
    typeof state.evidence.store.readActionPlan === 'function'
      ? state.evidence.store.readActionPlan(name)
      : validateBrowserActionPlan(state.evidence.store.readArtifact(name))
  ));
  const planIds = new Set();
  const actionIds = new Set();
  for (const plan of plans) {
    if (planIds.has(plan.plan_id)) throw new TypeError('action plan identity was reused');
    planIds.add(plan.plan_id);
    for (const action of plan.actions) {
      if (actionIds.has(action.action_id)) throw new TypeError('action identity was reused');
      actionIds.add(action.action_id);
    }
  }
  return plans;
}

function actionResultRecords(state) {
  const names = typeof state.evidence.store.listActionResults === 'function'
    ? state.evidence.store.listActionResults()
    : state.evidence.store.listArtifacts().filter((name) => /^action-result-\d+\.json$/.test(name));
  const receipts = names.sort().map((name) => state.evidence.store.readActionResult(name));
  const planIds = new Set();
  for (const receipt of receipts) {
    if (planIds.has(receipt.plan.plan_id)) throw new TypeError('action result identity was reused');
    planIds.add(receipt.plan.plan_id);
  }
  return receipts;
}

function plannedLedgerAction(planned) {
  if (planned.semantic_action === 'fill_text') return 'fill';
  if (planned.semantic_action === 'clear') return 'clear';
  if (planned.semantic_action === 'select_option') return 'select';
  if (planned.semantic_action === 'upload_file') return 'upload';
  if (planned.semantic_action === 'toggle') {
    return planned.steps[0].normalized_action.checked === true ? 'check' : 'uncheck';
  }
  throw new TypeError('planned action has an unsupported semantic action');
}

function plannedCanonicalRef(planned) {
  return planned.control_reference;
}

const ACTION_ATTEMPT_KEYS = Object.freeze([
  'action_id',
  'action',
  'field_id',
  'observation_id',
  'ref',
  'outcome',
  'retry_of',
  'error_code',
]);

function actionAttemptsMatch(left, right) {
  return ACTION_ATTEMPT_KEYS.every((key) => canonicalJson(left[key]) === canonicalJson(right[key]));
}

function assertCompletedAttemptBinding(attempt, planned, plan) {
  const outcomes = new Set(['succeeded', 'failed', 'retry', 'blocked', 'stale']);
  if (!outcomes.has(attempt.outcome)) {
    throw new TypeError('planned action result has an invalid outcome');
  }
  if (attempt.action_id !== planned.action_id
    || attempt.action !== plannedLedgerAction(planned)
    || attempt.field_id !== planned.field_id
    || attempt.observation_id !== plan.observation_id
    || attempt.ref !== plannedCanonicalRef(planned)
    || attempt.retry_of !== planned.retry_of) {
    throw new TypeError('planned action result has an unrelated binding');
  }
  if (attempt.outcome === 'succeeded' && attempt.error_code !== null) {
    throw new TypeError('successful planned action cannot carry an error');
  }
  if (['failed', 'retry', 'blocked', 'stale'].includes(attempt.outcome)
    && (typeof attempt.error_code !== 'string' || attempt.error_code.length === 0)) {
    throw new TypeError('failed planned action requires an error');
  }
}

function validatedReceipt(receipt, options = {}) {
  const validation = validateBrowserActionResult(
    receipt.result,
    receipt.plan,
    receipt.post_observation,
    options,
  );
  if (!Array.isArray(validation.attempts)
    || validation.attempts.length === 0
    || validation.attempts.length > receipt.plan.actions.length) {
    throw new TypeError('planned action result must contain an executed action prefix');
  }
  for (const [index, attempt] of validation.attempts.entries()) {
    assertCompletedAttemptBinding(attempt, receipt.plan.actions[index], receipt.plan);
  }
  return validation;
}

function pendingActionPlanRecords(state) {
  if (state.ledger === null || state.observation === null) return [];
  const plans = actionPlanRecords(state);
  const receipts = actionResultRecords(state);
  const receiptsByPlan = new Map();
  for (const receipt of receipts) {
    const plan = plans.find((candidate) => candidate.plan_id === receipt.plan.plan_id);
    if (!plan || !actionPlanMatches(plan, receipt.plan)) {
      throw new TypeError('action result does not match its action plan');
    }
    const validation = validatedReceipt(receipt);
    receiptsByPlan.set(receipt.plan.plan_id, { receipt, validation });
  }

  const ledgerActions = new Map(state.ledger.action_attempts.map((action) => [action.action_id, action]));
  const pending = [];
  for (const plan of plans) {
    const completed = receiptsByPlan.get(plan.plan_id);
    if (completed !== undefined) {
      for (const [index, attempt] of completed.validation.attempts.entries()) {
        const recorded = ledgerActions.get(attempt.action_id);
        if (!recorded) {
          throw new TypeError(`action result receipt action is missing: ${attempt.action_id}`);
        }
        if (!actionAttemptsMatch(recorded, attempt)) {
          const mismatched = ACTION_ATTEMPT_KEYS
            .find((key) => canonicalJson(recorded[key]) !== canonicalJson(attempt[key]));
          throw new TypeError(
            `action result receipt action differs at ${mismatched ?? 'unknown'}: ${attempt.action_id}`,
          );
        }
        assertCompletedAttemptBinding(recorded, plan.actions[index], plan);
      }
      if (!state.ledger.observation_ids.includes(completed.receipt.post_observation.observation_id)) {
        throw new TypeError('action result receipt post observation is not merged');
      }
      continue;
    }
    const present = plan.actions.filter((action) => ledgerActions.has(action.action_id));
    if (present.length !== 0) throw new TypeError('action plan has partial actions without a receipt');
    pending.push(validateBrowserActionPlan(plan, {
      observation: state.observation,
      ledger: state.ledger,
    }));
  }
  if (pending.length > 1) throw new TypeError('only one action plan may be pending');
  return pending;
}

function actionPlanMatches(left, right) {
  try {
    return canonicalJson(left) === canonicalJson(right);
  } catch {
    return false;
  }
}

function requireFreshPlannedObservation(state, plan, postObservation) {
  assertRecord(postObservation, 'postObservation');
  if (postObservation.observation_id === state.observation.observation_id
    || postObservation.previous_observation_id !== plan.observation_id) {
    throw new TypeError('post observation must be fresh and chained to the action plan observation');
  }
}

function requireNoPendingPlan(state, operation) {
  if (state.pendingPlan !== null) {
    throw new TypeError(`${operation} is blocked while an action plan is pending`);
  }
}

function sessionHandle(state) {
  const session = {};
  Object.defineProperties(session, {
    run: { enumerable: true, value: state.run },
    profile: { enumerable: true, value: state.profile },
    memory: { enumerable: true, get: () => state.memory },
    runMetadata: { enumerable: true, value: state.runMetadata },
    ledger: { enumerable: true, get: () => state.ledger },
    observation: { enumerable: true, get: () => state.observation },
    retentionProofs: { enumerable: true, get: () => state.retentionProofs },
    pendingActionPlan: { enumerable: true, get: () => state.pendingPlan },
    finalized: { enumerable: true, get: () => state.finalized },
    faulted: { enumerable: true, get: () => state.faulted },
  });
  SESSION_STATES.set(session, state);
  return Object.freeze(session);
}

function sortedArtifactNames(store, pattern) {
  return store.listArtifacts().filter((name) => pattern.test(name)).sort();
}

function readResumeObservations(store) {
  const observations = [];
  const byId = new Map();
  for (const name of sortedArtifactNames(store, /^observation-\d+\.json$/)) {
    const value = store.readArtifact(name);
    try {
      validateObservation(value);
    } catch {
      throw new TypeError('resume observation artifact is invalid');
    }
    if (byId.has(value.observation_id)) throw new TypeError('resume observation identity was reused');
    byId.set(value.observation_id, { value, name });
    observations.push(value);
  }
  return { observations, byId };
}

function readResumeLedger(store) {
  const names = sortedArtifactNames(store, /^ledger-\d+\.json$/);
  if (names.length === 0) return null;
  let latest = null;
  for (const name of names) {
    const ledger = store.readArtifact(name);
    try {
      validateLedger(ledger);
    } catch {
      throw new TypeError('resume ledger artifact is invalid');
    }
    latest = ledger;
  }
  return latest;
}

function readResumeProofs(store) {
  const names = typeof store.listRetentionProofs === 'function'
    ? store.listRetentionProofs()
    : sortedArtifactNames(store, /^retention-\d+\.json$/);
  const byObservation = new Set();
  let latest = null;
  for (const name of names.sort()) {
    const aggregate = typeof store.readRetentionProofs === 'function'
      ? store.readRetentionProofs(name)
      : store.readArtifact(name);
    if (byObservation.has(aggregate.observation_id)) {
      throw new TypeError('resume retention proof identity was reused');
    }
    byObservation.add(aggregate.observation_id);
    latest = aggregate;
  }
  return latest === null ? Object.freeze({}) : frozenClone(latest.proofs);
}

function readResumeFinalAudit(store, observationId) {
  if (observationId === null) return null;
  let latest = null;
  for (const name of sortedArtifactNames(store, /^audit-\d+\.json$/)) {
    const audit = store.readArtifact(name);
    if (audit?.final === true
      && audit.complete === true
      && audit.passed === true
      && audit.observation_id === observationId) {
      latest = { artifact: name };
    }
  }
  return latest;
}
function canonicalSha256(value) {
  return createHash('sha256').update(canonicalJson(value), 'utf8').digest('hex');
}

function readResumeSubmissionAuthorization(store, ledger, finalSubmits) {
  if (ledger === null) return null;
  const lastFinalSubmit = finalSubmits.at(-1) ?? null;
  if (lastFinalSubmit !== null
    && lastFinalSubmit.observation_id === ledger.latest_observation_id) {
    return null;
  }
  const names = typeof store.listSubmissionAuthorizations === 'function'
    ? store.listSubmissionAuthorizations()
    : sortedArtifactNames(store, /^submission-authorization-\d+\.json$/);
  let latest = null;
  for (const name of names.sort()) {
    const authorization = typeof store.readSubmissionAuthorization === 'function'
      ? store.readSubmissionAuthorization(name)
      : store.readArtifact(name);
    if (authorization.observation_id === ledger.latest_observation_id
      && authorization.ledger_sha256 === canonicalSha256(ledger)) {
      latest = {
        finalRef: authorization.final_ref,
        observationId: authorization.observation_id,
        auditRef: { artifact: authorization.audit.artifact },
      };
    }
  }
  return latest;
}

function findExactArtifact(store, pattern, value) {
  let found = null;
  for (const name of sortedArtifactNames(store, pattern)) {
    const candidate = store.readArtifact(name);
    if (candidate?.observation_id === value.observation_id
      && canonicalJson(candidate) === canonicalJson(value)) {
      if (found !== null) throw new TypeError('duplicate exact evidence artifact');
      found = { name, value: candidate };
    } else if (candidate?.observation_id === value.observation_id) {
      throw new TypeError('conflicting evidence artifact identity');
    }
  }
  return found;
}

function persistExactLedger(state, ledger) {
  for (const name of sortedArtifactNames(state.evidence.store, /^ledger-\d+\.json$/)) {
    const candidate = state.evidence.store.readArtifact(name);
    if (canonicalJson(candidate) === canonicalJson(ledger)) return null;
  }
  return state.evidence.recordLedger(ledger);
}

async function persistPostObservation(state, postObservation) {
  const existing = findExactArtifact(state.evidence.store, /^observation-\d+\.json$/, postObservation);
  if (existing !== null) return existing;
  return { ref: await state.evidence.recordObservation(postObservation), value: postObservation };
}

async function persistDiff(state, diff) {
  for (const name of sortedArtifactNames(state.evidence.store, /^diff-\d+\.json$/)) {
    const candidate = state.evidence.store.readArtifact(name);
    if (canonicalJson(candidate) === canonicalJson(diff)) return null;
  }
  return state.evidence.recordDiff(diff);
}

function generatedProofObject(plan, validation) {
  const generated = {};
  if (validation.upload_proofs && !Array.isArray(validation.upload_proofs)) {
    for (const [fieldId, proof] of Object.entries(validation.upload_proofs)) generated[fieldId] = proof;
  } else if (Array.isArray(validation.upload_proofs)) {
    for (const proof of validation.upload_proofs) {
      const planned = plan.actions.find((action) => action.action_id === proof.action_id);
      if (!planned) throw new TypeError('upload proof references an unknown action');
      generated[planned.field_id] = proof;
    }
  }
  return generated;
}

function mergeProofObjects(left, right) {
  const merged = { ...left };
  for (const [fieldId, proof] of Object.entries(right)) {
    merged[fieldId] = frozenClone(proof);
  }
  return frozenClone(merged);
}
function actionRetentionSummary(validation, retention, observation) {
  const attempts = validation.attempts;
  const fieldIds = [...new Set(
    attempts
      .map((attempt) => attempt.field_id)
      .filter((fieldId) => fieldId !== null),
  )];
  const attemptedFieldIds = new Set(fieldIds);
  const errors = retention.errors
    .filter((error) => attemptedFieldIds.has(error.field_id))
    .map((error) => ({ ...error }));
  const fieldsById = new Map(retention.ledger.fields.map((field) => [field.field_id, field]));
  let allOutcomesSucceeded = true;
  for (const attempt of attempts) {
    if (attempt.outcome === 'succeeded') continue;
    allOutcomesSucceeded = false;
    errors.push({
      code: 'ACTION_OUTCOME_FAILED',
      field_id: attempt.field_id,
      message: attempt.error_code ?? attempt.outcome,
    });
  }
  const allFieldsRetained = attempts.every((attempt) => {
    if (attempt.field_id === null) return true;
    const field = fieldsById.get(attempt.field_id);
    return field !== undefined
      && field.latest_observation_id === observation.observation_id
      && field.present_in_latest_observation === true
      && field.reachable === true
      && field.retained === true
      && field.valid === true;
  });
  return frozenClone({
    ok: allOutcomesSucceeded && allFieldsRetained,
    retryRequired: !(allOutcomesSucceeded && allFieldsRetained),
    fieldIds,
    errors,
  });
}


async function recoverReceiptInState(state, receipt) {
  const validation = validatedReceipt(receipt, { historical: true });
  const plan = receipt.plan;
  if (state.observation === null || state.ledger === null) {
    throw new TypeError('receipt recovery requires an initialized ledger');
  }
  const attempts = validation.attempts;
  const existing = new Map(state.ledger.action_attempts.map((action, index) => [
    action.action_id,
    { action, index },
  ]));
  const present = attempts.filter((attempt) => existing.has(attempt.action_id));
  if (present.length !== 0 && present.length !== attempts.length) {
    throw new TypeError('receipt recovery found partially applied actions');
  }

  if (present.length === attempts.length) {
    let priorIndex = -1;
    for (const attempt of attempts) {
      const recorded = existing.get(attempt.action_id);
      if (!actionAttemptsMatch(recorded.action, attempt) || recorded.index <= priorIndex) {
        throw new TypeError('receipt action conflicts with the ledger');
      }
      priorIndex = recorded.index;
    }
  } else {
    if (state.ledger.latest_observation_id !== plan.observation_id) {
      throw new TypeError('receipt action observation is not current');
    }
    const actionLedger = attempts.length === 1
      ? recordActionAttempt(state.ledger, attempts[0])
      : recordLedgerActionBatch(state.ledger, attempts);
    const normalizedActions = actionLedger.action_attempts.slice(-attempts.length);
    for (const action of normalizedActions) await state.evidence.recordAction(action);
    await persistExactLedger(state, actionLedger);
    state.ledger = actionLedger;
  }

  const postObservation = receipt.post_observation;
  const postRecord = await persistPostObservation(state, postObservation);
  const postAlreadyMerged = state.ledger.observation_ids.includes(postObservation.observation_id);
  if (postAlreadyMerged && state.ledger.latest_observation_id !== postObservation.observation_id) {
    return;
  }
  if (!postAlreadyMerged) {
    if (state.ledger.latest_observation_id !== plan.observation_id) {
      throw new TypeError('receipt post observation chain is not current');
    }
    const mergedLedger = mergeObservation(state.ledger, postObservation);
    await persistDiff(state, mergedLedger.diffs.at(-1));
    await persistExactLedger(state, mergedLedger);
    state.ledger = mergedLedger;
  }
  state.observation = frozenClone(postRecord.value);

  const generated = generatedProofObject(plan, validation);
  const proofs = mergeProofObjects(state.retentionProofs, generated);
  await state.evidence.recordRetentionProofs(postObservation.observation_id, proofs);
  const retention = verifyLedgerRetention(state.ledger, postObservation, proofs);
  await persistExactLedger(state, retention.ledger);
  state.ledger = retention.ledger;
  state.retentionProofs = proofs;
}
export async function startRun(runPath, options = {}) {
  const input = dataSnapshot(options, 'options');
  assertExactKeys(input, new Set(['startedAt', 'resume', 'agentInference', 'resumeExisting']), 'options');
  const resumeExisting = input.resumeExisting === true;
  const startedAt = input.startedAt ?? new Date().toISOString();
  if (typeof startedAt !== 'string' || !Number.isFinite(Date.parse(startedAt))) {
    throw new TypeError('options.startedAt must be an ISO date string');
  }

  const { run, identity: runIdentity } = await loadRunContractSnapshot(runPath, { local: false });
  const {
    profile,
    memory,
    resumeIdentity,
    sourceResumeIdentity,
    jobDescriptionIdentity,
  } = await loadRunInputs(run);
  const resume = boundCandidate(input.resume, sourceResumeIdentity, 'options.resume');
  const agentInference = boundAgentInference(
    input.agentInference,
    sourceResumeIdentity,
    jobDescriptionIdentity,
    'options.agentInference',
  );

  await ensurePrivateDirectory(run.run_artifact_dir, { create: true });
  const runMetadata = {
    schema: 'phase1-run-evidence-v1',
    application_url: run.application_url,
    run_contract_sha256: runIdentity.sha256,
    resume_upload_path: resumeIdentity.path,
    resume_upload_sha256: resumeIdentity.sha256,
    browser_mode: run.browser_mode,
    observer: run.observer,
    action_driver: run.action_driver,
    submit_policy: run.submit_policy,
    loop_contract: 'safe-batch-observe-act-reobserve',
    started_at: new Date(startedAt).toISOString(),
  };
  const evidence = await createEvidenceStore(run.run_artifact_dir, resumeExisting ? undefined : runMetadata, {
    maxInputBytes: MAX_RESUME_BYTES,
    maxJsonBytes: MAX_RESUME_BYTES * 2,
  });
  try {
    const persistedMetadata = resumeExisting ? evidence.store.readArtifact('run.json') : runMetadata;
    if (resumeExisting
      && (persistedMetadata.run_contract_sha256 !== runIdentity.sha256
        || persistedMetadata.resume_upload_sha256 !== resumeIdentity.sha256)) {
      throw new TypeError('resume evidence identity mismatch');
    }

    const existingLedger = resumeExisting ? readResumeLedger(evidence.store) : null;
    const resumeObservationData = resumeExisting
      ? readResumeObservations(evidence.store)
      : { observations: [], byId: new Map() };
    const existingObservation = existingLedger === null
      ? null
      : resumeObservationData.byId.get(existingLedger.latest_observation_id)?.value ?? null;
    if (resumeExisting && (existingLedger === null || existingObservation === null)) {
      throw new TypeError('resume evidence is incomplete');
    }
    if (resumeExisting && existingLedger !== null) {
      for (const observationId of existingLedger.observation_ids) {
        if (!resumeObservationData.byId.has(observationId)) {
          throw new TypeError('resume ledger references a missing observation');
        }
      }
      evidence.store.readActionJournal();
    }

    const persistedFinalSubmits = existingLedger?.action_attempts
      .filter((action) => action.action === 'final_submit') ?? [];
    const persistedSuccessfulSubmit = persistedFinalSubmits.some((action) => action.outcome === 'succeeded');
    const resumedAuthorization = resumeExisting && !persistedSuccessfulSubmit
      ? readResumeSubmissionAuthorization(evidence.store, existingLedger, persistedFinalSubmits)
      : null;
    const state = {
      run: frozenClone(run),
      profile: profile === null ? null : frozenClone(profile),
      memory: frozenClone(memory),
      resume,
      agentInference,
      runMetadata: frozenClone(persistedMetadata),
      evidence,
      ledger: existingLedger === null ? null : frozenClone(existingLedger),
      observation: existingObservation === null ? null : frozenClone(existingObservation),
      retentionProofs: resumeExisting ? readResumeProofs(evidence.store) : Object.freeze({}),
      pendingPlan: null,
      finalized: false,
      faulted: false,
      busy: false,
      submissionAuthorized: resumedAuthorization !== null,
      authorizedFinalRef: resumedAuthorization?.finalRef ?? null,
      authorizedObservationId: resumedAuthorization?.observationId ?? null,
      submissionSucceeded: persistedSuccessfulSubmit,
      lastFinalAttemptObservationId: persistedFinalSubmits.at(-1)?.observation_id ?? null,
      preSubmitAuditRef: resumeExisting
        ? readResumeFinalAudit(
          evidence.store,
          persistedFinalSubmits.at(-1)?.observation_id ?? null,
        ) ?? resumedAuthorization?.auditRef ?? null
        : null,
    };

    if (resumeExisting) {
      const plans = actionPlanRecords(state);
      const planById = new Map(plans.map((plan) => [plan.plan_id, plan]));
      const receipts = actionResultRecords(state);
      const order = new Map(plans.map((plan, index) => [plan.plan_id, index]));
      receipts.sort((left, right) => {
        if (!order.has(left.plan.plan_id) || !order.has(right.plan.plan_id)) {
          throw new TypeError('action result references an unknown plan');
        }
        return order.get(left.plan.plan_id) - order.get(right.plan.plan_id);
      });
      for (const receipt of receipts) {
        if (!planById.has(receipt.plan.plan_id)
          || !actionPlanMatches(planById.get(receipt.plan.plan_id), receipt.plan)) {
          throw new TypeError('action result does not match its action plan');
        }
        await recoverReceiptInState(state, receipt);
      }
    }
    state.pendingPlan = pendingActionPlanRecords(state)[0] ?? null;
    return sessionHandle(state);
  } catch (error) {
    await evidence.close();
    throw error;
  }
}

async function acceptObservationInState(state, observation, markPublished) {
  const accepted = frozenClone(observation);
  const mergedLedger = state.ledger === null
    ? createLedger(accepted)
    : mergeObservation(state.ledger, accepted);
  const diff = mergedLedger.diffs.at(-1);
  const nextLedger = mergedLedger.diffs.length === 1
    ? mergedLedger
    : Object.freeze({ ...mergedLedger, diffs: Object.freeze([diff]) });

  markPublished();
  const observationRef = await state.evidence.recordObservation(accepted);
  const diffRef = diff === null ? null : await state.evidence.recordDiff(diff);
  const ledgerRef = await state.evidence.recordLedger(nextLedger);
  invalidateSubmissionPreparation(state);
  state.observation = accepted;
  state.ledger = nextLedger;
  return Object.freeze({ observation: accepted, observationRef, diff, diffRef, ledger: nextLedger, ledgerRef });
}

export async function acceptObservation(session, observation) {
  return transact(session, async (state, markPublished) => {
    requireNoPendingPlan(state, 'acceptObservation');
    return acceptObservationInState(state, observation, markPublished);
  });
}

export async function resolveField(session, options) {
  const input = dataSnapshot(options, 'options');
  assertExactKeys(input, new Set([
    'field_id',
    'alias',
    'user',
    'deliberate_blank',
    'semantic_choice',
    'sensitive',
    'formatted_value',
    'remember',
    'approved_at',
  ]), 'options');
  return transact(session, async (state, markPublished) => {
    requireNoPendingPlan(state, 'resolveField');
    requireObservationState(state);
    if (state.submissionSucceeded) {
      throw new TypeError('field resolution is unavailable after final submission succeeds');
    }
    if (typeof input.field_id !== 'string' || typeof input.alias !== 'string') {
      throw new TypeError('options.field_id and options.alias must be strings');
    }
    if (input.alias.length === 0
      || input.alias.length > MAX_ALIAS_LENGTH
      || input.alias.includes('\0')
      || input.alias.trim().length === 0) {
      throw new TypeError('options.alias must be a valid answer alias');
    }
    if (input.sensitive !== undefined && typeof input.sensitive !== 'boolean') {
      throw new TypeError('options.sensitive must be boolean');
    }
    if (input.remember !== undefined && typeof input.remember !== 'boolean') {
      throw new TypeError('options.remember must be boolean');
    }
    const remember = input.remember === true;
    if (remember && input.approved_at === undefined) {
      throw new TypeError('remembered answers require options.approved_at');
    }
    if (!remember && input.approved_at !== undefined) {
      throw new TypeError('options.approved_at requires remember');
    }
    const deliberateBlank = input.deliberate_blank === true;
    if (input.deliberate_blank !== undefined && typeof input.deliberate_blank !== 'boolean') {
      throw new TypeError('options.deliberate_blank must be boolean');
    }
    if (input.semantic_choice !== undefined && !semanticChoiceIsDeliberate(input.semantic_choice)) {
      throw new TypeError('options.semantic_choice must be a supported deliberate choice');
    }
    if (deliberateBlank && input.semantic_choice === undefined) {
      throw new TypeError('deliberate blanks require options.semantic_choice');
    }
    if (!deliberateBlank && input.semantic_choice !== undefined) {
      throw new TypeError('options.semantic_choice requires deliberate_blank');
    }
    if (input.formatted_value !== undefined
      && (typeof input.formatted_value !== 'string'
        || input.formatted_value.length === 0
        || input.formatted_value.length > MAX_FORMATTED_VALUE_LENGTH
        || input.formatted_value.includes('\0'))) {
      throw new TypeError('options.formatted_value must be a non-empty bounded string');
    }
    if (deliberateBlank && input.formatted_value !== undefined) {
      throw new TypeError('options.formatted_value is unavailable for deliberate blanks');
    }
    const field = state.ledger.fields.find((item) => item.field_id === input.field_id);
    if (!field
      || !field.present_in_latest_observation
      || !field.reachable
      || field.final
      || field.latest_observation_id !== state.ledger.latest_observation_id) {
      throw new TypeError('options.field_id is not a current ledger field');
    }
    if (requiresReobservation(state.ledger)) {
      throw new TypeError('latest observation was consumed by a field mutation; accept a fresh observation');
    }
    const classifiedSensitive = isSensitiveInferenceField(input.alias, field);
    const workingLedger = input.sensitive === true || classifiedSensitive
      ? markFieldSensitive(state.ledger, field.field_id)
      : state.ledger;
    const workingField = workingLedger.fields.find((item) => item.field_id === field.field_id);
    const sensitive = workingField.sensitive === true;
    const allowAgentInference = !sensitive;
    const approvalContext = {
      run_contract_sha256: state.runMetadata.run_contract_sha256,
      observation_id: workingLedger.latest_observation_id,
      field_id: workingField.field_id,
      alias: input.alias,
    };
    const approvalContextDigest = approvalContextSha256(approvalContext);
    if (input.approved_at !== undefined) {
      try {
        createAnswerRecord({
          alias: input.alias,
          value: null,
          approved_at: input.approved_at,
          approval_context: approvalContext,
          approval_context_sha256: approvalContextDigest,
        });
      } catch {
        throw new TypeError('options.approved_at must be an exact ISO date string');
      }
    }
    const answer = resolveAnswer({
      alias: input.alias,
      memory: state.memory,
      profile: state.profile ?? undefined,
      resume: sensitive ? undefined : state.resume,
      agentInference: allowAgentInference ? state.agentInference : undefined,
      user: input.user,
    });
    if (answer.missing && input.formatted_value !== undefined) {
      throw new TypeError('options.formatted_value requires a resolved answer source');
    }
    if (remember && answer.missing) {
      throw new TypeError('remembered answers require an explicit user answer');
    }
    if (answer.missing) {
      if (workingLedger === state.ledger) {
        invalidateSubmissionPreparation(state);
        return Object.freeze({ missing: true, answer, ledger: state.ledger, ledgerRef: null });
      }
      markPublished();
      const ledgerRef = await state.evidence.recordLedger(workingLedger);
      invalidateSubmissionPreparation(state);
      state.ledger = workingLedger;
      return Object.freeze({ missing: true, answer, ledger: workingLedger, ledgerRef });
    }
    if (deliberateBlank && !DELIBERATE_BLANK_SOURCES.has(answer.source)) {
      throw new TypeError('deliberate blanks require memory, profile, evidence-backed agent inference, or user evidence');
    }
    if (remember && answer.source !== 'user') {
      throw new TypeError('remembered answers require user as the selected answer source');
    }
    const actionValue = deliberateBlank ? null : (input.formatted_value ?? answer.value);
    const resolution = {
      field_id: workingField.field_id,
      observation_id: workingLedger.latest_observation_id,
      ref: workingField.latest_ref,
      source: answer.source,
      value_digest: deliberateBlank ? null : digestObservedValue(workingField, actionValue),
      inference_rationale_digest: answer.source === 'agent_inference' ? answer.inference_rationale_digest ?? null : null,
      inference_evidence_digests: answer.source === 'agent_inference' ? answer.inference_evidence_digests ?? null : null,
    };
    if (input.semantic_choice !== undefined) resolution.semantic_choice = input.semantic_choice;
    if (sensitive || input.sensitive !== undefined) resolution.sensitive = sensitive;
    const nextLedger = recordResolution(workingLedger, resolution);
    const memoryRecord = remember
      ? createAnswerRecord({
        alias: input.alias,
        value: answer.value,
        approved_at: input.approved_at,
        approval_context: approvalContext,
        approval_context_sha256: approvalContextDigest,
      })
      : null;
    let nextMemory = state.memory;

    markPublished();
    const ledgerRef = await state.evidence.recordLedger(nextLedger);
    if (memoryRecord !== null) {
      await appendAnswerRecord(state.run.answer_memory_path, memoryRecord);
      nextMemory = frozenClone(await loadAnswerMemory(state.run.answer_memory_path));
    }
    invalidateSubmissionPreparation(state);
    state.memory = nextMemory;
    state.ledger = nextLedger;
    return Object.freeze({
      missing: false,
      answer,
      actionValue: structuredClone(actionValue),
      resolution: Object.freeze(resolution),
      ledger: nextLedger,
      ledgerRef,
    });
  });
}
export async function resolveCanonicalUpload(session, options) {
  const input = dataSnapshot(options, 'options');
  assertExactKeys(input, new Set(['field_id', 'alias']), 'options');
  if (typeof input.field_id !== 'string' || input.field_id.length === 0
    || typeof input.alias !== 'string' || input.alias.length === 0
    || input.alias.trim().length === 0
    || input.alias.length > MAX_ALIAS_LENGTH || input.alias.includes('\0')) {
    throw new TypeError('options.field_id and options.alias must be valid strings');
  }
  return transact(session, async (state, markPublished) => {
    requireNoPendingPlan(state, 'resolveCanonicalUpload');
    requireObservationState(state);
    if (state.submissionSucceeded || requiresReobservation(state.ledger)) {
      throw new TypeError('canonical upload resolution is unavailable in the current state');
    }
    const field = state.ledger.fields.find((item) => item.field_id === input.field_id);
    if (!field
      || !field.present_in_latest_observation
      || !field.reachable
      || field.final
      || field.latest_observation_id !== state.ledger.latest_observation_id) {
      throw new TypeError('options.field_id is not a current ledger field');
    }
    const control = state.observation.controls.find((item) => item.ref === field.latest_ref);
    const controlKinds = [control?.kind, control?.tag, control?.type, control?.role]
      .filter((value) => typeof value === 'string')
      .map((value) => value.toLowerCase());
    if (!control || (!controlKinds.includes('file') && control.file == null)) {
      throw new TypeError('options.field_id must reference a current file control');
    }
    const actionValue = state.runMetadata.resume_upload_path;
    const answer = Object.freeze({
      alias: input.alias,
      source: 'resume',
      value: actionValue,
      missing: false,
    });
    const resolution = {
      field_id: field.field_id,
      observation_id: state.ledger.latest_observation_id,
      ref: field.latest_ref,
      source: 'resume',
      value_digest: digestObservedValue(control, actionValue),
      inference_rationale_digest: null,
      inference_evidence_digests: null,
    };
    const nextLedger = recordResolution(state.ledger, resolution);

    markPublished();
    const ledgerRef = await state.evidence.recordLedger(nextLedger);
    invalidateSubmissionPreparation(state);
    state.ledger = nextLedger;
    return Object.freeze({
      missing: false,
      answer,
      actionValue,
      resolution: Object.freeze(resolution),
      ledger: nextLedger,
      ledgerRef,
    });
  });
}

async function recordActionInState(state, input, markPublished) {
  requireObservationState(state);
  const normalized = Object.hasOwn(input, 'observation_id')
    ? { ...input }
    : { ...input, observation_id: state.ledger.latest_observation_id };
  if (normalized.action === 'final_submit') {
    throw new TypeError('final_submit must use beginFinalSubmit and completeFinalSubmit');
  }
  if (normalized.action === 'submit') {
    throw new TypeError('automated submission must use beginFinalSubmit and completeFinalSubmit');
  }
  const fieldId = normalized.field_id ?? null;
  const field = fieldId === null
    ? null
    : state.ledger.fields.find((item) => item.field_id === fieldId) ?? null;
  if (field !== null && normalized.ref === undefined) normalized.ref = field.latest_ref;
  const nextLedger = recordActionAttempt(state.ledger, normalized);
  if (requiresReobservation(state.ledger)) {
    throw new TypeError('latest observation was consumed by a field mutation; accept a fresh observation');
  }
  const action = nextLedger.action_attempts.at(-1);

  markPublished();
  const actionRef = await state.evidence.recordAction(action);
  const shouldRecordRetry = action.retry_of !== null || action.outcome === 'failed' || action.outcome === 'retry';
  const retryRef = shouldRecordRetry ? await state.evidence.recordRetry(action) : null;
  const ledgerRef = await state.evidence.recordLedger(nextLedger);
  state.ledger = nextLedger;
  invalidateSubmissionPreparation(state);
  return Object.freeze({ action, actionRef, retryRef, ledger: nextLedger, ledgerRef });
}

export async function recordAction(session, attempt) {
  const input = dataSnapshot(attempt, 'attempt');
  return transact(session, async (state, markPublished) => {
    requireNoPendingPlan(state, 'recordAction');
    return recordActionInState(state, input, markPublished);
  });
}

async function recordActionBatchInState(state, input, markPublished) {
  requireObservationState(state);
  const normalizedAttempts = input.map((attempt) => {
    const normalized = { ...attempt };
    if (!Object.hasOwn(normalized, 'observation_id')) {
      normalized.observation_id = state.ledger.latest_observation_id;
    }
    const fieldId = normalized.field_id ?? null;
    const field = fieldId === null
      ? null
      : state.ledger.fields.find((item) => item.field_id === fieldId) ?? null;
    if (field !== null && normalized.ref === undefined) normalized.ref = field.latest_ref;
    return normalized;
  });
  const nextLedger = recordLedgerActionBatch(state.ledger, normalizedAttempts);
  const actions = nextLedger.action_attempts.slice(state.ledger.action_attempts.length);
  const actionRefs = [];
  const retryRefs = [];

  markPublished();
  for (const action of actions) {
    actionRefs.push(await state.evidence.recordAction(action));
    const shouldRecordRetry = action.retry_of !== null
      || action.outcome === 'failed'
      || action.outcome === 'retry'
      || action.outcome === 'blocked';
    retryRefs.push(shouldRecordRetry ? await state.evidence.recordRetry(action) : null);
  }
  const ledgerRef = await state.evidence.recordLedger(nextLedger);
  state.ledger = nextLedger;
  invalidateSubmissionPreparation(state);
  return Object.freeze({
    actions: Object.freeze(actions),
    actionRefs: Object.freeze(actionRefs),
    retryRefs: Object.freeze(retryRefs),
    ledger: nextLedger,
    ledgerRef,
  });
}

export async function recordActionBatch(session, attempts) {
  if (!Array.isArray(attempts)) throw new TypeError('attempts must be an array');
  const input = snapshotValue(attempts, 'attempts', new Set());
  return transact(session, async (state, markPublished) => {
    requireNoPendingPlan(state, 'recordActionBatch');
    return recordActionBatchInState(state, input, markPublished);
  });
}

export async function recordActionPlan(session, plan) {
  const input = dataSnapshot(plan, 'plan');
  return transact(session, async (state, markPublished) => {
    requireObservationState(state);
    if (state.submissionSucceeded || hasPendingFinalSubmit(state) || state.submissionAuthorized) {
      throw new TypeError('action plans cannot be created during or after final submission');
    }
    const normalized = validateBrowserActionPlan(input, {
      observation: state.observation,
      ledger: state.ledger,
    });
    const existing = actionPlanRecords(state);
    if (existing.some((item) => item.plan_id === normalized.plan_id)) {
      throw new TypeError('action plan identity was reused');
    }
    const ledgerActionIds = new Set(state.ledger.action_attempts.map((action) => action.action_id));
    if (normalized.actions.some((action) => ledgerActionIds.has(action.action_id))) {
      throw new TypeError('action identity was reused');
    }
    if (pendingActionPlanRecords(state).length > 0) {
      throw new TypeError('only one action plan may be pending');
    }

    markPublished();
    const planRef = await state.evidence.recordActionPlan(normalized);
    invalidateSubmissionPreparation(state);
    state.pendingPlan = normalized;
    return frozenClone({ plan: normalized, planRef });
  });
}

export async function getPendingActionPlans(session) {
  const state = stateFor(session);
  if (state.busy) throw new Error('session operation already in progress');
  const pending = pendingActionPlanRecords(state);
  state.pendingPlan = pending[0] ?? null;
  return frozenClone(pending);
}

export async function recordPlannedActionResult(session, plan, result, postObservation) {
  const planInput = dataSnapshot(plan, 'plan');
  const resultInput = dataSnapshot(result, 'result');
  const postInput = dataSnapshot(postObservation, 'postObservation');
  return transact(session, async (state, markPublished) => {
    requireObservationState(state);
    const pending = pendingActionPlanRecords(state);
    if (pending.length !== 1) {
      throw new TypeError('exactly one pending action plan is required');
    }
    const normalizedPlan = validateBrowserActionPlan(planInput, {
      observation: state.observation,
      ledger: state.ledger,
    });
    if (!actionPlanMatches(normalizedPlan, pending[0])) {
      throw new TypeError('action plan does not match the pending plan');
    }
    requireFreshPlannedObservation(state, normalizedPlan, postInput);
    const receipt = {
      schema: 'phase1-browser-action-execution-v1',
      plan: normalizedPlan,
      result: resultInput,
      post_observation: postInput,
    };
    const validation = validatedReceipt(receipt);
    const ledgerActionIds = new Set(state.ledger.action_attempts.map((action) => action.action_id));
    if (validation.attempts.some((attempt) => ledgerActionIds.has(attempt.action_id))) {
      throw new TypeError('planned action result reused an action identity');
    }

    markPublished();
    const receiptRef = await state.evidence.recordActionResult(normalizedPlan, resultInput, postInput);
    let recorded;
    if (validation.attempts.length === 1) {
      recorded = await recordActionInState(state, validation.attempts[0], markPublished);
    } else if (validation.attempts.every((attempt) => attempt.action === 'fill')) {
      recorded = await recordActionBatchInState(state, validation.attempts, markPublished);
    } else {
      throw new TypeError('planned action result must use a routine fill batch or one action');
    }
    const accepted = await acceptObservationInState(state, postInput, markPublished);
    const generated = generatedProofObject(normalizedPlan, validation);
    const proofs = mergeProofObjects(state.retentionProofs, generated);
    await state.evidence.recordRetentionProofs(postInput.observation_id, proofs);
    const retention = verifyLedgerRetention(state.ledger, postInput, proofs);
    const retentionLedgerRef = await state.evidence.recordLedger(retention.ledger);
    state.ledger = retention.ledger;
    state.retentionProofs = proofs;
    state.pendingPlan = null;
    const actionRetention = actionRetentionSummary(validation, retention, postInput);
    return frozenClone({
      receiptRef,
      validation,
      recorded,
      accepted,
      retention,
      retentionLedgerRef,
      actionRetention,
    });
  });
}

function nextFinalSubmitActionId(ledger) {
  const actionIds = new Set(ledger.action_attempts.map((action) => action.action_id));
  let sequence = ledger.action_attempts.length + 1;
  let actionId = `action-${sequence}`;
  while (actionIds.has(actionId)) {
    sequence += 1;
    actionId = `action-${sequence}`;
  }
  return actionId;
}

export async function beginFinalSubmit(session) {
  return transact(session, async (state, markPublished) => {
    requireNoPendingPlan(state, 'beginFinalSubmit');
    requireObservationState(state);
    if (state.submissionSucceeded || state.ledger.action_attempts.some(
      (action) => action.action === 'final_submit' && action.outcome === 'succeeded',
    )) {
      throw new TypeError('final submission already succeeded');
    }
    if (hasPendingFinalSubmit(state)) {
      throw new TypeError('final submission attempt is pending; completeFinalSubmit is required');
    }
    if (!state.submissionAuthorized) {
      throw new TypeError('final submission requires prepareSubmission authorization first');
    }
    if (state.authorizedObservationId !== state.ledger.latest_observation_id) {
      throw new TypeError('final submission authorization is stale');
    }

    const nextLedger = recordActionAttempt(state.ledger, {
      action_id: nextFinalSubmitActionId(state.ledger),
      action: 'final_submit',
      observation_id: state.authorizedObservationId,
      ref: state.authorizedFinalRef,
      outcome: 'attempted',
    });
    const action = nextLedger.action_attempts.at(-1);

    markPublished();
    const actionRef = await state.evidence.recordAction(action);
    const ledgerRef = await state.evidence.recordLedger(nextLedger);
    state.ledger = nextLedger;
    state.lastFinalAttemptObservationId = action.observation_id;
    state.submissionAuthorized = false;
    state.authorizedFinalRef = null;
    state.authorizedObservationId = null;
    return Object.freeze({
      attemptId: action.action_id,
      ref: action.ref,
      observationId: action.observation_id,
      action,
      actionRef,
      ledger: nextLedger,
      ledgerRef,
    });
  });
}

export async function completeFinalSubmit(session, options) {
  const input = dataSnapshot(options, 'options');
  assertExactKeys(input, new Set(['attemptId', 'outcome', 'errorCode']), 'options');
  if (typeof input.attemptId !== 'string' || input.attemptId.length === 0) {
    throw new TypeError('options.attemptId must be a non-empty string');
  }
  if (typeof input.outcome !== 'string' || !FINAL_SUBMIT_TERMINAL_OUTCOMES.has(input.outcome)) {
    throw new TypeError('options.outcome must be succeeded, failed, or blocked');
  }
  const errorCode = input.errorCode ?? null;
  if (errorCode !== null && (typeof errorCode !== 'string' || errorCode.length === 0)) {
    throw new TypeError('options.errorCode must be a non-empty string when provided');
  }

  return transact(session, async (state, markPublished) => {
    requireNoPendingPlan(state, 'completeFinalSubmit');
    requireObservationState(state);
    const nextLedger = resolveFinalSubmitAttempt(state.ledger, {
      action_id: input.attemptId,
      outcome: input.outcome,
      error_code: errorCode,
    });
    const result = {
      action_id: `${input.attemptId}-result`,
      action: 'final_submit_result',
      attempt_id: input.attemptId,
      outcome: input.outcome,
      error_code: errorCode,
    };

    markPublished();
    const resultRef = await state.evidence.recordAction(result);
    const ledgerRef = await state.evidence.recordLedger(nextLedger);
    state.ledger = nextLedger;
    state.submissionAuthorized = false;
    state.authorizedFinalRef = null;
    state.authorizedObservationId = null;
    if (input.outcome === 'succeeded') {
      state.submissionSucceeded = true;
    } else {
      state.preSubmitAuditRef = null;
    }
    return Object.freeze({
      attemptId: input.attemptId,
      outcome: input.outcome,
      result,
      resultRef,
      ledger: nextLedger,
      ledgerRef,
    });
  });
}


export async function verifyRetention(session, proofs = undefined) {
  return transact(session, async (state, markPublished) => {
    requireNoPendingPlan(state, 'verifyRetention');
    requireObservationState(state);
    const acceptedProofs = proofs === undefined ? state.retentionProofs : frozenClone(proofs);
    const retention = verifyLedgerRetention(state.ledger, state.observation, acceptedProofs);

    markPublished();
    const proofRef = await state.evidence.recordRetentionProofs(state.observation.observation_id, acceptedProofs);
    const ledgerRef = await state.evidence.recordLedger(retention.ledger);
    invalidateSubmissionPreparation(state);
    state.ledger = retention.ledger;
    state.retentionProofs = acceptedProofs;
    return Object.freeze({ ...retention, proofRef, ledgerRef });
  });
}
export async function prepareSubmission(session, options) {
  const input = dataSnapshot(options, 'options');
  assertExactKeys(input, new Set(['finalRef']), 'options');
  if (typeof input.finalRef !== 'string' || input.finalRef.length === 0) {
    throw new TypeError('options.finalRef must be a non-empty string');
  }

  return transact(session, async (state, markPublished) => {
    requireNoPendingPlan(state, 'prepareSubmission');
    requireObservationState(state);
    if (state.submissionSucceeded || state.ledger.action_attempts.some(
      (action) => action.action === 'final_submit' && action.outcome === 'succeeded',
    )) {
      throw new TypeError('submission preparation is unavailable after final submission succeeds');
    }
    if (hasPendingFinalSubmit(state)) {
      throw new TypeError('final submission attempt is pending; completeFinalSubmit is required');
    }
    invalidateSubmissionPreparation(state);
    if (state.lastFinalAttemptObservationId === state.ledger.latest_observation_id) {
      throw new TypeError('accept a fresh observation before retrying final submission');
    }
    const retention = verifyLedgerRetention(state.ledger, state.observation, state.retentionProofs);
    const audit = auditCompletion(retention.ledger, state.observation);

    markPublished();
    const ledgerRef = await state.evidence.recordLedger(retention.ledger);
    state.ledger = retention.ledger;
    if (!retention.ok || !audit.complete) {
      const auditRef = await state.evidence.recordAudit(audit);
      return Object.freeze({ authorized: false, retention, audit, ledgerRef, auditRef });
    }
    if (!audit.final_candidate_refs.includes(input.finalRef)) {
      const auditRef = await state.evidence.recordAudit(audit, { final: true });
      return Object.freeze({
        authorized: false,
        reason: 'selected final ref is not an audited final candidate',
        retention,
        audit,
        ledgerRef,
        auditRef,
      });
    }
    const auditRef = await state.evidence.recordAudit(audit, { final: true });
    const authorizationRef = await state.evidence.recordSubmissionAuthorization({
      schema: SUBMISSION_AUTHORIZATION_SCHEMA,
      observation_id: audit.observation_id,
      final_ref: input.finalRef,
      ledger_sha256: canonicalSha256(retention.ledger),
      audit: {
        artifact: auditRef.path,
        sha256: auditRef.sha256,
      },
    });
    state.submissionAuthorized = true;
    state.authorizedFinalRef = input.finalRef;
    state.authorizedObservationId = audit.observation_id;
    state.preSubmitAuditRef = auditRef;
    return Object.freeze({
      authorized: true,
      retention,
      audit,
      ledgerRef,
      auditRef,
      authorizationRef,
      authorizedFinalRef: state.authorizedFinalRef,
      authorizedObservationId: state.authorizedObservationId,
    });
  });
}

export async function finalizeRun(session, options) {
  const input = dataSnapshot(options, 'options');
  assertExactKeys(input, new Set(['screenshotPath', 'finalUrl']), 'options');
  if (typeof input.screenshotPath !== 'string' || input.screenshotPath.length === 0) {
    throw new TypeError('options.screenshotPath must be a non-empty string');
  }
  if (typeof input.finalUrl !== 'string' || input.finalUrl.length === 0) {
    throw new TypeError('options.finalUrl must be a non-empty string');
  }
  let finalUrl;
  try {
    finalUrl = new URL(input.finalUrl);
  } catch {
    throw new TypeError('options.finalUrl must be an absolute http(s) URL');
  }
  if (finalUrl.protocol !== 'http:' && finalUrl.protocol !== 'https:') {
    throw new TypeError('options.finalUrl must be an absolute http(s) URL');
  }
  return transact(session, async (state, markPublished) => {
    requireNoPendingPlan(state, 'finalizeRun');
    requireObservationState(state);
    if (hasPendingFinalSubmit(state)) {
      throw new TypeError('finalizeRun rejects unresolved final submission attempts');
    }
    if (state.preSubmitAuditRef === null || !state.submissionSucceeded) {
      throw new TypeError('one authorized successful final_submit is required before finalizeRun');
    }
    const finalSubmits = state.ledger.action_attempts.filter(
      (action) => action.action === 'final_submit',
    );
    const succeededSubmits = finalSubmits.filter((action) => action.outcome === 'succeeded');
    if (state.ledger.submit_action_count !== finalSubmits.length
      || succeededSubmits.length !== 1) {
      throw new TypeError('finalizeRun requires exactly one successful final_submit');
    }
    const lastAttemptObservationId =
      state.lastFinalAttemptObservationId ?? succeededSubmits.at(-1).observation_id;
    const lastAttemptIndex = state.ledger.observation_ids.indexOf(lastAttemptObservationId);
    const currentObservationIndex =
      state.ledger.observation_ids.indexOf(state.observation.observation_id);
    if (lastAttemptIndex < 0 || currentObservationIndex <= lastAttemptIndex) {
      throw new TypeError('finalizeRun requires a fresh post-submit observation');
    }
    if (input.finalUrl !== state.observation.url) {
      throw new TypeError('options.finalUrl must match the post-submit observation URL');
    }

    markPublished();
    const ledgerRef = await state.evidence.recordLedger(state.ledger);
    const screenshotRef = await state.evidence.recordScreenshot(input.screenshotPath);
    const uploadRef = await state.evidence.recordUpload(state.runMetadata.resume_upload_path);
    const completionRef = await state.evidence.finalize({
      audit: state.preSubmitAuditRef,
      screenshot: screenshotRef,
      upload: uploadRef,
      finalUrl: input.finalUrl,
      submitActionCount: state.ledger.submit_action_count,
    });
    await state.evidence.close();
    state.finalized = true;
    return Object.freeze({
      finalized: true,
      ledgerRef,
      screenshotRef,
      uploadRef,
      completionRef,
      submitActionCount: state.ledger.submit_action_count,
      finalUrl: input.finalUrl,
    });
  });
}
