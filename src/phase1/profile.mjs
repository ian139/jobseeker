import {
    MAX_ALIAS_LENGTH,
    MAX_PROFILE_BYTES,
    ValidationError,
    canonicalJson,
    isPlainObject,
    readJsonFileSecure,
} from './contract.mjs';

export const PROFILE_SCHEMA = 'phase1-profile-v1';

export const PROFILE_KEYS = Object.freeze([
    'schema',
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

function validateAliasMap(value, location, valueValidator) {
    const map = object(value, location);
    for (const [alias, answer] of Object.entries(map)) {
        if (alias.length === 0 || alias.length > MAX_ALIAS_LENGTH || alias.trim().length === 0 || alias.includes('\u0000')) {
            fail('E_PROFILE_ALIAS', `${location}.${alias}`);
        }
        valueValidator(answer, `${location}.${alias}`);
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
    checkOptional(profile, 'contact', '', validateContact);
    checkOptional(profile, 'address', '', validateAddress);
    checkOptional(profile, 'links', '', validateLinks);
    checkOptional(profile, 'education', '', validateEducation);
    checkOptional(profile, 'employment', '', validateEmployment);
    checkOptional(profile, 'skills', '', validateSkills);
    checkOptional(profile, 'availability', '', validateAvailability);
    checkOptional(profile, 'location_preferences', '', validateLocationPreferences);
    checkOptional(profile, 'relocation', '', validateRelocation);
    checkOptional(profile, 'compensation', '', validateCompensation);
    checkOptional(profile, 'work_authorization', '', validateWorkAuthorization);
    checkOptional(profile, 'sponsorship', '', validateSponsorship);
    checkOptional(profile, 'demographics', '', validateDemographics);
    checkOptional(profile, 'answers', '', (value, location) => validateAliasMap(value, 'answers', validateJsonValue));
    checkOptional(profile, 'explanations', '', (value, location) => {
        validateAliasMap(value, 'explanations', optionalString);
    });
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
    if (!Object.hasOwn(validated, 'answers') || !Object.hasOwn(validated.answers, alias)) {
        return { found: false };
    }
    return { found: true, value: structuredClone(validated.answers[alias]) };
}

export function profileExplanation(profile, alias) {
    const validated = validateProfile(profile);
    if (!Object.hasOwn(validated, 'explanations') || !Object.hasOwn(validated.explanations, alias)) {
        return { found: false };
    }
    return { found: true, value: validated.explanations[alias] };
}
