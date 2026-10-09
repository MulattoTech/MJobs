import type { DynamicApplicationField } from './dynamic-application-form';

export type DynamicApplicationFields = {
  profile_id: string;
  application_id: string;
  fingerprint: string;
  status: 'READY' | 'REVIEW_REQUIRED';
  fields: DynamicApplicationField[];
};

export class DynamicApplicationFieldsClientError extends Error {
  constructor(readonly code: 'INVALID_RESPONSE' | 'SCOPE_MISMATCH' | 'STALE' | 'UNAVAILABLE') {
    super(code === 'STALE'
      ? 'The application form changed. Reload its current fields before continuing.'
      : code === 'SCOPE_MISMATCH'
        ? 'The dynamic fields belong to a different profile or application.'
        : code === 'UNAVAILABLE'
          ? 'Dynamic application fields could not be loaded. Retry when the local service is available.'
          : 'The dynamic application field response could not be validated.');
    this.name = 'DynamicApplicationFieldsClientError';
  }
}

type RecordValue = Record<string, unknown>;
const isRecord = (value: unknown): value is RecordValue =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const identifier = /^[A-Za-z0-9_-]{1,80}$/;
const fingerprint = /^[0-9a-f]{64}$/;

function safeStrings(value: unknown, maximum = 100): string[] | null {
  if (!Array.isArray(value) || value.length > maximum
      || value.some((item) => typeof item !== 'string' || !item.trim() || item.length > 500)) return null;
  return [...new Set(value.map((item) => (item as string).replace(/\s+/g, ' ').trim()))];
}

function optionalSections(value: unknown): Map<string, string> | null {
  if (value === undefined) return new Map();
  if (!Array.isArray(value) || value.length > 32) return null;
  const result = new Map<string, string>();
  for (const section of value) {
    if (!isRecord(section) || typeof section.id !== 'string' || !identifier.test(section.id)
        || (section.label !== undefined && (typeof section.label !== 'string' || section.label.length > 500))
        || !Array.isArray(section.field_ids) || section.field_ids.length > 200
        || section.field_ids.some((id) => typeof id !== 'string' || !identifier.test(id))
        || result.has(section.id)) return null;
    result.set(section.id, typeof section.label === 'string' && section.label.trim()
      ? section.label.replace(/\s+/g, ' ').trim() : `Optional section ${section.id}`);
  }
  return result;
}

type RepeatableGroup = RecordValue & { field_ids: string[]; minimum_rows: number; maximum_rows: number };

function repeatableGroups(value: unknown): Map<string, RepeatableGroup> | null {
  if (value === undefined) return new Map();
  if (!Array.isArray(value) || value.length > 20) return null;
  const result = new Map<string, RepeatableGroup>();
  for (const group of value) {
    if (!isRecord(group) || typeof group.id !== 'string' || !identifier.test(group.id)
        || !Array.isArray(group.field_ids) || group.field_ids.length < 1 || group.field_ids.length > 100
        || group.field_ids.some((id) => typeof id !== 'string' || !identifier.test(id))
        || !Number.isInteger(group.minimum_rows) || !Number.isInteger(group.maximum_rows)
        || (group.minimum_rows as number) < 0 || (group.maximum_rows as number) < 1
        || (group.minimum_rows as number) > (group.maximum_rows as number)
        || (group.maximum_rows as number) > 20 || result.has(group.id)) return null;
    result.set(group.id, {
      ...group,
      field_ids: group.field_ids as string[],
      minimum_rows: group.minimum_rows as number,
      maximum_rows: group.maximum_rows as number,
    });
  }
  return result;
}

