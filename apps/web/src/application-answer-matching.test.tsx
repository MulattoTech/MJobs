import React, { Children, isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import type {
  ApplicationAnswerMatchCandidate,
  ApplicationAnswerMatchResponse,
  ApplicationAnswerMatchScope,
  ApplicationAnswerSuggestionDraft,
} from './application-answer-matching-client';
import {
  ApplicationAnswerMatching,
  type ApplicationAnswerMatchingProps,
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

const scope: ApplicationAnswerMatchScope = {
  profileId: 'profile-1',
  applicationId: 'application-1',
  question: 'Are you available to work weekends?',
};

const candidate: ApplicationAnswerMatchCandidate = {
  answer_id: 'answer-1',
  question: 'Can you work weekends?',
  text: 'Yes, with advance notice.',
  source: 'Reviewed profile answer',
  confidence: 94,
  verified: true,
  user_approved: true,
  method: 'CURATED_SEMANTIC',
  score: 0.95,
  explanation: 'Question wording matches the curated weekend availability paraphrase.',
};

const response: ApplicationAnswerMatchResponse = {
  scope,
  profile_id: scope.profileId,
  application_id: scope.applicationId,
  question: scope.question,
  candidates: [candidate],
};

const baseProps: ApplicationAnswerMatchingProps = {
  profileId: scope.profileId,
  applicationId: scope.applicationId,
  question: scope.question,
  response: null,
  onFindMatches: vi.fn(),
  onRetry: vi.fn(),
  onSelectDraft: vi.fn(),
};

describe('application answer matching picker', () => {
  it('renders the client candidate shape with explanation, provenance, score, and review status', () => {
    const markup = renderToStaticMarkup(
      <ApplicationAnswerMatching {...baseProps} response={response} />,
    );

    expect(markup).toContain('aria-label="Answer match 1"');
    expect(markup).toContain('Rank 1');
    expect(markup).toContain('Curated semantic match');
    expect(markup).toContain('95%');
    expect(markup).toContain(candidate.explanation);
    expect(markup).toContain(candidate.question);
    expect(markup).toContain(candidate.text);
    expect(markup).toContain(candidate.source);
    expect(markup).toContain('Verified and approved for reuse');
    expect(markup).toContain('94% confidence');
    expect(markup).toContain('Unreviewed draft');
    expect(markup).not.toContain('Export');
    expect(baseProps.onSelectDraft).not.toHaveBeenCalled();
  });

  it('creates a fresh unreviewed draft only after explicit selection', () => {
    const onSelectDraft = vi.fn<(draft: ApplicationAnswerSuggestionDraft) => void>();
    const props = { ...baseProps, response, onSelectDraft };
    const selectButton = findButtons(ApplicationAnswerMatching(props))
      .find(button => button.props['aria-label'] === 'Select answer as draft: Yes, with advance notice.');

    expect(selectButton).toBeDefined();
    expect(onSelectDraft).not.toHaveBeenCalled();
    selectButton?.props.onClick?.();

    expect(onSelectDraft).toHaveBeenCalledExactlyOnceWith({
      answer_id: candidate.answer_id,
      text: candidate.text,
      profile_id: scope.profileId,
      application_id: scope.applicationId,
      question: scope.question,
      reviewed: false,
      method: candidate.method,
      score: candidate.score,
      saved_question: candidate.question,
      source: candidate.source,
      confidence: candidate.confidence,
      verified: true,
      user_approved: true,
      explanation: candidate.explanation,
    });
  });

  it('offers an explicit lookup and never matches automatically on render', () => {
    const onFindMatches = vi.fn();
    const props = { ...baseProps, onFindMatches };
    const markup = renderToStaticMarkup(<ApplicationAnswerMatching {...props} />);

    expect(markup).toContain('Find saved answer matches');
    expect(markup).not.toContain(candidate.text);
    expect(onFindMatches).not.toHaveBeenCalled();
    findButtons(ApplicationAnswerMatching(props))
      .find(button => button.props['aria-label'] === 'Find saved answer matches')?.props.onClick?.();
    expect(onFindMatches).toHaveBeenCalledExactlyOnceWith();
  });

  it('does not render results when profile, application, or question scope is stale', () => {
    const staleResponses: ApplicationAnswerMatchResponse[] = [
      { ...response, profile_id: 'other-profile' },
      { ...response, application_id: 'other-application' },
      { ...response, question: 'A different question?' },
      { ...response, scope: { ...scope, applicationId: 'other-application' } },
    ];

    for (const staleResponse of staleResponses) {
      const markup = renderToStaticMarkup(
        <ApplicationAnswerMatching {...baseProps} response={staleResponse} />,
      );
      expect(markup).toContain('could not be safely matched to this profile, application, and question');
      expect(markup).not.toContain(candidate.text);
      expect(markup).not.toContain('Select answer as draft');
    }
  });

  it('hides invalid or unapproved candidates even if a response is malformed', () => {
    const rejectedCandidates: ApplicationAnswerMatchCandidate[] = [
      { ...candidate, user_approved: false },
      { ...candidate, verified: false },
      { ...candidate, confidence: 79 },
      { ...candidate, score: 0.74 },
    ];

    for (const rejected of rejectedCandidates) {
      const markup = renderToStaticMarkup(
        <ApplicationAnswerMatching {...baseProps} response={{ ...response, candidates: [rejected] }} />,
      );
      expect(markup).toContain('No eligible saved answer matches were found.');
      expect(markup).not.toContain(candidate.text);
    }
  });

  it('shows accessible loading, empty, error, and retry states with no draft side effects', () => {
    const loading = renderToStaticMarkup(<ApplicationAnswerMatching {...baseProps} loading />);
    expect(loading).toContain('role="status"');
    expect(loading).toContain('Loading saved answer matches');

    const empty = renderToStaticMarkup(
      <ApplicationAnswerMatching {...baseProps} response={{ ...response, candidates: [] }} />,
    );
    expect(empty).toContain('No eligible saved answer matches were found.');

    const onRetry = vi.fn();
    const errorProps = { ...baseProps, error: 'Could not load saved answer matches. Try again.', onRetry };
    const markup = renderToStaticMarkup(<ApplicationAnswerMatching {...errorProps} />);
    expect(markup).toContain('role="alert"');
    expect(markup).toContain(errorProps.error);
    findButtons(ApplicationAnswerMatching(errorProps))
      .find(button => button.props['aria-label'] === 'Retry saved answer matches')?.props.onClick?.();
    expect(onRetry).toHaveBeenCalledExactlyOnceWith();
    expect(baseProps.onSelectDraft).not.toHaveBeenCalled();
  });
});
