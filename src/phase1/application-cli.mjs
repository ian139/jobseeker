import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';

import {
  ensurePrivateDirectory,
  loadRunContractSnapshot,
  readJsonFileSecure,
} from './contract.mjs';
import { validateApplicationDecision } from './decision.mjs';
import { canonicalJson } from './evidence.mjs';
import { resolveGreenhouseEducationOption } from './greenhouse-catalog.mjs';
import {
  ACTION_RESULT_SCHEMA,
  createBrowserActionPlan,
} from './action-plan.mjs';
import {
  acceptObservation,
  beginFinalSubmit,
  completeFinalSubmit,
  finalizeRun,
  getPendingActionPlans,
  prepareSubmission,
  recordActionPlan,
  recordPlannedActionResult,
  resolveCanonicalUpload,
  resolveField,
  startRun,
  verifyRetention,
} from './session.mjs';

export const BROWSER_ACTION_REQUEST_SCHEMA = 'phase1-browser-action-request-v1';
export const MAX_CLI_JSON_BYTES = 2 * 1024 * 1024;

const SHA256 = /^[0-9a-f]{64}$/u;
const ISO_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/u;
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$/u;
const DRIVERS = new Set(['omp_browser', 'playwright_cli', 'computer']);
const ATS_VALUES = new Set(['greenhouse', 'ashby', 'company_site']);
const SEMANTIC_CHOICES = new Set([
  'blank',
  'decline',
  'not_applicable',
  'prefer_not_to_answer',
  'none',
]);
const COMMAND_FLAGS = Object.freeze({
  'accept-observation': Object.freeze(['run', 'observation']),
  plan: Object.freeze(['run', 'request', 'output']),
  'pending-plan': Object.freeze(['run', 'output']),
  'complete-action': Object.freeze(['run', 'plan', 'result', 'observation']),
  'verify-retention': Object.freeze(['run', 'proofs']),
  'prepare-submit': Object.freeze(['run', 'final-ref']),
  'begin-submit': Object.freeze(['run', 'output']),
  'complete-submit': Object.freeze(['run', 'attempt-id', 'outcome', 'error-code']),
  finalize: Object.freeze(['run', 'screenshot', 'final-url']),
});
const OPTIONAL_FLAGS = new Set(['proofs', 'error-code']);

export class ApplicationCliError extends Error {
  constructor(code, message = code) {
    super(message);
    this.name = 'ApplicationCliError';
    this.code = code;
  }
}

function fail(code, message = code) {
  throw new ApplicationCliError(code, message);
}

function isObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function exactKeys(value, keys, location) {
  if (!isObject(value)) fail('E_CLI_SCHEMA', `${location} must be an object`);
  const actual = Object.keys(value);
  if (actual.length !== keys.size || actual.some((key) => !keys.has(key))) {
    fail('E_CLI_SCHEMA', `${location} has invalid keys`);
  }
}

function boundedString(value, location, { nullable = false, identifier = false } = {}) {
  if (nullable && value === null) return;
  if (typeof value !== 'string' || value.length === 0 || value.length > 16_384
    || value.includes('\0') || (identifier && !IDENTIFIER.test(value))) {
    fail('E_CLI_SCHEMA', `${location} must be a bounded string`);
  }
}

function exactIsoTimestamp(value, location) {
  boundedString(value, location);
  if (!ISO_TIMESTAMP.test(value) || new Date(value).toISOString() !== value) {
    fail('E_CLI_SCHEMA', `${location} must be an exact ISO timestamp`);
  }
}

function validateJsonObject(value, location) {
  if (!isObject(value)) fail('E_CLI_SCHEMA', `${location} must be an object`);
  try {
    canonicalJson(value, { maxBytes: MAX_CLI_JSON_BYTES });
  } catch {
    fail('E_CLI_SCHEMA', `${location} must contain bounded JSON data`);
  }
}

