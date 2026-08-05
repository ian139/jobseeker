import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
    ANSWER_SOURCES,
    ANSWER_SCHEMA,
    LEGACY_ANSWER_SCHEMA,
    MAX_ANSWER_RECORD_BYTES,
    RUN_SCHEMA,
    appendAnswerRecord,
    approvalContextSha256,
    canonicalJson,
    createAnswerRecord,
    loadAnswerMemory,
    loadAnswerMemoryInventory,
    readRegularFile,
    resolveAnswer,
    validateAnswerRecord,
    validateRunContract,
    validateRunContractLocal,
} from '../src/phase1/contract.mjs';
import {
    PROFILE_SCHEMA,
    loadProfile,
    profileAnswer,
    profileExplanation,
    validateProfile,
} from '../src/phase1/profile.mjs';

async function privateFixture(t) {
    const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'phase1-contract-'));
    await fs.chmod(directory, 0o700);
    t.after(async () => {
        await fs.rm(directory, { recursive: true, force: true });
    });
    return directory;
}

async function privateFile(filePath, contents, mode = 0o600) {
    await fs.writeFile(filePath, contents, { encoding: 'utf8', mode });
    await fs.chmod(filePath, mode);
}

const TEST_RUN_CONTRACT_SHA256 = 'a'.repeat(64);

function testApprovalContext(alias) {
    return {
        run_contract_sha256: TEST_RUN_CONTRACT_SHA256,
        observation_id: 'test-observation',
        field_id: 'test-field',
        alias,
    };
}

function testAnswerRecord(alias, value, approved_at = '2026-01-01T00:00:00.000Z') {
    const approval_context = testApprovalContext(alias);
    return createAnswerRecord({
        alias,
        value,
        approved_at,
        approval_context,
        approval_context_sha256: approvalContextSha256(approval_context),
    });
}

function minimalProfile() {
    return {
        schema: PROFILE_SCHEMA,
        contact: { name: 'Example Applicant', email: 'applicant@example.invalid' },
        address: { city: 'Example City', country: 'Example Country' },
        links: [{ label: 'Portfolio', url: 'https://example.invalid/portfolio' }],
        education: [{ institution: 'Example University' }],
        employment: [{ employer: 'Example Employer', title: 'Example Role' }],
        skills: ['JavaScript'],
        availability: { currently_available: true },
        location_preferences: { remote: true },
        relocation: { willing: false },
        compensation: { currency: 'USD', negotiable: true },
        work_authorization: { authorized: true },
        sponsorship: { needed: false },
        demographics: { prefer_not_to_say: true },
        answers: { 'work-authorized': true },
        explanations: { 'work-authorized': 'User-authored answer.' },
    };
}

function runFor(root, { profilePath, sourceResumePath, uploadPath, memoryPath, artifactPath }) {
    return {
        schema: RUN_SCHEMA,
        application_url: 'https://example.invalid/jobs/1',
        job_description_path: path.join(root, 'job.txt'),
        ...(profilePath === undefined ? {} : { applicant_profile_path: profilePath }),
        ...(sourceResumePath === undefined ? {} : { source_resume_path: sourceResumePath }),
        resume_upload_path: uploadPath,
        answer_memory_path: memoryPath,
        run_artifact_dir: artifactPath,
        browser_mode: 'headed',
        observer: 'playwright_dom_v1',
        action_driver: 'omp_browser',
        submit_policy: 'omp_agent',
    };
}

