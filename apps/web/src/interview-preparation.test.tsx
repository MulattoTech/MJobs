import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {describe,expect,it,vi} from 'vitest';
import {InterviewPreparation, interviewPrepExportFilename, loadInterviewPrepPack, type InterviewPrepPack, type InterviewPrepStoryOption} from './interview-preparation';

const pack: InterviewPrepPack = {
  schema_version: 1,
  pack_id: 'pack-2',
  pack_version: 2,
  profile_id: 'profile-a',
  profile_version: 'profile-v3',
  job_id: 'job-a',
  job_fingerprint: 'job-sha256',
  created_at: '2026-10-08T14:00:00Z',
  questions: [{id:'q-1',prompt:'Tell me about a service outage you resolved.',source_refs:['fact:service-outage','story:incident#2']}],
  talking_points: [{text:'Reduced repeat incidents through a documented handoff.',source_refs:['fact:handoff']}],
  star_prompts: [{
    question:'How did you improve team handoffs?',
    situation_prompt:'Set the operational context from the linked story.',
    task_prompt:'Describe your responsibility from verified facts.',
    action_prompt:'Explain the documented actions you took.',
    result_prompt:'Use only the recorded outcome; add no unsupported metric.',
    source_refs:['story:handoff#1'],
  }],
  missing_evidence: [{topic:'Scale of outage impact',reason:'No verified impact measure is linked.'}],
  selected_story_revision_ids:['story-row-2'],
  export_text:'INTERVIEW PREPARATION · VERSION 2\n\nTell me about a service outage you resolved.',
};

const stories: InterviewPrepStoryOption[] = [
  {revision_id:'story-row-2',title:'Improved team handoffs',revision:2,source_refs:['fact:handoff','fact:incident-response']},
];

describe('interview preparation review',()=>{
  it('shows grounded questions, talking point sources, STAR prompts, missing evidence, and exact pack identity',()=>{
    const html=renderToStaticMarkup(<InterviewPreparation
      profileId="profile-a" jobId="job-a" packs={[pack]} availableStories={stories}
    />);

    expect(html).toContain('Interview preparation');
    expect(html).toContain('Version 2');
    expect(html).toContain('profile-v3');
    expect(html).toContain('Tell me about a service outage you resolved.');
    expect(html).toContain('fact:service-outage');
    expect(html).toContain('Reduced repeat incidents through a documented handoff.');
    expect(html).toContain('story:handoff#1');
    expect(html).toContain('Set the operational context from the linked story.');
    expect(html).toContain('No verified impact measure is linked.');
    expect(html).toContain('INTERVIEW PREPARATION · VERSION 2');
    expect(html).toContain('Download deterministic export');
  });

  it('offers approved story revisions with none selected by default',()=>{
    const html=renderToStaticMarkup(<InterviewPreparation
      profileId="profile-a" jobId="job-a" packs={[]} availableStories={stories}
    />);

    expect(html).toContain('No stories are selected automatically');
    expect(html).not.toMatch(/type="checkbox"[^>]*checked/);
    expect(html).toContain('Improved team handoffs · revision 2');
    expect(html).toContain('fact:handoff');
    expect(html).toContain('Create interview prep pack');
  });

  it('loads history and writes an explicitly selected revision through the profile/job API routes',async()=>{
    const calls: Array<{url:string;method:string;body?:string}> = [];
    const fetcher=vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url=String(input);
      const method=init?.method || 'GET';
      calls.push({url,method,body:typeof init?.body==='string'?init.body:undefined});
      const data=method==='POST'?pack:{packs:[pack]};
      return new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}});
    });

    const {loadInterviewPrepPacks,createInterviewPrepPack}=await import('./interview-preparation');
    await loadInterviewPrepPacks('profile/a','job b',fetcher);
    await createInterviewPrepPack('profile/a','job b',['story-row-2'],fetcher);

    expect(calls).toEqual([
      {url:'/profiles/profile%2Fa/jobs/job%20b/interview-prep-packs',method:'GET'},
      {url:'/profiles/profile%2Fa/jobs/job%20b/interview-prep-packs',method:'POST',body:JSON.stringify({selected_story_revision_ids:['story-row-2']})},
    ]);
  });

  it('reads an exact persisted version and uses a stable versioned download name',async()=>{
    const fetcher=vi.fn(async () => new Response(JSON.stringify(pack),{status:200}));
    const saved=await loadInterviewPrepPack('profile-a','job-a','pack/2',fetcher);

    expect(fetcher).toHaveBeenCalledWith('/profiles/profile-a/jobs/job-a/interview-prep-packs/pack%2F2',{
      headers:{'Content-Type':'application/json'},
    });
    expect(saved).toEqual(pack);
    expect(interviewPrepExportFilename(saved)).toBe('interview-preparation-v2.txt');
  });
});