export function parseCliArgs(argv = []) {
  if (!Array.isArray(argv) || argv.length === 0 || typeof argv[0] !== 'string') {
    fail('E_CLI_ARGUMENTS');
  }
  const command = argv[0];
  const flagNames = COMMAND_FLAGS[command];
  if (flagNames === undefined) fail('E_CLI_COMMAND');
  const allowed = new Set(flagNames);
  const args = {};
  for (let index = 1; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (typeof flag !== 'string' || !/^--[a-z][a-z0-9-]*$/u.test(flag)
      || !allowed.has(flag.slice(2)) || Object.hasOwn(args, flag.slice(2))) {
      fail('E_CLI_ARGUMENTS');
    }
    if (typeof value !== 'string' || value.length === 0 || value.startsWith('--') || value.includes('\0')) {
      fail('E_CLI_ARGUMENTS');
    }
    args[flag.slice(2)] = value;
  }
  for (const flagName of allowed) {
    if (!OPTIONAL_FLAGS.has(flagName) && !Object.hasOwn(args, flagName)) {
      fail('E_CLI_ARGUMENTS');
    }
  }
  return Object.freeze({ command, args: Object.freeze(args) });
}

export async function secureReadJson(filePath) {
  return readJsonFileSecure(path.resolve(filePath), {
    maxBytes: MAX_CLI_JSON_BYTES,
    ownerOnly: true,
  });
}

function currentUid() {
  if (typeof process.geteuid === 'function') return process.geteuid();
  return typeof process.getuid === 'function' ? process.getuid() : null;
}

async function secureOutputParent(filePath) {
  const parentPath = path.dirname(filePath);
  await ensurePrivateDirectory(parentPath);
  const status = await fsp.lstat(parentPath);
  const uid = currentUid();
  if (!status.isDirectory() || status.isSymbolicLink()
    || (status.mode & 0o777) !== 0o700 || (uid !== null && status.uid !== uid)) {
    fail('E_CLI_OUTPUT_SECURITY');
  }
  return parentPath;
}

export async function secureWriteJson(filePath, value) {
  if (typeof filePath !== 'string' || filePath.length === 0 || filePath.includes('\0')) {
    fail('E_CLI_OUTPUT_SECURITY');
  }
  const target = path.resolve(filePath);
  const parentPath = await secureOutputParent(target);
  let bytes;
  try {
    bytes = Buffer.from(canonicalJson(value, { maxBytes: MAX_CLI_JSON_BYTES }), 'utf8');
  } catch {
    fail('E_CLI_OUTPUT_INVALID');
  }
  const temporaryPath = path.join(
    parentPath,
    `.${path.basename(target)}.${process.pid}.${crypto.randomBytes(12).toString('hex')}.tmp`,
  );
  let handle = null;
  try {
    handle = await fsp.open(
      temporaryPath,
      fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL
        | (fs.constants.O_NOFOLLOW ?? 0),
      0o600,
    );
    await handle.writeFile(bytes);
    await handle.sync();
    await handle.close();
    handle = null;
    try {
      await fsp.link(temporaryPath, target);
    } catch (error) {
      if (error?.code === 'EEXIST') fail('E_CLI_OUTPUT_EXISTS');
      throw error;
    }
    const directory = await fsp.open(parentPath, fs.constants.O_RDONLY);
    try {
      await directory.sync();
    } finally {
      await directory.close();
    }
  } finally {
    if (handle !== null) await handle.close().catch(() => {});
    await fsp.unlink(temporaryPath).catch(() => {});
  }
  const status = await fsp.lstat(target);
  if (!status.isFile() || status.isSymbolicLink() || (status.mode & 0o777) !== 0o600) {
    fail('E_CLI_OUTPUT_SECURITY');
  }
  return target;
}

function validateResumeCandidate(value) {
  if (value === null) return;
  exactKeys(value, new Set(['source_sha256', 'answers']), '$.resume');
  if (!SHA256.test(value.source_sha256)) fail('E_CLI_SCHEMA', '$.resume.source_sha256');
  validateJsonObject(value.answers, '$.resume.answers');
}

