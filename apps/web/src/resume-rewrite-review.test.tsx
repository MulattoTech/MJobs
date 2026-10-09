import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {describe, expect, it, vi} from 'vitest';
import {
  ResumeRewritePreviewRows,
  ResumeRewriteReviewPreview,
  isResumeRewriteAcknowledgementAllowed,
  prepareResumeRewriteReviewPreview,
  protectedTokenAuditGroups,
  resumeRewriteFailureMessage,
  resumeRewriteReviewPreviewUrl,
  resumeRewriteScopeSignature,
  type ResumeRewriteReviewPreviewResponse,
} from './resume-rewrite-review';

const fact = {
  fact_id: 'fact-17', kind: 'experience', text: 'Maintained 250+ servers from 2022 through 2025; held CCNA.',
  source: 'Verified employment record', confidence: 95, verified: true, user_approved: true,
};
const rewrite = {fact_id: 'fact-17', text: 'Supported 250+ servers during the 2022–2025 period and maintained CCNA.'};
const preview: ResumeRewriteReviewPreviewResponse = {
  profile_id: 'profile/7', profile_version: 'p'.repeat(64), job_id: 'job 9', job_fingerprint: 'j'.repeat(64),
  selected_fact_ids: ['fact-17'], read_only: true, review_required: true, semantic_equivalence: 'USER_REVIEW_REQUIRED',
  rewrites: [{
    fact_id: 'fact-17', fact_kind: 'experience', source_text: fact.text, source_sha256: 'a'.repeat(64),
    proposed_text: rewrite.text,
    protected_token_audit: {
      required: [
        {type: 'NUMBER', value: '250+'},
        {type: 'DATE', value: '2022 through 2025'},
        {type: 'NAMED_CREDENTIAL', value: 'CCNA'},
      ],
      retained: [
        {type: 'NUMBER', value: '250+'},
        {type: 'DATE', value: '2022–2025'},
        {type: 'NAMED_CREDENTIAL', value: 'CCNA'},
      ],
      missing: [],
    },
  }],
};
const input = {
  profileId: 'profile/7', jobId: 'job 9', profileVersion: 'p'.repeat(64), expectedJobFingerprint: 'j'.repeat(64),
  selectedFactIds: ['fact-17'], facts: [fact], rewrites: [rewrite],
};
const props = {...input, client: async () => preview, onReviewChange: () => undefined};

