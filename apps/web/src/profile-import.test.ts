import { describe, expect, it } from 'vitest';
import { addSelectedExtraction, cleanLines } from './profile-import';

describe('resume extraction review mapping', () => {
  it('maps selected name/email to profile fields and phones to contact', () => {
    const result = addSelectedExtraction({name:'Old',email:'old@example.test',facts:[],contact:{}}, {contact:{name:'New',email:'new@example.test',phones:['555','666']},facts:[]}, new Set(), new Set(['name','email','phones']));
    expect(result.profile.name).toBe('New'); expect(result.profile.email).toBe('new@example.test'); expect(result.profile.contact?.phones).toEqual(['555','666']);
  });
  it('deduplicates repeat imports and preserves existing verification', () => {
    const result = addSelectedExtraction({name:'',email:'',facts:[{id:'f1',kind:'skill',text:'Python',source:'manual',confidence:100,verified:true,user_approved:true}]}, {contact:{},facts:[{id:'f1',kind:'skill',text:'Python',confidence:90},{id:'f2',kind:'skill',text:'SQL',confidence:80}]}, new Set(['f1','f2']), new Set());
    expect(result.added).toBe(1); expect(result.skipped).toBe(1); expect(result.profile.facts[0].verified).toBe(true);
  });
  it('keeps pending flags and enforces the visible fact cap', () => {
    const existing = Array.from({length: 199}, (_, i) => ({id:`old-${i}`,kind:'skill',text:`old ${i}`,confidence:100,verified:false,user_approved:false}));
    const result = addSelectedExtraction({name:'',email:'',facts:existing}, {contact:{},facts:[{id:'new',kind:'skill',text:'new',confidence:40}]}, new Set(['new']), new Set(), 200);
    expect(result.profile.facts).toHaveLength(200); expect(result.profile.facts[199].verified).toBe(false);
    const blocked = addSelectedExtraction({...result.profile}, {contact:{},facts:[{id:'another',kind:'skill',text:'another',confidence:40}]}, new Set(['another']), new Set(), 200);
    expect(blocked.added).toBe(0); expect(blocked.truncated).toBe(true);
  });
  it('cleans phone and multiline role values only at save time', () => { expect(cleanLines(['  555 ', '', ' 666'])).toEqual(['555','666']); });
  it('requires new explicit approval even if an extraction response claims approval', () => {
    const result = addSelectedExtraction({name:'Example',email:'example@example.invalid',facts:[]},
      {contact:{name:'Replacement'},facts:[{id:'new',kind:'skill',text:'Python',source:'uploaded-resume:one',confidence:90,verified:true,user_approved:true}]},new Set(['new']),new Set());
    expect(result.profile.name).toBe('Example');
    expect(result.profile.facts[0].verified).toBe(false);
    expect(result.profile.facts[0].user_approved).toBe(false);
  });
});