function validateInferenceCandidate(value) {
  if (value === null) return;
  exactKeys(
    value,
    new Set(['source_resume_sha256', 'job_description_sha256', 'answers']),
    '$.agent_inference',
  );
  if (!SHA256.test(value.source_resume_sha256) || !SHA256.test(value.job_description_sha256)) {
    fail('E_CLI_SCHEMA', '$.agent_inference identity');
  }
  validateJsonObject(value.answers, '$.agent_inference.answers');
  for (const [alias, entry] of Object.entries(value.answers)) {
    boundedString(alias, '$.agent_inference alias');
    exactKeys(entry, new Set(['value', 'rationale', 'evidence']), `$.agent_inference.${alias}`);
    boundedString(entry.rationale, `$.agent_inference.${alias}.rationale`);
    exactKeys(
      entry.evidence,
      new Set(['resume_sha256', 'job_description_sha256']),
      `$.agent_inference.${alias}.evidence`,
    );
    if (!SHA256.test(entry.evidence.resume_sha256)
      || !SHA256.test(entry.evidence.job_description_sha256)) {
      fail('E_CLI_SCHEMA', `$.agent_inference.${alias}.evidence`);
    }
  }
}

function validateResolution(value, location) {
  if (!isObject(value)) fail('E_CLI_SCHEMA', `${location}.resolution`);
  if (value.kind === 'canonical_upload') {
    exactKeys(value, new Set(['kind', 'field_id', 'alias']), `${location}.resolution`);
    boundedString(value.field_id, `${location}.resolution.field_id`, { identifier: true });
    boundedString(value.alias, `${location}.resolution.alias`);
    return;
  }
  exactKeys(value, new Set([
    'kind',
    'field_id',
    'alias',
    'sensitive',
    'remember',
    'approved_at',
    'deliberate_blank',
    'semantic_choice',
    'formatted_value',
    'user',
  ]), `${location}.resolution`);
  if (value.kind !== 'answer') fail('E_CLI_SCHEMA', `${location}.resolution.kind`);
  boundedString(value.field_id, `${location}.resolution.field_id`, { identifier: true });
  boundedString(value.alias, `${location}.resolution.alias`);
  if (value.sensitive !== null && typeof value.sensitive !== 'boolean') fail('E_CLI_SCHEMA');
  if (typeof value.remember !== 'boolean' || typeof value.deliberate_blank !== 'boolean') {
    fail('E_CLI_SCHEMA');
  }
  if (value.approved_at !== null) exactIsoTimestamp(value.approved_at, `${location}.approved_at`);
  if (value.remember !== (value.approved_at !== null)) fail('E_CLI_SCHEMA', `${location}.remember`);
  if (value.semantic_choice !== null && !SEMANTIC_CHOICES.has(value.semantic_choice)) {
    fail('E_CLI_SCHEMA', `${location}.semantic_choice`);
  }
  if (value.deliberate_blank !== (value.semantic_choice !== null)) fail('E_CLI_SCHEMA');
  if (value.formatted_value !== null) boundedString(value.formatted_value, `${location}.formatted_value`);
  if (value.deliberate_blank && value.formatted_value !== null) fail('E_CLI_SCHEMA');
  if (value.user !== null) {
    exactKeys(value.user, new Set(['answers']), `${location}.user`);
    validateJsonObject(value.user.answers, `${location}.user.answers`);
  }
}

function validateOption(value, location) {
  if (value === null) return;
  if (!isObject(value)) fail('E_CLI_SCHEMA', location);
  if (value.kind === 'observed_exact') {
    exactKeys(value, new Set(['kind', 'option_text', 'option_value']), location);
    boundedString(value.option_text, `${location}.option_text`);
    boundedString(value.option_value, `${location}.option_value`);
    return;
  }
  if (value.kind === 'greenhouse_education') {
    exactKeys(value, new Set(['kind', 'category']), location);
    if (!new Set(['schools', 'degrees', 'disciplines']).has(value.category)) {
      fail('E_CLI_SCHEMA', `${location}.category`);
    }
    return;
  }
  fail('E_CLI_SCHEMA', `${location}.kind`);
}

