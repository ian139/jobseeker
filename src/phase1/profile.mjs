import {
    MAX_ALIAS_LENGTH,
    MAX_PROFILE_BYTES,
    PROFILE_INFERENCE_EVIDENCE_KEYS,
    PROFILE_SCHEMA,
    ValidationError,
    canonicalJson,
    isPlainObject,
    readJsonFileSecure,
    validateInferenceEntry,
} from './contract.mjs';

export { PROFILE_SCHEMA } from './contract.mjs';

export const PROFILE_KEYS = Object.freeze([
    'schema',
    'verified_facts',
    'user_attested_facts',
    'inferred_facts',
    'unknowns',
]);

export const PROFILE_BODY_KEYS = Object.freeze([
    'contact',
    'address',
    'links',
    'education',
    'employment',
    'skills',
    'availability',
    'location_preferences',
    'relocation',
    'compensation',
    'work_authorization',
    'sponsorship',
    'demographics',
    'answers',
    'explanations',
]);

const PROFILE_KEY_SET = new Set(PROFILE_KEYS);
const PROFILE_BODY_KEY_SET = new Set(PROFILE_BODY_KEYS);

function fail(code, location = '') {
    throw new ValidationError(code, location);
}

function unknownKeys(value, allowed, location) {
    const unknown = Object.keys(value).filter((key) => !allowed.has(key)).sort();
    if (unknown.length > 0) {
        fail('E_PROFILE_UNKNOWN_KEY', `${location}.${unknown[0]}`);
    }
}

function object(value, location) {
    if (!isPlainObject(value)) {
        fail('E_PROFILE_OBJECT', location);
    }
    return value;
}

function optionalString(value, location, max = 8192) {
    if (typeof value !== 'string' || value.length === 0 || value.length > max || value.includes('\u0000')) {
        fail('E_PROFILE_STRING', location);
    }
}

function optionalBoolean(value, location) {
    if (typeof value !== 'boolean') {
        fail('E_PROFILE_BOOLEAN', location);
    }
}

function optionalNumber(value, location) {
    if (typeof value !== 'number' || !Number.isFinite(value)) {
        fail('E_PROFILE_NUMBER', location);
    }
}

function optionalStringArray(value, location) {
    if (!Array.isArray(value) || value.length > 256) {
        fail('E_PROFILE_ARRAY', location);
    }
    for (let index = 0; index < value.length; index += 1) {
        optionalString(value[index], `${location}[${index}]`);
    }
}

function checkOptional(value, key, location, validator) {
    if (Object.hasOwn(value, key)) {
        validator(value[key], `${location}.${key}`);
    }
}

function validateContact(value) {
    const location = 'contact';
    const contact = object(value, location);
    const keys = new Set(['name', 'preferred_name', 'first_name', 'last_name', 'email', 'phone']);
    unknownKeys(contact, keys, location);
    for (const key of keys) {
        checkOptional(contact, key, location, optionalString);
    }
}

function validateAddress(value) {
    const location = 'address';
    const address = object(value, location);
    const keys = new Set(['street', 'street2', 'city', 'region', 'postal_code', 'country', 'formatted']);
    unknownKeys(address, keys, location);
    for (const key of keys) {
        checkOptional(address, key, location, optionalString);
    }
}

function validateUrl(value, location) {
    optionalString(value, location, 8192);
    let parsed;
    try {
        parsed = new URL(value);
    } catch {
        fail('E_PROFILE_URL', location);
    }
    if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password) {
        fail('E_PROFILE_URL', location);
    }
}

function validateLinks(value) {
    const location = 'links';
    if (!Array.isArray(value) || value.length > 128) {
        fail('E_PROFILE_ARRAY', location);
    }
    const keys = new Set(['label', 'kind', 'url']);
    for (let index = 0; index < value.length; index += 1) {
        const link = object(value[index], `${location}[${index}]`);
        unknownKeys(link, keys, `${location}[${index}]`);
        if (!Object.hasOwn(link, 'url')) {
            fail('E_PROFILE_REQUIRED', `${location}[${index}].url`);
        }
        validateUrl(link.url, `${location}[${index}].url`);
        checkOptional(link, 'label', `${location}[${index}]`, optionalString);
        checkOptional(link, 'kind', `${location}[${index}]`, optionalString);
    }
}

