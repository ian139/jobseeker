import test from 'node:test';
import assert from 'node:assert/strict';
import {
  COMPANY_SITE_ERROR_CODES,
  CompanySiteIngestionError,
  createCompanySiteAdapter,
} from '../src/ingestion/company-site.mjs';
import { validateNormalizedJob } from '../src/ingestion/contracts.mjs';

const response = (body, status = 200, contentType = 'text/html') => new Response(body, {
  status,
  headers: { 'content-type': contentType },
});

const companies = [
  { id: 'gh', source: 'greenhouse', hostname: 'boards.greenhouse.io', boardToken: 'acme' },
  { id: 'ash', source: 'ashby', hostname: 'jobs.ashbyhq.com', boardName: 'acme' },
  { id: 'custom', source: 'custom', hostname: 'careers.example.test', careerUrl: 'https://careers.example.test/jobs' },
];

test('buildRequest represents the full registry and normalizes checkpoints', () => {
  const adapter = createCompanySiteAdapter({ companies, fetch: async () => response('') });
  const request = adapter.buildRequest({ page: 2, limit: 10, checkpoint: '2024-01-01', windowEnd: '2024-02-01' });
  assert.equal(request.url, 'https://boards.greenhouse.io');
  assert.deepEqual(request.body.companies, ['gh', 'ash', 'custom']);
  assert.equal(request.page, 2);
  assert.equal(request.checkpoint, '2024-01-01T00:00:00.000Z');
  assert.equal(adapter.normalizeCheckpoint(undefined), '1970-01-01T00:00:00.000Z');
});

test('delegates Greenhouse board requests and pages', async () => {
  let requested;
  const fetch = async (url) => {
    requested = url;
    return response(JSON.stringify({ jobs: [{ id: 1, title: 'Engineer', location: { name: 'Remote' }, absolute_url: 'https://boards.greenhouse.io/acme/jobs/1', content: 'Build things' }] }), 200, 'application/json');
  };
  const adapter = createCompanySiteAdapter({ companies: [companies[0]], fetch });
  const page = await adapter.fetchPage({ request: adapter.buildRequest({ page: 0 }) });
  assert.match(requested, /^https:\/\/boards-api\.greenhouse\.io\/v1\/boards\/acme\/jobs\?content=true$/u);
  assert.equal(page.items.length, 1);
  const normalized = adapter.normalizeJob(page.items[0], {
    observedAt: page.receivedAt,
    rawPayloadPath: 'raw/jobs/greenhouse-1.json',
    rawPayloadSha256: 'c'.repeat(64),
  });
  assert.equal(validateNormalizedJob(normalized).atsKind, 'greenhouse');
  assert.equal(normalized.description, 'Build things');
  assert.equal(page.items[0]._atsSource, 'greenhouse');
});

test('delegates Ashby board requests and pages', async () => {
  let requested;
  const fetch = async (url) => {
    requested = url;
    return response(JSON.stringify([{ id: 'a1', title: 'Designer', locationName: 'New York', jobUrl: 'https://jobs.ashbyhq.com/acme/a1' }]), 200, 'application/json');
  };
  const adapter = createCompanySiteAdapter({ companies: [companies[1]], fetch });
  const page = await adapter.fetchPage({ request: adapter.buildRequest({ page: 0 }) });
  assert.match(requested, /ashbyhq\.com\/posting-api\/job-board\/acme/);
  assert.equal(page.items.length, 1);
  assert.equal(page.items[0]._atsSource, 'ashby');
});

test('extracts custom JSON-LD JobPosting and normalizes schema', async () => {
  const html = `<html><script type="application/ld+json">${JSON.stringify({ '@type': 'JobPosting', identifier: { value: 'j1' }, title: 'Backend Engineer', hiringOrganization: { name: 'Example Co' }, jobLocation: { address: { addressLocality: 'Remote' } }, jobLocationType: 'TELECOMMUTE', datePosted: '2024-01-02', url: 'https://careers.example.test/jobs/j1', description: 'A backend role.' })}</script></html>`;
  const adapter = createCompanySiteAdapter({ companies: [companies[2]], fetch: async () => response(html), now: () => '2024-02-01T00:00:00.000Z' });
  const page = await adapter.fetchPage({ request: adapter.buildRequest({}) });
  assert.equal(page.totalResults, 1);
  const job = adapter.normalizeJob(page.items[0], { observedAt: page.receivedAt, rawPayloadPath: 'raw/jobs/j1.json', rawPayloadSha256: 'a'.repeat(64) });
  assert.equal(job.title, 'Backend Engineer');
  assert.equal(job.workplaceType, 'remote');
  assert.equal(job.rawPayloadSha256, 'a'.repeat(64));
  assert.equal(validateNormalizedJob(job).schema, 'normalized-job-v1');
});

test('falls back to deterministic career links when JSON-LD is absent', async () => {
  const html = '<main><a href="/jobs/42">Operations Manager</a><a href="/about">About</a></main>';
  const adapter = createCompanySiteAdapter({ companies: [companies[2]], fetch: async () => response(html) });
  const page = await adapter.fetchPage({ request: adapter.buildRequest({}) });
  assert.equal(page.totalResults, 1);
  assert.equal(page.items[0].title, 'Operations Manager');
  assert.equal(page.items[0].url, 'https://careers.example.test/jobs/42');
});

test('fetchPage combines the registry once and slices stable pages', async () => {
  let fetchCount = 0;
  const registry = [
    { id: 'one', source: 'custom', hostname: 'one.example.test', careerUrl: 'https://one.example.test/jobs' },
    { id: 'two', source: 'custom', hostname: 'two.example.test', careerUrl: 'https://two.example.test/jobs' },
  ];
  const adapter = createCompanySiteAdapter({
    companies: registry,
    fetch: async (url) => {
      fetchCount += 1;
      const id = new URL(url).hostname.split('.')[0];
      return response(`<a href="/jobs/${id}">${id} Engineer</a>`);
    },
  });
  const first = await adapter.fetchPage({ request: adapter.buildRequest({ page: 0, limit: 1 }) });
  const second = await adapter.fetchPage({ request: adapter.buildRequest({ page: 1, limit: 1 }) });
  assert.equal(first.items.length, 1);
  assert.equal(second.items.length, 1);
  assert.equal(first.totalResults, 2);
  assert.equal(second.totalResults, 2);
  assert.equal(fetchCount, 2);
});

test('reports stable errors and treats deferred ATS sites as deterministic HTML sources', async () => {
  assert.throws(() => createCompanySiteAdapter({ companies: [] }), (error) => error instanceof CompanySiteIngestionError && error.code === COMPANY_SITE_ERROR_CODES.invalid_config);
  const adapter = createCompanySiteAdapter({ companies: [companies[2]], fetch: async () => response('') });
  assert.throws(() => adapter.normalizeCheckpoint('not-a-date'), (error) => error.code === COMPANY_SITE_ERROR_CODES.invalid_checkpoint);
  const workday = createCompanySiteAdapter({
    companies: [{ id: 'wd', source: 'workday', hostname: 'wd.test' }],
    fetch: async () => response('<a href="/jobs/1">Platform Engineer</a>'),
  });
  const page = await workday.fetchPage({ request: workday.buildRequest({}) });
  assert.equal(page.items.length, 1);
  const job = workday.normalizeJob(page.items[0], {
    observedAt: page.receivedAt,
    rawPayloadPath: 'raw/jobs/workday-1.json',
    rawPayloadSha256: 'b'.repeat(64),
  });
  assert.equal(job.atsKind, 'workday');
});
