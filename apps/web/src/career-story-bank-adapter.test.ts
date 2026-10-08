import { describe, expect, it } from 'vitest';

import {
  decodeStoryText,
  encodeStoryText,
  groupStoryRows,
  toCreatePayload,
} from './career-story-bank-adapter';

const starInput = {
  title: 'Improved report performance',
  situation: 'Reports were slow to load.',
  task: 'Reduce their response time.',
  action: 'Optimized the Python query path.',
  result: 'Reports loaded faster.',
  factIds: ['fact-python'],
  tags: ['performance'],
  allowedUses: ['RESUME', 'INTERVIEW_PREPARATION'],
};

describe('career story bank API adapter', () => {
  it('round trips versioned STAR text without losing Unicode or newlines', () => {
    const input = {
      ...starInput,
      situation: 'The team used résumé exports.\nThe delay was visible.',
      action: 'Optimized résumé parsing — safely.',
    };

    const encoded = encodeStoryText(input);

    expect(decodeStoryText(encoded)).toEqual({
      kind: 'STAR_STORY',
      situation: input.situation,
      task: input.task,
      action: input.action,
      result: input.result,
    });
  });

  it('keeps legacy and malformed versioned text as unstructured content', () => {
    const legacy = 'A user-authored story with no STAR structure.';
    const malformed = 'MJOB-CAREER-STORY/1\n{"version":1,"situation":false}';

    expect(decodeStoryText(legacy)).toEqual({ kind: 'UNSTRUCTURED', text: legacy });
    expect(decodeStoryText(malformed)).toEqual({ kind: 'UNSTRUCTURED', text: malformed });
  });

  it('maps a component draft to the exact API payload and requires an allowed use', () => {
    const payload = toCreatePayload(starInput);

    expect(payload).toEqual({
      kind: 'STAR_STORY',
      title: starInput.title,
      text: encodeStoryText(starInput),
      fact_ids: ['fact-python'],
      tags: ['performance'],
      allowed_uses: ['RESUME', 'INTERVIEW_PREPARATION'],
    });
    expect(() => toCreatePayload({ ...starInput, allowedUses: [] })).toThrow(/allowed use/i);
  });

  it('groups all API revisions by stable story key while keeping revision IDs and fact provenance', () => {
    const older = {
      story_id: 'revision-a', story_key: 'story-a', revision: 1,
      title: 'Improved report performance', text: encodeStoryText(starInput),
      fact_ids: ['fact-python'],
      fact_fingerprints: [{ fact_id: 'fact-python', fingerprint: 'sha256:abc' }],
      review_status: 'APPROVED', review_note: 'Checked', created_at: '2026-01-01T00:00:00Z',
      reviewed_at: '2026-01-02T00:00:00Z', is_latest: false, reusable: false,
    };
    const latest = {
      ...older,
      story_id: 'revision-b',
      revision: 2,
      text: 'A legacy note kept exactly as entered.',
      review_status: 'PENDING',
      review_note: null,
      created_at: '2026-01-03T00:00:00Z',
      reviewed_at: null,
      is_latest: true,
      reusable: false,
    };

    const stories = groupStoryRows([latest, older]);

    expect(stories).toHaveLength(1);
    expect(stories[0].storyId).toBe('story-a');
    expect(stories[0].revisions.map(row => [row.revisionId, row.revision])).toEqual([
      ['revision-a', 1], ['revision-b', 2],
    ]);
    expect(stories[0].revisions[0].reviewStatus).toBe('APPROVED');
    expect(stories[0].revisions[0].provenance).toEqual(['fact-python']);
    expect(stories[0].revisions[0].factFingerprints).toEqual([
      { fact_id: 'fact-python', fingerprint: 'sha256:abc' },
    ]);
    expect(stories[0].revisions[1]).toMatchObject({
      revisionId: 'revision-b',
      reviewStatus: 'PENDING',
      situation: '',
      task: '',
      action: '',
      result: '',
      unstructuredText: 'A legacy note kept exactly as entered.',
    });
  });
});
