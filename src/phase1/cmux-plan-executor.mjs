import { execFile } from 'node:child_process';
import fsp from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';

import { validateBrowserActionPlan } from './action-plan.mjs';
import { readJsonFileSecure } from './contract.mjs';

const execFilePromise = promisify(execFile);

export class CmuxPlanExecutorError extends Error {
  constructor(
    code,
    message = code,
    {
      planId = null,
      failedActionId = null,
      completedActionCount = 0,
      completedStepCount = 0,
    } = {},
  ) {
    super(message);
    this.name = 'CmuxPlanExecutorError';
    this.code = code;
    this.planId = planId;
    this.failedActionId = failedActionId;
    this.completedActionCount = completedActionCount;
    this.completedStepCount = completedStepCount;
  }
}

export function validateSurfaceRef(surface) {
  if (typeof surface !== 'string' || surface.length === 0 || surface.length > 256) {
    throw new CmuxPlanExecutorError('E_INVALID_SURFACE_REF', 'surface reference must be a non-empty string up to 256 characters');
  }
  const trimmed = surface.trim();
  if (trimmed !== surface || !/^[a-zA-Z0-9_.:-]+$/.test(surface)) {
    throw new CmuxPlanExecutorError('E_INVALID_SURFACE_REF', 'surface reference contains invalid format or characters');
  }
  return surface;
}

export async function validateCmuxPath(cmuxPath) {
  if (typeof cmuxPath !== 'string' || cmuxPath.length === 0) {
    throw new CmuxPlanExecutorError('E_INVALID_CMUX_PATH', 'cmux executable path required');
  }
  if (!path.isAbsolute(cmuxPath)) {
    throw new CmuxPlanExecutorError('E_INVALID_CMUX_PATH', 'cmux executable path must be absolute');
  }
  const resolved = path.resolve(cmuxPath);
  try {
    const lstats = await fsp.lstat(resolved);
    if (!lstats.isFile() && !lstats.isSymbolicLink()) {
      throw new CmuxPlanExecutorError('E_INVALID_CMUX_PATH', 'cmux path must be a file');
    }
    const stats = await fsp.stat(resolved);
    if (!stats.isFile()) {
      throw new CmuxPlanExecutorError('E_INVALID_CMUX_PATH', 'cmux path must resolve to a regular file');
    }
    if ((stats.mode & 0o111) === 0) {
      throw new CmuxPlanExecutorError('E_INVALID_CMUX_PATH', 'cmux path is not executable');
    }
  } catch (error) {
    if (error instanceof CmuxPlanExecutorError) throw error;
    throw new CmuxPlanExecutorError('E_INVALID_CMUX_PATH', `invalid cmux executable path: ${error.message}`);
  }
  return resolved;
}

export function parseCliArgs(argv = []) {
  const args = Array.isArray(argv) ? argv.slice(2) : [];
  let cmux = null;
  let surface = null;
  let plan = null;

  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (arg === '--cmux') {
      if (i + 1 >= args.length || args[i + 1].startsWith('--')) {
        throw new CmuxPlanExecutorError('E_CLI_MISSING_ARG', 'missing value for --cmux');
      }
      cmux = args[i + 1];
      i += 1;
    } else if (arg.startsWith('--cmux=')) {
      cmux = arg.slice(7);
    } else if (arg === '--surface' || arg === '--surface-ref') {
      if (i + 1 >= args.length || args[i + 1].startsWith('--')) {
        throw new CmuxPlanExecutorError('E_CLI_MISSING_ARG', 'missing value for --surface');
      }
      surface = args[i + 1];
      i += 1;
    } else if (arg.startsWith('--surface=')) {
      surface = arg.slice(10);
    } else if (arg.startsWith('--surface-ref=')) {
      surface = arg.slice(14);
    } else if (arg === '--plan' || arg === '--plan-file') {
      if (i + 1 >= args.length || args[i + 1].startsWith('--')) {
        throw new CmuxPlanExecutorError('E_CLI_MISSING_ARG', 'missing value for --plan');
      }
      plan = args[i + 1];
      i += 1;
    } else if (arg.startsWith('--plan=')) {
      plan = arg.slice(7);
    } else if (arg.startsWith('--plan-file=')) {
      plan = arg.slice(12);
    } else {
      throw new CmuxPlanExecutorError('E_CLI_INVALID_ARGS', `unknown option: ${arg}`);
    }
  }

  if (!cmux) {
    throw new CmuxPlanExecutorError('E_CLI_MISSING_ARG', '--cmux argument required');
  }
  if (!surface) {
    throw new CmuxPlanExecutorError('E_CLI_MISSING_ARG', '--surface argument required');
  }
  if (!plan) {
    throw new CmuxPlanExecutorError('E_CLI_MISSING_ARG', '--plan argument required');
  }

  return {
    cmuxPath: cmux,
    surface,
    planPath: plan,
  };
}

