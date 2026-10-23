"""Connected API acceptance for profile-scoped saved Jobs filters."""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.company_discovery.sources import register_ats_tenant
from services.discovery.scheduler import schedule_due_sources
from services.discovery.pipeline import discover
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE


class SavedSearchFiltersApiTests(unittest.TestCase):
    def test_selected_search_filters_before_result_limit(self):
        criteria = {**self._profile(job_filters={"q": "", "remote_type": "", "provider": "", "sort": "title"}),
                    "include_titles": ["Python Developer"], "remote": "ANY"}
        saved = self.client.put('/search-profiles', json=criteria)
        self.assertEqual(saved.status_code, 200, saved.text)
        response = self.client.get('/jobs', params={
            'profile_id': 'fixture', 'search_profile_id': 'saved-filter',
            'sort': 'title', 'limit': 1,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row['title'] for row in response.json()['jobs']], ['Python Developer'])
        self.assertTrue(response.json()['jobs'][0]['search_evidence']['matched'])
        # The in-memory newest path and SQL sort paths share the same predicate.
        for sort in ('score', 'newest', 'company', 'salary_high', 'salary_low'):
            with self.subTest(sort=sort):
                response = self.client.get('/jobs', params={
                    'profile_id': 'fixture', 'search_profile_id': 'saved-filter',
                    'sort': sort, 'limit': 1, 'application_capability': 'FIXTURE_ONLY',
                })
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual([row['title'] for row in response.json()['jobs']], ['Python Developer'])
                self.assertNotIn('application_route_id', response.json()['jobs'][0])

        reopened = Store(self.store.url)
        try:
            with TestClient(create_app(reopened, 'http://127.0.0.1:1', self.root,
                                       operation_mode='FIXTURE')) as client:
                params = {'profile_id': 'fixture', 'search_profile_id': 'saved-filter',
                          'sort': 'title', 'limit': 1}
                self.assertEqual(client.get('/jobs', params=params).json()['jobs'][0]['title'],
                                 'Python Developer')
                self.assertEqual(client.get('/jobs', params={**params, 'profile_id': 'other'}).status_code, 409)
                edited = client.put('/search-profiles', json={**criteria, 'include_titles': ['Data Engineer']})
                self.assertEqual(edited.status_code, 200, edited.text)
                self.assertEqual(client.get('/jobs', params=params).json()['jobs'][0]['title'], 'Data Engineer')
                self.assertEqual(client.get('/jobs', params={**params, 'title': 'Python'}).json()['jobs'], [])
                self.assertEqual(len(client.get('/jobs').json()['jobs']), 2)
        finally:
            reopened.engine.dispose()

    def test_dismissed_search_match_does_not_consume_limit(self):
        criteria = {**self._profile(job_filters={"q": "", "remote_type": "", "provider": "", "sort": "title"}),
                    "include_titles": [], "remote": "ANY"}
        self.assertEqual(self.client.put('/search-profiles', json=criteria).status_code, 200)
        first = self.client.get('/jobs', params={'sort': 'title'}).json()['jobs'][0]
        from services.tracking.jobs import set_job_disposition
        set_job_disposition(self.store, 'fixture', first['id'], 'HIDDEN', 'hide-first')
        params = {'profile_id': 'fixture', 'search_profile_id': 'saved-filter', 'sort': 'title', 'limit': 1}
        response = self.client.get('/jobs', params=params)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([job['title'] for job in response.json()['jobs']], ['Python Developer'])
        self.assertEqual(self.client.get('/jobs', params={**params, 'include_dismissed': True})
                         .json()['jobs'][0]['id'], first['id'])
        self.assertEqual(self.client.get('/jobs', params={'profile_id': 'other', 'sort': 'title', 'limit': 1})
                         .json()['jobs'][0]['id'], first['id'])

    def test_lever_source_configuration_and_saved_ats_filters_survive_reopen(self):
        for region in ('global','eu'):
            configured = self.client.put('/sources',json={'provider':'lever','tenant':region+':example'})
            self.assertEqual(configured.status_code,200,configured.text)
        self.assertEqual(self.client.put('/sources',json={'provider':'lever','tenant':'example'}).status_code,422)
        self._add_ats_route('Python Developer',route_provider='lever',route_tenant='eu:example',
                            identity_provider='lever')
        # A route label alone cannot establish a verified ATS identity.
        self._add_ats_route('Data Engineer',route_provider='lever',route_tenant='global:other')
        filters = {'q':'','remote_type':'','provider':'lever','ats_provider':'lever','sort':'title'}
        saved = self.client.put('/search-profiles',json=self._profile(job_filters=filters))
        self.assertEqual(saved.status_code,200,saved.text)
        reopened = Store(self.store.url)
        try:
            with TestClient(create_app(reopened,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
                rows = client.get('/search-profiles',params={'profile_id':'fixture'}).json()['search_profiles']
                stored = next(row for row in rows if row['id']=='saved-filter')['criteria']['job_filters']
                self.assertEqual(stored['provider'],'lever')
                self.assertEqual(stored['ats_provider'],'lever')
                response = client.get('/jobs',params={'provider':stored['provider'],'ats_provider':stored['ats_provider']})
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual([row['title'] for row in response.json()['jobs']],['Python Developer'])
                sources = client.get('/sources').json()['sources']
                self.assertEqual({row['tenant'] for row in sources if row['provider']=='lever'},
                                 {'global:example','eu:example'})
        finally:
            reopened.engine.dispose()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-saved-search-filters-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'jobs.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                 "email": "other@example.test"})
        self.source_id = configure_source(self.store, "saved-filter-source")
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Python Developer")
                               .values(location="Boston, MA"))
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Data Engineer")
                               .values(location="Berlin"))
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _profile(self, *, identifier="saved-filter", profile_id="fixture", job_filters=None):
        return {
            "id": identifier, "profile_id": profile_id, "name": f"Remote admin roles {identifier}",
            "enabled": False, "include_titles": ["System Administrator"],
            "remote": "REQUIRED", "employment_types": [], "source_ids": [],
            "job_filters": job_filters or {"q": "network", "title": "Developer", "location": "Boston",
                                           "company": "Example", "remote_type": "REMOTE",
                                           "provider": "greenhouse", "sort": "company",
                                           "salary_min": "", "salary_max": ""},
        }

    def _add_ats_route(self, title, *, route_provider="greenhouse", route_tenant,
                       identity_provider=None, identity_tenant=None, identity_organization="",
                       confidence=0.95, evidence=("VERIFIED_ROUTE_TEST_EVIDENCE",)):
        with self.store.engine.begin() as connection:
            job = connection.execute(select(m.jobs).where(m.jobs.c.title == title)).mappings().one()
            company_id = job["company_id"]
        if identity_provider is not None:
            register_ats_tenant(
                self.store, company_id, provider=identity_provider,
                tenant_identifier=identity_tenant if identity_tenant is not None else route_tenant,
                organization_identifier=identity_organization,
                confidence=confidence, evidence=list(evidence),
            )
        with self.store.engine.begin() as connection:
            upsert(connection, m.routes, {
                "id": identity("saved-ats-route", title, route_provider, route_tenant),
                "job_id": job["id"], "company_id": company_id, "provider": route_provider,
                "tenant": route_tenant, "requisition": "ats-filter-test-" + title,
                "url": f"https://jobs.example.test/{route_provider}/{route_tenant}/{title}",
            })

    def _seed_ats_filter_routes(self):
        self._add_ats_route(
            "Python Developer", route_tenant="python-board", identity_provider="greenhouse",
            confidence=0.75, evidence=("QUALIFIED_GREENHOUSE_ROUTE",),
        )
        self._add_ats_route("Data Engineer", route_tenant="source-only-board")
        self._add_ats_route(
            "Data Engineer", route_tenant="route-mismatch",
            identity_provider="greenhouse", identity_tenant="different-tenant",
        )
        self._add_ats_route(
            "Data Engineer", route_tenant="low-confidence-board",
            identity_provider="greenhouse", confidence=0.74,
        )
        self._add_ats_route(
            "Data Engineer", route_tenant="blank-evidence-board",
            identity_provider="greenhouse", evidence=("  ",),
        )
        self._add_ats_route(
            "Data Engineer", route_tenant="ambiguous-board", identity_provider="greenhouse",
        )
        self._add_ats_route(
            "Data Engineer", route_tenant="ambiguous-board", identity_provider="greenhouse",
            identity_organization="second-organization",
        )

    def test_ats_filter_requires_qualified_persisted_route_identity_before_limit(self):
        self._seed_ats_filter_routes()

        response = self.client.get("/jobs", params={
            "ats_provider": "greenhouse", "sort": "title", "limit": 1,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row["title"] for row in response.json()["jobs"]], ["Python Developer"])
        self.assertEqual(self.client.get("/jobs", params={
            "ats_provider": "greenhouse", "title": "Data",
        }).json()["jobs"], [])

        # Existing source filtering remains separate; it cannot establish the ATS facet.
        source_only = self.client.get("/jobs", params={"provider": "greenhouse", "sort": "title"})
        self.assertEqual([row["title"] for row in source_only.json()["jobs"]],
                         ["Data Engineer", "Python Developer"])
        self.assertEqual(self.client.get("/jobs", params={"ats_provider": "workday"}).status_code, 422)

    def test_ats_filter_saved_search_round_trip_clear_legacy_and_profile_scope(self):
        self._seed_ats_filter_routes()
        filters = {
            "q": "", "title": "", "company": "", "location": "", "salary_min": "",
            "salary_max": "", "min_match_score": "", "experience_level": "",
            "employment_type": "", "skill": "", "education_requirement": "",
            "clearance_level": "", "posted_within_days": "", "remote_type": "",
            "provider": "", "ats_provider": "greenhouse", "sort": "title",
        }
        profile_data = {**self._profile(job_filters=filters), "include_titles": [], "remote": "ANY"}
        saved = self.client.put("/search-profiles", json=profile_data)
        self.assertEqual(saved.status_code, 200, saved.text)
        row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "saved-filter")
        self.assertEqual(row["criteria"]["job_filters"]["ats_provider"], "greenhouse")
        self.assertEqual(self.client.get("/search-profiles", params={"profile_id": "other"})
                         .json()["search_profiles"], [])

        selected = self.client.get("/jobs", params={
            "profile_id": "fixture", "search_profile_id": "saved-filter",
            "ats_provider": row["criteria"]["job_filters"]["ats_provider"], "sort": "title",
        })
        self.assertEqual([job["title"] for job in selected.json()["jobs"]], ["Python Developer"])
        self.assertEqual(self.client.get("/jobs", params={
            "profile_id": "other", "search_profile_id": "saved-filter",
        }).status_code, 409)

        cleared_filters = {**filters, "ats_provider": ""}
        cleared = self.client.put("/search-profiles", json=self._profile(job_filters=cleared_filters))
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertEqual(len(self.client.get("/jobs", params={"sort": "title"}).json()["jobs"]), 2)

        legacy = {key: value for key, value in filters.items() if key != "ats_provider"}
        legacy_saved = self.client.put("/search-profiles", json=self._profile(
            identifier="ats-legacy", job_filters=legacy))
        self.assertEqual(legacy_saved.status_code, 200, legacy_saved.text)
        legacy_row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "ats-legacy")
        self.assertEqual(legacy_row["criteria"]["job_filters"]["ats_provider"], "")
        self.assertEqual(self.client.put("/search-profiles", json=self._profile(
            identifier="ats-invalid", job_filters={**filters, "ats_provider": "workday"})).status_code, 422)
        operations_before = self.store.count("worker_operations")
        self.assertEqual(schedule_due_sources(self.store)["scheduled"], [])
        self.assertEqual(self.store.count("worker_operations"), operations_before)

    def test_qualified_synthetic_mock_identity_is_filterable(self):
        response = self.client.get("/jobs", params={"ats_provider": "mock-ats", "sort": "title"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row["title"] for row in response.json()["jobs"]],
                         ["Data Engineer", "Python Developer"])

    def test_saved_jobs_filters_persist_profile_scope_and_do_not_schedule_discovery(self):
        response = self.client.put("/search-profiles", json=self._profile())
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.client.get("/search-profiles", params={"profile_id": "fixture"})
        self.assertEqual(saved.status_code, 200, saved.text)
        row = next(item for item in saved.json()["search_profiles"]
                   if item["id"] == "saved-filter")

        self.assertEqual(row["criteria"]["include_titles"], ["System Administrator"])
        self.assertEqual(row["criteria"]["remote"], "REQUIRED")
        self.assertEqual(row["criteria"]["job_filters"], {
            "q": "network", "title": "Developer", "location": "Boston", "company": "Example",
            "experience_level": "", "employment_type": "", "posted_within_days": "", "remote_type": "REMOTE", "provider": "greenhouse", "sort": "company",
            "salary_min": "", "salary_max": "", "skill": "", "education_requirement": "",
            "clearance_level": "", "ats_provider": "", "job_source_id": ""})
        self.assertFalse(row["enabled"])
        self.assertEqual(self.client.get("/search-profiles", params={"profile_id": "other"})
                         .json()["search_profiles"], [])
        before = self.store.count("worker_operations")
        tick = schedule_due_sources(self.store)
        self.assertEqual(tick["scheduled"], [])
        self.assertEqual(self.store.count("worker_operations"), before)

    def test_old_saved_job_filters_without_location_remain_valid(self):
        response = self.client.put("/search-profiles", json=self._profile(
            identifier="old-saved-filter",
            job_filters={"q": "network", "title": "Developer", "company": "Example",
                         "remote_type": "REMOTE", "provider": "greenhouse", "sort": "company"}))
        self.assertEqual(response.status_code, 200, response.text)
        row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"]
                   if item["id"] == "old-saved-filter")
        self.assertEqual(row["criteria"]["job_filters"]["location"], "")
        self.assertEqual(row["criteria"]["job_filters"]["salary_min"], "")
        self.assertEqual(row["criteria"]["job_filters"]["salary_max"], "")
        self.assertEqual(row["criteria"]["job_filters"]["employment_type"], "")
        self.assertEqual(row["criteria"]["job_filters"]["skill"], "")

    def test_saved_job_filters_accept_experience_level_and_legacy_payload(self):
        filters = {"q": "", "title": "", "company": "", "location": "",
                   "salary_min": "", "salary_max": "", "min_match_score": "",
                   "experience_level": "SENIOR", "remote_type": "", "provider": "", "sort": "score"}
        saved = self.client.put("/search-profiles", json=self._profile(job_filters=filters))
        self.assertEqual(saved.status_code, 200, saved.text)
        row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "saved-filter")
        self.assertEqual(row["criteria"]["job_filters"]["experience_level"], "SENIOR")
        legacy = self.client.put("/search-profiles", json=self._profile(
            identifier="experience-legacy", job_filters={"q": "", "remote_type": "",
                                                           "provider": "", "sort": "score"}))
        self.assertEqual(legacy.status_code, 200, legacy.text)
        legacy_row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "experience-legacy")
        self.assertEqual(legacy_row["criteria"]["job_filters"]["experience_level"], "")
        self.assertEqual(legacy_row["criteria"]["job_filters"]["skill"], "")
        self.assertEqual(self.client.get("/search-profiles", params={"profile_id": "other"})
                         .json()["search_profiles"], [])
        invalid = self.client.put("/search-profiles", json=self._profile(
            identifier="experience-invalid", job_filters={**filters, "experience_level": "STAFF_PLUS"}))
        self.assertEqual(invalid.status_code, 422)

    def test_experience_level_filter_is_direct_fail_closed_and_before_limit(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Python Developer").values(
                title="A Unsupported level", attributes={"experience_level": "STAFF_PLUS"}))
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Data Engineer").values(
                title="B Senior role", attributes={"experience_level": "SENIOR"}))
        profile = self.store.profile("fixture")
        for job in {item["id"]: item for item in self.store.jobs()}.values():
            with self.store.engine.begin() as connection:
                upsert(connection, m.matches, dict(
                    id=identity(job["id"], "fixture", profile["version"]), job_id=job["id"],
                    profile_id="fixture", profile_version=profile["version"], score=63.5,
                    missing=[], components={}), replace=True)
        with self.store.engine.connect() as connection:
            job_rows_before = connection.execute(
                select(m.jobs.c.id, m.jobs.c.title, m.jobs.c.attributes).order_by(m.jobs.c.id)).all()
            match_rows_before = connection.execute(
                select(m.matches.c.job_id, m.matches.c.score).order_by(m.matches.c.job_id)).all()
        operations_before = self.store.count("worker_operations")
        selected = self.client.get("/jobs", params={"experience_level": "SENIOR", "sort": "title", "limit": 1})
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertEqual([job["title"] for job in selected.json()["jobs"]], ["B Senior role"])
        unknown = self.client.get("/jobs", params={"experience_level": "UNKNOWN", "sort": "title"})
        self.assertEqual(unknown.status_code, 200, unknown.text)
        self.assertEqual(unknown.json()["jobs"], [])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "A Unsupported level").values(
                attributes={"experience_level": "UNKNOWN"}))
        normalized_unknown = self.client.get("/jobs", params={"experience_level": "UNKNOWN", "sort": "title"})
        self.assertEqual([job["title"] for job in normalized_unknown.json()["jobs"]], ["A Unsupported level"])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "A Unsupported level").values(
                attributes={"experience_level": "STAFF_PLUS"}))
        self.assertEqual(self.client.get("/jobs", params={"experience_level": "UNKNOWN"}).json()["jobs"], [])
        unsupported = self.client.get("/jobs", params={"experience_level": "STAFF_PLUS"})
        self.assertEqual(unsupported.status_code, 422)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(
                select(m.jobs.c.id, m.jobs.c.title, m.jobs.c.attributes).order_by(m.jobs.c.id)).all(), job_rows_before)
            self.assertEqual(connection.execute(
                select(m.matches.c.job_id, m.matches.c.score).order_by(m.matches.c.job_id)).all(), match_rows_before)
        self.assertEqual(schedule_due_sources(self.store)["scheduled"], [])
        self.assertEqual(self.store.count("worker_operations"), operations_before)

    def test_saved_experience_filter_round_trip_and_profile_scoping(self):
        filters = {"q": "", "title": "", "company": "", "location": "",
                   "salary_min": "", "salary_max": "", "min_match_score": "",
                   "experience_level": "SENIOR", "remote_type": "", "provider": "", "sort": "score"}
        self.client.put("/search-profiles", json=self._profile(job_filters=filters)).raise_for_status()
        results = self.client.get("/jobs", params={"profile_id": "fixture", "search_profile_id": "saved-filter",
                                                    "experience_level": "SENIOR"})
        self.assertEqual(results.status_code, 200, results.text)
        wrong_owner = self.client.get("/jobs", params={"profile_id": "other", "search_profile_id": "saved-filter",
                                                       "experience_level": "SENIOR"})
        self.assertEqual(wrong_owner.status_code, 409)

    def test_employment_type_filter_is_normalized_fail_closed_and_before_limit(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Python Developer").values(
                title="A Unrecognized type", attributes={"employment_type": "FULLTIME"}))
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Data Engineer").values(
                title="B Full-time role", attributes={"employment_type": "FULL_TIME"}))
        selected = self.client.get("/jobs", params={"employment_type": "FULL_TIME", "sort": "title", "limit": 1})
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertEqual([job["title"] for job in selected.json()["jobs"]], ["B Full-time role"])
        self.assertEqual(self.client.get("/jobs", params={"employment_type": "OTHER"}).json()["jobs"], [])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "B Full-time role").values(attributes={}))
        unknown = self.client.get("/jobs", params={"employment_type": "UNKNOWN", "sort": "title"})
        self.assertEqual([job["title"] for job in unknown.json()["jobs"]],
                         ["A Unrecognized type", "B Full-time role"])
        self.assertEqual(self.client.get("/jobs", params={"employment_type": "FULL_TIME"}).json()["jobs"], [])
        unsupported = self.client.get("/jobs", params={"employment_type": "FULLTIME"})
        self.assertEqual(unsupported.status_code, 422)

    def test_employment_type_saved_search_round_trip_clear_legacy_and_profile_scope(self):
        filters = {"q": "", "title": "", "company": "", "location": "", "salary_min": "",
                   "salary_max": "", "min_match_score": "", "experience_level": "",
                   "posted_within_days": "", "employment_type": "FULL_TIME",
                   "remote_type": "", "provider": "", "sort": "score"}
        saved = self.client.put("/search-profiles", json={**self._profile(job_filters=filters),
                                                            "include_titles": [], "remote": "ANY"})
        self.assertEqual(saved.status_code, 200, saved.text)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Python Developer").values(
                attributes={"employment_type": "FULL_TIME"}))
        row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "saved-filter")
        self.assertEqual(row["criteria"]["job_filters"]["employment_type"], "FULL_TIME")
        results = self.client.get("/jobs", params={"profile_id": "fixture", "search_profile_id": "saved-filter",
                                                    "employment_type": "FULL_TIME"})
        self.assertEqual(results.status_code, 200, results.text)
        self.assertEqual([job["title"] for job in results.json()["jobs"]], ["Python Developer"])
        contradictory = self.client.put("/search-profiles", json={**self._profile(
            job_filters=filters), "include_titles": [], "remote": "ANY", "employment_types": ["PART_TIME"]})
        self.assertEqual(contradictory.status_code, 200, contradictory.text)
        composed = self.client.get("/jobs", params={"profile_id": "fixture", "search_profile_id": "saved-filter",
                                                     "employment_type": "FULL_TIME"})
        self.assertEqual(composed.status_code, 200, composed.text)
        self.assertEqual(composed.json()["jobs"], [])
        self.assertEqual(self.client.get("/search-profiles", params={"profile_id": "other"})
                         .json()["search_profiles"], [])
        legacy = self.client.put("/search-profiles", json=self._profile(
            identifier="employment-legacy", job_filters={"q": "", "remote_type": "",
                                                           "provider": "", "sort": "score"}))
        self.assertEqual(legacy.status_code, 200, legacy.text)
        legacy_row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "employment-legacy")
        self.assertEqual(legacy_row["criteria"]["job_filters"]["employment_type"], "")
        cleared = self.client.put("/search-profiles", json=self._profile(
            job_filters={**filters, "employment_type": ""}))
        self.assertEqual(cleared.status_code, 200, cleared.text)
        invalid = self.client.put("/search-profiles", json=self._profile(
            identifier="employment-invalid", job_filters={**filters, "employment_type": "FULLTIME"}))
        self.assertEqual(invalid.status_code, 422)
        operations_before = self.store.count("worker_operations")
        self.assertEqual(schedule_due_sources(self.store)["scheduled"], [])
        self.assertEqual(self.store.count("worker_operations"), operations_before)

    def test_skill_filter_normalizes_alias_matches_exact_ids_and_labels_before_limit(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Python Developer").values(
                title="A unrecognized skill", location="Berlin", skills=["Active Directory Plus"],
                attributes={"skill_ids": ["ACTIVE_DIRECTORY_EXTRA"]}))
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Data Engineer").values(
                title="Z normalized skill", skills=["Active Directory"],
                attributes={"skill_ids": ["ACTIVE_DIRECTORY"]}))
        selected = self.client.get("/jobs", params={"skill": "AD", "sort": "title", "limit": 1,
                                                     "location": "Berlin"})
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertEqual([job["title"] for job in selected.json()["jobs"]], ["Z normalized skill"])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Z normalized skill").values(
                attributes={}))
        label_match = self.client.get("/jobs", params={"skill": "ACTIVE_DIRECTORY", "sort": "title"})
        self.assertEqual([job["title"] for job in label_match.json()["jobs"]], ["Z normalized skill"])
        no_match = self.client.get("/jobs", params={"skill": "PowerShell"})
        self.assertEqual(no_match.status_code, 200, no_match.text)
        self.assertEqual(no_match.json()["jobs"], [])
        invalid = self.client.get("/jobs", params={"skill": "UNSUPPORTED_TECHNOLOGY"})
        self.assertEqual(invalid.status_code, 422)
        taxonomy = self.client.get("/search-skills")
        self.assertEqual(taxonomy.status_code, 200, taxonomy.text)
        self.assertIn({"id": "ACTIVE_DIRECTORY", "label": "Active Directory"}, taxonomy.json()["skills"])

    def test_skill_filter_saved_search_normalizes_alias_and_composes_semantic_requirements(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Data Engineer").values(
                skills=["Active Directory"], attributes={"skill_ids": ["ACTIVE_DIRECTORY"]}))
        filters = {"q": "", "title": "", "company": "", "location": "", "salary_min": "",
                   "salary_max": "", "min_match_score": "", "experience_level": "",
                   "employment_type": "", "posted_within_days": "", "skill": "AD",
                   "remote_type": "", "provider": "", "sort": "score"}
        saved = self.client.put("/search-profiles", json={**self._profile(job_filters=filters),
                                                            "include_titles": [], "remote": "ANY"})
        self.assertEqual(saved.status_code, 200, saved.text)
        row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "saved-filter")
        self.assertEqual(row["criteria"]["job_filters"]["skill"], "ACTIVE_DIRECTORY")
        result = self.client.get("/jobs", params={"profile_id": "fixture", "search_profile_id": "saved-filter",
                                                   "skill": "AD"})
        self.assertEqual([job["title"] for job in result.json()["jobs"]], ["Data Engineer"])
        contradictory = self.client.put("/search-profiles", json={**self._profile(job_filters=filters),
            "include_titles": [], "remote": "ANY", "required_skills": ["RUST"]})
        self.assertEqual(contradictory.status_code, 200, contradictory.text)
        result = self.client.get("/jobs", params={"profile_id": "fixture", "search_profile_id": "saved-filter",
                                                   "skill": "AD"})
        self.assertEqual(result.json()["jobs"], [])
        legacy = self.client.put("/search-profiles", json=self._profile(
            identifier="skill-legacy", job_filters={"q": "", "remote_type": "", "provider": "", "sort": "score"}))
        self.assertEqual(legacy.status_code, 200, legacy.text)
        legacy_row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "skill-legacy")
        self.assertEqual(legacy_row["criteria"]["job_filters"]["skill"], "")
        cleared = self.client.put("/search-profiles", json={**self._profile(job_filters={**filters, "skill": ""}),
            "include_titles": [], "remote": "ANY"})
        self.assertEqual(cleared.status_code, 200, cleared.text)
        cleared_row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "saved-filter")
        self.assertEqual(cleared_row["criteria"]["job_filters"]["skill"], "")
        invalid = self.client.put("/search-profiles", json={**self._profile(
            identifier="skill-invalid", job_filters={**filters, "skill": "unsupported technology"}),
            "include_titles": [], "remote": "ANY"})
        self.assertEqual(invalid.status_code, 422)
        operations_before = self.store.count("worker_operations")
        self.assertEqual(schedule_due_sources(self.store)["scheduled"], [])
        self.assertEqual(self.store.count("worker_operations"), operations_before)

    def test_education_filter_uses_parser_classifications_and_filters_before_limit(self):
        from services.discovery.search_semantics import parse_education_requirement

        classifications = {
            parse_education_requirement("Bachelor's degree required") ["classification"],
            parse_education_requirement("Bachelor's degree preferred") ["classification"],
            parse_education_requirement("Bachelor's degree or equivalent experience") ["classification"],
            parse_education_requirement("College degree preferred") ["classification"],
            parse_education_requirement("No education details here") ["classification"],
        }
        self.assertEqual(classifications, {
            "BACHELORS_REQUIRED", "BACHELORS_PREFERRED", "BACHELORS_OR_EQUIVALENT_EXPERIENCE",
            "DEGREE_PREFERRED", "NONE_SPECIFIED",
        })
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Python Developer").values(
                title="A unrecognized education", attributes={"education_requirement": "ASSOCIATE_REQUIRED"}))
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Data Engineer").values(
                title="B bachelor's requirement", attributes={"education_requirement": "BACHELORS_REQUIRED"}))
        selected = self.client.get("/jobs", params={"education_requirement": "BACHELORS_REQUIRED",
                                                      "sort": "title", "limit": 1})
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertEqual([job["title"] for job in selected.json()["jobs"]], ["B bachelor's requirement"])
        normalized = self.client.get("/jobs", params={"education_requirement": " bachelors_required ",
                                                        "sort": "title", "limit": 1})
        self.assertEqual([job["title"] for job in normalized.json()["jobs"]], ["B bachelor's requirement"])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "A unrecognized education").values(
                attributes={}))
        missing_excluded = self.client.get("/jobs", params={"education_requirement": "BACHELORS_REQUIRED",
                                                               "sort": "title", "limit": 1})
        self.assertEqual([job["title"] for job in missing_excluded.json()["jobs"]],
                         ["B bachelor's requirement"])
        invalid = self.client.get("/jobs", params={"education_requirement": "ASSOCIATE_REQUIRED"})
        self.assertEqual(invalid.status_code, 422)

    def test_education_filter_saved_search_normalizes_round_trips_and_composes(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Data Engineer").values(
                attributes={"education_requirement": "BACHELORS_REQUIRED"}))
        filters = {"q": "", "title": "", "company": "", "location": "", "salary_min": "",
                   "salary_max": "", "min_match_score": "", "experience_level": "",
                   "employment_type": "", "posted_within_days": "", "skill": "",
                   "education_requirement": "bachelors_required",
                   "remote_type": "", "provider": "", "sort": "score"}
        saved = self.client.put("/search-profiles", json={**self._profile(job_filters=filters),
                                                            "include_titles": [], "remote": "ANY"})
        self.assertEqual(saved.status_code, 200, saved.text)
        row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "saved-filter")
        self.assertEqual(row["criteria"]["job_filters"]["education_requirement"], "BACHELORS_REQUIRED")
        result = self.client.get("/jobs", params={"profile_id": "fixture", "search_profile_id": "saved-filter",
                                                   "education_requirement": "BACHELORS_REQUIRED"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual([job["title"] for job in result.json()["jobs"]], ["Data Engineer"])
        contradictory = self.client.put("/search-profiles", json={**self._profile(job_filters=filters),
            "include_titles": [], "remote": "ANY", "required_skills": ["RUST"]})
        self.assertEqual(contradictory.status_code, 200, contradictory.text)
        result = self.client.get("/jobs", params={"profile_id": "fixture", "search_profile_id": "saved-filter",
                                                   "education_requirement": "BACHELORS_REQUIRED"})
        self.assertEqual(result.json()["jobs"], [])
        self.assertEqual(self.client.get("/search-profiles", params={"profile_id": "other"})
                         .json()["search_profiles"], [])
        legacy = self.client.put("/search-profiles", json=self._profile(
            identifier="education-legacy", job_filters={"q": "", "remote_type": "", "provider": "",
                                                           "sort": "score"}))
        self.assertEqual(legacy.status_code, 200, legacy.text)
        legacy_row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "education-legacy")
        self.assertEqual(legacy_row["criteria"]["job_filters"]["education_requirement"], "")
        cleared = self.client.put("/search-profiles", json={**self._profile(job_filters={
            **filters, "education_requirement": ""}), "include_titles": [], "remote": "ANY"})
        self.assertEqual(cleared.status_code, 200, cleared.text)
        cleared_row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "saved-filter")
        self.assertEqual(cleared_row["criteria"]["job_filters"]["education_requirement"], "")
        invalid = self.client.put("/search-profiles", json={**self._profile(
            identifier="education-invalid", job_filters={**filters, "education_requirement": "ASSOCIATE_REQUIRED"}),
            "include_titles": [], "remote": "ANY"})
        self.assertEqual(invalid.status_code, 422)
        operations_before = self.store.count("worker_operations")
        self.assertEqual(schedule_due_sources(self.store)["scheduled"], [])
        self.assertEqual(self.store.count("worker_operations"), operations_before)

    def test_clearance_filter_matches_exact_posted_classification_before_limit_and_rejects_unknown(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Python Developer").values(
                title="B exact secret", attributes={"clearance": {"level": "SECRET"}}))
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Data Engineer").values(
                title="A malformed clearance", attributes={"clearance": "SECRET"}))
        selected = self.client.get("/jobs", params={"clearance_level": "SECRET", "sort": "title", "limit": 1})
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertEqual([job["title"] for job in selected.json()["jobs"]], ["B exact secret"])
        normalized = self.client.get("/jobs", params={"clearance_level": " secret ", "sort": "title"})
        self.assertEqual([job["title"] for job in normalized.json()["jobs"]], ["B exact secret"])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "A malformed clearance").values(
                attributes={"clearance": {"level": "SECRET_EXTRA"}}))
        exact_only = self.client.get("/jobs", params={"clearance_level": "SECRET", "sort": "title"})
        self.assertEqual([job["title"] for job in exact_only.json()["jobs"]], ["B exact secret"])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "A malformed clearance").values(
                attributes={}))
        missing_excluded = self.client.get("/jobs", params={"clearance_level": "SECRET", "sort": "title"})
        self.assertEqual([job["title"] for job in missing_excluded.json()["jobs"]], ["B exact secret"])
        invalid = self.client.get("/jobs", params={"clearance_level": "SECRET_EXTRA"})
        self.assertEqual(invalid.status_code, 422)
        supported = {"NONE_STATED", "PUBLIC_TRUST", "SECRET", "TOP_SECRET", "TS_SCI", "OTHER"}
        from services.discovery.requirement_parsers import CLEARANCE_LEVELS
        self.assertEqual(CLEARANCE_LEVELS, supported)
        for level in supported:
            self.assertEqual(self.client.get("/jobs", params={"clearance_level": level}).status_code, 200)

    def test_clearance_filter_saved_search_round_trips_clears_and_composes_without_side_effects(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Python Developer").values(
                attributes={"clearance": {"level": "PUBLIC_TRUST"}}))
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Data Engineer").values(
                attributes={"clearance": {"level": "SECRET"}}))
        filters = {"q": "", "title": "", "company": "", "location": "", "salary_min": "",
                   "salary_max": "", "min_match_score": "", "experience_level": "",
                   "employment_type": "", "posted_within_days": "", "skill": "",
                   "education_requirement": "", "clearance_level": "public_trust",
                   "remote_type": "", "provider": "", "sort": "score"}
        saved = self.client.put("/search-profiles", json={**self._profile(job_filters=filters),
                                                            "include_titles": [], "remote": "ANY"})
        self.assertEqual(saved.status_code, 200, saved.text)
        row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "saved-filter")
        self.assertEqual(row["criteria"]["job_filters"]["clearance_level"], "PUBLIC_TRUST")
        result = self.client.get("/jobs", params={"profile_id": "fixture", "search_profile_id": "saved-filter",
                                                   "clearance_level": "PUBLIC_TRUST"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual([job["title"] for job in result.json()["jobs"]], ["Python Developer"])
        contradictory = self.client.put("/search-profiles", json={**self._profile(job_filters=filters),
            "include_titles": [], "remote": "ANY", "required_skills": ["RUST"]})
        self.assertEqual(contradictory.status_code, 200, contradictory.text)
        result = self.client.get("/jobs", params={"profile_id": "fixture", "search_profile_id": "saved-filter",
                                                   "clearance_level": "PUBLIC_TRUST"})
        self.assertEqual(result.json()["jobs"], [])
        self.assertEqual(self.client.get("/search-profiles", params={"profile_id": "other"})
                         .json()["search_profiles"], [])
        legacy = self.client.put("/search-profiles", json=self._profile(
            identifier="clearance-legacy", job_filters={"q": "", "remote_type": "", "provider": "",
                                                           "sort": "score"}))
        self.assertEqual(legacy.status_code, 200, legacy.text)
        legacy_row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "clearance-legacy")
        self.assertEqual(legacy_row["criteria"]["job_filters"]["clearance_level"], "")
        cleared = self.client.put("/search-profiles", json={**self._profile(job_filters={
            **filters, "clearance_level": ""}), "include_titles": [], "remote": "ANY"})
        self.assertEqual(cleared.status_code, 200, cleared.text)
        cleared_row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "saved-filter")
        self.assertEqual(cleared_row["criteria"]["job_filters"]["clearance_level"], "")
        invalid = self.client.put("/search-profiles", json={**self._profile(
            identifier="clearance-invalid", job_filters={**filters, "clearance_level": "SECRET_EXTRA"}),
            "include_titles": [], "remote": "ANY"})
        self.assertEqual(invalid.status_code, 422)
        operations_before = self.store.count("worker_operations")
        self.assertEqual(schedule_due_sources(self.store)["scheduled"], [])
        self.assertEqual(self.store.count("worker_operations"), operations_before)

    def test_saved_filter_validation_and_cross_profile_update_rejection(self):
        self.client.put("/search-profiles", json=self._profile()).raise_for_status()
        malformed = self.client.put("/search-profiles", json=self._profile(
            identifier="malformed", job_filters={"q": "x", "remote_type": "LUNAR"}))
        self.assertEqual(malformed.status_code, 422)

        cross_profile = self.client.put("/search-profiles", json=self._profile(
            profile_id="other", job_filters={"q": "x", "title": "", "company": "",
                                               "remote_type": "REMOTE", "provider": "", "sort": "score"}))
        self.assertNotEqual(cross_profile.status_code, 200, cross_profile.text)
        self.assertEqual(self.client.get("/search-profiles", params={"profile_id": "other"})
                         .json()["search_profiles"], [])

        wrong_owner = self.client.get("/jobs", params={
            "profile_id": "other", "search_profile_id": "saved-filter"})
        self.assertEqual(wrong_owner.status_code, 409)

    def test_title_and_company_are_literal_case_insensitive_and_filter_before_limit(self):
        response = self.client.get("/jobs", params={
            "title": "pYtHoN", "company": "eXaMpLe", "sort": "title", "limit": 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([job["title"] for job in response.json()["jobs"]], ["Python Developer"])

        title_literal = self.client.get("/jobs", params={"title": "%", "limit": 1})
        company_literal = self.client.get("/jobs", params={"company": "_", "limit": 1})
        self.assertEqual(title_literal.status_code, 200, title_literal.text)
        self.assertEqual(company_literal.status_code, 200, company_literal.text)
        self.assertEqual(title_literal.json()["jobs"], [])
        self.assertEqual(company_literal.json()["jobs"], [])

        combined = self.client.get("/jobs", params={
            "q": "Python", "title": "Developer", "company": "Example", "limit": 10})
        self.assertEqual(combined.status_code, 200, combined.text)
        self.assertEqual([job["title"] for job in combined.json()["jobs"]], ["Python Developer"])

    def test_location_text_is_literal_case_insensitive_and_filters_before_limit(self):
        response = self.client.get("/jobs", params={
            "location": "bOsToN", "sort": "title", "limit": 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([job["title"] for job in response.json()["jobs"]], ["Python Developer"])

        for literal in ("%", "_", "Seattle", "REMOTE"):
            with self.subTest(location=literal):
                unmatched = self.client.get("/jobs", params={"location": literal})
                self.assertEqual(unmatched.status_code, 200, unmatched.text)
                self.assertEqual(unmatched.json()["jobs"], [])

        combined = self.client.get("/jobs", params={
            "q": "Python", "title": "Developer", "company": "Example",
            "location": "Boston", "remote_type": "REMOTE", "provider": "mock-ats",
            "sort": "title", "limit": 1})
        self.assertEqual(combined.status_code, 200, combined.text)
        self.assertEqual([job["title"] for job in combined.json()["jobs"]], ["Python Developer"])

    def test_annual_usd_salary_filters_are_inclusive_fail_closed_and_apply_before_limit(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Python Developer").values(
                title="A Unknown salary", attributes={"salary_min": 120000, "salary_max": 140000,
                                                       "salary_currency": "EUR", "salary_period": "YEAR"}))
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Data Engineer").values(
                title="B Annual salary", attributes={"salary_min": 100000, "salary_max": 120000,
                                                       "salary_currency": "USD", "salary_period": "YEAR"}))

        operations_before = self.store.count("worker_operations")
        included = self.client.get("/jobs", params={"salary_min": 100000, "salary_max": 120000,
                                                     "sort": "title", "limit": 1})
        self.assertEqual(included.status_code, 200, included.text)
        self.assertEqual([job["title"] for job in included.json()["jobs"]], ["B Annual salary"])

        lower_endpoint = self.client.get("/jobs", params={"salary_min": 120000})
        upper_endpoint = self.client.get("/jobs", params={"salary_max": 100000})
        no_match = self.client.get("/jobs", params={"salary_min": 120001})
        self.assertEqual([job["title"] for job in lower_endpoint.json()["jobs"]], ["B Annual salary"])
        self.assertEqual([job["title"] for job in upper_endpoint.json()["jobs"]], ["B Annual salary"])
        self.assertEqual(no_match.json()["jobs"], [])
        self.assertEqual(self.client.get("/jobs", params={"salary_min": 120001, "salary_max": 100000}).status_code, 422)
        matching_job = next(job for job in self.store.jobs() if job["title"] == "B Annual salary")
        composed = self.client.get("/jobs", params={"q": "B Annual", "title": "B Annual",
                                                     "company": "Example", "location": "Berlin",
                                                     "remote_type": matching_job["remote_type"],
                                                     "provider": "mock-ats", "salary_min": 100000,
                                                     "salary_max": 120000, "limit": 1})
        self.assertEqual(composed.status_code, 200, composed.text)
        self.assertEqual([job["title"] for job in composed.json()["jobs"]], ["B Annual salary"])
        for metadata in ({}, {"salary_min": 120000, "salary_max": 140000,
                              "salary_currency": "USD", "salary_period": "HOUR"},
                         {"salary_min": "competitive", "salary_max": "negotiable",
                          "salary_currency": "USD", "salary_period": "YEAR"}):
            with self.subTest(metadata=metadata):
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.jobs).where(m.jobs.c.title == "A Unknown salary")
                                       .values(attributes=metadata))
                filtered = self.client.get("/jobs", params={"salary_min": 100000})
                self.assertEqual([job["title"] for job in filtered.json()["jobs"]], ["B Annual salary"])
        self.assertEqual(self.store.count("worker_operations"), operations_before)

    def test_salary_bounds_round_trip_as_profile_scoped_filters_without_side_effects(self):
        filters = {"q": "network", "title": "Developer", "location": "Boston", "company": "Example",
                   "remote_type": "REMOTE", "provider": "greenhouse", "sort": "company",
                   "salary_min": "85000", "salary_max": "130000", "experience_level": "",
                   "employment_type": "", "posted_within_days": "", "skill": "",
                   "education_requirement": "", "clearance_level": "", "ats_provider": "",
                   "job_source_id": ""}
        operations_before = self.store.count("worker_operations")
        response = self.client.put("/search-profiles", json=self._profile(job_filters=filters))
        self.assertEqual(response.status_code, 200, response.text)
        row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "saved-filter")
        self.assertEqual(row["criteria"]["job_filters"], filters)
        self.assertEqual(self.client.get("/search-profiles", params={"profile_id": "other"})
                         .json()["search_profiles"], [])
        malformed = self.client.put("/search-profiles", json=self._profile(
            identifier="inverted-salary", job_filters={**filters, "salary_min": "130001"}))
        self.assertEqual(malformed.status_code, 422)
        self.assertEqual(self.store.count("worker_operations"), operations_before)
        self.assertEqual(schedule_due_sources(self.store)["scheduled"], [])
        self.assertEqual(self.store.count("worker_operations"), operations_before)

    def test_posted_within_filters_before_limit_and_excludes_unknown_posting_dates(self):
        reference_time = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
        clock = self.enterContext(patch("apps.api.main.datetime", wraps=datetime))
        clock.now.return_value = reference_time
        jobs = {row["title"]: row for row in self.store.jobs()}
        old_id = jobs["Python Developer"]["id"]
        recent_id = jobs["Data Engineer"]["id"]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == old_id).values(
                title="A old posting", attributes={"reported_posted_at": "2026-09-01T00:00:00+00:00",
                                                     "first_seen_at": "2026-10-08T00:00:00+00:00"}))
            connection.execute(update(m.jobs).where(m.jobs.c.id == recent_id).values(
                title="Z recent posting", attributes={"reported_posted_at": "2026-10-07T00:00:00+00:00"}))
        response = self.client.get("/jobs", params={"sort": "title", "limit": 1,
                                                      "posted_within_days": 7})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row["id"] for row in response.json()["jobs"]], [recent_id])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == recent_id).values(
                attributes={"reported_posted_at": None, "first_seen_at": "2026-10-08T00:00:00+00:00"}))
        unknown = self.client.get("/jobs", params={"posted_within_days": 7})
        self.assertEqual(unknown.status_code, 200, unknown.text)
        self.assertEqual(unknown.json()["jobs"], [])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == recent_id).values(
                attributes={"reported_posted_at": "not a source date"}))
        malformed = self.client.get("/jobs", params={"posted_within_days": 7})
        self.assertEqual(malformed.status_code, 200, malformed.text)
        self.assertEqual(malformed.json()["jobs"], [])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == recent_id).values(
                attributes={"reported_posted_at": "now"}))
        relative_response = self.client.get("/jobs", params={"posted_within_days": 7})
        self.assertEqual(relative_response.status_code, 200, relative_response.text)
        self.assertEqual(relative_response.json()["jobs"], [])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == recent_id).values(
                attributes={"reported_posted_at": "2026-02-30T12:00:00+00:00"}))
        impossible_date_rows = self.store.jobs(
            posted_after=datetime(2026, 3, 1, tzinfo=timezone.utc),
            posted_before=datetime(2026, 3, 31, tzinfo=timezone.utc))
        self.assertNotIn(recent_id, [row["id"] for row in impossible_date_rows])
        future = (reference_time + timedelta(days=1)).isoformat()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == recent_id).values(
                attributes={"reported_posted_at": future}))
        future_response = self.client.get("/jobs", params={"posted_within_days": 7})
        self.assertEqual(future_response.status_code, 200, future_response.text)
        self.assertEqual(future_response.json()["jobs"], [])

    def test_canonical_date_windows_include_boundaries_and_exclude_older_postings(self):
        reference_time = datetime(2026, 10, 8, 12, 34, 56, tzinfo=timezone.utc)
        clock = self.enterContext(patch("apps.api.main.datetime", wraps=datetime))
        clock.now.return_value = reference_time
        jobs = {row["title"]: row for row in self.store.jobs()}
        included_id = jobs["Python Developer"]["id"]
        excluded_id = jobs["Data Engineer"]["id"]
        cases = {
            "LAST_HOUR": reference_time - timedelta(hours=1),
            "TODAY": reference_time.replace(hour=0, minute=0, second=0, microsecond=0),
            "LAST_24_HOURS": reference_time - timedelta(hours=24),
            "LAST_3_DAYS": reference_time - timedelta(days=3),
            "LAST_7_DAYS": reference_time - timedelta(days=7),
            "LAST_30_DAYS": reference_time - timedelta(days=30),
        }
        for date_window, boundary in cases.items():
            with self.subTest(date_window=date_window):
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.jobs).where(m.jobs.c.id == included_id).values(
                        attributes={"reported_posted_at": boundary.isoformat()}))
                    connection.execute(update(m.jobs).where(m.jobs.c.id == excluded_id).values(
                        attributes={"reported_posted_at": (boundary - timedelta(seconds=1)).isoformat()}))
                response = self.client.get("/jobs", params={"date_window": date_window, "sort": "title"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual([row["id"] for row in response.json()["jobs"]], [included_id])
        for reported in (None, "not a source date", (reference_time + timedelta(seconds=1)).isoformat()):
            with self.subTest(reported=reported):
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.jobs).values(
                        attributes={"reported_posted_at": reported}))
                response = self.client.get("/jobs", params={"date_window": "LAST_30_DAYS"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["jobs"], [])

    def test_custom_date_window_is_inclusive_and_rejects_ambiguous_or_mixed_bounds(self):
        jobs = {row["title"]: row for row in self.store.jobs()}
        start_id = jobs["Python Developer"]["id"]
        end_id = jobs["Data Engineer"]["id"]
        start = "2026-10-01T00:00:00+00:00"
        end = "2026-10-02T00:00:00+00:00"
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == start_id).values(
                attributes={"reported_posted_at": start}))
            connection.execute(update(m.jobs).where(m.jobs.c.id == end_id).values(
                attributes={"reported_posted_at": end}))
        response = self.client.get("/jobs", params={
            "date_window": "CUSTOM", "date_start": start, "date_end": end, "sort": "title",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual({row["id"] for row in response.json()["jobs"]}, {start_id, end_id})

        invalid = (
            {"date_window": "CUSTOM", "date_start": start},
            {"date_window": "CUSTOM", "date_start": end, "date_end": start},
            {"date_window": "CUSTOM", "date_start": "2026-10-01T00:00:00", "date_end": end},
            {"date_window": "CUSTOM", "date_start": "2026-10-01T01:00:00+01:00", "date_end": end},
            {"date_window": "LAST_HOUR", "date_start": start, "date_end": end},
            {"date_window": "LAST_HOUR", "posted_within_days": 1},
        )
        for params in invalid:
            with self.subTest(params=params):
                self.assertEqual(self.client.get("/jobs", params=params).status_code, 422)

    def test_custom_date_window_round_trips_reopens_and_invalid_update_is_atomic(self):
        jobs = {row["title"]: row for row in self.store.jobs()}
        included_id = jobs["Python Developer"]["id"]
        excluded_id = jobs["Data Engineer"]["id"]
        start = "2026-10-01T00:00:00Z"
        end = "2026-10-02T00:00:00Z"
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == included_id).values(
                attributes={"reported_posted_at": "2026-10-01T12:00:00+00:00"}))
            connection.execute(update(m.jobs).where(m.jobs.c.id == excluded_id).values(
                attributes={"reported_posted_at": "2026-10-03T00:00:00+00:00"}))
        payload = {
            **self._profile(job_filters={
                "q": "", "remote_type": "", "provider": "", "sort": "title",
                "posted_within_days": "",
            }),
            "include_titles": [], "remote": "ANY", "date_window": "CUSTOM",
            "date_start": start, "date_end": end,
        }
        saved = self.client.put("/search-profiles", json=payload)
        self.assertEqual(saved.status_code, 200, saved.text)
        row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "saved-filter")
        self.assertEqual(row["criteria"]["date_window"], "CUSTOM")
        self.assertEqual(row["criteria"]["date_start"], "2026-10-01T00:00:00+00:00")
        self.assertEqual(row["criteria"]["date_end"], "2026-10-02T00:00:00+00:00")

        reopened = Store(self.store.url)
        try:
            with TestClient(create_app(reopened, "http://127.0.0.1:1", self.root,
                                       operation_mode="FIXTURE")) as client:
                with patch.object(reopened, "jobs", wraps=reopened.jobs) as jobs_query:
                    response = client.get("/jobs", params={
                        "profile_id": "fixture", "search_profile_id": "saved-filter", "sort": "title",
                    })
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual([job["id"] for job in response.json()["jobs"]], [included_id])
                    self.assertEqual(jobs_query.call_args.kwargs["posted_after"],
                                     datetime(2026, 10, 1, tzinfo=timezone.utc))
                    self.assertEqual(jobs_query.call_args.kwargs["posted_before"],
                                     datetime(2026, 10, 2, tzinfo=timezone.utc))
                    jobs_query.reset_mock()
                    explicit_any = client.get("/jobs", params={
                        "profile_id": "fixture", "search_profile_id": "saved-filter",
                        "date_window": "ANY", "override_saved_match_score": True, "sort": "title",
                    })
                    self.assertEqual(explicit_any.status_code, 200, explicit_any.text)
                    self.assertIsNone(jobs_query.call_args.kwargs["posted_after"])
                    self.assertIsNone(jobs_query.call_args.kwargs["posted_before"])
                for invalid_start in ("2026-10-01T00:00:00", "2026-10-01T01:00:00+01:00"):
                    invalid = client.put("/search-profiles", json={**payload, "date_start": invalid_start})
                    self.assertEqual(invalid.status_code, 422, invalid.text)
                unchanged = next(item for item in client.get("/search-profiles", params={
                    "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "saved-filter")
                self.assertEqual(unchanged["criteria"]["date_start"], "2026-10-01T00:00:00+00:00")
        finally:
            reopened.engine.dispose()

    def test_posted_within_search_filter_round_trips_profile_scoped(self):
        filters = {"q": "", "title": "", "company": "", "location": "", "salary_min": "",
                   "salary_max": "", "min_match_score": "", "experience_level": "",
                   "posted_within_days": "7", "remote_type": "", "provider": "", "sort": "score"}
        response = self.client.put("/search-profiles", json=self._profile(job_filters=filters))
        self.assertEqual(response.status_code, 200, response.text)
        row = next(item for item in self.client.get("/search-profiles", params={"profile_id": "fixture"})
                   .json()["search_profiles"] if item["id"] == "saved-filter")
        self.assertEqual(row["criteria"]["job_filters"]["posted_within_days"], "7")
        cleared = {**filters, "posted_within_days": ""}
        response = self.client.put("/search-profiles", json=self._profile(job_filters=cleared))
        self.assertEqual(response.status_code, 200, response.text)
        row = next(item for item in self.client.get("/search-profiles", params={"profile_id": "fixture"})
                   .json()["search_profiles"] if item["id"] == "saved-filter")
        self.assertEqual(row["criteria"]["job_filters"]["posted_within_days"], "")


if __name__ == "__main__":
    unittest.main()
