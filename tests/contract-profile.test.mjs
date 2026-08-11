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
    loadRunInputs,
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
        verified_facts: {
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
        },
        user_attested_facts: {
            answers: { 'work-authorized': true },
            explanations: { 'work-authorized': 'User-authored answer.' },
        },
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
        verified_facts: {
            education: [
                { institution: 'Example University', level: 'college', gpa: 3.5, end_date: 'May 2026' },
                { institution: 'Example Secondary School', level: 'high_school', gpa: 3.9, end_date: 'June 2022' },
            ],
        },
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
            source: 'profile_verified',
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
        profile: {
            ...profile,
            verified_facts: {
                ...profile.verified_facts,
                answers: { 'High School Name': 'Exact Alias School' },
            },
        },
    }).value, 'Exact Alias School');
    for (const education of [
        [{ institution: 'Example School', level: 'graduate' }],
        [{ institution: 'Example School', gpa: '3.9' }],
        [{ institution: 'Example School', gpa: Number.POSITIVE_INFINITY }],
    ]) {
        assert.throws(() => validateProfile({
            schema: PROFILE_SCHEMA,
            verified_facts: { education },
        }), /E_PROFILE_/);
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

test('v2 tiers reuse body field validation and reject nested schemas', () => {
    assert.deepEqual(
        validateProfile({
            schema: PROFILE_SCHEMA,
            verified_facts: { contact: { name: 'Verified Name' } },
            user_attested_facts: { contact: { name: 'Attested Name' } },
        }),
        {
            schema: PROFILE_SCHEMA,
            verified_facts: { contact: { name: 'Verified Name' } },
            user_attested_facts: { contact: { name: 'Attested Name' } },
        },
    );
    for (const profile of [
        { schema: PROFILE_SCHEMA, verified_facts: { unexpected: true } },
        { schema: PROFILE_SCHEMA, user_attested_facts: { contact: { name: 42 } } },
        { schema: PROFILE_SCHEMA, verified_facts: { schema: 'nested', contact: {} } },
        { schema: PROFILE_SCHEMA, verified_facts: 'not-an-object' },
        { schema: PROFILE_SCHEMA, inferred_facts: 'not-an-object' },
        { schema: PROFILE_SCHEMA, unknowns: 'not-an-array' },
    ]) {
        assert.throws(() => validateProfile(profile), /E_PROFILE_/);
    }
});

test('strict v1 profiles are rejected', () => {
    assert.throws(() => validateProfile({ schema: 'phase1-profile-v1' }), /E_PROFILE_SCHEMA/);
    assert.throws(() => validateProfile({
        schema: 'phase1-profile-v1',
        answers: { 'work-authorized': true },
    }), /E_PROFILE_/);
});

test('secure local run checks reject a v1 profile on disk', async (t) => {
    const root = await privateFixture(t);
    const uploadPath = path.join(root, 'resume.pdf');
    const memoryDirectory = path.join(root, 'memory');
    const memoryPath = path.join(memoryDirectory, 'answers.jsonl');
    const artifactPath = path.join(root, 'artifacts');
    await fs.mkdir(memoryDirectory, { mode: 0o700 });
    await fs.mkdir(artifactPath, { mode: 0o700 });
    await fs.chmod(memoryDirectory, 0o700);
    await fs.chmod(artifactPath, 0o700);
    await privateFile(path.join(root, 'job.txt'), 'Synthetic job snapshot.');
    await privateFile(uploadPath, '%PDF-1.7\nsynthetic pdf bytes');
    const profilePath = path.join(root, 'profile.json');
    await privateFile(profilePath, `${JSON.stringify({ schema: 'phase1-profile-v1' })}\n`);
    await assert.rejects(
        validateRunContractLocal(runFor(root, { profilePath, uploadPath, memoryPath, artifactPath })),
        /E_RUN_PROFILE_SCHEMA/,
    );
    await privateFile(profilePath, `${JSON.stringify({ schema: PROFILE_SCHEMA })}\n`);
    assert.deepEqual(
        await validateRunContractLocal(runFor(root, { profilePath, uploadPath, memoryPath, artifactPath })),
        runFor(root, { profilePath, uploadPath, memoryPath, artifactPath }),
    );
});

test('inferred facts require rationale and both sha256 evidence digests', () => {
    const rationale = 'Derived from the resume and the job description.';
    const evidence = {
        source_resume_sha256: 'a'.repeat(64),
        job_description_sha256: 'b'.repeat(64),
    };
    const valid = {
        schema: PROFILE_SCHEMA,
        inferred_facts: {
            'inferred-question': { value: 'inferred', rationale, evidence },
        },
    };
    assert.deepEqual(validateProfile(valid), valid);
    for (const [entry, error] of [
        [{ value: 'x' }, /E_PROFILE_INFERENCE_SCHEMA/],
        [{ value: 'x', rationale, evidence: { ...evidence, extra: 'c'.repeat(64) } }, /E_PROFILE_INFERENCE_SCHEMA/],
        [{ value: 'x', rationale: '', evidence }, /E_PROFILE_INFERENCE_RATIONALE/],
        [{ value: 'x', rationale: 42, evidence }, /E_PROFILE_INFERENCE_RATIONALE/],
        [{ value: 'x', rationale, evidence: { ...evidence, source_resume_sha256: '' } }, /E_PROFILE_INFERENCE_EVIDENCE/],
        [{
            value: 'x',
            rationale,
            evidence: { ...evidence, job_description_sha256: 'not-a-sha256' },
        }, /E_PROFILE_INFERENCE_EVIDENCE/],
        [{
            value: 'x',
            rationale,
            evidence: { source_resume_sha256: 'a'.repeat(64) },
        }, /E_PROFILE_INFERENCE_SCHEMA/],
    ]) {
        assert.throws(
            () => validateProfile({
                schema: PROFILE_SCHEMA,
                inferred_facts: { 'inferred-question': entry },
            }),
            error,
        );
    }
});

test('duplicate unknown aliases and cross-tier alias conflicts are rejected', () => {
    assert.throws(() => validateProfile({
        schema: PROFILE_SCHEMA,
        unknowns: ['question-a', 'question-a'],
    }), /E_PROFILE_UNKNOWN_DUPLICATE/);
    assert.throws(() => validateProfile({
        schema: PROFILE_SCHEMA,
        verified_facts: { answers: { 'question-a': true } },
        user_attested_facts: { answers: { 'question-a': 'attested' } },
    }), /E_PROFILE_ALIAS_CONFLICT/);
    assert.throws(() => validateProfile({
        schema: PROFILE_SCHEMA,
        verified_facts: { answers: { 'question-a': true } },
        inferred_facts: {
            'question-a': {
                value: 'inferred',
                rationale: 'rationale',
                evidence: {
                    source_resume_sha256: 'a'.repeat(64),
                    job_description_sha256: 'b'.repeat(64),
                },
            },
        },
    }), /E_PROFILE_ALIAS_CONFLICT/);
    assert.throws(() => validateProfile({
        schema: PROFILE_SCHEMA,
        user_attested_facts: { answers: { 'question-a': 'attested' } },
        inferred_facts: {
            'question-a': {
                value: 'inferred',
                rationale: 'rationale',
                evidence: {
                    source_resume_sha256: 'a'.repeat(64),
                    job_description_sha256: 'b'.repeat(64),
                },
            },
        },
    }), /E_PROFILE_ALIAS_CONFLICT/);
});

test('memory outranks every lower source and aliases are exact', () => {
    const result = resolveAnswer({
        alias: 'Question-ID',
        memory: [testAnswerRecord('Question-ID', 'memory', '2026-01-01T00:00:00.000Z')],
        profile: { user_attested_facts: { answers: { 'Question-ID': 'profile' } } },
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
    assert.equal(
        resolveAnswer({
            alias: 'question-id',
            profile: { user_attested_facts: { answers: { 'Question-ID': true } } },
        }).missing,
        true,
    );
    const structuredProfile = {
        schema: PROFILE_SCHEMA,
        verified_facts: {
            address: { country: 'Structured Country' },
            location_preferences: { onsite: true },
            relocation: { willing: true },
            links: [{
                label: 'Portfolio',
                kind: 'portfolio',
                url: 'https://example.test/work',
            }],
            answers: { 'profile.address.country': 'Conflicting Alias Country' },
        },
    };
    assert.deepEqual(
        resolveAnswer({ alias: 'profile.address.country', profile: structuredProfile }),
        {
            alias: 'profile.address.country',
            source: 'profile_verified',
            value: 'Structured Country',
            missing: false,
        },
    );
    assert.equal(
        resolveAnswer({
            alias: 'profile.address.city',
            profile: { schema: PROFILE_SCHEMA, verified_facts: { address: { city: 'Exact City' } } },
        }).value,
        'Exact City',
    );
    assert.equal(
        resolveAnswer({
            alias: 'profile.address.city.is:Exact City',
            profile: { schema: PROFILE_SCHEMA, verified_facts: { address: { city: 'Exact City' } } },
        }).value,
        true,
    );
    assert.equal(
        resolveAnswer({
            alias: 'profile.address.city.is_not:Los Angeles',
            profile: { schema: PROFILE_SCHEMA, verified_facts: { address: { city: 'Exact City' } } },
        }).value,
        true,
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
            alias: 'profile.sponsorship.not_needed',
            profile: { schema: PROFILE_SCHEMA, verified_facts: { sponsorship: { needed: false } } },
        }).value,
        true,
    );
    assert.equal(
        resolveAnswer({
            alias: 'profile.sponsorship.not_needed',
            profile: { schema: PROFILE_SCHEMA, verified_facts: { sponsorship: { needed: true } } },
        }).value,
        false,
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
                verified_facts: { answers: { 'profile.address.country': 'Fallback Country' } },
            },
        }).value,
        'Fallback Country',
    );
    assert.deepEqual(
        resolveAnswer({
            alias: 'tier-question',
            profile: {
                schema: PROFILE_SCHEMA,
                verified_facts: { answers: { 'tier-question': 'verified' } },
                user_attested_facts: { answers: { 'tier-question': 'attested' } },
            },
        }),
        { alias: 'tier-question', source: 'profile_verified', value: 'verified', missing: false },
    );
    assert.deepEqual(
        resolveAnswer({
            alias: 'tier-question',
            profile: {
                schema: PROFILE_SCHEMA,
                user_attested_facts: { answers: { 'tier-question': 'attested' } },
            },
        }),
        { alias: 'tier-question', source: 'profile_user_attested', value: 'attested', missing: false },
    );
    assert.deepEqual(
        resolveAnswer({
            alias: 'profile.address.country',
            profile: {
                schema: PROFILE_SCHEMA,
                verified_facts: { address: { country: 'Verified Country' } },
                user_attested_facts: { answers: { 'profile.address.country': 'Attested Country' } },
            },
        }),
        {
            alias: 'profile.address.country',
            source: 'profile_verified',
            value: 'Verified Country',
            missing: false,
        },
    );
    assert.deepEqual(
        resolveAnswer({
            alias: 'profile.address.country',
            profile: {
                schema: PROFILE_SCHEMA,
                user_attested_facts: { address: { country: 'Attested Country' } },
            },
        }),
        {
            alias: 'profile.address.country',
            source: 'profile_user_attested',
            value: 'Attested Country',
            missing: false,
        },
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
            profile: { user_attested_facts: { answers: { 'profile-question': 'profile' } } },
            source: 'profile_user_attested',
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
        verified_facts: {
            contact: {
                name: 'Ada Lovelace',
                preferred_name: 'Ada',
                first_name: 'Ada',
                last_name: 'Lovelace',
                email: 'ada@example.test',
                phone: '555-0100',
            },
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
            source: 'profile_verified',
            value,
            missing: false,
        });
    }
    assert.equal(resolveAnswer({ alias: 'full name', profile }).missing, true);
});