export function parseDynamicApplicationFields(
  value: unknown,
  profileId: string,
  applicationId: string,
): DynamicApplicationFields {
  if (!isRecord(value) || value.profile_id !== profileId || value.application_id !== applicationId) {
    throw new DynamicApplicationFieldsClientError('SCOPE_MISMATCH');
  }
  if (typeof value.fingerprint !== 'string' || !fingerprint.test(value.fingerprint)
      || !Array.isArray(value.fields) || value.fields.length < 1 || value.fields.length > 200) {
    throw new DynamicApplicationFieldsClientError('INVALID_RESPONSE');
  }
  const sections = optionalSections(value.optional_sections);
  const groups = repeatableGroups(value.repeatable_groups);
  if (!sections || !groups) throw new DynamicApplicationFieldsClientError('INVALID_RESPONSE');
  const seen = new Set<string>();
  const fields: DynamicApplicationField[] = [];
  let needsReview = value.status === 'REVIEW_REQUIRED';

  for (const raw of value.fields) {
    if (!isRecord(raw)) throw new DynamicApplicationFieldsClientError('INVALID_RESPONSE');
    const id = raw.id;
    const rawLabel = raw.label;
    const rawType = raw.type;
    const options = safeStrings(raw.options);
    const dependentOn = raw.depends_on;
    const sectionId = raw.section_id;
    if (typeof id !== 'string' || !identifier.test(id) || seen.has(id)
        || typeof rawLabel !== 'string' || !rawLabel.trim() || rawLabel.length > 500
        || typeof rawType !== 'string' || typeof raw.required !== 'boolean'
        || typeof raw.visible !== 'boolean' || typeof raw.repeatable !== 'boolean'
        || typeof raw.supported !== 'boolean' || typeof raw.sensitive !== 'boolean'
        || typeof raw.manual_review_required !== 'boolean' || options === null
        || (dependentOn !== null && (typeof dependentOn !== 'string' || !identifier.test(dependentOn)))
        || (sectionId !== undefined && (typeof sectionId !== 'string' || !sections.has(sectionId)))) {
      throw new DynamicApplicationFieldsClientError('INVALID_RESPONSE');
    }
    seen.add(id);

    const kindMap: Record<string, DynamicApplicationField['kind']> = {
      text: 'text', select: 'select', boolean: 'checkbox', choice: 'select',
    };
    let kind = kindMap[rawType] || 'unsupported';
    let supported = raw.supported && !raw.manual_review_required && !raw.sensitive && kind !== 'unsupported';
    let reason: string | undefined;
    if (raw.sensitive || raw.manual_review_required || !supported) {
      supported = false;
      kind = 'unsupported';
      reason = 'This field requires manual review. MJobs will not infer or send an answer.';
      needsReview = true;
    }
    const field: DynamicApplicationField = {
      id,
      label: supported ? rawLabel.replace(/\s+/g, ' ').trim() : 'Field requires manual review',
      kind,
      required: raw.required,
      supported,
      sensitive: raw.sensitive,
      reason,
    };
    if (supported && options.length) field.options = options;
    if (supported && sectionId !== undefined) {
      field.optional_section_id = sectionId;
      field.optional_section_label = sections.get(sectionId);
    }
    if (supported && dependentOn !== null && isRecord(raw.condition)
        && raw.condition.question_id === dependentOn
        && typeof raw.condition.equals === 'string' && raw.condition.equals.length <= 500) {
      field.visible_when = { field_id: dependentOn, equals: raw.condition.equals };
    }
    if (supported && dependentOn !== null && isRecord(raw.options_by)) {
      const byValue: Record<string, string[]> = {};
      for (const [parentValue, rawOptions] of Object.entries(raw.options_by)) {
        const allowed = safeStrings(rawOptions);
        if (!parentValue || parentValue.length > 500 || !allowed || !allowed.length) {
          throw new DynamicApplicationFieldsClientError('INVALID_RESPONSE');
        }
        byValue[parentValue] = allowed;
      }
      field.options_when = { field_id: dependentOn, by_value: byValue };
    }
    if (supported && raw.repeatable) {
      const groupId = raw.repeatable_group;
      const group = typeof groupId === 'string' ? groups.get(groupId) : undefined;
      if (!group || group.field_ids.length !== 1 || group.field_ids[0] !== id) {
        field.kind = 'unsupported';
        field.supported = false;
        field.reason = 'This repeated group needs manual review before it can be represented safely.';
        needsReview = true;
      } else {
        field.repeatable = {
          row_label: field.label,
          min_rows: group.minimum_rows,
          max_rows: group.maximum_rows,
        };
      }
    }
    fields.push(field);
  }
  if (fields.some((field) => field.visible_when && !seen.has(field.visible_when.field_id)
      || field.options_when && !seen.has(field.options_when.field_id))) {
    throw new DynamicApplicationFieldsClientError('INVALID_RESPONSE');
  }
  return {
    profile_id: profileId,
    application_id: applicationId,
    fingerprint: value.fingerprint,
    status: needsReview ? 'REVIEW_REQUIRED' : 'READY',
    fields,
  };
}

export function createDynamicApplicationFieldsClient(fetcher: typeof fetch = globalThis.fetch) {
  return {
    async load(profileId: string, applicationId: string, signal?: AbortSignal,
      expectedFingerprint?: string): Promise<DynamicApplicationFields> {
      const query = expectedFingerprint ? `?expected_fingerprint=${encodeURIComponent(expectedFingerprint)}` : '';
      let response: Response;
      try {
        response = await fetcher(`/profiles/${encodeURIComponent(profileId)}/applications/${encodeURIComponent(applicationId)}/dynamic-fields${query}`, {
          method: 'GET', cache: 'no-store', signal,
        });
      } catch {
        if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
        throw new DynamicApplicationFieldsClientError('UNAVAILABLE');
      }
      if (response.status === 409) throw new DynamicApplicationFieldsClientError('STALE');
      if (!response.ok) throw new DynamicApplicationFieldsClientError('UNAVAILABLE');
      let body: unknown;
      try { body = await response.json(); }
      catch { throw new DynamicApplicationFieldsClientError('INVALID_RESPONSE'); }
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      return parseDynamicApplicationFields(body, profileId, applicationId);
    },
  };
}
