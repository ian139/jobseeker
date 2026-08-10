import crypto from 'node:crypto';
import * as fs from 'node:fs';
import { promises as fsp } from 'node:fs';
import path from 'node:path';

export const RUN_SCHEMA = 'phase1-run-v1';
export const ANSWER_SCHEMA = 'phase1-answer-v2';
export const LEGACY_ANSWER_SCHEMA = 'phase1-answer-v1';
export const BROWSER_MODE = 'headed';
export const OBSERVER = 'playwright_dom_v1';
export const ACTION_DRIVER = 'omp_browser';
export const SUBMIT_POLICY = 'omp_agent';
export const RUN_FIXED_VALUES = Object.freeze({
    browser_mode: BROWSER_MODE,
    observer: OBSERVER,
    action_driver: ACTION_DRIVER,
    submit_policy: SUBMIT_POLICY,
});

export const CONTRACT_SCHEMAS = Object.freeze({
    run: RUN_SCHEMA,
    profile: 'phase1-profile-v1',
    answer: ANSWER_SCHEMA,
});


export const ANSWER_SOURCES = Object.freeze([
    'memory',
    'profile',
    'resume',
    'agent_inference',
    'user',
]);
export const ANSWER_SOURCE_PRECEDENCE = ANSWER_SOURCES;

export const MAX_JSON_BYTES = 256 * 1024;
export const MAX_PROFILE_BYTES = MAX_JSON_BYTES;
export const MAX_ANSWER_RECORD_BYTES = 64 * 1024;
export const MAX_ANSWER_MEMORY_BYTES = 8 * 1024 * 1024;
export const MAX_JOB_SNAPSHOT_BYTES = 4 * 1024 * 1024;
export const MAX_RESUME_BYTES = 16 * 1024 * 1024;
export const MAX_ALIAS_LENGTH = 256;
const MAX_INFERENCE_RATIONALE_LENGTH = 8192;
const SHA256_HEX = /^[0-9a-f]{64}$/u;
const INFERENCE_ENTRY_KEYS = new Set(['value', 'rationale', 'evidence']);
const INFERENCE_EVIDENCE_KEYS = new Set(['resume_sha256', 'job_description_sha256']);

export const RUN_KEYS = Object.freeze([
    'schema',
    'application_url',
    'job_description_path',
    'applicant_profile_path',
    'source_resume_path',
    'resume_upload_path',
    'answer_memory_path',
    'run_artifact_dir',
    'browser_mode',
    'observer',
    'action_driver',
    'submit_policy',
]);

export const REQUIRED_RUN_KEYS = Object.freeze([
    'schema',
    'application_url',
    'job_description_path',
    'resume_upload_path',
    'answer_memory_path',
    'run_artifact_dir',
    'browser_mode',
    'observer',
    'action_driver',
    'submit_policy',
]);

export const ANSWER_KEYS = Object.freeze([
    'schema',
    'alias',
    'value',
    'source',
    'approved_at',
    'approval_context',
    'approval_context_sha256',
]);

const LEGACY_ANSWER_KEYS = Object.freeze([
    'schema',
    'alias',
    'value',
    'source',
    'approved_at',
]);

const APPROVAL_CONTEXT_KEYS = Object.freeze([
    'run_contract_sha256',
    'observation_id',
    'field_id',
    'alias',
]);
const APPROVAL_CONTEXT_KEY_SET = new Set(APPROVAL_CONTEXT_KEYS);

const RUN_KEY_SET = new Set(RUN_KEYS);
const ANSWER_KEY_SET = new Set(ANSWER_KEYS);
const LEGACY_ANSWER_KEY_SET = new Set(LEGACY_ANSWER_KEYS);
const NOFOLLOW = fs.constants.O_NOFOLLOW ?? 0;
const READ_ONLY = fs.constants.O_RDONLY;

export class ValidationError extends Error {
    constructor(code, location = '') {
        const suffix = location ? `:${location}` : '';
        super(`${code}${suffix}`);
        this.name = 'ValidationError';
        this.code = code;
        this.location = location;
    }
}


function deepFreeze(value, seen = new Set()) {
    if (value === null || typeof value !== 'object' || seen.has(value)) {
        return value;
    }
    seen.add(value);
    for (const nested of Object.values(value)) {
        deepFreeze(nested, seen);
    }
    return Object.freeze(value);
}

function frozenClone(value) {
    return deepFreeze(structuredClone(value));
}
function fail(code, location = '') {
    throw new ValidationError(code, location);
}

export function isPlainObject(value) {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
        return false;
    }
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
}

function requirePlainObject(value, location) {
    if (!isPlainObject(value)) {
        fail('E_SCHEMA_OBJECT', location);
    }
    return value;
}

function rejectUnknownKeys(value, allowed, location) {
    const unknown = Object.keys(value)
        .filter((key) => !allowed.has(key))
        .sort();
    if (unknown.length > 0) {
        fail('E_SCHEMA_UNKNOWN_KEY', `${location}.${unknown[0]}`);
    }
}

function requireString(value, code, location, { nonEmpty = true, max = 8192 } = {}) {
    if (typeof value !== 'string' || (nonEmpty && value.length === 0) || value.length > max || value.includes('\u0000')) {
        fail(code, location);
    }
    return value;
}

function requirePresent(value, key, location) {
    if (!Object.hasOwn(value, key)) {
        fail('E_SCHEMA_REQUIRED', `${location}.${key}`);
    }
}

function assertJsonValue(value, location = '$', seen = new Set()) {
    if (value === null || typeof value === 'string' || typeof value === 'boolean') {
        if (typeof value === 'string' && value.length > 64 * 1024) {
            fail('E_JSON_VALUE_TOO_LARGE', location);
        }
        return;
    }
    if (typeof value === 'number') {
        if (!Number.isFinite(value)) {
            fail('E_JSON_VALUE', location);
        }
        return;
    }
    if (typeof value !== 'object') {
        fail('E_JSON_VALUE', location);
    }
    if (seen.has(value)) {
        fail('E_JSON_CYCLE', location);
    }
    seen.add(value);
    if (Array.isArray(value)) {
        if (value.length > 1024) {
            fail('E_JSON_VALUE_TOO_LARGE', location);
        }
        for (let index = 0; index < value.length; index += 1) {
            assertJsonValue(value[index], `${location}[${index}]`, seen);
        }
    } else {
        if (!isPlainObject(value) || Object.keys(value).length > 1024) {
            fail('E_JSON_VALUE', location);
        }
        for (const key of Object.keys(value)) {
            if (key.includes('\u0000')) {
                fail('E_JSON_VALUE', `${location}.${key}`);
            }
            assertJsonValue(value[key], `${location}.${key}`, seen);
        }
    }
    seen.delete(value);
}