test('valid run, profile, and append-only memory round-trip', async (t) => {
    const root = await privateFixture(t);
    const profilePath = path.join(root, 'profile.json');
    const sourceResumePath = path.join(root, 'source-resume.txt');
    const uploadPath = path.join(root, 'resume.pdf');
    const memoryDirectory = path.join(root, 'memory');
    const memoryPath = path.join(memoryDirectory, 'answers.jsonl');
    const artifactPath = path.join(root, 'artifacts');
    await fs.mkdir(memoryDirectory, { mode: 0o700 });
    await fs.mkdir(artifactPath, { mode: 0o700 });
    await fs.chmod(memoryDirectory, 0o700);
    await fs.chmod(artifactPath, 0o700);
    await privateFile(path.join(root, 'job.txt'), 'Synthetic job snapshot.');
    await privateFile(profilePath, `${JSON.stringify(minimalProfile())}\n`);
    await privateFile(sourceResumePath, 'Synthetic source resume.');
    await privateFile(uploadPath, '%PDF-1.7\nsynthetic pdf bytes');

    const profile = validateProfile(minimalProfile());
    assert.deepEqual(profile, minimalProfile());
    assert.deepEqual(await loadProfile(profilePath), minimalProfile());

    const run = runFor(root, {
        profilePath,
        sourceResumePath,
        uploadPath,
        memoryPath,
        artifactPath,
    });
    assert.deepEqual(validateRunContract(run), run);
    assert.deepEqual(await validateRunContractLocal(run), run);

    const approval_context = testApprovalContext('work-authorized');
    const record = createAnswerRecord({
        alias: 'work-authorized',
        value: true,
        approved_at: '2026-01-01T00:00:00.000Z',
        approval_context,
        approval_context_sha256: approvalContextSha256(approval_context),
    });
    assert.deepEqual(record, {
        schema: ANSWER_SCHEMA,
        alias: 'work-authorized',
        value: true,
        source: 'user',
        approved_at: '2026-01-01T00:00:00.000Z',
        approval_context,
        approval_context_sha256: approvalContextSha256(approval_context),
    });
    await appendAnswerRecord(memoryPath, record);
    const loaded = await loadAnswerMemory(memoryPath);
    assert.deepEqual(loaded, [record]);
    assert.equal(Object.isFrozen(loaded), true);
    assert.equal(Object.isFrozen(loaded[0]), true);
    assert.equal((await fs.readFile(memoryPath, 'utf8')), `${canonicalJson(record)}\n`);
});

test('legacy answer bytes stay quarantined and v2 is the only active memory', async (t) => {
    const root = await privateFixture(t);
    const memoryDirectory = path.join(root, 'memory');
    await fs.mkdir(memoryDirectory, { mode: 0o700 });
    await fs.chmod(memoryDirectory, 0o700);
    const memoryPath = path.join(memoryDirectory, 'answers.jsonl');
    const legacy = {
        schema: LEGACY_ANSWER_SCHEMA,
        alias: 'legacy-question',
        value: 'quarantined-secret',
        source: 'user',
        approved_at: '2026-01-01T00:00:00.000Z',
    };
    assert.throws(() => validateAnswerRecord(legacy), /E_SCHEMA/);
    const legacyBytes = `${canonicalJson(legacy)}\n`;
    await privateFile(memoryPath, legacyBytes);

    const inventory = await loadAnswerMemoryInventory(memoryPath);
    assert.deepEqual(inventory.active, []);
    assert.equal(inventory.quarantined.length, 1);
    assert.deepEqual(inventory.quarantined[0], {
        line: 1,
        schema: LEGACY_ANSWER_SCHEMA,
        alias: 'legacy-question',
        source: 'user',
        approved_at: '2026-01-01T00:00:00.000Z',
        reason: 'legacy_schema',
    });
    assert.equal(Object.hasOwn(inventory.quarantined[0], 'value'), false);
    assert.doesNotMatch(JSON.stringify(inventory.quarantined[0]), /quarantined-secret/);
    assert.equal(Object.isFrozen(inventory), true);
    assert.equal(Object.hasOwn(inventory.quarantined[0], 'byte_length'), false);
    assert.equal(Object.hasOwn(inventory.quarantined[0], 'record_sha256'), false);
    assert.equal(Object.isFrozen(inventory.active), true);
    assert.equal(Object.isFrozen(inventory.quarantined), true);
    assert.equal(Object.isFrozen(inventory.quarantined[0]), true);

    const beforeLoad = await fs.readFile(memoryPath, 'utf8');
    assert.equal(beforeLoad, legacyBytes);
    assert.deepEqual(await loadAnswerMemory(memoryPath), []);
    assert.equal(await fs.readFile(memoryPath, 'utf8'), beforeLoad);
    assert.deepEqual(
        resolveAnswer({
            alias: 'legacy-question',
            memory: [legacy],
            user: { answers: { 'legacy-question': 'user fallback' } },
        }),
        {
            alias: 'legacy-question',
            source: 'user',
            value: 'user fallback',
            missing: false,
        },
    );

    const active = testAnswerRecord('legacy-question', 'active-answer', '2026-01-02T00:00:00.000Z');
    await appendAnswerRecord(memoryPath, active);
    const afterAppend = await fs.readFile(memoryPath, 'utf8');
    assert.ok(afterAppend.startsWith(legacyBytes));
    assert.deepEqual(await loadAnswerMemory(memoryPath), [active]);
    const finalInventory = await loadAnswerMemoryInventory(memoryPath);
    assert.deepEqual(finalInventory.active, [active]);
    assert.equal(finalInventory.quarantined.length, 1);
});

