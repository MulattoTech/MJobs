import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { SourceValueEvidence, authorityLabel, qualityLabel } from './source-value-evidence';

const resolution = {
  selected_candidate_id:'candidate-1', selected_posting_id:'posting-1', authority:'First party employer source',
  authority_evidence:['Verified source precedence','Fresh recent collection'], quality:{score:82,coverage:71},
  effective_quality:77, evaluated_at:'2026-10-03T12:00:00Z', candidate_count:2,
  conflicts:['salary field differs'], selected_application_url:'https://jobs.example/current',
  route_review_required:true, preserved_route_id:'route-old', preserved_route_url:'https://jobs.example/old',
};

describe('source value evidence', () => {
  it('formats quality as a comparison measure with coverage', () => expect(qualityLabel({score:82,coverage:71})).toBe('82/100 quality score · 71% measurement coverage'));
  it('maps authority enums to plain-language labels', () => {
    expect(authorityLabel('VERIFIED_ATS')).toBe('Verified applicant-tracking source');
    expect(authorityLabel('VERIFIED_EMPLOYER')).toBe('Verified employer source');
    expect(authorityLabel('AGGREGATOR')).toBe('Job aggregator source');
    expect(authorityLabel('UNKNOWN')).toBe('Source authority not established');
    expect(authorityLabel('CUSTOM_SOURCE')).toBe('Custom Source');
  });
  it('renders authority, disagreements, and distinct preserved route review', () => {
    const html = renderToStaticMarkup(<SourceValueEvidence resolution={{...resolution, authority:'VERIFIED_ATS'}} onPreparePreserved={vi.fn()} />);
    for (const text of ['Selected source evidence','Verified applicant-tracking source','Verified source precedence','salary field differs','not a probability or employer verification','Selected application URL','Previously used route','Prepare using preserved route','existing application targets are unchanged']) expect(html).toContain(text);
    expect(html).not.toContain('VERIFIED_ATS');
    expect(html).toContain('href="https://jobs.example/current"');
    expect(html).toContain('href="https://jobs.example/old"');
  });
  it('omits the panel when backend supplies no resolution', () => expect(renderToStaticMarkup(<SourceValueEvidence resolution={null} />)).toBe(''));
});
