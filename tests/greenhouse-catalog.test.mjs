import assert from 'node:assert/strict';
import test from 'node:test';
import {
  GreenhouseCatalogError,
  fetchGreenhouseEducationCatalog,
  findExactGreenhouseEducationOption,
  normalizeGreenhouseCatalogText,
  resolveGreenhouseEducationOption,
} from '../src/phase1/greenhouse-catalog.mjs';

const item = (id, text) => ({ id, text });
const page = (items, total = items.length, perPage = Math.max(1, items.length)) => ({
  items,
  meta: { total_count: total, per_page: perPage },
});
const response = (body, status = 200, headers = { 'content-type': 'application/json' }) => ({
  ok: status >= 200 && status < 300,
  status,
  headers,
  async text() {
    return typeof body === 'string' ? body : JSON.stringify(body);
  },
});

function sequence(fixtures) {
  const requests = [];
  const fetchImpl = async (url, init) => {
    requests.push({ url, init });
    const requestPage = Number(new URL(url).searchParams.get('page'));
    const fixture = fixtures[requestPage - 1];
    if (fixture === undefined) throw new Error(`unexpected page ${requestPage}`);
    return response(fixture);
  };
  return { fetchImpl, requests };
}

function catalogOptions(fetchImpl, extra = {}) {
  return {
    boardToken: 'acme/board',
    category: 'schools',
    queryText: 'Massachusetts Institute',
    fetchImpl,
    ...extra,
  };
}

test('fetches realistic paginated pages and freezes cloned output', async () => {
  const { fetchImpl, requests } = sequence([
    page([item(1, 'Massachusetts Institute')], 2, 1),
    page([item(2, 'Another Institute')], 2, 1),
  ]);
  const result = await fetchGreenhouseEducationCatalog(catalogOptions(fetchImpl));

  assert.equal(result.items_seen, 2);
  assert.equal(result.pages_fetched, 2);
  assert.equal(result.reported_total, 2);
  assert.deepEqual(result.items, [
    { value: '1', label: 'Massachusetts Institute' },
    { value: '2', label: 'Another Institute' },
  ]);
  assert.equal(requests.length, 2);
  assert.equal(requests[0].init.method, 'GET');
  assert.ok(requests[0].init.signal instanceof AbortSignal);
  assert.equal(new URL(requests[0].url).pathname, '/v1/boards/acme%2Fboard/education/schools');
  assert.equal(new URL(requests[0].url).searchParams.get('term'), 'Massachusetts Institute');
  assert.equal(new URL(requests[0].url).searchParams.get('page'), '1');
  assert.ok(Object.isFrozen(result));
  assert.ok(Object.isFrozen(result.items));
  assert.ok(Object.isFrozen(result.items[0]));
  assert.throws(() => {
    result.items[0].label = 'changed';
  }, TypeError);
  assert.equal(normalizeGreenhouseCatalogText('  A\tB  '), 'a b');
});

test('stops on the reported total and rejects overlapping IDs', async () => {
  const early = sequence([
    page([item(1, 'A'), item(2, 'B')], 2, 2),
    page([item(9, 'unexpected')], 2, 2),
  ]);
  const result = await fetchGreenhouseEducationCatalog(catalogOptions(early.fetchImpl));
  assert.equal(result.pages_fetched, 1);
  assert.equal(early.requests.length, 1);

  const overlap = sequence([
    page([item(1, 'A'), item(2, 'B')], 4, 2),
    page([item(2, 'B'), item(3, 'C')], 4, 2),
  ]);
  await assert.rejects(
    fetchGreenhouseEducationCatalog(catalogOptions(overlap.fetchImpl)),
    (error) => error.code === 'E_GREENHOUSE_REPEATED_PAGE',
  );
});

test('rejects repeated ordered pages, short-page truncation, and metadata drift', async () => {
  const repeated = sequence([
    page([item(1, 'A')], 2, 1),
    page([item(1, 'A')], 2, 1),
  ]);
  await assert.rejects(
    fetchGreenhouseEducationCatalog(catalogOptions(repeated.fetchImpl)),
    (error) => error instanceof GreenhouseCatalogError && error.code === 'E_GREENHOUSE_REPEATED_PAGE',
  );

  const short = sequence([page([item(1, 'A')], 3, 2)]);
  await assert.rejects(
    fetchGreenhouseEducationCatalog(catalogOptions(short.fetchImpl)),
    (error) => error.code === 'E_GREENHOUSE_TOTAL_MISMATCH',
  );

  const drift = sequence([
    page([item(1, 'A')], 2, 1),
    page([item(2, 'B')], 3, 1),
  ]);
  await assert.rejects(
    fetchGreenhouseEducationCatalog(catalogOptions(drift.fetchImpl)),
    (error) => error.code === 'E_GREENHOUSE_TOTAL_MISMATCH',
  );

  const unknownMeta = sequence([{
    items: [item(1, 'A')],
    meta: { total_count: 1, per_page: 1, page: 1 },
  }]);
  await assert.rejects(
    fetchGreenhouseEducationCatalog(catalogOptions(unknownMeta.fetchImpl)),
    (error) => error.code === 'E_GREENHOUSE_RESPONSE',
  );
});

