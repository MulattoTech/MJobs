import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { CAREER_STORY_ALLOWED_USES, CareerStoryBank, isStoryDraftSaveReady, isStoryRevisionReusable, type CareerStory } from './career-story-bank';

const stories: CareerStory[] = [{
  storyId: 'story-1',
  revisions: [
    {
      revisionId: 'rev-1', revision: 1, createdAt: '2026-10-08T12:00:00Z',
      title: 'Improved service reliability', situation: 'A service had recurring outages.',
      task: 'Improve reliability.', action: 'Added health checks and alerts.', result: 'Incidents became easier to detect.',
      factIds: ['fact-1'], allowedUses: ['RESUME', 'INTERVIEW_PREPARATION'], factFingerprints: [{factId: 'fact-1', fingerprint: 'fact-v1'}], provenance: ['fact-1'], staleFactIds: [], isLatest: false, reviewStatus: 'PENDING', reviewedAt: null,
    },
    {
      revisionId: 'rev-2', revision: 2, createdAt: '2026-10-08T13:00:00Z',
      title: 'Improved service reliability', situation: 'A service had recurring outages.',
      task: 'Improve reliability.', action: 'Added health checks and alerts.', result: 'Incidents became easier to detect.',
      factIds: ['fact-1'], allowedUses: ['RESUME', 'INTERVIEW_PREPARATION'], factFingerprints: [{factId: 'fact-1', fingerprint: 'fact-v1'}], provenance: ['fact-1'], staleFactIds: [], isLatest: true, reviewStatus: 'APPROVED', reviewedAt: '2026-10-08T14:00:00Z',
    },
  ],
}];