function sortedJsonValue(value) {
    if (Array.isArray(value)) {
        return value.map(sortedJsonValue);
    }
    if (isPlainObject(value)) {
        return Object.fromEntries(
            Object.keys(value)
                .sort()
                .map((key) => [key, sortedJsonValue(value[key])]),
        );
    }
    return value;
}

export function canonicalJson(value) {
    assertJsonValue(value);
    const encoded = JSON.stringify(sortedJsonValue(value));
    if (typeof encoded !== 'string') {
        fail('E_JSON_VALUE');
    }
    return encoded;
}

function validateApprovalContext(input) {
    const context = requirePlainObject(input, 'approval_context');
    rejectUnknownKeys(context, APPROVAL_CONTEXT_KEY_SET, 'approval_context');
    for (const key of APPROVAL_CONTEXT_KEYS) {
        requirePresent(context, key, 'approval_context');
    }
    const ownKeys = Reflect.ownKeys(context);
    if (ownKeys.length !== APPROVAL_CONTEXT_KEYS.length
        || ownKeys.some((key) => typeof key !== 'string' || !APPROVAL_CONTEXT_KEY_SET.has(key))) {
        fail('E_ANSWER_APPROVAL_CONTEXT', 'approval_context');
    }
    for (const key of APPROVAL_CONTEXT_KEYS) {
        const descriptor = Object.getOwnPropertyDescriptor(context, key);
        if (descriptor === undefined
            || descriptor.enumerable !== true
            || !Object.hasOwn(descriptor, 'value')) {
            fail('E_ANSWER_APPROVAL_CONTEXT', `approval_context.${key}`);
        }
    }
    requireString(
        context.run_contract_sha256,
        'E_ANSWER_APPROVAL_CONTEXT',
        'approval_context.run_contract_sha256',
        { max: 64 },
    );
    if (!SHA256_HEX.test(context.run_contract_sha256)) {
        fail('E_ANSWER_APPROVAL_CONTEXT', 'approval_context.run_contract_sha256');
    }
    requireString(context.observation_id, 'E_ANSWER_APPROVAL_CONTEXT', 'approval_context.observation_id');
    requireString(context.field_id, 'E_ANSWER_APPROVAL_CONTEXT', 'approval_context.field_id');
    requireAlias(context.alias, 'approval_context.alias');
    return {
        run_contract_sha256: context.run_contract_sha256,
        observation_id: context.observation_id,
        field_id: context.field_id,
        alias: context.alias,
    };
}

export function approvalContextSha256(input) {
    const context = validateApprovalContext(input);
    return crypto.createHash('sha256').update(canonicalJson(context), 'utf8').digest('hex');
}

function pathString(value, location) {
    requireString(value, 'E_PATH', location, { max: 4096 });
    if (value === '.' || value === '..' || value.endsWith(`${path.sep}.`) || value.endsWith(`${path.sep}..`)) {
        fail('E_PATH', location);
    }
    return value;
}

function validateApplicationUrl(value) {
    requireString(value, 'E_RUN_URL', 'application_url', { max: 8192 });
    let parsed;
    try {
        parsed = new URL(value);
    } catch {
        fail('E_RUN_URL', 'application_url');
    }
    if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password) {
        fail('E_RUN_URL', 'application_url');
    }
    return value;
}

function normalizedPath(value) {
    return path.resolve(value);
}

function validateDistinctRunPaths(run) {
    const pathEntries = [
        ['job_description_path', run.job_description_path],
        ['applicant_profile_path', run.applicant_profile_path],
        ['source_resume_path', run.source_resume_path],
        ['resume_upload_path', run.resume_upload_path],
        ['answer_memory_path', run.answer_memory_path],
    ].filter(([, value]) => value !== undefined);
    const seen = new Map();
    for (const [key, value] of pathEntries) {
        const normalized = normalizedPath(value);
        const previous = seen.get(normalized);
        if (previous && !((previous === 'source_resume_path' && key === 'resume_upload_path') ||
            (previous === 'resume_upload_path' && key === 'source_resume_path'))) {
            fail('E_RUN_PATH_COLLISION', `${previous}:${key}`);
        }
        seen.set(normalized, key);
    }
    const artifactPath = normalizedPath(run.run_artifact_dir);
    for (const [key, value] of pathEntries) {
        if (normalizedPath(value) === artifactPath) {
            fail('E_RUN_PATH_COLLISION', `${key}:run_artifact_dir`);
        }
    }
}

export function validateRunContract(input) {
    const run = requirePlainObject(input, '$');
    rejectUnknownKeys(run, RUN_KEY_SET, '$');
    for (const key of REQUIRED_RUN_KEYS) {
        requirePresent(run, key, '$');
    }
    if (run.schema !== RUN_SCHEMA) {
        fail('E_RUN_SCHEMA', 'schema');
    }
    validateApplicationUrl(run.application_url);
    for (const key of ['job_description_path', 'resume_upload_path', 'answer_memory_path', 'run_artifact_dir']) {
        pathString(run[key], key);
    }
    for (const key of ['applicant_profile_path', 'source_resume_path']) {
        if (Object.hasOwn(run, key)) {
            pathString(run[key], key);
        }
    }
    if (!Object.hasOwn(run, 'applicant_profile_path') && !Object.hasOwn(run, 'source_resume_path')) {
        fail('E_RUN_EVIDENCE_REQUIRED', 'applicant_profile_path:source_resume_path');
    }
    if (!run.resume_upload_path.toLowerCase().endsWith('.pdf')) {
        fail('E_RUN_UPLOAD_NOT_PDF', 'resume_upload_path');
    }
    if (run.browser_mode !== BROWSER_MODE) {
        fail('E_RUN_BROWSER_MODE', 'browser_mode');
    }
    if (run.observer !== OBSERVER) {
        fail('E_RUN_OBSERVER', 'observer');
    }
    if (run.action_driver !== ACTION_DRIVER) {
        fail('E_RUN_ACTION_DRIVER', 'action_driver');
    }
    if (run.submit_policy !== SUBMIT_POLICY) {
        fail('E_RUN_SUBMIT_POLICY', 'submit_policy');
    }
    validateDistinctRunPaths(run);
    return structuredClone(run);
}

