import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { JobSourceFilter } from './job-source-filter';

describe('JobSourceFilter', () => {
  it('offers known collection sources and explains that selection only filters saved jobs', () => {
    const html = renderToStaticMarkup(<JobSourceFilter value="source-remotive" disabled={false} onChange={vi.fn()} sources={[
      { id: 'source-greenhouse', name: 'Greenhouse', provider: 'greenhouse', tenant: 'acme' },
      { id: 'source-remotive', name: 'Remotive', provider: 'remotive', tenant: 'remote' },
    ]} />);

    expect(html).toContain('<option value="">All job sources</option>');
    expect(html).toContain('<option value="source-greenhouse">Greenhouse (greenhouse · acme)</option>');
    expect(html).toContain('<option value="source-remotive" selected="">Remotive (remotive · remote)</option>');
    expect(html).toContain('does not start or schedule collection');
  });

  it('keeps saved values visible while disabled during another operation', () => {
    const html = renderToStaticMarkup(<JobSourceFilter value="source-greenhouse" disabled sources={[
      { id: 'source-greenhouse', name: 'Greenhouse', provider: 'greenhouse', tenant: 'acme' },
    ]} onChange={vi.fn()} />);
    expect(html).toContain('<select aria-label="Job source" disabled="">');
    expect(html).toContain('<option value="source-greenhouse" selected="">');
  });
});
