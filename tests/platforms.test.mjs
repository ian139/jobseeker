import assert from 'node:assert/strict';
import test from 'node:test';

import {
  canonicalizeApplicationUrl,
  classifyApplicationUrl,
  extractPlatformJobSnapshot,
  filterSupportedJobs,
  planPlatformApplication,
  reclassifyApplicationRedirect,
} from '../src/phase1/platforms.mjs';

const GREENHOUSE_URL = 'https://job-boards.greenhouse.io/northstar/jobs/1234567';
const GREENHOUSE_ALIAS_URL = 'https://boards.greenhouse.io/northstar/jobs/1234567';
const GREENHOUSE_EU_URL = 'https://job-boards.eu.greenhouse.io/northstar/jobs/1234567';
const GREENHOUSE_EU_ALIAS_URL = 'https://boards.eu.greenhouse.io/northstar/jobs/1234567';
const ASHBY_URL = 'https://jobs.ashbyhq.com/orbit/11111111-1111-4111-8111-111111111111';
const EMPLOYER_HOST = 'jobs.northstar.example';
const EMPLOYER_HOSTED_URL = `https://${EMPLOYER_HOST}/careers/backend-engineer/apply`;
const RESUME_UPLOAD_PATH = '/tmp/synthetic-resume.pdf';


function answer(source, value) {
  return { source, value };
}

function observerControl(stable_id, ref, overrides = {}) {
  return {
    stable_id,
    ref,
    kind: 'input',
    tag: 'input',
    type: 'text',
    role: 'textbox',
    label: stable_id,
    name: stable_id,
    options: [],
    candidate: { class: 'field', reason: 'synthetic field' },
    required: true,
    visible: true,
    enabled: true,
    disabled: false,
    readonly: false,
    value: null,
    value_present: false,
    checked: null,
    selected: null,
    ...overrides,
  };
}

function planInput(platform, observation_id, controls, answers, extras = {}) {
  const { observationUrl, ...topLevel } = extras;
  return {
    platform,
    ...topLevel,
    observation: {
      observation_id,
      controls,
      ...(observationUrl === undefined ? {} : { url: observationUrl }),
    },
    answers,
    resumeUploadPath: RESUME_UPLOAD_PATH,
  };
}

function assertFrozenDeep(value) {
  assert.equal(Object.isFrozen(value), true);
  if (value && typeof value === 'object') {
    for (const child of Object.values(value)) assertFrozenDeep(child);
  }
}