function normalizeRunPaths(run) {
    const normalized = structuredClone(run);
    for (const key of [
        'job_description_path',
        'applicant_profile_path',
        'source_resume_path',
        'resume_upload_path',
        'answer_memory_path',
        'run_artifact_dir',
    ]) {
        if (Object.hasOwn(normalized, key)) normalized[key] = path.resolve(normalized[key]);
    }
    validateDistinctRunPaths(normalized);
    return normalized;
}

async function lstatOrFail(filePath, location, { optional = false } = {}) {
    let status;
    try {
        status = await fsp.lstat(filePath);
    } catch (error) {
        if (optional && error?.code === 'ENOENT') {
            return null;
        }
        if (error?.code === 'ENOENT') {
            fail('E_PATH_MISSING', location);
        }
        fail('E_PATH_ACCESS', location);
    }
    if (status.isSymbolicLink()) {
        fail('E_PATH_SYMLINK', location);
    }
    return status;
}

async function parentDirectory(parentPath, location, { create = false } = {}) {
    if (create) {
        try {
            await fsp.mkdir(parentPath, { recursive: true, mode: 0o700 });
        } catch {
            fail('E_PATH_ACCESS', location);
        }
    }
    const status = await lstatOrFail(parentPath, location);
    if (!status?.isDirectory()) {
        fail('E_PATH_NOT_DIRECTORY', location);
    }
    if ((status.mode & 0o777) !== 0o700) {
        fail('E_PATH_PERMISSIONS', location);
    }
    return status;
}

export async function ensurePrivateDirectory(directoryPath, { create = false } = {}) {
    const value = pathString(directoryPath, 'path');
    await parentDirectory(value, 'path', { create });
    return value;
}

function currentProcessUid() {
    const getEffectiveUid = process.geteuid;
    if (typeof getEffectiveUid === 'function') {
        return getEffectiveUid();
    }
    const getUid = process.getuid;
    return typeof getUid === 'function' ? getUid() : undefined;
}

function isCurrentOwner(status) {
    const processUid = currentProcessUid();
    return processUid === undefined || status.uid === processUid;
}

async function openRegular(filePath, location, { flags = READ_ONLY, mode = undefined, optional = false } = {}) {
    const parent = await lstatOrFail(path.dirname(filePath), `${location}_parent`);
    if (!parent?.isDirectory()) {
        fail('E_PATH_NOT_DIRECTORY', `${location}_parent`);
    }

    const existing = await lstatOrFail(filePath, location, { optional });
    if (existing === null) {
        return null;
    }
    if (!existing.isFile()) {
        fail('E_PATH_NOT_FILE', location);
    }
    let handle;
    try {
        handle = await fsp.open(filePath, flags | NOFOLLOW, mode);
    } catch (error) {
        if (error?.code === 'ELOOP') {
            fail('E_PATH_SYMLINK', location);
        }
        fail('E_PATH_ACCESS', location);
    }
    let current;
    try {
        current = await handle.stat();
    } catch {
        await handle.close();
        fail('E_PATH_ACCESS', location);
    }
    if (!current.isFile()) {
        await handle.close();
        fail('E_PATH_NOT_FILE', location);
    }
    if (current.dev !== existing.dev || current.ino !== existing.ino) {
        await handle.close();
        fail('E_PATH_CHANGED', location);
    }
    return { handle, status: current };
}

async function readRegularBytes(filePath, location, {
    maxBytes,
    optional = false,
    ownerOnly = false,
} = {}) {
    const opened = await openRegular(filePath, location, { optional });
    if (opened === null) {
        return null;
    }
    const { handle, status } = opened;
    try {
        if (ownerOnly && ((status.mode & 0o077) !== 0 || !isCurrentOwner(status))) {
            fail('E_PATH_PERMISSIONS', location);
        }
        if (status.size > maxBytes) {
            fail('E_JSON_OVERSIZE', location);
        }
        const bytes = await handle.readFile();
        if (bytes.length > maxBytes) {
            fail('E_JSON_OVERSIZE', location);
        }
        return bytes;
    } finally {
        await handle.close();
    }
}

function decodeUtf8(bytes, location) {
    const text = bytes.toString('utf8');
    if (!Buffer.from(text, 'utf8').equals(bytes)) {
        fail('E_JSON_UTF8', location);
    }
    return text;
}

export async function readRegularFile(filePath, {
    maxBytes = MAX_JSON_BYTES,
    ownerOnly = false,
    optional = false,
} = {}) {
    const value = pathString(filePath, 'path');
    const bytes = await readRegularBytes(value, 'path', { maxBytes, ownerOnly, optional });
    return bytes === null ? null : decodeUtf8(bytes, 'path');
}

export async function readJsonFileSecure(filePath, {
    maxBytes = MAX_JSON_BYTES,
    ownerOnly = false,
    optional = false,
} = {}) {
    const text = await readRegularFile(filePath, { maxBytes, ownerOnly, optional });
    if (text === null) {
        return null;
    }
    try {
        return JSON.parse(text);
    } catch {
        fail('E_JSON_MALFORMED', 'path');
    }
}

export async function loadRunContractSnapshot(filePath, { local = true } = {}) {
    const resolvedPath = path.resolve(pathString(filePath, 'run_contract'));
    const bytes = await readRegularBytes(resolvedPath, 'run_contract', {
        maxBytes: MAX_JSON_BYTES,
        ownerOnly: true,
    });
    const text = decodeUtf8(bytes, 'run_contract');
    let parsed;
    try {
        parsed = JSON.parse(text);
    } catch {
        fail('E_JSON_MALFORMED', 'run_contract');
    }
    const run = normalizeRunPaths(validateRunContract(parsed));
    if (local) {
        await validateRunContractLocal(run);
    }
    return Object.freeze({
        run,
        identity: inputIdentity(resolvedPath, bytes),
    });
}

export async function loadRunContract(filePath, options = {}) {
    return (await loadRunContractSnapshot(filePath, options)).run;
}

async function checkRegularPath(filePath, location, { maxBytes, ownerOnly = true, optional = false } = {}) {
    const bytes = await readRegularBytes(filePath, location, { maxBytes, ownerOnly, optional });
    if (bytes === null) {
        return null;
    }
    return bytes;
}

function inputIdentity(filePath, bytes) {
    return Object.freeze({
        path: filePath,
        size: bytes.length,
        sha256: crypto.createHash('sha256').update(bytes).digest('hex'),
    });
}

