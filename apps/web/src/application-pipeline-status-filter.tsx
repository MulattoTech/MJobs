import React from 'react';

export const APPLICATION_PIPELINE_STATUSES = [
  'SAVED',
  'READY',
  'APPLIED',
  'RECRUITER_CONTACT',
  'INTERVIEW',
  'TECHNICAL_INTERVIEW',
  'FINAL_INTERVIEW',
  'OFFER',
  'REJECTED',
  'WITHDRAWN',
] as const;

export type ApplicationPipelineStatus = typeof APPLICATION_PIPELINE_STATUSES[number];
export type ApplicationPipelineStatusFilterValue = ApplicationPipelineStatus | 'NOT_TRACKED' | '';

export type ApplicationPipelineStatusFilterProps = {
  value: ApplicationPipelineStatusFilterValue;
  onChange: (value: ApplicationPipelineStatusFilterValue) => void;
  disabled?: boolean;
};

function statusLabel(status: ApplicationPipelineStatus) {
  if (status === 'APPLIED') return 'Applied (reported by you)';
  return status.split('_').map(word => word[0] + word.slice(1).toLowerCase()).join(' ');
}

export function ApplicationPipelineStatusFilter({
  value,
  onChange,
  disabled = false,
}: ApplicationPipelineStatusFilterProps) {
  return <div className="filter-control">
    <label htmlFor="reported-application-pipeline-status">Application pipeline status (reported by you)</label>
    <select
      id="reported-application-pipeline-status"
      aria-label="Application pipeline status (reported by you)"
      value={value}
      disabled={disabled}
      onChange={event => onChange(event.target.value as ApplicationPipelineStatusFilterValue)}
    >
      <option value="">All / clear</option>
      <option value="NOT_TRACKED">Not tracked</option>
      {APPLICATION_PIPELINE_STATUSES.map(status =>
        <option key={status} value={status}>{statusLabel(status)}</option>,
      )}
    </select>
    <small className="muted">
      Your reported tracking stage. Applied is your report and does not confirm that an employer received your application. This is separate from MJobs document preparation.
    </small>
  </div>;
}