test('accepts exact Greenhouse/Ashby routes and strips tracking queries canonically', () => {
  const valid = [
    [GREENHOUSE_URL, 'greenhouse', GREENHOUSE_URL],
    [`${GREENHOUSE_ALIAS_URL}?gh_src=synthetic&utm_source=fixture`, 'greenhouse', GREENHOUSE_ALIAS_URL],
    [GREENHOUSE_EU_URL, 'greenhouse', GREENHOUSE_EU_URL],
    [`${GREENHOUSE_EU_ALIAS_URL}?gh_src=synthetic`, 'greenhouse', GREENHOUSE_EU_ALIAS_URL],
    [ASHBY_URL, 'ashby', ASHBY_URL],
    [`${ASHBY_URL}?utm_source=synthetic&source=fixture`, 'ashby', ASHBY_URL],
  ];

  for (const [url, platform, canonical] of valid) {
    assert.equal(classifyApplicationUrl(url), platform, url);
    assert.equal(canonicalizeApplicationUrl(url), canonical, url);
  }
});
test('classifies employer routes only with an exact verified host and bounded pathname', () => {
  const employerWithQuery = `${EMPLOYER_HOSTED_URL}?utm_source=fixture`;
  assert.equal(classifyApplicationUrl(EMPLOYER_HOSTED_URL), null);
  assert.equal(canonicalizeApplicationUrl(EMPLOYER_HOSTED_URL), null);
  assert.equal(
    classifyApplicationUrl(employerWithQuery, { verifiedEmployerHost: EMPLOYER_HOST }),
    'employer_hosted',
  );
  assert.equal(
    canonicalizeApplicationUrl(employerWithQuery, { verifiedEmployerHost: EMPLOYER_HOST }),
    EMPLOYER_HOSTED_URL,
  );
  for (const [url, host] of [
    [`https://evil.${EMPLOYER_HOST}/careers/backend-engineer/apply`, EMPLOYER_HOST],
    [`https://${EMPLOYER_HOST}:443/careers/backend-engineer/apply`, EMPLOYER_HOST],
    [`https://user:secret@${EMPLOYER_HOST}/careers/backend-engineer/apply`, EMPLOYER_HOST],
    [`https://${EMPLOYER_HOST}/careers//backend-engineer`, EMPLOYER_HOST],
    [`https://${EMPLOYER_HOST}/careers/../apply`, EMPLOYER_HOST],
    [`https://${EMPLOYER_HOST}/careers/backend%2Dengineer/apply`, EMPLOYER_HOST],
    [`https://${EMPLOYER_HOST}/jobs/apply`, 'other.northstar.example'],
    [`https://${EMPLOYER_HOST}/backend-engineer/apply`, EMPLOYER_HOST],
    [`https://${EMPLOYER_HOST}/careers/backend-engineer/apply/extra/one/two/three/four/five/six`, EMPLOYER_HOST],
  ]) {
    assert.equal(classifyApplicationUrl(url, { verifiedEmployerHost: host }), null, url);
    assert.equal(canonicalizeApplicationUrl(url, { verifiedEmployerHost: host }), null, url);
  }
  assert.throws(
    () => classifyApplicationUrl(EMPLOYER_HOSTED_URL, { verifiedEmployerHost: 'xn--northstar.example' }),
    (error) => error.code === 'INVALID_URL_OPTIONS',
  );
  assert.throws(
    () => classifyApplicationUrl(EMPLOYER_HOSTED_URL, { verifiedEmployerHost: '127.0.0.1' }),
    (error) => error.code === 'INVALID_URL_OPTIONS',
  );
  assert.throws(
    () => classifyApplicationUrl(EMPLOYER_HOSTED_URL, { verifiedEmployerHost: EMPLOYER_HOST, extra: true }),
    (error) => error.code === 'INVALID_URL_OPTIONS',
  );
});


test('rejects credential-bearing, lookalike, malformed, and unsupported ATS URLs', () => {
  const invalid = [
    'https://greenhouse.io/northstar/jobs/1234567',
    'https://www.greenhouse.io/northstar/jobs/1234567',
    'https://job-boards.greenhouse.io.evil.test/northstar/jobs/1234567',
    'https://evil.job-boards.greenhouse.io/northstar/jobs/1234567',
    'https://user@job-boards.greenhouse.io/northstar/jobs/1234567',
    'https://user:secret@job-boards.greenhouse.io/northstar/jobs/1234567',
    'https://jobs.ashbyhq.com.evil.test/orbit/11111111-1111-4111-8111-111111111111',
    'https://evil.jobs.ashbyhq.com/orbit/11111111-1111-4111-8111-111111111111',
    'https://user@jobs.ashbyhq.com/orbit/11111111-1111-4111-8111-111111111111',
    'https://user:secret@jobs.ashbyhq.com/orbit/11111111-1111-4111-8111-111111111111',
    'https://jobs.lever.co/northstar/backend-engineer',
    'https://northstar.wd1.myworkdayjobs.com/jobs/backend-engineer',
    'https://job-boards.greenhouse.io/northstar',
    'https://job-boards.greenhouse.io/northstar/jobs/0',
    'https://job-boards.greenhouse.io/northstar/jobs/1234567/',
    'https://jobs.ashbyhq.com/orbit',
    'https://jobs.ashbyhq.com/orbit/not-a-uuid',
    `${ASHBY_URL}/extra`,
    `${GREENHOUSE_URL}#fragment`,
    'not a URL',
  ];

  for (const url of invalid) {
    assert.equal(classifyApplicationUrl(url), null, url);
    assert.equal(canonicalizeApplicationUrl(url), null, url);
  }
});

