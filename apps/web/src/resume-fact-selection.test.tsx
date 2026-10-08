import {describe,expect,it} from 'vitest';
import {REWRITABLE_RESUME_FACT_KINDS,resumeFactRewriteLabel} from './resume-fact-selection';

describe('resume fact rewrite kinds',()=>{
  it('offers the reviewed rewrite flow for approved experience facts',()=>{
    expect(REWRITABLE_RESUME_FACT_KINDS).toContain('experience');
    expect(resumeFactRewriteLabel('experience')).toBe('experience');
  });
});
