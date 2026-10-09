import {afterEach,describe,expect,it,vi} from 'vitest';
import {
  matchApplicationAnswers,
  selectApplicationAnswerSuggestion,
  AnswerMatchClientError,
  type ApplicationAnswerMatchScope,
} from './application-answer-matching-client';

const candidates=[
  {answer_id:'exact-1',question:'How many years of experience?',text:'Seven years.',source:'Reviewed career profile answer',confidence:97,verified:true,user_approved:true,method:'EXACT_NORMALIZATION',score:1,explanation:'Question text is exactly normalized.'},
  {answer_id:'fuzzy-1',question:'How many years of experience do you have?',text:'Seven years.',source:'Reviewed career profile answer',confidence:94,verified:true,user_approved:true,method:'FUZZY',score:0.82,explanation:'Question wording is a bounded fuzzy match.'},
  {answer_id:'semantic-1',question:'Can you work weekends?',text:'Yes, with notice.',source:'Reviewed application answer',confidence:93,verified:true,user_approved:true,method:'CURATED_SEMANTIC',score:0.95,explanation:'Question wording matches the curated weekend availability paraphrase.'},
];

const scope:ApplicationAnswerMatchScope={
  profileId:'profile/one',applicationId:'application two',question:'Are you available to work weekends?',
};

const apiResponse={profile_id:scope.profileId,application_id:scope.applicationId,question:scope.question,candidates};

afterEach(()=>vi.unstubAllGlobals());

