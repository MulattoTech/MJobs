export type RemoteResidenceEvidence = {
  state: string;
  status: 'ELIGIBLE' | 'UNKNOWN' | 'EXCLUDED';
};

export function remoteResidenceLabel(evidence: RemoteResidenceEvidence): string {
  if (evidence.status === 'ELIGIBLE') {
    return `Remote eligibility confirmed for ${evidence.state}`;
  }
  if (evidence.status === 'EXCLUDED') {
    return `Remote eligibility excludes ${evidence.state}`;
  }
  return `Remote eligibility unknown for ${evidence.state}`;
}

export function preserveRemoteResidenceEvidence<T extends object>(
  job: T,
  evidence?: RemoteResidenceEvidence,
): T {
  const result = job as T & {
    search_evidence?: { remote_residence?: RemoteResidenceEvidence };
  };
  if (result.search_evidence?.remote_residence || !evidence) {
    return job;
  }
  return {
    ...job,
    search_evidence: {
      ...result.search_evidence,
      remote_residence: evidence,
    },
  } as T;
}
