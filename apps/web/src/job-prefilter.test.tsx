import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { JobPrefilter, parseJobPrefilterList, type JobPrefilterCriteria } from './job-prefilter';

const criteria: JobPrefilterCriteria = {
  include_titles: ['Systems Administrator'],
  exclude_titles: ['Intern'],
  required_keywords: ['Linux'],
  excluded_keywords: ['commission'],
  exact_phrases: ['on-call rotation'],
};

describe('cheap job title and keyword prefilter', () => {
  it('shows the supported criteria, captured-posting retention, and candidate-only scope', () => {
    const html = renderToStaticMarkup(<JobPrefilter criteria={criteria} onChange={() => {}} onPreview={vi.fn()} />);

    expect(html).toContain('Included job titles');
    expect(html).toContain('Excluded job titles');
    expect(html).toContain('Required keywords');
    expect(html).toContain('Excluded keywords');
    expect(html).toContain('Exact phrases');
    expect(html).toContain('Captured postings are retained. These filters only narrow current candidate work.');
    expect(html).toContain('Preview title and keyword filter');
  });

  it('renders only stage counts actually returned by the preview API', () => {
    const html = renderToStaticMarkup(<JobPrefilter
      criteria={criteria}
      onChange={() => {}}
      onPreview={vi.fn()}
      result={{ stage_counts: { captured: 20000, prefiltered: 240 }, rejected_count: 19760, deferred_count: 0 }}
    />);

    expect(html).toContain('Captured: 20,000');
    expect(html).toContain('Prefiltered candidates: 240');
    expect(html).toContain('Rejected by prefilter: 19,760');
    expect(html).not.toContain('AI enrichment:');
  });

  it('parses comma or line separated lists without empty or duplicate entries', () => {
    expect(parseJobPrefilterList(' Linux, Windows\nLinux \n macOS ')).toEqual(['Linux', 'Windows', 'macOS']);
  });
});
