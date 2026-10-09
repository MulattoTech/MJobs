import React from 'react';

export type SkillGapHeatmapSkill = {
  skill_id: string;
  skill: string;
  job_count: number;
  required_job_count: number;
  preferred_job_count: number;
  nice_to_have_job_count: number;
  unknown_posting_metadata_job_count: number;
  no_approved_evidence_job_count: number;
  approved_evidence_job_count: number;
  applied_job_count: number;
  interview_job_count: number;
  no_evidence_required_job_count: number;
  no_evidence_applied_job_count: number;
  no_evidence_interview_job_count: number;
  evidence_job_count: number;
  evidence_jobs: Array<{
    job_id: string;
    title: string | null;
    company: string | null;
    posting_strengths: string[];
    profile_evidence: string[];
    user_reported_applied: boolean;
    user_reported_interview: boolean;
  }>;
};

export type SkillGapHeatmapData = {
  profile_version: string;
  jobs_examined: number;
  skills: SkillGapHeatmapSkill[];
};

export type SkillGapHeatmapProps = {
  status: 'loading' | 'error' | 'ready';
  data?: SkillGapHeatmapData;
  error?: string;
  onRetry?: () => void;
};

function count(value: number) {
  return Number.isFinite(value) && value >= 0 ? Math.trunc(value) : 0;
}

function jobs(value: number) {
  const safeCount = count(value);
  return `${safeCount} ${safeCount === 1 ? 'job' : 'jobs'}`;
}

export function SkillGapHeatmap({ status, data, error, onRetry }: SkillGapHeatmapProps) {
  if (status === 'loading') {
    return <section className="panel" aria-labelledby="skill-gap-title" aria-busy="true">
      <h2 id="skill-gap-title">Skill gap heatmap</h2>
      <p role="status">Loading skill gap heatmap…</p>
    </section>;
  }

  if (status === 'error') {
    return <section className="panel" aria-labelledby="skill-gap-title">
      <h2 id="skill-gap-title">Skill gap heatmap</h2>
      <div className="alert" role="alert">
        <p>{error || 'The skill gap heatmap could not be loaded.'}</p>
        {onRetry && <button type="button" onClick={onRetry}>Retry</button>}
      </div>
    </section>;
  }

  if (!data || count(data.jobs_examined) === 0) {
    return <section className="panel" aria-labelledby="skill-gap-title">
      <h2 id="skill-gap-title">Skill gap heatmap</h2>
      <p>No scored jobs to summarize.</p>
      <p className="muted">The heatmap uses scored jobs for the selected profile and remains available without AI.</p>
    </section>;
  }

  if (data.skills.length === 0) {
    return <section className="panel" aria-labelledby="skill-gap-title">
      <h2 id="skill-gap-title">Skill gap heatmap</h2>
      <p>No recurring skill requirements found.</p>
      <p className="muted">{count(data.jobs_examined)} scored jobs examined. The heatmap uses deterministic job and profile evidence and remains available without AI.</p>
    </section>;
  }

  return <section className="panel" aria-labelledby="skill-gap-title">
    <h2 id="skill-gap-title">Skill gap heatmap</h2>
    <p>{count(data.jobs_examined)} scored jobs examined. Priority order: interview-reported gaps, applied-reported gaps, required-gap frequency, then total gap frequency. Ties use the stable skill identifier.</p>

    <div className="notice" aria-label="How to read this heatmap">
      <strong>How to read this heatmap</strong>
      <p>“No approved profile evidence” means MJobs did not find approved evidence for that skill in your profile. It does not confirm that you lack the skill.</p>
      <p>Unknown posting skill data is shown separately and is not counted as a profile evidence gap. This deterministic heatmap works when AI is disabled or unavailable.</p>
    </div>

    <h3>Skills without approved profile evidence</h3>
    <p className="muted">These counts describe scored-job mentions and approved profile evidence, not a definitive judgment about your abilities.</p>

    <div className="skill-gap-list" aria-label="Skill gap results">
      {data.skills.map(item => <article className="fact" key={item.skill_id}>
        <h3>{item.skill}</h3>
        <p>{count(item.no_approved_evidence_job_count)} of {jobs(item.job_count)} have no approved profile evidence for this skill.</p>
        <p>No approved evidence in {jobs(item.no_evidence_applied_job_count)} with a user-reported application and {jobs(item.no_evidence_interview_job_count)} with a user-reported interview.</p>
        {item.evidence_job_count > 0 && <details>
          <summary>{count(item.evidence_jobs.length) === count(item.evidence_job_count)
            ? `View ${jobs(item.evidence_job_count)} behind this skill`
            : `View ${count(item.evidence_jobs.length)} of ${jobs(item.evidence_job_count)} behind this skill`}</summary>
          <ul aria-label={`${item.skill} scored job evidence`}>
            {item.evidence_jobs.map(job => {
              const strengths = job.posting_strengths.map(value => {
                if (value === 'REQUIRED') return 'Required';
                if (value === 'PREFERRED') return 'Preferred';
                if (value === 'NICE_TO_HAVE') return 'Nice to have';
                return 'Posting requirement strength unknown';
              });
              const evidence = job.profile_evidence.map(value => {
                if (value === 'APPROVED') return 'approved profile evidence';
                if (value === 'NO_APPROVED_EVIDENCE') return 'no approved profile evidence';
                return 'profile evidence unknown';
              });
              return <li key={job.job_id}>
                <strong>{job.title || 'Untitled job'}</strong>
                <span> — {job.company || 'Employer unknown'}</span>
                <p>{[...strengths, ...evidence].join('; ')}</p>
                {job.user_reported_applied && <p>Application reported by you</p>}
                {job.user_reported_interview && <p>Interview reported by you</p>}
              </li>;
            })}
          </ul>
          {item.evidence_jobs.length < item.evidence_job_count && <p className="muted">
            Showing the ten most relevant scored jobs. Aggregate counts above use every current scored job.
          </p>}
        </details>}
        <dl>
          <div>
            <dt>Required mentions without approved evidence</dt>
            <dd>{jobs(item.no_evidence_required_job_count)}</dd>
          </div>
          <div>
            <dt>Required mentions</dt>
            <dd>{jobs(item.required_job_count)}</dd>
          </div>
          <div>
            <dt>Preferred mentions</dt>
            <dd>{jobs(item.preferred_job_count)}</dd>
          </div>
          <div>
            <dt>Nice-to-have mentions</dt>
            <dd>{jobs(item.nice_to_have_job_count)}</dd>
          </div>
          <div>
            <dt>Posting skill data unavailable</dt>
            <dd>{count(item.unknown_posting_metadata_job_count)} of {jobs(item.job_count)} have unknown posting skill metadata for this skill.</dd>
          </div>
          <div>
            <dt>Approved profile evidence found</dt>
            <dd>{jobs(item.approved_evidence_job_count)}</dd>
          </div>
          <div>
            <dt>Applications (reported by you)</dt>
            <dd>{jobs(item.applied_job_count)}</dd>
          </div>
          <div>
            <dt>Interviews (reported by you)</dt>
            <dd>{jobs(item.interview_job_count)}</dd>
          </div>
        </dl>
      </article>)}
    </div>
    <p className="muted">This activity is based on statuses you reported; it does not verify employer receipt or interview attendance.</p>
  </section>;
}
