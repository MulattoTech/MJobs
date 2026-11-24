import { describe, expect, it } from 'vitest';
import { preserveRemoteResidenceEvidence, remoteResidenceLabel } from './remote-residence-evidence';

describe('remote residence evidence', () => {
  it('labels the saved state and eligibility outcome', () => {
    expect(remoteResidenceLabel({ state: 'OK', status: 'ELIGIBLE' }))
      .toBe('Remote eligibility confirmed for OK');
    expect(remoteResidenceLabel({ state: 'OK', status: 'UNKNOWN' }))
      .toBe('Remote eligibility unknown for OK');
    expect(remoteResidenceLabel({ state: 'OK', status: 'EXCLUDED' }))
      .toBe('Remote eligibility excludes OK');
  });

  it('retains the selected search eligibility when job detail readback omits it', () => {
    const job = { id: 'job-1', title: 'Infrastructure Engineer' };
    const evidence = { state: 'OK', status: 'ELIGIBLE' as const };

    expect(preserveRemoteResidenceEvidence(job, evidence)).toEqual({
      ...job,
      search_evidence: { remote_residence: evidence },
    });
  });

  it('uses evidence returned by detail readback when present', () => {
    const detailEvidence = { state: 'TX', status: 'UNKNOWN' as const };
    const listEvidence = { state: 'OK', status: 'ELIGIBLE' as const };
    const job = { id: 'job-1', search_evidence: { remote_residence: detailEvidence } };

    expect(preserveRemoteResidenceEvidence(job, listEvidence)).toBe(job);
  });
});