test('profile values remain optional until a field is asked', () => {
    assert.deepEqual(validateProfile({ schema: PROFILE_SCHEMA }), { schema: PROFILE_SCHEMA });
});

test('education levels, GPAs, and graduation dates validate and resolve without guessing', () => {
    const profile = {
        schema: PROFILE_SCHEMA,
        education: [
            { institution: 'Example University', level: 'college', gpa: 3.5, end_date: 'May 2026' },
            { institution: 'Example Secondary School', level: 'high_school', gpa: 3.9, end_date: 'June 2022' },
        ],
    };
    assert.deepEqual(validateProfile(profile), profile);
    for (const [alias, value] of new Map([
        ['High School Name', 'Example Secondary School'],
        ['What high school did you attend?', 'Example Secondary School'],
        ['High school GPA', 3.9],
        ['Secondary school grade-point average', 3.9],
        ['College GPA', 3.5],
        ['Undergraduate grade point average', 3.5],
        ['Anticipated graduation date?', 'May 2026'],
        ['Graduation Date', 'May 2026'],
        ['College graduation date', 'May 2026'],
        ['High school graduation date', 'June 2022'],
    ])) {
        assert.deepEqual(resolveAnswer({ alias, profile }), {
            alias,
            source: 'profile',
            value,
            missing: false,
        });
    }
    assert.deepEqual(resolveAnswer({ alias: 'GPA', profile }), {
        alias: 'GPA',
        source: 'user',
        value: undefined,
        missing: true,
    });
    assert.equal(resolveAnswer({
        alias: 'High School Name',
        profile: { ...profile, answers: { 'High School Name': 'Exact Alias School' } },
    }).value, 'Exact Alias School');
    for (const education of [
        [{ institution: 'Example School', level: 'graduate' }],
        [{ institution: 'Example School', gpa: '3.9' }],
        [{ institution: 'Example School', gpa: Number.POSITIVE_INFINITY }],
    ]) {
        assert.throws(() => validateProfile({ schema: PROFILE_SCHEMA, education }), /E_PROFILE_/);
    }
});

test('unknown keys, fixed enum changes, and missing evidence fail closed', () => {
    assert.throws(() => validateProfile({ schema: PROFILE_SCHEMA, unexpected: true }), /E_PROFILE_UNKNOWN_KEY/);
    assert.throws(() => validateRunContract({
        schema: RUN_SCHEMA,
        application_url: 'https://example.invalid/jobs/1',
        job_description_path: 'job.txt',
        applicant_profile_path: 'profile.json',
        resume_upload_path: 'resume.pdf',
        answer_memory_path: 'answers.jsonl',
        run_artifact_dir: 'artifacts',
        browser_mode: 'headless',
        observer: 'playwright_dom_v1',
        action_driver: 'omp_browser',
        submit_policy: 'omp_agent',
    }), /E_RUN_BROWSER_MODE/);
    assert.throws(() => validateRunContract({
        schema: RUN_SCHEMA,
        application_url: 'https://example.invalid/jobs/1',
        job_description_path: 'job.txt',
        resume_upload_path: 'resume.txt',
        answer_memory_path: 'answers.jsonl',
        run_artifact_dir: 'artifacts',
        browser_mode: 'headed',
        observer: 'playwright_dom_v1',
        action_driver: 'omp_browser',
        submit_policy: 'omp_agent',
    }), /E_RUN_EVIDENCE_REQUIRED/);
});