function validateEducation(value) {
    const location = 'education';
    if (!Array.isArray(value) || value.length > 128) {
        fail('E_PROFILE_ARRAY', location);
    }
    const keys = new Set([
        'institution',
        'degree',
        'field',
        'start_date',
        'end_date',
        'location',
        'description',
        'current',
        'level',
        'gpa',
    ]);
    for (let index = 0; index < value.length; index += 1) {
        const entryLocation = `${location}[${index}]`;
        const entry = object(value[index], entryLocation);
        unknownKeys(entry, keys, entryLocation);
        if (!Object.hasOwn(entry, 'institution')) {
            fail('E_PROFILE_REQUIRED', `${entryLocation}.institution`);
        }
        for (const key of ['institution', 'degree', 'field', 'start_date', 'end_date', 'location', 'description']) {
            checkOptional(entry, key, entryLocation, optionalString);
        }
        if (Object.hasOwn(entry, 'level') && !['college', 'high_school'].includes(entry.level)) {
            fail('E_PROFILE_EDUCATION_LEVEL', `${entryLocation}.level`);
        }
        checkOptional(entry, 'gpa', entryLocation, optionalNumber);
        checkOptional(entry, 'current', entryLocation, optionalBoolean);
    }
}

function validateEmployment(value) {
    const location = 'employment';
    if (!Array.isArray(value) || value.length > 256) {
        fail('E_PROFILE_ARRAY', location);
    }
    const keys = new Set([
        'employer',
        'company',
        'title',
        'start_date',
        'end_date',
        'location',
        'description',
        'highlights',
        'current',
    ]);
    for (let index = 0; index < value.length; index += 1) {
        const entryLocation = `${location}[${index}]`;
        const entry = object(value[index], entryLocation);
        unknownKeys(entry, keys, entryLocation);
        if (!Object.hasOwn(entry, 'employer') && !Object.hasOwn(entry, 'company')) {
            fail('E_PROFILE_REQUIRED', `${entryLocation}.employer`);
        }
        for (const key of ['employer', 'company', 'title', 'start_date', 'end_date', 'location', 'description']) {
            checkOptional(entry, key, entryLocation, optionalString);
        }
        checkOptional(entry, 'highlights', entryLocation, optionalStringArray);
        checkOptional(entry, 'current', entryLocation, optionalBoolean);
    }
}

const SKILL_GROUPS = new Set([
    'languages',
    'frameworks',
    'databases',
    'tools',
    'platforms',
    'methodologies',
    'other',
]);

function validateSkillEntry(value, location) {
    if (typeof value === 'string') {
        optionalString(value, location);
        return;
    }
    const entry = object(value, location);
    const keys = new Set(['name', 'category', 'keywords', 'proficiency', 'sources']);
    unknownKeys(entry, keys, location);
    if (!Object.hasOwn(entry, 'name')) {
        fail('E_PROFILE_REQUIRED', `${location}.name`);
    }
    optionalString(entry.name, `${location}.name`);
    checkOptional(entry, 'category', location, optionalString);
    checkOptional(entry, 'keywords', location, optionalStringArray);
    checkOptional(entry, 'proficiency', location, optionalString);
    checkOptional(entry, 'sources', location, optionalStringArray);
}

function validateSkills(value) {
    const location = 'skills';
    if (Array.isArray(value)) {
        if (value.length > 512) {
            fail('E_PROFILE_ARRAY', location);
        }
        for (let index = 0; index < value.length; index += 1) {
            validateSkillEntry(value[index], `${location}[${index}]`);
        }
        return;
    }
    const groups = object(value, location);
    unknownKeys(groups, SKILL_GROUPS, location);
    for (const [group, entries] of Object.entries(groups)) {
        if (!Array.isArray(entries) || entries.length > 512) {
            fail('E_PROFILE_ARRAY', `${location}.${group}`);
        }
        for (let index = 0; index < entries.length; index += 1) {
            validateSkillEntry(entries[index], `${location}.${group}[${index}]`);
        }
    }
}

