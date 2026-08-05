const SOURCE_RE = /^[a-z][a-z0-9_-]{0,63}$/u;
const PROFILE_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/u;

function deepFreeze(value, seen = new Set()) {
  if (value === null || (typeof value !== 'object' && typeof value !== 'function')) {
    return value;
  }
  if (seen.has(value)) {
    return value;
  }
  seen.add(value);
  Object.freeze(value);
  for (const prop of Object.getOwnPropertyNames(value)) {
    const desc = Object.getOwnPropertyDescriptor(value, prop);
    if (desc && (desc.value !== undefined || desc.get !== undefined)) {
      if (typeof desc.value === 'object' || typeof desc.value === 'function') {
        deepFreeze(desc.value, seen);
      }
    }
  }
  return value;
}

function toPascalCase(str) {
  return str
    .split(/[-_]/u)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join('');
}

function factoryNameFor(source) {
  if (source === 'theirstack') return 'createTheirStackAdapter';
  if (source === 'linkedin') return 'createLinkedInAdapter';
  return `create${toPascalCase(source)}Adapter`;
}
export async function createAdapter({
  source = 'theirstack',
  profile = 'default',
  config = {},
  env = process.env,
  now,
} = {}) {
  const actualSource = typeof source === 'string' && source ? source : 'theirstack';
  const actualProfile = typeof profile === 'string' && profile ? profile : 'default';

  if (!SOURCE_RE.test(actualSource)) {
    throw new Error('source_invalid');
  }
  if (!PROFILE_RE.test(actualProfile)) {
    throw new Error('profile_invalid');
  }

  let module;
  try {
    module = await import(`./${actualSource}.mjs`);
  } catch (error) {
    if (error?.code === 'ERR_MODULE_NOT_FOUND') throw new Error('source_unsupported');
    throw error;
  }

  const factoryName = factoryNameFor(actualSource);
  const factory = module[factoryName] ?? module.createAdapter ?? (typeof module.default === 'function' ? module.default : null);

  if (typeof factory !== 'function') {
    throw new Error('source_unsupported');
  }

  const supportedProfiles = module.THEIRSTACK_PROFILE_NAMES
    ?? module.SUPPORTED_PROFILES
    ?? module.PROFILE_NAMES;

  if (Array.isArray(supportedProfiles) && !supportedProfiles.includes(actualProfile)) {
    throw new Error('profile_unsupported');
  }

  const options = {};

  if (config.fetch) options.fetch = config.fetch;
  if (now || config.now) options.now = now ?? (typeof config.now === 'string' ? () => config.now : config.now);
  if (config.timeoutMs !== undefined) options.timeoutMs = config.timeoutMs;
  if (config.postedAtMaxAgeDays !== undefined) options.postedAtMaxAgeDays = config.postedAtMaxAgeDays;
  if (config.maxResponseBytes !== undefined) options.maxResponseBytes = config.maxResponseBytes;
  if (config.queryFilters !== undefined) options.queryFilters = config.queryFilters;
  if (config.windowEnd !== undefined) options.windowEnd = config.windowEnd;
  if (config.paidAuthorization !== undefined) options.paidAuthorization = config.paidAuthorization;

  if (actualSource === 'theirstack') {
    const apiKey = env?.THEIRSTACK_API_KEY ?? config.apiKey ?? config.apiToken;
    if (apiKey !== undefined) options.apiKey = apiKey;
    if (config.maxPreviewRetries !== undefined) options.maxPreviewRetries = config.maxPreviewRetries;
    if (config.retryDelayMs !== undefined) options.retryDelayMs = config.retryDelayMs;
    if (config.creditNow !== undefined) options.creditNow = config.creditNow;
  } else if (actualSource === 'greenhouse') {
    const boardToken = env?.GREENHOUSE_BOARD_TOKEN ?? config.boardToken ?? (actualProfile !== 'default' ? actualProfile : null);
    if (boardToken) options.boardToken = boardToken;
    if (config.baseUrl) options.baseUrl = config.baseUrl;
  } else if (actualSource === 'ashby') {
    const boardName = env?.ASHBY_BOARD_NAME ?? config.boardName ?? (actualProfile !== 'default' ? actualProfile : null);
    if (boardName) options.boardName = boardName;
    if (config.baseUrl) options.baseUrl = config.baseUrl;
  } else if (actualSource === 'company-site') {
    const companies = config.companies ?? (env?.COMPANY_SOURCES ? JSON.parse(env.COMPANY_SOURCES) : null);
    if (companies) options.companies = companies;
  } else if (actualSource === 'linkedin') {
    const searchUrl = env?.LINKEDIN_SEARCH_URL ?? config.searchUrl ?? config.savedSearchQuery ?? null;
    if (searchUrl) options.searchUrl = searchUrl;
    const savedSearchQuery = config.savedSearchQuery ?? null;
    if (savedSearchQuery) options.savedSearchQuery = savedSearchQuery;
    if (config.baseUrl) options.baseUrl = config.baseUrl;
    if (config.browserFetch) options.browserFetch = config.browserFetch;
  }

  const adapter = factory(options);
  return deepFreeze(adapter);
}
