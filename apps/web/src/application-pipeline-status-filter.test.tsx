import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ApplicationPipelineStatusFilter } from './application-pipeline-status-filter';

describe('reported application pipeline status filter', () => {
  it('offers all persisted stages plus not tracked under an accessible reported-status label', () => {
    const html = renderToStaticMarkup(
      <ApplicationPipelineStatusFilter value="" onChange={() => {}} />,
    );

    expect(html).toContain('Reported application pipeline status');
    expect(html).toContain('<option value="" selected="">All / clear</option>');
    for (const stage of [
      'SAVED', 'READY', 'APPLIED', 'RECRUITER_CONTACT', 'INTERVIEW',
      'TECHNICAL_INTERVIEW', 'FINAL_INTERVIEW', 'OFFER', 'REJECTED', 'WITHDRAWN',
      'NOT_TRACKED',
    ]) {
      expect(html).toContain(`value="${stage}"`);
    }
    expect(html).toContain('Applied (reported by you)');
    expect(html).toContain('Your reported tracking stage');
    expect(html).toContain('does not confirm that an employer received your application');
    expect(html).toContain('separate from MJobs document preparation');
  });

  it('reflects the selected status and disabled state', () => {
    const html = renderToStaticMarkup(
      <ApplicationPipelineStatusFilter value="NOT_TRACKED" onChange={() => {}} disabled />,
    );

    expect(html).toContain('aria-label="Reported application pipeline status" disabled=""');
    expect(html).toContain('<option value="NOT_TRACKED" selected="">Not tracked</option>');
  });

  it('leaves status changes to the Jobs search owner', () => {
    const onChange = vi.fn();
    renderToStaticMarkup(
      <ApplicationPipelineStatusFilter value="INTERVIEW" onChange={onChange} />,
    );

    expect(onChange).not.toHaveBeenCalled();
  });
});