test('profile aliases never redirect non-profile answer sources', () => {
    const alias = 'Portfolio URL';
    const profileAlias = 'profile.links.portfolio';
    const profile = {
        schema: PROFILE_SCHEMA,
        verified_facts: {
            links: [{ kind: 'portfolio', url: 'https://example.test/portfolio' }],
        },
    };
    assert.deepEqual(resolveAnswer({ alias, profileAlias, profile }), {
        alias,
        source: 'profile_verified',
        value: 'https://example.test/portfolio',
        missing: false,
    });
    const githubAlias = 'GitHub URL';
    assert.deepEqual(resolveAnswer({
        alias: githubAlias,
        profileAlias: 'profile.links.github',
        profile: {
            schema: PROFILE_SCHEMA,
            verified_facts: {
                links: [{ kind: 'github', url: 'https://github.example.test/ada' }],
            },
        },
    }), {
        alias: githubAlias,
        source: 'profile_verified',
        value: 'https://github.example.test/ada',
        missing: false,
    });
    assert.deepEqual(resolveAnswer({
        alias,
        profileAlias,
        resume: { answers: { [alias]: 'observed-alias', [profileAlias]: 'synthetic-alias' } },
    }), {
        alias,
        source: 'resume',
        value: 'observed-alias',
        missing: false,
    });
    assert.equal(resolveAnswer({
        alias,
        profileAlias,
        memory: [testAnswerRecord(profileAlias, 'synthetic-alias', '2026-01-01T00:00:00.000Z')],

        resume: { answers: { [profileAlias]: 'synthetic-alias' } },
        user: { [profileAlias]: 'synthetic-alias' },
    }).missing, true);
});
test('canonical college institution resolves only one exact profile credential', () => {
    const alias = 'Current Company or University';
    const profileAlias = 'profile.education.college.institution';
    const profile = {
        schema: PROFILE_SCHEMA,
        verified_facts: {
            education: [{
                institution: 'Example University',
                level: 'college',
            }],
        },
    };
    assert.deepEqual(resolveAnswer({ alias, profileAlias, profile }), {
        alias,
        source: 'profile_verified',
        value: 'Example University',
        missing: false,
    });
    assert.equal(resolveAnswer({
        alias,
        profileAlias,
        profile: {
            ...profile,
            verified_facts: {
                ...profile.verified_facts,
                education: [
                    ...profile.verified_facts.education,
                    { institution: 'Second University', level: 'college' },
                ],
            },
        },
    }).missing, true);
});


