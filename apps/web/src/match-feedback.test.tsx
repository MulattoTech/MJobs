import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  FEEDBACK_CATEGORIES,
  MatchFeedbackView,
  resolveFeedbackRequestId,
  resolvePreferenceRequestId,
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
    error: null, retryable: false, history: [], appliedPreferences: [], savedFeedback: null, preferenceProposal: null,
    draftProposal: null, proposalEditing: false,
    preferenceApplyPhase: 'idle', preferenceApplyError: null, preferenceApplyRetryable: false,
    preferenceHistoryError: null,
    onCategoryChange: vi.fn(), onSave: vi.fn(), onRetry: vi.fn(),
    onReviewProposal: vi.fn(), onProposalChange: vi.fn(),
    onApplyPreference: vi.fn(), onRetryPreference: vi.fn(), ...overrides,
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

  it('requires review before applying a deterministic preference and never changes hard rules', () => {
    const edit = vi.fn();
    const apply = vi.fn();
    const tree = MatchFeedbackView(props({
      phase: 'saved', savedFeedback: feedback, history: [feedback],
      preferenceProposal: proposal, draftProposal: proposal, proposalEditing: true,
      onProposalChange: edit, onApplyPreference: apply,
    }));
    const html = renderToStaticMarkup(tree);
    expect(html).toContain('aria-label="Preference proposal"');
    expect(html).toContain('Review preference proposal');
    expect(html).toContain('only changes advisory priority after you explicitly apply it');
    expect(html).toContain('Linux matches were helpful.');
    expect(html).toContain('aria-label="Proposed dimension"');
    expect(html).toContain('aria-label="Proposed direction"');
    expect(html).toContain('aria-label="Apply reviewed preference"');
    findElement(tree, element => element.props['aria-label'] === 'Apply reviewed preference')?.props.onClick();
    expect(apply).toHaveBeenCalledOnce();
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

  it('shows read-back applied preferences without exposing request identifiers', () => {
    const html = renderToStaticMarkup(<MatchFeedbackView {...props({
      appliedPreferences: [{
        id: 'preference-1', job_id: 'job-a', company_id: 'company-a', seniority_level: null, feedback_id: feedback.id,
        category: 'BAD_SALARY', dimension: 'salary', direction: 'DECREASE',
        created_at: '2026-10-09T11:00:00Z',
      }],
    })} />);
    expect(html).toContain('aria-label="Applied profile preferences"');
    expect(html).toContain('salary: decrease weight');
    expect(html).not.toContain('request-id');
  });

  it('shows when profile preference history could not be loaded', () => {
    const html = renderToStaticMarkup(<MatchFeedbackView {...props({
      preferenceHistoryError: 'Applied preference history could not be loaded.',
    })} />);
    expect(html).toContain('role="alert"');
    expect(html).toContain('Applied preference history could not be loaded.');
  });

  it('reuses preference apply request IDs on retry and replaces them after edits', () => {
    const create = vi.fn().mockReturnValueOnce('apply-a').mockReturnValueOnce('apply-b');
    const first = resolvePreferenceRequestId(
      null, 'profile-a', 'feedback-a', 'salary', 'decrease', create,
    );
    const retry = resolvePreferenceRequestId(
      first.context, 'profile-a', 'feedback-a', 'salary', 'decrease', create,
    );
    const edited = resolvePreferenceRequestId(
      retry.context, 'profile-a', 'feedback-a', 'salary', 'increase', create,
    );
    expect(first.requestId).toBe('apply-a');
    expect(retry.requestId).toBe('apply-a');
    expect(edited.requestId).toBe('apply-b');
    expect(create).toHaveBeenCalledTimes(2);
  });
});
