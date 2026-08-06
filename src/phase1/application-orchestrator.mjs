import {
  claimNextQueuedJob,
  recoverActiveRun,
} from './backlog-runner.mjs';
import { prepareNextQueuedResume } from './resume-preparation.mjs';

const OPTION_KEYS = new Set([
  'ownerId',
  'browserSessionId',
  'now',
  'leaseSeconds',
  'maxActiveJobs',
  'workspaceRoot',
  'applicantProfilePath',
  'sourceResumePath',
  'answerMemoryPath',
  'resumePreparation',
  'applicationJobId',
]);

function invalid() {
  throw new TypeError('E_APPLICATION_ORCHESTRATOR_OPTIONS');
}

function normalizeOptions(options) {
  if (!options || typeof options !== 'object' || Array.isArray(options)
    || Object.keys(options).some((key) => !OPTION_KEYS.has(key))) invalid();
  for (const key of [
    'ownerId', 'browserSessionId', 'now', 'workspaceRoot', 'applicantProfilePath',
    'sourceResumePath', 'answerMemoryPath', 'resumePreparation',
  ]) {
    if (!Object.hasOwn(options, key)) invalid();
  }
  if (options.maxActiveJobs !== undefined && options.maxActiveJobs !== 1) invalid();
  if (options.applicationJobId !== undefined
    && (!Number.isInteger(options.applicationJobId) || options.applicationJobId < 1)) invalid();
  return options;
}

/** Recover first; otherwise generate, validate, persist, and atomically claim one bound resume. */
export async function recoverPrepareOrClaimBacklogRun(database, options = {}) {
  const normalized = normalizeOptions(options);
  const recovered = await recoverActiveRun(database, {
    ownerId: normalized.ownerId,
    browserSessionId: normalized.browserSessionId,
    now: normalized.now,
    leaseSeconds: normalized.leaseSeconds,
  });
  if (recovered !== null) {
    return Object.freeze({ kind: 'recovered', run: recovered, preparation: null });
  }

  const preparation = await prepareNextQueuedResume(database, {
    ...normalized.resumePreparation,
    now: normalized.now,
    applicationJobId: normalized.applicationJobId,
  });
  if (preparation === null) {
    return Object.freeze({ kind: 'idle', run: null, preparation: null });
  }

  const claimed = await claimNextQueuedJob(database, {
    ownerId: normalized.ownerId,
    browserSessionId: normalized.browserSessionId,
    now: normalized.now,
    leaseSeconds: normalized.leaseSeconds,
    maxActiveJobs: 1,
    workspaceRoot: normalized.workspaceRoot,
    jobDescriptionPath: preparation.jobDescriptionPath,
    applicantProfilePath: normalized.applicantProfilePath,
    sourceResumePath: normalized.sourceResumePath,
    resumeUploadPath: preparation.pdfPath,
    answerMemoryPath: normalized.answerMemoryPath,
    resumeArtifactPath: preparation.pdfPath,
    resumeArtifactSha256: preparation.pdfSha256,
  });
  if (claimed === null) {
    return Object.freeze({ kind: 'idle', run: null, preparation });
  }
  if (claimed.jobId !== preparation.applicationJobId) {
    throw new Error('E_PREPARED_CLAIM_MISMATCH');
  }
  return Object.freeze({ kind: 'claimed', run: claimed, preparation });
}