test('rejects item and catalog bounds instead of returning a partial result', async () => {
  const tooManyItems = sequence([page([item(1, 'A')], 3, 1)]);
  await assert.rejects(
    fetchGreenhouseEducationCatalog(catalogOptions(tooManyItems.fetchImpl, { maxItems: 2 })),
    (error) => error.code === 'E_GREENHOUSE_BOUND',
  );

  const tooManyPages = sequence([
    page([item(1, 'A')], 3, 1),
    page([item(2, 'B')], 3, 1),
  ]);
  await assert.rejects(
    fetchGreenhouseEducationCatalog(catalogOptions(tooManyPages.fetchImpl, { maxPages: 1 })),
    (error) => error.code === 'E_GREENHOUSE_BOUND',
  );

  const oversized = sequence([page([item(1, 'x'.repeat(1_048_576))], 1, 1)]);
  await assert.rejects(
    fetchGreenhouseEducationCatalog(catalogOptions(oversized.fetchImpl)),
    (error) => error.code === 'E_GREENHOUSE_BOUND',
  );
});

test('classifies status, network, timeout, and caller abort errors', async () => {
  await assert.rejects(
    fetchGreenhouseEducationCatalog(catalogOptions(async () => response({}, 503))),
    (error) => error.code === 'E_GREENHOUSE_HTTP',
  );
  await assert.rejects(
    fetchGreenhouseEducationCatalog(catalogOptions(async () => {
      throw new Error('network down');
    })),
    (error) => error.code === 'E_GREENHOUSE_HTTP',
  );

  await assert.rejects(
    fetchGreenhouseEducationCatalog(catalogOptions(async (_url, { signal }) => new Promise((_, reject) => {
      signal.addEventListener('abort', () => reject(new Error('aborted')));
    }), { timeoutMs: 5 })),
    (error) => error.code === 'E_GREENHOUSE_TIMEOUT',
  );

  const controller = new AbortController();
  const aborted = fetchGreenhouseEducationCatalog(catalogOptions(async (_url, { signal }) => new Promise((_, reject) => {
    signal.addEventListener('abort', () => reject(new Error('aborted')));
  }), { signal: controller.signal }));
  controller.abort();
  await assert.rejects(aborted, (error) => error.code === 'E_GREENHOUSE_TIMEOUT');
});

test('rejects malformed responses and unknown options', async () => {
  await assert.rejects(
    fetchGreenhouseEducationCatalog(catalogOptions(async () => response('{'))),
    (error) => error.code === 'E_GREENHOUSE_RESPONSE',
  );
  await assert.rejects(
    fetchGreenhouseEducationCatalog(catalogOptions(async () => response({
      items: [{ id: 1, text: 'A', extra: true }],
      meta: { total_count: 1, per_page: 1 },
    }))),
    (error) => error.code === 'E_GREENHOUSE_RESPONSE',
  );
  await assert.rejects(
    fetchGreenhouseEducationCatalog({ ...catalogOptions(async () => response(page([], 0))), extra: true }),
    (error) => error.code === 'E_GREENHOUSE_ARGUMENT',
  );
  await assert.rejects(
    fetchGreenhouseEducationCatalog(catalogOptions(
      async () => response(page([], 0)),
      { boardToken: 'acme\u0000board' },
    )),
    (error) => error.code === 'E_GREENHOUSE_ARGUMENT',
  );
  await assert.rejects(
    fetchGreenhouseEducationCatalog(catalogOptions(
      async () => response(page([], 0)),
      { maxPages: 0 },
    )),
    (error) => error.code === 'E_GREENHOUSE_ARGUMENT',
  );
});
test('requires JSON content type and validates bounded response bodies and items', async () => {
  await assert.rejects(
    fetchGreenhouseEducationCatalog(catalogOptions(async () => response(page([], 0), 200, {
      'content-type': 'text/plain',
    }))),
    (error) => error.code === 'E_GREENHOUSE_HTTP',
  );
  await assert.rejects(
    fetchGreenhouseEducationCatalog(catalogOptions(async () => response(page([], 0), 200, {
      'content-type': 'application/json',
      'content-length': String(1_048_577),
    }))),
    (error) => error.code === 'E_GREENHOUSE_BOUND',
  );

  const streamBody = new TextEncoder().encode('x'.repeat(1_048_577));
  let cancelled = false;
  await assert.rejects(
    fetchGreenhouseEducationCatalog(catalogOptions(async () => ({
      ok: true,
      status: 200,
      headers: { 'content-type': 'application/json' },
      body: {
        getReader() {
          return {
            async read() {
              return { done: false, value: streamBody };
            },
            async cancel() {
              cancelled = true;
            },
            releaseLock() {},
          };
        },
      },
    }))),
    (error) => error.code === 'E_GREENHOUSE_BOUND',
  );
  assert.equal(cancelled, true);

  for (const invalid of [
    item(0, 'A'),
    item(-1, 'A'),
    item(1.5, 'A'),
    item(Number.MAX_SAFE_INTEGER + 1, 'A'),
    item(1, ' A'),
    item(1, 'A '),
    item(1, '\t'),
    item(1, `A${String.fromCharCode(0)}`),
    item(1, 'x'.repeat(4097)),
  ]) {
    await assert.rejects(
      fetchGreenhouseEducationCatalog(catalogOptions(async () => response(page([invalid], 1, 1)))),
      (error) => error.code === 'E_GREENHOUSE_RESPONSE',
    );
  }
});

