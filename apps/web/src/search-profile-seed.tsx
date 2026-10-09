import React from 'react';

export type SearchProfileSeedDraft = {
  name: string;
  includeTitles: string[];
  remote: 'REQUIRED';
  employmentType: 'FULL_TIME';
};

export const REMOTE_INFRASTRUCTURE_SEED: SearchProfileSeedDraft = {
  name: 'Remote Infrastructure / SysAdmin',
  includeTitles: [
    'System Administrator',
    'Systems Administrator',
    'Windows Administrator',
    'IT Administrator',
    'Infrastructure Support',
    'Endpoint Administrator',
    'Microsoft 365 Administrator',
    'Systems Engineer',
    'Technical Support Engineer',
    'NOC Engineer',
  ],
  remote: 'REQUIRED',
  employmentType: 'FULL_TIME',
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
    <p>Load an editable remote IT infrastructure search. Review the titles before saving; scheduled discovery stays off until you turn it on.</p>
    <button type="button" disabled={disabled} onClick={() => onApply({
      ...REMOTE_INFRASTRUCTURE_SEED,
      includeTitles: [...REMOTE_INFRASTRUCTURE_SEED.includeTitles],
    })}>
      Use remote IT infrastructure example
    </button>
  </section>;
}
