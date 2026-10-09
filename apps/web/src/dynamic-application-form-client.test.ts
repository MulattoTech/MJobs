import { describe, expect, it } from 'vitest';
import {
  DynamicApplicationFieldsClientError,
  parseDynamicApplicationFields,
} from './dynamic-application-form-client';

const scopedResponse = (fields: unknown[]) => ({
  profile_id: 'profile-a',
  application_id: 'application-a',
  fingerprint: 'a'.repeat(64),
  fields,
  optional_sections: [{ id: 'history', label: 'Employment history', field_ids: ['employer'] }],
  repeatable_groups: [{ id: 'employment', field_ids: ['employer'], minimum_rows: 1, maximum_rows: 3 }],
});

describe('dynamic application form metadata client', () => {
  it('maps only the selected profile scoped field shape into review controls', () => {
    const parsed = parseDynamicApplicationFields(scopedResponse([
      { id: 'country', label: 'Country', type: 'select', required: true, visible: true,
        depends_on: null, options: ['United States', 'Canada'], repeatable: false,
        supported: true, sensitive: false, manual_review_required: false },
      { id: 'region', label: 'State', type: 'select', required: true, visible: true,
        depends_on: 'country', condition: { question_id: 'country', equals: 'United States' },
        options_by: { 'United States': ['Oklahoma', 'Texas'] }, options: [], repeatable: false,
        supported: true, sensitive: false, manual_review_required: false },
      { id: 'employer', label: 'Employer', type: 'text', required: false, visible: true,
        depends_on: null, options: [], repeatable: true, section_id: 'history',
        repeatable_group: 'employment', repeatable_min_rows: 1, repeatable_max_rows: 3,
        supported: true, sensitive: false, manual_review_required: false },
    ]), 'profile-a', 'application-a');

    expect(parsed.status).toBe('READY');
    expect(parsed.fields[1].visible_when).toEqual({ field_id: 'country', equals: 'United States' });
    expect(parsed.fields[1].options_when).toEqual({
      field_id: 'country', by_value: { 'United States': ['Oklahoma', 'Texas'] },
    });
    expect(parsed.fields[2].optional_section_label).toBe('Employment history');
    expect(parsed.fields[2].repeatable).toEqual({ row_label: 'Employer', min_rows: 1, max_rows: 3 });
  });

  it('keeps sensitive and unclassified fields manual without exposing their labels or choices', () => {
    const parsed = parseDynamicApplicationFields(scopedResponse([
      { id: 'private-field', label: 'Social Security number', type: 'select', required: true,
        visible: true, depends_on: null, options: ['Choice A'], repeatable: false,
        supported: false, sensitive: true, manual_review_required: true },
    ]), 'profile-a', 'application-a');

    expect(parsed.fields[0].kind).toBe('unsupported');
    expect(parsed.fields[0].sensitive).toBe(true);
    expect(parsed.fields[0].label).not.toContain('Social Security');
    expect(parsed.fields[0].options).toBeUndefined();
  });

  it('rejects scope mismatch and malformed response metadata', () => {
    expect(() => parseDynamicApplicationFields(
      { ...scopedResponse([]), profile_id: 'other-profile' }, 'profile-a', 'application-a',
    )).toThrowError(DynamicApplicationFieldsClientError);
    expect(() => parseDynamicApplicationFields(
      { ...scopedResponse([]), fingerprint: 'bad' }, 'profile-a', 'application-a',
    )).toThrowError(DynamicApplicationFieldsClientError);
  });
});
