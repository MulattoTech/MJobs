import React, { Children, isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  ApplicationAnswerMatching,
  type ApplicationAnswerMatchCandidate,
  type ApplicationAnswerMatchingProps,
  type ApplicationAnswerMatchResponse,
} from './application-answer-matching';

function findButtons(node: ReactNode): ReactElement<{
  children?: ReactNode;
  onClick?: () => void;
  'aria-label'?: string;
}>[] {
  return Children.toArray(node).flatMap(child => {
    if (!isValidElement<{
      children?: ReactNode;
      onClick?: () => void;
      'aria-label'?: string;
    }>(child)) return [];
    const current = child.type === 'button' ? [child] : [];
    return [...current, ...findButtons(child.props.children)];
  });
}

const candidate = {
  answer_id: 'answer-1',
  method: 'CURATED_SEMANTIC' as const,
  score: 0.95,
  saved_question: 'Can you work weekends?',
  answer: 'Yes, with advance notice.',
  source: 'Reviewed profile answer',
  confidence: 94,
  verified: true,
  user_approved: true,
};

const response: ApplicationAnswerMatchResponse = {
  profile_id: 'profile-1',
  application_id: 'application-1',
  question: 'Are you available to work weekends?',
  candidates: [candidate],
};

const baseProps: ApplicationAnswerMatchingProps = {
  profileId: 'profile-1',
  applicationId: 'application-1',
  question: response.question,
  response: null,
  onFindMatches: vi.fn(),
  onRetry: vi.fn(),
  onSelectDraft: vi.fn(),
};

describe('application answer matching picker', () => {
  it('shows ranked match evidence and approval provenance without selecting or exporting', () => {
    const markup = renderToStaticMarkup(
      <ApplicationAnswerMatching {...baseProps} response={response} />,
    );

    expect(markup).toContain('aria-label="Answer match 1"');
    expect(markup).toContain('Rank 1');
    expect(markup).toContain('Curated semantic match');
    expect(markup).toContain('95%');
    expect(markup).toContain('Why it matched');
    expect(markup).toContain(candidate.saved_question);
    expect(markup).toContain(candidate.source);
    expect(markup).toContain('Verified and approved for reuse');
    expect(markup).toContain('94% confidence');
    expect(markup).toContain('Unreviewed draft');
    expect(markup).not.toContain('Export');
    expect(baseProps.onSelectDraft).not.toHaveBeenCalled();
  });

  it('adds only the deliberately selected candidate to the draft callback', () => {
    const onSelectDraft = vi.fn();
    const props = { ...baseProps, response, onSelectDraft };
    const buttons = findButtons(ApplicationAnswerMatching(props));
    const selectButton = buttons.find(button => button.props['aria-label'] === 'Select answer as draft: Yes, with advance notice.');

    expect(selectButton).toBeDefined();
    expect(onSelectDraft).not.toHaveBeenCalled();
    selectButton?.props.onClick?.();

    expect(onSelectDraft).toHaveBeenCalledExactlyOnceWith(candidate, props.question);
  });

  it('offers an explicit lookup action and does not auto-match on render', () => {
    const onFindMatches = vi.fn();
    const props = { ...baseProps, onFindMatches };
    const markup = renderToStaticMarkup(<ApplicationAnswerMatching {...props} />);

    expect(markup).toContain('Find saved answer matches');
    expect(markup).not.toContain(candidate.answer);
    expect(onFindMatches).not.toHaveBeenCalled();
    const findButton = findButtons(ApplicationAnswerMatching(props))
      .find(button => button.props['aria-label'] === 'Find saved answer matches');
    findButton?.props.onClick?.();
    expect(onFindMatches).toHaveBeenCalledExactlyOnceWith();
  });

  it('does not display candidates from another profile, application, or question', () => {
    const mismatches: ApplicationAnswerMatchResponse[] = [
      { ...response, profile_id: 'other-profile' },
      { ...response, application_id: 'other-application' },
      { ...response, question: 'A different question?' },
    ];

    for (const mismatched of mismatches) {
      const markup = renderToStaticMarkup(
        <ApplicationAnswerMatching {...baseProps} response={mismatched} />,
      );
      expect(markup).toContain('could not be safely matched to this profile, application, and question');
      expect(markup).not.toContain(candidate.answer);
      expect(markup).not.toContain('Select answer as draft');
    }
  });

  it('fails closed when a candidate is unapproved or unverified', () => {
    const rejectedCandidates = [
      { ...candidate, user_approved: false },
      { ...candidate, verified: false },
      { ...candidate, confidence: 79 },
      { ...candidate, method: 'UNSUPPORTED' as ApplicationAnswerMatchCandidate['method'] },
      { ...candidate, user_approved: undefined as unknown as boolean },
    ];

    for (const rejected of rejectedCandidates) {
      const markup = renderToStaticMarkup(
        <ApplicationAnswerMatching {...baseProps} response={{ ...response, candidates: [rejected] }} />,
      );
      expect(markup).toContain('No eligible saved answer matches were found.');
      expect(markup).not.toContain(candidate.answer);
    }
  });

  it('shows accessible loading, empty, error, and retry states without selection side effects', () => {
    const loading = renderToStaticMarkup(<ApplicationAnswerMatching {...baseProps} loading />);
    expect(loading).toContain('role="status"');
    expect(loading).toContain('Loading saved answer matches');

    const empty = renderToStaticMarkup(<ApplicationAnswerMatching {...baseProps} response={{ ...response, candidates: [] }} />);
    expect(empty).toContain('No eligible saved answer matches were found.');

    const onRetry = vi.fn();
    const errorProps = { ...baseProps, error: 'Could not load saved answer matches. Try again.', onRetry };
    const errorMarkup = renderToStaticMarkup(<ApplicationAnswerMatching {...errorProps} />);
    expect(errorMarkup).toContain('role="alert"');
    expect(errorMarkup).toContain(errorProps.error);
    const retryButton = findButtons(ApplicationAnswerMatching(errorProps))
      .find(button => button.props['aria-label'] === 'Retry saved answer matches');
    retryButton?.props.onClick?.();

    expect(onRetry).toHaveBeenCalledExactlyOnceWith();
    expect(baseProps.onSelectDraft).not.toHaveBeenCalled();
  });
});