describe('career story bank', () => {
  it('makes reuse unavailable until the exact revision is explicitly reviewed', () => {
    const approvedFacts = [{factId: 'fact-1', kind: 'accomplishment', text: 'Added service health checks', source: 'Acme work history', verified: true, userApproved: true}];
    const draft = {factIds: ['fact-1'], allowedUses: ['RESUME']};
    expect(isStoryRevisionReusable(stories[0].revisions[0], approvedFacts)).toBe(false);
    expect(isStoryRevisionReusable(stories[0].revisions[1], approvedFacts)).toBe(true);
    expect(isStoryRevisionReusable(stories[0].revisions[1], [])).toBe(false);
    expect(isStoryRevisionReusable({...stories[0].revisions[1], allowedUses: []}, approvedFacts)).toBe(false);
    expect(isStoryRevisionReusable({...stories[0].revisions[1], reviewStatus: 'REJECTED'}, approvedFacts)).toBe(false);
    expect(isStoryRevisionReusable({...stories[0].revisions[1], staleFactIds: ['fact-1']}, approvedFacts)).toBe(false);
    expect(isStoryRevisionReusable({...stories[0].revisions[1], isLatest: false}, approvedFacts)).toBe(false);
    expect(isStoryDraftSaveReady(draft, approvedFacts)).toBe(true);
    expect(isStoryDraftSaveReady({...draft, allowedUses: []}, approvedFacts)).toBe(false);
    expect(isStoryDraftSaveReady({...draft, factIds: []}, approvedFacts)).toBe(false);
    expect(isStoryDraftSaveReady(draft, [{...approvedFacts[0], verified: false}])).toBe(false);
    expect(CAREER_STORY_ALLOWED_USES.map(use => use.value)).toEqual([
      'RESUME', 'COVER_LETTER', 'APPLICATION_ANSWER', 'INTERVIEW_PREPARATION',
    ]);
  });

  it('shows immutable revision history, approved fact links and provenance', () => {
    const html = renderToStaticMarkup(<CareerStoryBank
      stories={stories}
      approvedFacts={[{factId: 'fact-1', kind: 'accomplishment', text: 'Added service health checks', source: 'Acme work history', verified: true, userApproved: true}]}
      onCreateRevision={async () => ({storyId: stories[0].storyId, revision: stories[0].revisions[1]})}
      onReviewRevision={async () => stories[0].revisions[1]}
    />);

    expect(html).toContain('Revision 1');
    expect(html).toContain('Revision 2');
    expect(html).toContain('Added service health checks');
    expect(html).toContain('Acme work history');
    expect(html).toContain('Review this revision');
    expect(html).toContain('STORY REVIEW STATUS: APPROVED');
    expect(html).toContain('FACT VERIFIED');
    expect(html).toContain('FACT USER-APPROVED');
    expect(html).toContain('Allowed uses:</strong> Resume');
    expect(html).toContain('Linked fact fingerprint: fact-v1');
    expect(html).toMatch(/Revision 1[\s\S]*?STORY REVIEW STATUS: PENDING[\s\S]*?UNAVAILABLE FOR REUSE/i);
    expect(html).toMatch(/Revision 2[\s\S]*?STORY REVIEW STATUS: APPROVED[\s\S]*?AVAILABLE FOR REUSE/i);
  });

  it('offers a create action and states review is required before reuse', () => {
    const html = renderToStaticMarkup(<CareerStoryBank
      stories={stories.slice(0, 0)}
      approvedFacts={[]}
      onCreateRevision={async () => ({storyId: stories[0].storyId, revision: stories[0].revisions[1]})}
      onReviewRevision={async () => stories[0].revisions[1]}
    />);

    expect(html).toContain('New career story');
    expect(html).toContain('Save as new revision');
    expect(html).toContain('A saved revision is unavailable for reuse until you review that exact revision.');
    expect(html).toContain('Allowed uses');
    expect(html).toContain('Allow use: Resume');
    expect(html).toContain('Allow use: Cover letter');
    expect(html).toContain('Allow use: Application answer');
    expect(html).toContain('Allow use: Interview preparation');
    expect(html).toContain('Select at least one allowed use and one currently verified, user-approved fact before saving.');
    expect(html).toMatch(/<button type="submit" disabled="">Save as new revision/i);
  });

  it('does not present an unverified linked claim as established fact', () => {
    const html = renderToStaticMarkup(<CareerStoryBank
      stories={stories}
      approvedFacts={[{factId: 'fact-1', kind: 'accomplishment', text: 'Added service health checks', source: 'Acme work history', verified: false, userApproved: true}]}
      onCreateRevision={async () => ({storyId: stories[0].storyId, revision: stories[0].revisions[1]})}
      onReviewRevision={async () => stories[0].revisions[1]}
    />);

    expect(html).toContain('FACT NOT VERIFIED');
    expect(html).toContain('FACT USER-APPROVED');
    expect(html).toContain('This fact is not verified; its linked story text is not established as true.');
    expect(html).toContain('UNAVAILABLE FOR REUSE');
  });

  it('displays legacy unstructured text without parsing it as verified STAR fields', () => {
    const legacyStory: CareerStory = {
      storyId: 'legacy-story',
      revisions: [{
        revisionId: 'legacy-rev', revision: 1, createdAt: '2026-10-08T13:00:00Z',
        title: 'Legacy note', situation: '', task: '', action: '', result: '', factIds: [],
        allowedUses: [], provenance: [], staleFactIds: [], isLatest: true, reviewStatus: 'PENDING', reviewedAt: null,
        unstructuredText: 'Led a migration and improved reliability.',
      }],
    };
    const html = renderToStaticMarkup(<CareerStoryBank
      stories={[legacyStory]}
      approvedFacts={[]}
      onCreateRevision={async () => ({storyId: legacyStory.storyId, revision: legacyStory.revisions[0]})}
      onReviewRevision={async () => legacyStory.revisions[0]}
    />);

    expect(html).toContain('Legacy unstructured story text (not parsed as STAR fields)');
    expect(html).toContain('Led a migration and improved reliability.');
    expect(html).toContain('UNAVAILABLE FOR REUSE');
  });
});