test('filters supported jobs without mutation, deduplication, or reordering', () => {
  const rows = [
    {
      id: 'first-greenhouse',
      applicationUrl: GREENHOUSE_URL,
      metadata: { source: 'synthetic-a' },
    },
    {
      id: 'unsupported',
      applicationUrl: 'https://jobs.lever.co/northstar/backend-engineer',
      metadata: { source: 'synthetic-b' },
    },
    {
      id: 'duplicate-greenhouse',
      applicationUrl: `${GREENHOUSE_URL}?gh_src=duplicate`,
      metadata: { source: 'synthetic-c' },
    },
    {
      id: 'ashby',
      applicationUrl: `${ASHBY_URL}?utm_source=duplicate`,
      metadata: { source: 'synthetic-d' },
    },
    {
      id: 'employer-hosted',
      applicationUrl: `${EMPLOYER_HOSTED_URL}?source=fixture`,
      verifiedEmployerHost: EMPLOYER_HOST,
      metadata: { source: 'synthetic-e' },
    },
  ];
  const before = structuredClone(rows);

  const filtered = filterSupportedJobs(rows);

  assert.deepEqual(rows, before);
  assert.deepEqual(
    filtered.map(({ id, platform }) => ({ id, platform })),
    [
      { id: 'first-greenhouse', platform: 'greenhouse' },
      { id: 'duplicate-greenhouse', platform: 'greenhouse' },
      { id: 'ashby', platform: 'ashby' },
      { id: 'employer-hosted', platform: 'employer_hosted' },
    ],
  );
  assert.equal(filtered[1].applicationUrl, `${GREENHOUSE_URL}?gh_src=duplicate`);
  assert.notEqual(filtered[0], rows[0]);
  assert.notEqual(filtered[0].metadata, rows[0].metadata);
  assertFrozenDeep(filtered);
});

test('extracts a Greenhouse snapshot while preserving text and removing script/style content', () => {
  const input = {
    applicationUrl: GREENHOUSE_URL,
    payload: {
      id: 1234567,
      title: 'Backend Platform Engineer',
      company_name: 'Northstar Robotics',
      location: { name: 'Remote, United States' },
      absolute_url: GREENHOUSE_URL,
      content: [
        '<p>Build reliable data services for robots &amp; teams.</p>',
        '<h2>Requirements</h2><ul><li>Python</li><li>SQL</li></ul>',
        '<script>SECRET_SCRIPT Python</script>',
        '<style>.secret { content: "SECRET_STYLE"; }</style>',
      ].join(''),
    },
  };

  const snapshot = extractPlatformJobSnapshot(input);

  assert.deepEqual(snapshot, {
    schema: 'platform-job-snapshot-v1',
    platform: 'greenhouse',
    applicationUrl: GREENHOUSE_URL,
    applicationHost: 'job-boards.greenhouse.io',
    externalJobId: '1234567',
    title: 'Backend Platform Engineer',
    company: 'Northstar Robotics',
    location: 'Remote, United States',
    description: 'Build reliable data services for robots & teams. Requirements Python SQL',
  });
  assert.equal(snapshot.description.includes('SECRET_SCRIPT'), false);
  assert.equal(snapshot.description.includes('SECRET_STYLE'), false);
  assert.equal(snapshot.description.includes('<'), false);
  assertFrozenDeep(snapshot);
});

