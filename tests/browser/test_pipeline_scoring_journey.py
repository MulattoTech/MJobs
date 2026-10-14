"""Connected Pipeline scoring -> documents -> stop-before-submit acceptance."""
from copy import deepcopy
import hashlib

from playwright.sync_api import expect
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from tests.browser.test_mock_run_controls import MockRunBrowserHarness
from tests.integration.test_spine import PROFILE


class PipelineScoringJourneyTests(MockRunBrowserHarness):
    def seed_workflow(self):
        discover(self.store, self.fixture, "fixture")
        self.job = self.store.jobs("fixture")[0]
        previous = self.store.profile("fixture")["version"]
        changed = deepcopy(PROFILE)
        changed["name"] = "Updated Synthetic Applicant"
        self.store.save_profile(changed, expected_version=previous)
        self.store.save_profile({**deepcopy(PROFILE), "id": "other-profile", "name": "Other Synthetic Profile"})
        self.app_id = None

    def open_initial_view(self):
        self.page.reload(wait_until="domcontentloaded")
        self.page.get_by_role("button", name="Pipeline", exact=True).click()
        self.workbench = self.page.get_by_role("region", name="Pipeline workbench")
        expect(self.workbench).to_contain_text("Needs scoring")

    def test_lost_scoring_acknowledgement_replays_after_reload_without_requeue(self):
        requests = []
        def lose_ack(route):
            requests.append(route.request.post_data_json)
            response = route.fetch()
            self.assertEqual(response.status, 202)
            route.abort()
        self.page.route(self.origin + "/matches", lose_ack, times=1)
        button = self.workbench.get_by_role("button", name="Refresh all scores for this profile", exact=True)
        button.click()
        expect(self.workbench).to_contain_text("Scores are refreshing")
        self.page.reload(wait_until="domcontentloaded")
        self.page.get_by_role("button", name="Pipeline", exact=True).click()
        expect(button).to_be_disabled()
        with self.store.engine.connect() as connection:
            operations = connection.execute(select(m.operations).where(m.operations.c.kind == "DISCOVER")).mappings().all()
        self.assertEqual(len(operations), 1)
        self.run_worker(operations[0]["id"], refresh_panel=False)
        expect(self.workbench).to_contain_text("Score refresh completed")
        with self.page.expect_response(lambda response: response.request.method == "POST"
                                       and response.url == self.origin + "/matches") as replay:
            button.click()
        self.assertEqual(replay.value.request.post_data_json["request_id"], requests[0]["request_id"])
        self.assertEqual(replay.value.json(), {"operation_id": operations[0]["id"], "state": "COMPLETED"})
        expect(button).to_be_enabled()
        with self.page.expect_response(lambda response: response.request.method == "POST"
                                       and response.url == self.origin + "/matches") as fresh:
            button.click()
        self.assertNotEqual(fresh.value.json()["operation_id"], operations[0]["id"])
        # A terminal failure must not be represented as successful score refresh.
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == fresh.value.json()["operation_id"]).values(state="FAILED"))
        expect(self.workbench).to_contain_text("Score refresh did not complete")
        expect(button).to_be_enabled()
        self.assertEqual(self.counts(), {"review": 0, "submit": 0})

    def test_storage_failure_prevents_an_unrecoverable_scoring_request(self):
        requests = []
        self.page.on("request", lambda request: requests.append(request.url)
                     if request.method == "POST" and request.url.endswith("/matches") else None)
        self.page.evaluate("""() => {
            const original = Storage.prototype.setItem;
            Storage.prototype.setItem = function(key, value) {
                if (key.startsWith('mjobs-pipeline-score-request:')) throw new Error('synthetic storage failure');
                return original.call(this, key, value);
            };
        }""")
        self.workbench.get_by_role("button", name="Refresh all scores for this profile", exact=True).click()
        expect(self.workbench).to_contain_text("Browser storage is unavailable")
        self.assertEqual(requests, [])
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.operations.c.id)).all(), [])

    def test_delayed_score_acknowledgement_cannot_cross_profile_boundary(self):
        held = []
        def delay_ack(route):
            held.append((route, route.fetch()))
        self.page.route(self.origin + "/matches", delay_ack, times=1)
        self.workbench.get_by_role("button", name="Refresh all scores for this profile", exact=True).click()
        expect(self.workbench.get_by_role("button", name="Refresh all scores for this profile", exact=True)).to_be_disabled()
        self.page.get_by_label("Active profile", exact=True).select_option("other-profile")
        expect(self.workbench).to_contain_text("No jobs are available")
        self.assertEqual(len(held), 1)
        held[0][0].fulfill(response=held[0][1])
        expect(self.workbench).not_to_contain_text("Scores are refreshing")
        expect(self.workbench).not_to_contain_text(self.job["title"])
        self.page.get_by_label("Active profile", exact=True).select_option("fixture")
        expect(self.workbench).to_contain_text("Scores are refreshing")
        # Failure to re-establish saved progress removes actionable readiness.
        self.page.route(self.origin + "/profiles/fixture/pipeline-workbench", lambda route: route.abort())
        expect(self.workbench).to_contain_text("could not be loaded")
        expect(self.workbench.get_by_role("button", name="Prepare application documents", exact=True)).to_have_count(0)
        self.assertEqual(self.counts(), {"review": 0, "submit": 0})

    def test_stale_discovery_to_durable_documents_and_reviewed_dry_run(self):
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.applications.c.id)).all(), [])
            self.assertEqual(connection.execute(select(m.documents.c.id)).all(), [])
        collection_counts = self.counts()
        with self.page.expect_response(lambda response: response.request.method == "POST"
                                       and response.url == self.origin + "/matches") as admission:
            self.workbench.get_by_role("button", name="Refresh all scores for this profile", exact=True).click()
        self.assertEqual(admission.value.status, 202, admission.value.text())
        scoring_id = admission.value.json()["operation_id"]
        with self.store.engine.connect() as connection:
            saved = connection.execute(select(m.operations).where(m.operations.c.id == scoring_id)).mappings().one()
            self.assertEqual(saved["entity_id"], "fixture")
            self.assertIs(saved["params"]["rescore_only"], True)
        expect(self.workbench).to_contain_text("Scores are refreshing")
        self.page.reload(wait_until="domcontentloaded")
        self.page.get_by_role("button", name="Pipeline", exact=True).click()
        expect(self.workbench).to_contain_text("Scores are refreshing")
        expect(self.workbench.get_by_role("button", name="Refresh all scores for this profile", exact=True)).to_be_disabled()
        expect(self.workbench.get_by_role("button", name="Prepare application documents", exact=True)).to_have_count(0)
        self.run_worker(scoring_id, refresh_panel=False)
        expect(self.workbench).to_contain_text("Score refresh completed")
        self.assertEqual(self.counts(), collection_counts, "Local rescore contacted the fixture source")
        row = self.workbench.locator("li.fact").filter(
            has=self.page.get_by_role("heading", name=self.job["title"], exact=True))
        with self.page.expect_response(lambda response: response.request.method == "POST"
                                       and "/pipeline-workbench/" in response.url and response.url.endswith("/prepare")) as preparing:
            row.get_by_role("button", name="Prepare application documents", exact=True).click()
        self.assertEqual(preparing.value.status, 202, preparing.value.text())
        self.run_worker(preparing.value.json()["operation_id"], refresh_panel=False)
        expect(row.get_by_role("button", name="Open existing application", exact=True)).to_be_visible()
        with self.store.engine.connect() as connection:
            self.app_id = connection.execute(select(m.applications.c.id).where(
                m.applications.c.job_id == self.job["id"], m.applications.c.profile_id == "fixture")).scalar_one()
            docs = connection.execute(select(m.documents).where(m.documents.c.application_id == self.app_id)).mappings().all()
            self.assertEqual({doc["kind"] for doc in docs}, {"resume", "cover-letter"})
            self.assertTrue(all(doc["sha256"] for doc in docs))
            self.assertTrue(all(doc["profile_version"] == self.store.profile("fixture")["version"] for doc in docs))
        row.get_by_role("button", name="Open existing application", exact=True).click()
        readback = self.context.request.get(self.origin + f"/profiles/fixture/applications/{self.app_id}/documents")
        self.assertTrue(readback.ok)
        for document in readback.json()["documents"]:
            download = self.context.request.get(self.origin + f"/profiles/fixture/documents/{document['id']}/txt")
            self.assertTrue(download.ok)
            saved_document = next(doc for doc in docs if doc["id"] == document["id"])
            self.assertIn(hashlib.sha256(download.body()).hexdigest(), saved_document["sha256"].values())
        self.panel = self.page.get_by_role("region", name="Recoverable mock run", exact=True)
        expect(self.panel).to_be_visible()
        self.panel.get_by_role("button", name="Run dry-run", exact=True).click()
        expect(self.panel).to_contain_text("QUEUED")
        self.assertEqual(len(self.posts), 1)
        self.assertIs(self.posts[0]["submit"], False)
        self.run_worker(self.operations()[0]["id"])
        expect(self.page.get_by_role("status", name="Application state", exact=True)).to_have_text("DRY_RUN_COMPLETE")
        self.open_application()
        expect(self.panel).to_contain_text("COMPLETED")
        fresh = Store(self.store.url)
        try:
            application = fresh.application(self.app_id)
            self.assertEqual(application["state"], "DRY_RUN_COMPLETE")
            self.assertFalse(application["confirmation"])
        finally:
            fresh.engine.dispose()
        self.assertEqual(self.counts(), {"review": 1, "submit": 0})
        self.assertFalse(self.external)
