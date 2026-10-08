import React, { useEffect, useState, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import { api, eligible, terminal } from './api';
import { updateReviewedFact } from './fact-review';
import { CompanySources, type CompanySourceView } from './company-sources';
import { CompanyDiscovery } from './company-discovery';
import { CompanyDiscoveryRequests, type DiscoveryRequest } from './company-discovery-requests';
import { addSelectedExtraction, cleanLines } from './profile-import';
import { ResumeLibrary, type Resume, type Extraction } from './resume-library';
import { SourceValueEvidence, type SourceValueResolution } from './source-value-evidence';
import { DocumentHistory, type GeneratedDocument } from './document-history';
import { DocumentLibrary } from './document-library';
import { ResumeFactSelection,type ResumeFactChoice } from './resume-fact-selection';
import './style.css';

type Fact = { id: string; fact_id?: string; kind: string; text: string; source: string; confidence: number; verified: boolean; user_approved: boolean };
type Answer = Omit<Fact, 'kind' | 'fact_id'> & {question?:string};
type Contact = {first_name:string;middle_name:string;last_name:string;preferred_name:string;phones:string[];address_line1:string;address_line2:string;city:string;state:string;postal_code:string;country:string;linkedin:string;github:string;portfolio:string};
type Preferences = {desired_roles:string[];employment_types:string[];work_arrangements:string[];shifts:string[];max_commute_miles:number|null;max_commute_minutes:number|null;travel_preference:string;salary_expectations:string;career_goals:string;work_authorization:string;sponsorship_needs:string;avoid_terms:string[]};
type Profile = {id:string; name:string; email:string; version?:string; facts:Fact[]; answers:Answer[]; contact?:Partial<Contact>; preferences?:Partial<Preferences>};
type Match = {score:number; missing:string[]; components:{method?:string; matched?:string[]; detected?:string[]; limitation?:string;requirements?:{skill:string;strength:'REQUIRED'|'PREFERRED'|'NICE_TO_HAVE'|'UNKNOWN';profile_evidence:'APPROVED'|'NO_APPROVED_EVIDENCE';explanation:string}[]}};
type JobTracking = {disposition:'ACTIVE'|'SAVED'|'HIDDEN'|'NOT_INTERESTED';notes?:{id:string;text:string;sequence:number;timestamp:string}[];history?:{type:'disposition'|'note';disposition?:string;id?:string;text?:string;sequence:number;timestamp:string}[]};
type Job = {id:string; title:string; company:string; provider:string; location:string; remote_type:string; description:string; skills:string[]; application_url:string; match_score:number|null; posting_count:number; tracking?:JobTracking; attributes:{fictional?:boolean; captured_at?:string; source_type?:string; company_identity?:string; skill_evidence?:string; certifications?:{id:string;name:string;strength:'REQUIRED'|'PREFERRED'|'NICE_TO_HAVE'|'UNKNOWN'}[]; salary?:{min:number;max:number;currency:string;period:string}[]; last_collected_at?:string; source_resolution?:SourceValueResolution|null}; match?:Match|null; postings?:{source:string;source_job_id:string;source_url?:string;application_url?:string;fetched_at:string;raw?:{submitted_fields?:{application_url?:string;reference_url?:string;source_details?:Record<string,unknown>}};snapshots:{sha256:string;retrieved_at:string}[]}[]};
type ApplicationTracking = {status:string|null;attribution:'USER_REPORTED'|'EXTERNAL_USER_REPORTED'|null;notes:{id:string;text:string;sequence:number;timestamp:string}[];external_reports?:{source:string;applied_at:string;application_url?:string;sequence:number;timestamp:string}[];history:{type:'status'|'note'|'external_application';status?:string;attribution?:string;text?:string;source?:string;applied_at?:string;sequence:number;timestamp:string}[]};
type Application = {id:string; title:string; company:string; state:string; target:string; job_id:string; profile_id:string; document_version:number; profile_version:string; fact_ids:string[]; trace:string[]; confirmation:Record<string,unknown>|null; question_preflight?:{result:'PASS'|'FAIL'|'UNKNOWN';reason:string|null;observed_at:string;missing:{question_id:string;question:string;fact_ids?:string[];answer_ids?:string[];profile_paths?:string[];document_kinds?:string[];application_context?:string[]}[]}|null; pipeline_status?:string|null; pipeline_attribution?:string|null; external_reports?:{source:string;applied_at:string;application_url?:string;sequence:number;timestamp:string}[]; answer_bank?:Answer[]; attempts?:{state:string; started_at:string;steps:string[]}[]};
type Doc = GeneratedDocument;
type Operation = {id:string; kind:string; state:string; error:string|null;attempt_count:number;cancel_requested:boolean;updated_at:string;last_attempt_at?:string|null;dead_lettered_at?:string|null;discarded_at?:string|null;retryable?:boolean;retry_blocked_reason?:string};
type AuditEvent = {id:string;type:string;summary:string;subject:string;company:string;timestamp:string};
type Source = {id:string;provider:string;tenant:string;name:string;enabled:boolean;health_status:string;jobs_seen:number;last_error:string|null;last_success:string|null;backoff_until:string|null;poll_interval:number;concurrency:number;request_timeout:number;rate_limit:number;jitter_seconds:number;strategy:string;capabilities:{capabilities:string[]}};
type SourceCandidateReview = {id:string;profile_id:string;state:string;site_type:string;reason:string;api_feed_evidence_status:'PUBLIC_DOCUMENTED'|'UNKNOWN'|'BLOCKED';api_feed_evidence_reference:string|null;api_feed_evidence_note:string;access_terms_note:string;capabilities_limits_note:string;expected_value_maintenance_note:string;reviewed_at:string};
type SourceCandidate = {id:string;domain:string;display_name:string;site_type:string;state:string;first_seen_at:string;last_seen_at:string;collection_enabled:boolean;review_reason:string|null;observations:{id:string;job_id:string|null;application_id:string|null;discovery_origin:string|null;origin_attribution:string;source_url:string|null;application_url:string|null;observed_hops:(string|{state:string})[];capture_method:string;evidence_status:string;observed_at:string}[];review_history:SourceCandidateReview[]};
type SourceCandidateEvaluationDraft = {api_feed_evidence_status:'PUBLIC_DOCUMENTED'|'UNKNOWN'|'BLOCKED';api_feed_evidence_reference:string;api_feed_evidence_note:string;access_terms_note:string;capabilities_limits_note:string;expected_value_maintenance_note:string};
type SearchProfile = {id:string;profile_id:string;name:string;enabled:boolean;criteria:{include_titles:string[];remote:string;employment_types:string[]};source_ids:string[];created_at:string;updated_at:string};
type OperationMode = 'FIXTURE'|'PERSONAL';
type Tab = 'Jobs'|'Profile'|'Sources'|'Applications'|'AI Settings'|'Activity';
type AITaskKey = 'job_parsing'|'job_scoring'|'requirement_extraction'|'resume_tailoring'|'cover_letters'|'free_form_answers'|'company_summarization';
type AIProviderChoice = 'disabled'|'ollama';
type AITaskSelection = {provider:AIProviderChoice;model:string|null};
type AITaskSettings = Record<AITaskKey,AITaskSelection>;
type AITaskFallback = {mode:'DETERMINISTIC'|'QUEUE_FOR_LATER'|'UNAVAILABLE';description:string};
type AITaskFallbacks = Record<AITaskKey,AITaskFallback>;
type CredentialProvider = 'openai'|'anthropic';
type ProviderCredential = {provider:CredentialProvider;configured:boolean;masked:string;created_at:string;updated_at:string;last_tested_at:string|null;last_test_status:'NOT_TESTED'|'CONNECTED'|'FAILED';rotation_count:number};
type AICostPolicy = {minimum_score:number;max_input_candidates:number;max_ai_candidates:number;max_document_candidates:number};
type AICostPlan = {scanned_job_count:number;scored_candidate_count:number;unique_count:number;eligible_count:number;ai_candidate_count:number;document_candidate_count:number};
type AICostEvaluation = {job_id:string;title:string;company:string;deterministic_score:number;category:'STRONG'|'REVIEW'|'WEAK';rationale:string;document_reviewed?:boolean};
type AICostEvaluationResult = {run_id:string;profile_version:string;policy:AICostPolicy;task_provider:string;task_model:string;created_at:string;evaluated_candidate_count:number;evaluations:AICostEvaluation[];document_candidate_ids:string[];documents_created:number;deterministic_matches_unchanged:boolean};
const aiTaskRows: {key:AITaskKey;label:string}[] = [
  {key:'job_parsing',label:'Job parsing'},
  {key:'job_scoring',label:'Job scoring'},
  {key:'requirement_extraction',label:'Requirement extraction'},
  {key:'resume_tailoring',label:'Resume tailoring'},
  {key:'cover_letters',label:'Cover letters'},
  {key:'free_form_answers',label:'Free-form answers'},
  {key:'company_summarization',label:'Company summarization'},
];
const defaultAITaskSettings = ():AITaskSettings => Object.fromEntries(
  aiTaskRows.map(({key})=>[key,{provider:'disabled',model:null}])
) as AITaskSettings;
const defaultAICostPolicy = ():AICostPolicy => ({minimum_score:75,max_input_candidates:5000,max_ai_candidates:75,max_document_candidates:20});
const newFact = ():Fact => ({id:crypto.randomUUID(),kind:'skill',text:'',source:'',confidence:100,verified:false,user_approved:false});
const newAnswer = ():Answer => ({id:crypto.randomUUID(),question:'',text:'',source:'Manual entry',confidence:100,verified:false,user_approved:false});
const newProfile = ():Profile => ({id:crypto.randomUUID(),name:'',email:'',facts:[],answers:[],contact:{},preferences:{}});
type ManualCaptureDraft = {title:string;company:string;description:string;location:string;remote_type:string;reference_url:string;application_url:string;discovery_origin:string;source_details_json:string};
const emptyManualCapture = ():ManualCaptureDraft => ({title:'',company:'',description:'',location:'',remote_type:'UNKNOWN',reference_url:'',application_url:'',discovery_origin:'',source_details_json:'{}'});
const stamp = (value:string|null|undefined) => value ? new Date(value).toLocaleString() : 'Not yet';
const normalizedQuestion = (value:string) => value.toLocaleLowerCase().match(/[a-z0-9]+/g)?.join(' ')||'';
const sensitiveAnswerQuestion = (value:string) => /\b(race|ethnic\w*|gender|sex|sexual orientation|disabilit\w*|veteran\w*|relig\w*|date of birth|birth date|age|marital|citizenship|citizen|authoriz\w*|work authorization|eligible to work|work eligibility|right to work|visa|sponsorship|criminal|convict\w*|felon\w*|background check|salary expectation\w*|salary range|desired compensation|compensation expectation\w*|non[ -]?compete|conflict\w* of interest|minimum age|attest\w*|certif\w* under penalty)\b/i.test(value);
const applicationStages = ['SAVED','READY','APPLIED','RECRUITER_CONTACT','INTERVIEW','TECHNICAL_INTERVIEW','FINAL_INTERVIEW','OFFER','REJECTED','WITHDRAWN'];
const applicationStageLabel = (stage:string|null|undefined) => stage==='APPLIED'?'Applied — reported by you':stage?stage.split('_').map(word=>word[0]+word.slice(1).toLowerCase()).join(' '):'Not tracked';
const localDateTimeInput = () => {const date=new Date();const part=(value:number)=>String(value).padStart(2,'0');return `${date.getFullYear()}-${part(date.getMonth()+1)}-${part(date.getDate())}T${part(date.getHours())}:${part(date.getMinutes())}`;};

function App() {
  const [tab,setTab] = useState<Tab>('Jobs');
  const [profiles,setProfiles] = useState<Profile[]>([]);
  const [profileId,setProfileId] = useState(localStorage.getItem('mjobs-profile') || '');
  const [editor,setEditor] = useState<Profile>(newProfile);
  const [jobs,setJobs] = useState<Job[]>([]);
  const [showDismissed,setShowDismissed] = useState(false);
  const [jobNoteDraft,setJobNoteDraft] = useState('');
  const [jobNoteRequestId,setJobNoteRequestId] = useState(()=>crypto.randomUUID());
  const jobDispositionRequestIds = useRef(new Map<string,string>());
  const [manualCaptureOpen,setManualCaptureOpen] = useState(false);
  const [manualCapture,setManualCapture] = useState<ManualCaptureDraft>(emptyManualCapture);
  const [manualCaptureRequestId,setManualCaptureRequestId] = useState(()=>crypto.randomUUID());
  const [apps,setApps] = useState<Application[]>([]);
  const [externalCompany,setExternalCompany]=useState('');
  const [externalTitle,setExternalTitle]=useState('');
  const [externalUrl,setExternalUrl]=useState('');
  const [externalSourceUrl,setExternalSourceUrl]=useState('');
  const [externalOrigin,setExternalOrigin]=useState('');
  const [externalDescription,setExternalDescription]=useState('');
  const [externalSource,setExternalSource]=useState<'MANUAL'|'REFERRAL'|'BROWSER'|'OTHER'>('MANUAL');
  const [externalAppliedAt,setExternalAppliedAt]=useState(localDateTimeInput);
  const externalCaptureRequestId=useRef(crypto.randomUUID());
  const [sources,setSources] = useState<Source[]>([]);
  const [sourceCandidates,setSourceCandidates] = useState<SourceCandidate[]>([]);
  const [sourceCandidateReviewDraft,setSourceCandidateReviewDraft] = useState<Record<string,string>>({});
  const [sourceCandidateSiteTypeDraft,setSourceCandidateSiteTypeDraft] = useState<Record<string,SourceCandidate['site_type']>>({});
  const [sourceCandidateEvaluationDraft,setSourceCandidateEvaluationDraft] = useState<Record<string,SourceCandidateEvaluationDraft>>({});
  const [sourceReportUrl,setSourceReportUrl] = useState('');
  const [sourceReportOrigin,setSourceReportOrigin] = useState('');
  const [aiTaskSettings,setAITaskSettings] = useState<AITaskSettings>(defaultAITaskSettings);
  const [aiTaskFallbacks,setAITaskFallbacks] = useState<Partial<AITaskFallbacks>>({});
  const [aiSettingsLoading,setAISettingsLoading] = useState(false);
  const [providerCredentials,setProviderCredentials] = useState<ProviderCredential[]>([]);
  const [credentialDrafts,setCredentialDrafts] = useState<Record<CredentialProvider,string>>({openai:'',anthropic:''});
  const credentialSequence = useRef(0);
  const [aiCostPolicy,setAICostPolicy] = useState<AICostPolicy>(defaultAICostPolicy);
  const [aiCostLoading,setAICostLoading] = useState(false);
  const [aiCostReady,setAICostReady] = useState(false);
  const [aiCostPlan,setAICostPlan] = useState<AICostPlan|null>(null);
  const [aiCostHistory,setAICostHistory] = useState<AICostEvaluationResult[]>([]);
  const [aiEvaluationRunning,setAIEvaluationRunning] = useState(false);
  const [aiEvaluationCancelling,setAIEvaluationCancelling] = useState(false);
  const aiEvaluationRequest = useRef<{profileId:string;requestId:string}|null>(null);
  const [aiDocumentReviewConsent,setAIDocumentReviewConsent] = useState<Record<string,boolean>>({});
  const [aiDocumentPreparation,setAIDocumentPreparation] = useState<{runId:string;jobId:string}|null>(null);
  const [companySources,setCompanySources] = useState<CompanySourceView[]>([]);
  const [discoveryRequests,setDiscoveryRequests] = useState<DiscoveryRequest[]>([]);
  const companySourcesSequence = useRef(0);
  const [operationMode,setOperationMode] = useState<OperationMode|null>(null);
  const [ops,setOps] = useState<Operation[]>([]);
  const [deadLetters,setDeadLetters] = useState<Operation[]>([]);
  const [deadLetterCount,setDeadLetterCount] = useState(0);
  const [auditEvents,setAuditEvents] = useState<AuditEvent[]>([]);
  const [ready,setReady] = useState<{status:string;checks:Record<string,boolean>}|null>(null);
  const [q,setQ] = useState(''); const [remote,setRemote]=useState(''); const [provider,setProvider]=useState(''); const [sort,setSort]=useState('score');
  const [detail,setDetail]=useState<Job|null>(null);
  const [tailorResume,setTailorResume]=useState(false);
  const [includeResumeContact,setIncludeResumeContact]=useState(false);
  const [coverLetterLength,setCoverLetterLength]=useState<'VERY_SHORT'|'CONCISE'|'NORMAL'|'DETAILED'>('NORMAL');
  const [coverLetterStyle,setCoverLetterStyle]=useState<'professional'|'warm'|'technical'|'direct'|'conversational'|'formal'>('professional');
  const [resumeSelection,setResumeSelection]=useState<ResumeFactChoice|null>(null);
  useEffect(()=>{setTailorResume(false);setIncludeResumeContact(false);setResumeSelection(null);setJobNoteDraft('');setJobNoteRequestId(crypto.randomUUID());jobDispositionRequestIds.current.clear();},[detail?.id,profileId]);
  useEffect(()=>{setSourceCandidates([]);setSourceCandidateReviewDraft({});setExternalOrigin('');setExternalSourceUrl('');},[profileId]);
  const [selectedApp,setSelectedApp]=useState<Application|null>(null);
  const [applicationTracking,setApplicationTracking]=useState<ApplicationTracking|null>(null);
  const [applicationStatusDraft,setApplicationStatusDraft]=useState('');
  const [applicationNoteDraft,setApplicationNoteDraft]=useState('');
  const [applicationNoteRequestId,setApplicationNoteRequestId]=useState(()=>crypto.randomUUID());
  const applicationStatusRequestIds=useRef(new Map<string,string>());
  const [answerDrafts,setAnswerDrafts]=useState<{question:string;text:string;answer_id?:string;reviewed:boolean}[]>([]);
  const [docs,setDocs]=useState<Doc[]>([]);
  const [resumes,setResumes]=useState<Resume[]>([]);
  const [tenant,setTenant]=useState(''); const [sourceIntervalMinutes,setSourceIntervalMinutes]=useState(30); const [sourceConcurrency,setSourceConcurrency]=useState(1); const [sourceTimeout,setSourceTimeout]=useState(10); const [sourceRate,setSourceRate]=useState(60); const [sourceJitter,setSourceJitter]=useState(30);
  const [searchProfiles,setSearchProfiles]=useState<SearchProfile[]>([]); const [selectedSearchProfileId,setSelectedSearchProfileId]=useState(''); const [searchName,setSearchName]=useState(''); const [searchTitles,setSearchTitles]=useState(''); const [searchRemote,setSearchRemote]=useState<'ANY'|'REQUIRED'|'EXCLUDED'>('ANY'); const [searchEmployment,setSearchEmployment]=useState('FULL_TIME'); const [searchSource,setSearchSource]=useState('');
  const [mockConsent,setMockConsent]=useState(false);
  const [busy,setBusy]=useState(''); const [error,setError]=useState(''); const [notice,setNotice]=useState('');
  const [activeOp,setActiveOp]=useState('');
  const profileRef = useRef(profileId); profileRef.current = profileId;
  const profileBaselineRef = useRef<Profile|null>(null);
  const queryRef = useRef({q,sort,remote,provider,showDismissed,searchProfileId:selectedSearchProfileId}); queryRef.current={q,sort,remote,provider,showDismissed,searchProfileId:selectedSearchProfileId};
  const jobsSequence = useRef(0); const detailSequence = useRef(0); const appSequence = useRef(0); const editorSequence = useRef(0);

  async function refresh(id=profileId) {
    const companySequence = ++companySourcesSequence.current;
    const suffix = id ? `?profile_id=${encodeURIComponent(id)}` : '';
    const [p,a,s,o,sp,cs,dr,sc] = await Promise.all([api<{profiles:Profile[]}>('/profiles'),api<{applications:Application[]}>('/applications'+suffix),api<{sources:Source[]}>('/sources'),api<{operations:Operation[]}>('/operations'+suffix),id?api<{search_profiles:SearchProfile[]}>('/search-profiles'+suffix):Promise.resolve({search_profiles:[]} as {search_profiles:SearchProfile[]}),api<{company_sources:CompanySourceView[]}>('/company-sources/view'+suffix),api<{requests:DiscoveryRequest[]}>('/company-discovery/requests'),id?api<{candidates:SourceCandidate[]}>('/source-candidates'+suffix):Promise.resolve({candidates:[]} as {candidates:SourceCandidate[]})]);
    if(companySequence===companySourcesSequence.current&&id===profileRef.current){setCompanySources(cs.company_sources);setDiscoveryRequests(dr.requests);}
    setProfiles(p.profiles);setSources(s.sources);if(id===profileRef.current){setApps(a.applications);setOps(o.operations);setSearchProfiles(sp.search_profiles);setSourceCandidates(sc.candidates);}
  }
  async function loadDeadLetters(id=profileId) {
    if(!id){setDeadLetters([]);setDeadLetterCount(0);return;}
    const response=await api<{count:number;operations:Operation[]}>('/operations/dead-letter-queue?profile_id='+encodeURIComponent(id));
    if(id===profileRef.current){setDeadLetters(response.operations);setDeadLetterCount(response.count);}
  }
  async function loadAuditHistory(id=profileId) {
    if(!id){setAuditEvents([]);return;}
    const response=await api<{events:AuditEvent[]}>('/profiles/'+encodeURIComponent(id)+'/audit-history');
    if(id===profileRef.current)setAuditEvents(response.events);
  }
  async function loadJobs(id=profileId) {
    const sequence=++jobsSequence.current; const current=queryRef.current;
    const params = new URLSearchParams({q:current.q,sort:current.sort});
    if(id)params.set('profile_id',id);if(current.remote)params.set('remote_type',current.remote);if(current.provider)params.set('provider',current.provider);
    if(searchProfiles.some(search=>search.id===current.searchProfileId&&search.profile_id===id))params.set('search_profile_id',current.searchProfileId);
    if(current.showDismissed)params.set('include_dismissed','true');
    const result=await api<{jobs:Job[]}>('/jobs?'+params);
    if(sequence===jobsSequence.current&&id===profileRef.current)setJobs(result.jobs);
  }
  async function readiness() {
    try { const response=await fetch('/readiness');setReady(await response.json()); } catch {setReady({status:'unavailable',checks:{api:false}});}
  }
  useEffect(() => {void refresh().catch(e=>setError(String(e)));void api<{operation_mode:OperationMode}>('/settings').then(s=>setOperationMode(s.operation_mode)).catch(e=>setError(String(e)));void readiness();const timer=setInterval(()=>void readiness(),15000);return()=>clearInterval(timer);},[]);
  useEffect(()=>{if(tab==='Activity'){void loadDeadLetters(profileId).catch(e=>setError(String(e)));void loadAuditHistory(profileId).catch(e=>setError(String(e)));}},[tab,profileId]);
  useEffect(()=>{setDeadLetters([]);setDeadLetterCount(0);setAuditEvents([]);},[profileId]);
  useEffect(() => {const sequence=++editorSequence.current;++appSequence.current;profileBaselineRef.current=null;localStorage.setItem('mjobs-profile',profileId);setSearchProfiles([]);setSelectedSearchProfileId('');setDetail(null);setSelectedApp(null);setApplicationTracking(null);setApplicationStatusDraft('');setApplicationNoteDraft('');setApplicationNoteRequestId(crypto.randomUUID());externalCaptureRequestId.current=crypto.randomUUID();setExternalCompany('');setExternalTitle('');setExternalUrl('');setExternalDescription('');setExternalAppliedAt(localDateTimeInput());setDocs([]);setResumes([]);setApps([]);setOps([]);setCompanySources([]);setMockConsent(false);setAITaskSettings(defaultAITaskSettings());setAITaskFallbacks({});setAISettingsLoading(!!profileId);void refresh(profileId).catch(e=>setError(String(e)));if(profileId){void api<Profile>('/profiles/'+encodeURIComponent(profileId)).then(p=>{if(sequence===editorSequence.current&&p.id===profileRef.current){const loaded={...p,contact:p.contact||{},preferences:p.preferences||{},facts:(p.facts||[]).map(f=>({...f,id:f.fact_id||f.id}))};setEditor(loaded);profileBaselineRef.current=loaded;}}).catch(e=>setError(String(e)));void api<{resumes:Resume[]}>('/profiles/'+encodeURIComponent(profileId)+'/resumes').then(r=>{if(sequence===editorSequence.current&&profileId===profileRef.current)setResumes(r.resumes);}).catch(e=>setError(String(e)));void api<{tasks:AITaskSettings;fallback_capabilities:AITaskFallbacks}>(`/profiles/${encodeURIComponent(profileId)}/ai-task-settings`).then(r=>{if(sequence===editorSequence.current&&profileId===profileRef.current){setAITaskSettings(r.tasks);setAITaskFallbacks(r.fallback_capabilities);}}).catch(e=>setError(String(e))).finally(()=>{if(sequence===editorSequence.current)setAISettingsLoading(false);});}else setAISettingsLoading(false);},[profileId]);
  useEffect(()=>{++credentialSequence.current;setProviderCredentials([]);setCredentialDrafts({openai:'',anthropic:''});},[profileId]);
  useEffect(()=>{const sequence=++credentialSequence.current;if(!profileId||tab!=='AI Settings')return;const selected=profileId;void api<{credentials:ProviderCredential[]}>(`/profiles/${encodeURIComponent(selected)}/provider-credentials`).then(result=>{if(sequence===credentialSequence.current&&profileRef.current===selected)setProviderCredentials(result.credentials);}).catch(e=>{if(sequence===credentialSequence.current&&profileRef.current===selected)setError(String(e));});},[profileId,tab]);
  useEffect(()=>{setAICostPolicy(defaultAICostPolicy());setAICostPlan(null);setAICostHistory([]);setAICostReady(false);setAICostLoading(!!profileId);if(!profileId)return;const selected=profileId;void api<{policy:AICostPolicy}>(`/profiles/${encodeURIComponent(selected)}/ai-cost-policy`).then(result=>{if(profileRef.current===selected){setAICostPolicy(result.policy);setAICostReady(true);}}).catch(e=>{if(profileRef.current===selected)setError(String(e));}).finally(()=>{if(profileRef.current===selected)setAICostLoading(false);});void api<{runs:AICostEvaluationResult[]}>(`/profiles/${encodeURIComponent(selected)}/ai-cost-plan/evaluations`).then(result=>{if(profileRef.current===selected)setAICostHistory(result.runs.map(run=>({...run,document_candidate_ids:[],documents_created:0,deterministic_matches_unchanged:true})));}).catch(e=>{if(profileRef.current===selected)setError(String(e));});},[profileId]);
  useEffect(() => {++jobsSequence.current;++detailSequence.current;setJobs([]);setDetail(null);const timer=setTimeout(()=>void loadJobs().catch(e=>setError(String(e))),250);return()=>clearTimeout(timer);},[q,sort,remote,provider,profileId,showDismissed,selectedSearchProfileId]);
  async function action(label:string, fn:()=>Promise<void>) {
    if(busy)return;setBusy(label);setError('');setNotice('');
    try {await fn();await refresh(profileRef.current);await loadJobs(profileRef.current);}catch(e){setError(e instanceof Error?e.message:String(e));}finally{setBusy('');setActiveOp('');}
  }
  async function queue(path:string, body:unknown) {
    const result=await api<{operation_id:string;application_id?:string}>(path,{method:'POST',body:JSON.stringify(body)});
    setActiveOp(result.operation_id);
    const deadline=Date.now()+210000;
    while(Date.now()<deadline) {
      const operation=await api<Operation>('/operations/'+result.operation_id);
      setOps(current=>[operation,...current.filter(item=>item.id!==operation.id)]);
      if(terminal.has(operation.state)) {
        if(operation.state!=='COMPLETED')throw new Error(`${operation.state}: ${operation.error||'Review Activity for details'}`);
        setNotice('Completed. Your workspace has been updated.');return result;
      }
      await new Promise(resolve=>setTimeout(resolve,800));
    }
    throw new Error('This operation is still pending. Check Activity; it can recover when services return.');
  }
  async function discoverCompany(companyName:string, companyWebsite:string) {
    const result = await api<{status:string;operation_id:string|null}>('/companies/discovery', {
      method:'POST', body:JSON.stringify({company_name:companyName,company_website:companyWebsite,
        reason:'USER_ADDED_COMPANY',request_id:crypto.randomUUID(),...(profileId?{profile_id:profileId}:{})})
    });
    if (result.status === 'ALREADY_MONITORED') {
      setNotice('This company is already being monitored. Its saved source information is unchanged.');
      return;
    }
    if (!result.operation_id) throw new Error('Discovery could not be queued. Check Activity for details.');
    setActiveOp(result.operation_id);
    const deadline=Date.now()+210000;
    while(Date.now()<deadline) {
      const operation=await api<Operation>('/operations/'+result.operation_id);
      setOps(current=>[operation,...current.filter(item=>item.id!==operation.id)]);
      if(terminal.has(operation.state)) {
        if(operation.state!=='COMPLETED') throw new Error(`${operation.state}: ${operation.error||'Review Activity for details'}`);
        setNotice('Completed. Public company jobs and source information have been updated.');
        return;
      }
      await new Promise(resolve=>setTimeout(resolve,800));
    }
    throw new Error('This discovery is still pending. Check Activity; it can recover when services return.');
  }
  const request = () => ({profile_id:profileId,request_id:crypto.randomUUID()});
  function editManualCapture<K extends keyof ManualCaptureDraft>(key:K,value:ManualCaptureDraft[K]) {
    setManualCapture(current=>({...current,[key]:value}));
    setManualCaptureRequestId(crypto.randomUUID());
  }
  async function saveManualCapture() {
    await action('Saving captured job',async()=>{
      const sourceDetails:unknown=JSON.parse(manualCapture.source_details_json);
      if(!sourceDetails||typeof sourceDetails!=='object'||Array.isArray(sourceDetails))throw new Error('Other source fields must be a JSON object.');
      const result=await api<{job:Job;created:boolean}>('/jobs/manual',{method:'POST',body:JSON.stringify({
        request_id:manualCaptureRequestId,title:manualCapture.title,company:manualCapture.company,
        description:manualCapture.description,location:manualCapture.location.trim()||'Unknown',
        remote_type:manualCapture.remote_type,reference_url:manualCapture.reference_url.trim()||null,
        application_url:manualCapture.application_url.trim()||null,profile_id:profileId||null,
        discovery_origin:manualCapture.discovery_origin.trim()||null,source_details:sourceDetails,
      })});
      setManualCaptureOpen(false);setManualCapture(emptyManualCapture());setManualCaptureRequestId(crypto.randomUUID());
      setQ('');setRemote('');setProvider('');setSort('title');setDetail(result.job);
      if(profileId){
        try{await queue('/matches',request());await showJob(result.job.id);}
        catch{setNotice('Job saved; match refresh failed. The capture remains in your Jobs list. Use Refresh matches to retry.');return;}
      }
      setNotice(result.created?'Job captured and evaluated from approved profile facts. No job page was fetched.':'This capture was already saved and evaluated.');
    });
  }
  async function showJob(id:string) {
    const sequence=++detailSequence.current;const selected=profileRef.current;
    const result=await api<Job>('/jobs/'+id+(selected?'?profile_id='+encodeURIComponent(selected):''));
    if(sequence===detailSequence.current&&selected===profileRef.current)setDetail(result);
  }
  async function updateJobDisposition(disposition:JobTracking['disposition']) {
    if(!profileId||!detail)throw new Error('Select a profile and job before updating tracking.');
    await action('Saving job status',async()=>{
      const key=`${profileId}:${detail.id}:${disposition}`;
      const requestId=jobDispositionRequestIds.current.get(key)||crypto.randomUUID();
      jobDispositionRequestIds.current.set(key,requestId);
      const result=await api<JobTracking>(`/profiles/${encodeURIComponent(profileId)}/jobs/${encodeURIComponent(detail.id)}/tracking/disposition`,{method:'PUT',body:JSON.stringify({disposition,request_id:requestId})});
      jobDispositionRequestIds.current.delete(key);
      setDetail(current=>current?.id===detail.id?{...current,tracking:result}:current);
    });
  }
  async function addJobNote() {
    if(!profileId||!detail)throw new Error('Select a profile and job before adding a note.');
    const text=jobNoteDraft;
    await action('Saving job note',async()=>{
      const result=await api<JobTracking>(`/profiles/${encodeURIComponent(profileId)}/jobs/${encodeURIComponent(detail.id)}/tracking/notes`,{method:'POST',body:JSON.stringify({text,request_id:jobNoteRequestId})});
      setDetail(current=>current?.id===detail.id?{...current,tracking:result}:current);setJobNoteDraft('');setJobNoteRequestId(crypto.randomUUID());
    });
  }
  async function showApp(id:string) {
    const sequence=++appSequence.current;const selected=profileRef.current;
    if(!selected)return;
    const [application,documents,answerBank,tracking]=await Promise.all([api<Application>('/profiles/'+encodeURIComponent(selected)+'/applications/'+encodeURIComponent(id)),api<{documents:Doc[]}>('/profiles/'+encodeURIComponent(selected)+'/applications/'+encodeURIComponent(id)+'/documents'),api<{answers:Answer[]}>('/profiles/'+encodeURIComponent(selected)+'/applications/'+encodeURIComponent(id)+'/answers'),api<ApplicationTracking>('/profiles/'+encodeURIComponent(selected)+'/applications/'+encodeURIComponent(id)+'/tracking')]);
    if(sequence!==appSequence.current||selected!==profileRef.current||(selected&&application.profile_id!==selected))return;
    setSelectedApp({...application,answer_bank:answerBank.answers});setApplicationTracking(tracking);setApplicationStatusDraft(tracking.status||'');setApplicationNoteDraft('');setApplicationNoteRequestId(crypto.randomUUID());setDocs(documents.documents);setAnswerDrafts([]);setMockConsent(false);setTab('Applications');
  }
  async function updateApplicationStatus() {
    if(!selectedApp||!profileId||!applicationStatusDraft)throw new Error('Choose a manual application stage first.');
    const applicationId=selectedApp.id;const selectedProfile=profileId;const selectionSequence=appSequence.current;const status=applicationStatusDraft;
    await action('Saving reported application status',async()=>{
      const key=`${selectedProfile}:${applicationId}:${status}`;
      const requestId=applicationStatusRequestIds.current.get(key)||crypto.randomUUID();applicationStatusRequestIds.current.set(key,requestId);
      const result=await api<ApplicationTracking>(`/profiles/${encodeURIComponent(selectedProfile)}/applications/${encodeURIComponent(applicationId)}/tracking/status`,{method:'PUT',body:JSON.stringify({status,request_id:requestId})});
      applicationStatusRequestIds.current.delete(key);
      const prefix=`${selectedProfile}:${applicationId}:`;
      for(const pendingKey of applicationStatusRequestIds.current.keys())if(pendingKey.startsWith(prefix))applicationStatusRequestIds.current.delete(pendingKey);
      setApps(rows=>rows.map(row=>row.id===applicationId?{...row,pipeline_status:result.status,pipeline_attribution:result.attribution}:row));
      if(selectionSequence===appSequence.current)setApplicationTracking(result);
    });
  }
  async function addApplicationNote() {
    if(!selectedApp||!profileId)throw new Error('Select an application and profile before adding a note.');
    const text=applicationNoteDraft;const requestId=applicationNoteRequestId;const applicationId=selectedApp.id;const selectedProfile=profileId;const selectionSequence=appSequence.current;
    await action('Saving application note',async()=>{
      const result=await api<ApplicationTracking>(`/profiles/${encodeURIComponent(selectedProfile)}/applications/${encodeURIComponent(applicationId)}/tracking/notes`,{method:'POST',body:JSON.stringify({text,request_id:requestId})});
      if(selectionSequence!==appSequence.current)return;
      setApplicationTracking(result);setApplicationNoteDraft('');setApplicationNoteRequestId(crypto.randomUUID());
    });
  }
  async function recordExternalApplication() {
    if(!profileId)throw new Error('Select a profile before recording an application.');
    const appliedAt=new Date(externalAppliedAt);
    if(Number.isNaN(appliedAt.getTime()))throw new Error('Enter a valid application date and time.');
    const selectedProfile=profileId;
    const result=await api<{application_id:string;created:boolean}>('/applications/external',{method:'POST',body:JSON.stringify({
      profile_id:selectedProfile,request_id:externalCaptureRequestId.current,
      company:externalCompany,title:externalTitle,source:externalSource,
      applied_at:appliedAt.toISOString(),application_url:externalUrl||null,
      source_url:externalSourceUrl.trim()||null,discovery_origin:externalOrigin.trim()||null,
      job_description:externalDescription||null,
    })});
    if(selectedProfile!==profileRef.current)return;
    await refresh(selectedProfile);
    await showApp(result.application_id);
    externalCaptureRequestId.current=crypto.randomUUID();
    setExternalCompany('');setExternalTitle('');setExternalUrl('');setExternalSourceUrl('');setExternalOrigin('');setExternalDescription('');
    setExternalAppliedAt(localDateTimeInput());
    setNotice(result.created?'External application report recorded. This is your report, not confirmation from the employer.':'This external application report was already recorded.');
  }
  async function reviewSourceCandidate(candidate:SourceCandidate,state:'NEEDS_REVIEW'|'INTEGRATION_QUEUED'|'BLOCKED'|'REJECTED') {
    if(!profileId)throw new Error('Select a career profile before reviewing a source candidate.');
    const reason=(sourceCandidateReviewDraft[candidate.id]||'').trim();
    if(!reason)throw new Error('Enter the evidence or reason for this review decision.');
    const evaluation=sourceCandidateEvaluationDraft[candidate.id];
    if(!evaluation||![evaluation.api_feed_evidence_note,evaluation.access_terms_note,evaluation.capabilities_limits_note,evaluation.expected_value_maintenance_note].every(value=>value.trim()))throw new Error('Complete each source evaluation note before saving the review.');
    if(evaluation.api_feed_evidence_status==='PUBLIC_DOCUMENTED'&&!evaluation.api_feed_evidence_reference.trim())throw new Error('Add a reference URL for documented public API or feed evidence.');
    await action('Saving source-candidate review',async()=>{
      await api(`/profiles/${encodeURIComponent(profileId)}/source-candidates/${encodeURIComponent(candidate.id)}/review`,{
        method:'PUT',body:JSON.stringify({state,site_type:sourceCandidateSiteTypeDraft[candidate.id]||candidate.site_type,reason,...evaluation,api_feed_evidence_reference:evaluation.api_feed_evidence_reference.trim()||null,request_id:crypto.randomUUID()}),
      });
      setNotice('Source-candidate review saved. Collection remains disabled.');
    });
  }
  function updateSourceCandidateEvaluation(candidate:SourceCandidate,field:keyof SourceCandidateEvaluationDraft,value:string) {
    const candidateId=candidate.id;
    setSourceCandidateEvaluationDraft(current=>{
      const latest=candidate.review_history[candidate.review_history.length-1];
      const saved=current[candidateId]||{api_feed_evidence_status:latest?.api_feed_evidence_status||'UNKNOWN',api_feed_evidence_reference:latest?.api_feed_evidence_reference||'',api_feed_evidence_note:latest?.api_feed_evidence_note||'',access_terms_note:latest?.access_terms_note||'',capabilities_limits_note:latest?.capabilities_limits_note||'',expected_value_maintenance_note:latest?.expected_value_maintenance_note||''};
      return {...current,[candidateId]:{...saved,[field]:value}};
    });
  }
  async function reportSourceCandidate() {
    if(!profileId)throw new Error('Select a profile before recording a source URL.');
    if(!sourceReportUrl.trim())throw new Error('Enter the source or application URL you want to catalogue.');
    const selectedProfile=profileId;
    await action('Recording source URL',async()=>{
      const result=await api<{created:boolean}>('/source-candidates',{method:'POST',body:JSON.stringify({
        profile_id:selectedProfile,request_id:crypto.randomUUID(),
        application_url:null,discovery_origin:sourceReportOrigin.trim()||null,
        source_url:sourceReportUrl.trim(),observed_hops:[],
      })});
      if(selectedProfile!==profileRef.current)return;
      await refresh(selectedProfile);
      setSourceReportUrl('');setSourceReportOrigin('');
      setNotice(result.created?'Source URL recorded for review. No site was opened and collection remains disabled.':'Source URL report already exists. No site was opened.');
    });
  }
  function switchProfile(id:string) {
    if(id===profileId)return;
    profileRef.current=id;
    ++companySourcesSequence.current;++jobsSequence.current;++detailSequence.current;
    ++appSequence.current;++editorSequence.current;
    profileBaselineRef.current=null;
    setDetail(null);setJobs([]);setJobNoteDraft('');setJobNoteRequestId(crypto.randomUUID());setAIDocumentPreparation(null);setAIDocumentReviewConsent({});setSourceCandidates([]);setSourceCandidateReviewDraft({});setSourceCandidateSiteTypeDraft({});setSourceReportUrl('');setSourceReportOrigin('');
    jobDispositionRequestIds.current.clear();applicationStatusRequestIds.current.clear();setSelectedApp(null);setApplicationTracking(null);setApplicationStatusDraft('');setApplicationNoteDraft('');setApplicationNoteRequestId(crypto.randomUUID());externalCaptureRequestId.current=crypto.randomUUID();setExternalCompany('');setExternalTitle('');setExternalUrl('');setExternalSourceUrl('');setExternalOrigin('');setExternalDescription('');setExternalAppliedAt(localDateTimeInput());setAnswerDrafts([]);
    setApps([]);setDocs([]);setResumes([]);setOps([]);setCompanySources([]);
    setDiscoveryRequests([]);setSearchProfiles([]);setMockConsent(false);setActiveOp('');
    setSearchName('');setSearchTitles('');setSearchRemote('ANY');setSearchEmployment('FULL_TIME');setSearchSource('');
    setEditor(newProfile());setError('');setNotice('');setProfileId(id);
  }
  async function prepareJobRoute(job:Job, routeMode:'DEFAULT'|'THIS_ROUTE'='DEFAULT', routeId?:string) {
    if(tailorResume&&resumeSelection&&(!resumeSelection.version||!resumeSelection.ids.length||resumeSelection.profileId!==profileId||resumeSelection.jobId!==job.id))throw new Error('Reload approved facts and select at least one fact for this resume.');
    if(tailorResume&&resumeSelection?.rewrites.some(item=>!item.reviewed))throw new Error('Review each rewritten resume fact before preparing documents.');
    const op=await queue('/applications',{profile_id:profileId,job_id:job.id,request_id:crypto.randomUUID(),route_mode:routeMode,tailor_resume:tailorResume,include_resume_contact:includeResumeContact,
      cover_letter_length:coverLetterLength,cover_letter_style:coverLetterStyle,
      ...(tailorResume&&resumeSelection?{tailor_fact_ids:resumeSelection.ids,expected_profile_version:resumeSelection.version}:{}),
      ...(tailorResume&&resumeSelection?.rewrites.length?{tailor_fact_rewrites:resumeSelection.rewrites}:{}),...(routeId?{route_id:routeId}:{}),
      ...(aiDocumentPreparation?.jobId===job.id?{ai_evaluation_run_id:aiDocumentPreparation.runId}:{})});
    if(op.application_id)await showApp(op.application_id);
    if(aiDocumentPreparation?.jobId===job.id)setAIDocumentPreparation(null);
  }
  async function demo() {
    const id='demo-'+crypto.randomUUID();
    const profile:Profile={id,name:'Fictional Test Applicant',email:'fixture@example.invalid',facts:[{id:'python',kind:'skill',text:'Python',source:'Fictional test fixture',verified:true,user_approved:true,confidence:100},{id:'sql',kind:'skill',text:'SQL',source:'Fictional test fixture',verified:true,user_approved:true,confidence:100}],answers:[{id:'country',text:'United States',source:'Fictional test fixture',verified:true,user_approved:true,confidence:100},{id:'remote',text:'yes',source:'Fictional test fixture',verified:true,user_approved:true,confidence:100},{id:'fixture_question',text:'Controlled fictional test application.',source:'Fictional test fixture',verified:true,user_approved:true,confidence:100}]};
    await api('/profiles',{method:'PUT',body:JSON.stringify(profile)});switchProfile(id);setEditor({...profile,id:''});
    await queue('/discovery',{profile_id:id,request_id:crypto.randomUUID()});await refresh(id);await loadJobs(id);setTab('Jobs');setNotice('Fictional demo loaded. Mock applications stay inside this local test system.');
  }
  function editFact(index:number,change:Partial<Fact>) {setEditor(current=>({...current,facts:current.facts.map((fact,i)=>i===index?updateReviewedFact(fact,change):fact)}));}
  function editAnswer(index:number,change:Partial<Answer>) {setEditor(current=>({...current,answers:current.answers.map((answer,i)=>i===index?({...answer,...change,...(('question'in change||'text'in change||'source'in change||'confidence'in change)?{verified:false,user_approved:false}:{})}):answer)}));}
  function needsMatchRefresh(clean:{facts:Fact[];preferences:Partial<Preferences>}) {
    const baseline=profileBaselineRef.current;
    if(!baseline)return true;
    const factProjection=(rows:Fact[])=>rows.map(f=>({id:f.fact_id||f.id,kind:f.kind||'skill',text:f.text,source:f.source,confidence:f.confidence,verified:f.verified,user_approved:f.user_approved}));
    const stable=(value:object)=>JSON.stringify(Object.fromEntries(Object.entries(value).sort(([a],[b])=>a.localeCompare(b))));
    const matchingPreferences=(preferences:Partial<Preferences>)=>Object.fromEntries(Object.entries(preferences).filter(([key])=>key!=='avoid_terms'));
    return JSON.stringify(factProjection(clean.facts))!==JSON.stringify(factProjection(baseline.facts))||stable(matchingPreferences(clean.preferences))!==stable(matchingPreferences(baseline.preferences||{}));
  }
  async function exportAnswers() {
    if(!selectedApp||!profileId)throw new Error('Select an application and profile before exporting answers');
    const result=await api<{document_id:string}>(`/profiles/${encodeURIComponent(profileId)}/applications/${encodeURIComponent(selectedApp.id)}/answers/export`,{method:'POST',body:JSON.stringify({profile_id:profileId,items:answerDrafts})});
    await showApp(selectedApp.id);setNotice(`Reviewed answer export saved as document ${result.document_id.slice(0,10)}.`);
  }
  function setContact(key:keyof Contact,value:string) { setEditor(current=>({...current,contact:{...current.contact,[key]:value}})); }
  function setPreference(key:keyof Preferences,value:unknown) { setEditor(current=>({...current,preferences:{...current.preferences,[key]:value}})); }
  function setAITaskProvider(task:AITaskKey,value:AIProviderChoice) {
    setAITaskSettings(current=>({...current,[task]:{
      provider:value,model:value==='disabled'?null:current[task].model,
    }}));
  }
  function setAITaskModel(task:AITaskKey,value:string) {
    setAITaskSettings(current=>({...current,[task]:{...current[task],model:value||null}}));
  }
  async function saveAITaskSettings() {
    if(!profileId)throw new Error('Select an active profile before saving AI task settings.');
    const result=await api<{tasks:AITaskSettings;fallback_capabilities:AITaskFallbacks}>(`/profiles/${encodeURIComponent(profileId)}/ai-task-settings`,{
      method:'PUT',body:JSON.stringify({tasks:aiTaskSettings}),
    });
    setAITaskSettings(result.tasks);
    setAITaskFallbacks(result.fallback_capabilities);
    setNotice('AI task settings saved for this profile.');
  }
  async function saveProviderCredential(provider:CredentialProvider) {
    const secret=credentialDrafts[provider];
    if(!profileId||!secret)throw new Error('Enter a provider credential before saving.');
    const result=await api<ProviderCredential & {profile_id:string}>(`/profiles/${encodeURIComponent(profileId)}/provider-credentials/${provider}`,{method:'PUT',body:JSON.stringify({secret})});
    setProviderCredentials(current=>[...current.filter(item=>item.provider!==provider),result].sort((a,b)=>a.provider.localeCompare(b.provider)));
    setCredentialDrafts(current=>({...current,[provider]:''}));
    setNotice(`${provider==='openai'?'OpenAI':'Anthropic'} credential saved encrypted. The secret is not shown again.`);
  }
  async function testProviderCredential(provider:CredentialProvider) {
    if(!profileId)throw new Error('Select a profile before testing a provider credential.');
    const result=await api<{status:'CONNECTED'|'FAILED';code:string|null}>(`/profiles/${encodeURIComponent(profileId)}/provider-credentials/${provider}/test`,{method:'POST'});
    const metadata=await api<{credentials:ProviderCredential[]}>(`/profiles/${encodeURIComponent(profileId)}/provider-credentials`);
    setProviderCredentials(metadata.credentials);
    setNotice(result.status==='CONNECTED'?'Provider authentication succeeded. No model inference was run.':`Provider check failed (${result.code||'CREDENTIAL_CHECK_FAILED'}).`);
  }
  async function deleteProviderCredential(provider:CredentialProvider) {
    if(!profileId)throw new Error('Select a profile before deleting a provider credential.');
    if(!window.confirm(`Delete the saved ${provider==='openai'?'OpenAI':'Anthropic'} credential for this profile?`))return;
    await api(`/profiles/${encodeURIComponent(profileId)}/provider-credentials/${provider}`,{method:'DELETE'});
    setProviderCredentials(current=>current.filter(item=>item.provider!==provider));
    setCredentialDrafts(current=>({...current,[provider]:''}));
    setNotice('Provider credential deleted.');
  }
  function setAICostValue<K extends keyof AICostPolicy>(key:K,value:number) {
    setAICostPolicy(current=>{const next={...current,[key]:value};if(key==='max_ai_candidates'&&next.max_document_candidates>value)next.max_document_candidates=value;return next;});setAICostPlan(null);
  }
  async function saveAICostPolicy() {
    if(!profileId)throw new Error('Select an active profile before saving AI cost controls.');
    const result=await api<{policy:AICostPolicy}>(`/profiles/${encodeURIComponent(profileId)}/ai-cost-policy`,{method:'PUT',body:JSON.stringify(aiCostPolicy)});
    setAICostPolicy(result.policy);setAICostPlan(null);setNotice('AI cost policy saved for this profile.');
  }
  async function previewAICostPlan() {
    if(!profileId)throw new Error('Select an active profile before previewing AI work.');
    const result=await api<{plan:AICostPlan}>(`/profiles/${encodeURIComponent(profileId)}/ai-cost-plan`);
    if(profileRef.current===profileId)setAICostPlan(result.plan);
  }
  async function evaluateAICostPlan() {
    if(!profileId)throw new Error('Select an active profile before evaluating AI candidates.');
    if(!aiCostPlan)throw new Error('Preview the saved AI work plan before evaluation.');
    const selected=profileId;
    const requestId=crypto.randomUUID();
    aiEvaluationRequest.current={profileId:selected,requestId};
    setAIEvaluationRunning(true);setAIEvaluationCancelling(false);
    try {
      const result=await api<AICostEvaluationResult&{cancelled?:boolean}>(
        `/profiles/${encodeURIComponent(selected)}/ai-cost-plan/evaluate`,
        {method:'POST',body:JSON.stringify({request_id:requestId})});
      if(profileRef.current!==selected)return;
      if(result.cancelled){setNotice('AI evaluation canceled. The interrupted batch was discarded.');return;}
      setAICostHistory(current=>[result,...current.filter(run=>run.run_id!==result.run_id)].slice(0,20));
    } finally {
      if(aiEvaluationRequest.current?.requestId===requestId)aiEvaluationRequest.current=null;
      setAIEvaluationRunning(false);setAIEvaluationCancelling(false);
    }
  }
  async function cancelAICostEvaluation() {
    const active=aiEvaluationRequest.current;
    if(!active||aiEvaluationCancelling)return;
    setAIEvaluationCancelling(true);
    try {
      const result=await api<{cancelled:boolean}>(
        `/profiles/${encodeURIComponent(active.profileId)}/ai-cost-plan/evaluations/${encodeURIComponent(active.requestId)}/cancel`,
        {method:'POST'});
      if(profileRef.current!==active.profileId)return;
      if(result.cancelled) {
        setNotice('Cancellation accepted. Waiting for the bounded provider call; partial results will not be saved.');
      } else {
        setAIEvaluationCancelling(false);
        setNotice('Cancellation was not confirmed. The AI evaluation may still complete.');
      }
    } catch(error) {
      setAIEvaluationCancelling(false);
      setError(error instanceof Error?error.message:String(error));
    }
  }
  function aiReviewKey(runId:string,jobId:string) {return `${runId}:${jobId}`;}
  async function confirmAIDocumentReview(run:AICostEvaluationResult,item:AICostEvaluation) {
    if(!profileId)throw new Error('Select a profile before reviewing document candidates.');
    const selected=profileId;
    await api(`/profiles/${encodeURIComponent(selected)}/ai-cost-plan/evaluations/${encodeURIComponent(run.run_id)}/review-documents`,{
      method:'POST',body:JSON.stringify({job_ids:[item.job_id]}),
    });
    if(profileRef.current!==selected)return;
    setAICostHistory(rows=>rows.map(current=>current.run_id===run.run_id?{
      ...current,evaluations:current.evaluations.map(candidate=>candidate.job_id===item.job_id?{...candidate,document_reviewed:true}:candidate),
    }:current));
    setAIDocumentReviewConsent(current=>({...current,[aiReviewKey(run.run_id,item.job_id)]:false}));
    setAIDocumentPreparation({runId:run.run_id,jobId:item.job_id});
    await showJob(item.job_id);
    setTab('Jobs');
    setNotice('Review saved. Confirm document preparation on the job page; nothing has been submitted to an employer.');
  }
  async function openReviewedAIDocumentCandidate(run:AICostEvaluationResult,item:AICostEvaluation) {
    setAIDocumentPreparation({runId:run.run_id,jobId:item.job_id});
    await showJob(item.job_id);
    setTab('Jobs');
  }
  function addExtraction(extraction:Extraction, selectedFacts:Set<string>, selectedContact:Set<string>) {
    const result = addSelectedExtraction({name:editor.name,email:editor.email,contact:editor.contact as Record<string,unknown>,facts:editor.facts.filter(f=>f.text.trim())}, extraction, selectedFacts, selectedContact);
    setEditor(current=>({...current,name:result.profile.name,email:result.profile.email,contact:result.profile.contact as Partial<Contact>,facts:result.profile.facts as Fact[]}));
    setNotice(result.truncated?'Profile has reached 200 facts. Some suggestions were not added. Review existing facts before importing more.':`${result.added} pending facts added; ${result.skipped} existing suggestions skipped. Review, verify and approve before saving.`);
  }
  return <div className="layout" data-operation-mode={operationMode||'LOADING'}>
    <aside className="sidebar"><a className="brand" href="/">M<span>Jobs</span><small>YOUR LOCAL WORKSPACE</small></a><nav aria-label="Main navigation">{(['Jobs','Profile','Sources','Applications','AI Settings','Activity'] as Tab[]).map(item=><button key={item} aria-current={tab===item?'page':undefined} onClick={()=>setTab(item)}>{item}<span>{item==='Jobs'?jobs.length:item==='Applications'?apps.length:''}</span></button>)}</nav><div className="side-note"><strong>Built around your facts.</strong><p>Discover opportunities, prepare truthful documents, and keep a clear record.</p><span className="status">External submission OFF</span></div></aside>
    <main><header><div className="eyebrow">LOCAL FIRST · USER TESTING</div><div className="header-row"><h1>{tab==='Jobs'?'Find your next opportunity.':tab==='Profile'?'Your experience, in your words.':tab==='Sources'?'Choose where to look.':tab==='Applications'?'Every application, accounted for.':tab==='AI Settings'?'Choose models by task.':'See what is happening.'}</h1><span className={'status '+(ready?.status==='ready'?'good':'warn')}>{ready?.status==='ready'?'Services ready':'Services '+(ready?.status||'checking')}</span></div><div className="profile-switch"><label htmlFor="active-profile">Active profile</label><select id="active-profile" disabled={!!busy} value={profileId} onChange={e=>switchProfile(e.target.value)}><option value="">Select a profile</option>{profiles.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select><button className="quiet" disabled={!!busy} onClick={()=>{++editorSequence.current;switchProfile('');profileBaselineRef.current=null;setEditor(newProfile());setTab('Profile');}}>New profile</button></div></header>
      <div className="safety">Real applications require your own manual review and submission. This workspace cannot submit to an external employer.</div>
      {error&&<div role="alert" className="alert"><strong>Action needs attention</strong><p>{error}</p></div>}
      {notice&&<div role="status" className="notice">{notice}</div>}
      {busy&&<div role="status" className="notice">{busy}… {activeOp&&<button className="quiet" onClick={()=>void api('/operations/'+activeOp+'/cancel',{method:'POST'}).then(()=>setNotice('Cancellation requested.')).catch(e=>setError(String(e)))}>Cancel operation</button>}</div>}
      {tab==='AI Settings'&&<section className="panel" aria-label="AI task settings"><div className="toolbar"><div><h2>AI task configuration</h2><p>Choose a provider and model independently for each task and profile.</p></div></div><p className="muted">All tasks start disabled. Ollama uses the configured loopback-only local service. Saving a selection does not make a model call. Saved provider credentials below are separate from model routing.</p><section className="fact" aria-label="No-AI fallback modes"><h3>No-AI fallback modes</h3><p className="muted">These are the explicit behaviors when a task has no usable AI provider. Deterministic paths do not generate unsupported career claims; unavailable tasks stay unavailable.</p><ul>{aiTaskRows.map(({key,label})=>{const fallback=aiTaskFallbacks[key];return <li key={key} aria-label={`${label} no-AI behavior`}>{label}: {fallback?fallback.mode.replaceAll('_',' '):'Loading'}{fallback?` — ${fallback.description}`:''}</li>;})}</ul></section>{!profileId&&<p className="muted">Select a profile to view and save its AI settings.</p>}{aiSettingsLoading&&<p role="status">Loading AI task settings…</p>}<div className="ai-task-settings">{aiTaskRows.map(({key,label})=><fieldset className="fact" key={key}><legend>{label}</legend><div className="two-col"><label>{label} provider<select aria-label={`${label} provider`} disabled={!!busy||!profileId||aiSettingsLoading} value={aiTaskSettings[key].provider} onChange={event=>setAITaskProvider(key,event.target.value as AIProviderChoice)}><option value="disabled">Disabled</option><option value="ollama">Ollama (local)</option></select></label><label>{label} model<input aria-label={`${label} model`} maxLength={128} disabled={!!busy||!profileId||aiSettingsLoading||aiTaskSettings[key].provider==='disabled'} value={aiTaskSettings[key].model||''} onChange={event=>setAITaskModel(key,event.target.value)} placeholder="For example, llama3.2:3b"/></label></div></fieldset>)}</div><button className="primary" type="button" disabled={!!busy||!profileId||aiSettingsLoading} onClick={()=>void action('Saving AI task settings',saveAITaskSettings)}>Save AI task settings</button></section>}
      {tab==='AI Settings'&&<section className="panel" aria-label="Provider credentials"><div className="toolbar"><div><h2>Provider credentials</h2><p>Store optional OpenAI or Anthropic keys for this profile.</p></div></div><p className="muted">Credentials are encrypted in the database with the server-only key from the private project .env file. MJobs returns masked metadata only. Saving a key does not enable model inference. Test connection sends one bounded authentication GET to the provider after you click; it sends no prompt, job, or profile data.</p>{!profileId&&<p className="muted">Select a profile before managing provider credentials.</p>}{(['openai','anthropic'] as CredentialProvider[]).map(provider=>{const credential=providerCredentials.find(item=>item.provider===provider);const label=provider==='openai'?'OpenAI':'Anthropic';return <fieldset className="fact" key={provider}><legend>{label}</legend><p aria-live="polite">{credential?`Configured · ${credential.masked} · rotated ${credential.rotation_count} time${credential.rotation_count===1?'':'s'} · last check: ${credential.last_test_status==='NOT_TESTED'?'not tested':credential.last_test_status.toLowerCase()} (${stamp(credential.last_tested_at)})`:'No credential saved for this profile.'}</p><label>{credential?'Replace or rotate credential':'Provider credential'}<input aria-label={`${label} credential`} type="password" autoComplete="new-password" spellCheck={false} maxLength={16384} disabled={!!busy||!profileId} value={credentialDrafts[provider]} onChange={event=>setCredentialDrafts(current=>({...current,[provider]:event.target.value}))}/></label><div className="toolbar"><button className="primary" type="button" disabled={!!busy||!profileId||!credentialDrafts[provider]} onClick={()=>void action(`Saving ${label} credential`,()=>saveProviderCredential(provider))}>{credential?'Rotate credential':'Save credential'}</button><button type="button" disabled={!!busy||!profileId||!credential} onClick={()=>void action(`Testing ${label} credential`,()=>testProviderCredential(provider))}>Test connection</button><button type="button" disabled={!!busy||!profileId||!credential} onClick={()=>void action(`Deleting ${label} credential`,()=>deleteProviderCredential(provider))}>Delete credential</button></div></fieldset>;})}</section>}
      {tab==='AI Settings'&&<section className="panel" aria-label="AI cost controls"><div className="toolbar"><div><h2>AI cost controls</h2><p>Bound the deterministic funnel before any model or document work.</p></div></div><p className="muted">Defaults scan up to 5,000 jobs, require a score of 75, cap AI candidates at 75 and cap reviewed-document candidates at 20. Lower the limits to fit your workflow. Preview reads this profile’s deterministic match scores and makes no model calls. Evaluation runs only after you request it, uses the saved job-scoring provider, and creates no documents.</p>{aiCostLoading&&<p role="status">Loading AI cost policy…</p>}<div className="two-col"><label>Minimum deterministic score<input aria-label="Minimum deterministic score" type="number" min="0" max="100" step="1" disabled={!!busy||!profileId||aiCostLoading||!aiCostReady} value={aiCostPolicy.minimum_score} onChange={event=>setAICostValue('minimum_score',Number(event.target.value))}/></label><label>Maximum jobs scanned<input aria-label="Maximum jobs scanned" type="number" min="1" max="100000" step="1" disabled={!!busy||!profileId||aiCostLoading||!aiCostReady} value={aiCostPolicy.max_input_candidates} onChange={event=>setAICostValue('max_input_candidates',Number(event.target.value))}/></label><label>Maximum AI candidates<input aria-label="Maximum AI candidates" type="number" min="0" max="75" step="1" disabled={!!busy||!profileId||aiCostLoading||!aiCostReady} value={aiCostPolicy.max_ai_candidates} onChange={event=>setAICostValue('max_ai_candidates',Number(event.target.value))}/></label><label>Maximum document candidates<input aria-label="Maximum document candidates" type="number" min="0" max={Math.min(20,aiCostPolicy.max_ai_candidates)} step="1" disabled={!!busy||!profileId||aiCostLoading||!aiCostReady} value={aiCostPolicy.max_document_candidates} onChange={event=>setAICostValue('max_document_candidates',Number(event.target.value))}/></label></div><div className="toolbar"><button className="primary" type="button" disabled={!!busy||!profileId||aiCostLoading||!aiCostReady} onClick={()=>void action('Saving AI cost policy',saveAICostPolicy)}>Save AI cost policy</button><button type="button" disabled={!!busy||!profileId||aiCostLoading||!aiCostReady} onClick={()=>void action('Previewing AI work plan',previewAICostPlan)}>Preview AI work plan</button>{aiEvaluationRunning?<button type="button" disabled={aiEvaluationCancelling} onClick={()=>void cancelAICostEvaluation()}>{aiEvaluationCancelling?'Cancellation requested':'Cancel AI evaluation'}</button>:<button type="button" disabled={!!busy||!profileId||!aiCostPlan||!aiCostPlan.ai_candidate_count} onClick={()=>void action('Evaluating planned AI candidates',evaluateAICostPlan)}>Evaluate planned AI candidates</button>}</div>{aiCostPlan&&<p aria-live="polite">{aiCostPlan.scanned_job_count} jobs scanned · {aiCostPlan.eligible_count} eligible · {aiCostPlan.ai_candidate_count} AI {aiCostPlan.ai_candidate_count===1?'candidate':'candidates'} · {aiCostPlan.document_candidate_count} reviewed-document candidates</p>}<section aria-label="AI evaluation history">{!aiCostHistory.length?<p>No saved AI evaluations for this profile.</p>:aiCostHistory.map(run=><section aria-label="AI evaluation results" key={run.run_id}><h3>AI evaluations for review</h3><p role="status">{run.evaluated_candidate_count} candidates evaluated. These advisory results do not change deterministic matches. {run.documents_created} documents created.</p><p className="muted">Saved {stamp(run.created_at)} · profile version {run.profile_version.slice(0,12)} · {run.task_provider}/{run.task_model} · score threshold {run.policy.minimum_score}</p>{run.evaluations.map(item=><article className="fact ai-evaluation" key={item.job_id}><h4>{item.title} · {item.company}</h4><p><strong>{item.category}</strong> — {item.rationale}</p><p className="muted">Deterministic score: {item.deterministic_score} · Job ID: {item.job_id}</p>{item.document_reviewed?<button type="button" disabled={!!busy||!profileId} onClick={()=>void action('Opening reviewed job',()=>openReviewedAIDocumentCandidate(run,item))}>Prepare reviewed documents</button>:<><label className="consent"><input aria-label="I reviewed this AI result and approve document preparation" type="checkbox" disabled={!!busy||!profileId||run.policy.max_document_candidates===0||run.evaluations.filter(candidate=>candidate.document_reviewed).length>=run.policy.max_document_candidates} checked={!!aiDocumentReviewConsent[aiReviewKey(run.run_id,item.job_id)]} onChange={event=>setAIDocumentReviewConsent(current=>({...current,[aiReviewKey(run.run_id,item.job_id)]:event.target.checked}))}/> I reviewed this AI result and approve document preparation</label><button type="button" disabled={!!busy||!profileId||!aiDocumentReviewConsent[aiReviewKey(run.run_id,item.job_id)]||run.policy.max_document_candidates===0||run.evaluations.filter(candidate=>candidate.document_reviewed).length>=run.policy.max_document_candidates} onClick={()=>void action('Confirming document review',()=>confirmAIDocumentReview(run,item))}>Confirm review and open job</button></>}</article>)}</section>)}</section><p className="muted">Provider calls can incur cost. Evaluation is an explicit action, follows the saved per-profile threshold and candidate limits, and creates no documents. Document preparation requires a separate review and confirm action; it uses the existing factual workflow and never submits an application.</p></section>}
      {tab==='Jobs'&&<>
        <div className="toolbar"><p>{jobs.length} stored opportunities <span className="muted">· matching uses approved facts</span></p><div><button disabled={!!busy} onClick={()=>setManualCaptureOpen(value=>!value)}>{manualCaptureOpen?'Close capture':'Capture a job'}</button>{' '}<button disabled={!!busy||!profileId} onClick={()=>void action('Refreshing matches',async()=>{await queue('/matches',request());})}>Refresh matches</button></div></div>
        {!profileId&&<section className="welcome"><h2>Start with a profile.</h2><p>{operationMode==='FIXTURE'?'Add your own sourced career facts, or explore the flow with fictional demo data.':'Add your own sourced career facts to begin.'}</p><button className="primary" onClick={()=>setTab('Profile')}>Set up my profile</button> {operationMode==='FIXTURE'&&<button disabled={!!busy} onClick={()=>void action('Loading fictional demo',demo)}>Load fictional demo</button>}</section>}
        {manualCaptureOpen&&<form className="panel" aria-label="Manual job capture" onSubmit={event=>{event.preventDefault();void saveManualCapture();}}>
          <div className="toolbar"><div><h2>Capture a job manually</h2><p className="muted">Enter what the posting says. MJobs stores these links as references and does not visit them.</p></div></div>
          <div className="two-col"><label>Job title<input required maxLength={500} value={manualCapture.title} onChange={event=>editManualCapture('title',event.target.value)}/></label><label>Company<input required maxLength={200} value={manualCapture.company} onChange={event=>editManualCapture('company',event.target.value)}/></label></div>
          <label>Job description<textarea required maxLength={50000} value={manualCapture.description} onChange={event=>editManualCapture('description',event.target.value)}/></label>
          <div className="two-col"><label>Location<input maxLength={1000} value={manualCapture.location} onChange={event=>editManualCapture('location',event.target.value)}/></label><label>Work arrangement<select value={manualCapture.remote_type} onChange={event=>editManualCapture('remote_type',event.target.value)}><option value="UNKNOWN">Unknown</option><option value="REMOTE_US">Remote — US</option><option value="REMOTE_WORLDWIDE">Remote — worldwide</option><option value="REMOTE_COUNTRY">Remote — country restricted</option><option value="REMOTE_SPECIFIC_STATES">Remote — state restricted</option><option value="HYBRID">Hybrid</option><option value="ONSITE">On site</option></select></label></div>
          <div className="two-col"><label>Job reference URL<input type="url" maxLength={2000} value={manualCapture.reference_url} onChange={event=>editManualCapture('reference_url',event.target.value)} placeholder="https://…"/></label><label>Application URL<input type="url" maxLength={2000} value={manualCapture.application_url} onChange={event=>editManualCapture('application_url',event.target.value)} placeholder="https://…"/></label></div>
          <div className="two-col"><label>Reported discovery origin<input maxLength={253} value={manualCapture.discovery_origin} onChange={event=>editManualCapture('discovery_origin',event.target.value)} placeholder="For example, LinkedIn"/></label><p className="muted">This is your attribution; MJobs does not verify it or open either link.</p></div>
          <label>Other source fields (JSON)<textarea maxLength={100000} value={manualCapture.source_details_json} onChange={event=>editManualCapture('source_details_json',event.target.value)}/></label>
          <p className="muted">Provide a job reference URL or application URL. Unknown information stays unknown. Saving does not submit an application.</p>
          <button className="primary" disabled={!!busy}>Save captured job</button>{' '}<button type="button" className="quiet" disabled={!!busy} onClick={()=>setManualCaptureOpen(false)}>Cancel</button>
        </form>}
        <div className="filters"><label>Saved search profile<select aria-label="Saved search profile" value={selectedSearchProfileId} disabled={!!busy||!profileId} onChange={event=>setSelectedSearchProfileId(event.target.value)}><option value="">All jobs</option>{searchProfiles.filter(search=>search.profile_id===profileId).map(search=><option key={search.id} value={search.id}>{search.name}</option>)}</select></label><label>Search jobs<input type="search" placeholder="Title, company, description, skills or location" value={q} onChange={e=>setQ(e.target.value)}/></label><label>Work location<select aria-label="Work location" value={remote} onChange={e=>setRemote(e.target.value)}><option value="">All locations</option><option>REMOTE</option><option>HYBRID</option><option>ONSITE</option><option>UNKNOWN</option></select></label><label>Source<select aria-label="Source" value={provider} onChange={e=>setProvider(e.target.value)}><option value="">All sources</option><option value="greenhouse">Greenhouse</option><option value="mock-ats">Fictional mock</option></select></label><label>Sort<select aria-label="Sort" value={sort} onChange={e=>setSort(e.target.value)}><option value="score">Match score</option><option value="title">Title</option></select></label></div>
        <label className="consent"><input type="checkbox" checked={showDismissed} onChange={e=>setShowDismissed(e.target.checked)}/> Show hidden and not interested</label>
        <div className="job-grid"><div className="job-list">{jobs.map(job=><button className={'job-card '+(detail?.id===job.id?'selected':'')} key={job.id} onClick={()=>void showJob(job.id).catch(e=>setError(String(e)))}><div className="card-top"><span className="eyebrow">{job.company}</span><span className="score">{job.match_score===null?'Unscored':job.match_score+'%'}</span></div><h2>{job.title}</h2><p>{job.location} · {job.remote_type}</p><div className="card-foot"><span className={'tag '+(job.attributes.fictional?'fictional':'')}>{job.attributes.fictional?'Fictional demo':job.provider==='manual'?'Manual capture · user-entered':job.provider+' · read only'}</span><span>{job.posting_count} posting{job.posting_count===1?'':'s'}</span></div></button>)}{jobs.length===0&&<section className="empty"><h2>No matching jobs yet.</h2><p>{operationMode==='FIXTURE'?'Clear your filters, load a fictional demo, or configure a public job board in Sources.':'Clear your filters or configure a public job board in Sources.'}</p><button onClick={()=>setTab('Sources')}>Manage sources</button>{' '}<button onClick={()=>setManualCaptureOpen(true)}>Capture a job</button></section>}</div>
        <section className="job-detail">{detail?<><span className="eyebrow">{detail.company}</span><h2>{detail.title}</h2><p>{detail.location} · {detail.remote_type}</p>{profileId&&<section className="panel" aria-label="Job tracking"><h3>Track this job</h3><p>Current status: <strong>{detail.tracking?.disposition||'ACTIVE'}</strong></p><div className="toolbar"><button disabled={!!busy} onClick={()=>void updateJobDisposition(detail.tracking?.disposition==='SAVED'?'ACTIVE':'SAVED')}>{detail.tracking?.disposition==='SAVED'?'Unsave job':'Save job'}</button>{' '}<button disabled={!!busy} onClick={()=>void updateJobDisposition(detail.tracking?.disposition==='HIDDEN'?'ACTIVE':'HIDDEN')}>{detail.tracking?.disposition==='HIDDEN'?'Restore hidden job':'Hide job'}</button>{' '}<button disabled={!!busy} onClick={()=>void updateJobDisposition(detail.tracking?.disposition==='NOT_INTERESTED'?'ACTIVE':'NOT_INTERESTED')}>{detail.tracking?.disposition==='NOT_INTERESTED'?'Clear not interested':'Not interested'}</button></div><form onSubmit={event=>{event.preventDefault();void addJobNote();}}><label htmlFor="job-note">Private job note</label><textarea id="job-note" aria-label="Job note" maxLength={4000} value={jobNoteDraft} onChange={event=>{setJobNoteDraft(event.target.value);setJobNoteRequestId(crypto.randomUUID());}}/><button className="primary" disabled={!!busy||!jobNoteDraft.trim()}>Add note</button></form><h4>Tracking history</h4>{detail.tracking?.history?.length?<ol aria-label="Job tracking history">{detail.tracking.history.map((event,index)=><li key={event.id||`${event.type}-${event.timestamp}-${index}`}><strong>{event.type==='note'?'Note':'Status: '+event.disposition}</strong>{event.text&&<p>{event.text}</p>}<small>{stamp(event.timestamp)}</small></li>)}</ol>:<p className="muted">No status changes or notes yet.</p>}</section>}<div className="divider"/><h3>Why this match?</h3>{detail.match?<><strong className="big-score">{detail.match.score}%</strong><p>Weighted coverage of detected skill evidence; this is not a qualification assessment.</p><p><strong>Supported by approved facts:</strong> {detail.match.components.matched?.join(', ')||'None'}</p><p><strong>Not found in approved profile facts:</strong> {detail.match.missing.join(', ')||'None detected'}</p>{detail.match.components.requirements?.length?<ul aria-label="Skill match explanation">{detail.match.components.requirements.map((row,index)=><li key={row.skill+index}><strong>{row.skill}</strong> — {row.strength.replaceAll('_',' ')}; {row.profile_evidence==='APPROVED'?'supported by an approved fact':'no approved profile evidence'}<span className="muted">. {row.explanation}</span></li>)}</ul>:null}{detail.match.components.limitation&&<p className="muted">{detail.match.components.limitation}</p>}</>:<p>Select a profile and refresh matches.</p>}
           {detail.attributes.certifications?.length?<section className="panel" aria-label="Certification mentions"><h3>Certification mentions</h3><p className="muted">Strength reflects wording in this job description. UNKNOWN means the description does not state whether a certification is required or preferred.</p><ul aria-label="Certification requirement strength">{detail.attributes.certifications.map((item,index)=><li key={item.id+index}><strong>{item.name}</strong> — {item.strength.replaceAll('_',' ')}</li>)}</ul></section>:null}
           {detail.attributes.salary&&<p>Published range: {detail.attributes.salary.map(s=>`${s.currency} ${s.min}–${s.max} (period ${s.period.toLowerCase()})`).join('; ')}</p>}
          <label className="consent"><input type="checkbox" checked={tailorResume} disabled={!!busy||!profileId} onChange={e=>{setTailorResume(e.target.checked);setResumeSelection(null);}}/> Order approved skills for this job</label><p className="muted">Skills are ordered within the chosen resume facts. Job-matched skills and technology facts appear in bold in DOCX and PDF. Approved profile facts stay unchanged; any reviewed bullet wording is saved only in this application. Review the generated document before use.</p>
           <label className="consent"><input type="checkbox" checked={includeResumeContact} disabled={!!busy||!profileId} onChange={e=>setIncludeResumeContact(e.target.checked)}/> Include saved resume contact details</label><p className="muted">Include saved phones, city/region/country and LinkedIn, GitHub and portfolio links in this resume.</p>
           {tailorResume&&<ResumeFactSelection key={profileId+':'+detail.id} profileId={profileId} jobId={detail.id} disabled={!!busy} onChange={setResumeSelection}/>}
           <div className="two-col"><label>Cover letter length<select aria-label="Cover letter length" value={coverLetterLength} disabled={!!busy||!profileId} onChange={e=>setCoverLetterLength(e.target.value as typeof coverLetterLength)}><option value="VERY_SHORT">Very Short</option><option value="CONCISE">Concise</option><option value="NORMAL">Normal</option><option value="DETAILED">Detailed</option></select></label><label>Cover letter style<select aria-label="Cover letter style" value={coverLetterStyle} disabled={!!busy||!profileId} onChange={e=>setCoverLetterStyle(e.target.value as typeof coverLetterStyle)}>{['professional','warm','technical','direct','conversational','formal'].map(style=><option key={style} value={style}>{style[0].toUpperCase()+style.slice(1)}</option>)}</select></label></div>
           <p className="muted">The letter uses exact job details and only relevant approved facts. Style and length change the deterministic template; no AI text is added. Review the validated preview in Applications before using it. {detail.attributes.company_identity==='USER_ENTERED_UNVERIFIED'?'Employer name was entered manually and is unverified; check it against the saved source.':''}</p>
          <SourceValueEvidence resolution={detail.attributes.source_resolution} onPreparePreserved={!busy&&!!profileId&&detail.attributes.source_resolution?.route_review_required&&detail.attributes.source_resolution.preserved_route_id?()=>void action('Preparing with preserved route',()=>prepareJobRoute(detail,'THIS_ROUTE',detail.attributes.source_resolution?.preserved_route_id||undefined)):undefined}/>
          <button className="primary" disabled={!!busy||!profileId||!!detail.attributes.source_resolution?.route_review_required||(detail.attributes.source_type==='MANUAL_USER_ENTRY'&&!detail.postings?.some(p=>p.source==='manual'&&p.raw?.submitted_fields?.application_url))} onClick={()=>void action('Preparing factual documents',()=>prepareJobRoute(detail))}>Prepare documents</button>
          {detail.attributes.source_type==='MANUAL_USER_ENTRY'&&!detail.postings?.some(p=>p.source==='manual'&&p.raw?.submitted_fields?.application_url)&&<p className="muted">Add an application URL before preparing documents. A job reference alone is not an application route.</p>}
          {!detail.attributes.fictional&&detail.application_url&&(!detail.attributes.source_type||detail.postings?.some(p=>p.source!=='manual'||Boolean(p.raw?.submitted_fields?.application_url)))&&<a className="button-link" href={detail.application_url} target="_blank" rel="noreferrer">Open employer application ↗</a>}
          <h3>Job description</h3><pre className="description" style={{whiteSpace:'pre-wrap',fontFamily:'inherit'}}>{detail.description||'No description supplied.'}</pre><h3>Source evidence</h3>{detail.postings?.map(p=><div className="provenance" key={p.source+p.source_job_id}><code>{p.source}</code><p>{p.source==='manual'?'Captured':'Fetched'} {stamp(p.fetched_at)}</p>{p.source_url&&(p.source!=='manual'||p.raw?.submitted_fields?.reference_url)&&<p>Job reference: <a href={p.source_url} target="_blank" rel="noreferrer">Open saved reference ↗</a></p>}{p.application_url&&(p.source!=='manual'||p.raw?.submitted_fields?.application_url)&&<p>Application route: <a href={p.application_url} target="_blank" rel="noreferrer">Open saved application link ↗</a></p>}{p.raw?.submitted_fields?.source_details&&<details><summary>Other user-entered source fields</summary><pre>{JSON.stringify(p.raw.submitted_fields.source_details,null,2)}</pre></details>}{p.snapshots.map(s=><small key={s.sha256}>SHA256 {s.sha256}</small>)}</div>)}</>:<div className="empty"><h2>A closer look.</h2><p>Select an opportunity to see its description, source evidence, and match explanation.</p></div>}</section></div>
      </>}
  {tab==='Profile'&&<form onSubmit={e=>{e.preventDefault();if(profileId&&editor.id!==profileId)return;void action('Saving profile',async()=>{const clean={id:editor.id,name:editor.name,email:editor.email,...(editor.version?{expected_version:editor.version}:{}),contact:{...editor.contact,phones:cleanLines(editor.contact?.phones||[])},preferences:{...editor.preferences,desired_roles:cleanLines(editor.preferences?.desired_roles||[]),avoid_terms:cleanLines(editor.preferences?.avoid_terms||[])},facts:editor.facts.filter(f=>f.text.trim()).map(({id,kind,text,source,confidence,verified,user_approved})=>({id,kind,text,source,confidence,verified,user_approved})),answers:editor.answers.filter(a=>a.text.trim()).map(({id,question,text,source,confidence,verified,user_approved})=>({id,...(question?.trim()?{question:question.trim()}:{}),text,source,confidence,verified,user_approved}))};const shouldRescore=needsMatchRefresh(clean);await api('/profiles',{method:'PUT',body:JSON.stringify(clean)});const saved=await api<Profile>('/profiles/'+encodeURIComponent(editor.id));const normalized={...saved,contact:saved.contact||{},preferences:saved.preferences||{},facts:(saved.facts||[]).map(f=>({...f,id:f.fact_id||f.id}))};setEditor(normalized);profileBaselineRef.current=normalized;setProfileId(editor.id);if(shouldRescore&&clean.facts.some(eligible)){try{await queue('/matches',{profile_id:editor.id,request_id:crypto.randomUUID()});}catch{setNotice('Profile saved. Reusable answers saved; match refresh is unavailable. Your saved data is intact.');return;}}setNotice('Profile saved. Reusable answers saved. Substantive edits require fresh approval.');});}}>
        {!!profileId&&editor.id!==profileId&&<p role="status">Loading the selected profile before edits can be made.</p>}
        <fieldset className="profile-editor-gate" disabled={!!profileId&&editor.id!==profileId}>
        <section className="panel"><h2>Contact details</h2><p className="muted">Name and email are required to save. Other fields are optional. Resume contact details are included only when you select that option.</p><div className="two-col"><label>Full name<input required maxLength={200} value={editor.name} onChange={e=>setEditor({...editor,name:e.target.value})}/></label><label>Email<input type="email" required maxLength={320} value={editor.email} onChange={e=>setEditor({...editor,email:e.target.value})}/></label></div><div className="two-col">{(['first_name','middle_name','last_name','preferred_name'] as const).map(key=><label key={key}>{key.replaceAll('_',' ')}<input value={editor.contact?.[key]||''} onChange={e=>setContact(key,e.target.value)}/></label>)}</div><label>Phone numbers (one per line)<textarea value={(editor.contact?.phones||[]).join('\n')} onChange={e=>setEditor({...editor,contact:{...editor.contact,phones:e.target.value.split('\n')}})}/></label><div className="two-col">{(['address_line1','address_line2','city','state','postal_code','country'] as const).map(key=><label key={key}>{key.replaceAll('_',' ')}<input value={editor.contact?.[key]||''} onChange={e=>setContact(key,e.target.value)}/></label>)}</div><div className="two-col">{(['linkedin','github','portfolio'] as const).map(key=><label key={key}>{key}<input type="url" value={editor.contact?.[key]||''} onChange={e=>setContact(key,e.target.value)}/></label>)}</div></section>
        <section className="panel"><h2>Preferences</h2><p className="muted">Stored preferences are ready for future matching controls; they are not automatic search filters yet.</p><label>Desired roles (one per line)<textarea value={(editor.preferences?.desired_roles||[]).join('\n')} onChange={e=>setPreference('desired_roles',e.target.value.split('\n'))}/></label><label>Avoid terms in generated career content (one per line)<textarea maxLength={6149} value={(editor.preferences?.avoid_terms||[]).join('\n')} onChange={e=>setPreference('avoid_terms',e.target.value.split('\n'))}/><span className="muted">Whole terms are blocked in resumes, cover letters, and reviewed answer exports. Maximum 50 terms, 120 characters each.</span></label><div className="two-col"><label>Employment types<select multiple value={editor.preferences?.employment_types||[]} onChange={e=>setPreference('employment_types',Array.from(e.target.selectedOptions,e=>e.value))}>{['FULL_TIME','PART_TIME','CONTRACT','CONSULTING','PER_DIEM','TEMPORARY','INTERNSHIP'].map(v=><option key={v}>{v}</option>)}</select></label><label>Work arrangements<select multiple value={editor.preferences?.work_arrangements||[]} onChange={e=>setPreference('work_arrangements',Array.from(e.target.selectedOptions,e=>e.value))}>{['REMOTE','HYBRID','ON_SITE'].map(v=><option key={v}>{v}</option>)}</select></label></div><div className="two-col"><label>Shifts<select multiple value={editor.preferences?.shifts||[]} onChange={e=>setPreference('shifts',Array.from(e.target.selectedOptions,e=>e.value))}>{['DAY','EVENING','NIGHT','FLEXIBLE'].map(v=><option key={v}>{v}</option>)}</select></label><label>Travel preference<input value={editor.preferences?.travel_preference||''} onChange={e=>setPreference('travel_preference',e.target.value)}/></label></div><div className="two-col"><label>Salary expectations<input value={editor.preferences?.salary_expectations||''} onChange={e=>setPreference('salary_expectations',e.target.value)}/></label><label>Career goals<textarea value={editor.preferences?.career_goals||''} onChange={e=>setPreference('career_goals',e.target.value)}/></label></div><div className="two-col"><label>Maximum commute miles<input type="number" min="0" value={editor.preferences?.max_commute_miles??''} onChange={e=>setPreference('max_commute_miles',e.target.value===''?null:Number(e.target.value))}/></label><label>Maximum commute minutes<input type="number" min="0" value={editor.preferences?.max_commute_minutes??''} onChange={e=>setPreference('max_commute_minutes',e.target.value===''?null:Number(e.target.value))}/></label></div></section>
        <section className="panel"><h2>Work authorization</h2><p>Optional, in your own words. These details do not approve application answers or authorize submission.</p><div className="two-col"><label>Work authorization details<input maxLength={300} value={editor.preferences?.work_authorization||''} onChange={e=>setPreference('work_authorization',e.target.value)}/></label><label>Sponsorship needs<select value={editor.preferences?.sponsorship_needs||'NOT_SPECIFIED'} onChange={e=>setPreference('sponsorship_needs',e.target.value)}><option value="NOT_SPECIFIED">Not specified</option><option value="REQUIRED">Sponsorship required</option><option value="NOT_REQUIRED">Sponsorship not required</option></select></label></div></section>
        <section className="panel"><div className="toolbar"><div><h2>Career facts</h2><p>New or edited facts stay pending until you verify and approve them.</p></div><button type="button" onClick={()=>setEditor({...editor,facts:[...editor.facts,newFact()]})}>Add fact</button></div>{editor.facts.map((fact,i)=><fieldset className="fact" key={fact.id}><legend>Fact {i+1} <span className="tag">{eligible(fact)?'Eligible':'Pending'}</span></legend><div className="two-col"><label>Type<select value={fact.kind} onChange={e=>editFact(i,{kind:e.target.value})}>{['skill','summary','experience','responsibility','education','certification','project','accomplishment','technology_experience'].map(k=><option key={k}>{k}</option>)}</select></label><label>Confidence (0–100)<input type="number" min={0} max={100} required value={fact.confidence} onChange={e=>editFact(i,{confidence:Number(e.target.value)})}/></label></div><label>Fact text<textarea maxLength={10000} value={fact.text} onChange={e=>editFact(i,{text:e.target.value})}/></label><label>Evidence / source<input maxLength={1000} value={fact.source} onChange={e=>editFact(i,{source:e.target.value})}/></label><div className="approvals"><label><input type="checkbox" checked={fact.verified} onChange={e=>editFact(i,{verified:e.target.checked})}/> I verified this fact</label><label><input type="checkbox" checked={fact.user_approved} onChange={e=>editFact(i,{user_approved:e.target.checked})}/> I approve its use in documents</label><button type="button" className="quiet" onClick={()=>setEditor({...editor,facts:editor.facts.filter((_,index)=>index!==i)})}>Remove</button></div></fieldset>)}<p className="muted">Only sourced, verified, approved facts with confidence of at least 80 are included.</p><button className="primary" disabled={!!busy}>Save profile</button></section>
        <section className="panel"><div className="toolbar"><div><h2>Reusable application answers</h2><p>Answers are suggested only for an exact question match. Any substantive edit resets approval.</p></div><button type="button" onClick={()=>setEditor({...editor,answers:[...editor.answers,newAnswer()]})}>Add answer</button></div>{editor.answers.map((answer,i)=><fieldset className="fact" key={answer.id}><legend>Answer {i+1} <span className="tag">{answer.verified&&answer.user_approved&&answer.confidence>=80&&answer.question?'Approved':'Pending'}</span></legend><label>Question this answer addresses<input maxLength={2000} value={answer.question||''} onChange={e=>editAnswer(i,{question:e.target.value})} placeholder="Exact employer question"/></label><label>Answer text<textarea maxLength={10000} value={answer.text} onChange={e=>editAnswer(i,{text:e.target.value})}/></label><div className="two-col"><label>Provenance<input maxLength={1000} value={answer.source} onChange={e=>editAnswer(i,{source:e.target.value})}/></label><label>Confidence (0–100)<input type="number" min={0} max={100} value={answer.confidence} onChange={e=>editAnswer(i,{confidence:Number(e.target.value)})}/></label></div><div className="approvals"><label><input type="checkbox" checked={answer.verified} onChange={e=>editAnswer(i,{verified:e.target.checked})}/> I reviewed and verified this answer</label><label><input type="checkbox" checked={answer.user_approved} onChange={e=>editAnswer(i,{user_approved:e.target.checked})}/> I approve exact reuse for this question</label><button type="button" className="quiet" onClick={()=>setEditor({...editor,answers:editor.answers.filter((_,index)=>index!==i)})}>Remove</button></div>{!answer.question&&<p className="muted">Legacy answer: retained for its original fictional test path, and unavailable for question matching until you provide its source question.</p>}</fieldset>)}<button className="primary" disabled={!!busy}>Save profile and answers</button></section>
        <ResumeLibrary key={editor.id} profileId={editor.id} saved={profileId===editor.id&&profiles.some(p=>p.id===editor.id)} resumes={editor.id===profileId?resumes:[]} busy={!!busy} action={action} onStored={rows=>{setResumes(rows);setNotice('Resume stored locally. Use Extract and review to populate your profile.');}} onApply={addExtraction}/>
        <DocumentLibrary key={'library-'+editor.id} profileId={editor.id} saved={profileId===editor.id&&profiles.some(p=>p.id===editor.id)} baseResumes={resumes} busy={!!busy} action={action} onBaseStored={async()=>{const rows=await api<{resumes:Resume[]}>('/profiles/'+encodeURIComponent(editor.id)+'/resumes');if(editor.id===profileRef.current)setResumes(rows.resumes);}}/>
        {operationMode==='FIXTURE'&&<section className="panel"><h2>Try the local demo</h2><p>Creates a separate fictional profile and two fictional jobs. It never overwrites your profile.</p><button type="button" disabled={!!busy} onClick={()=>void action('Loading fictional demo',demo)}>Load fictional demo</button></section>}
        </fieldset>
      </form>}
      {tab==='Sources'&&<>
        <CompanySources rows={companySources} busy={!!busy} profileSelected={!!profileId}
          onMonitor={(row,interval)=>action('Saving source monitoring',async()=>{
            await api('/company-sources/'+encodeURIComponent(row.id)+'/monitoring',{method:'PUT',body:JSON.stringify({source_version:row.source_version,revision:row.monitoring?.revision||0,poll_interval:interval})});
            setNotice('Monitoring saved. Automatic checks use enabled search profiles covering all sources.');
          })}
          onRun={row=>action('Refreshing employer source',async()=>{
            try {await queue('/company-sources/'+encodeURIComponent(row.id)+'/refresh',{profile_id:profileRef.current});}
            finally {await refresh(profileRef.current);}
          })}
        onSet={async(row,control,value)=>{
          await action('Updating company source',async()=>{
            const field=control==='pin'?'pinned':control==='watch'?'watched':'enabled';
            await api('/company-sources/'+encodeURIComponent(row.id)+'/'+control,{method:'PUT',body:JSON.stringify({[field]:value})});
            setNotice('Company source preference saved.');
          });
        }}/>
        <section className="panel" aria-label="Discovered source candidates"><div className="toolbar"><div><h2>Discovered source candidates</h2><p>Review sites recorded from your captures. Recording a site does not inspect it, enable collection, or confirm it is a supported job source.</p></div><span className="tag">{sourceCandidates.length} saved</span></div>
          <form aria-label="Report a source site" onSubmit={event=>{event.preventDefault();void reportSourceCandidate();}}>
            <p className="muted">Know the site but not the job title or employer? Record the URL and optional reported origin; MJobs will not open it.</p>
            <div className="two-col"><label>Source site URL<input aria-label="Source site URL" type="url" maxLength={2000} required disabled={!!busy||!profileId} value={sourceReportUrl} onChange={event=>setSourceReportUrl(event.target.value)}/></label><label>Reported discovery origin<input aria-label="Reported discovery origin for source site" maxLength={253} disabled={!!busy||!profileId} value={sourceReportOrigin} onChange={event=>setSourceReportOrigin(event.target.value)} placeholder="For example, LinkedIn"/></label></div>
            <button className="primary" disabled={!!busy||!profileId||!sourceReportUrl.trim()}>Record source URL</button>
          </form>
          {!profileId?<p className="muted">Select a profile to view its source candidates.</p>:!sourceCandidates.length?<p>No third-party source candidates are recorded for this profile.</p>:sourceCandidates.map(candidate=>{
            const latestReview=candidate.review_history[candidate.review_history.length-1];
            const evaluation=sourceCandidateEvaluationDraft[candidate.id]||{
              api_feed_evidence_status:latestReview?.api_feed_evidence_status||'UNKNOWN',
              api_feed_evidence_reference:latestReview?.api_feed_evidence_reference||'',
              api_feed_evidence_note:latestReview?.api_feed_evidence_note||'',
              access_terms_note:latestReview?.access_terms_note||'',
              capabilities_limits_note:latestReview?.capabilities_limits_note||'',
              expected_value_maintenance_note:latestReview?.expected_value_maintenance_note||'',
            };
            const completeEvaluation=[evaluation.api_feed_evidence_note,evaluation.access_terms_note,evaluation.capabilities_limits_note,evaluation.expected_value_maintenance_note].every(value=>value.trim())&&(evaluation.api_feed_evidence_status!=='PUBLIC_DOCUMENTED'||!!evaluation.api_feed_evidence_reference.trim());
            const canSave=!!sourceCandidateReviewDraft[candidate.id]?.trim()&&completeEvaluation;
            return <article className="fact" key={candidate.id} aria-label={`Source candidate ${candidate.domain}`}>
              <div className="toolbar"><div><h3>{candidate.display_name}</h3><p><code>{candidate.domain}</code> · {candidate.site_type.replaceAll('_',' ')} · first seen {stamp(candidate.first_seen_at)} · last seen {stamp(candidate.last_seen_at)}</p></div><span className="tag">{candidate.state.replaceAll('_',' ')}</span></div>
              <p className="muted">Collection {candidate.collection_enabled?'enabled':'disabled'} · reviewed suitability does not authorize collection.</p>
              {candidate.observations.map(observation=><div className="provenance" key={observation.id}><p>{observation.capture_method.replaceAll('_',' ')} · {observation.evidence_status.replaceAll('_',' ')} · {stamp(observation.observed_at)}</p><p>Discovery origin: {observation.discovery_origin||'Unknown'} ({observation.origin_attribution.replaceAll('_',' ').toLowerCase()})</p><p>Source URL: <code>{observation.source_url||'Unknown'}</code></p><p>Application URL: <code>{observation.application_url||'Unknown'}</code></p><p>Observed hops: {observation.observed_hops.map(hop=>typeof hop==='string'?hop:hop.state).join(' → ')||'Unknown'}</p><p>Linked job: {observation.job_id||'None'} · application: {observation.application_id||'None'}</p></div>)}
              {!!latestReview&&<div className="provenance" aria-label={`Latest source evaluation for ${candidate.domain}`}>
                <h4>Latest source evaluation · {latestReview.site_type.replaceAll('_',' ')}</h4>
                <p>API/feed evidence: {latestReview.api_feed_evidence_status.replaceAll('_',' ')}{latestReview.api_feed_evidence_reference&&<> · <code>{latestReview.api_feed_evidence_reference}</code></>}</p>
                <p>{latestReview.api_feed_evidence_note}</p><p>Access and terms: {latestReview.access_terms_note}</p>
                <p>Capabilities and limits: {latestReview.capabilities_limits_note}</p>
                <p>Expected value and maintenance: {latestReview.expected_value_maintenance_note}</p>
              </div>}
              <label>Reviewed source kind<select aria-label={`Site type assessment for ${candidate.domain}`} value={sourceCandidateSiteTypeDraft[candidate.id]||candidate.site_type} disabled={!!busy} onChange={event=>setSourceCandidateSiteTypeDraft(current=>({...current,[candidate.id]:event.target.value as SourceCandidate['site_type']}))}><option value="UNKNOWN">Unknown</option><option value="JOB_BOARD">Job board or aggregator</option><option value="EMPLOYER">Employer source</option><option value="ATS">ATS or application destination</option></select></label>
              <label>Review outcome reason<textarea aria-label={`Review reason for ${candidate.domain}`} maxLength={1000} value={sourceCandidateReviewDraft[candidate.id]||candidate.review_reason||''} disabled={!!busy} onChange={event=>setSourceCandidateReviewDraft(current=>({...current,[candidate.id]:event.target.value}))}/></label>
              <label>Public API or feed evidence status<select aria-label={`API or feed evidence status for ${candidate.domain}`} value={evaluation.api_feed_evidence_status} disabled={!!busy} onChange={event=>updateSourceCandidateEvaluation(candidate,'api_feed_evidence_status',event.target.value)}><option value="UNKNOWN">Unknown / not verified</option><option value="PUBLIC_DOCUMENTED">Public documented evidence</option><option value="BLOCKED">Blocked or access denied</option></select></label>
              <label>Public documentation or feed reference<input aria-label={`API or feed evidence reference for ${candidate.domain}`} type="url" maxLength={2000} value={evaluation.api_feed_evidence_reference} disabled={!!busy} onChange={event=>updateSourceCandidateEvaluation(candidate,'api_feed_evidence_reference',event.target.value)}/></label>
              <label>Evidence note<textarea aria-label={`API or feed evidence note for ${candidate.domain}`} maxLength={1000} value={evaluation.api_feed_evidence_note} disabled={!!busy} onChange={event=>updateSourceCandidateEvaluation(candidate,'api_feed_evidence_note',event.target.value)}/></label>
              <label>Access and terms note<textarea aria-label={`Access and terms note for ${candidate.domain}`} maxLength={1000} value={evaluation.access_terms_note} disabled={!!busy} onChange={event=>updateSourceCandidateEvaluation(candidate,'access_terms_note',event.target.value)}/></label>
              <label>Capabilities and limits note<textarea aria-label={`Capabilities and limits note for ${candidate.domain}`} maxLength={1000} value={evaluation.capabilities_limits_note} disabled={!!busy} onChange={event=>updateSourceCandidateEvaluation(candidate,'capabilities_limits_note',event.target.value)}/></label>
              <label>Expected value and maintenance note<textarea aria-label={`Expected value and maintenance note for ${candidate.domain}`} maxLength={1000} value={evaluation.expected_value_maintenance_note} disabled={!!busy} onChange={event=>updateSourceCandidateEvaluation(candidate,'expected_value_maintenance_note',event.target.value)}/></label>
              <div className="toolbar"><button type="button" disabled={!!busy||!canSave} onClick={()=>void reviewSourceCandidate(candidate,'NEEDS_REVIEW')}>Keep needs review</button><button type="button" disabled={!!busy||!canSave} onClick={()=>void reviewSourceCandidate(candidate,'INTEGRATION_QUEUED')}>Queue integration review</button><button type="button" disabled={!!busy||!canSave} onClick={()=>void reviewSourceCandidate(candidate,'BLOCKED')}>Mark blocked</button><button type="button" className="quiet" disabled={!!busy||!canSave} onClick={()=>void reviewSourceCandidate(candidate,'REJECTED')}>Reject candidate</button></div>
              {!!candidate.review_history.length&&<details><summary>Review history ({candidate.review_history.length})</summary><ol>{candidate.review_history.map((review,index)=><li key={`${review.reviewed_at}-${index}`}><time>{stamp(review.reviewed_at)}</time> · {review.state.replaceAll('_',' ')} · {review.site_type.replaceAll('_',' ')} · {review.api_feed_evidence_status.replaceAll('_',' ')} · {review.reason}. {review.api_feed_evidence_note} Access/terms: {review.access_terms_note} Capabilities/limits: {review.capabilities_limits_note} Value/maintenance: {review.expected_value_maintenance_note}</li>)}</ol></details>}
            </article>;
          })}
          <p className="safety">Review status is not collection authorization. No source is enabled by capture or review.</p>
        </section>
        {operationMode==='PERSONAL'&&<CompanyDiscoveryRequests rows={discoveryRequests} busy={!!busy}
          onReview={(row,website)=>action('Reviewing company website',async()=>{
            try {await queue('/company-discovery/requests/'+encodeURIComponent(row.id)+'/review',{website:website.trim(),revision:row.revision});}
            finally {await refresh(profileRef.current);}
          })}
          onDismiss={row=>action('Dismissing company request',async()=>{
            await api('/company-discovery/requests/'+encodeURIComponent(row.id)+'/dismiss',{method:'POST',body:JSON.stringify({revision:row.revision})});
            setNotice('Company discovery request dismissed.');
          })}/>}
        {operationMode==='PERSONAL'&&<CompanyDiscovery busy={!!busy} onDiscover={(companyName,companyWebsite)=>action('Discovering company jobs',()=>discoverCompany(companyName,companyWebsite))}/>}
        <section className="panel">
          <h2>Public Greenhouse board</h2>
          <p>Reads public job listings from the provider API. Source concurrency limits parallel reads of board details and the job inventory (two independent requests); request starts still follow the configured rate limit. Each board keeps its own bounded schedule and backoff. No login or application submission is performed.</p>
          <form onSubmit={e=>{e.preventDefault();void action('Saving source',async()=>{
            await api('/sources',{method:'PUT',body:JSON.stringify({tenant,enabled:true,poll_interval:sourceIntervalMinutes*60,concurrency:sourceConcurrency,request_timeout:sourceTimeout,rate_limit:sourceRate,jitter_seconds:sourceJitter})});
            setTenant('');setNotice('Source saved. Create a search profile below to enable scheduled discovery.');
          });}}>
            <div className="source-add">
              <label>Board token<input required pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,79}" value={tenant} onChange={e=>setTenant(e.target.value)} placeholder="e.g. greenhouse"/></label>
              <label>Interval (minutes)<input type="number" min={5} max={10080} required value={sourceIntervalMinutes} onChange={e=>setSourceIntervalMinutes(Number(e.target.value))}/></label>
              <button disabled={!!busy}>Add board</button>
            </div>
            <div className="two-col">
              <label>Request timeout (seconds)<input type="number" min={2} max={60} value={sourceTimeout} onChange={e=>setSourceTimeout(Number(e.target.value))}/></label>
              <label>Rate limit (requests/minute)<input type="number" min={1} max={600} value={sourceRate} onChange={e=>setSourceRate(Number(e.target.value))}/></label>
              <label>Source concurrency<input type="number" min={1} max={8} value={sourceConcurrency} onChange={e=>setSourceConcurrency(Number(e.target.value))}/></label>
              <label>Schedule jitter (seconds)<input type="number" min={0} max={300} value={sourceJitter} onChange={e=>setSourceJitter(Number(e.target.value))}/></label>
            </div>
          </form>
        </section>
        {sources.map(source=><section className="panel" key={source.id}>
          <div className="toolbar"><div><h2>{source.name}</h2><p>{source.provider} / {source.tenant} · {source.jobs_seen} jobs collected</p></div><span className="tag">{source.enabled?source.health_status:'DISABLED'}</span></div>
          <p>{source.strategy} · every {source.poll_interval/60} minutes · timeout {source.request_timeout}s · rate {source.rate_limit}/min · concurrency {source.concurrency} · jitter ≤ {source.jitter_seconds}s</p>
          <p className="muted">Capabilities: {source.capabilities?.capabilities?.join(', ')||'Not declared'}</p>
          <p>Last success: {stamp(source.last_success)}</p>
          {source.backoff_until&&<p className="alert">Backoff until {stamp(source.backoff_until)}: {source.last_error}</p>}
          <button disabled={!!busy||!profileId||!source.enabled} onClick={()=>void action('Collecting public jobs',async()=>{await queue('/sources/'+source.id+'/collect',request());setTab('Jobs');})}>Collect now</button>{' '}
          <button className="quiet" disabled={!!busy} onClick={()=>void action('Updating source',async()=>{await api('/sources',{method:'PUT',body:JSON.stringify({tenant:source.tenant,enabled:!source.enabled,poll_interval:source.poll_interval,concurrency:source.concurrency,request_timeout:source.request_timeout,rate_limit:source.rate_limit,jitter_seconds:source.jitter_seconds})});})}>{source.enabled?'Disable':'Enable'}</button>
        </section>)}
        <section className="panel">
          <div className="toolbar"><div><h2>Scheduled search profiles</h2><p>Profiles are data-driven. The scheduler evaluates each enabled source independently and queues durable discovery work when it is due.</p></div><span className="tag">{searchProfiles.length} saved</span></div>
          {!profileId?<p className="muted">Select or create a career profile before adding a scheduled search.</p>:<form onSubmit={e=>{e.preventDefault();void action('Saving search profile',async()=>{
            const titles=searchTitles.split(/[\n,]+/).map(value=>value.trim()).filter(Boolean);
            await api('/search-profiles',{method:'PUT',body:JSON.stringify({id:crypto.randomUUID(),profile_id:profileId,name:searchName,enabled:true,include_titles:titles,remote:searchRemote,employment_types:searchEmployment?[searchEmployment]:[],source_ids:searchSource?[searchSource]:[]})});
            setSearchName('');setSearchTitles('');setNotice('Search profile saved. Due enabled sources will now be scheduled automatically.');
          });}}>
            <div className="two-col">
              <label>Search name<input required maxLength={200} value={searchName} onChange={e=>setSearchName(e.target.value)} placeholder="Remote Infrastructure"/></label>
              <label>Source<select value={searchSource} onChange={e=>setSearchSource(e.target.value)}><option value="">All enabled sources</option>{sources.map(source=><option key={source.id} value={source.id}>{source.name}</option>)}</select></label>
              <label>Remote preference<select value={searchRemote} onChange={e=>setSearchRemote(e.target.value as 'ANY'|'REQUIRED'|'EXCLUDED')}><option value="ANY">Any</option><option value="REQUIRED">Remote required</option><option value="EXCLUDED">Exclude remote</option></select></label>
              <label>Employment type<select value={searchEmployment} onChange={e=>setSearchEmployment(e.target.value)}><option value="">Any</option><option value="FULL_TIME">Full-time</option><option value="PART_TIME">Part-time</option><option value="CONTRACT">Contract</option><option value="CONTRACT_TO_HIRE">Contract-to-hire</option><option value="TEMPORARY">Temporary</option><option value="INTERNSHIP">Internship</option><option value="OTHER">Other</option></select></label>
            </div>
            <label>Include titles<textarea required value={searchTitles} onChange={e=>setSearchTitles(e.target.value)} placeholder={"System Administrator\nInfrastructure Engineer\nNOC Engineer"}/></label>
            <button disabled={!!busy||sources.length===0}>Save scheduled search</button>
          </form>}
          {searchProfiles.map(search=><div className="document" key={search.id}>
            <div className="toolbar"><div><strong>{search.name}</strong><p>{search.criteria.include_titles.join(', ')||'Any title'} · remote {search.criteria.remote.toLowerCase()} · {search.criteria.employment_types.join(', ')||'any employment type'}</p></div><span className="tag">{search.enabled?'ENABLED':'PAUSED'}</span></div>
            <p className="muted">{search.source_ids.length?(search.source_ids.length+' selected source(s)'):'All enabled sources'} · updated {stamp(search.updated_at)}</p>
            <button type="button" className="quiet" disabled={!!busy} onClick={()=>void action(search.enabled?'Pausing search':'Resuming search',async()=>{await api('/search-profiles',{method:'PUT',body:JSON.stringify({id:search.id,profile_id:search.profile_id,name:search.name,enabled:!search.enabled,include_titles:search.criteria.include_titles,remote:search.criteria.remote,employment_types:search.criteria.employment_types,source_ids:search.source_ids})});})}>{search.enabled?'Pause':'Resume'}</button>
          </div>)}
        </section>
        {operationMode==='FIXTURE'&&<section className="panel"><h2>Fictional local source</h2><p>Use the demo to test document preparation, dry-run, mock confirmation and tracking without contacting an employer.</p><button disabled={!!busy} onClick={()=>void action('Loading fictional demo',demo)}>Load fictional demo</button></section>}
      </>}
      {tab==='Applications'&&<><section className="panel" aria-label="Record external application"><h2>Log an application completed outside MJobs</h2><p>This records what you report. MJobs does not open the employer site, submit anything, or verify the employer received it.</p><label>Company<input aria-label="External application company" maxLength={200} value={externalCompany} disabled={!!busy} onChange={event=>{externalCaptureRequestId.current=crypto.randomUUID();setExternalCompany(event.target.value);}}/></label><label>Job title<input aria-label="External application title" maxLength={500} value={externalTitle} disabled={!!busy} onChange={event=>{externalCaptureRequestId.current=crypto.randomUUID();setExternalTitle(event.target.value);}}/></label><label>Application URL (needed when no matching job exists)<input aria-label="External application URL" type="url" maxLength={2000} value={externalUrl} disabled={!!busy} onChange={event=>{externalCaptureRequestId.current=crypto.randomUUID();setExternalUrl(event.target.value);}}/></label><div className="two-col"><label>Reported discovery origin<input aria-label="Reported discovery origin" maxLength={253} value={externalOrigin} disabled={!!busy} onChange={event=>{externalCaptureRequestId.current=crypto.randomUUID();setExternalOrigin(event.target.value);}} placeholder="For example, LinkedIn"/></label><label>Source URL, if known<input aria-label="External application source URL" type="url" maxLength={2000} value={externalSourceUrl} disabled={!!busy} onChange={event=>{externalCaptureRequestId.current=crypto.randomUUID();setExternalSourceUrl(event.target.value);}}/></label></div><label>Application source<select aria-label="External application source" value={externalSource} disabled={!!busy} onChange={event=>{externalCaptureRequestId.current=crypto.randomUUID();setExternalSource(event.target.value as 'MANUAL'|'REFERRAL'|'BROWSER'|'OTHER');}}><option value="MANUAL">Manual</option><option value="REFERRAL">Referral</option><option value="BROWSER">Browser</option><option value="OTHER">Other</option></select></label><label>When you applied<input aria-label="External application date and time" type="datetime-local" value={externalAppliedAt} disabled={!!busy} onChange={event=>{externalCaptureRequestId.current=crypto.randomUUID();setExternalAppliedAt(event.target.value);}}/></label><label>Job description, if available<textarea aria-label="External application job description" maxLength={50000} value={externalDescription} disabled={!!busy} onChange={event=>{externalCaptureRequestId.current=crypto.randomUUID();setExternalDescription(event.target.value);}}/></label><button type="button" className="primary" disabled={!!busy||!profileId||!externalCompany.trim()||!externalTitle.trim()||!externalAppliedAt} onClick={()=>void action('Recording external application',recordExternalApplication)}>Record external application</button></section><div className="application-layout"><section className="application-pipeline" aria-label="Application pipeline"><div className="toolbar"><div><h2>{apps.length} logged applications</h2><p>Stages below are manual reports. They do not verify activity on an employer’s site.</p></div></div><div className="pipeline-columns"><section className="pipeline-column" aria-label="Not tracked"><h3>Not tracked</h3>{apps.filter(application=>!application.pipeline_status).map(application=><button className="job-card" key={application.id} onClick={()=>void showApp(application.id).catch(e=>setError(String(e)))}><div className="card-top"><span className="eyebrow">{application.company}</span><span className="tag">{application.target==='MOCK'?'Fictional mock':application.external_reports?.length?'Externally reported':'Manual only'}</span></div><h2>{application.title}</h2><p>{application.state} · documents v{application.document_version}</p></button>)}</section>{applicationStages.map(stage=><section className="pipeline-column" key={stage} aria-label={applicationStageLabel(stage)}><h3>{applicationStageLabel(stage)}</h3>{apps.filter(application=>application.pipeline_status===stage).map(application=><button className="job-card" key={application.id} onClick={()=>void showApp(application.id).catch(e=>setError(String(e)))}><div className="card-top"><span className="eyebrow">{application.company}</span><span className="tag">{application.target==='MOCK'?'Fictional mock':application.external_reports?.length?'Externally reported':'Manual only'}</span></div><h2>{application.title}</h2><p>{application.state} · documents v{application.document_version}</p></button>)}</section>)}</div>{!apps.length&&<p className="empty">Record an external application or prepare documents for a job to start its history here.</p>}</section><section className="job-detail">{selectedApp?<><h2>Application details</h2><span className="tag">{selectedApp.state}</span>{(applicationTracking?.external_reports||[]).length>0&&<span className="tag">Externally reported</span>}<p>Document revision {selectedApp.document_version} · {selectedApp.fact_ids.length} approved facts</p><section className="panel application-tracking" aria-label="Application tracking"><h3>Manual application tracking</h3><p>Statuses and notes are your reports. “Applied” does not confirm that an application reached the employer. MJobs will not submit anything from this tracker.</p><label>Manual application status<select aria-label="Manual application status" value={applicationStatusDraft} disabled={!!busy} onChange={event=>setApplicationStatusDraft(event.target.value)}><option value="">Not tracked</option>{applicationStages.map(stage=><option key={stage} value={stage}>{applicationStageLabel(stage)}</option>)}</select></label><button type="button" disabled={!!busy||!applicationStatusDraft||applicationStatusDraft===applicationTracking?.status} onClick={()=>void updateApplicationStatus().catch(e=>setError(String(e)))}>Update status</button><label>Application note<textarea aria-label="Application note" maxLength={4000} value={applicationNoteDraft} disabled={!!busy} onChange={event=>{const value=event.target.value;setApplicationNoteDraft(value);if(value!==applicationNoteDraft)setApplicationNoteRequestId(crypto.randomUUID());}}/></label><button type="button" disabled={!!busy||!applicationNoteDraft.trim()} onClick={()=>void addApplicationNote().catch(e=>setError(String(e)))}>Add application note</button><h4>Application history</h4><ol aria-label="Application history">{applicationTracking?.history.map((event,index)=><li key={`${event.sequence}-${index}`}><time>{stamp(event.timestamp)}</time> · {event.type==='status'?`${applicationStageLabel(event.status)}${event.attribution==='EXTERNAL_USER_REPORTED'?' · external user report':''}`:event.type==='external_application'?`External application reported via ${event.source} · applied ${stamp(event.applied_at)}`:event.text}</li>)}</ol>{!applicationTracking?.history.length&&<p className="muted">No application tracking history yet.</p>}</section><h3>Documents</h3><DocumentHistory documents={docs}/>
        <section className="panel"><div className="toolbar"><div><h3>Application answers</h3><p>Exact matches are suggestions only. Select them deliberately, review every answer, then save a local export. Nothing is filled into or submitted to an employer.</p></div><button type="button" onClick={()=>setAnswerDrafts(rows=>[...rows,{question:'',text:'',reviewed:false}])}>Add question</button></div>{answerDrafts.map((draft,index)=>{const sensitive=sensitiveAnswerQuestion(draft.question);const candidate=!sensitive?(selectedApp.answer_bank||[]).find(answer=>answer.question&&normalizedQuestion(answer.question)===normalizedQuestion(draft.question)&&answer.verified&&answer.user_approved&&answer.confidence>=80):undefined;return <fieldset className="fact" key={index}><legend>Question {index+1}</legend><label>Employer question<textarea maxLength={2000} value={draft.question} onChange={e=>setAnswerDrafts(rows=>rows.map((row,i)=>i===index?{...row,question:e.target.value,answer_id:undefined,reviewed:false}:row))}/></label>{sensitive&&<p className="safety">Sensitive or legal question: enter your answer for this application. Reusable answers are not suggested.</p>}{candidate&&!draft.answer_id&&<div className="provenance"><p>Exact saved question match · {candidate.confidence}% confidence · {candidate.source}</p><button type="button" onClick={()=>setAnswerDrafts(rows=>rows.map((row,i)=>i===index?{...row,text:candidate.text,answer_id:candidate.id,reviewed:false}:row))}>Use saved answer (review required)</button></div>}<label>Answer<textarea maxLength={10000} value={draft.text} onChange={e=>setAnswerDrafts(rows=>rows.map((row,i)=>i===index?{...row,text:e.target.value,answer_id:undefined,reviewed:false}:row))}/></label><label className="consent"><input type="checkbox" checked={draft.reviewed} onChange={e=>setAnswerDrafts(rows=>rows.map((row,i)=>i===index?{...row,reviewed:e.target.checked}:row))}/> I reviewed this exact answer for this job</label><button type="button" className="quiet" onClick={()=>setAnswerDrafts(rows=>rows.filter((_,i)=>i!==index))}>Remove question</button></fieldset>})}{answerDrafts.length>0&&<button className="primary" disabled={!!busy||!answerDrafts.every(row=>row.question.trim()&&row.text.trim()&&row.reviewed)} onClick={()=>void action('Saving reviewed answer export',exportAnswers)}>Save reviewed answers and export TXT</button>}<p className="muted">Unreviewed answers, missing text, and saved answers for a different question are blocked. Earlier exports remain available in document history.</p></section>
        {selectedApp.target==='MOCK'?(operationMode==='FIXTURE'?<section className="mock-box"><h3>Controlled fictional test</h3><p>The dry-run fills and reviews the local mock form, then stops before submission.</p><button disabled={!!busy||selectedApp.state==='CONFIRMED'} onClick={()=>void action('Running mock dry-run',async()=>{await queue('/profiles/'+encodeURIComponent(profileId||'')+'/applications/'+selectedApp.id+'/mock-run',{submit:false});await showApp(selectedApp.id);})}>Run dry-run</button><label className="consent"><input type="checkbox" checked={mockConsent} onChange={e=>setMockConsent(e.target.checked)}/> I approve one fictional mock submission</label><button disabled={!!busy||!mockConsent||selectedApp.state==='CONFIRMED'} onClick={()=>void action('Submitting to local mock',async()=>{await queue('/profiles/'+encodeURIComponent(profileId||'')+'/applications/'+selectedApp.id+'/mock-run',{submit:true});await showApp(selectedApp.id);})}>Submit to local mock</button></section>:<p className="safety">Fictional application actions are disabled in personal mode.</p>):<p className="safety">Manual preparation only. Review the documents and use the employer’s own application page.</p>}
        <section className="panel" aria-label="ATS question preflight"><h3>ATS question preflight</h3>{selectedApp.question_preflight?<><p>{selectedApp.question_preflight.result} · checked {stamp(selectedApp.question_preflight.observed_at)}{selectedApp.question_preflight.reason?` · ${selectedApp.question_preflight.reason}`:''}</p>{selectedApp.question_preflight.missing.length>0&&<ul aria-label="Unresolved required questions">{selectedApp.question_preflight.missing.map(item=><li key={item.question_id}><strong>{item.question}</strong> ({item.question_id}) · missing {Object.entries({facts:item.fact_ids,answers:item.answer_ids,profile:item.profile_paths,documents:item.document_kinds,application:item.application_context}).filter(([,values])=>values?.length).map(([kind,values])=>`${kind}: ${values!.join(', ')}`).join('; ')}</li>)}</ul>}</>:<p>No structured question preflight has been run yet.</p>}<p className="muted">Only supported structured metadata is checked. UNKNOWN or missing required answers stop the browser flow for review.</p></section>
        <h3>Tracking evidence</h3><p className="description">{selectedApp.trace.join(' → ')}</p>{selectedApp.confirmation&&<pre>{JSON.stringify(selectedApp.confirmation,null,2)}</pre>}<h3>Attempts</h3>{selectedApp.attempts?.map((attempt,i)=><p key={i}>{stamp(attempt.started_at)} · {attempt.state} · {attempt.steps.join(' → ')}</p>)}</>:<div className="empty"><h2>Your paper trail.</h2><p>Select an application to download documents and inspect its status and attempts.</p></div>}</section></div></>}
      {tab==='Activity'&&<><section className="panel"><div className="toolbar"><h2>Service health</h2><button onClick={()=>{void readiness();void refresh().catch(e=>setError(String(e)));void loadDeadLetters().catch(e=>setError(String(e)));void loadAuditHistory().catch(e=>setError(String(e)));}}>Refresh activity</button></div>{ready&&Object.entries(ready.checks).map(([name,ok])=><p key={name}><span className={'status '+(ok?'good':'warn')}>{ok?'Ready':'Unavailable'}</span> {name}</p>)}<p className="muted">Queued work is durable. A browser interruption before submission requires review; uncertain submission outcomes require reconciliation.</p></section><section className="panel" aria-label="Audit history"><h2>Audit history</h2>{!profileId?<p>Select a profile to view its history.</p>:auditEvents.length?<ol aria-label="Audit history">{auditEvents.map(event=><li key={event.id}><time>{stamp(event.timestamp)}</time> · <strong>{event.summary}</strong>{event.subject&&event.subject!=='Profile'?` · ${event.subject}`:''}{event.company?` at ${event.company}`:''}<span className="muted"> · {event.type}</span></li>)}</ol>:<p>No user-facing audit history is available yet.</p>}<p className="muted">History summaries are profile-scoped. Employer receipt is never inferred from a local or mock event.</p></section><section className="panel" aria-label="Dead-letter queue"><div className="toolbar"><div><h2>Dead-letter queue</h2><p>{deadLetterCount} task{deadLetterCount===1?'':'s'} {deadLetterCount===1?'needs':'need'} operator review.</p></div></div>{!profileId?<p>Select a profile to view its dead-lettered tasks.</p>:deadLetters.map(op=><div className="operation" key={op.id}><div><strong>{op.kind}</strong><p>{op.error||'No failure reason recorded'} · {op.attempt_count} attempt(s)</p><p>Last attempt: {stamp(op.last_attempt_at)} · Dead-lettered: {stamp(op.dead_lettered_at)}</p>{op.retry_blocked_reason&&<p className="safety">Retry unavailable: {op.retry_blocked_reason}</p>}<code>{op.id.slice(0,16)}</code></div><div><button disabled={!!busy||op.retryable===false} onClick={()=>void action('Retrying failed task',async()=>{await api('/operations/'+op.id+'/retry',{method:'POST'});await loadDeadLetters();setNotice('Task queued for an explicit retry.');})}>Retry task</button>{' '}<button className="quiet" disabled={!!busy} onClick={()=>{if(window.confirm('Discard this task? Its failure and audit history will be retained.'))void action('Discarding failed task',async()=>{await api('/operations/'+op.id+'/discard',{method:'POST'});await loadDeadLetters();setNotice('Task discarded; its audit history was retained.');});}}>Discard</button></div></div>)}{profileId&&deadLetterCount===0&&<p>No tasks are dead-lettered.</p>}{deadLetterCount>deadLetters.length&&<p className="muted">Showing the 100 most recent tasks. The count includes all matching tasks.</p>}</section><section className="panel"><h2>Background operations</h2>{ops.map(op=><div className="operation" key={op.id}><div><strong>{op.kind}</strong><p>{op.state} · {op.attempt_count} attempt(s) · {stamp(op.updated_at)}</p>{op.error&&<p className="error-text">{op.error}</p>}<code>{op.id.slice(0,16)}</code></div>{!terminal.has(op.state)&&<button onClick={()=>void api('/operations/'+op.id+'/cancel',{method:'POST'}).then(()=>refresh()).catch(e=>setError(String(e)))}>{op.cancel_requested?'Cancellation requested':'Cancel'}</button>}</div>)}{!ops.length&&<p>No operations for this profile yet.</p>}</section></>}
      <footer>MJobs · Your data stays in this local workspace. User-testing milestone; broader integrations and automation are still in development.</footer>
    </main>
  </div>;
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