test('resolveAnswer maps novel status questions to canonical user-backed profile facts', () => {
    const profile = {
        schema: PROFILE_SCHEMA,
        verified_facts: {
            work_authorization: {
                authorized: true,
                countries: ['US'],
                status: 'US citizen',
            },
            sponsorship: { needed: false },
        },
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
            source: 'profile_verified',
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
    assert.deepEqual(ANSWER_SOURCES, [
        'memory',
        'profile_verified',
        'profile_user_attested',
        'resume',
        'agent_inference',
        'user',
    ]);
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

test('profile answer and explanation lookup preserve exact aliases and tier order', () => {
    const profile = validateProfile({
        schema: PROFILE_SCHEMA,
        verified_facts: {
            answers: { 'verified-alias': true },
            explanations: { 'verified-alias': 'Verified explanation.' },
        },
        user_attested_facts: {
            answers: { 'attested-alias': false },
            explanations: { 'attested-alias': 'Attested explanation.' },
        },
    });
    assert.deepEqual(profileAnswer(profile, 'verified-alias'), {
        found: true, source: 'profile_verified', value: true,
    });
    assert.deepEqual(profileExplanation(profile, 'verified-alias'), {
        found: true, source: 'profile_verified', value: 'Verified explanation.',
    });
    assert.deepEqual(profileAnswer(profile, 'attested-alias'), {
        found: true, source: 'profile_user_attested', value: false,
    });
    assert.deepEqual(profileExplanation(profile, 'attested-alias'), {
        found: true, source: 'profile_user_attested', value: 'Attested explanation.',
    });
    assert.deepEqual(profileAnswer(profile, 'EXACT-ALIAS'), { found: false, unknown: false });
    const attestedOnly = validateProfile({
        schema: PROFILE_SCHEMA,
        user_attested_facts: {
            answers: { 'attested-alias': false },
            explanations: { 'attested-alias': 'Attested explanation.' },
        },
    });
    assert.deepEqual(profileAnswer(attestedOnly, 'attested-alias'), {
        found: true, source: 'profile_user_attested', value: false,
    });
    assert.deepEqual(profileExplanation(attestedOnly, 'attested-alias'), {
        found: true, source: 'profile_user_attested', value: 'Attested explanation.',
    });
    const cloned = validateProfile(profile);
    cloned.verified_facts.answers['verified-alias'] = 'tampered';
    assert.deepEqual(profileAnswer(profile, 'verified-alias'), {
        found: true, source: 'profile_verified', value: true,
    });
});

test('stored profile inferred facts resolve after resume and emit canonical agent inference digests', () => {
    const rationale = 'Grounded in the verified source resume and job description.';
    const evidence = {
        source_resume_sha256: 'a'.repeat(64),
        job_description_sha256: 'b'.repeat(64),
    };
    const profile = {
        schema: PROFILE_SCHEMA,
        inferred_facts: {
            'inferred-question': { value: 'stored inference', rationale, evidence },
        },
    };
    assert.deepEqual(
        resolveAnswer({ alias: 'inferred-question', profile }),
        {
            alias: 'inferred-question',
            source: 'agent_inference',
            value: 'stored inference',
            missing: false,
            inference_rationale_digest: crypto.createHash('sha256').update(rationale, 'utf8').digest('hex'),
            inference_evidence_digests: {
                resume_sha256: 'a'.repeat(64),
                job_description_sha256: 'b'.repeat(64),
            },
        },
    );
    assert.deepEqual(
        resolveAnswer({ alias: 'inferred-question', profile, allowInference: false }),
        { alias: 'inferred-question', source: 'user', value: undefined, missing: true },
    );
    assert.equal(
        resolveAnswer({
            alias: 'inferred-question',
            resume: { 'inferred-question': 'resume wins' },
            profile,
        }).source,
        'resume',
    );
    assert.equal(
        resolveAnswer({
            alias: 'inferred-question',
            memory: [testAnswerRecord('inferred-question', 'memory wins', '2026-01-01T00:00:00.000Z')],
            resume: { 'inferred-question': 'resume' },
            profile,
        }).source,
        'memory',
    );
    const tampered = structuredClone(profile);
    tampered.inferred_facts['inferred-question'].rationale = '';
    assert.throws(
        () => resolveAnswer({ alias: 'inferred-question', profile: tampered }),
        /E_PROFILE_INFERENCE_RATIONALE/,
    );
});

test('loadRunInputs binds every stored inferred fact to the active source resume and job description', async (t) => {
    const root = await privateFixture(t);
    const jobDescription = 'Synthetic job snapshot.';
    const sourceResume = 'Synthetic source resume.';
    const jobSha256 = crypto.createHash('sha256').update(jobDescription).digest('hex');
    const sourceSha256 = crypto.createHash('sha256').update(sourceResume).digest('hex');
    const jobPath = path.join(root, 'job.txt');
    const sourceResumePath = path.join(root, 'source-resume.txt');
    const uploadPath = path.join(root, 'resume.pdf');
    const memoryDirectory = path.join(root, 'memory');
    const memoryPath = path.join(memoryDirectory, 'answers.jsonl');
    const artifactPath = path.join(root, 'artifacts');
    await fs.mkdir(memoryDirectory, { mode: 0o700 });
    await fs.mkdir(artifactPath, { mode: 0o700 });
    await fs.chmod(memoryDirectory, 0o700);
    await fs.chmod(artifactPath, 0o700);
    await privateFile(jobPath, jobDescription);
    await privateFile(sourceResumePath, sourceResume);
    await privateFile(uploadPath, '%PDF-1.7\nsynthetic pdf bytes');

    const profile = {
        schema: PROFILE_SCHEMA,
        inferred_facts: {
            'inferred-question': {
                value: 'stored inference',
                rationale: 'Grounded in the active source resume and job description.',
                evidence: {
                    source_resume_sha256: sourceSha256,
                    job_description_sha256: jobSha256,
                },
            },
        },
    };
    const profilePath = path.join(root, 'profile.json');
    await privateFile(profilePath, `${JSON.stringify(profile)}\n`);

    const inputs = await loadRunInputs(runFor(root, {
        profilePath,
        sourceResumePath,
        uploadPath,
        memoryPath,
        artifactPath,
    }));
    assert.equal(inputs.sourceResumeIdentity.sha256, sourceSha256);
    assert.equal(inputs.jobDescriptionIdentity.sha256, jobSha256);
    assert.equal(
        inputs.profile.inferred_facts['inferred-question'].evidence.source_resume_sha256,
        sourceSha256,
    );

    await assert.rejects(
        loadRunInputs(runFor(root, {
            profilePath,
            uploadPath,
            memoryPath,
            artifactPath,
        })),
        (error) => error.code === 'E_RUN_INFERENCE_UNBOUND',
    );

    const resumeMismatched = structuredClone(profile);
    resumeMismatched.inferred_facts['inferred-question'].evidence = {
        source_resume_sha256: 'c'.repeat(64),
        job_description_sha256: jobSha256,
    };
    const resumeMismatchedPath = path.join(root, 'profile-resume-mismatch.json');
    await privateFile(resumeMismatchedPath, `${JSON.stringify(resumeMismatched)}\n`);
    await assert.rejects(
        loadRunInputs(runFor(root, {
            profilePath: resumeMismatchedPath,
            sourceResumePath,
            uploadPath,
            memoryPath,
            artifactPath,
        })),
        (error) => error.code === 'E_RUN_INFERENCE_EVIDENCE_MISMATCH',
    );

    const jobMismatched = structuredClone(profile);
    jobMismatched.inferred_facts['inferred-question'].evidence = {
        source_resume_sha256: sourceSha256,
        job_description_sha256: 'd'.repeat(64),
    };
    const jobMismatchedPath = path.join(root, 'profile-job-mismatch.json');
    await privateFile(jobMismatchedPath, `${JSON.stringify(jobMismatched)}\n`);
    await assert.rejects(
        loadRunInputs(runFor(root, {
            profilePath: jobMismatchedPath,
            sourceResumePath,
            uploadPath,
            memoryPath,
            artifactPath,
        })),
        (error) => error.code === 'E_RUN_INFERENCE_EVIDENCE_MISMATCH',
    );
});

test('resolveAnswer inference permission gates both stored and runtime inference', () => {
    const rationale = 'A grounded inference.';
    const storedProfile = {
        schema: PROFILE_SCHEMA,
        inferred_facts: {
            stored: {
                value: 'stored',
                rationale,
                evidence: {
                    source_resume_sha256: 'a'.repeat(64),
                    job_description_sha256: 'b'.repeat(64),
                },
            },
        },
    };
    const runtime = {
        runtime: {
            value: 'runtime',
            rationale,
            evidence: {
                resume_sha256: 'a'.repeat(64),
                job_description_sha256: 'b'.repeat(64),
            },
        },
    };
    assert.equal(resolveAnswer({ alias: 'stored', profile: storedProfile }).source, 'agent_inference');
    assert.equal(
        resolveAnswer({ alias: 'stored', profile: storedProfile, allowInference: false }).missing,
        true,
    );
    assert.equal(resolveAnswer({ alias: 'runtime', agent_inference: runtime }).source, 'agent_inference');
    assert.equal(
        resolveAnswer({ alias: 'runtime', agent_inference: runtime, allowInference: false }).missing,
        true,
    );
    assert.equal(
        resolveAnswer({
            alias: 'runtime',
            agent_inference: runtime,
            allowInference: false,
            user: { runtime: 'explicit' },
        }).source,
        'user',
    );
    assert.equal(
        resolveAnswer({ alias: 'runtime', agent_inference: runtime, user: { runtime: 'explicit' } }).source,
        'agent_inference',
    );
});

test('explicit unknowns block resume and inference but permit memory and explicit user', () => {
    const profile = {
        schema: PROFILE_SCHEMA,
        unknowns: ['unknown-question'],
        verified_facts: { answers: { 'unknown-question': 'verified' } },
        inferred_facts: {
            'unknown-question': {
                value: 'inferred',
                rationale: 'rationale',
                evidence: {
                    source_resume_sha256: 'a'.repeat(64),
                    job_description_sha256: 'b'.repeat(64),
                },
            },
        },
    };
    const runtimeInference = {
        'unknown-question': {
            value: 'runtime inference',
            rationale: 'rationale',
            evidence: {
                resume_sha256: 'a'.repeat(64),
                job_description_sha256: 'b'.repeat(64),
            },
        },
    };
    assert.deepEqual(
        resolveAnswer({
            alias: 'unknown-question',
            profile,
            resume: { 'unknown-question': 'resume' },
            agent_inference: runtimeInference,
        }),
        { alias: 'unknown-question', source: 'user', value: undefined, missing: true },
    );
    assert.equal(
        resolveAnswer({
            alias: 'unknown-question',
            memory: [testAnswerRecord('unknown-question', 'memory', '2026-01-01T00:00:00.000Z')],
            profile,
            resume: { 'unknown-question': 'resume' },
            agent_inference: runtimeInference,
        }).source,
        'memory',
    );
    assert.deepEqual(
        resolveAnswer({
            alias: 'unknown-question',
            profile,
            resume: { 'unknown-question': 'resume' },
            agent_inference: runtimeInference,
            user: { 'unknown-question': 'explicit user' },
        }),
        { alias: 'unknown-question', source: 'user', value: 'explicit user', missing: false },
    );
    assert.equal(
        resolveAnswer({
            alias: 'known-question',
            profile,
            resume: { 'known-question': 'resume' },
            agent_inference: runtimeInference,
        }).source,
        'resume',
    );
});

test('provenance answer sources resolve in exact precedence order', () => {
    const entry = {
        value: 'runtime inference',
        rationale: 'rationale',
        evidence: {
            resume_sha256: 'a'.repeat(64),
            job_description_sha256: 'b'.repeat(64),
        },
    };
    const sources = [
        ['memory', {
            memory: [testAnswerRecord('provenance', 'memory', '2026-01-01T00:00:00.000Z')],
        }],
        ['profile_verified', {
            profile: {
                schema: PROFILE_SCHEMA,
                verified_facts: { answers: { provenance: 'verified' } },
            },
        }],
        ['profile_user_attested', {
            profile: {
                schema: PROFILE_SCHEMA,
                user_attested_facts: { answers: { provenance: 'attested' } },
            },
        }],
        ['resume', { resume: { provenance: 'resume' } }],
        ['agent_inference', { agent_inference: { provenance: entry } }],
        ['user', { user: { provenance: 'explicit user' } }],
    ];
    for (let index = 0; index < sources.length; index += 1) {
        const [source, options] = sources[index];
        const overlap = Object.fromEntries(
            sources.slice(0, index).map(([otherSource]) => [otherSource, undefined]),
        );
        assert.deepEqual(
            resolveAnswer({ alias: 'provenance', ...overlap, ...options }),
            {
                alias: 'provenance',
                source,
                value: { memory: 'memory', profile_verified: 'verified', profile_user_attested: 'attested', resume: 'resume', agent_inference: 'runtime inference', user: 'explicit user' }[source],
                missing: false,
                ...(source === 'agent_inference'
                    ? {
                        inference_rationale_digest: crypto.createHash('sha256').update('rationale', 'utf8').digest('hex'),
                        inference_evidence_digests: entry.evidence,
                    }
                    : {}),
            },
        );
    }
});

