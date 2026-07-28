import {
  MAX_RESUME_BYTES,
  MAX_ALIAS_LENGTH,
  appendAnswerRecord,
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
  verifyRetention as verifyLedgerRetention,
} from './ledger.mjs';
import { auditCompletion } from './audit.mjs';
import { createEvidenceStore } from './evidence.mjs';

const SESSION_STATES = new WeakMap();
const DELIBERATE_BLANK_SOURCES = new Set(['memory', 'profile', 'agent_inference', 'user']);
const FINAL_SUBMIT_TERMINAL_OUTCOMES = new Set(['succeeded', 'failed', 'blocked']);

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
    finalized: { enumerable: true, get: () => state.finalized },
    faulted: { enumerable: true, get: () => state.faulted },
  });
  SESSION_STATES.set(session, state);
  return Object.freeze(session);
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
  const existingLedgerName = resumeExisting
    ? evidence.store.listArtifacts()
      .filter((name) => /^ledger-\d+\.json$/.test(name))
      .sort()
      .at(-1)
    : undefined;
  const existingLedger = existingLedgerName === undefined
    ? null
    : evidence.store.readArtifact(existingLedgerName);
  const existingObservationName = existingLedger === null
    ? undefined
    : evidence.store.listArtifacts()
      .filter((name) => /^observation-\d+\.json$/.test(name))
      .find((name) => evidence.store.readArtifact(name).observation_id === existingLedger.latest_observation_id);
  const existingObservation = existingObservationName === undefined
    ? null
    : evidence.store.readArtifact(existingObservationName);
  if (resumeExisting && (existingObservation === null || existingLedger === null)) {
    throw new TypeError('resume evidence is incomplete');
  }
  const persistedMetadata = resumeExisting ? evidence.store.readArtifact('run.json') : runMetadata;
  if (resumeExisting
    && (persistedMetadata.run_contract_sha256 !== runIdentity.sha256
      || persistedMetadata.resume_upload_sha256 !== resumeIdentity.sha256)) {
    throw new TypeError('resume evidence identity mismatch');
  }
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
    retentionProofs: Object.freeze({}),
    finalized: false,
    faulted: false,
    busy: false,
    submissionAuthorized: false,
    authorizedFinalRef: null,
    authorizedObservationId: null,
    preSubmitAuditRef: null,
    submissionSucceeded: false,
    lastFinalAttemptObservationId: null,
  };
  return sessionHandle(state);
}

export async function acceptObservation(session, observation) {
  return transact(session, async (state, markPublished) => {
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
    'remember',
    'approved_at',
  ]), 'options');
  return transact(session, async (state, markPublished) => {
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
    if (input.approved_at !== undefined) {
      try {
        createAnswerRecord(input.alias, null, input.approved_at);
      } catch {
        throw new TypeError('options.approved_at must be an exact ISO date string');
      }
    }
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
    const answer = resolveAnswer({
      alias: input.alias,
      memory: state.memory,
      profile: state.profile ?? undefined,
      resume: sensitive ? undefined : state.resume,
      agentInference: allowAgentInference ? state.agentInference : undefined,
      user: input.user,
    });
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
    const resolution = {
      field_id: workingField.field_id,
      observation_id: workingLedger.latest_observation_id,
      ref: workingField.latest_ref,
      source: answer.source,
      value_digest: deliberateBlank ? null : digestObservedValue(workingField, answer.value),
      inference_rationale_digest: answer.source === 'agent_inference' ? answer.inference_rationale_digest ?? null : null,
      inference_evidence_digests: answer.source === 'agent_inference' ? answer.inference_evidence_digests ?? null : null,
    };
    if (input.semantic_choice !== undefined) resolution.semantic_choice = input.semantic_choice;
    if (sensitive || input.sensitive !== undefined) resolution.sensitive = sensitive;
    const nextLedger = recordResolution(workingLedger, resolution);
    const memoryRecord = remember
      ? createAnswerRecord(input.alias, answer.value, input.approved_at)
      : null;
    let nextMemory = state.memory;

    markPublished();
    if (memoryRecord !== null) {
      await appendAnswerRecord(state.run.answer_memory_path, memoryRecord);
      nextMemory = frozenClone(await loadAnswerMemory(state.run.answer_memory_path));
    }
    const ledgerRef = await state.evidence.recordLedger(nextLedger);
    invalidateSubmissionPreparation(state);
    state.memory = nextMemory;
    state.ledger = nextLedger;
    return Object.freeze({ missing: false, answer, resolution: Object.freeze(resolution), ledger: nextLedger, ledgerRef });
  });
}

export async function recordAction(session, attempt) {
  const input = dataSnapshot(attempt, 'attempt');
  return transact(session, async (state, markPublished) => {
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
  });
}

export async function recordActionBatch(session, attempts) {
  if (!Array.isArray(attempts)) throw new TypeError('attempts must be an array');
  const input = snapshotValue(attempts, 'attempts', new Set());
  return transact(session, async (state, markPublished) => {
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
    requireObservationState(state);
    const nextLedger = resolveFinalSubmitAttempt(state.ledger, {
      action_id: input.attemptId,
      outcome: input.outcome,
      error_code: errorCode,
    });
    const result = {
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
    requireObservationState(state);
    const acceptedProofs = proofs === undefined ? state.retentionProofs : frozenClone(proofs);
    const retention = verifyLedgerRetention(state.ledger, state.observation, acceptedProofs);

    markPublished();
    const ledgerRef = await state.evidence.recordLedger(retention.ledger);
    invalidateSubmissionPreparation(state);
    state.ledger = retention.ledger;
    state.retentionProofs = acceptedProofs;
    return Object.freeze({ ...retention, ledgerRef });
  });
}
export async function prepareSubmission(session, options) {
  const input = dataSnapshot(options, 'options');
  assertExactKeys(input, new Set(['finalRef']), 'options');
  if (typeof input.finalRef !== 'string' || input.finalRef.length === 0) {
    throw new TypeError('options.finalRef must be a non-empty string');
  }

  return transact(session, async (state, markPublished) => {
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
    state.submissionAuthorized = true;
    state.authorizedFinalRef = input.finalRef;
    state.authorizedObservationId = audit.observation_id;
    const auditRef = await state.evidence.recordAudit(audit, { final: true });
    state.preSubmitAuditRef = auditRef;
    return Object.freeze({
      authorized: true,
      retention,
      audit,
      ledgerRef,
      auditRef,
      authorizedFinalRef: state.authorizedFinalRef,
      authorizedObservationId: state.authorizedObservationId,
    });
  });
}

export async function finalizeRun(session, options) {
  const input = dataSnapshot(options, 'options');
  assertExactKeys(input, new Set(['screenshotPath']), 'options');
  if (typeof input.screenshotPath !== 'string' || input.screenshotPath.length === 0) {
    throw new TypeError('options.screenshotPath must be a non-empty string');
  }
  return transact(session, async (state, markPublished) => {
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

    markPublished();
    const ledgerRef = await state.evidence.recordLedger(state.ledger);
    const screenshotRef = await state.evidence.recordScreenshot(input.screenshotPath);
    const uploadRef = await state.evidence.recordUpload(state.runMetadata.resume_upload_path);
    const completionRef = await state.evidence.finalize({
      audit: state.preSubmitAuditRef,
      screenshot: screenshotRef,
      upload: uploadRef,
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
    });
  });
}

export { finalizeRun as finalizePreSubmit };
