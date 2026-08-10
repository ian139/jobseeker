import { execFile } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';

import { canonicalJson, ensurePrivateDirectory } from './contract.mjs';
import { validateObservation } from './ledger.mjs';

const execFilePromise = promisify(execFile);

export class CmuxObserverCliError extends Error {
  constructor(code, message = code) {
    super(message);
    this.name = 'CmuxObserverCliError';
    this.code = code;
  }
}

function currentUid() {
  if (typeof process.geteuid === 'function') return process.geteuid();
  return typeof process.getuid === 'function' ? process.getuid() : null;
}

export function validateSurfaceRef(surface) {
  if (typeof surface !== 'string' || surface.length === 0 || surface.length > 256) {
    throw new CmuxObserverCliError('E_INVALID_SURFACE_REF', 'surface reference must be a non-empty string up to 256 characters');
  }
  const trimmed = surface.trim();
  if (trimmed !== surface || !/^[a-zA-Z0-9_.:-]+$/.test(surface)) {
    throw new CmuxObserverCliError('E_INVALID_SURFACE_REF', 'surface reference contains invalid format or characters');
  }
  return surface;
}

export async function validateCmuxPath(cmuxPath) {
  if (typeof cmuxPath !== 'string' || cmuxPath.length === 0) {
    throw new CmuxObserverCliError('E_INVALID_CMUX_PATH', 'cmux executable path required');
  }
  const resolved = path.resolve(cmuxPath);
  try {
    const lstats = await fsp.lstat(resolved);
    if (!lstats.isFile() && !lstats.isSymbolicLink()) {
      throw new CmuxObserverCliError('E_INVALID_CMUX_PATH', 'cmux path must be a file');
    }
    const stats = await fsp.stat(resolved);
    if (!stats.isFile()) {
      throw new CmuxObserverCliError('E_INVALID_CMUX_PATH', 'cmux path must resolve to a regular file');
    }
    if ((stats.mode & 0o111) === 0) {
      throw new CmuxObserverCliError('E_INVALID_CMUX_PATH', 'cmux path is not executable');
    }
  } catch (error) {
    if (error instanceof CmuxObserverCliError) throw error;
    throw new CmuxObserverCliError('E_INVALID_CMUX_PATH', `invalid cmux executable path: ${error.message}`);
  }
  return resolved;
}

export async function secureWriteObservationJson(filePath, value) {
  if (typeof filePath !== 'string' || filePath.length === 0 || filePath.includes('\0')) {
    throw new CmuxObserverCliError('E_UNSAFE_OUTPUT_PATH', 'invalid output path string');
  }
  const target = path.resolve(filePath);
  const parentPath = path.dirname(target);

  try {
    await ensurePrivateDirectory(parentPath, { create: true });
  } catch (error) {
    throw new CmuxObserverCliError('E_UNSAFE_OUTPUT_PATH', `parent directory check failed: ${error.message}`);
  }

  let parentStats;
  try {
    parentStats = await fsp.lstat(parentPath);
  } catch (error) {
    throw new CmuxObserverCliError('E_UNSAFE_OUTPUT_PATH', `cannot stat output parent directory: ${error.message}`);
  }

  const uid = currentUid();
  if (!parentStats.isDirectory() || parentStats.isSymbolicLink()
    || (parentStats.mode & 0o777) !== 0o700 || (uid !== null && parentStats.uid !== uid)) {
    throw new CmuxObserverCliError('E_UNSAFE_OUTPUT_PATH', 'output parent directory permissions or ownership unsafe');
  }

  try {
    await fsp.lstat(target);
    throw new CmuxObserverCliError('E_OUTPUT_EXISTS', 'output file already exists');
  } catch (error) {
    if (error instanceof CmuxObserverCliError) throw error;
    if (error?.code !== 'ENOENT') {
      throw new CmuxObserverCliError('E_UNSAFE_OUTPUT_PATH', `error checking output path existence: ${error.message}`);
    }
  }

  let bytes;
  try {
    bytes = Buffer.from(canonicalJson(value), 'utf8');
  } catch (error) {
    throw new CmuxObserverCliError('E_INVALID_OBSERVATION', `canonical json encoding failed: ${error.message}`);
  }

  const tmpFilename = `.tmp.${path.basename(target)}.${process.pid}.${crypto.randomBytes(8).toString('hex')}`;
  const tmpPath = path.join(parentPath, tmpFilename);

  let handle = null;
  try {
    handle = await fsp.open(
      tmpPath,
      fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL
        | (fs.constants.O_NOFOLLOW ?? 0),
      0o600,
    );
    await handle.writeFile(bytes);
    await handle.sync();
    await handle.close();
    handle = null;

    try {
      await fsp.link(tmpPath, target);
    } catch (error) {
      if (error?.code === 'EEXIST') {
        throw new CmuxObserverCliError('E_OUTPUT_EXISTS', 'output file already exists');
      }
      throw new CmuxObserverCliError('E_UNSAFE_OUTPUT_PATH', `failed to link output file: ${error.message}`);
    }

    let dirHandle = null;
    try {
      dirHandle = await fsp.open(parentPath, fs.constants.O_RDONLY);
      await dirHandle.sync();
    } catch (_) {
      // Best effort sync for parent directory
    } finally {
      if (dirHandle) await dirHandle.close();
    }
  } finally {
    if (handle) {
      try { await handle.close(); } catch (_) {}
    }
    try { await fsp.unlink(tmpPath); } catch (_) {}
  }
}

