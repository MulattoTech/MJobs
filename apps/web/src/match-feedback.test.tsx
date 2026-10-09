import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  FEEDBACK_CATEGORIES,
  MatchFeedbackView,
  resolveFeedbackRequestId,
  type MatchFeedbackViewProps,
} from './match-feedback';

const feedback = {
  id: 'feedback-7', category: 'GOOD_MATCH' as const, created_at: '2026-10-09T10:30:00Z',
};
const proposal = {
  dimension: 'technology' as const, direction: 'increase' as const,
  explanation: 'Linux matches were helpful.', review_required: true as const,
};

function findElement(
  node: React.ReactNode,
  predicate: (element: React.ReactElement<any>) => boolean,
): React.ReactElement<any> | null {
  if (Array.isArray(node)) {
    for (const child of node) {
      const result = findElement(child, predicate);
      if (result) return result;
    }
    return null;
  }
  if (!React.isValidElement(node)) return null;
  const element = node as React.ReactElement<any>;
  if (predicate(element)) return element;
  return findElement(element.props.children, predicate);
}

function props(overrides: Partial<MatchFeedbackViewProps> = {}): MatchFeedbackViewProps {
  return {
    profileId: 'profile-a', jobId: 'job-a', category: null, phase: 'idle',
    error: null, retryable: false, history: [], savedFeedback: null, preferenceProposal: null,
    draftProposal: null, proposalEditing: false,
    onCategoryChange: vi.fn(), onSave: vi.fn(), onRetry: vi.fn(),
    onReviewProposal: vi.fn(), onProposalChange: vi.fn(), ...overrides,
  };
}

describe('match feedback view', () => {
  it('presents all eight explicit categories and requires a category before save', () => {
    const onCategoryChange = vi.fn();
    const onSave = vi.fn();
    const tree = MatchFeedbackView(props({ onCategoryChange, onSave }));
    const html = renderToStaticMarkup(tree);
    expect(html).toContain('aria-label="Job match feedback"');
    expect(html).toContain('aria-label="Feedback category"');
    for (const item of FEEDBACK_CATEGORIES) expect(html).toContain(item.label);
    const save = findElement(tree, element => element.props['aria-label'] === 'Save feedback');
    expect(save?.props.disabled).toBe(true);
    expect(html).not.toContain('Feedback saved and confirmed by readback');
    findElement(tree, element => element.props['aria-label'] === 'Feedback category')
      ?.props.onChange({ currentTarget: { value: 'BAD_SALARY' } });
    expect(onCategoryChange).toHaveBeenCalledWith('BAD_SALARY');
    expect(onSave).not.toHaveBeenCalled();
  });

  it('shows only saved/read-back feedback and omits request IDs from history', () => {
    const html = renderToStaticMarkup(<MatchFeedbackView {...props({
      category: 'GOOD_MATCH', phase: 'saved', savedFeedback: feedback,
      history: [feedback],
    })} />);
    expect(html).toContain('Feedback saved and confirmed by readback');
    expect(html).toContain('Good match');
    expect(html).toContain('2026-10-09T10:30:00Z');
    expect(html).not.toContain('request-stable');
    expect(html).not.toMatch(/(hard filters were changed|preference was applied)/i);
    expect(html).toContain('This feedback does not change hard filters or job eligibility.');
  });

  it('shows a deterministic preference as a locally editable proposal, never an applied rule', () => {
    const edit = vi.fn();
    const tree = MatchFeedbackView(props({
      phase: 'saved', savedFeedback: feedback, history: [feedback],
      preferenceProposal: proposal, draftProposal: proposal, proposalEditing: true,
      onProposalChange: edit,
    }));
    const html = renderToStaticMarkup(tree);
    expect(html).toContain('aria-label="Preference proposal"');
    expect(html).toContain('Review preference proposal');
    expect(html).toContain('This proposal is an editable draft only; it has not been applied.');
    expect(html).toContain('Linux matches were helpful.');
    expect(html).toContain('aria-label="Proposed dimension"');
    expect(html).toContain('aria-label="Proposed direction"');
    expect(html).not.toMatch(/<button[^>]*>(?:Apply proposal|Save preference|Change hard filters)/i);
    const direction = findElement(tree, element => element.props['aria-label'] === 'Proposed direction');
    direction?.props.onChange({ currentTarget: { value: 'review' } });
    expect(edit).toHaveBeenCalledWith({ ...proposal, direction: 'review' });
  });

  it('renders a safe error and exposes an explicit retry action', () => {
    const onRetry = vi.fn();
    const tree = MatchFeedbackView(props({
      phase: 'error', retryable: true,
      error: 'Feedback could not be saved. Retry with the same request.', onRetry,
    }));
    const html = renderToStaticMarkup(tree);
    expect(html).toContain('role="alert"');
    expect(html).toContain('Feedback could not be saved');
    findElement(tree, element => element.props['aria-label'] === 'Retry feedback')?.props.onClick();
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it('does not offer retry for a non-retryable error', () => {
    const html = renderToStaticMarkup(<MatchFeedbackView {...props({
      phase: 'error', retryable: false, error: 'Review the selection and try again.',
    })} />);
    expect(html).toContain('Review the selection and try again.');
    expect(html).not.toContain('Retry feedback');
  });

  it('reuses the retry request ID and creates a new ID when the category or scope changes', () => {
    const create = vi.fn()
      .mockReturnValueOnce('request-a')
      .mockReturnValueOnce('request-b')
      .mockReturnValueOnce('request-c');
    const first = resolveFeedbackRequestId(null, 'profile-a', 'job-a', 'GOOD_MATCH', create);
    const retry = resolveFeedbackRequestId(first.context, 'profile-a', 'job-a', 'GOOD_MATCH', create);
    const nextCategory = resolveFeedbackRequestId(retry.context, 'profile-a', 'job-a', 'BAD_MATCH', create);
    const nextScope = resolveFeedbackRequestId(nextCategory.context, 'profile-b', 'job-a', 'BAD_MATCH', create);
    expect(first.requestId).toBe('request-a');
    expect(retry.requestId).toBe('request-a');
    expect(nextCategory.requestId).toBe('request-b');
    expect(nextScope.requestId).toBe('request-c');
    expect(create).toHaveBeenCalledTimes(3);
  });
});