export async function loadPlanSecure(planPath) {
  if (typeof planPath !== 'string' || planPath.trim().length === 0) {
    throw new CmuxPlanExecutorError('E_CLI_MISSING_ARG', 'plan path required');
  }
  const resolved = path.resolve(planPath);
  let plan;
  try {
    plan = await readJsonFileSecure(resolved, {
      maxBytes: 10 * 1024 * 1024,
      ownerOnly: true,
    });
  } catch (error) {
    if (
      error?.code === 'E_PATH_PERMISSIONS'
      || error?.code === 'E_PATH_NOT_FILE'
      || error?.code === 'E_PATH_ACCESS'
      || error?.code === 'E_PATH_SYMLINK'
    ) {
      throw new CmuxPlanExecutorError('E_UNSAFE_PLAN_PATH', 'plan file security validation failed');
    }
    if (
      error?.code === 'E_JSON_MALFORMED'
      || error?.code === 'E_JSON_UTF8'
      || error?.code === 'E_JSON_OVERSIZE'
    ) {
      throw new CmuxPlanExecutorError('E_MALFORMED_PLAN_JSON', 'plan file is not valid json');
    }
    throw new CmuxPlanExecutorError('E_UNSAFE_PLAN_PATH', 'failed to read plan file');
  }

  if (plan === null) {
    throw new CmuxPlanExecutorError('E_UNSAFE_PLAN_PATH', 'plan file not found');
  }
  return plan;
}

const ALLOWED_HELPERS = new Set(['fill', 'click', 'select']);
const DISALLOWED_ACTIONS = new Set(['upload_file', 'final_submit', 'submit', 'click_submit']);
const ALLOWED_SEMANTIC_ACTIONS = new Set(['fill_text', 'clear', 'select_option', 'toggle']);

export function validatePlanForCmux(plan) {
  if (!plan || typeof plan !== 'object' || Array.isArray(plan)) {
    throw new CmuxPlanExecutorError('E_INVALID_SCHEMA', 'plan must be an object');
  }
  if (plan.schema !== 'phase1-browser-action-plan-v2') {
    throw new CmuxPlanExecutorError('E_INVALID_SCHEMA', `unsupported plan schema: ${plan.schema}`);
  }

  const planId = typeof plan.plan_id === 'string' ? plan.plan_id : null;

  try {
    validateBrowserActionPlan(plan);
  } catch {
    throw new CmuxPlanExecutorError('E_INVALID_ACTION_PLAN', 'action plan failed structural validation', { planId });
  }

  if (!Array.isArray(plan.actions) || plan.actions.length === 0) {
    throw new CmuxPlanExecutorError('E_INVALID_ACTION_PLAN', 'plan actions must be a non-empty array', { planId });
  }

  for (let actionIdx = 0; actionIdx < plan.actions.length; actionIdx += 1) {
    const action = plan.actions[actionIdx];
    const actionId = typeof action.action_id === 'string' ? action.action_id : null;

    if (DISALLOWED_ACTIONS.has(action.semantic_action)) {
      throw new CmuxPlanExecutorError('E_UNSUPPORTED_ACTION', `disallowed action: ${action.semantic_action}`, { planId, failedActionId: actionId });
    }
    if (!ALLOWED_SEMANTIC_ACTIONS.has(action.semantic_action)) {
      throw new CmuxPlanExecutorError('E_UNSUPPORTED_ACTION', `unsupported semantic action: ${action.semantic_action}`, { planId, failedActionId: actionId });
    }

    if (!Array.isArray(action.steps) || action.steps.length === 0) {
      throw new CmuxPlanExecutorError('E_UNSUPPORTED_ACTION', 'action steps missing', { planId, failedActionId: actionId });
    }

    for (let stepIdx = 0; stepIdx < action.steps.length; stepIdx += 1) {
      const step = action.steps[stepIdx];

      if (!ALLOWED_HELPERS.has(step.helper)) {
        throw new CmuxPlanExecutorError('E_UNSUPPORTED_ACTION', `unsupported helper: ${step.helper}`, { planId, failedActionId: actionId });
      }

      if (step.wait_after !== null || step.reobserve_after !== null) {
        throw new CmuxPlanExecutorError('E_UNSUPPORTED_ACTION', 'wait_after and reobserve_after not supported', { planId, failedActionId: actionId });
      }

      if (step.file_path !== null) {
        throw new CmuxPlanExecutorError('E_UNSUPPORTED_ACTION', 'file_path upload not supported', { planId, failedActionId: actionId });
      }

      if (typeof step.selector !== 'string' || step.selector.length === 0 || step.selector.length > 4096 || step.selector.includes('\0')) {
        throw new CmuxPlanExecutorError('E_UNSAFE_SELECTOR', 'invalid or unsafe step selector', { planId, failedActionId: actionId });
      }

      if (step.helper === 'fill') {
        if (typeof step.value !== 'string') {
          throw new CmuxPlanExecutorError('E_UNSUPPORTED_ACTION', 'fill step requires string value', { planId, failedActionId: actionId });
        }
      } else if (step.helper === 'select') {
        const val = step.option_value ?? step.value;
        if (typeof val !== 'string') {
          throw new CmuxPlanExecutorError('E_UNSUPPORTED_ACTION', 'select step requires option_value or value string', { planId, failedActionId: actionId });
        }
      }
    }
  }

  return plan;
}

