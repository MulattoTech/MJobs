import React, { Children, isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import {
  ApplicationStoryReferencePanel,
  type ApplicationStoryCitation,
} from './application-story-reference-panel';
import type { CareerStorySelectionCandidate } from './career-story-selection';

const currentCandidate: CareerStorySelectionCandidate = {
  storyId: 'story-current',
  revisionId: 'story-revision-4',
  revision: 4,
  title: 'Restored service availability',
  reviewStatus: 'APPROVED',
  reviewedAt: '2026-10-08T16:30:00Z',
  isLatest: true,
  reusable: true,
  allowedUses: ['APPLICATION_ANSWER'],
  factIds: ['fact-availability', 'fact-automation'],
  factFingerprints: [
    {factId: 'fact-availability', fingerprint: 'approved-fingerprint-availability'},
    {factId: 'fact-automation', fingerprint: 'approved-fingerprint-automation'},
  ],
  staleFactIds: [],
};

const expectedCitation: ApplicationStoryCitation = {
  profileId: 'profile-current',
  jobId: 'job-current',
  storyId: 'story-current',
  revisionId: 'story-revision-4',
  revision: 4,
  factIds: ['fact-availability', 'fact-automation'],
  factFingerprints: [
    {factId: 'fact-availability', fingerprint: 'approved-fingerprint-availability'},
    {factId: 'fact-automation', fingerprint: 'approved-fingerprint-automation'},
  ],
};

const baseProps = {
  profileId: 'profile-current',
  jobId: 'job-current',
  jobTitle: 'Systems Administrator',
  candidates: [currentCandidate],
  approvedFactIds: ['fact-availability', 'fact-automation'],
  selectedCitation: null as ApplicationStoryCitation | null,
  onCitationChange: vi.fn<(citation: ApplicationStoryCitation | null) => void>(),
};

type RadioElement = ReactElement<{
  type?: string;
  onChange?: (event: {target: {checked: boolean}}) => void;
  disabled?: boolean;
  children?: ReactNode;
}>;

function findCitationRadios(node: ReactNode): RadioElement[] {
  return Children.toArray(node).flatMap(child => {
    if (!isValidElement<RadioElement['props']>(child)) return [];
    const radio = child.type === 'input' && child.props.type === 'radio' ? [child] : [];
    return [...radio, ...findCitationRadios(child.props.children)];
  });
}

function renderPanel(props: typeof baseProps) {
  const tree = ApplicationStoryReferencePanel(props);
  return {tree, html: renderToStaticMarkup(tree), radios: findCitationRadios(tree)};
}

describe('application story citation review integration', () => {
  it('renders the exact job-scoped story revision and approved fact provenance', () => {
    const {html} = renderPanel(baseProps);

    expect(html).toContain('Career story citations for Systems Administrator');
    expect(html).toContain('Profile profile-current · job job-current');
    expect(html).toContain('story-current');
    expect(html).toContain('story-revision-4');
    expect(html).toContain('revision 4');
    expect(html).toContain('APPROVED');
    expect(html).toContain('Application answer');
    expect(html).toContain('fact-availability');
    expect(html).toContain('approved-fingerprint-availability');
    expect(html).toContain('fact-automation');
    expect(html).toContain('approved-fingerprint-automation');
    expect(html).not.toContain('Restored service availability demonstrated');
    expect(html).not.toMatch(/type="radio"[^>]*checked/);
    expect(baseProps.onCitationChange).not.toHaveBeenCalled();
  });

  it.each([
    ['foreign profile', {...expectedCitation, profileId: 'profile-foreign'},
      'The saved citation does not match the current profile, job, or story revision.'],
    ['foreign job', {...expectedCitation, jobId: 'job-foreign'},
      'The saved citation does not match the current profile, job, or story revision.'],
    ['stale revision', {...expectedCitation, revisionId: 'story-revision-3', revision: 3},
      'The saved citation does not match a current approved story revision or its fact provenance.'],
  ])('does not restore a %s citation as selected', (_label, selectedCitation, expectedReason) => {
    const {html, radios} = renderPanel({...baseProps, selectedCitation});

    expect(html).not.toMatch(/type="radio"[^>]*checked/);
    expect(html).toContain(expectedReason);
    expect(radios[0]?.props.disabled).toBeFalsy();
    expect(baseProps.onCitationChange).not.toHaveBeenCalled();
  });

  it('selects metadata only and never calls answer draft, save, or submit actions', () => {
    const onCitationChange = vi.fn<(citation: ApplicationStoryCitation | null) => void>();
    const answerDraft = vi.fn();
    const saveAnswer = vi.fn();
    const submitApplication = vi.fn();
    const extendedProps = {
      ...baseProps,
      onCitationChange,
      onAnswerDraft: answerDraft,
      onSaveAnswer: saveAnswer,
      onSubmitApplication: submitApplication,
    };
    const {html, radios} = renderPanel(extendedProps);

    expect(radios).toHaveLength(1);
    radios[0]?.props.onChange?.({target: {checked: true}});

    expect(onCitationChange).toHaveBeenCalledExactlyOnceWith(expectedCitation);
    const serialized = JSON.stringify(onCitationChange.mock.calls[0]?.[0]);
    expect(serialized).not.toContain('Restored service availability demonstrated');
    expect(serialized).not.toMatch(/answerText|answer_text|draft/i);
    expect(answerDraft).not.toHaveBeenCalled();
    expect(saveAnswer).not.toHaveBeenCalled();
    expect(submitApplication).not.toHaveBeenCalled();
    expect(html).toContain('will not fill answer text or create an application answer');
  });

  it('disables stale or foreign story records before they can emit a citation', () => {
    const onCitationChange = vi.fn<(citation: ApplicationStoryCitation | null) => void>();
    const invalidCandidates: CareerStorySelectionCandidate[] = [
      {...currentCandidate, revisionId: 'stale-fact-revision', isLatest: false, reusable: false,
        staleFactIds: ['fact-availability']},
      {...currentCandidate, revisionId: 'unapproved-revision', reviewStatus: 'PENDING'},
    ];

    for (const candidate of invalidCandidates) {
      const {radios} = renderPanel({...baseProps, candidates: [candidate], onCitationChange});
      expect(radios[0]?.props.disabled).toBe(true);
      radios[0]?.props.onChange?.({target: {checked: true}});
    }

    expect(onCitationChange).not.toHaveBeenCalled();
  });
});