export function validateBrowserActionRequest(value) {
  exactKeys(value, new Set([
    'schema',
    'driver',
    'screenshot_sha256',
    'retry_of',
    'created_at',
    'ats',
    'resume',
    'agent_inference',
    'greenhouse_board_token',
    'items',
  ]), '$');
  if (value.schema !== BROWSER_ACTION_REQUEST_SCHEMA || !DRIVERS.has(value.driver)) fail('E_CLI_SCHEMA');
  if (value.screenshot_sha256 !== null && !SHA256.test(value.screenshot_sha256)) fail('E_CLI_SCHEMA');
  exactIsoTimestamp(value.created_at, '$.created_at');
  if (!ATS_VALUES.has(value.ats)) fail('E_CLI_SCHEMA', '$.ats');
  if (value.greenhouse_board_token !== null) {
    boundedString(value.greenhouse_board_token, '$.greenhouse_board_token', { identifier: true });
  }
  validateResumeCandidate(value.resume);
  validateInferenceCandidate(value.agent_inference);
  if (value.retry_of !== null) {
    validateJsonObject(value.retry_of, '$.retry_of');
    for (const [fieldId, sequence] of Object.entries(value.retry_of)) {
      boundedString(fieldId, '$.retry_of field', { identifier: true });
      if (sequence !== null && (!Number.isSafeInteger(sequence) || sequence < 0)) fail('E_CLI_SCHEMA');
    }
  }
  if (!Array.isArray(value.items) || value.items.length < 1 || value.items.length > 3) {
    fail('E_CLI_SCHEMA', '$.items');
  }
  const fieldIds = new Set();
  let needsGreenhouseCatalog = false;
  for (const [index, item] of value.items.entries()) {
    const location = `$.items[${index}]`;
    exactKeys(item, new Set(['resolution', 'decision', 'option']), location);
    validateResolution(item.resolution, location);
    const decision = validateApplicationDecision(item.decision);
    if (decision.fieldId !== item.resolution.field_id || fieldIds.has(decision.fieldId)) {
      fail('E_CLI_SCHEMA', `${location}.decision.fieldId`);
    }
    fieldIds.add(decision.fieldId);
    validateOption(item.option, `${location}.option`);
    const selection = decision.proposedAction === 'select_option';
    if (selection !== (item.option !== null)) fail('E_CLI_SCHEMA', `${location}.option`);
    if (item.resolution.kind === 'canonical_upload'
      && decision.proposedAction !== 'upload_file') {
      fail('E_CLI_SCHEMA', `${location}.resolution`);
    }
    if (item.resolution.kind !== 'canonical_upload'
      && decision.proposedAction === 'upload_file') {
      fail('E_CLI_SCHEMA', `${location}.resolution`);
    }
    if (item.option?.kind === 'greenhouse_education') needsGreenhouseCatalog = true;
  }
  if (value.items.length > 1
    && value.items.some((item) => item.decision.proposedAction !== 'fill_text')) {
    fail('E_CLI_SCHEMA', '$.items');
  }
  if (needsGreenhouseCatalog
    && (value.ats !== 'greenhouse' || value.greenhouse_board_token === null)) {
    fail('E_CLI_SCHEMA', '$.greenhouse_board_token');
  }
  return structuredClone(value);
}

function resolutionOptions(resolution) {
  const options = {
    field_id: resolution.field_id,
    alias: resolution.alias,
  };
  if (resolution.sensitive !== null) options.sensitive = resolution.sensitive;
  if (resolution.remember) {
    options.remember = true;
    options.approved_at = resolution.approved_at;
  }
  if (resolution.deliberate_blank) {
    options.deliberate_blank = true;
    options.semantic_choice = resolution.semantic_choice;
  }
  if (resolution.formatted_value !== null) options.formatted_value = resolution.formatted_value;
  if (resolution.user !== null) options.user = resolution.user;
  return options;
}