test('extracts an Ashby snapshot while preserving text and removing script/style content', () => {
  const input = {
    applicationUrl: ASHBY_URL,
    payload: {
      jobPosting: {
        id: '11111111-1111-4111-8111-111111111111',
        title: 'Frontend Product Engineer',
        organizationName: 'Orbit Software',
        locationName: 'Remote, United States',
        applicationUrl: ASHBY_URL,
        descriptionHtml: [
          '<div><p>Build accessible product interfaces.</p>',
          '<p>Requirements: React and TypeScript.</p>',
          '<script>SECRET_SCRIPT React</script>',
          '<style>.secret { content: "SECRET_STYLE"; }</style></div>',
        ].join(''),
      },
    },
  };

  const snapshot = extractPlatformJobSnapshot(input);

  assert.deepEqual(snapshot, {
    schema: 'platform-job-snapshot-v1',
    platform: 'ashby',
    applicationUrl: ASHBY_URL,
    applicationHost: 'jobs.ashbyhq.com',
    externalJobId: '11111111-1111-4111-8111-111111111111',
    title: 'Frontend Product Engineer',
    company: 'Orbit Software',
    location: 'Remote, United States',
    description: 'Build accessible product interfaces. Requirements: React and TypeScript.',
  });
  assert.equal(snapshot.description.includes('SECRET_SCRIPT'), false);
  assert.equal(snapshot.description.includes('SECRET_STYLE'), false);
  assert.equal(snapshot.description.includes('<'), false);
  assertFrozenDeep(snapshot);
});

