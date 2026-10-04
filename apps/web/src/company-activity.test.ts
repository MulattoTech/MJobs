import { describe, expect, it } from 'vitest';
const modules=import.meta.glob<{discoveryActivity: (events: unknown)=>{id:string; message:string; timestamp:string|null}[]}>('./company-activity.ts');
async function activity(events: unknown) {
  expect(modules['./company-activity.ts'], 'Readable discovery activity formatter is required').toBeTypeOf('function');
  return (await modules['./company-activity.ts']()).discoveryActivity(events);
}
describe('source-linked discovery activity', () => {
  it('describes exact known imports without claiming active or eligible jobs', async () => {
    const result=await activity([{id:'one', action:'COMPANY_FEED_IMPORT_COMPLETE', timestamp:'2026-10-02T10:00:00Z', details:{jobs_imported:3,jobs_enumerated:5,secret:'PRIVATE'}}]);
    expect(result[0].message).toBe('Imported 3 jobs from 5 feed records.');
    expect(JSON.stringify(result)).not.toContain('PRIVATE');
    expect(result[0].message).not.toContain('active');
  });
  it('retains zero but does not invent counts from absent or invalid values', async () => {
    const rows=await activity([
      {id:'zero',action:'COMPANY_FEED_IMPORT_COMPLETE',details:{jobs_imported:0,jobs_enumerated:2}},
      {id:'absent',action:'COMPANY_FEED_IMPORT_COMPLETE',details:{}},
      {id:'invalid',action:'COMPANY_FEED_IMPORT_COMPLETE',details:{jobs_imported:-1}},
      {id:'conflict',action:'COMPANY_FEED_IMPORT_COMPLETE',details:{jobs_imported:4,jobs_enumerated:3}},
    ]);
    expect(rows[0].message).toBe('Imported 0 jobs from 2 feed records.');
    expect(rows[1].message).toBe('Feed import recorded; job count unavailable.');
    expect(rows[2].message).toBe('Feed import recorded; job count unavailable.');
    expect(rows[3].message).toBe('Feed import recorded; inconsistent counts need review.');
  });
  it('describes source enrollment without falsely verifying employer identity', async () => {
    const result=await activity([{id:'source',action:'COMPANY_SOURCE_DISCOVERED',details:{provider:'rss'}}]);
    expect(result[0].message).toBe('Added a careers source (rss). Recorded source provider RSS.');
    expect(result[0].timestamp).toBeNull();
  });
  it('includes recorded provider and import facts in discovery activity', async () => {
    const result=await activity([{id:'source',action:'COMPANY_SOURCE_DISCOVERED',timestamp:'2026-10-02T10:00:00Z',details:{provider:'lever',jobs_imported:4}}]);
    expect(result[0].message).toContain('Recorded source provider Lever');
    expect(result[0].message).toContain('Imported 4 jobs');
  });
  it('explains recorded feed discovery and current-profile matches', async () => {
    const result=await activity([{id:'source',action:'COMPANY_SOURCE_DISCOVERED',details:{discovery_method:'SAME_ORIGIN_MACHINE_READABLE_FEED'}},
      {id:'import',action:'COMPANY_FEED_IMPORT_COMPLETE',details:{jobs_imported:3,jobs_enumerated:3,matched_jobs:2}}]);
    expect(result[0].message).toContain('Found a feed on the careers site');
    expect(result[1].message).toContain('2 imported jobs had skill matches at import');
  });
  it('omits a match claim when no evaluated count was recorded', async () => {
    const result=await activity([{id:'import',action:'COMPANY_FEED_IMPORT_COMPLETE',details:{jobs_imported:1,jobs_enumerated:1}}]);
    expect(result[0].message).toBe('Imported 1 jobs from 1 feed records.');
  });
  it('bounds history and ignores malformed or unrelated actions', async () => {
    expect(await activity(null)).toEqual([]);
    expect(await activity([null, {}, {id:'x', action:'PROFILE_SAVED',details:{secret:'PRIVATE'}}])).toEqual([]);
    const rows=Array.from({length:30},(_,i)=>({id:String(i), action:'COMPANY_SOURCE_DISCOVERED', timestamp:'invalid', details:{}}));
    const result=await activity(rows);
    expect(result).toHaveLength(20);
    expect(result[0].id).toBe('0');
    expect(result[0].timestamp).toBeNull();
  });
});