export async function executeCmuxPlan(options = {}) {
  const {
    cmuxPath,
    surface,
    planPath,
    plan: inputPlan = null,
    execFileFn = execFilePromise,
    timeoutMs = 15000,
    maxBufferBytes = 10 * 1024 * 1024,
  } = options;

  const resolvedCmuxPath = await validateCmuxPath(cmuxPath);
  const validatedSurface = validateSurfaceRef(surface);

  let plan = inputPlan;
  if (!plan) {
    plan = await loadPlanSecure(planPath);
  }

  validatePlanForCmux(plan);

  const planId = plan.plan_id;
  let completedActionCount = 0;
  let completedStepCount = 0;

  for (let actionIdx = 0; actionIdx < plan.actions.length; actionIdx += 1) {
    const action = plan.actions[actionIdx];
    const actionId = action.action_id;

    for (let stepIdx = 0; stepIdx < action.steps.length; stepIdx += 1) {
      const step = action.steps[stepIdx];
      let cmuxArgs;

      if (step.helper === 'fill') {
        const textVal = typeof step.value === 'string' ? step.value : '';
        cmuxArgs = ['browser', '--surface', validatedSurface, 'fill', '--selector', step.selector, '--text', textVal];
      } else if (step.helper === 'click') {
        cmuxArgs = ['browser', '--surface', validatedSurface, 'click', '--selector', step.selector];
      } else if (step.helper === 'select') {
        const selectVal = step.option_value ?? step.value ?? '';
        cmuxArgs = ['browser', '--surface', validatedSurface, 'select', '--selector', step.selector, '--value', selectVal];
      } else {
        return Object.freeze({
          status: 'failed',
          plan_id: planId,
          completed_action_count: completedActionCount,
          completed_step_count: completedStepCount,
          failed_action_id: actionId,
          error_code: 'E_UNSUPPORTED_ACTION',
        });
      }

      try {
        await execFileFn(resolvedCmuxPath, cmuxArgs, {
          timeout: timeoutMs,
          maxBuffer: maxBufferBytes,
          windowsHide: true,
          env: process.env,
        });
        completedStepCount += 1;
      } catch (error) {
        let errCode = 'E_CMUX_EXEC_FAILED';
        if (error?.code === 'ETIMEDOUT' || error?.killed) {
          errCode = 'E_CMUX_TIMEOUT';
        } else if (error?.code === 'ERR_CHILD_PROCESS_STDIO_MAXBUFFER') {
          errCode = 'E_CMUX_OVERSIZED_OUTPUT';
        }

        return Object.freeze({
          status: 'failed',
          plan_id: planId,
          completed_action_count: completedActionCount,
          completed_step_count: completedStepCount,
          failed_action_id: actionId,
          error_code: errCode,
        });
      }
    }

    completedActionCount += 1;
  }

  return Object.freeze({
    status: 'ok',
    plan_id: planId,
    completed_action_count: completedActionCount,
    completed_step_count: completedStepCount,
    failed_action_id: null,
    error_code: null,
  });
}

export async function runCmuxPlanExecutor(argv = process.argv) {
  let result;
  try {
    const parsed = parseCliArgs(argv);
    result = await executeCmuxPlan(parsed);
  } catch (error) {
    result = Object.freeze({
      status: 'failed',
      plan_id: error?.planId ?? null,
      completed_action_count: error?.completedActionCount ?? 0,
      completed_step_count: error?.completedStepCount ?? 0,
      failed_action_id: error?.failedActionId ?? null,
      error_code: error?.code ?? 'E_UNKNOWN_ERROR',
    });
  }

  console.log(JSON.stringify(result));
  if (result.status !== 'ok') {
    process.exitCode = 1;
  }
  return result;
}