function validateAvailability(value) {
    const location = 'availability';
    const availability = object(value, location);
    const keys = new Set([
        'available_from',
        'start_date',
        'notice_period',
        'schedule',
        'timezone',
        'currently_available',
    ]);
    unknownKeys(availability, keys, location);
    for (const key of ['available_from', 'start_date', 'notice_period', 'schedule', 'timezone']) {
        checkOptional(availability, key, location, optionalString);
    }
    checkOptional(availability, 'currently_available', location, optionalBoolean);
}

function validateLocationPreferences(value) {
    const location = 'location_preferences';
    const preferences = object(value, location);
    const keys = new Set([
        'current_location',
        'preferred_locations',
        'remote',
        'hybrid',
        'onsite',
        'willing_to_relocate',
        'relocation_timing',
    ]);
    unknownKeys(preferences, keys, location);
    checkOptional(preferences, 'current_location', location, optionalString);
    checkOptional(preferences, 'preferred_locations', location, optionalStringArray);
    for (const key of ['remote', 'hybrid', 'onsite', 'willing_to_relocate']) {
        checkOptional(preferences, key, location, optionalBoolean);
    }
    checkOptional(preferences, 'relocation_timing', location, optionalString);
}

function validateRelocation(value) {
    const location = 'relocation';
    const relocation = object(value, location);
    const keys = new Set(['willing', 'locations', 'timing', 'support_required', 'notes']);
    unknownKeys(relocation, keys, location);
    checkOptional(relocation, 'willing', location, optionalBoolean);
    checkOptional(relocation, 'locations', location, optionalStringArray);
    checkOptional(relocation, 'timing', location, optionalString);
    checkOptional(relocation, 'support_required', location, optionalBoolean);
    checkOptional(relocation, 'notes', location, optionalString);
}

function validateCompensation(value) {
    const location = 'compensation';
    const compensation = object(value, location);
    const keys = new Set(['currency', 'minimum', 'maximum', 'target', 'period', 'negotiable', 'notes']);
    unknownKeys(compensation, keys, location);
    checkOptional(compensation, 'currency', location, optionalString,);
    for (const key of ['minimum', 'maximum', 'target']) {
        checkOptional(compensation, key, location, optionalNumber);
    }
    checkOptional(compensation, 'period', location, optionalString);
    checkOptional(compensation, 'negotiable', location, optionalBoolean);
    checkOptional(compensation, 'notes', location, optionalString);
}

function validateWorkAuthorization(value) {
    const location = 'work_authorization';
    const authorization = object(value, location);
    const keys = new Set(['authorized', 'countries', 'status', 'expires_on', 'notes']);
    unknownKeys(authorization, keys, location);
    checkOptional(authorization, 'authorized', location, optionalBoolean);
    checkOptional(authorization, 'countries', location, optionalStringArray);
    for (const key of ['status', 'expires_on', 'notes']) {
        checkOptional(authorization, key, location, optionalString);
    }
}

function validateSponsorship(value) {
    const location = 'sponsorship';
    const sponsorship = object(value, location);
    const keys = new Set(['needed', 'type', 'details']);
    unknownKeys(sponsorship, keys, location);
    checkOptional(sponsorship, 'needed', location, optionalBoolean);
    checkOptional(sponsorship, 'type', location, optionalString);
    checkOptional(sponsorship, 'details', location, optionalString);
}