async function checkPdfIdentity(filePath) {
    const bytes = await checkRegularPath(filePath, 'resume_upload_path', {
        maxBytes: MAX_RESUME_BYTES,
        ownerOnly: true,
    });
    if (bytes.length < 5 || bytes.subarray(0, 5).toString('ascii') !== '%PDF-') {
        fail('E_RUN_UPLOAD_NOT_PDF', 'resume_upload_path');
    }
    return inputIdentity(filePath, bytes);
}

async function checkPdf(filePath) {
    await checkPdfIdentity(filePath);
}

export async function validateRunContractLocal(input) {
    const run = validateRunContract(input);
    const jobBytes = await checkRegularPath(run.job_description_path, 'job_description_path', {
        maxBytes: MAX_JOB_SNAPSHOT_BYTES,
        ownerOnly: true,
    });
    if (jobBytes.length === 0) {
        fail('E_RUN_JOB_EMPTY', 'job_description_path');
    }
    if (Object.hasOwn(run, 'applicant_profile_path')) {
        const profileBytes = await checkRegularPath(run.applicant_profile_path, 'applicant_profile_path', {
            maxBytes: MAX_PROFILE_BYTES,
            ownerOnly: true,
        });
        if (profileBytes.length === 0) {
            fail('E_RUN_PROFILE_EMPTY', 'applicant_profile_path');
        }
        let profile;
        try {
            profile = JSON.parse(decodeUtf8(profileBytes, 'applicant_profile_path'));
        } catch {
            fail('E_JSON_MALFORMED', 'applicant_profile_path');
        }
        if (!isPlainObject(profile) || profile.schema !== 'phase1-profile-v1') {
            fail('E_RUN_PROFILE_SCHEMA', 'applicant_profile_path');
        }
        const { validateProfile } = await import('./profile.mjs');
        validateProfile(profile);
    }
    if (Object.hasOwn(run, 'source_resume_path')) {
        const resumeBytes = await checkRegularPath(run.source_resume_path, 'source_resume_path', {
            maxBytes: MAX_RESUME_BYTES,
            ownerOnly: true,
        });
        if (resumeBytes.length === 0) {
            fail('E_RUN_RESUME_EMPTY', 'source_resume_path');
        }
    }
    await checkPdf(run.resume_upload_path);
    await parentDirectory(path.dirname(run.run_artifact_dir), 'run_artifact_parent');
    await parentDirectory(run.run_artifact_dir, 'run_artifact_dir');
    const memoryStatus = await lstatOrFail(run.answer_memory_path, 'answer_memory_path', { optional: true });
    await parentDirectory(path.dirname(run.answer_memory_path), 'answer_memory_parent');
    if (memoryStatus !== null) {
        if (!memoryStatus.isFile()) {
            fail('E_PATH_NOT_FILE', 'answer_memory_path');
        }
        await loadAnswerMemory(run.answer_memory_path);
    }
    return run;
}

export async function loadRunInputs(input) {
    const run = normalizeRunPaths(validateRunContract(input));
    const jobBytes = await checkRegularPath(run.job_description_path, 'job_description_path', {
        maxBytes: MAX_JOB_SNAPSHOT_BYTES,
        ownerOnly: true,
    });
    if (jobBytes.length === 0) fail('E_RUN_JOB_EMPTY', 'job_description_path');
    const jobDescriptionIdentity = inputIdentity(run.job_description_path, jobBytes);

    let profile = null;
    if (Object.hasOwn(run, 'applicant_profile_path')) {
        const profileBytes = await checkRegularPath(run.applicant_profile_path, 'applicant_profile_path', {
            maxBytes: MAX_PROFILE_BYTES,
            ownerOnly: true,
        });
        if (profileBytes.length === 0) fail('E_RUN_PROFILE_EMPTY', 'applicant_profile_path');
        let parsed;
        try {
            parsed = JSON.parse(decodeUtf8(profileBytes, 'applicant_profile_path'));
        } catch {
            fail('E_JSON_MALFORMED', 'applicant_profile_path');
        }
        const { validateProfile } = await import('./profile.mjs');
        profile = validateProfile(parsed);
    }

    let sourceResumeIdentity = null;
    if (Object.hasOwn(run, 'source_resume_path')) {
        const resumeBytes = await checkRegularPath(run.source_resume_path, 'source_resume_path', {
            maxBytes: MAX_RESUME_BYTES,
            ownerOnly: true,
        });
        if (resumeBytes.length === 0) fail('E_RUN_RESUME_EMPTY', 'source_resume_path');
        sourceResumeIdentity = inputIdentity(run.source_resume_path, resumeBytes);
    }
    const resumeIdentity = await checkPdfIdentity(run.resume_upload_path);
    const memory = await loadAnswerMemory(run.answer_memory_path);
    return Object.freeze({
        profile,
        memory,
        resumeIdentity,
        sourceResumeIdentity,
        jobDescriptionIdentity,
    });
}

function requireAlias(value, location) {
    requireString(value, 'E_ANSWER_ALIAS', location, { max: MAX_ALIAS_LENGTH });
    if (value.trim().length === 0) {
        fail('E_ANSWER_ALIAS', location);
    }
    return value;
}

function requireIsoDate(value, location) {
    requireString(value, 'E_ANSWER_APPROVED_AT', location, { max: 64 });
    const parsed = Date.parse(value);
    const canonical = typeof value === 'string' && value.includes('.')
        ? value
        : `${value.slice(0, -1)}.000Z`;
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/.test(value) ||
        !Number.isFinite(parsed) ||
        new Date(parsed).toISOString() !== canonical) {
        fail('E_ANSWER_APPROVED_AT', location);
    }
    return value;
}

function validateStoredAnswerRecord(input, {
    schema,
    keys,
    keySet,
    requireApprovalContext,
} = {}) {
    const record = requirePlainObject(input, '$');
    rejectUnknownKeys(record, keySet, '$');
    for (const key of keys) {
        requirePresent(record, key, '$');
    }
    if (record.schema !== schema) {
        fail('E_ANSWER_SCHEMA', 'schema');
    }
    requireAlias(record.alias, 'alias');
    if (record.source !== 'user') {
        fail('E_ANSWER_SOURCE', 'source');
    }
    assertJsonValue(record.value, 'value');
    requireIsoDate(record.approved_at, 'approved_at');
    if (requireApprovalContext) {
        const context = validateApprovalContext(record.approval_context);
        if (context.alias !== record.alias) {
            fail('E_ANSWER_APPROVAL_CONTEXT_ALIAS', 'approval_context.alias');
        }
        requireString(
            record.approval_context_sha256,
            'E_ANSWER_APPROVAL_CONTEXT_SHA256',
            'approval_context_sha256',
            { max: 64 },
        );
        if (!SHA256_HEX.test(record.approval_context_sha256)
            || approvalContextSha256(context) !== record.approval_context_sha256) {
            fail('E_ANSWER_APPROVAL_CONTEXT_SHA256', 'approval_context_sha256');
        }
    }
    const encoded = canonicalJson(record);
    if (Buffer.byteLength(encoded, 'utf8') > MAX_ANSWER_RECORD_BYTES) {
        fail('E_JSON_OVERSIZE', 'answer');
    }
    return frozenClone(record);
}

