import test from 'node:test';
import assert from 'node:assert/strict';
import { createLinkedInAdapter, LinkedInIngestionError } from '../src/ingestion/linkedin.mjs';
import { validateNormalizedJob, sha256Text } from '../src/ingestion/contracts.mjs';

const fixture = `<!doctype html><html><body>
<div class="results-context-header__job-count">2 results</div>
<ul class="jobs-search-results__list"><li class="job-card-container" data-job-id="12345">
<a class="job-card-container__link" href="https://www.linkedin.com/jobs/view/12345/?refId=tracking">Senior Engineer</a>
<div class="job-card-container__company-name">Acme Corp</div><div class="job-card-container__metadata-item">Remote</div>
<time datetime="2026-07-01">2 weeks ago</time><div class="job-card-list__snippet">Build reliable systems.</div>
<a href="https://jobs.acme.example/apply/12345?utm_source=linkedin">Apply</a></li>
<li class="job-card-container" data-job-id="12345"><a href="/jobs/view/12345/">Duplicate</a></li>
<li class="job-card-container" data-job-id="67890"><a class="job-card-list__title" href="/jobs/view/67890/">Product Manager</a>
<div class="job-card-container__primary-description">Beta Inc</div><div class="job-card-container__metadata-item">New York, NY</div></li></ul></body></html>`;

function response(body, status = 200) {
  return { status, ok: status >= 200 && status < 300, text: async () => body };
}

test('buildRequest paginates configured search URL', () => {
  const adapter = createLinkedInAdapter({ searchUrl: 'https://www.linkedin.com/jobs/search/?keywords=engineer' });
  const request = adapter.buildRequest({ page: 2, limit: 25 });
  assert.equal(new URL(request.url).searchParams.get('start'), '50');
  assert.equal(request.method, 'GET');
  assert.match(request.requestSha256, /^[0-9a-f]{64}$/);
});

test('fetchPage extracts and deduplicates visible listings', async () => {
  const adapter = createLinkedInAdapter({ savedSearchQuery: 'ignored', browserFetch: async () => response(fixture), now: () => '2026-08-01T00:00:00.000Z' });
  const request = adapter.buildRequest({});
  const page = await adapter.fetchPage({ request });
  assert.equal(page.items.length, 2);
  assert.equal(page.totalResults, 2);
  assert.equal(page.items[0].canonicalListingUrl, 'https://www.linkedin.com/jobs/view/12345');
  assert.equal(page.items[0].company, 'Acme Corp');
  assert.equal(page.items[0].descriptionSnippet, 'Build reliable systems.');
});

test('normalizeJob produces schema-valid output and checkpoint is canonical', () => {
  const adapter = createLinkedInAdapter({ now: () => '2026-08-01T00:00:00.000Z' });
  const job = adapter.normalizeJob({ id: '12345', title: 'Engineer', company: 'Acme', listingUrl: 'https://www.linkedin.com/jobs/view/12345/', descriptionSnippet: 'Remote full-time role' }, { observedAt: '2026-08-01', rawPayloadPath: '/linkedin/1.json', rawPayloadSha256: sha256Text('{}') });
  assert.equal(validateNormalizedJob(job).source, 'linkedin');
  assert.equal(adapter.normalizeCheckpoint('2026-08-01'), '2026-08-01T00:00:00.000Z');
});

for (const [name, html] of [
  ['login', '<form id="username" action="/uas/login-submit">Sign in to LinkedIn</form>'],
  ['captcha', '<div class="g-recaptcha">Verify you are human</div>'],
  ['identity', '<h1>Verify your identity</h1>'],
  ['unusual', '<p>Unusual activity detected</p>'],
  ['restricted', '<p>Your account has been restricted</p>'],
  ['consent', '<p>Cookie preferences required</p>'],
  ['unexpected', '<div class="error-container">Something went wrong</div>'],
]) {
  test(`stops on ${name} account health condition`, async () => {
    const adapter = createLinkedInAdapter({ browserFetch: async () => response(html) });
    const request = adapter.buildRequest({ searchUrl: 'https://www.linkedin.com/jobs/search/' });
    await assert.rejects(() => adapter.fetchPage({ request }), (error) => error instanceof LinkedInIngestionError && error.code === 'account_health');
  });
}

for (const status of [401, 403, 429]) {
  test(`stops on HTTP ${status}`, async () => {
    const adapter = createLinkedInAdapter({ browserFetch: async () => response('<html></html>', status) });
    const request = adapter.buildRequest({ searchUrl: 'https://www.linkedin.com/jobs/search/' });
    await assert.rejects(() => adapter.fetchPage({ request }), (error) => error.code === 'account_health');
  });
}

test('fetchPage fails closed without an authenticated visible-browser bridge', async () => {
  const adapter = createLinkedInAdapter({ savedSearchQuery: 'engineer' });
  await assert.rejects(
    () => adapter.fetchPage({ request: adapter.buildRequest({}) }),
    (error) => error.code === 'invalid_request' && /visible-browser bridge/u.test(error.message),
  );
});