test('memory outranks every lower source and aliases are exact', () => {
    const result = resolveAnswer({
        alias: 'Question-ID',
        memory: [testAnswerRecord('Question-ID', 'memory', '2026-01-01T00:00:00.000Z')],
        profile: { answers: { 'Question-ID': 'profile' } },
        resume: { 'Question-ID': 'resume' },
        jobWording: { 'Question-ID': 'job' },
        agent_inference: {
            'Question-ID': {
                value: 'inference',
                rationale: 'A grounded inference for this exact question.',
                evidence: {
                    resume_sha256: 'a'.repeat(64),
                    job_description_sha256: 'b'.repeat(64),
                },
            },
        },
        user: { 'Question-ID': 'user' },
    });
    assert.deepEqual(result, { alias: 'Question-ID', source: 'memory', value: 'memory', missing: false });
    assert.throws(
        () => resolveAnswer({
            alias: 'Question-ID',
            memory: { 'Question-ID': 'unbound legacy value' },
        }),
        (error) => error.code === 'E_ANSWER_MEMORY_SCHEMA',
    );
    assert.equal(resolveAnswer({ alias: 'question-id', profile: { answers: { 'Question-ID': true } } }).missing, true);
    const structuredProfile = {
        schema: PROFILE_SCHEMA,
        address: { country: 'Structured Country' },
        location_preferences: { onsite: true },
        relocation: { willing: true },
        links: [{
            label: 'Portfolio',
            kind: 'portfolio',
            url: 'https://example.test/work',
        }],
        answers: { 'profile.address.country': 'Conflicting Alias Country' },
    };
    assert.deepEqual(
        resolveAnswer({ alias: 'profile.address.country', profile: structuredProfile }),
        {
            alias: 'profile.address.country',
            source: 'profile',
            value: 'Structured Country',
            missing: false,
        },
    );
    assert.equal(
        resolveAnswer({
            alias: 'profile.location_preferences.onsite',
            profile: structuredProfile,
        }).value,
        true,
    );
    assert.equal(
        resolveAnswer({
            alias: 'profile.relocation.willing',
            profile: structuredProfile,
        }).value,
        true,
    );
    assert.equal(
        resolveAnswer({
            alias: 'Website/Portfolio',
            profile: structuredProfile,
        }).value,
        'https://example.test/work',
    );
    assert.equal(
        resolveAnswer({
            alias: 'profile.address.country',
            memory: [testAnswerRecord(
                'profile.address.country',
                'Remembered Country',
                '2026-01-01T00:00:00.000Z',
            )],
            profile: structuredProfile,
        }).value,
        'Remembered Country',
    );
    assert.equal(
        resolveAnswer({
            alias: 'profile.address.country',
            profile: {
                schema: PROFILE_SCHEMA,
                answers: { 'profile.address.country': 'Fallback Country' },
            },
        }).value,
        'Fallback Country',
    );
    for (const alias of ['constructor', 'toString', '__proto__']) {
        assert.deepEqual(
            resolveAnswer({ alias }),
            { alias, source: 'user', value: undefined, missing: true },
        );
    }
    assert.equal(resolveAnswer({ alias: 'new-question' }).source, 'user');
    assert.equal(resolveAnswer({ alias: 'new-question' }).missing, true);
});
test('exact agent inference returns digests and malformed inference fails closed', () => {
    const rationale = 'A grounded inference for this exact question.';
    const evidence = {
        resume_sha256: 'a'.repeat(64),
        job_description_sha256: 'b'.repeat(64),
    };
    const entry = {
        value: 'inferred answer',
        rationale,
        evidence,
    };
    assert.deepEqual(
        resolveAnswer({
            alias: 'Question-ID',
            agent_inference: { 'Question-ID': entry },
        }),
        {
            alias: 'Question-ID',
            source: 'agent_inference',
            value: 'inferred answer',
            missing: false,
            inference_rationale_digest: crypto.createHash('sha256').update(rationale, 'utf8').digest('hex'),
            inference_evidence_digests: evidence,
        },
    );
    assert.equal(
        resolveAnswer({
            alias: 'question-id',
            agent_inference: { 'Question-ID': entry },
        }).missing,
        true,
    );

    const malformedInference = {
        'unrelated-question': {
            value: 'ignored',
            rationale: '',
            evidence,
        },
    };
    for (const candidate of [
        {
            alias: 'memory-question',
            memory: [testAnswerRecord('memory-question', 'memory', '2026-01-01T00:00:00.000Z')],
            source: 'memory',
            value: 'memory',
        },
        {
            alias: 'profile-question',
            profile: { answers: { 'profile-question': 'profile' } },
            source: 'profile',
            value: 'profile',
        },
        {
            alias: 'resume-question',
            resume: { 'resume-question': 'resume' },
            source: 'resume',
            value: 'resume',
        },
    ]) {
        const { source, value, ...options } = candidate;
        assert.deepEqual(
            resolveAnswer({ ...options, agent_inference: malformedInference }),
            { alias: candidate.alias, source, value, missing: false },
        );
    }

    for (const [invalidEntry, error] of [
        [{ ...entry, rationale: '' }, /E_ANSWER_INFERENCE_RATIONALE/],
        [{ ...entry, rationale: 42 }, /E_ANSWER_INFERENCE_RATIONALE/],
        [{ ...entry, evidence: { ...evidence, resume_sha256: '' } }, /E_ANSWER_INFERENCE_EVIDENCE/],
        [{
            ...entry,
            evidence: { ...evidence, job_description_sha256: 'not-a-sha256-digest' },
        }, /E_ANSWER_INFERENCE_EVIDENCE/],
    ]) {
        assert.throws(
            () => resolveAnswer({
                alias: 'Question-ID',
                agent_inference: { 'Question-ID': invalidEntry },
            }),
            error,
        );
    }
});


