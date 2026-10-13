"""Selected-search advisory batches use frozen scores without changing matches."""
import copy
import unittest
from sqlalchemy import select, update
from services.discovery.search_profiles import save_search_profile
from services.discovery.matching import recalculate
from packages.persistence import models as m
from tests.integration import test_durable_ai_evaluation as durable

class SelectedSearchAIHarness(unittest.TestCase):
    new_client = durable.DurableAIEvaluationTests.new_client
    run_worker = durable.DurableAIEvaluationTests.run_worker
    def setUp(self):
        durable.DurableAIEvaluationTests.setUp(self)
        response=self.client.post('/jobs/manual',json={
            'request_id':'selected-ai-second-job','title':'Cloud engineer','company':'Synthetic Company',
            'description':'Kubernetes infrastructure','reference_url':'https://jobs.example.test/cloud',
            'application_url':'https://apply.example.test/cloud','source_details':{}})
        self.assertEqual(response.status_code,201,response.text)
        with self.store.engine.begin() as connection:
            rows=list(connection.execute(select(m.jobs)).mappings())
            for row in rows:
                cloud=row['title']=='Cloud engineer'
                connection.execute(update(m.jobs).where(m.jobs.c.id==row['id']).values(
                    skills=['Kubernetes'] if cloud else ['Python'],
                    attributes={**row['attributes'],'specialized_signals':{'ai_intensity':{
                        'score':95 if cloud else 5,'provenance':{'source':'SYNTHETIC','evidence_ref':row['id']}}}}))
                if cloud:self.cloud=row['id']
                else:self.systems=row['id']
        recalculate(self.store,'fixture')
        with self.store.engine.begin() as connection:
            connection.execute(update(m.matches).where(m.matches.c.profile_id=='fixture').values(score=99))
        self.store.save_ai_cost_policy('fixture',{'minimum_score':0,'max_input_candidates':50,
            'max_ai_candidates':1,'max_document_candidates':0})
        for name,weights in [('systems',{'skills':100}),('cloud',{'skills':0,'ai_intensity':100})]:
            save_search_profile(self.store,{'id':name,'profile_id':'fixture','name':name+' focus',
                'enabled':False,'include_titles':[],'source_ids':[]})
            response=self.client.put('/profiles/fixture/search-profiles/'+name+'/role-scoring-profile',
                json={'expected_version':None,'profile':{'schema_version':1,'role_family':name,
                    'weights':weights,'thresholds':{}}})
            self.assertEqual(response.status_code,200,response.text)
    def preview(self,search):
        response=self.client.get('/profiles/fixture/ai-cost-plan',params={'search_profile_id':search})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()
    def admit(self,search,preview,request='selected-search-intent-0001'):
        return self.client.post('/profiles/fixture/ai-cost-plan/evaluate/queued',json={
            'request_id':request,'search_profile_id':search,'preview_fingerprint':preview['fingerprint']})