export function validateAnswerRecord(input) {
    return validateStoredAnswerRecord(input, {
        schema: ANSWER_SCHEMA,
        keys: ANSWER_KEYS,
        keySet: ANSWER_KEY_SET,
        requireApprovalContext: true,
    });
}

function validateLegacyAnswerRecord(input) {
    return validateStoredAnswerRecord(input, {
        schema: LEGACY_ANSWER_SCHEMA,
        keys: LEGACY_ANSWER_KEYS,
        keySet: LEGACY_ANSWER_KEY_SET,
        requireApprovalContext: false,
    });
}

function quarantinedMetadata(record, line) {
    return frozenClone({
        line,
        schema: record.schema,
        alias: record.alias,
        source: record.source,
        approved_at: record.approved_at,
        reason: 'legacy_schema',
    });
}


function parseMemoryText(text) {
    if (text.length === 0) {
        return { active: [], quarantined: [] };
    }
    if (!text.endsWith('\n')) {
        fail('E_JSONL_MALFORMED', 'answer_memory.trailing_newline');
    }
    const lines = text.split('\n');
    lines.pop();
    const active = [];
    const quarantined = [];
    for (let index = 0; index < lines.length; index += 1) {
        const line = lines[index];
        const lineLocation = `answer_memory.line_${index + 1}`;
        if (line.length === 0 || line.endsWith('\r')) {
            fail('E_JSONL_MALFORMED', lineLocation);
        }
        const lineBytes = Buffer.byteLength(line, 'utf8');
        if (lineBytes > MAX_ANSWER_RECORD_BYTES) {
            fail('E_JSON_OVERSIZE', lineLocation);
        }
        let parsed;
        try {
            parsed = JSON.parse(line);
        } catch {
            fail('E_JSONL_MALFORMED', lineLocation);
        }
        const record = parsed?.schema === LEGACY_ANSWER_SCHEMA
            ? validateLegacyAnswerRecord(parsed)
            : validateAnswerRecord(parsed);
        if (canonicalJson(record) !== line) {
            fail('E_JSONL_NON_CANONICAL', lineLocation);
        }
        if (record.schema === LEGACY_ANSWER_SCHEMA) {
            quarantined.push(quarantinedMetadata(record, index + 1));
        } else {
            active.push(record);
        }
    }
    return { active, quarantined };
}

async function memoryParent(memoryPath, { create = false } = {}) {
    const parent = path.dirname(memoryPath);
    await parentDirectory(parent, 'answer_memory_parent', { create });
    return parent;
}

async function readAnswerMemoryText(memoryPath) {
    const value = pathString(memoryPath, 'answer_memory_path');
    await memoryParent(value);
    const status = await lstatOrFail(value, 'answer_memory_path', { optional: true });
    if (status !== null && (status.mode & 0o777) !== 0o600) {
        fail('E_PATH_PERMISSIONS', 'answer_memory_path');
    }
    const bytes = await readRegularBytes(value, 'answer_memory_path', {
        maxBytes: MAX_ANSWER_MEMORY_BYTES,
        ownerOnly: true,
        optional: true,
    });
    return bytes === null ? null : decodeUtf8(bytes, 'answer_memory_path');
}

export async function loadAnswerMemoryInventory(memoryPath) {
    const text = await readAnswerMemoryText(memoryPath);
    if (text === null) {
        return deepFreeze({ active: [], quarantined: [] });
    }
    const parsed = parseMemoryText(text);
    return deepFreeze({
        active: parsed.active,
        quarantined: parsed.quarantined,
    });
}

export async function loadAnswerMemory(memoryPath) {
    const inventory = await loadAnswerMemoryInventory(memoryPath);
    return inventory.active;
}

export const readAnswerMemory = loadAnswerMemory;

async function openMemoryForAppend(memoryPath, additionalBytes = 0) {
    await memoryParent(memoryPath, { create: true });
    const existing = await lstatOrFail(memoryPath, 'answer_memory_path', { optional: true });
    if (existing !== null) {
        if (!existing.isFile()) {
            fail('E_PATH_NOT_FILE', 'answer_memory_path');
        }
        if ((existing.mode & 0o777) !== 0o600) {
            fail('E_PATH_PERMISSIONS', 'answer_memory_path');
        }
        await loadAnswerMemory(memoryPath);
    }
    const flags = fs.constants.O_WRONLY | fs.constants.O_APPEND | fs.constants.O_CREAT | NOFOLLOW;
    let handle;
    try {
        handle = await fsp.open(memoryPath, flags, 0o600);
    } catch (error) {
        if (error?.code === 'ELOOP') {
            fail('E_PATH_SYMLINK', 'answer_memory_path');
        }
        fail('E_PATH_ACCESS', 'answer_memory_path');
    }
    const status = await handle.stat();
    if (!status.isFile()) {
        await handle.close();
        fail('E_PATH_NOT_FILE', 'answer_memory_path');
    }
    if ((status.mode & 0o777) !== 0o600) {
        await handle.close();
        fail('E_PATH_PERMISSIONS', 'answer_memory_path');
    }
    if (status.size > MAX_ANSWER_MEMORY_BYTES || status.size + additionalBytes > MAX_ANSWER_MEMORY_BYTES) {
        await handle.close();
        fail('E_JSON_OVERSIZE', 'answer_memory_path');
    }
    return handle;
}

export async function appendAnswerRecord(memoryPath, input) {
    const value = pathString(memoryPath, 'answer_memory_path');
    const record = validateAnswerRecord(input);
    const line = `${canonicalJson(record)}\n`;
    const lineBytes = Buffer.byteLength(line, 'utf8');
    if (lineBytes > MAX_ANSWER_RECORD_BYTES + 1) {
        fail('E_JSON_OVERSIZE', 'answer');
    }
    const handle = await openMemoryForAppend(value, lineBytes);
    try {
        await handle.write(line);
    } finally {
        await handle.close();
    }
    return record;
}

