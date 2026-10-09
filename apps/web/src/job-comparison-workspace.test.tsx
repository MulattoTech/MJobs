import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  JobComparisonWorkspaceView,
  loadJobComparison,
  normalizeJobComparisonResponse,
  type JobComparisonRequest,
  type JobComparisonResponse,
  type JobComparisonWorkspaceViewProps,
} from './job-comparison-workspace';

const jobs = [
  { job_id: 'job-a', title: 'Cloud Engineer', company: 'Example One' },
  { job_id: 'job-b', title: 'Platform Engineer', company: 'Example Two' },
  { job_id: 'job-c', title: 'Systems Engineer', company: 'Example Three' },
];

const response: JobComparisonResponse = {
  profile_id: 'profile-a',
  search_profile_id: 'search-a',
  job_ids: ['job-a', 'job-b'],
  jobs: [
    {
      job_id: 'job-a',
      title: 'Cloud Engineer',
      company: 'Example One',
      match_score: 82,
      scoring_contributions: [
        {
          dimension: 'skills', weight: 70, raw_score: 80, weighted_contribution: 56,
          explanation: 'The stored skill signal matched the job snapshot.',
          provenance: { source: 'saved_match', evidence_ref: 'match:job-a:skills' },
        },
        {
          dimension: 'certification', weight: 10, raw_score: null, weighted_contribution: null,
          explanation: 'No stored certification contribution is available.',
          provenance: { source: 'UNKNOWN' },
        },
      ],
    },
    {
      job_id: 'job-b', title: 'Platform Engineer', company: 'Example Two',
      match_score: null, scoring_contributions: [],
    },
  ],
  shared_requirements: [
    {
      term: 'Linux', job_ids: ['job-a', 'job-b'],
      provenance: [
        { job_id: 'job-a', source: 'normalized_required_skill', evidence_ref: 'job-a:req:linux' },
        { job_id: 'job-b', source: 'normalized_required_skill', evidence_ref: 'job-b:req:linux' },
      ],
    },
  ],
  job_specific_requirements: [
    {
      term: 'Kubernetes', job_id: 'job-a',
      provenance: { source: 'normalized_required_skill', evidence_ref: 'job-a:req:kubernetes' },
    },
  ],
  unknowns: [
    { category: 'match_score', job_ids: ['job-b'], reason: 'MATCH_SCORE_MISSING' },
    { category: 'contributions', job_ids: ['job-b'], reason: 'SCORING_CONTRIBUTIONS_MISSING' },
    { category: 'required_terms', job_ids: ['job-b'], reason: 'REQUIREMENTS_MISSING' },
  ],
};

function findElement(
  node: React.ReactNode,
  predicate: (element: React.ReactElement<any>) => boolean,
): React.ReactElement<any> | null {
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findElement(child, predicate);
      if (found) return found;
    }
    return null;
  }
  if (!React.isValidElement(node)) return null;
  const element = node as React.ReactElement<any>;
  if (predicate(element)) return element;
  return findElement(element.props.children, predicate);
}

function viewProps(
  overrides: Partial<JobComparisonWorkspaceViewProps> = {},
): JobComparisonWorkspaceViewProps {
  return {
    profileId: 'profile-a',
    searchProfileId: 'search-a',
    availableJobs: jobs,
    selectedJobIds: ['job-a', 'job-b'],
    state: 'ready',
    response,
    error: null,
    onSelectionChange: vi.fn(),
    onCompare: vi.fn(),
    onRetry: vi.fn(),
    ...overrides,
  };
}

