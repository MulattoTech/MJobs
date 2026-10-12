import { describe, expect, it } from 'vitest';
import { api, eligible, terminal, resumeMediaType, uploadResume } from './api';
describe('user safety presentation', () => {
  it('preserves HTTP conflict status for safe stale-review recovery', async () => {
    const original = globalThis.fetch;
    globalThis.fetch = async () => new Response(JSON.stringify({detail:{code:'PROFILE_STALE'}}), {status:409});
    try {
      await expect(api('/synthetic-preview')).rejects.toMatchObject({status:409});
      await expect(api('/synthetic-preview')).rejects.toThrow('Request rejected (409)');
    } finally { globalThis.fetch = original; }
  });
  it('keeps pending, low confidence and unsourced facts out of document eligibility', () => {
    const fact = { verified: true, user_approved: true, confidence: 100, source: 'My records' };
    expect(eligible(fact)).toBe(true);
    for (const change of [{verified:false}, {user_approved:false}, {confidence:79}, {source:'  '}]) expect(eligible({...fact, ...change})).toBe(false);
  });
  it('stops polling failed, blocked and cancelled work', () => {
    for (const state of ['COMPLETED','FAILED','BLOCKED','CANCELLED']) expect(terminal.has(state)).toBe(true);
    expect(terminal.has('QUEUED')).toBe(false);
  });
  it('stops polling dead-lettered and explicitly discarded work', () => {
    expect(terminal.has('DEAD_LETTERED')).toBe(true);
    expect(terminal.has('DISCARDED')).toBe(true);
  });
  it('selects a precise MIME type for supported base resume formats', () => {
    expect(resumeMediaType('Experience.PDF')).toBe('application/pdf');
    expect(resumeMediaType('Experience.docx')).toBe('application/vnd.openxmlformats-officedocument.wordprocessingml.document');
    expect(resumeMediaType('Experience.txt')).toBe('text/plain');
    expect(() => resumeMediaType('installer.exe')).toThrow();
  });
  it('sends resume series metadata as explicit upload headers', async () => {
    const original = globalThis.fetch;
    let request: RequestInfo|URL|undefined;
    let init: RequestInit|undefined;
    globalThis.fetch = (input, options) => { request = input; init = options; return Promise.resolve(new Response('{}', {status: 200})); };
    try {
      await uploadResume('profile/1', new File(['resume'], 'Resume.pdf', {type:'application/pdf'}), {label:'Platform resume', seriesId:'series-1'});
      expect(String(request)).toContain('/profiles/profile%2F1/resumes');
      expect((init?.headers as Record<string,string>)['X-Resume-Label']).toBe('Platform%20resume');
      expect((init?.headers as Record<string,string>)['X-Resume-Series']).toBe('series-1');
    } finally { globalThis.fetch = original; }
  });
});