function validateDemographics(value) {
    const location = 'demographics';
    const demographics = object(value, location);
    const keys = new Set([
        'gender',
        'race_ethnicity',
        'ethnicity',
        'veteran_status',
        'disability_status',
        'sexual_orientation',
        'pronouns',
        'prefer_not_to_say',
    ]);
    unknownKeys(demographics, keys, location);
    for (const key of [
        'gender',
        'race_ethnicity',
        'ethnicity',
        'veteran_status',
        'disability_status',
        'sexual_orientation',
        'pronouns',
    ]) {
        checkOptional(demographics, key, location, (item, itemLocation) => {
            if (typeof item === 'string') {
                optionalString(item, itemLocation);
            } else {
                optionalStringArray(item, itemLocation);
            }
        });
    }
    checkOptional(demographics, 'prefer_not_to_say', location, optionalBoolean);
}

function requireProfileAlias(alias, location) {
    if (typeof alias !== 'string'
        || alias.length === 0
        || alias.length > MAX_ALIAS_LENGTH
        || alias.trim().length === 0
        || alias.includes('\u0000')) {
        fail('E_PROFILE_ALIAS', location);
    }
}

function validateAliasMap(value, location, valueValidator) {
    const map = object(value, location);
    for (const [alias, answer] of Object.entries(map)) {
        requireProfileAlias(alias, `${location}.${alias}`);
        valueValidator(answer, `${location}.${alias}`);
    }
}

const PROFILE_BODY_VALIDATORS = Object.freeze({
    contact: validateContact,
    address: validateAddress,
    links: validateLinks,
    education: validateEducation,
    employment: validateEmployment,
    skills: validateSkills,
    availability: validateAvailability,
    location_preferences: validateLocationPreferences,
    relocation: validateRelocation,
    compensation: validateCompensation,
    work_authorization: validateWorkAuthorization,
    sponsorship: validateSponsorship,
    demographics: validateDemographics,
    answers: (value, location) => validateAliasMap(value, location, validateJsonValue),
    explanations: (value, location) => validateAliasMap(value, location, optionalString),
});

function validateTier(value, location) {
    const tier = object(value, location);
    unknownKeys(tier, PROFILE_BODY_KEY_SET, location);
    for (const key of Object.keys(tier)) {
        PROFILE_BODY_VALIDATORS[key](tier[key], `${location}.${key}`);
    }
}

function validateInferredFacts(value) {
    const location = 'inferred_facts';
    const map = object(value, location);
    for (const [alias, entry] of Object.entries(map)) {
        requireProfileAlias(alias, `${location}.${alias}`);
        validateInferenceEntry(entry, `${location}.${alias}`, {
            evidenceKeys: PROFILE_INFERENCE_EVIDENCE_KEYS,
            schemaCode: 'E_PROFILE_INFERENCE_SCHEMA',
            rationaleCode: 'E_PROFILE_INFERENCE_RATIONALE',
            evidenceCode: 'E_PROFILE_INFERENCE_EVIDENCE',
        });
    }
}

function validateUnknowns(value) {
    const location = 'unknowns';
    if (!Array.isArray(value)) {
        fail('E_PROFILE_UNKNOWN', location);
    }
    const seen = new Set();
    for (let index = 0; index < value.length; index += 1) {
        const itemLocation = `${location}[${index}]`;
        const alias = value[index];
        if (typeof alias !== 'string'
            || alias.length === 0
            || alias.length > MAX_ALIAS_LENGTH
            || alias.trim().length === 0
            || alias.includes('\u0000')) {
            fail('E_PROFILE_UNKNOWN', itemLocation);
        }
        if (seen.has(alias)) {
            fail('E_PROFILE_UNKNOWN_DUPLICATE', itemLocation);
        }
        seen.add(alias);
    }
}

function assertNoAliasConflicts(profile) {
    const homes = new Map();
    for (const tierKey of ['verified_facts', 'user_attested_facts']) {
        const tier = profile[tierKey];
        if (!isPlainObject(tier) || !isPlainObject(tier.answers)) {
            continue;
        }
        for (const alias of Object.keys(tier.answers)) {
            const previous = homes.get(alias);
            if (previous !== undefined) {
                fail('E_PROFILE_ALIAS_CONFLICT', `${previous}:${tierKey}.answers:${alias}`);
            }
            homes.set(alias, `${tierKey}.answers`);
        }
    }
    if (isPlainObject(profile.inferred_facts)) {
        for (const alias of Object.keys(profile.inferred_facts)) {
            const previous = homes.get(alias);
            if (previous !== undefined) {
                fail('E_PROFILE_ALIAS_CONFLICT', `${previous}:inferred_facts:${alias}`);
            }
            homes.set(alias, 'inferred_facts');
        }
    }
}

