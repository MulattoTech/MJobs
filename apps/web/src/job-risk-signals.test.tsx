import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  JobRiskSignalsPanelView,
  requestJobRiskSignals,
  type JobRiskSignalsClient,
  type JobRiskSignalsResponse,
} from './job-risk-signals';

const response: JobRiskSignalsResponse = {
  profile_id: 'profile-1',
  job_id: 'job-9',
  signals: [{
    code: 'PAYMENT_REQUEST',
    label: 'Potential Risk',
    severity: 'ADVISORY',
    reason: 'The posting asks applicants to pay a processing fee.',
    evidence_source: 'job description text',
    observed_value: 'Pay a $45 processing fee to continue',
  }],
  unknowns: [{ code: 'COMPANY_IDENTITY', reason: 'Company identity could not be verified from the supplied job record.' }],
};

describe('job risk signals panel', () => {
  it('renders loading state without a blocking or rejection action', () => {
    const html = renderToStaticMarkup(<JobRiskSignalsPanelView
      profileId="profile-1"
      jobId="job-9"
      state={{ status: 'loading' }}
      onRetry={() => {}}
    />);

    expect(html).toContain('Loading risk signals');
    expect(html).not.toMatch(/<button[^>]*>[^<]*(reject|block|dismiss|enable collection)/i);
  });

  it('shows errors with an explicit retry action and does not retry during rendering', () => {
    const onRetry = vi.fn();
    const html = renderToStaticMarkup(<JobRiskSignalsPanelView
      profileId="profile-1"
      jobId="job-9"
      state={{ status: 'error', error: 'Risk signals are unavailable.' }}
      onRetry={onRetry}
    />);

    expect(html).toContain('Risk signals are unavailable.');
    expect(html).toContain('Retry risk check');
    expect(onRetry).not.toHaveBeenCalled();
    expect(html).not.toMatch(/<button[^>]*>[^<]*(reject|block posting)/i);
  });

  it('labels heuristic evidence as Potential Risk and retains UNKNOWN with its reason', () => {
    const html = renderToStaticMarkup(<JobRiskSignalsPanelView
      profileId="profile-1"
      jobId="job-9"
      state={{ status: 'ready', data: response }}
      onRetry={() => {}}
    />);

    expect(html).toContain('Potential Risk');
    expect(html).toContain('The posting asks applicants to pay a processing fee.');
    expect(html).toContain('Evidence source: job description text');
    expect(html).toContain('Pay a $45 processing fee to continue');
    expect(html).toContain('Unknown signal: COMPANY_IDENTITY');
    expect(html).toContain('Company identity could not be verified from the supplied job record.');
    expect(html).toContain('heuristic');
    expect(html).toContain('do not determine whether a posting is fraudulent');
    expect(html).not.toContain('<a ');
    expect(html).not.toContain('href=');
    expect(html).not.toMatch(/<button[^>]*>[^<]*(reject|block|enable collection)/i);
  });

  it('keeps a clean empty result distinct from unknown signals', () => {
    const html = renderToStaticMarkup(<JobRiskSignalsPanelView
      profileId="profile-1"
      jobId="job-9"
      state={{ status: 'ready', data: { ...response, signals: [], unknowns: [] } }}
      onRetry={() => {}}
    />);

    expect(html).toContain('No potential risk signals were reported.');
    expect(html).toContain('This is not a guarantee that the posting is safe.');
  });

  it('uses the injected profile/job-scoped client and rejects mismatched response scope', async () => {
    const client: JobRiskSignalsClient = { get: vi.fn().mockResolvedValue(response) };

    await expect(requestJobRiskSignals(client, 'profile-1', 'job-9')).resolves.toEqual(response);
    expect(client.get).toHaveBeenCalledOnce();
    expect(client.get).toHaveBeenCalledWith('profile-1', 'job-9');

    const mismatch: JobRiskSignalsClient = {
      get: vi.fn().mockResolvedValue({ ...response, profile_id: 'other-profile' }),
    };
    await expect(requestJobRiskSignals(mismatch, 'profile-1', 'job-9')).rejects.toThrow(/profile and job/i);
  });
});
