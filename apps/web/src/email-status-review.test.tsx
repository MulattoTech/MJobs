import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  EmailStatusReviewContent,
  EMAIL_STATUS_MAX_BYTES,
  emailStatusStateForScope,
  isEmailStatusMessageWithinLimit,
  isEmailStatusStaleConflict,
  isReviewableEmailStatusPreview,
  requestIdForEmailStatusRetry,
  type EmailStatusHistory,
  type EmailStatusPreview,
} from './email-status-review';

const preview: EmailStatusPreview = {
  preview_id: 'preview-1',
  content_sha256: 'a'.repeat(64),
  category: 'INTERVIEW_INVITATION',
  suggested_status: 'INTERVIEW',
  reason_codes: ['EXPLICIT_INTERVIEW_INVITATION'],
  expected_tracking_sequence: 4,
  attribution: 'USER_REPORTED_EMAIL',
};

const history: EmailStatusHistory = {
  items: [{
    preview_id: 'preview-old',
    content_sha256: 'b'.repeat(64),
    category: 'CONFIRMATION',
    suggested_status: 'APPLIED',
    reason_codes: ['EXPLICIT_CONFIRMATION'],
    decision: 'APPLIED',
    reported_status: 'APPLIED',
    attribution: 'USER_REPORTED_EMAIL',
    sequence: 3,
    created_at: '2026-10-11T10:00:00Z',
  }],
};

const baseProps = {
  draft: '',
  format: 'text' as const,
  fileSelected: false,
  preview: null,
  history: { items: [] },
  loading: false,
  busy: false,
  confirmed: false,
  stale: false,
  error: null,
  onDraftChange: vi.fn(),
  onFormatChange: vi.fn(),
  onSelectFile: vi.fn(),
  onClearFile: vi.fn(),
  onPreview: vi.fn(),
  onConfirm: vi.fn(),
  onRefresh: vi.fn(),
  onConfirmationChange: vi.fn(),
};

describe('reviewed email status UI', () => {
  it('supports manual text and .eml import while explaining the local review boundary', () => {
    const html = renderToStaticMarkup(<EmailStatusReviewContent {...baseProps} />);

    expect(html).toContain('Paste email text');
    expect(html).toContain('Choose a .eml or text file');
    expect(html).toContain('No mailbox is connected');
    expect(html).toContain('Email content is analyzed as text; links and attachments are never opened.');
    expect(html).not.toContain('type="password"');
  });

  it('shows a fixed suggestion and requires explicit user-reported confirmation', () => {
    const html = renderToStaticMarkup(<EmailStatusReviewContent {...baseProps} preview={preview} />);

    expect(html).toContain('Suggested status: Interview');
    expect(html).toContain('This is a status you report from the email, not a verified employer receipt.');
    expect(html).toContain('I reviewed this email and want to record the suggested status.');
    expect(html).toContain('Confirm reported status');
    expect(html).toContain('disabled=""');
  });

  it('does not allow confirmation for unknown or unsafe suggestions', () => {
    const ambiguous = { ...preview, category: 'AMBIGUOUS', suggested_status: null, reason_codes: ['CONFLICTING_SIGNALS'] };
    expect(isReviewableEmailStatusPreview(ambiguous)).toBe(false);
    const html = renderToStaticMarkup(<EmailStatusReviewContent {...baseProps} preview={ambiguous} />);
    expect(html).toContain('No status can be safely suggested from this message.');
    expect(html).not.toContain('Confirm reported status');
  });

  it('shows sanitized persistent history without source hashes or message content', () => {
    const html = renderToStaticMarkup(<EmailStatusReviewContent {...baseProps} history={history} />);

    expect(html).toContain('Reported status history');
    expect(html).toContain('Application confirmed');
    expect(html).toContain('Reported by you from email');
    expect(html).not.toContain('b'.repeat(64));
  });

  it('handles stale tracking conflicts with refresh guidance and never echoes API error text', () => {
    expect(isEmailStatusStaleConflict({ status: 409, code: 'EMAIL_STATUS_STALE_PREVIEW' })).toBe(true);
    expect(isEmailStatusStaleConflict({ status: 409, code: 'EMAIL_STATUS_REQUEST_CONFLICT' })).toBe(false);
    const html = renderToStaticMarkup(<EmailStatusReviewContent {...baseProps} stale error="private raw API detail" />);
    expect(html).toContain('Application status changed since this preview. Refresh history and review the email again.');
    expect(html).toContain('Refresh history');
    expect(html).not.toContain('private raw API detail');
  });

  it('exposes clear loading/history states and documents the size limit', () => {
    expect(EMAIL_STATUS_MAX_BYTES).toBe(256 * 1024);
    expect(isEmailStatusMessageWithinLimit('a'.repeat(EMAIL_STATUS_MAX_BYTES))).toBe(true);
    expect(isEmailStatusMessageWithinLimit('a'.repeat(EMAIL_STATUS_MAX_BYTES + 1))).toBe(false);
    expect(isEmailStatusMessageWithinLimit('é'.repeat(EMAIL_STATUS_MAX_BYTES))).toBe(false);
    const loading = renderToStaticMarkup(<EmailStatusReviewContent {...baseProps} loading />);
    expect(loading).toContain('Loading reported status history');
    const empty = renderToStaticMarkup(<EmailStatusReviewContent {...baseProps} />);
    expect(empty).toContain('No email status reports have been recorded for this application.');
  });

  it('drops private draft and preview state immediately when the application scope changes', () => {
    const prior = {
      scopeKey: 'profile-a\u0000application-a', draft: 'private email body', format: 'eml' as const,
      fileSelected: true, preview, history, loading: false, busy: false, confirmed: true,
      stale: false, error: '', success: '',
    };
    const next = emailStatusStateForScope(prior, 'profile-b\u0000application-b');
    expect(next.draft).toBe('');
    expect(next.preview).toBeNull();
    expect(next.fileSelected).toBe(false);
    expect(next.confirmed).toBe(false);
  });

  it('reuses the same idempotency request on retry of one preview and rotates for a new preview', () => {
    const existing = { scopeKey: 'p\u0000a', previewId: 'preview-1', requestId: 'request-1' };
    const create = vi.fn(() => 'request-2');
    expect(requestIdForEmailStatusRetry(existing, 'p\u0000a', 'preview-1', create)).toBe(existing);
    expect(create).not.toHaveBeenCalled();
    expect(requestIdForEmailStatusRetry(existing, 'p\u0000a', 'preview-2', create)).toEqual({
      scopeKey: 'p\u0000a', previewId: 'preview-2', requestId: 'request-2',
    });
    expect(create).toHaveBeenCalledOnce();
  });

  it('escapes pasted HTML instead of rendering it as active email markup', () => {
    const html = renderToStaticMarkup(<EmailStatusReviewContent {...baseProps} draft={'<img src=x onerror="alert(1)">'} />);
    expect(html).toContain('&lt;img');
    expect(html).not.toContain('<img');
    expect(html).not.toContain('onerror="');
  });
});