export const appendAnswer = appendAnswerRecord;

export function createAnswerRecord(input) {
    if (arguments.length !== 1) {
        throw new TypeError('createAnswerRecord requires one object argument');
    }
    const record = requirePlainObject(input, 'answer');
    rejectUnknownKeys(
        record,
        new Set(['alias', 'value', 'approved_at', 'approval_context', 'approval_context_sha256']),
        'answer',
    );
    for (const key of ['alias', 'value', 'approval_context', 'approval_context_sha256']) {
        requirePresent(record, key, 'answer');
    }
    return validateAnswerRecord({
        schema: ANSWER_SCHEMA,
        alias: record.alias,
        value: record.value,
        source: 'user',
        approved_at: Object.hasOwn(record, 'approved_at')
            ? record.approved_at
            : new Date().toISOString(),
        approval_context: record.approval_context,
        approval_context_sha256: record.approval_context_sha256,
    });
}

function candidateMap(candidate) {
    if (!isPlainObject(candidate)) {
        return null;
    }
    if (isPlainObject(candidate.answers)) {
        return candidate.answers;
    }
    return candidate;
}

function candidateValue(candidate, alias) {
    const map = candidateMap(candidate);
    if (map !== null && Object.hasOwn(map, alias)) {
        return { found: true, value: map[alias] };
    }
    return { found: false };
}

const PROFILE_CANONICAL_PATHS = Object.freeze({
    'profile.address.country': Object.freeze(['address', 'country']),
    'profile.address.city': Object.freeze(['address', 'city']),
    'profile.address.formatted': Object.freeze(['address', 'formatted']),
    'profile.address.postal_code': Object.freeze(['address', 'postal_code']),
    'profile.address.region': Object.freeze(['address', 'region']),
    'profile.location_preferences.onsite': Object.freeze(['location_preferences', 'onsite']),
    'profile.relocation.willing': Object.freeze(['relocation', 'willing']),
    'profile.address.street': Object.freeze(['address', 'street']),
    'profile.address.street2': Object.freeze(['address', 'street2']),
    'profile.location_preferences.current_location': Object.freeze(['location_preferences', 'current_location']),
    'profile.compensation.target': Object.freeze(['compensation', 'target']),
    'Full Name': Object.freeze(['contact', 'name']),
    Name: Object.freeze(['contact', 'name']),
    'Preferred First Name': Object.freeze(['contact', 'preferred_name']),
    'First Name': Object.freeze(['contact', 'first_name']),
    'Last Name': Object.freeze(['contact', 'last_name']),
    Email: Object.freeze(['contact', 'email']),
    Phone: Object.freeze(['contact', 'phone']),
});
const PROFILE_CANONICAL_LINK_KINDS = Object.freeze({
    'profile.links.github': 'github',
    'profile.links.linkedin': 'linkedin',
    'profile.links.portfolio': 'portfolio',
});
const PROFILE_CANONICAL_EMPLOYMENT_KEYS = Object.freeze({
    'profile.employment.current.employer': 'employer',
    'profile.employment.current.title': 'title',
});

function ownPathValue(candidate, segments) {
    let value = candidate;
    for (const segment of segments) {
        if (!isPlainObject(value) || !Object.hasOwn(value, segment)) {
            return { found: false };
        }
        value = value[segment];
    }
    return { found: true, value };
}
function canonicalRelocationValue(profile, alias) {
    if (alias !== 'profile.relocation.willing') return { found: false };
    const relocation = ownPathValue(profile, ['relocation', 'willing']);
    const preference = ownPathValue(profile, ['location_preferences', 'willing_to_relocate']);
    if (relocation.found && preference.found && relocation.value !== preference.value) {
        return { found: false };
    }
    return relocation.found ? relocation : preference;
}

function canonicalSponsorshipValue(profile, alias) {
    if (alias !== 'profile.sponsorship.not_needed') return { found: false };
    const needed = ownPathValue(profile, ['sponsorship', 'needed']);
    return needed.found && typeof needed.value === 'boolean'
        ? { found: true, value: !needed.value }
        : { found: false };
}

function canonicalCityMatchValue(profile, alias) {
    const positivePrefix = 'profile.address.city.is:';
    const negativePrefix = 'profile.address.city.is_not:';
    const positive = alias.startsWith(positivePrefix);
    const prefix = positive ? positivePrefix : negativePrefix;
    if (!positive && !alias.startsWith(negativePrefix)) return { found: false };
    const expected = normalizedQuestion(alias.slice(prefix.length));
    const city = ownPathValue(profile, ['address', 'city']);
    if (!city.found || typeof city.value !== 'string' || expected.length === 0) {
        return { found: false };
    }
    const matches = normalizedQuestion(city.value) === expected;
    return { found: true, value: positive ? matches : !matches };
}


function normalizedQuestion(value) {
    return value.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
}
function canonicalLinkValue(profile, alias) {
    const kind = PROFILE_CANONICAL_LINK_KINDS[alias];
    if (kind === undefined || !isPlainObject(profile) || !Array.isArray(profile.links)) {
        return { found: false };
    }
    let match;
    for (const link of profile.links) {
        if (!isPlainObject(link) || typeof link.url !== 'string') continue;
        const matched = [link.kind, link.label].some((value) =>
            typeof value === 'string' && normalizedQuestion(value) === kind);
        if (!matched) continue;
        if (match !== undefined) return { found: false };
        match = link.url;
    }
    return match === undefined ? { found: false } : { found: true, value: match };
}
function canonicalEmploymentValue(profile, alias) {
    const key = PROFILE_CANONICAL_EMPLOYMENT_KEYS[alias];
    if (key === undefined || !isPlainObject(profile) || !Array.isArray(profile.employment)) {
        return { found: false };
    }
    let match;
    for (const employment of profile.employment) {
        if (!isPlainObject(employment)
            || employment.current !== true
            || !Object.hasOwn(employment, key)) continue;
        if (match !== undefined) return { found: false };
        match = employment[key];
    }
    return match === undefined ? { found: false } : { found: true, value: match };
}

function canonicalEducationValue(profile, alias) {
    if (alias !== 'profile.education.college.institution'
        || !isPlainObject(profile)
        || !Array.isArray(profile.education)) {
        return { found: false };
    }
    const candidates = profile.education.filter((entry) =>
        isPlainObject(entry)
        && entry.level === 'college'
        && typeof entry.institution === 'string'
        && entry.institution.length > 0);
    return candidates.length === 1
        ? { found: true, value: candidates[0].institution }
        : { found: false };
}