async function runExists(run) {
  let status;
  try {
    status = await fsp.lstat(run.run_artifact_dir);
  } catch (error) {
    if (error?.code === 'ENOENT') return false;
    throw error;
  }
  const uid = currentUid();
  if (!status.isDirectory() || status.isSymbolicLink()
    || (status.mode & 0o777) !== 0o700 || (uid !== null && status.uid !== uid)) {
    fail('E_CLI_RUN_SECURITY');
  }
  return (await readJsonFileSecure(path.join(run.run_artifact_dir, 'run.json'), {
    maxBytes: MAX_CLI_JSON_BYTES,
    ownerOnly: true,
    optional: true,
  })) !== null;
}

async function openSession(runPath, { initialize = false, resume, agentInference, now } = {}) {
  const { run } = await loadRunContractSnapshot(runPath, { local: false });
  const exists = await runExists(run);
  if (!initialize && !exists) fail('E_CLI_RUN_NOT_INITIALIZED');
  return startRun(runPath, {
    startedAt: now ?? new Date().toISOString(),
    resumeExisting: exists,
    ...(resume === undefined ? {} : { resume }),
    ...(agentInference === undefined ? {} : { agentInference }),
  });
}

async function createPlan(args, deps) {
  const request = validateBrowserActionRequest(await secureReadJson(args.request));
  const session = await openSession(args.run, {
    resume: request.resume ?? undefined,
    agentInference: request.agent_inference ?? undefined,
    now: deps.now,
  });
  const decisions = [];
  const answerAliases = {};
  const optionMatches = {};
  for (const item of request.items) {
    let normalizedDecision = validateApplicationDecision(item.decision);
    let resolved = item.resolution.kind === 'canonical_upload'
      ? await resolveCanonicalUpload(session, {
        field_id: item.resolution.field_id,
        alias: item.resolution.alias,
      })
      : await resolveField(session, resolutionOptions(item.resolution));
    if (resolved.missing) fail('E_CLI_ANSWER_MISSING');
    if (item.option?.kind === 'observed_exact') {
      optionMatches[normalizedDecision.fieldId] = {
        option_text: item.option.option_text,
        option_value: item.option.option_value,
      };
    } else if (item.option?.kind === 'greenhouse_education') {
      if (canonicalJson(normalizedDecision.proposedAnswer) !== canonicalJson(resolved.actionValue)
        || item.resolution.kind !== 'answer') {
        fail('E_CLI_CATALOG_DECISION');
      }
      const match = await resolveGreenhouseEducationOption({
        boardToken: request.greenhouse_board_token,
        category: item.option.category,
        value: resolved.actionValue,
        fetchImpl: deps.fetchImpl,
      });
      const control = session.observation.controls.find(
        (candidate) => candidate.ref === normalizedDecision.controlReference,
      );
      if (control === undefined) fail('E_CLI_CATALOG_DECISION');
      const actionValue = String(control.tag ?? '').toLowerCase() === 'select'
        ? match.option_value
        : match.option_text;
      optionMatches[normalizedDecision.fieldId] = {
        option_text: match.option_text,
        option_value: actionValue,
      };
      resolved = await resolveField(session, {
        ...resolutionOptions(item.resolution),
        formatted_value: actionValue,
      });
      normalizedDecision = validateApplicationDecision({
        ...normalizedDecision,
        proposedAnswer: actionValue,
        expectedRetainedState: actionValue,
      });
    }
    decisions.push(normalizedDecision);
    answerAliases[normalizedDecision.fieldId] = {
      alias: item.resolution.alias,
      value: resolved.actionValue,
    };
  }
  const plan = createBrowserActionPlan({
    observation: session.observation,
    ledger: session.ledger,
    decisions,
    answerAliases,
    optionMatches,
    resumeUpload: {
      path: session.runMetadata.resume_upload_path,
      sha256: session.runMetadata.resume_upload_sha256,
    },
    driver: request.driver,
    screenshotSha256: request.screenshot_sha256,
    retryOf: request.retry_of ?? undefined,
    createdAt: request.created_at,
    ats: request.ats,
  });
  const persisted = await recordActionPlan(session, plan);
  await secureWriteJson(args.output, persisted.plan);
  return Object.freeze({
    status: 'ok',
    command: 'plan',
    plan_id: persisted.plan.plan_id,
    action_count: persisted.plan.actions.length,
  });
}

