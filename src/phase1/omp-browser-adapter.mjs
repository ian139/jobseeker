import {
  BrowserAdapter,
  BrowserAdapterError,
  BrowserIdentityError,
  BrowserTransportError,
  createBrowserAdapter,
} from './browser-adapter.mjs';

export const OMP_BROWSER_ADAPTER_SCHEMA = 'phase1-omp-browser-adapter-v1';

/**
 * OMP `browser` tool actions remain an injected transport boundary bound to
 * the visible CMUX browser surface. Native browser/OS interactions that the
 * transport cannot perform are handled separately through the OMP `computer` tool.
 */
export class OmpBrowserAdapter extends BrowserAdapter {}

export function createOmpBrowserAdapter(transport, options = {}) {
  if (transport === null || typeof transport !== 'object') {
    throw new BrowserTransportError('TRANSPORT_REQUIRED');
  }
  return new OmpBrowserAdapter(transport, options);
}

export {
  BrowserAdapter,
  BrowserAdapterError,
  BrowserIdentityError,
  BrowserTransportError,
  createBrowserAdapter,
};
