import {
  BrowserAdapter,
  BrowserAdapterError,
  BrowserIdentityError,
  BrowserTransportError,
  createBrowserAdapter,
} from './browser-adapter.mjs';
import {
  CmuxGuiBrowserRuntimeError,
  createCmuxGuiBrowserBinding,
  createCmuxGuiBrowserTransport,
} from './cmux-gui-browser-runtime.mjs';

export const OMP_BROWSER_ADAPTER_SCHEMA = 'phase1-omp-browser-adapter-v1';

/**
 * OMP `browser` tool actions are bound to one visible surface target in one CMUX
 * GUI workspace/window. The injected transport carries the binding on every request;
 * closing a target never exposes a runtime/session close operation.
 */
export class OmpBrowserAdapter extends BrowserAdapter {
  #binding;
  #identity;
  #runtimeTransport;

  constructor(transport, binding, options = {}) {
    const normalizedBinding = createCmuxGuiBrowserBinding(binding);
    if (options !== undefined && options !== null && typeof options === 'object' &&
      Object.prototype.hasOwnProperty.call(options, 'workspaceId')) {
      throw new BrowserAdapterError('INVALID_OPTIONS');
    }
    const runtimeTransport = createCmuxGuiBrowserTransport(transport, normalizedBinding);
    super(runtimeTransport, options);
    this.#binding = normalizedBinding;
    this.#identity = Object.freeze({
      windowId: normalizedBinding.windowId,
      workspaceId: normalizedBinding.workspaceId,
      surfaceId: normalizedBinding.surfaceId,
    });
    this.#runtimeTransport = runtimeTransport;
  }

  get binding() {
    return this.#binding;
  }

  get identity() {
    return this.#identity;
  }

  get windowId() {
    return this.#binding.windowId;
  }

  get workspaceId() {
    return this.#binding.workspaceId;
  }

  get surfaceId() {
    return this.#binding.surfaceId;
  }

  get socketPath() {
    return this.#binding.socketPath;
  }

  get profileMode() {
    return this.#binding.profileMode;
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
  CmuxGuiBrowserRuntimeError,
  createBrowserAdapter,
};