describe('application answer matching API client',()=>{
  it('posts only the question to the encoded profile and application path and retains advisory explanations',async()=>{
    const fetchMock=vi.fn<typeof fetch>().mockResolvedValue(Response.json(apiResponse));
    vi.stubGlobal('fetch',fetchMock);

    const result=await matchApplicationAnswers(scope,()=>scope);

    expect(result).toEqual({scope,...apiResponse});
    expect(result.candidates.map(({method,score,question,source,explanation})=>[method,score,question,source,explanation])).toEqual([
      ['EXACT_NORMALIZATION',1,'How many years of experience?','Reviewed career profile answer','Question text is exactly normalized.'],
      ['FUZZY',0.82,'How many years of experience do you have?','Reviewed career profile answer','Question wording is a bounded fuzzy match.'],
      ['CURATED_SEMANTIC',0.95,'Can you work weekends?','Reviewed application answer','Question wording matches the curated weekend availability paraphrase.'],
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      '/profiles/profile%2Fone/applications/application%20two/answers/match',
      expect.objectContaining({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:scope.question})}),
    );
  });

  it('accepts an empty candidate list with the original scope intact',async()=>{
    vi.stubGlobal('fetch',vi.fn<typeof fetch>().mockResolvedValue(Response.json({...apiResponse,candidates:[]})));

    await expect(matchApplicationAnswers(scope,()=>scope)).resolves.toEqual({scope,...apiResponse,candidates:[]});
  });

  it('rejects response profile, application, or question bindings that differ from the request',async()=>{
    const fetchMock=vi.fn<typeof fetch>();
    vi.stubGlobal('fetch',fetchMock);
    const mismatches=[
      {...apiResponse,profile_id:'profile-2'},
      {...apiResponse,application_id:'application-2'},
      {...apiResponse,question:'Which tools have you used?'},
    ];

    for(const response of mismatches) {
      fetchMock.mockResolvedValueOnce(Response.json(response));
      await expect(matchApplicationAnswers(scope,()=>scope)).rejects.toMatchObject({code:'STALE_SCOPE'});
    }
  });

  it('rejects a response with a missing scope envelope as malformed',async()=>{
    vi.stubGlobal('fetch',vi.fn<typeof fetch>().mockResolvedValue(Response.json({candidates:[]})));

    await expect(matchApplicationAnswers(scope,()=>scope)).rejects.toMatchObject({code:'INVALID_RESPONSE'});
  });

  it('rejects malformed candidates with a safe error',async()=>{
    vi.stubGlobal('fetch',vi.fn<typeof fetch>().mockResolvedValue(Response.json({...apiResponse,candidates:[{...candidates[0],score:'high'}]})));

    await expect(matchApplicationAnswers(scope,()=>scope))
      .rejects.toMatchObject({name:'AnswerMatchClientError',code:'INVALID_RESPONSE',message:'The answer suggestion service returned an invalid response.'});
  });

  it('maps server failures without exposing details or invoking mutation actions',async()=>{
    const fetchMock=vi.fn<typeof fetch>().mockResolvedValue(Response.json({detail:'private backend trace'}, {status:503}));
    vi.stubGlobal('fetch',fetchMock);

    let caught:unknown;
    try { await matchApplicationAnswers(scope,()=>scope); }
    catch(error) { caught=error; }

    expect(caught).toBeInstanceOf(AnswerMatchClientError);
    expect(caught).toMatchObject({code:'REQUEST_FAILED',message:'Application answer suggestions are temporarily unavailable.'});
    expect(String(caught)).not.toContain('private backend trace');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({method:'POST',body:JSON.stringify({question:scope.question})});
  });

  it('rejects a changed profile, application, or question before sending the request',async()=>{
    const fetchMock=vi.fn<typeof fetch>();
    vi.stubGlobal('fetch',fetchMock);
    const changedScopes:ApplicationAnswerMatchScope[]=[
      {...scope,profileId:'profile-2'},
      {...scope,applicationId:'application-2'},
      {...scope,question:'What cloud platforms have you used?'},
    ];

    for(const activeScope of changedScopes) {
      await expect(matchApplicationAnswers(scope,()=>activeScope)).rejects.toMatchObject({code:'STALE_SCOPE'});
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('discards a response if the active scope changes while fetching',async()=>{
    let activeScope=scope;
    let resolveResponse!:(response:Response)=>void;
    const fetchMock=vi.fn<typeof fetch>().mockReturnValue(new Promise<Response>(resolve=>{resolveResponse=resolve;}));
    vi.stubGlobal('fetch',fetchMock);

    const pending=matchApplicationAnswers(scope,()=>activeScope);
    activeScope={...scope,applicationId:'application-2'};
    resolveResponse(Response.json(apiResponse));

    await expect(pending).rejects.toMatchObject({code:'STALE_SCOPE'});
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('reports stale scope when the draft changes while parsing a malformed response body',async()=>{
    let activeScope=scope;
    let resolveBody!:(value:unknown)=>void;
    const response=new Response();
    vi.spyOn(response,'json').mockReturnValue(new Promise(resolve=>{resolveBody=resolve;}));
    vi.stubGlobal('fetch',vi.fn<typeof fetch>().mockResolvedValue(response));

    const pending=matchApplicationAnswers(scope,()=>activeScope);
    await Promise.resolve();
    activeScope={...scope,question:'Which tools have you used?'};
    resolveBody({unexpected:'malformed'});

    await expect(pending).rejects.toMatchObject({code:'STALE_SCOPE'});
  });

  it('rejects sensitive or legally significant questions before contacting matching',async()=>{
    const fetchMock=vi.fn<typeof fetch>();
    vi.stubGlobal('fetch',fetchMock);
    const sensitiveScope={...scope,question:'Are you legally authorized to work in the United States?'};

    await expect(matchApplicationAnswers(sensitiveScope,()=>sensitiveScope)).rejects.toMatchObject({code:'SENSITIVE_QUESTION'});
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('only prepares an unreviewed draft on explicit selection and fails closed after scope changes',async()=>{
    const fetchMock=vi.fn<typeof fetch>().mockResolvedValue(Response.json(apiResponse));
    vi.stubGlobal('fetch',fetchMock);
    const result=await matchApplicationAnswers(scope,()=>scope);

    expect(selectApplicationAnswerSuggestion(result.candidates[2],result.scope,scope)).toEqual({ok:true,draft:{
      answer_id:'semantic-1',text:'Yes, with notice.',profile_id:scope.profileId,application_id:scope.applicationId,
      question:scope.question,reviewed:false,method:'CURATED_SEMANTIC',score:0.95,
      saved_question:'Can you work weekends?',source:'Reviewed application answer',confidence:93,
      explanation:'Question wording matches the curated weekend availability paraphrase.',verified:true,user_approved:true,
    }});
    expect(selectApplicationAnswerSuggestion(
      result.candidates[2],result.scope,{...scope,applicationId:'application-2'},
    )).toEqual({ok:false,reason:'STALE_SCOPE'});
    expect(selectApplicationAnswerSuggestion(
      result.candidates[2],result.scope,scope,{minimumConfidence:95,minimumMatchScore:0.97},
    )).toEqual({ok:false,reason:'BELOW_THRESHOLD'});
    expect(selectApplicationAnswerSuggestion(
      {...result.candidates[2],verified:false},result.scope,scope,
    )).toEqual({ok:false,reason:'BELOW_THRESHOLD'});
    expect(selectApplicationAnswerSuggestion(
      {...result.candidates[2],user_approved:false},result.scope,scope,
    )).toEqual({ok:false,reason:'BELOW_THRESHOLD'});
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/profiles/profile%2Fone/applications/application%20two/answers/match');
  });
});
