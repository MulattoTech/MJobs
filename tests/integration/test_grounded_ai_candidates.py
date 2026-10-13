"""Current approved evidence, not cached match rows, admits paid advisory work."""
from sqlalchemy import delete, select, update

from packages.persistence import models as m
from tests.integration.test_selected_search_ai import SelectedSearchAIHarness


class GroundedAICandidateTests(SelectedSearchAIHarness):
    def profile_preview(self):
        response = self.client.get('/profiles/fixture/ai-cost-plan')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_current_approved_evidence_outranks_stale_cached_score(self):
        with self.store.engine.begin() as connection:
            for job_id, score in ((self.systems, 1), (self.cloud, 99)):
                connection.execute(update(m.matches).where(m.matches.c.profile_id == 'fixture',
                    m.matches.c.job_id == job_id).values(score=score))
        preview = self.profile_preview()
        self.assertEqual([row['job_id'] for row in preview['candidates']], [self.systems])
        candidate = preview['candidates'][0]
        self.assertEqual(candidate['deterministic_score'], 100)
        self.assertEqual(candidate['score_evidence']['basis'], 'APPROVED_SKILL_COVERAGE')
        self.assertEqual(preview['scoring_basis'], 'CURRENT_APPROVED_SKILL_COVERAGE_V1')
        self.assertEqual(self.calls, [])
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.matches.c.score).where(
                m.matches.c.profile_id == 'fixture', m.matches.c.job_id == self.cloud)).scalar_one(), 99)

    def test_unknown_coverage_is_disclosed_and_never_admitted_as_zero(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == self.systems).values(
                skills=[], description='Synthetic role with no supported skill information.', attributes={}))
        self.store.save_ai_cost_policy('fixture', {**self.store.ai_cost_policy('fixture'), 'minimum_score': 75})
        preview = self.profile_preview()
        self.assertEqual(preview['plan']['unscored_job_count'], 1)
        self.assertEqual(preview['plan']['scored_candidate_count'], 1)
        self.assertEqual(preview['candidates'], [])
        self.assertEqual(self.calls, [])

    def test_missing_cached_match_does_not_hide_current_evidence_and_disabled_mode_is_local(self):
        with self.store.engine.begin() as connection:
            connection.execute(delete(m.matches).where(m.matches.c.profile_id == 'fixture'))
        self.store.save_ai_task_settings('fixture', {'job_scoring': {'provider': 'disabled', 'model': None}})
        preview = self.profile_preview()
        self.assertEqual(preview['candidates'][0]['job_id'], self.systems)
        response = self.client.post('/profiles/fixture/ai-cost-plan/evaluate/queued', json={
            'request_id': 'grounded-disabled-intent', 'preview_fingerprint': preview['fingerprint']})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail']['code'], 'AI_PROVIDER_DISABLED')
        self.assertEqual(self.calls, [])

    def test_actual_worker_persists_frozen_explanation_without_sending_profile_evidence(self):
        preview = self.profile_preview()
        response = self.client.post('/profiles/fixture/ai-cost-plan/evaluate/queued', json={
            'request_id': 'grounded-worker-intent', 'preview_fingerprint': preview['fingerprint']})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.run_worker(response.json()['operation_id'])['status'], 'COMPLETED')
        run = self.client.get('/profiles/fixture/ai-cost-plan/evaluations').json()['runs'][0]
        self.assertEqual(run['scoring_basis'], preview['scoring_basis'])
        self.assertEqual(run['evaluations'][0]['score_evidence'], preview['candidates'][0]['score_evidence'])
        self.assertEqual(len(self.calls), 1)
        prompt = self.calls[0]['prompt']
        self.assertNotIn('APPROVED_POSITIVE_EVIDENCE', prompt)
        self.assertNotIn('scoring_contributions', prompt)
        self.assertNotIn(self.store.profile('fixture')['name'], prompt)
        self.assertNotIn(self.store.profile('fixture')['email'], prompt)
        self.assertEqual(self.client.get('/profiles/other/ai-cost-plan/evaluations').json()['runs'], [])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == self.systems).values(skills=['Kubernetes']))
        fresh_client = self.new_client()
        historical = fresh_client.get('/profiles/fixture/ai-cost-plan/evaluations').json()['runs'][0]
        self.assertEqual(historical['evaluations'][0]['input_status'], 'STALE')
        self.assertEqual(historical['evaluations'][0]['score_evidence'], run['evaluations'][0]['score_evidence'])

    def test_changed_evidence_requires_new_preview_before_admission(self):
        preview = self.profile_preview()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == self.systems).values(skills=['Kubernetes']))
        response = self.client.post('/profiles/fixture/ai-cost-plan/evaluate/queued', json={
            'request_id': 'grounded-stale-preview', 'preview_fingerprint': preview['fingerprint']})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail']['code'], 'AI_EVALUATION_PREVIEW_CHANGED')
        self.assertEqual(self.calls, [])

    def test_unapproved_facts_do_not_award_evidence_credit(self):
        profile = self.store.profile('fixture')
        for fact in profile['facts']:
            fact['user_approved'] = False
        self.store.save_profile(profile)
        preview = self.profile_preview()
        self.assertEqual(preview['candidates'][0]['deterministic_score'], 0)
        evidence = preview['candidates'][0]['score_evidence']
        self.assertTrue(all(row['profile_evidence'] == 'NO_APPROVED_EVIDENCE'
                            for row in evidence['skill_evidence']))
        other = self.client.get('/profiles/other/ai-cost-plan').json()
        self.assertEqual(other['candidates'][0]['deterministic_score'], 100)
        self.assertEqual(self.calls, [])