test('extracts normalized source payloads through platform-bound URL identity', () => {
  const greenhouse = extractPlatformJobSnapshot({
    applicationUrl: GREENHOUSE_URL,
    payload: {
      url: `${GREENHOUSE_URL}?gh_src=source`,
      job_title: 'Backend Platform Engineer',
      company: 'Northstar Robotics',
      location: 'Remote, United States',
      description: '<p>Build Python services.</p><script>discard()</script>',
    },
  });
  const ashby = extractPlatformJobSnapshot({
    applicationUrl: ASHBY_URL,
    payload: {
      url: `${ASHBY_URL}?utm_source=source`,
      job_title: 'Frontend Product Engineer',
      company: 'Orbit Software',
      location: 'Remote, United States',
      description: '<p>Build accessible React interfaces.</p>',
    },
  });

  assert.deepEqual(
    [greenhouse, ashby].map(({ platform, applicationHost, externalJobId, description }) => ({
      platform,
      applicationHost,
      externalJobId,
      description,
    })),
    [
      {
        platform: 'greenhouse',
        applicationHost: 'job-boards.greenhouse.io',
        externalJobId: '1234567',
        description: 'Build Python services.',
      },
      {
        platform: 'ashby',
        applicationHost: 'jobs.ashbyhq.com',
        externalJobId: '11111111-1111-4111-8111-111111111111',
        description: 'Build accessible React interfaces.',
      },
    ],
  );
  assert.throws(
    () => extractPlatformJobSnapshot({
      applicationUrl: GREENHOUSE_URL,
      payload: {
        url: 'https://job-boards.greenhouse.io/other/jobs/1234567',
        job_title: 'Wrong binding',
        company: 'Other',
        location: 'Remote',
        description: 'Wrong board.',
      },
    }),
    (error) => error.code === 'PAYLOAD_URL_MISMATCH',
  );
});
test('extracts employer snapshots only from the explicit bound host', () => {
  const snapshot = extractPlatformJobSnapshot({
    applicationUrl: `${EMPLOYER_HOSTED_URL}?source=fixture`,
    verifiedEmployerHost: EMPLOYER_HOST,
    payload: {
      url: EMPLOYER_HOSTED_URL,
      job_title: 'Backend Platform Engineer',
      company: 'Northstar Robotics',
      location: 'Remote, United States',
      description: '<p>Build Python services.</p>',
    },
  });
  assert.deepEqual(snapshot, {
    schema: 'platform-job-snapshot-v1',
    platform: 'employer_hosted',
    applicationUrl: `${EMPLOYER_HOSTED_URL}?source=fixture`,
    applicationHost: EMPLOYER_HOST,
    externalJobId: 'careers/backend-engineer/apply',
    title: 'Backend Platform Engineer',
    company: 'Northstar Robotics',
    location: 'Remote, United States',
    description: 'Build Python services.',
  });
  assertFrozenDeep(snapshot);
  assert.throws(
    () => extractPlatformJobSnapshot({
      applicationUrl: EMPLOYER_HOSTED_URL,
      payload: {
        url: EMPLOYER_HOSTED_URL,
        job_title: 'Unbound',
        company: 'Northstar Robotics',
        location: 'Remote',
        description: 'No host binding.',
      },
    }),
    (error) => error.code === 'UNSUPPORTED_APPLICATION_URL',
  );
});
test('reclassifies only bounded same-host or exact ATS destinations', () => {
  const sameHost = reclassifyApplicationRedirect({
    applicationUrl: `${EMPLOYER_HOSTED_URL}?source=initial`,
    applicationHost: EMPLOYER_HOST,
    finalUrl: `https://${EMPLOYER_HOST}/careers/backend-engineer/apply?step=2`,
  });
  assert.deepEqual(sameHost, {
    platform: 'employer_hosted',
    applicationUrl: EMPLOYER_HOSTED_URL,
    applicationHost: EMPLOYER_HOST,
    reclassified: false,
  });
  assertFrozenDeep(sameHost);
  const greenhouse = reclassifyApplicationRedirect({
    applicationUrl: EMPLOYER_HOSTED_URL,
    applicationHost: EMPLOYER_HOST,
    finalUrl: `${GREENHOUSE_ALIAS_URL}?gh_src=redirect`,
  });
  assert.deepEqual(greenhouse, {
    platform: 'greenhouse',
    applicationUrl: GREENHOUSE_ALIAS_URL,
    applicationHost: 'boards.greenhouse.io',
    reclassified: true,
  });
  const ashby = reclassifyApplicationRedirect({
    applicationUrl: EMPLOYER_HOSTED_URL,
    applicationHost: EMPLOYER_HOST,
    finalUrl: `${ASHBY_URL}?utm_source=redirect`,
  });
  assert.equal(ashby.platform, 'ashby');
  assert.equal(ashby.applicationUrl, ASHBY_URL);
  assert.equal(ashby.applicationHost, 'jobs.ashbyhq.com');
  assert.equal(ashby.reclassified, true);
  for (const finalUrl of [
    'https://evil.northstar.example/careers/backend-engineer/apply',
    'https://jobs.lever.co/northstar/backend-engineer',
    'not a URL',
  ]) {
    assert.throws(
      () => reclassifyApplicationRedirect({
        applicationUrl: EMPLOYER_HOSTED_URL,
        applicationHost: EMPLOYER_HOST,
        finalUrl,
      }),
      (error) => error.code === 'UNSUPPORTED_REDIRECT',
    );
  }
  assert.throws(
    () => reclassifyApplicationRedirect({
      applicationUrl: EMPLOYER_HOSTED_URL,
      applicationHost: '127.0.0.1',
      finalUrl: EMPLOYER_HOSTED_URL,
    }),
    (error) => error.code === 'INVALID_REDIRECT_INPUT',
  );
});