export function parseCliArgs(argv = []) {
  const args = argv.slice(2);
  let cmux = null;
  let surface = null;
  let previousObservationId = undefined;
  let initial = false;
  let output = null;

  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (arg === '--cmux' || arg === '--cmux-path') {
      if (i + 1 >= args.length || args[i + 1].startsWith('--')) {
        throw new CmuxObserverCliError('E_CLI_MISSING_ARG', 'missing value for --cmux');
      }
      cmux = args[i + 1];
      i += 1;
    } else if (arg.startsWith('--cmux=')) {
      cmux = arg.slice(7);
    } else if (arg.startsWith('--cmux-path=')) {
      cmux = arg.slice(12);
    } else if (arg === '--surface' || arg === '--surface-ref') {
      if (i + 1 >= args.length || args[i + 1].startsWith('--')) {
        throw new CmuxObserverCliError('E_CLI_MISSING_ARG', 'missing value for --surface');
      }
      surface = args[i + 1];
      i += 1;
    } else if (arg.startsWith('--surface=')) {
      surface = arg.slice(10);
    } else if (arg.startsWith('--surface-ref=')) {
      surface = arg.slice(14);
    } else if (arg === '--previous-observation-id' || arg === '--previous-id') {
      if (i + 1 >= args.length || args[i + 1].startsWith('--')) {
        throw new CmuxObserverCliError('E_CLI_MISSING_ARG', 'missing value for --previous-observation-id');
      }
      previousObservationId = args[i + 1];
      i += 1;
    } else if (arg.startsWith('--previous-observation-id=')) {
      previousObservationId = arg.slice(26);
    } else if (arg === '--initial') {
      initial = true;
    } else if (arg === '--output' || arg === '--output-path') {
      if (i + 1 >= args.length || args[i + 1].startsWith('--')) {
        throw new CmuxObserverCliError('E_CLI_MISSING_ARG', 'missing value for --output');
      }
      output = args[i + 1];
      i += 1;
    } else if (arg.startsWith('--output=')) {
      output = arg.slice(9);
    } else if (arg.startsWith('--output-path=')) {
      output = arg.slice(14);
    } else {
      throw new CmuxObserverCliError('E_CLI_UNKNOWN_FLAG', `unknown flag or argument: ${arg}`);
    }
  }

  if (!cmux) {
    throw new CmuxObserverCliError('E_CLI_MISSING_ARG', '--cmux argument required');
  }
  if (!surface) {
    throw new CmuxObserverCliError('E_CLI_MISSING_ARG', '--surface argument required');
  }
  if (!output) {
    throw new CmuxObserverCliError('E_CLI_MISSING_ARG', '--output argument required');
  }

  if (initial && previousObservationId !== undefined && previousObservationId !== 'null') {
    throw new CmuxObserverCliError('E_CLI_INVALID_ARGS', 'cannot specify both --initial and a non-null --previous-observation-id');
  }

  let finalPreviousId = null;
  if (initial) {
    finalPreviousId = null;
  } else if (previousObservationId !== undefined) {
    if (previousObservationId === 'null' || previousObservationId === '') {
      finalPreviousId = null;
    } else {
      finalPreviousId = previousObservationId;
    }
  } else {
    throw new CmuxObserverCliError('E_CLI_MISSING_ARG', 'either --previous-observation-id or --initial must be specified');
  }

  return {
    cmuxPath: cmux,
    surface,
    previousObservationId: finalPreviousId,
    outputPath: output,
  };
}

