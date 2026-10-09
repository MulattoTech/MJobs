import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { SkillGapHeatmap, type SkillGapHeatmapData } from './skill-gap-heatmap';

const sample: SkillGapHeatmapData = {
  profile_version: 'a'.repeat(64),
  jobs_examined: 3,
  skills: [
    {
      skill_id: 'python',
      skill: 'Python',
      job_count: 3,
      required_job_count: 2,
      preferred_job_count: 1,
      nice_to_have_job_count: 0,
      unknown_posting_metadata_job_count: 1,
      no_approved_evidence_job_count: 2,
      approved_evidence_job_count: 1,
      applied_job_count: 1,
      interview_job_count: 1,
      no_evidence_required_job_count: 2,
      no_evidence_applied_job_count: 1,
      no_evidence_interview_job_count: 1,
      evidence_job_count: 1,
      evidence_jobs: [
        {
          job_id: 'job-python',
          title: 'Systems Engineer',
          company: 'Example Employer',
          posting_strengths: ['REQUIRED'],
          profile_evidence: ['NO_APPROVED_EVIDENCE'],
          user_reported_applied: true,
          user_reported_interview: false,
        },
      ],
    },
  ],
};

describe('skill gap heatmap', () => {
  it('announces a loading state accessibly', () => {
    const html = renderToStaticMarkup(<SkillGapHeatmap status="loading" />);

    expect(html).toContain('role="status"');
    expect(html).toContain('aria-busy="true"');
    expect(html).toContain('Loading skill gap heatmap');
  });

  it('shows a recoverable error and exposes the retry action', () => {
    const html = renderToStaticMarkup(
      <SkillGapHeatmap status="error" error="Heatmap unavailable" onRetry={() => {}} />,
    );

    expect(html).toContain('role="alert"');
    expect(html).toContain('Heatmap unavailable');
    expect(html).toContain('Retry');
  });

  it('distinguishes no analyzed jobs from an analyzed result with no recurring skills', () => {
    const noJobs = renderToStaticMarkup(
      <SkillGapHeatmap status="ready" data={{ ...sample, jobs_examined: 0, skills: [] }} />,
    );
    const noSkills = renderToStaticMarkup(
      <SkillGapHeatmap status="ready" data={{ ...sample, skills: [] }} />,
    );

    expect(noJobs).toContain('No scored jobs to summarize');
    expect(noSkills).toContain('No recurring skill requirements found');
    expect(noSkills).toContain('3 scored jobs examined');
  });

  it('separates unknown posting metadata from missing approved evidence and labels user reports', () => {
    const html = renderToStaticMarkup(<SkillGapHeatmap status="ready" data={sample} />);

    expect(html).toContain('Skills without approved profile evidence');
    expect(html).toContain('It does not confirm that you lack the skill.');
    expect(html).toContain('Posting skill data unavailable');
    expect(html).toContain('1 of 3 jobs have unknown posting skill metadata for this skill.');
    expect(html).toContain('2 of 3 jobs have no approved profile evidence for this skill.');
    expect(html).toContain('Applications (reported by you)');
    expect(html).toContain('Interviews (reported by you)');
    expect(html).toContain('No approved evidence in 1 job with a user-reported application and 1 job with a user-reported interview.');
    expect(html).toContain('Priority order: interview-reported gaps, applied-reported gaps, required-gap frequency, then total gap frequency.');
    expect(html).toContain('This activity is based on statuses you reported; it does not verify employer receipt or interview attendance.');
    expect(html).toContain('<h3>Python</h3>');
    expect(html).toContain('View 1 job behind this skill');
    expect(html).toContain('Systems Engineer');
    expect(html).toContain('Example Employer');
    expect(html).toContain('Required; no approved profile evidence');
    expect(html).toContain('Application reported by you');
  });
});