describe('resume rewrite review preview', () => {
  it('builds an encoded profile/job-scoped preview URL', () => {
    expect(resumeRewriteReviewPreviewUrl('profile/7', 'job 9'))
      .toBe('/profiles/profile%2F7/jobs/job%209/resume-rewrite-review-preview');
  });

  it('posts ordered selected fact IDs and exact proposals, then verifies scope and read-only response', async () => {
    const calls: {path: string; init?: RequestInit}[] = [];
    const result = await prepareResumeRewriteReviewPreview(input, async (path, init) => {
      calls.push({path, init});
      return preview;
    });
    expect(calls).toHaveLength(1);
    expect(calls[0].path).toBe('/profiles/profile%2F7/jobs/job%209/resume-rewrite-review-preview');
    expect(calls[0].init?.method).toBe('POST');
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      expected_profile_version: 'p'.repeat(64), expected_job_fingerprint: 'j'.repeat(64),
      selected_fact_ids: ['fact-17'], rewrites: [{fact_id: 'fact-17', text: rewrite.text}],
    });
    expect(result).toEqual(preview);
  });

  it('fails closed when scope, selected identity, source, proposal, hash, or review caveat differs', async () => {
    const request = (response: unknown) => prepareResumeRewriteReviewPreview(input, async () => response);
    await expect(request({...preview, job_fingerprint: 'stale'})).rejects.toThrow(/current profile, job and approved facts/);
    await expect(request({...preview, selected_fact_ids: ['fact-other']})).rejects.toThrow(/selected fact IDs/);
    await expect(request({...preview, rewrites: [{...preview.rewrites[0], source_text: 'changed'}]}))
      .rejects.toThrow(/source text or proposed wording/);
    await expect(request({...preview, rewrites: [{...preview.rewrites[0], proposed_text: 'changed'}]}))
      .rejects.toThrow(/source text or proposed wording/);
    await expect(request({...preview, rewrites: [{...preview.rewrites[0], source_sha256: 'invalid'}]}))
      .rejects.toThrow(/source text or proposed wording/);
    await expect(request({...preview, semantic_equivalence: 'EQUIVALENT'})).rejects.toThrow(/USER_REVIEW_REQUIRED/);
  });

  it('requires one rewrite for each selected approved fact in the same order', async () => {
    const client = vi.fn(async () => preview);
    await expect(prepareResumeRewriteReviewPreview({
      ...input, selectedFactIds: ['fact-17', 'fact-18'], facts: [fact, {...fact, fact_id: 'fact-18'}],
    }, client)).rejects.toThrow(/every selected approved fact in the same order/);
    expect(client).not.toHaveBeenCalled();
  });

  it('shows exact source provenance, hash and missing-token blocker without checking acknowledgement', () => {
    const row = {...preview.rewrites[0], protected_token_audit: {
      ...preview.rewrites[0].protected_token_audit,
      missing: [{type: 'NAMED_CREDENTIAL' as const, value: 'CCNA'}],
    }};
    const html = renderToStaticMarkup(<ResumeRewritePreviewRows
      rows={[row]} facts={[fact]} reviews={{'fact-17': false}} disabled={false} onReviewChange={() => undefined}/>
    );
    expect(html).toContain('Verified employment record');
    expect(html).toContain('95% confidence');
    expect(html).toContain('Verified');
    expect(html).toContain('Approved for document use');
    expect(html).toContain(fact.text);
    expect(html).toContain(rewrite.text);
    expect(html).toContain('a'.repeat(64));
    expect(html).toContain('Missing NAMED CREDENTIAL tokens: CCNA');
    expect(html).toContain('A protected token is missing');
    expect(html).toContain('Review acknowledgement is blocked');
    expect(html).toMatch(/type="checkbox"[^>]*disabled/);
    expect(html).not.toMatch(/type="checkbox"[^>]*checked/);
  });

  it('groups DATE, NUMBER, and NAMED_CREDENTIAL audits and only permits acknowledgement when missing is empty', () => {
    const groups = protectedTokenAuditGroups({
      required: [
        {type: 'DATE', value: '2022 through 2025'}, {type: 'NUMBER', value: '250+'},
        {type: 'NAMED_CREDENTIAL', value: 'CCNA'},
      ],
      retained: [{type: 'DATE', value: '2022–2025'}, {type: 'NUMBER', value: '250+'}],
      missing: [{type: 'NAMED_CREDENTIAL', value: 'CCNA'}],
    });
    expect(groups.map(group => [group.type, group.missing])).toEqual([
      ['DATE', []], ['NUMBER', []], ['NAMED_CREDENTIAL', ['CCNA']],
    ]);
    expect(isResumeRewriteAcknowledgementAllowed(preview.rewrites[0])).toBe(true);
    expect(isResumeRewriteAcknowledgementAllowed({...preview.rewrites[0], protected_token_audit: {
      ...preview.rewrites[0].protected_token_audit, missing: [{type: 'DATE', value: '2022 through 2025'}],
    }})).toBe(false);
  });

  it('invalidates scope signatures when source/proposed wording changes and maps stale failures clearly', () => {
    expect(resumeRewriteScopeSignature(input)).not.toBe(resumeRewriteScopeSignature({
      ...input, facts: [{...fact, text: `${fact.text} Updated`}],
    }));
    expect(resumeRewriteScopeSignature(input)).not.toBe(resumeRewriteScopeSignature({
      ...input, rewrites: [{...rewrite, text: `${rewrite.text} Updated`}],
    }));
    expect(resumeRewriteFailureMessage(new Error('RESUME_REWRITE_JOB_STALE')))
      .toMatch(/job changed.*refresh/i);
    expect(resumeRewriteFailureMessage(new Error('UNSUPPORTED_CLAIM')))
      .toMatch(/dates, numbers, or named credentials/i);
  });

  it('does not preview unapproved source facts', () => {
    const html = renderToStaticMarkup(<ResumeRewriteReviewPreview
      {...props} facts={[{...fact, user_approved: false}]}/>);
    expect(html).toMatch(/Preview exact resume rewrites<\/button>/);
    expect(html).toMatch(/<button[^>]*disabled/);
    expect(html).not.toContain(fact.text);
  });

  it('starts with no selected review, explains the semantic caveat, and has no generation/save action', () => {
    const client = vi.fn(async () => preview);
    const html = renderToStaticMarkup(<ResumeRewriteReviewPreview {...props} client={client}/>);
    expect(html).toContain('Resume rewrite review preview');
    expect(html).toContain('USER_REVIEW_REQUIRED');
    expect(html).toContain('No wording has been reviewed yet');
    expect(html).not.toContain(fact.text);
    expect(html).not.toContain(rewrite.text);
    expect(html).not.toMatch(/type="checkbox"[^>]*checked/);
    expect(html).not.toContain('Generate resume');
    expect(html).not.toContain('Save rewrite');
    expect(client).not.toHaveBeenCalled();
  });
});