function validateJsonValue(value, location, seen = new Set()) {
    if (value === null || typeof value === 'string' || typeof value === 'boolean') {
        if (typeof value === 'string' && value.length > 64 * 1024) {
            fail('E_PROFILE_VALUE_TOO_LARGE', location);
        }
        return;
    }
    if (typeof value === 'number') {
        if (!Number.isFinite(value)) {
            fail('E_PROFILE_VALUE', location);
        }
        return;
    }
    if (typeof value !== 'object' || seen.has(value)) {
        fail('E_PROFILE_VALUE', location);
    }
    seen.add(value);
    if (Array.isArray(value)) {
        if (value.length > 256) {
            fail('E_PROFILE_VALUE_TOO_LARGE', location);
        }
        for (let index = 0; index < value.length; index += 1) {
            validateJsonValue(value[index], `${location}[${index}]`, seen);
        }
    } else {
        if (!isPlainObject(value) || Object.keys(value).length > 256) {
            fail('E_PROFILE_VALUE', location);
        }
        for (const [key, item] of Object.entries(value)) {
            validateJsonValue(item, `${location}.${key}`, seen);
        }
    }
    seen.delete(value);
}

export function validateProfile(input) {
    const profile = object(input, '$');
    unknownKeys(profile, PROFILE_KEY_SET, '$');
    if (profile.schema !== PROFILE_SCHEMA) {
        fail('E_PROFILE_SCHEMA', 'schema');
    }
    for (const key of ['verified_facts', 'user_attested_facts']) {
        if (Object.hasOwn(profile, key)) {
            validateTier(profile[key], key);
        }
    }
    if (Object.hasOwn(profile, 'inferred_facts')) {
        validateInferredFacts(profile.inferred_facts);
    }
    if (Object.hasOwn(profile, 'unknowns')) {
        validateUnknowns(profile.unknowns);
    }
    assertNoAliasConflicts(profile);
    const encoded = canonicalJson(profile);
    if (Buffer.byteLength(encoded, 'utf8') > MAX_PROFILE_BYTES) {
        fail('E_JSON_OVERSIZE', 'profile');
    }
    return structuredClone(profile);
}

export async function loadProfile(profilePath) {
    const parsed = await readJsonFileSecure(profilePath, {
        maxBytes: MAX_PROFILE_BYTES,
        ownerOnly: true,
    });
    return validateProfile(parsed);
}

export function profileAnswer(profile, alias) {
    const validated = validateProfile(profile);
    for (const [tierKey, source] of [
        ['verified_facts', 'profile_verified'],
        ['user_attested_facts', 'profile_user_attested'],
    ]) {
        const tier = validated[tierKey];
        if (isPlainObject(tier) && isPlainObject(tier.answers) && Object.hasOwn(tier.answers, alias)) {
            return { found: true, source, value: structuredClone(tier.answers[alias]) };
        }
    }
    return { found: false, unknown: Array.isArray(validated.unknowns) && validated.unknowns.includes(alias) };
}

export function profileExplanation(profile, alias) {
    const validated = validateProfile(profile);
    for (const [tierKey, source] of [
        ['verified_facts', 'profile_verified'],
        ['user_attested_facts', 'profile_user_attested'],
    ]) {
        const tier = validated[tierKey];
        if (isPlainObject(tier) && isPlainObject(tier.explanations) && Object.hasOwn(tier.explanations, alias)) {
            return { found: true, source, value: structuredClone(tier.explanations[alias]) };
        }
    }
    return { found: false, unknown: Array.isArray(validated.unknowns) && validated.unknowns.includes(alias) };
}