function authorizationCountryIsSupported(question, countries) {
    const match = question.match(/\b(?:work|employment)\s+(?:in|for)\s+(?:the\s+)?(.+)$/);
    if (match === null) return true;
    const requested = match[1]
        .replace(/\b(?:now|currently|without sponsorship|now or in the future)\b.*$/, '')
        .trim();
    if (requested.length === 0 || !countries.found || !Array.isArray(countries.value)) return false;
    const configured = new Set(countries.value.map(normalizedQuestion));
    if (new Set(['us', 'u s', 'usa', 'u s a', 'united states']).has(requested)) {
        return ['us', 'u s', 'usa', 'u s a', 'united states'].some((value) => configured.has(value));
    }
    return configured.has(requested);
}


function semanticEducationValue(profile, alias) {
    if (!isPlainObject(profile) || !Array.isArray(profile.education)) {
        return { found: false };
    }
    const question = normalizedQuestion(alias);
    const asksGpa = /\b(?:gpa|grade point average)\b/.test(question);
    const asksHighSchool = /\b(?:high school|secondary school)\b/.test(question);
    const asksCollege = /\b(?:college|university|undergraduate)\b/.test(question);
    if (asksGpa) {
        if (asksHighSchool && asksCollege) {
            return { found: false };
        }
        const candidates = profile.education.filter((entry) =>
            isPlainObject(entry)
            && Object.hasOwn(entry, 'gpa')
            && (!asksHighSchool || entry.level === 'high_school')
            && (!asksCollege || entry.level === 'college'));
        return candidates.length === 1
            ? { found: true, value: candidates[0].gpa }
            : { found: false };
    }
    const asksGraduation = /\b(?:graduation|graduated|expected graduation|completion|end date)\b/.test(question);
    if (asksGraduation) {
        if (asksHighSchool && asksCollege) {
            return { found: false };
        }
        const levelFilter = asksHighSchool ? 'high_school' : (asksCollege ? 'college' : 'college');
        let candidates = profile.education.filter((entry) =>
            isPlainObject(entry)
            && Object.hasOwn(entry, 'end_date')
            && entry.level === levelFilter);
        if (candidates.length !== 1) {
            candidates = profile.education.filter((entry) =>
                isPlainObject(entry)
                && Object.hasOwn(entry, 'end_date')
                && (!asksHighSchool || entry.level === 'high_school')
                && (!asksCollege || entry.level === 'college'));
        }
        return candidates.length === 1
            ? { found: true, value: candidates[0].end_date }
            : { found: false };
    }
    const asksInstitution =
        /^(?:high school|secondary school)$/.test(question)
        || /\b(?:high school|secondary school)\b.*\b(?:institution|name|attend|attended)\b/.test(question)
        || /\b(?:institution|name)\b.*\b(?:high school|secondary school)\b/.test(question);
    if (!asksInstitution) {
        return { found: false };
    }
    const candidates = profile.education.filter((entry) =>
        isPlainObject(entry)
        && entry.level === 'high_school'
        && Object.hasOwn(entry, 'institution'));
    return candidates.length === 1
        ? { found: true, value: candidates[0].institution }
        : { found: false };
}

function semanticLinkValue(profile, alias) {
    if (!isPlainObject(profile) || !Array.isArray(profile.links)) {
        return { found: false };
    }
    const question = normalizedQuestion(alias);
    let matches;
    if (/\blinkedin\b/.test(question)) {
        matches = profile.links.filter((link) => {
            if (!isPlainObject(link) || typeof link.url !== 'string') return false;
            try {
                return /(?:^|\.)linkedin\.com$/i.test(new URL(link.url).hostname);
            } catch {
                return false;
            }
        });
    } else if (/\b(?:portfolio|website|personal site)\b/.test(question)) {
        matches = profile.links.filter((link) => {
            if (!isPlainObject(link) || typeof link.url !== 'string') return false;
            const marker = normalizedQuestion(`${link.kind ?? ''} ${link.label ?? ''}`);
            return /\b(?:portfolio|website|personal site)\b/.test(marker);
        });
    } else {
        return { found: false };
    }
    return matches.length === 1
        ? { found: true, value: matches[0].url }
        : { found: false };
}

function semanticStatusValue(profile, alias) {
    if (!isPlainObject(profile)) {
        return { found: false };
    }
    const question = normalizedQuestion(alias);
    const status = ownPathValue(profile, ['work_authorization', 'status']);
    const authorized = ownPathValue(profile, ['work_authorization', 'authorized']);
    const countries = ownPathValue(profile, ['work_authorization', 'countries']);
    const sponsorshipNeeded = ownPathValue(profile, ['sponsorship', 'needed']);

    if (/\b(?:sponsor|sponsorship)\b/.test(question)) {
        if (/\bwithout\b.*\b(?:sponsor|sponsorship)\b/.test(question) ||
            /\b(?:sponsor|sponsorship)\b.*\bwithout\b/.test(question)) {
            return sponsorshipNeeded.found
                ? { found: true, value: !sponsorshipNeeded.value }
                : { found: false };
        }
        return sponsorshipNeeded;
    }
    if (/\b(?:citizenship|citizen|immigration status|visa status)\b/.test(question)) {
        const asksUsStatus =
            /\b(?:u s a?|usa|united states|american)\b/.test(question) ||
            /\bus (?:citizen|citizenship)\b/.test(question);
        const asksGenericCitizenship = /^(?:are|is) you (?:a )?citizen$/.test(question);
        if (/\b(?:are|is|do)\b.*\bcitizen\b/.test(question)) {
            if (!asksUsStatus && !asksGenericCitizenship) return { found: false };
            if (!status.found) return { found: false };
            const normalizedStatus = normalizedQuestion(status.value);
            const isCitizen = /\bcitizen\b/.test(normalizedStatus);
            const isUsCitizen = /\b(?:us|u s|united states|american)\b.*\bcitizen\b/.test(normalizedStatus);
            return { found: true, value: asksUsStatus ? isUsCitizen : isCitizen };
        }
        if (/\bcitizenship\b.*\b(?:of|in)\b/.test(question) && !asksUsStatus) {
            return { found: false };
        }
        return status;
    }
    if (/\b(?:which|what)\s+countries?\b.*\b(?:authorized|eligible|permitted)\b/.test(question) ||
        /\bcountries?\b.*\b(?:work authorization|authorized to work)\b/.test(question)) {
        return countries;
    }
    if (/\b(?:work|employment) authorization status\b/.test(question)) {
        return status;
    }
    const asksAuthorization =
        /\b(?:authorized|authorization|eligible|eligibility|permitted|permission|right|legally able)\b.*\b(?:work|employment)\b/.test(question) ||
        /\b(?:work|employment)\b.*\b(?:authorized|authorization|eligible|permitted|permission|right)\b/.test(question);
    if (asksAuthorization) {
        if (!authorizationCountryIsSupported(question, countries)) {
            return { found: false };
        }
        return authorized;
    }
    return { found: false };
}