test('plans Greenhouse controls from observer snake_case fields with distinct mechanics and audit-gated final action', () => {
  const controls = [
    observerControl('gh-name', 'gh-ref-name'),
    observerControl('gh-resume', 'gh-ref-resume', { type: 'file' }),
    observerControl('gh-country', 'gh-ref-country', {
      kind: 'select',
      tag: 'select',
      type: 'select',
      role: 'combobox',
      options: [
        { label: 'United States', value: 'US' },
        { label: 'Canada', value: 'CA' },
      ],
    }),
    observerControl('gh-authorization', 'gh-ref-authorization', {
      kind: 'radio',
      type: 'radio',
      role: 'radio',
      options: [
        { label: 'Yes', value: 'yes' },
        { label: 'No', value: 'no' },
      ],
    }),
    observerControl('gh-interest', 'gh-ref-interest', {
      kind: 'textarea',
      tag: 'textarea',
      type: 'textarea',
      role: 'textbox',
    }),
    observerControl('gh-submit', 'gh-ref-submit', {
      candidate: { class: 'final_candidate', reason: 'synthetic final control' },
    }),
  ];
  const input = planInput('greenhouse', 'gh-observation-1', controls, {
    'gh-name': answer('profile', 'Avery Applicant'),
    'gh-resume': answer('resume', RESUME_UPLOAD_PATH),
    'gh-country': answer('profile', 'United States'),
    'gh-authorization': answer('memory', 'Yes'),
  });
  const before = structuredClone(input);

  const plan = planPlatformApplication(input);
  const repeated = planPlatformApplication(input);

  assert.deepEqual(input, before);
  assert.equal(plan.schema, 'deterministic-platform-plan-v1');
  assert.equal(plan.platform, 'greenhouse');
  assert.equal(plan.adapter, 'greenhouse_v1');
  assert.equal(plan.observationId, 'gh-observation-1');
  assert.deepEqual(plan.actions, [
    {
      fieldId: 'gh-name',
      operation: 'fill_text',
      mechanic: 'greenhouse_native_input',
      value: 'Avery Applicant',
      source: 'profile',
      controlReference: 'gh-ref-name',
    },
    {
      fieldId: 'gh-resume',
      operation: 'upload_file',
      mechanic: 'greenhouse_file_input',
      value: RESUME_UPLOAD_PATH,
      source: 'resume',
      controlReference: 'gh-ref-resume',
    },
    {
      fieldId: 'gh-country',
      operation: 'select_option',
      mechanic: 'greenhouse_native_select',
      value: 'US',
      source: 'profile',
      controlReference: 'gh-ref-country',
    },
    {
      fieldId: 'gh-authorization',
      operation: 'toggle',
      mechanic: 'greenhouse_native_radio',
      value: 'yes',
      source: 'memory',
      controlReference: 'gh-ref-authorization',
    },
  ]);
  assert.deepEqual(plan.unresolved, [
    { fieldId: 'gh-interest', reason: 'inference_required' },
  ]);
  assert.equal(plan.finalCandidateRef, 'gh-ref-submit');
  assert.equal(plan.actions.some(({ controlReference }) => controlReference === 'gh-ref-submit'), false);
  assert.equal(plan.actions.some(({ fieldId }) => fieldId === 'gh-interest'), false);
  assert.deepEqual(plan, repeated);
  assert.equal(JSON.stringify(plan), JSON.stringify(repeated));
  assertFrozenDeep(plan);
});