class SelectedSearchAITests(SelectedSearchAIHarness):
    def test_provider_outage_leaves_deterministic_search_usable_without_partial_results(self):
        from services.ai.providers import AIProviderError
        preview=self.preview('cloud')
        admitted=self.admit('cloud',preview)
        self.assertEqual(admitted.status_code,202,admitted.text)
        class UnavailableRouter(self.router):
            def generate_structured(self, **kwargs):
                raise AIProviderError('AI_PROVIDER_TIMEOUT')
        self.router=UnavailableRouter
        result=self.run_worker(admitted.json()['operation_id'])
        self.assertEqual(result['status'],'BLOCKED')
        self.assertEqual(result['error_category'],'AI_PROVIDER_TIMEOUT')
        self.assertEqual(self.preview('cloud')['candidates'],preview['candidates'])
        self.assertEqual(self.client.get('/profiles/fixture/ai-cost-plan/evaluations').json()['runs'],[])

    def test_selected_scope_requires_preview_but_legacy_request_remains_supported(self):
        response=self.client.post('/profiles/fixture/ai-cost-plan/evaluate/queued',json={
            'request_id':'missing-preview-intent-0001','search_profile_id':'systems'})
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(response.json()['detail']['code'],'AI_EVALUATION_PREVIEW_REQUIRED')
        with self.store.engine.connect() as c:
            self.assertEqual(list(c.execute(select(m.operations))),[])
        self.assertEqual(self.calls,[])
        legacy=self.client.post('/profiles/fixture/ai-cost-plan/evaluate/queued',json={
            'request_id':'legacy-profile-intent-0001'})
        self.assertEqual(legacy.status_code,202,legacy.text)

    def test_saved_skill_filter_is_applied_before_the_input_cap(self):
        with self.store.engine.begin() as c:
            row=c.execute(select(m.search_profiles).where(m.search_profiles.c.id=='cloud')).mappings().one()
            criteria=copy.deepcopy(row['criteria'])
            criteria['job_filters']={'q':'','remote_type':'','provider':'','sort':'score','skill':'PYTHON'}
            c.execute(update(m.search_profiles).where(m.search_profiles.c.id=='cloud').values(criteria=criteria))
        self.store.save_ai_cost_policy('fixture',{'minimum_score':0,'max_input_candidates':1,
            'max_ai_candidates':1,'max_document_candidates':0})
        preview=self.preview('cloud')
        self.assertEqual([row['job_id'] for row in preview['candidates']],[self.systems])
        self.assertEqual(preview['plan']['scanned_job_count'],1)

    def test_saved_filters_use_recomputed_score_and_dismissal_before_admission(self):
        from services.tracking.jobs import set_job_disposition
        with self.store.engine.begin() as c:
            row=c.execute(select(m.search_profiles).where(m.search_profiles.c.id=='cloud')).mappings().one()
            criteria=copy.deepcopy(row['criteria'])
            criteria['job_filters']={'q':'','remote_type':'','provider':'','sort':'score','min_match_score':'90'}
            c.execute(update(m.search_profiles).where(m.search_profiles.c.id=='cloud').values(criteria=criteria))
            # A stale shared score must not suppress the true selected-search winner.
            c.execute(update(m.matches).where(m.matches.c.job_id==self.cloud).values(score=0))
        preview=self.preview('cloud')
        self.assertEqual([row['job_id'] for row in preview['candidates']],[self.cloud])
        set_job_disposition(self.store,'fixture',self.cloud,'HIDDEN','selected-ai-hide-0001')
        self.assertEqual(self.preview('cloud')['candidates'],[])
        self.assertEqual(self.calls,[])

    def test_text_filters_blacklist_and_input_cap_are_enforced(self):
        from services.matching.company_preferences import set_company_preference
        with self.store.engine.begin() as c:
            row=c.execute(select(m.search_profiles).where(m.search_profiles.c.id=='cloud')).mappings().one()
            criteria=copy.deepcopy(row['criteria'])
            criteria['job_filters']={'q':'','title':'Infrastructure','remote_type':'','provider':'','sort':'score'}
            c.execute(update(m.search_profiles).where(m.search_profiles.c.id=='cloud').values(criteria=criteria))
        self.assertEqual([row['job_id'] for row in self.preview('cloud')['candidates']],[self.systems])
        company=self.store.job(self.systems)['company_id']
        set_company_preference(self.store,'fixture',company,'BLACKLISTED','selected-ai-blacklist-0001')
        self.assertEqual(self.preview('cloud')['candidates'],[])
        # The legacy profile-wide mode retains its old behavior and limits.
        self.store.save_ai_cost_policy('fixture',{'minimum_score':0,'max_input_candidates':1,
            'max_ai_candidates':1,'max_document_candidates':0})
        response=self.client.get('/profiles/fixture/ai-cost-plan')
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(response.json()['detail']['code'],'AI_FUNNEL_INPUT_LIMIT')

    def test_unknown_signal_remains_disclosed_and_invalid_scoring_fails_before_queue(self):
        with self.store.engine.begin() as c:
            c.execute(update(m.jobs).values(attributes={}))
        preview=self.preview('cloud')
        contribution=preview['candidates'][0]['score_evidence']['scoring_contributions'][0]
        self.assertIsNone(contribution['raw_score'])
        with self.store.engine.begin() as c:
            row=c.execute(select(m.search_profiles).where(m.search_profiles.c.id=='cloud')).mappings().one()
            criteria=copy.deepcopy(row['criteria']);criteria['role_scoring_profile']['weights']={'skills':-10}
            c.execute(update(m.search_profiles).where(m.search_profiles.c.id=='cloud').values(criteria=criteria))
        response=self.admit('cloud',preview)
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.calls,[])

    def test_same_request_cannot_be_reused_for_another_scope(self):
        first=self.admit('systems',self.preview('systems'))
        self.assertEqual(first.status_code,202,first.text)
        replay=self.admit('systems',self.preview('systems'))
        self.assertTrue(replay.json()['replayed'])
        conflict=self.admit('cloud',self.preview('cloud'))
        self.assertEqual(conflict.status_code,409,conflict.text)
        self.assertEqual(conflict.json()['detail']['code'],'AI_EVALUATION_IDENTITY_CONFLICT')
        foreign=self.client.post('/profiles/other/ai-cost-plan/evaluate/queued',json={
            'request_id':'foreign-scoped-intent-0001','search_profile_id':'systems'})
        self.assertEqual(foreign.status_code,404,foreign.text)
        with self.store.engine.connect() as c:
            params=c.execute(select(m.operations.c.params)).scalar_one()
        self.assertEqual(set(params),{'profile_id','request_id','snapshot_digest'})
        self.assertEqual(self.calls,[])

    def test_opposite_scopes_freeze_scores_history_and_never_rewrite_matches(self):
        before=self.store.jobs(profile_id='fixture')
        for search,expected in [('systems',self.systems),('cloud',self.cloud)]:
            preview=self.preview(search)
            self.assertEqual(preview['candidates'][0]['job_id'],expected)
            self.assertEqual(preview['search_scope']['search_profile_id'],search)
            self.assertEqual(len(self.calls), 0 if search == 'systems' else 1)
            admitted=self.admit(search,preview,'selected-search-intent-'+search)
            self.assertEqual(admitted.status_code,202,admitted.text)
            operation=admitted.json()['operation_id']
            self.assertEqual(self.run_worker(operation)['status'],'COMPLETED')
            result=self.client.get('/profiles/fixture/ai-cost-plan/operations/'+operation).json()['result']
            self.assertEqual(result['evaluations'][0]['job_id'],expected)
            self.assertEqual(result['search_scope'],preview['search_scope'])
            self.assertEqual(result['evaluations'][0]['score_evidence'],preview['candidates'][0]['score_evidence'])
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.store.jobs(profile_id='fixture'),before)
        history=self.new_client().get('/profiles/fixture/ai-cost-plan/evaluations').json()['runs']
        self.assertEqual({r['search_scope']['search_profile_id'] for r in history},{'systems','cloud'})
    def test_foreign_missing_and_stale_preview_make_no_operation_or_provider_call(self):
        preview=self.preview('cloud')
        with self.store.engine.begin() as c:
            c.execute(update(m.search_profiles).where(m.search_profiles.c.id=='cloud').values(name='Changed'))
        stale=self.admit('cloud',preview)
        self.assertEqual(stale.status_code,409,stale.text)
        self.assertEqual(stale.json()['detail']['code'],'AI_EVALUATION_PREVIEW_CHANGED')
        for search in ['absent','systems']:
            path='/profiles/other/ai-cost-plan' if search=='systems' else '/profiles/fixture/ai-cost-plan'
            response=self.client.get(path,params={'search_profile_id':search})
            self.assertEqual(response.status_code,404,response.text)
        with self.store.engine.connect() as c:
            self.assertEqual(list(c.execute(select(m.operations))),[])
        self.assertEqual(self.calls,[])
    def test_admitted_search_edit_keeps_original_snapshot_and_replay_does_not_call_twice(self):
        preview=self.preview('cloud'); response=self.admit('cloud',preview)
        self.assertEqual(response.status_code,202,response.text)
        operation=response.json()['operation_id']
        with self.store.engine.begin() as c:
            row=c.execute(select(m.search_profiles).where(m.search_profiles.c.id=='cloud')).mappings().one()
            criteria=copy.deepcopy(row['criteria']);criteria['role_scoring_profile']['weights']={'skills':100}
            c.execute(update(m.search_profiles).where(m.search_profiles.c.id=='cloud').values(name='Edited later',criteria=criteria))
        self.assertEqual(self.run_worker(operation)['status'],'COMPLETED')
        self.run_worker(operation)
        result=self.new_client().get('/profiles/fixture/ai-cost-plan/operations/'+operation).json()['result']
        self.assertEqual(result['search_scope'],preview['search_scope'])
        self.assertEqual(result['evaluations'][0]['job_id'],self.cloud)
        self.assertEqual(len(self.calls),1)
