import {
  BrowserAdapter,
  BrowserAdapterError,
  BrowserIdentityError,
  BrowserTransportError,
  createBrowserAdapter,
} from './browser-adapter.mjs';
import {
  CmuxTuiBrowserRuntimeError,
  createCmuxTuiBrowserBinding,
  createCmuxTuiBrowserTransport,
} from './cmux-tui-browser-runtime.mjs';

export const OMP_BROWSER_ADAPTER_SCHEMA = 'phase1-omp-browser-adapter-v1';

/**
 * OMP `browser` tool actions are bound to one target in one CMUX-TUI
 * session. The injected transport carries the binding on every request;
 * closing a target never exposes a runtime/session close operation.
 */
export class OmpBrowserAdapter extends BrowserAdapter {
  #binding;
  #identity;
  #runtimeTransport;

  constructor(transport, binding, options = {}) {
    const normalizedBinding = createCmuxTuiBrowserBinding(binding);
    if (options !== undefined && options !== null && typeof options === 'object' &&
      Object.prototype.hasOwnProperty.call(options, 'workspaceId')) {
      throw new BrowserAdapterError('INVALID_OPTIONS');
    }
    const runtimeTransport = createCmuxTuiBrowserTransport(transport, normalizedBinding);
    super(runtimeTransport, options);
    this.#binding = normalizedBinding;
    this.#identity = Object.freeze({
      muxSessionId: normalizedBinding.muxSessionId,
      targetId: normalizedBinding.targetId,
    });
    this.#runtimeTransport = runtimeTransport;
  }

  get binding() {
    return this.#binding;
  }

  get identity() {
    return this.#identity;
  }

  get muxSessionId() {
    return this.#binding.muxSessionId;
  }

  get targetId() {
    return this.#binding.targetId;
  }

  async closeTarget(options = {}) {
    return this.#runtimeTransport.closeTarget(options);
  }
}

export function createOmpBrowserAdapter(transport, binding, options = {}) {
  return new OmpBrowserAdapter(transport, binding, options);
}

export {
  BrowserAdapter,
  BrowserAdapterError,
  BrowserIdentityError,
  BrowserTransportError,
  CmuxTuiBrowserRuntimeError,
  createBrowserAdapter,
};
