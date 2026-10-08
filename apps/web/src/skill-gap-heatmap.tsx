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
    <p>{count(data.jobs_examined)} scored jobs examined. Skills are ordered by how often approved profile evidence was not found in scored jobs.</p>

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
        <dl>
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
