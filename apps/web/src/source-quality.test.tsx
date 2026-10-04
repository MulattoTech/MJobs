import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { SourceQuality } from './source-quality';

describe('source quality', () => {
  it('renders measured score, coverage, all components, and unknowns honestly', () => {
    const html=renderToStaticMarkup(<SourceQuality quality={{score:75,coverage:57.142857,components:{freshness:100,completeness:null,availability:90,parser_reliability:null,salary_completeness:50,application_link_accuracy:null,historical_uptime:80},limitations:['Application-link accuracy is unknown.']}}/>);
    expect(html).toContain('75/100');
    expect(html).toContain('57% measurement coverage');
    expect(html).toContain('Freshness');
    expect(html).toContain('Unknown');
    expect(html).toContain('Application-link accuracy is unknown.');
    expect(html).toContain('class="source-quality-limitations"');
    expect(html).not.toContain('\u00c2\u00b7');
  });
  it('renders malformed nonfinite scores as unknown', () => {
    const html=renderToStaticMarkup(<SourceQuality quality={{score:Number.NaN,coverage:Number.NaN,components:{}}}/>);
    expect(html).toContain('Unknown');
    expect(html).not.toContain('NaN');
  });
});
