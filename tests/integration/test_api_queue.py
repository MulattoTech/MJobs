"""API and outbox tests exercise persistence with the queue unavailable."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app, create_from_env
from cli.compose_recovery import collect_public_job_inventory, compare_public_job_inventory
from packages.domain.operation_types import REQUIRED_OPERATION_TYPES
from packages.persistence import models as m
from packages.persistence.store import Store, now
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation, dispatch_pending
from fixtures.mock_server import fixture_server
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.documents.history import application_documents
from services.documents.tailor import target_fingerprint
from services.career_profile.story_bank import create_story, review_story
from workers.tasks import execute_operation


class ApiQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_queue_disconnect_preserves_one_durable_operation(self):
        first = enqueue_operation(self.store, "DISCOVER", "fixture", {})
        second = enqueue_operation(self.store, "DISCOVER", "fixture", {})
        self.assertEqual(first, second)
        result = dispatch_pending(self.store, "redis://127.0.0.1:1/0")
        self.assertEqual(result["status"], "QUEUE_UNAVAILABLE")
        self.assertEqual(self.store.count("worker_operations"), 1)

    def test_invalid_operation_types_are_redacted_and_never_persisted(self):
        for value in ("search_source", "UNKNOWN_PRIVATE_SENTINEL", None, 7, True, [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "^OPERATION_TYPE_INVALID$") as raised:
                    enqueue_operation(self.store, value, "fixture", {})
                self.assertNotIn("UNKNOWN_PRIVATE_SENTINEL", repr(raised.exception))
                self.assertEqual(self.store.count("worker_operations"), 0)

    def test_canonical_types_without_handlers_use_stable_unsupported_path(self):
        for operation_type in sorted(REQUIRED_OPERATION_TYPES):
            with self.subTest(operation_type=operation_type):
                with self.assertRaisesRegex(ValueError, "^Unsupported operation kind$"):
                    enqueue_operation(self.store, operation_type, "fixture", {})
                self.assertEqual(self.store.count("worker_operations"), 0)

    def test_persisted_invalid_type_is_dead_lettered_before_queue_handler_lookup(self):
        operation_id = "persisted-invalid-operation-type"
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert().values(
                id=operation_id,
                kind="UNKNOWN_PRIVATE_SENTINEL",
                entity_id="fixture",
                params={},
                state="QUEUED",
                error=None,
                updated_at=now(),
            ))
        redis = Mock()
        redis.ping.return_value = True
        with patch("workers.dispatch.Redis.from_url", return_value=redis), \
                patch("workers.dispatch.Job.fetch") as fetch, \
                patch("workers.dispatch.Queue") as queue:
            self.assertEqual(
                dispatch_pending(self.store, "redis://synthetic/0"),
                {"status": "DISPATCHED", "count": 0},
            )
        fetch.assert_not_called()
        queue.assert_not_called()
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id)).mappings().one()
            action = connection.execute(select(m.audit.c.action).where(
                m.audit.c.entity_id == operation_id)).scalar_one()
        self.assertEqual(row["state"], "DEAD_LETTERED")
        self.assertEqual(row["error"], "OPERATION_TYPE_INVALID")
        self.assertEqual(row["current_step"], "FINALIZING")
        self.assertIsNotNone(row["completed_at"])
        self.assertEqual(row["recovery_metadata"], {"action": "MANUAL_REVIEW"})
        self.assertEqual(action, "OPERATION_DEAD_LETTERED")

    def test_dispatch_serializes_registry_selector_for_real_task_entrypoint(self):
        from rq.exceptions import NoSuchJobError

        import workers.tasks as tasks
        from services.applications.provider_registry import ApplicationAdapterRegistry
        from services.discovery.sources.collect import configure_source
        from services.discovery.sources.contract import (
            CollectionStrategy,
            SourceCapabilities,
            SourceCapability,
        )
        from services.discovery.sources.registry import SourceAdapterRegistry

        class SourceAdapter:
            def capabilities(self):
                return SourceCapabilities(
                    CollectionStrategy.API,
                    frozenset({SourceCapability.DISCOVERY}),
                )

        module_name = "tests.synthetic_extension_registry"
        extension_module = ModuleType(module_name)
        extension_module.SOURCE_ADAPTER_REGISTRY = SourceAdapterRegistry([
            ("fixture_source", lambda _source, **_kwargs: SourceAdapter()),
        ])
        extension_module.APPLICATION_ADAPTER_REGISTRY = ApplicationAdapterRegistry([
            ("fixture_app", lambda: object()),
        ])
        sys.modules[module_name] = extension_module

        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(PROFILE)
        source_id = configure_source(
            self.store, "fixture-tenant", provider="fixture_source",
            registry=extension_module.SOURCE_ADAPTER_REGISTRY,
        )
        discovery_id = enqueue_operation(
            self.store, "DISCOVER", "fixture",
            {"source_id": source_id, "profile_ids": ["fixture"]},
        )
        review_id = enqueue_operation(
            self.store, "APPLICATION_FORM_REVIEW", "application-1",
            {"route_url": "https://fixture.invalid/jobs/1"},
        )
        deliveries = []

        class SyntheticQueue:
            def __init__(self, name, connection=None):
                self.name = name

            def enqueue(self, function, *args, **kwargs):
                options = {
                    key: kwargs.pop(key)
                    for key in ("job_id", "job_timeout", "result_ttl", "failure_ttl")
                }
                deliveries.append((function, args, kwargs, options))

        redis = Mock()
        redis.ping.return_value = True
        observed = {}

        def collect(_store, _source_id, _profile_id, **kwargs):
            observed["source_registry"] = kwargs.get("registry")
            return {"jobs_seen": 0}

        def review(_store, _operation, **kwargs):
            observed["application_registry"] = kwargs.get("registry")
            return {"status": "ANALYZED"}

        environment = {
            "MJOBS_DATABASE_URL": self.store.url,
            "MJOBS_OPERATION_MODE": "PERSONAL",
            "MJOBS_OUTPUT_DIR": str(self.root),
        }
        try:
            with patch("workers.dispatch.Redis.from_url", return_value=redis), \
                    patch("workers.dispatch.Job.fetch", side_effect=NoSuchJobError), \
                    patch("workers.dispatch.Queue", SyntheticQueue), \
                    patch.dict("os.environ", environment), \
                    patch.object(tasks, "collect_source", side_effect=collect), \
                    patch.object(tasks, "recalculate", return_value=None), \
                    patch.object(tasks, "handle_form_review", side_effect=review), \
                    patch.object(tasks, "log_operation_finished", return_value=None):
                result = dispatch_pending(
                    self.store, "redis://synthetic/0", registry_module=module_name,
                )
                self.assertEqual(result, {"status": "DISPATCHED", "count": 2})
                for function, args, kwargs, options in deliveries:
                    self.assertEqual(function, "workers.tasks.execute_operation")
                    self.assertIn(args[0], {discovery_id, review_id})
                    self.assertEqual(kwargs, {"registry_module": module_name})
                    self.assertEqual(options["job_id"], args[0])
                    module, name = function.rsplit(".", 1)
                    getattr(import_module(module), name)(*args, **kwargs)
        finally:
            sys.modules.pop(module_name, None)

        self.assertEqual(observed, {
            "source_registry": extension_module.SOURCE_ADAPTER_REGISTRY,
            "application_registry": extension_module.APPLICATION_ADAPTER_REGISTRY,
        })

    def test_duplicate_enqueue_does_not_reopen_a_dead_letter(self):
        from sqlalchemy import select, update
        from packages.persistence import models as m

        identifier = enqueue_operation(self.store, "DISCOVER", "fixture", {})
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == identifier).values(
                state="DEAD_LETTERED", error="RETRY_LIMIT", attempt_count=3))

        self.assertEqual(enqueue_operation(self.store, "DISCOVER", "fixture", {}), identifier)
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(m.operations.c.id == identifier)).mappings().one()
        self.assertEqual(row["state"], "DEAD_LETTERED")
        self.assertEqual(row["error"], "RETRY_LIMIT")
        self.assertEqual(row["attempt_count"], 3)

    def test_concurrent_requests_share_one_durable_operation(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            identifiers = list(pool.map(lambda _: enqueue_operation(self.store, "DISCOVER", "fixture", {}), range(2)))
        self.assertEqual(identifiers[0], identifiers[1])
        self.assertEqual(self.store.count("worker_operations"), 1)

    def test_task_key_replays_without_replacing_original_operation_params(self):
        task_key = "collect:source-a:2026-10-08T12:00:00+00:00"
        first = enqueue_operation(self.store, "DISCOVER", "fixture", {
            "source_id": "source-a", "scheduled_for": "2026-10-08T12:00:00+00:00",
            "scheduler_observation": "first",
        }, task_key=task_key)
        replay = enqueue_operation(self.store, "DISCOVER", "fixture", {
            "scheduler_observation": "replayed-after-restart",
            "scheduled_for": "2026-10-08T12:00:00+00:00", "source_id": "source-a",
        }, task_key=task_key)

        self.assertEqual(replay, first)
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(
                m.operations.c.id == first)).mappings().one()
        self.assertEqual(row["params"]["scheduler_observation"], "first")
        self.assertEqual(self.store.count("worker_operations"), 1)

    def test_task_key_reuse_for_another_entity_fails_closed(self):
        task_key = "collect:source-a:2026-10-08T12:00:00+00:00"
        identifier = enqueue_operation(
            self.store, "DISCOVER", "profile-a", {"source_id": "source-a"},
            task_key=task_key,
        )

        with self.assertRaisesRegex(ValueError, "TASK_IDENTITY_CONFLICT"):
            enqueue_operation(
                self.store, "DISCOVER", "profile-b", {"source_id": "source-a"},
                task_key=task_key,
            )
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(
                m.operations.c.id == identifier)).mappings().one()
        self.assertEqual(row["entity_id"], "profile-a")
        self.assertEqual(self.store.count("worker_operations"), 1)

    def test_api_saves_explicit_facts_and_reports_durable_queueing(self):
        with TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root, "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as client:
            response = client.put("/profiles", json=PROFILE)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["version"])
            queued = client.post("/discovery", json={"profile_id": "fixture"})
            self.assertEqual(queued.status_code, 202)
            operation = client.get("/operations/" + queued.json()["operation_id"])
            self.assertEqual(operation.json()["state"], "QUEUED")
            self.assertEqual(queued.json()["state"], "QUEUED")
            self.assertEqual(client.get("/readiness").status_code, 503)
            self.assertEqual(client.get("/settings").json()["external_submission_enabled"], False)

    def test_untrusted_browser_origin_and_string_verification_are_rejected(self):
        with self.assertRaises(ValueError):
            create_app(self.store, "https://external.example", self.root, operation_mode="FIXTURE")
        with TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root, operation_mode="FIXTURE")) as client:
            invalid = dict(PROFILE, facts=[dict(PROFILE["facts"][0], verified="yes")])
            self.assertEqual(client.put("/profiles", json=invalid).status_code, 422)

    def test_remote_ok_alias_cannot_bypass_global_source_tenant_policy(self):
        with TestClient(create_app(
                self.store, "http://127.0.0.1:8090", self.root,
                operation_mode="PERSONAL")) as client:
            response = client.put("/sources", json={
                "provider": "remote_ok",
                "tenant": "non-default",
            })

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.store.count("job_sources"), 0)

    def test_personal_mode_rejects_fixture_actions_and_keeps_public_collection(self):
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(PROFILE)
        with TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root)) as client:
            self.assertEqual(client.get("/settings").json()["operation_mode"], "PERSONAL")
            self.assertNotIn("mock", client.get("/readiness").json()["checks"])
            self.assertEqual(client.post("/discovery", json={"profile_id": "fixture"}).status_code, 409)
            self.assertEqual(client.post("/profiles/fixture/applications/nonexistent/mock-run",
                                         json={"submit": True}).status_code, 409)
            source = client.put("/sources", json={"tenant": "example"})
            self.assertEqual(source.status_code, 200)
            collect = client.post("/sources/" + source.json()["id"] + "/collect",
                                  json={"profile_id": "fixture"})
            self.assertEqual(collect.status_code, 202)
        self.assertEqual(self.store.count("worker_operations"), 1)
        with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url, MJOBS_MOCK_ORIGIN="http://127.0.0.1:8090",
                        MJOBS_OUTPUT_DIR=str(self.root), MJOBS_OPERATION_MODE="PERSONAL"), \
                patch("workers.tasks.collect_source") as collect_source:
            self.assertEqual(execute_operation(collect.json()["operation_id"])["status"], "COMPLETED")
            collect_source.assert_called_once()

    def test_personal_runtime_rejects_fixture_database(self):
        self.store.save_profile(PROFILE)
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url,
                        MJOBS_MOCK_ORIGIN="http://mock:8090", MJOBS_OUTPUT_DIR=str(self.root),
                        MJOBS_REDIS_URL="redis://127.0.0.1:1/0", MJOBS_OPERATION_MODE="PERSONAL"):
            with self.assertRaisesRegex(RuntimeError, "separate Compose project"):
                create_from_env()

    def test_readiness_requires_the_application_queue_worker(self):
        worker = Mock()
        worker.get_state.return_value = "idle"
        worker.queue_names.return_value = ["unrelated"]
        with fixture_server() as origin, patch("apps.api.main.Redis") as redis, patch("apps.api.main.Worker.all", return_value=[worker]):
            redis.from_url.return_value.ping.return_value = True
            with TestClient(create_app(self.store, origin, self.root, operation_mode="FIXTURE")) as client:
                response = client.get("/readiness")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["checks"]["worker"])

    def test_readiness_degrades_on_invalid_worker_metadata(self):
        with fixture_server() as origin, patch("apps.api.main.Redis") as redis, patch("apps.api.main.Worker.all", side_effect=ValueError("Invalid worker metadata")):
            redis.from_url.return_value.ping.return_value = True
            with TestClient(create_app(self.store, origin, self.root, operation_mode="FIXTURE")) as client:
                response = client.get("/readiness")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["checks"]["worker"])

    def test_preparation_is_durable_background_work_and_cancel_is_visible(self):
        with fixture_server() as origin, TestClient(create_app(self.store, origin, self.root, operation_mode="FIXTURE")) as client:
            client.put("/profiles", json=PROFILE).raise_for_status()
            discover(self.store, origin, "fixture")
            job = self.store.jobs()[0]
            response = client.post("/applications", json={"profile_id": "fixture", "job_id": job["id"]})
            self.assertEqual(response.status_code, 202)
            queued = response.json()
            self.assertEqual(queued["state"], "QUEUED")
            self.assertEqual(self.store.count("applications"), 0)
            with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url, MJOBS_MOCK_ORIGIN=origin, MJOBS_OUTPUT_DIR=str(self.root), MJOBS_OPERATION_MODE="FIXTURE"):
                self.assertEqual(execute_operation(queued["operation_id"])["status"], "COMPLETED")
            self.assertEqual(client.get("/profiles/fixture/applications/" + queued["application_id"]).json()["state"], "PREPARED")
            discovery = client.post("/discovery", json={"profile_id": "fixture"}).json()
            cancelled = client.post("/operations/" + discovery["operation_id"] + "/cancel")
            self.assertEqual(cancelled.status_code, 202)
            self.assertEqual(cancelled.json()["state"], "CANCELLED")

    def test_selected_career_story_survives_durable_preparation_and_document_readback(self):
        with fixture_server() as origin, TestClient(create_app(
                self.store, origin, self.root, operation_mode="FIXTURE")) as client:
            client.put("/profiles", json=PROFILE).raise_for_status()
            discover(self.store, origin, "fixture")
            job = self.store.jobs()[0]
            story = create_story(self.store, "fixture", {
                "kind": "ACCOMPLISHMENT",
                "title": "Reviewed Python accomplishment",
                "text": "User-authored evidence: I improved a synthetic Python support workflow.",
                "fact_ids": ["python"],
                "tags": ["support"],
                "allowed_uses": ["COVER_LETTER", "RESUME"],
            })
            story = review_story(
                self.store, "fixture", story["story_id"], True, None,
                expected_revision=story["revision"],
            )
            profile = self.store.profile("fixture")
            fingerprint = target_fingerprint(job)
            queued = client.post("/applications", json={
                "profile_id": "fixture",
                "job_id": job["id"],
                "expected_profile_version": profile["version"],
                "expected_job_fingerprint": fingerprint,
                "cover_letter_story_ids": [story["story_id"]],
                "resume_story_ids": [story["story_id"]],
            })
            self.assertEqual(queued.status_code, 202, queued.text)
            operation_id = queued.json()["operation_id"]
            operation = client.get("/operations/" + operation_id).json()
            self.assertEqual(operation["params"]["cover_letter_story_ids"], [story["story_id"]])
            self.assertEqual(operation["params"]["resume_story_ids"], [story["story_id"]])
            self.assertEqual(operation["params"]["cover_letter_story_job_fingerprint"], fingerprint)
            self.assertEqual(operation["params"]["resume_story_job_fingerprint"], fingerprint)
            with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url,
                            MJOBS_MOCK_ORIGIN=origin, MJOBS_OUTPUT_DIR=str(self.root),
                            MJOBS_OPERATION_MODE="FIXTURE"):
                self.assertEqual(execute_operation(operation_id)["status"], "COMPLETED")
            docs = client.get(
                f"/profiles/fixture/applications/{queued.json()['application_id']}/documents",
            ).json()["documents"]
            letter = next(row for row in docs if row["kind"] == "cover-letter")
            resume = next(row for row in docs if row["kind"] == "resume")
            self.assertIsNone(letter["preview_error"])
            self.assertIsNone(resume["preview_error"])
            self.assertIn(story["text"], letter["preview_text"])
            self.assertIn(story["text"], resume["preview_text"])
            self.assertEqual(letter["generation_context"]["story_refs"][0]["story_id"],
                             story["story_id"])
            self.assertEqual(resume["generation_context"]["story_refs"][0]["story_id"],
                             story["story_id"])
            download_path = f"/profiles/fixture/documents/{resume['id']}/txt"
            downloaded = client.get(download_path)
            self.assertEqual(downloaded.status_code, 200, downloaded.text)
            self.assertIn(story["text"], downloaded.text)
            with self.store.engine.begin() as connection:
                connection.execute(m.career_stories.update().where(
                    m.career_stories.c.id == story["story_id"],
                ).values(text="Changed story source must fail closed."))
            self.assertEqual(client.get(download_path).status_code, 409)

    def test_ui_job_inventory_survives_worker_and_store_reopen_and_rejects_source_copy_change(self):
        """The public UI inventory stays stable through durable discovery and reopen."""
        with fixture_server() as origin:
            self.store.bind_workspace_mode("FIXTURE")
            store_url = self.store.url
            with TestClient(create_app(
                    self.store, origin, self.root, operation_mode="FIXTURE")) as client:
                self.assertEqual(client.put("/profiles", json=PROFILE).status_code, 200)
                queued = client.post("/discovery", json={"profile_id": "fixture"})
                self.assertEqual(queued.status_code, 202, queued.text)
                operation_id = queued.json()["operation_id"]
                with patch.dict("os.environ", MJOBS_DATABASE_URL=store_url,
                                MJOBS_MOCK_ORIGIN=origin, MJOBS_OUTPUT_DIR=str(self.root),
                                MJOBS_OPERATION_MODE="FIXTURE"):
                    self.assertEqual(execute_operation(operation_id)["status"], "COMPLETED")

                def public_json(path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200, response.text)
                    return response.json()

                ui_baseline = collect_public_job_inventory(public_json, "fixture")
                self.assertEqual(ui_baseline["job_count"], 2)
                self.assertEqual(ui_baseline["source_copy_count"], 4)

            self.store.engine.dispose()
            self.store = Store(store_url)
            with TestClient(create_app(
                    self.store, origin, self.root, operation_mode="FIXTURE")) as reopened_client:
                def reopened_public_json(path):
                    response = reopened_client.get(path)
                    self.assertEqual(response.status_code, 200, response.text)
                    return response.json()

                after_reopen = collect_public_job_inventory(reopened_public_json, "fixture")
                result = compare_public_job_inventory(
                    {"job_inventory": ui_baseline}, ui_baseline, after_reopen)
                self.assertEqual(result["result"], "PASS")
                self.assertEqual(result["sha256"], ui_baseline["sha256"])

                # A persisted source-copy payload change must be visible through the
                # reopened public API and rejected against the original UI baseline.
                with self.store.engine.begin() as connection:
                    posting = connection.execute(
                        select(m.postings).order_by(m.postings.c.id).limit(1)
                    ).mappings().one()
                    changed_raw = dict(posting["raw"])
                    changed_raw["criterion_55_fixture_mutation"] = "changed"
                    connection.execute(m.postings.update().where(
                        m.postings.c.id == posting["id"],
                    ).values(raw=changed_raw))

                changed_inventory = collect_public_job_inventory(
                    reopened_public_json, "fixture")
                with self.assertRaisesRegex(ValueError, "Public job/source inventory changed"):
                    compare_public_job_inventory(
                        {"job_inventory": ui_baseline}, ui_baseline, changed_inventory)


if __name__ == "__main__":
    unittest.main()