function profileValue(profile, alias) {
    const segments = Object.hasOwn(PROFILE_CANONICAL_PATHS, alias)
        ? PROFILE_CANONICAL_PATHS[alias]
        : undefined;
    if (segments !== undefined) {
        const canonical = ownPathValue(profile, segments);
        if (canonical.found) {
            return canonical;
        }
    }
    const relocation = canonicalRelocationValue(profile, alias);
    if (relocation.found) {
        return relocation;
    }
    const sponsorship = canonicalSponsorshipValue(profile, alias);
    if (sponsorship.found) {
        return sponsorship;
    }
    const link = canonicalLinkValue(profile, alias);
    if (link.found) {
        return link;
    }
    const cityMatch = canonicalCityMatchValue(profile, alias);
    if (cityMatch.found) {
        return cityMatch;
    }
    const employment = canonicalEmploymentValue(profile, alias);
    if (employment.found) {
        return employment;
    }
    const canonicalEducation = canonicalEducationValue(profile, alias);
    if (canonicalEducation.found) {
        return canonicalEducation;
    }
    const exact = candidateValue(profile, alias);
    if (exact.found) {
        return exact;
    }
    const semantic = semanticLinkValue(profile, alias);
    if (semantic.found) {
        return semantic;
    }
    const education = semanticEducationValue(profile, alias);
    return education.found ? education : semanticStatusValue(profile, alias);
}

function memoryValue(memory, alias) {
    if (Array.isArray(memory)) {
        for (let index = memory.length - 1; index >= 0; index -= 1) {
            const candidate = memory[index];
            if (isPlainObject(candidate) && candidate.schema === LEGACY_ANSWER_SCHEMA) {
                validateLegacyAnswerRecord(candidate);
                continue;
            }
            const record = validateAnswerRecord(candidate);
            if (record.alias === alias) {
                return { found: true, value: record.value };
            }
        }
        return { found: false };
    }
    if (isPlainObject(memory) && memory.schema === LEGACY_ANSWER_SCHEMA) {
        validateLegacyAnswerRecord(memory);
        return { found: false };
    }
    if (isPlainObject(memory) && memory.schema === ANSWER_SCHEMA) {
        const record = validateAnswerRecord(memory);
        return record.alias === alias
            ? { found: true, value: record.value }
            : { found: false };
    }
    fail('E_ANSWER_MEMORY_SCHEMA', 'memory');
}

function exactPlainDataObject(value, keys, location) {
    const object = requirePlainObject(value, location);
    const ownKeys = Reflect.ownKeys(object);
    if (ownKeys.length !== keys.size
        || ownKeys.some((key) => typeof key !== 'string' || !keys.has(key))) {
        fail('E_ANSWER_INFERENCE_SCHEMA', location);
    }
    for (const key of keys) {
        const descriptor = Object.getOwnPropertyDescriptor(object, key);
        if (descriptor === undefined
            || descriptor.enumerable !== true
            || !Object.hasOwn(descriptor, 'value')) {
            fail('E_ANSWER_INFERENCE_SCHEMA', `${location}.${key}`);
        }
    }
    return object;
}

function inferenceValue(candidate, alias) {
    const selected = candidateValue(candidate, alias);
    if (!selected.found) {
        return selected;
    }
    const location = `agent_inference.${alias}`;
    const entry = exactPlainDataObject(selected.value, INFERENCE_ENTRY_KEYS, location);
    assertJsonValue(entry.value, `${location}.value`);
    requireString(entry.rationale, 'E_ANSWER_INFERENCE_RATIONALE', `${location}.rationale`, {
        max: MAX_INFERENCE_RATIONALE_LENGTH,
    });
    if (entry.rationale.trim().length === 0) {
        fail('E_ANSWER_INFERENCE_RATIONALE', `${location}.rationale`);
    }
    const evidence = exactPlainDataObject(
        entry.evidence,
        INFERENCE_EVIDENCE_KEYS,
        `${location}.evidence`,
    );
    for (const key of INFERENCE_EVIDENCE_KEYS) {
        requireString(evidence[key], 'E_ANSWER_INFERENCE_EVIDENCE', `${location}.evidence.${key}`, {
            max: 64,
        });
        if (!SHA256_HEX.test(evidence[key])) {
            fail('E_ANSWER_INFERENCE_EVIDENCE', `${location}.evidence.${key}`);
        }
    }
    return {
        found: true,
        value: structuredClone(entry.value),
        inference_rationale_digest: crypto.createHash('sha256').update(entry.rationale, 'utf8').digest('hex'),
        inference_evidence_digests: structuredClone(evidence),
    };
}

export function resolveAnswer({
    alias,
    profileAlias = alias,
    memory = [],
    profile = undefined,
    resume = undefined,
    agentInference = undefined,
    agent_inference = undefined,
    user = undefined,
} = {}) {
    requireAlias(alias, 'alias');
    requireAlias(profileAlias, 'profileAlias');
    const candidates = [
        { source: 'memory', candidate: memoryValue(memory, alias) },
        { source: 'profile', candidate: profileValue(profile, profileAlias) },
        { source: 'resume', candidate: candidateValue(resume, alias) },
    ];
    for (const { source, candidate } of candidates) {
        if (candidate.found) {
            return { alias, source, value: structuredClone(candidate.value), missing: false };
        }
    }

    const inference = inferenceValue(agentInference ?? agent_inference, alias);
    if (inference.found) {
        return {
            alias,
            source: 'agent_inference',
            value: inference.value,
            missing: false,
            inference_rationale_digest: inference.inference_rationale_digest,
            inference_evidence_digests: inference.inference_evidence_digests,
        };
    }

    const userCandidate = candidateValue(user, alias);
    if (userCandidate.found) {
        return { alias, source: 'user', value: structuredClone(userCandidate.value), missing: false };
    }
    return { alias, source: 'user', value: undefined, missing: true };
}


export function resolveAnswerSource(options = {}) {
    return resolveAnswer(options).source;
}