test('plans Ashby controls from observer snake_case fields with distinct mechanics and audit-gated final action', () => {
  const controls = [
    observerControl('ashby-name', 'ashby-ref-name'),
    observerControl('ashby-resume', 'ashby-ref-resume', { type: 'file' }),
    observerControl('ashby-location', 'ashby-ref-location', {
      kind: 'combobox',
      type: 'text',
      role: 'combobox',
      options: [
        { label: 'Remote', value: 'Remote' },
        { label: 'New York', value: 'New York' },
      ],
    }),
    observerControl('ashby-authorization', 'ashby-ref-authorization', {
      kind: 'radio_group',
      tag: 'div',
      type: 'radio',
      role: 'radiogroup',
      options: [
        { label: 'Yes', value: 'yes' },
        { label: 'No', value: 'no' },
      ],
    }),
    observerControl('ashby-interest', 'ashby-ref-interest', {
      kind: 'textarea',
      tag: 'textarea',
      type: 'textarea',
      role: 'textbox',
    }),
    observerControl('ashby-submit', 'ashby-ref-submit', {
      candidate: { class: 'final_candidate', reason: 'synthetic final control' },
    }),
  ];
  const input = planInput('ashby', 'ashby-observation-1', controls, {
    'ashby-name': answer('profile', 'Avery Applicant'),
    'ashby-resume': answer('resume', RESUME_UPLOAD_PATH),
    'ashby-location': answer('memory', 'Remote'),
    'ashby-authorization': answer('memory', 'Yes'),
  });

  const plan = planPlatformApplication(input);
  const repeated = planPlatformApplication(input);

  assert.equal(plan.schema, 'deterministic-platform-plan-v1');
  assert.equal(plan.platform, 'ashby');
  assert.equal(plan.adapter, 'ashby_v1');
  assert.equal(plan.observationId, 'ashby-observation-1');
  assert.deepEqual(plan.actions, [
    {
      fieldId: 'ashby-name',
      operation: 'fill_text',
      mechanic: 'ashby_native_input',
      value: 'Avery Applicant',
      source: 'profile',
      controlReference: 'ashby-ref-name',
    },
    {
      fieldId: 'ashby-resume',
      operation: 'upload_file',
      mechanic: 'ashby_file_input',
      value: RESUME_UPLOAD_PATH,
      source: 'resume',
      controlReference: 'ashby-ref-resume',
    },
    {
      fieldId: 'ashby-location',
      operation: 'select_option',
      mechanic: 'ashby_combobox_exact_option',
      value: 'Remote',
      source: 'memory',
      controlReference: 'ashby-ref-location',
    },
    {
      fieldId: 'ashby-authorization',
      operation: 'toggle',
      mechanic: 'ashby_yes_no',
      value: 'yes',
      source: 'memory',
      controlReference: 'ashby-ref-authorization',
    },
  ]);
  assert.deepEqual(plan.unresolved, [
    { fieldId: 'ashby-interest', reason: 'inference_required' },
  ]);
  assert.equal(plan.finalCandidateRef, 'ashby-ref-submit');
  assert.equal(plan.actions.some(({ controlReference }) => controlReference === 'ashby-ref-submit'), false);
  assert.equal(plan.actions.some(({ fieldId }) => fieldId === 'ashby-interest'), false);
  assert.deepEqual(plan, repeated);
  assert.equal(JSON.stringify(plan), JSON.stringify(repeated));
  assertFrozenDeep(plan);
});

test('reports unavailable controls without acting and rejects mismatched readonly values', () => {
  const unavailable = planPlatformApplication(planInput(
    'greenhouse',
    'gh-observation-unavailable',
    [observerControl('gh-disabled', 'gh-ref-disabled', {
      enabled: false,
      disabled: true,
    })],
    { 'gh-disabled': answer('profile', 'Avery Applicant') },
  ));
  assert.deepEqual(unavailable.actions, []);
  assert.deepEqual(unavailable.unresolved, [
    { fieldId: 'gh-disabled', reason: 'control_unavailable' },
  ]);

  const readonlyInput = planInput(
    'ashby',
    'ashby-observation-readonly',
    [observerControl('ashby-readonly', 'ashby-ref-readonly', {
      readonly: true,
      value: 'Observed value',
      value_present: true,
    })],
    { 'ashby-readonly': answer('profile', 'Different value') },
  );
  assert.throws(
    () => planPlatformApplication(readonlyInput),
    (error) => error.code === 'READONLY_MISMATCH',
  );
});

test('opens a closed custom combobox before exact option selection', () => {
  const plan = planPlatformApplication(planInput(
    'greenhouse',
    'gh-observation-closed-combobox',
    [observerControl('gh-team', 'gh-ref-team', {
      kind: 'aria',
      tag: 'div',
      type: null,
      role: 'combobox',
      options: [],
    })],
    { 'gh-team': answer('memory', 'Engineering') },
  ));

  assert.deepEqual(plan.actions, [
    {
      fieldId: 'gh-team',
      operation: 'open_combobox',
      mechanic: 'greenhouse_combobox_open',
      value: 'Engineering',
      source: 'memory',
      controlReference: 'gh-ref-team',
    },
  ]);
  assert.deepEqual(plan.unresolved, []);
});