test('resolveAnswer reads exact standard contact aliases from the profile', () => {
    const profile = {
        schema: PROFILE_SCHEMA,
        contact: {
            name: 'Ada Lovelace',
            preferred_name: 'Ada',
            first_name: 'Ada',
            last_name: 'Lovelace',
            email: 'ada@example.test',
            phone: '555-0100',
        },
    };
    for (const [alias, value] of Object.entries({
        'Full Name': 'Ada Lovelace',
        Name: 'Ada Lovelace',
        'Preferred First Name': 'Ada',
        'First Name': 'Ada',
        'Last Name': 'Lovelace',
        Email: 'ada@example.test',
        Phone: '555-0100',
    })) {
        assert.deepEqual(resolveAnswer({ alias, profile }), {
            alias,
            source: 'profile',
            value,
            missing: false,
        });
    }
    assert.equal(resolveAnswer({ alias: 'full name', profile }).missing, true);
});

test('resolveAnswer maps novel status questions to canonical user-backed profile facts', () => {
    const profile = {
        schema: PROFILE_SCHEMA,
        work_authorization: {
            authorized: true,
            countries: ['US'],
            status: 'US citizen',
        },
        sponsorship: { needed: false },
    };
    const cases = new Map([
        ['What is your citizenship status?', 'US citizen'],
        ['Are you a citizen of the United States?', true],
        ['Please provide us confirmation: are you a US citizen?', true],
        ['Are you legally permitted to work in the United States?', true],
        ['Are you now or in the future authorized to work in the United States?', true],
        ['Are you always authorized to work in the United States?', true],
        ['Which countries are you eligible to work in?', ['US']],
        ['What is your work authorization status?', 'US citizen'],
        ['Will employment visa sponsorship be required now or later?', false],
        ['Will you now or in the future require sponsorship?', false],
        ['Do you need sponsorship to obtain work authorization?', false],
        ['Can you work without employer sponsorship?', true],
        ['Work authorization status without sponsorship', true],
    ]);
    for (const [alias, value] of cases) {
        assert.deepEqual(resolveAnswer({ alias, profile }), {
            alias,
            source: 'profile',
            value,
            missing: false,
        });
    }
    for (const alias of [
        'Are you a citizen of Canada?',
        'Are you a Canadian citizen?',
        'Do you hold citizenship in Canada?',
        'Tell us: are you a citizen of Canada?',
        'Are you authorized to work in Canada?',
    ]) {
        assert.deepEqual(resolveAnswer({ alias, profile }), {
            alias,
            source: 'user',
            value: undefined,
            missing: true,
        });
    }
    const exactAlias = 'Are you legally permitted to work in the United States?';
    assert.equal(resolveAnswer({
        alias: exactAlias,
        memory: [testAnswerRecord(exactAlias, false, '2026-01-01T00:00:00.000Z')],
        profile,
    }).source, 'memory');
});

test('job-description wording cannot answer missing work authorization', () => {
    assert.deepEqual(ANSWER_SOURCES, ['memory', 'profile', 'resume', 'agent_inference', 'user']);
    assert.deepEqual(
        resolveAnswer({
            alias: 'work_authorization',
            jobWording: { work_authorization: { authorized: true } },
        }),
        { alias: 'work_authorization', source: 'user', value: undefined, missing: true },
    );
});