export async function runCli(argv = process.argv.slice(2), deps = {}) {
  const { command, args } = parseCliArgs(argv);
  if (command === 'plan') return createPlan(args, deps);
  const session = await openSession(args.run, {
    initialize: command === 'accept-observation',
    now: deps.now,
  });
  if (command === 'accept-observation') {
    const result = await acceptObservation(session, await secureReadJson(args.observation));
    return Object.freeze({
      status: 'ok',
      command,
      observation_id: session.observation.observation_id,
      field_count: result.ledger.fields.length,
    });
  }
  if (command === 'pending-plan') {
    const plans = await getPendingActionPlans(session);
    await secureWriteJson(args.output, plans);
    return Object.freeze({
      status: 'ok',
      command,
      count: plans.length,
      plan_id: plans[0]?.plan_id ?? null,
    });
  }
  if (command === 'complete-action') {
    const plan = await secureReadJson(args.plan);
    const resultInput = await secureReadJson(args.result);
    if (resultInput?.schema !== ACTION_RESULT_SCHEMA) fail('E_CLI_SCHEMA');
    const result = await recordPlannedActionResult(
      session,
      plan,
      resultInput,
      await secureReadJson(args.observation),
    );
    return Object.freeze({
      status: result.retention.ok ? 'ok' : 'blocked',
      command,
      attempt_count: result.validation.attempts.length,
      retention_ok: result.retention.ok,
      retry_required: result.retention.retry_required,
    });
  }
  if (command === 'verify-retention') {
    const result = await verifyRetention(
      session,
      args.proofs === undefined ? undefined : await secureReadJson(args.proofs),
    );
    return Object.freeze({
      status: result.ok ? 'ok' : 'blocked',
      command,
      retention_ok: result.ok,
      retry_required: result.retry_required,
    });
  }
  if (command === 'prepare-submit') {
    const result = await prepareSubmission(session, { finalRef: args['final-ref'] });
    return Object.freeze({
      status: result.authorized ? 'ok' : 'blocked',
      command,
      authorized: result.authorized,
      final_ref: result.authorizedFinalRef,
    });
  }
  if (command === 'begin-submit') {
    const result = await beginFinalSubmit(session);
    await secureWriteJson(args.output, {
      attempt_id: result.attemptId,
      ref: result.ref,
      observation_id: result.observationId,
    });
    return Object.freeze({ status: 'ok', command, attempt_id: result.attemptId });
  }
  if (command === 'complete-submit') {
    const result = await completeFinalSubmit(session, {
      attemptId: args['attempt-id'],
      outcome: args.outcome,
      errorCode: args['error-code'] ?? null,
    });
    return Object.freeze({
      status: result.outcome === 'succeeded' ? 'ok' : 'blocked',
      command,
      attempt_id: result.attemptId,
      outcome: result.outcome,
    });
  }
  if (command === 'finalize') {
    const result = await finalizeRun(session, {
      screenshotPath: args.screenshot,
      finalUrl: args['final-url'],
    });
    return Object.freeze({ status: 'ok', command, finalized: result.finalized });
  }
  fail('E_CLI_COMMAND');
}

const invokedDirectly = process.argv[1] !== undefined
  && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
if (invokedDirectly) {
  runCli().then(
    (result) => process.stdout.write(`${JSON.stringify(result)}\n`),
    (error) => {
      process.stderr.write(`${JSON.stringify({
        name: typeof error?.name === 'string' ? error.name : 'Error',
        code: typeof error?.code === 'string' ? error.code : 'E_CLI_OPERATION',
      })}\n`);
      process.exitCode = 1;
    },
  );
}