test('finds canonical exact matches as frozen clones and reports ambiguity', async () => {
  const source = { value: '1', label: '  Massachusetts  Institute ' };
  const match = findExactGreenhouseEducationOption([source], 'massachusetts institute');
  assert.deepEqual(match, { value: '1', label: '  Massachusetts  Institute ' });
  assert.notEqual(match, source);
  assert.ok(Object.isFrozen(match));
  source.label = 'changed';
  assert.equal(match.label, '  Massachusetts  Institute ');

  assert.deepEqual(
    findExactGreenhouseEducationOption(
      [{ value: '2', label: 'University of California - Los Angeles' }],
      'University of California, Los Angeles (UCLA)',
    ),
    { value: '2', label: 'University of California - Los Angeles' },
  );

  assert.equal(findExactGreenhouseEducationOption([{ value: '1', label: 'A' }], 'missing'), null);
  assert.throws(
    () => findExactGreenhouseEducationOption([
      { value: '1', label: 'A' },
      { value: '2', label: ' a ' },
    ], 'A'),
    (error) => error.code === 'E_GREENHOUSE_AMBIGUOUS',
  );
});

test('resolves full value before bounded word-prefix probes without fuzzy fallback', async () => {
  const terms = [];
  const fetchImpl = async (url) => {
    const parsedUrl = new URL(url);
    const term = parsedUrl.searchParams.get('term');
    terms.push(term);
    if (term === 'Massachusetts Institute') {
      return response(page([item(7, 'Massachusetts Institute of Technology')], 1, 1));
    }
    return response(page([], 0, 1));
  };
  const result = await resolveGreenhouseEducationOption({
    boardToken: 'acme',
    category: 'schools',
    value: 'Massachusetts Institute of Technology',
    fetchImpl,
    maxProbes: 3,
  });
  assert.deepEqual(terms, [
    'Massachusetts Institute of Technology',
    'Massachusetts Institute of',
    'Massachusetts Institute',
  ]);
  assert.deepEqual(result, {
    category: 'schools',
    query_text: 'Massachusetts Institute',
    option_text: 'Massachusetts Institute of Technology',
    option_value: '7',
    pages_fetched: 3,
    items_seen: 1,
    probes: 3,
  });
  assert.ok(Object.isFrozen(result));

  const attempted = [];
  await assert.rejects(
    resolveGreenhouseEducationOption({
      boardToken: 'acme',
      category: 'schools',
      value: 'A B C',
      fetchImpl: async (url) => {
        attempted.push(new URL(url).searchParams.get('term'));
        return response(page([], 0, 1));
      },
      maxProbes: 2,
    }),
    (error) => error.code === 'E_GREENHOUSE_NOT_FOUND',
  );
  assert.deepEqual(attempted, ['A B C', 'A B']);
});

test('reports ambiguity during resolution', async () => {
  await assert.rejects(
    resolveGreenhouseEducationOption({
      boardToken: 'acme',
      category: 'schools',
      value: 'MIT',
      fetchImpl: async () => response(page([
        item(1, 'MIT'),
        item(2, 'mit'),
      ], 2, 2)),
    }),
    (error) => error.code === 'E_GREENHOUSE_AMBIGUOUS',
  );
});