test('plans custom Greenhouse choices and checkbox transitions without redundant toggles', () => {
  const controls = [
    observerControl('gh-team', 'gh-ref-team', {
      kind: 'aria',
      tag: 'div',
      type: null,
      role: 'combobox',
      options: [
        { label: 'Engineering', value: 'engineering' },
        { label: 'Operations', value: 'operations' },
      ],
    }),
    observerControl('gh-consent', 'gh-ref-consent', {
      type: 'checkbox',
      role: 'checkbox',
      label: 'Consent',
      checked: false,
      value: false,
    }),
    observerControl('gh-retained-consent', 'gh-ref-retained-consent', {
      type: 'checkbox',
      role: 'checkbox',
      label: 'Retained consent',
      checked: true,
      value: true,
      value_present: true,
    }),
  ];

  const plan = planPlatformApplication(planInput(
    'greenhouse',
    'gh-observation-custom',
    controls,
    {
      'gh-team': answer('memory', 'Engineering'),
      'gh-consent': answer('profile', 'Yes'),
      'gh-retained-consent': answer('profile', 'Yes'),
    },
  ));

  assert.deepEqual(plan.actions, [
    {
      fieldId: 'gh-team',
      operation: 'select_option',
      mechanic: 'greenhouse_combobox_exact_option',
      value: 'engineering',
      source: 'memory',
      controlReference: 'gh-ref-team',
    },
    {
      fieldId: 'gh-consent',
      operation: 'toggle',
      mechanic: 'greenhouse_native_checkbox',
      value: true,
      source: 'profile',
      controlReference: 'gh-ref-consent',
    },
  ]);
  assert.deepEqual(plan.unresolved, []);
});

test('plans employer-hosted controls and leaves unknown widgets unresolved', () => {
  const plan = planPlatformApplication(planInput(
    'employer_hosted',
    'employer-observation-1',
    [
      observerControl('employer-name', 'employer-ref-name'),
      observerControl('employer-widget', 'employer-ref-widget', {
        kind: 'widget',
        tag: 'div',
        type: null,
        role: null,
      }),
    ],
    {
      'employer-name': answer('profile', 'Avery Applicant'),
      'employer-widget': answer('memory', 'Exact value'),
    },
    {
      applicationUrl: EMPLOYER_HOSTED_URL,
      applicationHost: EMPLOYER_HOST,
      observationUrl: `${EMPLOYER_HOSTED_URL}?step=2`,
    },
  ));

  assert.equal(plan.platform, 'employer_hosted');
  assert.equal(plan.adapter, 'employer_hosted_v1');
  assert.deepEqual(plan.actions, [{
    fieldId: 'employer-name',
    operation: 'fill_text',
    mechanic: 'employer_hosted_native_input',
    value: 'Avery Applicant',
    source: 'profile',
    controlReference: 'employer-ref-name',
  }]);
  assert.deepEqual(plan.unresolved, [{
    fieldId: 'employer-widget',
    reason: 'unsupported_widget',
  }]);
  assertFrozenDeep(plan);
});

test('reclassifies an employer redirect before selecting platform mechanics', () => {
  const plan = planPlatformApplication(planInput(
    'employer_hosted',
    'redirect-observation-1',
    [observerControl('redirect-name', 'redirect-ref-name')],
    { 'redirect-name': answer('profile', 'Avery Applicant') },
    {
      applicationUrl: EMPLOYER_HOSTED_URL,
      applicationHost: EMPLOYER_HOST,
      observationUrl: GREENHOUSE_URL,
    },
  ));

  assert.equal(plan.platform, 'greenhouse');
  assert.equal(plan.adapter, 'greenhouse_v1');
  assert.equal(plan.actions[0].mechanic, 'greenhouse_native_input');
});