export async function observeCmuxSurface(options = {}) {
  const {
    cmuxPath,
    surface,
    previousObservationId = null,
    outputPath,
  } = options;

  const resolvedCmuxPath = await validateCmuxPath(cmuxPath);
  const validatedSurface = validateSurfaceRef(surface);

  const observerUrl = new URL('./observer.js', import.meta.url);
  let observerSource;
  try {
    observerSource = await fsp.readFile(observerUrl, 'utf8');
  } catch (error) {
    throw new CmuxObserverCliError('E_OBSERVER_READ_FAILED', `failed to read observer.js: ${error.message}`);
  }

  const globalPrefix = previousObservationId !== null
    ? `globalThis.__omp_phase1_previous_observation_id_v1 = ${JSON.stringify(previousObservationId)};`
    : `globalThis.__omp_phase1_previous_observation_id_v1 = null;`;
  const script = `${globalPrefix}\n${observerSource}`;

  const args = ['browser', '--surface', validatedSurface, 'eval', '--script', script];
  let stdout;
  try {
    const result = await execFilePromise(resolvedCmuxPath, args, {
      maxBuffer: 10 * 1024 * 1024,
      encoding: 'utf8',
      env: process.env,
    });
    stdout = result.stdout;
  } catch (error) {
    if (error.code === 'ERR_CHILD_PROCESS_STDIO_MAXBUFFER') {
      throw new CmuxObserverCliError('E_OVERSIZED_OUTPUT', 'cmux stdout size limit exceeded');
    }
    throw new CmuxObserverCliError('E_CMUX_EXEC_FAILED', `cmux execution failed: ${error.message}`);
  }

  if (typeof stdout !== 'string' || stdout.trim().length === 0) {
    throw new CmuxObserverCliError('E_MALFORMED_OBSERVATION_JSON', 'cmux stdout was empty');
  }

  if (Buffer.byteLength(stdout, 'utf8') > 10 * 1024 * 1024) {
    throw new CmuxObserverCliError('E_OVERSIZED_OUTPUT', 'cmux stdout exceeds maximum allowed size');
  }

  let observation;
  try {
    observation = JSON.parse(stdout);
  } catch (error) {
    throw new CmuxObserverCliError('E_MALFORMED_OBSERVATION_JSON', `cmux stdout is not valid JSON: ${error.message}`);
  }

  try {
    validateObservation(observation);
  } catch (error) {
    throw new CmuxObserverCliError('E_INVALID_OBSERVATION', `observation validation failed: ${error.message}`);
  }

  if (previousObservationId === null) {
    if (observation.previous_observation_id !== null) {
      throw new CmuxObserverCliError(
        'E_OBSERVATION_CHAIN_MISMATCH',
        `expected previous_observation_id null for initial mode, got ${observation.previous_observation_id}`,
      );
    }
  } else {
    if (observation.previous_observation_id !== previousObservationId) {
      throw new CmuxObserverCliError(
        'E_OBSERVATION_CHAIN_MISMATCH',
        `expected previous_observation_id ${previousObservationId}, got ${observation.previous_observation_id}`,
      );
    }
  }

  await secureWriteObservationJson(outputPath, observation);

  const controls = Array.isArray(observation.controls) ? observation.controls : [];
  const blockers = Array.isArray(observation.blockers) ? observation.blockers : [];
  const uniqueFields = new Set(controls.map((c) => c.stable_id).filter(Boolean));

  return Object.freeze({
    status: 'ok',
    observation_id: observation.observation_id,
    previous_observation_id: observation.previous_observation_id,
    field_count: uniqueFields.size,
    control_count: controls.length,
    blocker_count: blockers.length,
  });
}

export async function runCmuxObserverCli(argv = process.argv) {
  const parsed = parseCliArgs(argv);
  const metadata = await observeCmuxSurface(parsed);
  console.log(JSON.stringify(metadata));
  return metadata;
}
