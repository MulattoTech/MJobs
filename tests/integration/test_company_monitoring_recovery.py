"""Recovery, authorization, and receipt boundaries for company-source monitoring."""
import copy
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

import httpx
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.company_discovery.domains import record_company_domain
from services.company_discovery.monitoring import configure_monitoring, queue_refresh, schedule_company_sources, source_version
from services.company_discovery.public_client import HTTPStatusError, PublicCompanyClient
from services.company_discovery.sources import upsert_company_job_source
from services.discovery.search_profiles import save_search_profile
from tests.integration.test_spine import PROFILE
from workers.operations import request_cancel
from workers.tasks import execute_operation


class MonitoringFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'monitor.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(copy.deepcopy(PROFILE))
        save_search_profile(self.store, dict(id="all", profile_id="fixture", name="All sources", source_ids=[]))
        self.company = identity("public-company-origin", "https://employer.example")
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {"id": self.company, "name": "Employer", "domain": ""})
        self.source = upsert_company_job_source(
            self.store, company_id=self.company, provider="json_feed", tenant_identifier=self.company,
            career_url="https://employer.example/careers", public_api_url="https://employer.example/jobs.json",
            collection_strategy="PUBLIC_JSON", enabled=True, discovered_automatically=True, confidence=.45)
        self.env = dict(MJOBS_DATABASE_URL=self.store.url, MJOBS_OPERATION_MODE="PERSONAL",
                        MJOBS_REDIS_URL="redis://127.0.0.1:1/0", MJOBS_OUTPUT_DIR=str(self.root / "out"))
        self.pages = {
            "/robots.txt": "User-agent: *\nAllow: /",
            "/": '<a rel="careers" href="/careers">Careers</a>',
            "/careers": '<link rel="alternate" type="application/feed+json" href="/jobs.json">',
            "/jobs.json": json.dumps({"version": "https://jsonfeed.org/version/1.1", "items": [
                {"id": "one", "title": "Python Engineer", "content_text": "Python SQL",
                 "url": "https://employer.example/jobs/one"}]}),
            "/jobs/one": "<html>No structured identity</html>",
        }

    def close(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def fetch(self, url, deadline):
        request = httpx.Request("GET", url)
        return httpx.Response(200, text=self.pages[request.url.path], request=request)

    def configure(self):
        return configure_monitoring(self.store, self.source["id"], source_version(self.source), 0, 3600)

    def monitor(self):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.company_source_monitoring)).mappings().one())


# Only the heartbeat and network transport are fixtures. Scheduling, durable
# admission, refresh validation/import and acknowledgements stay production code.
_MONITOR_SCHEDULER_CHILD = r"""
import json, os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from services.discovery import scheduler
report = Path(os.environ["MJOBS_TEST_PROCESS_REPORT"])
class HeartbeatRecorder:
    def set(self, key, value, ex):
        report.with_suffix(".heartbeat.json").write_text(json.dumps({
            "key": key, "value": json.loads(value), "ttl": ex}), encoding="utf-8")
    def close(self):
        pass
scheduler.Redis = SimpleNamespace(from_url=lambda *a, **k: HeartbeatRecorder())
original = scheduler.schedule_due_sources
def capture(store, at=None):
    fixture_at = os.environ.get("MJOBS_TEST_SCHEDULER_AT")
    result = original(store, at=datetime.fromisoformat(fixture_at) if fixture_at else at)
    report.write_text(json.dumps({"pid": os.getpid(), "tick": result}), encoding="utf-8")
    return result
scheduler.schedule_due_sources = capture
def exit_after_tick(_seconds):
    raise SystemExit(0)
scheduler.time.sleep = exit_after_tick
scheduler.run_forever()
"""

_MONITOR_WORKER_CHILD = r"""
import json, os, time
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch
import httpx
from services.company_discovery.public_client import PublicCompanyClient
from workers.tasks import execute_operation
origin = os.environ["MJOBS_TEST_FEED_ORIGIN"]
assert urlsplit(origin).hostname == "127.0.0.1"
seen = []
def loopback_transport(client, url, deadline):
    parsed = urlsplit(url)
    assert (parsed.scheme, parsed.netloc) == ("https", "employer.example"), "unexpected egress"
    assert not parsed.query and not parsed.fragment
    client._check()
    client._budget()
    remaining = deadline - time.monotonic()
    assert remaining > 0
    response = httpx.get(origin + parsed.path, timeout=min(5, remaining), trust_env=False,
                         follow_redirects=False)
    seen.append(parsed.path)
    return httpx.Response(response.status_code, content=response.content,
                          headers=response.headers, request=httpx.Request("GET", url))
with patch.object(PublicCompanyClient, "_request_once", loopback_transport):
    result = execute_operation(os.environ["MJOBS_TEST_OPERATION_ID"])
Path(os.environ["MJOBS_TEST_PROCESS_REPORT"]).write_text(json.dumps({
    "pid": os.getpid(), "result": result, "requests": seen}), encoding="utf-8")
"""


class CompanyMonitoringRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = MonitoringFixture()
        self.store = self.fixture.store

    def tearDown(self):
        self.fixture.close()

    def _scheduled(self, at=None):
        return schedule_company_sources(self.store, at=at)["scheduled"]

    def _execute(self, operation_id, fetch=True):
        with patch.dict(os.environ, self.fixture.env):
            if fetch:
                with patch.object(PublicCompanyClient, "_request_once", self.fixture.fetch):
                    return execute_operation(operation_id)
            with patch.object(PublicCompanyClient, "get", side_effect=AssertionError("unexpected egress")):
                return execute_operation(operation_id)


    @unittest.skipUnless(os.environ.get("MJOBS_RUN_COMPANY_MONITORING_STACK") == "1",
                         "STK01 requires frozen static audit and explicit disposable-stack resource grant")
    def test_owned_stack_restart_preserves_data_and_resumes_company_monitoring(self):
        from fixtures.company_monitoring_stack import run_stack
        result = run_stack(Path(os.environ["MJOBS_STK01_ARTIFACTS"]))
        self.assertEqual(result["result"], "PASS")
        self.assertTrue(all(not ids for ids in result["residual_resources"].values()))

    def test_public_discovery_enable_natural_due_and_safe_prepare_prerequisites(self):
        """Small API preflight; this does not claim an actual stack restart."""
        from apps.api.main import create_app
        store = Store(f"sqlite:///{(self.fixture.root / 'preflight.db').as_posix()}")
        store.migrate()
        store.bind_workspace_mode('PERSONAL')
        from fixtures.company_monitoring_stack import SYNTHETIC_JOB_DESCRIPTION
        pages = dict(self.fixture.pages)
        feed=json.loads(pages['/jobs.json'])
        feed['items'][0]['content_text']=SYNTHETIC_JOB_DESCRIPTION
        pages['/jobs.json']=json.dumps(feed)
        pages['/jobs/one'] = '<script type="application/ld+json">' + json.dumps({
            '@context':'https://schema.org','@type':'JobPosting','title':'Python Engineer',
            'identifier':{'name':'Synthetic Monitoring Employer','value':'one'},
            'hiringOrganization':{'@type':'Organization','name':'Synthetic Monitoring Employer',
                                  'sameAs':'https://employer.example/'},
            'url':'https://employer.example/jobs/one','description':SYNTHETIC_JOB_DESCRIPTION}) + '</script>'
        def transport(client, url, deadline):
            request=httpx.Request('GET',url)
            return httpx.Response(200,text=pages[request.url.path],request=request)
        env={**self.fixture.env,'MJOBS_DATABASE_URL':store.url,
             'MJOBS_OUTPUT_DIR':str(self.fixture.root/'preflight')}
        try:
            with patch.dict(os.environ,env), patch.object(PublicCompanyClient,'_request_once',transport), \
                    TestClient(create_app(store,'http://127.0.0.1:8090',self.fixture.root/'preflight',
                               'redis://127.0.0.1:1/0',operation_mode='PERSONAL')) as client:
                self.assertEqual(client.put('/profiles',json=PROFILE).status_code,200)
                self.assertEqual(client.put('/search-profiles',json={'id':'preflight-all','profile_id':'fixture',
                    'name':'Synthetic monitor','enabled':True,'source_ids':[]}).status_code,200)
                admitted=client.post('/companies/discovery',json={'company_name':'Synthetic Monitoring Employer',
                    'company_website':'https://employer.example/','reason':'USER_ADDED_COMPANY',
                    'profile_id':'fixture','request_id':'preflight'})
                self.assertEqual(admitted.status_code,202,admitted.text)
                result=execute_operation(admitted.json()['operation_id'])
                self.assertEqual(result['status'],'COMPLETED',result)
                def view():
                    return client.get('/company-sources/view',params={'profile_id':'fixture'}).json()['company_sources'][0]
                source=view();source_id=source['id']
                self.assertEqual(source['provider'],'json_feed')
                self.assertEqual(source['collection_strategy'],'PUBLIC_JSON')
                self.assertFalse(source['enabled'])
                rejected=client.put(f'/company-sources/{source_id}/monitoring',json={
                    'source_version':source['source_version'],'revision':0,'poll_interval':300})
                self.assertEqual(rejected.status_code,409)
                self.assertIn('MONITOR_SOURCE_DISABLED',rejected.text)
                self.assertEqual(client.put(f'/company-sources/{source_id}/enabled',json={'enabled':True}).status_code,200)
                fresh=view();self.assertTrue(fresh['enabled'])
                self.assertEqual(fresh['id'],source_id)
                configured=client.put(f'/company-sources/{source_id}/monitoring',json={
                    'source_version':fresh['source_version'],
                    'revision':(fresh['monitoring'] or {}).get('revision',0),'poll_interval':300})
                self.assertEqual(configured.status_code,200,configured.text)
                monitor=configured.json()
                self.assertEqual(monitor['authorization'],'USER_REVIEWED')
                due=datetime.fromisoformat(monitor['next_due'])
                self.assertEqual(due,datetime.fromisoformat(fresh['last_success'])+timedelta(seconds=300))
                self.assertEqual(schedule_company_sources(store)['scheduled'],[])
                job=client.get('/jobs',params={'profile_id':'fixture'}).json()['jobs'][0]
                prepared=client.post('/applications',json={'profile_id':'fixture','job_id':job['id']})
                self.assertEqual(prepared.status_code,202,prepared.text)
                result=execute_operation(prepared.json()['operation_id'])
                self.assertEqual(result['status'],'COMPLETED',result)
                docs=client.get('/profiles/fixture/applications/'+prepared.json()['application_id']+'/documents')
                self.assertEqual(docs.status_code,200,docs.text)
                saved_docs=docs.json()['documents']
                self.assertTrue(saved_docs)
            reopened=Store(store.url)
            try:
                with TestClient(create_app(reopened,'http://127.0.0.1:8090',self.fixture.root/'preflight',
                                    'redis://127.0.0.1:1/0',operation_mode='PERSONAL')) as fresh_client:
                    application_id=prepared.json()['application_id']
                    application=fresh_client.get('/profiles/fixture/applications/'+application_id)
                    self.assertEqual(application.status_code,200,application.text)
                    self.assertEqual(application.json()['id'],application_id)
                    for key in ['id','profile_id','job_id','state','route_id']:
                        self.assertIn(key,application.json())
                    documents=fresh_client.get('/profiles/fixture/applications/'+application_id+'/documents').json()['documents']
                    self.assertEqual(documents,saved_docs)
                    import hashlib
                    for document in documents:
                        for extension in ['txt','docx','pdf']:
                            response=fresh_client.get('/profiles/fixture/documents/'+document['id']+'/'+extension)
                            self.assertEqual(response.status_code,200)
                            self.assertTrue(response.content)
                            self.assertEqual(hashlib.sha256(response.content).hexdigest(),document['sha256'][extension])
            finally:
                reopened.engine.dispose()
        finally:
            store.engine.dispose()

    def test_company_monitoring_resumes_across_fresh_scheduler_and_refresh_workers(self):
        """A scheduler-process restart, not a full-stack/container restart."""
        from apps.api.main import create_app

        deadline = time.monotonic() + 180
        reports, requested_paths = [], []
        fixture = self.fixture

        class FeedHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                requested_paths.append(self.path)
                content = fixture.pages.get(self.path)
                self.send_response(200 if content is not None else 404)
                self.send_header("Content-Type", "application/json" if self.path == "/jobs.json" else "text/html")
                self.end_headers()
                self.wfile.write((content or "Not found").encode())

            def log_message(self, *_args):
                pass

        feed = ThreadingHTTPServer(("127.0.0.1", 0), FeedHandler)
        thread = threading.Thread(target=feed.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(feed.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(feed.shutdown)
        origin = f"http://127.0.0.1:{feed.server_port}"
        environment = {key: os.environ[key] for key in (
            "PATH", "SYSTEMROOT", "SystemDrive", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA", "APPDATA")
            if key in os.environ}
        environment.update(fixture.env, MJOBS_SCHEDULER_TICK_SECONDS="5", PYTHONUTF8="1",
                           MJOBS_TEST_FEED_ORIGIN=origin)

        def child(name, operation_id=None, at=None):
            self.assertLess(time.monotonic(), deadline, "owned case exceeded180s")
            report = fixture.root / (name + ".json")
            env = dict(environment, MJOBS_TEST_PROCESS_REPORT=str(report))
            if at is not None:
                env["MJOBS_TEST_SCHEDULER_AT"] = at.isoformat()
            if operation_id:
                env["MJOBS_TEST_OPERATION_ID"] = operation_id
            completed = subprocess.run(
                [sys.executable, "-c", _MONITOR_WORKER_CHILD if operation_id else _MONITOR_SCHEDULER_CHILD],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True,
                text=True, timeout=min(45, max(1, deadline-time.monotonic())))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(report.is_file(), "owned child must execute production work before exit")
            result = json.loads(report.read_text(encoding="utf-8"))
            reports.append(result)
            if not operation_id:
                heartbeat = json.loads(report.with_suffix(".heartbeat.json").read_text(encoding="utf-8"))
                self.assertEqual(heartbeat["key"], "mjobs:runtime:scheduler:last_success")
                self.assertEqual(heartbeat["value"]["interval_seconds"], 5)
                self.assertGreater(heartbeat["ttl"], 5)
            return result

        def public_readback():
            reopened = Store(self.store.url)
            try:
                with TestClient(create_app(reopened, origin, fixture.root,
                                           "redis://127.0.0.1:1/0", operation_mode="PERSONAL")) as client:
                    response = client.get("/company-sources/view", params={"profile_id": "fixture"})
                    self.assertEqual(response.status_code, 200, response.text)
                    rows = response.json()["company_sources"]
                    source = next(row for row in rows if row["id"] == fixture.source["id"])
                    jobs = client.get("/jobs", params={"profile_id": "fixture"})
                    self.assertEqual(jobs.status_code, 200, jobs.text)
                    return source, jobs.json()
            finally:
                reopened.engine.dispose()

        with TestClient(create_app(self.store, origin, fixture.root,
                                   "redis://127.0.0.1:1/0", operation_mode="PERSONAL")) as client:
            response = client.put(f"/company-sources/{fixture.source['id']}/monitoring", json={
                "source_version": source_version(fixture.source), "revision": 0, "poll_interval": 3600})
            self.assertEqual(response.status_code, 200, response.text)
        self.store.engine.dispose()
        first = child("scheduler-first")["tick"]["company_sources"]["scheduled"]
        self.assertEqual(len(first), 1)
        first_id = first[0]["operation_id"]
        self.assertEqual(child("scheduler-reopened-while-queued")["tick"]["company_sources"]["scheduled"], [])
        self.assertEqual(self.store.count(m.operations.name), 1)
        first_worker = child("worker-first", first_id)
        self.assertEqual(first_worker["result"]["status"], "COMPLETED", first_worker)
        first_monitor = fixture.monitor()
        self.assertEqual(first_monitor["status"], "HEALTHY")
        self.assertEqual(first_monitor["completed_operation_id"], first_id)
        first_due = datetime.fromisoformat(first_monitor["next_due"])
        self.assertGreater(first_due, datetime.fromisoformat(first_monitor["last_attempt"]))
        view, jobs = public_readback()
        self.assertEqual(view["monitoring"]["status"], "HEALTHY")
        self.assertEqual(view["next_check"], first_monitor["next_due"])
        self.assertEqual(view["jobs_found"], 1)
        self.assertIn("Python Engineer", json.dumps(jobs))
        self.assertEqual(child("scheduler-restarted-not-due")["tick"]["company_sources"]["scheduled"], [])

        fixture.pages["/jobs.json"] = json.dumps({"version": "https://jsonfeed.org/version/1.1", "items": [
            {"id": "one", "title": "Python Engineer", "content_text": "Python SQL",
             "url": "https://employer.example/jobs/one"},
            {"id": "two", "title": "Python Reliability Engineer", "content_text": "Python SQL",
             "url": "https://employer.example/jobs/two"}]})
        fixture.pages["/jobs/two"] = "<html>New synthetic posting</html>"
        # Only the fixture's tick input advances; host time and saved completion are untouched.
        due_at = first_due + timedelta(seconds=1)
        second = child("scheduler-restarted-due", at=due_at)["tick"]["company_sources"]["scheduled"]
        self.assertEqual(len(second), 1)
        second_id = second[0]["operation_id"]
        self.assertNotEqual(second_id, first_id)
        self.assertEqual(child("scheduler-duplicate-due", at=due_at)["tick"]["company_sources"]["scheduled"], [])
        self.assertEqual(self.store.count(m.operations.name), 2)
        self.assertEqual(child("worker-after-restart", second_id)["result"]["status"], "COMPLETED")
        self.store.engine.dispose()
        final_monitor = fixture.monitor()
        self.assertEqual(final_monitor["status"], "HEALTHY")
        self.assertEqual(final_monitor["completed_operation_id"], second_id)
        self.assertGreater(datetime.fromisoformat(final_monitor["next_due"]), first_due)
        self.assertEqual(final_monitor["failure_count"], 0)
        view, jobs = public_readback()
        self.assertEqual(view["monitoring"]["status"], "HEALTHY")
        self.assertEqual(view["jobs_found"], 2)
        self.assertEqual(view["next_check"], final_monitor["next_due"])
        self.assertIn("Python Reliability Engineer", json.dumps(jobs))
        self.assertEqual(self.store.count("source_quality_observations"), 2)
        with self.store.engine.connect() as connection:
            operations = list(connection.execute(select(m.operations)).mappings())
            self.assertEqual({row["id"] for row in operations}, {first_id, second_id})
            self.assertTrue(all(row["state"] == "COMPLETED" and row["attempt_count"] == 1 for row in operations))
            self.assertEqual(next(row for row in operations if row["id"] == second_id)["params"]["predecessor_operation_id"], first_id)
        self.assertEqual(child("scheduler-final-readback")["tick"]["company_sources"]["scheduled"], [])
        self.assertEqual(self.store.count(m.operations.name), 2)
        self.assertEqual(requested_paths.count("/jobs.json"), 2, "one actual feed request per refresh")
        self.assertIn("/jobs/two", requested_paths)
        self.assertEqual(len({row["pid"] for row in reports}), len(reports), "all children must be fresh processes")
        feed.shutdown()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "owned loopback feed must close")

    def test_cancelled_queued_operation_is_not_reused_or_reopened(self):
        self.fixture.configure()
        queued = self._scheduled()[0]
        cancelled = request_cancel(self.store, queued["operation_id"])
        self.assertEqual(cancelled["state"], "CANCELLED")
        tick = self._scheduled()
        self.assertEqual(tick, [])
        with self.store.engine.connect() as connection:
            state = connection.execute(select(m.operations.c.state).where(m.operations.c.id == queued["operation_id"])).scalar_one()
        self.assertEqual(state, "CANCELLED")

    def test_next_due_tick_creates_new_operation_with_terminal_predecessor(self):
        self.fixture.configure()
        first = self._scheduled()[0]
        request_cancel(self.store, first["operation_id"])
        base = datetime.now(timezone.utc) + timedelta(days=2)
        self.assertEqual(self._scheduled(at=base), [])
        second = self._scheduled(at=base + timedelta(seconds=3601))[0]
        self.assertNotEqual(second["operation_id"], first["operation_id"])
        with self.store.engine.connect() as connection:
            params = connection.execute(select(m.operations.c.params).where(m.operations.c.id == second["operation_id"])).scalar_one()
        self.assertEqual(params["predecessor_operation_id"], first["operation_id"])

    def test_paused_profiles_block_before_any_public_egress(self):
        self.fixture.configure()
        queued = self._scheduled()[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.search_profiles).values(enabled=False))
        result = self._execute(queued["operation_id"], fetch=False)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error_category"], "MONITOR_SEARCH_PAUSED")
        self.assertEqual(self.store.count("source_quality_observations"), 0)

    def test_endpoint_retarget_blocks_before_egress(self):
        self.fixture.configure()
        queued = self._scheduled()[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.company_sources).where(m.company_sources.c.id == self.fixture.source["id"])
                               .values(endpoint="https://employer.example/changed.json"))
        result = self._execute(queued["operation_id"], fetch=False)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error_category"], "MONITOR_SOURCE_CHANGED")
        self.assertEqual(self.store.count("source_quality_observations"), 0)

    def test_disable_while_queued_blocks_before_egress(self):
        self.fixture.configure()
        queued = self._scheduled()[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.company_sources).where(m.company_sources.c.id == self.fixture.source["id"])
                               .values(enabled=False))
        result = self._execute(queued["operation_id"], fetch=False)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error_category"], "MONITOR_SOURCE_DISABLED")
        self.assertEqual(self.store.count("source_quality_observations"), 0)

    def test_multiple_profiles_share_one_refresh_receipt_and_import(self):
        second = copy.deepcopy(PROFILE)
        second["id"] = "fixture-two"
        self.store.save_profile(second)
        save_search_profile(self.store, dict(id="all-two", profile_id="fixture-two", name="All sources", source_ids=[]))
        self.fixture.configure()
        queued = self._scheduled()[0]
        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(m.operations.c.id == queued["operation_id"])).mappings().one()
        self.assertEqual(operation["params"]["profile_ids"], ["fixture", "fixture-two"])
        result = self._execute(queued["operation_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        monitor = self.fixture.monitor()
        self.assertEqual(monitor["completed_operation_id"], queued["operation_id"])
        self.assertEqual(self.store.count("source_quality_observations"), 1)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.matches.c.profile_id).distinct()).scalars().all(), ["fixture", "fixture-two"])

    def test_profile_scope_change_after_final_get_blocks_without_import_or_receipt(self):
        second = copy.deepcopy(PROFILE)
        second["id"] = "fixture-two"
        self.store.save_profile(second)
        save_search_profile(self.store, dict(id="all-two", profile_id="fixture-two", name="All sources", source_ids=[]))
        self.fixture.configure()
        queued = self._scheduled()[0]
        from services.discovery.company_feed import import_company_feed_jobs as original_import

        def pause_second_then_import(*args, **kwargs):
            with self.store.engine.begin() as connection:
                connection.execute(update(m.search_profiles).where(m.search_profiles.c.profile_id == "fixture-two")
                                   .values(enabled=False))
            return original_import(*args, **kwargs)

        with patch.dict(os.environ, self.fixture.env), patch.object(PublicCompanyClient, "_request_once", self.fixture.fetch), \
                patch("services.discovery.company_feed.import_company_feed_jobs", side_effect=pause_second_then_import):
            result = execute_operation(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error_category"], "MONITOR_SEARCH_PROFILE_CHANGED")
        self.assertEqual(self.store.count("canonical_jobs"), 0)
        self.assertEqual(self.store.count("job_matches"), 0)
        self.assertEqual(self.store.count("source_quality_observations"), 0)
        self.assertIsNone(self.fixture.monitor()["completed_operation_id"])

    def test_conflicting_verified_domain_after_final_get_blocks_without_import_or_receipt(self):
        record_company_domain(self.store, self.fixture.company, "employer.example",
                              "VERIFIED_EXISTING_DOMAIN", ["Independent employer record"])
        self.fixture.configure()
        queued = self._scheduled()[0]
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {"id": "other-company", "name": "Other Employer", "domain": ""})
        from services.discovery.company_feed import import_company_feed_jobs as original_import

        def add_conflicting_domain_then_import(*args, **kwargs):
            record_company_domain(self.store, "other-company", "employer.example",
                                  "VERIFIED_EXISTING_DOMAIN", ["Independent conflicting employer record"])
            return original_import(*args, **kwargs)

        with patch.dict(os.environ, self.fixture.env), patch.object(PublicCompanyClient, "_request_once", self.fixture.fetch), \
                patch("services.discovery.company_feed.import_company_feed_jobs", side_effect=add_conflicting_domain_then_import):
            result = execute_operation(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error_category"], "COMPANY_IDENTITY_REVIEW_REQUIRED")
        self.assertEqual(self.store.count("canonical_jobs"), 0)
        self.assertEqual(self.store.count("source_quality_observations"), 0)
        self.assertIsNone(self.fixture.monitor()["completed_operation_id"])

    def test_domain_ambiguity_after_first_network_response_blocks_without_failed_quality(self):
        record_company_domain(self.store, self.fixture.company, "employer.example",
                              "VERIFIED_EXISTING_DOMAIN", ["Independent employer record"])
        self.fixture.configure()
        queued = self._scheduled()[0]
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {"id": "other-company", "name": "Other Employer", "domain": ""})
        calls = 0

        def fetch_then_conflict(client, url, deadline):
            nonlocal calls
            calls += 1
            response = self.fixture.fetch(url, deadline)
            if calls == 1:
                record_company_domain(self.store, "other-company", "employer.example",
                                      "VERIFIED_EXISTING_DOMAIN", ["Independent conflicting employer record"])
            return response

        with patch.dict(os.environ, self.fixture.env), patch.object(PublicCompanyClient, "_request_once", fetch_then_conflict):
            result = execute_operation(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error_category"], "COMPANY_IDENTITY_REVIEW_REQUIRED")
        self.assertEqual(self.store.count("source_quality_observations"), 0)
        self.assertEqual(self.store.count("canonical_jobs"), 0)
        self.assertIsNone(self.fixture.monitor()["completed_operation_id"])

    def test_failed_feed_has_no_success_receipt_and_retains_failed_observation(self):
        self.fixture.configure()
        queued = self._scheduled()[0]
        self.fixture.pages["/jobs.json"] = json.dumps({"version": "https://jsonfeed.org/version/1.1", "items": [
            {"id": "duplicate", "title": "One", "url": "https://employer.example/jobs/a"},
            {"id": "duplicate", "title": "Two", "url": "https://employer.example/jobs/b"}]})
        result = self._execute(queued["operation_id"])
        self.assertIn(result["status"], {"FAILED", "BLOCKED"})
        self.assertEqual(self.store.count("source_quality_observations"), 1)
        with self.store.engine.connect() as connection:
            self.assertFalse(connection.execute(select(m.source_quality_observations.c.succeeded)).scalar_one())
        monitor = self.fixture.monitor()
        self.assertIsNone(monitor["completed_operation_id"])

    def test_network_failure_sets_backoff_without_success_receipt(self):
        self.fixture.configure()
        queued = self._scheduled()[0]
        with patch.dict(os.environ, self.fixture.env), patch.object(PublicCompanyClient, "get", side_effect=HTTPStatusError(503)):
            result = execute_operation(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error_category"], "HTTP_STATUS_ERROR")
        monitor = self.fixture.monitor()
        self.assertEqual(monitor["status"], "IDLE")
        self.assertEqual(monitor["failure_count"], 1)
        self.assertIsNone(monitor["completed_operation_id"])
        self.assertEqual(self.store.count("source_quality_observations"), 1)
        with self.store.engine.connect() as connection:
            self.assertFalse(connection.execute(select(m.source_quality_observations.c.succeeded)).scalar_one())

    def test_running_cancellation_does_not_add_failure_or_quality_observation(self):
        self.fixture.configure()
        queued = self._scheduled()[0]
        cancelled = False

        def fetch_and_cancel(client, url, deadline):
            nonlocal cancelled
            if not cancelled:
                request_cancel(self.store, queued["operation_id"])
                cancelled = True
            return self.fixture.fetch(url, deadline)

        with patch.dict(os.environ, self.fixture.env), patch.object(PublicCompanyClient, "_request_once", fetch_and_cancel):
            result = execute_operation(queued["operation_id"])
        self.assertEqual(result["status"], "CANCELLED", result)
        monitor = self.fixture.monitor()
        self.assertEqual(monitor["failure_count"], 0)
        self.assertIsNone(monitor["completed_operation_id"])
        self.assertEqual(self.store.count("source_quality_observations"), 0)

    def test_source_retargeted_after_last_get_fails_import_guard_without_receipt(self):
        self.fixture.configure()
        queued = self._scheduled()[0]
        from services.discovery.company_feed import import_company_feed_jobs as original_import

        def retarget_then_import(*args, **kwargs):
            with self.store.engine.begin() as connection:
                connection.execute(update(m.company_sources).where(m.company_sources.c.id == self.fixture.source["id"])
                                   .values(endpoint="https://employer.example/retargeted.json"))
            return original_import(*args, **kwargs)

        with patch.dict(os.environ, self.fixture.env), patch.object(PublicCompanyClient, "_request_once", self.fixture.fetch), \
                patch("services.discovery.company_feed.import_company_feed_jobs", side_effect=retarget_then_import):
            result = execute_operation(queued["operation_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error_category"], "COMPANY_FEED_SOURCE_MISMATCH")
        self.assertEqual(self.store.count("source_quality_observations"), 0)
        self.assertEqual(self.store.count("canonical_jobs"), 0)


if __name__ == "__main__":
    unittest.main()