test('non-user answer sources and malformed or oversized JSONL fail closed', async (t) => {
    const root = await privateFixture(t);
    const memoryDirectory = path.join(root, 'memory');
    await fs.mkdir(memoryDirectory, { mode: 0o700 });
    await fs.chmod(memoryDirectory, 0o700);
    const approval_context = testApprovalContext('x');
    const memoryPath = path.join(memoryDirectory, 'answers.jsonl');
    assert.throws(() => validateAnswerRecord({
        schema: ANSWER_SCHEMA,
        alias: 'x',
        value: true,
        source: 'profile',
        approved_at: '2026-01-01T00:00:00.000Z',
        approval_context,
        approval_context_sha256: approvalContextSha256(approval_context),
    }), /E_ANSWER_SOURCE/);
    assert.throws(() => createAnswerRecord({ alias: 'x', value: true }), /E_SCHEMA_REQUIRED/);
    assert.throws(() => createAnswerRecord('x', true), /one object argument/);
    await privateFile(memoryPath, '{not-json}\n');
    await assert.rejects(loadAnswerMemory(memoryPath), /E_JSONL_MALFORMED/);
    await privateFile(memoryPath, `${'x'.repeat(MAX_ANSWER_RECORD_BYTES + 10)}\n`);
    await assert.rejects(loadAnswerMemory(memoryPath), /E_JSON_OVERSIZE/);
});

test('owner-only reads accept a real file owned by the current process', async (t) => {
    const root = await privateFixture(t);
    const filePath = path.join(root, 'owned.txt');
    await privateFile(filePath, 'owned');
    assert.equal(await readRegularFile(filePath, { ownerOnly: true }), 'owned');
});

test('owner-only reads reject a file owned by another UID', async (t) => {
    if (typeof process.geteuid !== 'function') {
        t.skip('runtime does not expose an effective UID');
        return;
    }
    const root = await privateFixture(t);
    const filePath = path.join(root, 'owned.txt');
    await privateFile(filePath, 'owned');
    const originalDescriptor = Object.getOwnPropertyDescriptor(process, 'geteuid');
    const originalGeteuid = process.geteuid;
    Object.defineProperty(process, 'geteuid', {
        ...originalDescriptor,
        value: () => originalGeteuid() + 1,
    });
    try {
        await assert.rejects(readRegularFile(filePath, { ownerOnly: true }), /E_PATH_PERMISSIONS/);
    } finally {
        Object.defineProperty(process, 'geteuid', originalDescriptor);
    }
});

test('symlink and unsafe permissions are rejected by local memory helpers', async (t) => {
    const root = await privateFixture(t);
    const privateDirectory = path.join(root, 'private');
    await fs.mkdir(privateDirectory, { mode: 0o700 });
    await fs.chmod(privateDirectory, 0o700);
    const target = path.join(privateDirectory, 'target.jsonl');
    const link = path.join(privateDirectory, 'link.jsonl');
    const record = testAnswerRecord('x', false, '2026-01-01T00:00:00.000Z');
    await privateFile(target, `${canonicalJson(record)}\n`);
    await fs.symlink(target, link);
    await assert.rejects(loadAnswerMemory(link), /E_PATH_SYMLINK/);

    const unsafeDirectory = path.join(root, 'unsafe');
    await fs.mkdir(unsafeDirectory, { mode: 0o755 });
    await fs.chmod(unsafeDirectory, 0o755);
    await assert.rejects(appendAnswerRecord(path.join(unsafeDirectory, 'answers.jsonl'), record), /E_PATH_PERMISSIONS/);

    await fs.chmod(privateDirectory, 0o700);
    await fs.chmod(target, 0o644);
    await assert.rejects(appendAnswerRecord(target, record), /E_PATH_PERMISSIONS/);
});

test('profile answer and explanation lookup preserve exact aliases', () => {
    const profile = validateProfile({
        schema: PROFILE_SCHEMA,
        answers: { 'exact-alias': false },
        explanations: { 'exact-alias': 'Synthetic explanation.' },
    });
    assert.deepEqual(profileAnswer(profile, 'exact-alias'), { found: true, value: false });
    assert.deepEqual(profileExplanation(profile, 'exact-alias'), { found: true, value: 'Synthetic explanation.' });
    assert.deepEqual(profileAnswer(profile, 'EXACT-ALIAS'), { found: false });
});

