import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { JobMatchScoreFilter } from './job-match-score-filter';

describe('minimum match score filter', () => {
  it('provides an accessible 0–100 control and explains unscored row handling', () => {
    const html = renderToStaticMarkup(<JobMatchScoreFilter value="75" onChange={() => {}} />);
    expect(html).toContain('Minimum match score (%)');
    expect(html).toContain('type="number"');
    expect(html).toContain('min="0"');
    expect(html).toContain('max="100"');
    expect(html).toContain('Unscored jobs are excluded while a minimum score is set.');
  });

  it('passes the edited threshold to the owner', () => {
    const onChange = vi.fn();
    const html = renderToStaticMarkup(<JobMatchScoreFilter value="" onChange={onChange} />);
    expect(html).toContain('aria-label="Minimum match score (%)"');
    expect(onChange).not.toHaveBeenCalled();
  });
});