describe('job comparison workspace', () => {
  it('renders saved scores and contribution provenance with shared and job-specific observed terms', async () => {
    const onCompare = vi.fn(async (_request: JobComparisonRequest) => response);
    const loaded = await loadJobComparison(onCompare, {
      profile_id: 'profile-a', search_profile_id: 'search-a', job_ids: ['job-a', 'job-b'],
    });
    expect(onCompare).toHaveBeenCalledExactlyOnceWith({
      profile_id: 'profile-a', search_profile_id: 'search-a', job_ids: ['job-a', 'job-b'],
    });
    expect(loaded.state).toBe('ready');
    const html = renderToStaticMarkup(<JobComparisonWorkspaceView {...viewProps({
      response: loaded.response,
    })} />);
    expect(html).toContain('Saved match score');
    expect(html).toContain('82/100');
    expect(html).toContain('saved_match');
    expect(html).toContain('match:job-a:skills');
    expect(html).toContain('The stored skill signal matched the job snapshot.');
    expect(html).toContain('Shared observed requirements');
    expect(html).toContain('Linux');
    expect(html).toContain('job-a:req:linux');
    expect(html).toContain('Job-specific observed requirements');
    expect(html).toContain('Kubernetes');
    expect(html).toContain('job-a:req:kubernetes');
    expect(html).toContain('Observed in job descriptions; not verified candidate qualifications');
  });

  it('labels missing scores, contributions, requirement evidence, and UNKNOWN rows', () => {
    const noRequirements: JobComparisonResponse = {
      ...response,
      shared_requirements: [],
      job_specific_requirements: [],
    };
    const html = renderToStaticMarkup(<JobComparisonWorkspaceView {...viewProps({
      response: noRequirements,
    })} />);
    expect(html).toContain('Platform Engineer');
    expect(html).toContain('UNKNOWN');
    expect(html).toContain('MATCH_SCORE_MISSING');
    expect(html).toContain('SCORING_CONTRIBUTIONS_MISSING');
    expect(html).toContain('REQUIREMENTS_MISSING');
    expect(html).toContain('certification');
    expect(html).toContain('No stored certification contribution is available.');
    expect(html).toContain('No shared observed requirements are available (UNKNOWN evidence).');
    expect(html).not.toContain('The profile has Kubernetes');
  });

  it('renders the service unknown-reason contract and missing job labels without losing evidence', async () => {
    const serviceResponse = {
      ...response,
      jobs: response.jobs.map(job => job.job_id === 'job-b'
        ? { ...job, title: null, company: null }
        : job),
      unknowns: [
        { category: 'required_terms', job_ids: ['job-b'], reason: 'REQUIREMENTS_MISSING' },
        { category: 'match_score', job_ids: ['job-b'], reason: 'MATCH_SCORE_MISSING' },
        { category: 'contributions', job_ids: ['job-b'], reason: 'SCORING_CONTRIBUTIONS_MISSING' },
      ],
    };
    const request = {
      profile_id: 'profile-a', search_profile_id: 'search-a', job_ids: ['job-a', 'job-b'],
    };
    const loaded = await loadJobComparison(vi.fn(async () => serviceResponse), request);

    expect(loaded.state).toBe('ready');
    expect(normalizeJobComparisonResponse(serviceResponse, request).unknowns.map(row => row.reason)).toEqual([
      'REQUIREMENTS_MISSING', 'MATCH_SCORE_MISSING', 'SCORING_CONTRIBUTIONS_MISSING',
    ]);
    const html = renderToStaticMarkup(<JobComparisonWorkspaceView {...viewProps({
      response: loaded.response,
    })} />);
    expect(html).toContain('Platform Engineer — Example Two');
    expect(html).toContain('job-b — Employer unavailable');
    expect(html).toContain('REQUIREMENTS_MISSING');
    expect(html).toContain('MATCH_SCORE_MISSING');
    expect(html).toContain('SCORING_CONTRIBUTIONS_MISSING');
    expect(html).toContain('UNKNOWN evidence');
  });

  it('rejects foreign profile, saved-search, and job response scope without returning result data', async () => {
    const malformedScope = [
      { ...response, profile_id: 'profile-other' },
      { ...response, search_profile_id: 'search-other' },
      { ...response, job_ids: ['job-a', 'job-foreign'] },
      { ...response, jobs: [response.jobs[0], { ...response.jobs[1], job_id: 'job-foreign' }] },
    ];
    for (const returned of malformedScope) {
      const loaded = await loadJobComparison(vi.fn(async () => returned), {
        profile_id: 'profile-a', search_profile_id: 'search-a', job_ids: ['job-a', 'job-b'],
      });
      expect(loaded.state).toBe('scope-mismatch');
      expect(loaded.response).toBeNull();
      const html = renderToStaticMarkup(<JobComparisonWorkspaceView {...viewProps({
        state: loaded.state, response: loaded.response, error: loaded.error,
      })} />);
      expect(html).toContain('does not match the selected profile, saved search, and jobs');
      expect(html).not.toContain('82/100');
      expect(html).not.toContain('Kubernetes');
    }
  });

  it('shows explicit stale recovery and clears all prior comparison data', async () => {
    const stale = Object.assign(new Error('private stale details'), {
      detail: { code: 'JOB_COMPARISON_STALE_CONTEXT' },
    });
    const loaded = await loadJobComparison(vi.fn(async () => { throw stale; }), {
      profile_id: 'profile-a', search_profile_id: 'search-a', job_ids: ['job-a', 'job-b'],
    });
    expect(loaded.state).toBe('stale');
    const html = renderToStaticMarkup(<JobComparisonWorkspaceView {...viewProps({
      state: loaded.state, response: loaded.response, error: loaded.error,
    })} />);
    expect(html).toContain('Saved job or profile context changed');
    expect(html).toContain('Refresh or change the selected jobs');
    expect(html).not.toContain('private stale details');
    expect(html).not.toContain('82/100');
    expect(html).not.toContain('Kubernetes');
    expect(html).not.toContain('Retry comparison');
    const staleTree = JobComparisonWorkspaceView(viewProps({ state: 'stale', response: null }));
    const compareButton = findElement(staleTree, element => element.props['aria-label'] === 'Compare selected jobs');
    expect(compareButton?.props.disabled).toBe(true);
  });

  it('fails closed on malformed data and hides private transport details', async () => {
    const invalid = await loadJobComparison(vi.fn(async () => ({
      ...response,
      shared_requirements: [{ ...response.shared_requirements[0], term: '' }],
    })), {
      profile_id: 'profile-a', search_profile_id: 'search-a', job_ids: ['job-a', 'job-b'],
    });
    expect(invalid.state).toBe('error');
    expect(invalid.response).toBeNull();

    const foreignRequirement = await loadJobComparison(vi.fn(async () => ({
      ...response,
      shared_requirements: [{ ...response.shared_requirements[0], job_ids: ['job-a', 'foreign-job'] }],
    })), {
      profile_id: 'profile-a', search_profile_id: 'search-a', job_ids: ['job-a', 'job-b'],
    });
    expect(foreignRequirement.state).toBe('scope-mismatch');
    expect(foreignRequirement.response).toBeNull();

    const failure = await loadJobComparison(vi.fn(async () => {
      throw new Error('private transport details');
    }), { profile_id: 'profile-a', search_profile_id: 'search-a', job_ids: ['job-a', 'job-b'] });
    const html = renderToStaticMarkup(<JobComparisonWorkspaceView {...viewProps({
      state: failure.state, response: failure.response, error: failure.error,
    })} />);
    expect(html).toContain('Job comparison could not be loaded');
    expect(html).toContain('Retry comparison');
    expect(html).not.toContain('private transport details');
    expect(html).not.toContain('82/100');
  });

  it('requires explicit selection and compare action for between two and ten unique jobs', () => {
    const onSelectionChange = vi.fn();
    const onCompare = vi.fn();
    const tree = JobComparisonWorkspaceView(viewProps({
      selectedJobIds: ['job-a'], onSelectionChange, onCompare,
    }));
    const compareButton = findElement(tree, element => element.props['aria-label'] === 'Compare selected jobs');
    expect(compareButton?.props.disabled).toBe(true);
    findElement(tree, element => element.props['aria-label'] === 'Select job job-b')
      ?.props.onChange({ currentTarget: { checked: true } });
    expect(onSelectionChange).toHaveBeenCalledWith(['job-a', 'job-b']);
    expect(onCompare).not.toHaveBeenCalled();

    const selectedTree = JobComparisonWorkspaceView(viewProps({ onCompare }));
    findElement(selectedTree, element => element.props['aria-label'] === 'Compare selected jobs')
      ?.props.onClick();
    expect(onCompare).toHaveBeenCalledTimes(1);
  });

  it('rejects invalid, duplicate, and out-of-range job selections before calling the client', async () => {
    for (const jobIds of [[], ['job-a'], ['job-a', 'job-a'], Array.from({ length: 11 }, (_, index) => `job-${index}`)]) {
      const onCompare = vi.fn(async () => response);
      const loaded = await loadJobComparison(onCompare, {
        profile_id: 'profile-a', search_profile_id: 'search-a', job_ids: jobIds,
      });
      expect(loaded.state).toBe('error');
      expect(loaded.error).toContain('Select between 2 and 10 unique jobs');
      expect(onCompare).not.toHaveBeenCalled();
    }
  });

  it('does not expose scoring, generation, eligibility, or submission actions', () => {
    const html = renderToStaticMarkup(<JobComparisonWorkspaceView {...viewProps()} />);
    expect(html).toContain('Read-only comparison');
    expect(html).not.toMatch(/\b(Recalculate|Generate|Reject|Exclude|Submit|Apply now)\b/i);
  });
});
