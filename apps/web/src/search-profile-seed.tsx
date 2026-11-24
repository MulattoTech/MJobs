import React from 'react';

export type SearchProfileSeedDraft = {
  name: string;
  includeTitles: string[];
  remote: 'ANY' | 'REQUIRED';
  employmentType: 'FULL_TIME' | '';
  remoteResidenceState?: string | null;
  states?: string[];
  cities?: string[];
  workArrangements?: Array<'REMOTE' | 'HYBRID' | 'ONSITE'>;
};

export const REMOTE_INFRASTRUCTURE_SEED: SearchProfileSeedDraft = {
  name: 'Remote IT / Infrastructure',
  includeTitles: [
    'System Administrator',
    'Systems Administrator',
    'Infrastructure Manager',
    'Infrastructure Operations Engineer',
    'Windows Administrator',
    'IT Administrator',
    'Infrastructure Support',
    'Endpoint Administrator',
    'Microsoft 365 Administrator',
    'Systems Engineer',
    'Technical Support Engineer',
    'NOC Engineer',
    'IT Support',
    'Cloud Engineer',
    'DevOps Engineer',
    'Site Reliability Engineer',
    'Cybersecurity Analyst',
    'Security Engineer',
  ],
  remote: 'REQUIRED',
  employmentType: '',
  remoteResidenceState: 'OK',
  workArrangements: ['REMOTE'],
};

export const TEXOMA_LOCAL_IT_SEED: SearchProfileSeedDraft = {
  name: 'Texoma Local IT / Support',
  includeTitles: [
    'IT Support Technician',
    'IT Support Specialist',
    'Help Desk Technician',
    'Desktop Support Technician',
    'Systems Administrator',
    'Network Administrator',
    'Infrastructure Support',
    'Technical Support Engineer',
  ],
  remote: 'ANY',
  employmentType: '',
  remoteResidenceState: null,
  states: ['OK', 'TX'],
  cities: ['Durant', 'Denison', 'Sherman'],
  workArrangements: ['HYBRID', 'ONSITE'],
};

export function SearchProfileSeed({
  disabled = false,
  onApply,
}: {
  disabled?: boolean;
  onApply: (draft: SearchProfileSeedDraft) => void;
}) {
  return <section className="panel" aria-label="Search profile example">
    <h3>Start with an example</h3>
    <p>Load an editable search. Review the titles and locations before saving; scheduled discovery stays off until you turn it on.</p>
    <button type="button" disabled={disabled} onClick={() => onApply({
      ...REMOTE_INFRASTRUCTURE_SEED,
      includeTitles: [...REMOTE_INFRASTRUCTURE_SEED.includeTitles],
      workArrangements: [...(REMOTE_INFRASTRUCTURE_SEED.workArrangements || [])],
    })}>
      Use Remote IT / Infrastructure
    </button>
    <button type="button" disabled={disabled} onClick={() => onApply({
      ...TEXOMA_LOCAL_IT_SEED,
      includeTitles: [...TEXOMA_LOCAL_IT_SEED.includeTitles],
      states: [...(TEXOMA_LOCAL_IT_SEED.states || [])],
      cities: [...(TEXOMA_LOCAL_IT_SEED.cities || [])],
      workArrangements: [...(TEXOMA_LOCAL_IT_SEED.workArrangements || [])],
    })}>
      Use Texoma Local IT / Support
    </button>
  </section>;
}
