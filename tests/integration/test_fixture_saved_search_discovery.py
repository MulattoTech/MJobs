"""Selected saved searches must control fixture discovery, not just its history."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.search_profiles import get_search_profile, save_search_profile
from services.discovery.search_history import list_search_history
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class FixtureSavedSearchDiscoveryTests(unittest.TestCase):
    def test_queue_edit_is_used_but_collection_edit_cannot_change_the_snapshot(self):
        from services.discovery.pipeline import discover

        with tempfile.TemporaryDirectory(prefix="mjobs-fixture-search-snapshot-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'fixture.db').as_posix()}")
            try:
                store.migrate()
                store.bind_workspace_mode("FIXTURE")
                store.save_profile(dict(PROFILE))
                search = {"id": "selected", "profile_id": "fixture", "name": "Selected",
                          "include_titles": ["Astronaut"], "enabled": True}
                save_search_profile(store, search)
                with fixture_server() as origin:
                    app = create_app(store, origin, root, "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
                    with TestClient(app) as client:
                        queued = client.post("/discovery", json={
                            "profile_id": "fixture", "search_profile_id": "selected",
                            "request_id": "fixture-search-snapshot",
                        })
                    self.assertEqual(queued.status_code, 202, queued.text)
                    save_search_profile(store, {**search, "include_titles": ["Python Developer"]})
                    expected = get_search_profile(store, "selected")["criteria"]

                    def edit_during_capture(*args, **kwargs):
                        save_search_profile(store, {**search, "include_titles": ["Data Engineer"]})
                        return discover(*args, **kwargs)

                    with patch("workers.tasks.discover", side_effect=edit_during_capture), patch.dict(os.environ, {
                        "MJOBS_DATABASE_URL": store.url, "MJOBS_MOCK_ORIGIN": origin,
                        "MJOBS_OUTPUT_DIR": str(root), "MJOBS_OPERATION_MODE": "FIXTURE",
                    }):
                        result = execute_operation(queued.json()["operation_id"])
                    self.assertEqual(result["status"], "COMPLETED", result)
                reopened = Store(store.url)
                try:
                    with reopened.engine.connect() as connection:
                        titles = connection.execute(select(m.jobs.c.title).join(
                            m.matches, m.matches.c.job_id == m.jobs.c.id)).scalars().all()
                    self.assertEqual(titles, ["Python Developer"])
                    history = list_search_history(reopened, "fixture")["runs"][0]
                    self.assertEqual(history["filters"], expected)
                    self.assertEqual(history["results"], 4)
                    self.assertEqual(get_search_profile(reopened, "selected")["criteria"]["include_titles"],
                                     ["Data Engineer"])
                finally:
                    reopened.engine.dispose()
            finally:
                store.engine.dispose()

    def test_search_invalidated_after_queue_blocks_before_capture(self):
        for invalidation, error in (("disabled", "FIXTURE_SEARCH_DISABLED"),
                                    ("deleted", "FIXTURE_SEARCH_NOT_FOUND"),
                                    ("scoped", "FIXTURE_SEARCH_SOURCE_SCOPE"),
                                    ("descriptors", "FIXTURE_SEARCH_DESCRIPTORS_UNSUPPORTED")):
            with self.subTest(invalidation=invalidation), tempfile.TemporaryDirectory(
                    prefix="mjobs-fixture-search-stale-") as directory:
                root = Path(directory)
                store = Store(f"sqlite:///{(root / 'fixture.db').as_posix()}")
                try:
                    store.migrate()
                    store.bind_workspace_mode("FIXTURE")
                    store.save_profile(dict(PROFILE))
                    search = {"id": "selected", "profile_id": "fixture", "name": "Selected",
                              "include_titles": ["Python Developer"], "enabled": True}
                    save_search_profile(store, search)
                    app = create_app(store, "http://127.0.0.1:1", root,
                                     "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
                    with TestClient(app) as client:
                        queued = client.post("/discovery", json={
                            "profile_id": "fixture", "search_profile_id": "selected",
                            "request_id": "invalidated-" + invalidation,
                        })
                    self.assertEqual(queued.status_code, 202, queued.text)
                    if invalidation == "deleted":
                        with store.engine.begin() as connection:
                            connection.execute(m.search_profiles.delete().where(m.search_profiles.c.id == "selected"))
                    elif invalidation == "disabled":
                        save_search_profile(store, {**search, "enabled": False})
                    elif invalidation == "scoped":
                        source_id = configure_source(store, "incompatible-fixture-board")
                        save_search_profile(store, {**search, "source_ids": [source_id]})
                    else:
                        with store.engine.begin() as connection:
                            operation = connection.execute(select(m.operations).where(
                                m.operations.c.id == queued.json()["operation_id"])).mappings().one()
                            connection.execute(m.operations.update().where(
                                m.operations.c.id == operation["id"]).values(
                                    params={**operation["params"], "filters": {"remote": "REQUIRED"}}))
                    with patch("workers.tasks.discover") as capture, patch.dict(os.environ, {
                        "MJOBS_DATABASE_URL": store.url, "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
                        "MJOBS_OUTPUT_DIR": str(root), "MJOBS_OPERATION_MODE": "FIXTURE",
                    }):
                        result = execute_operation(queued.json()["operation_id"])
                    self.assertEqual(result["status"], "BLOCKED", result)
                    capture.assert_not_called()
                    with store.engine.connect() as connection:
                        operation = connection.execute(select(m.operations).where(
                            m.operations.c.id == queued.json()["operation_id"])).mappings().one()
                    self.assertEqual(operation["error"], error)
                    self.assertEqual(store.count("job_postings"), 0)
                finally:
                    store.engine.dispose()

    def test_invalid_selected_search_never_queues(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-fixture-search-validation-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'fixture.db').as_posix()}")
            try:
                store.migrate()
                store.bind_workspace_mode("FIXTURE")
                store.save_profile(dict(PROFILE))
                store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture"})
                source_id = configure_source(store, "fixture-search-public-board")
                searches = [
                    ("disabled", "fixture", False, [], 409),
                    ("scoped", "fixture", True, [source_id], 409),
                    ("other-search", "other", True, [], 404),
                ]
                for search_id, profile_id, enabled, sources, _ in searches:
                    save_search_profile(store, {
                        "id": search_id, "profile_id": profile_id, "name": search_id,
                        "enabled": enabled, "source_ids": sources,
                    })
                save_search_profile(store, {"id": "valid", "profile_id": "fixture",
                                            "name": "Valid", "enabled": True})
                app = create_app(store, "http://127.0.0.1:1", root,
                                 "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
                with TestClient(app) as client:
                    for search_id, _, _, _, expected in searches + [("missing", "fixture", True, [], 404)]:
                        with self.subTest(search=search_id):
                            response = client.post("/discovery", json={
                                "profile_id": "fixture", "search_profile_id": search_id,
                                "request_id": "rejected-" + search_id,
                            })
                            self.assertEqual(response.status_code, expected, response.text)
                    for descriptors in ({"query": "Python"}, {"filters": {"remote": "REQUIRED"}}):
                        response = client.post("/discovery", json={
                            "profile_id": "fixture", "search_profile_id": "valid",
                            "request_id": "unsupported-descriptors", **descriptors,
                        })
                        self.assertEqual(response.status_code, 409, response.text)
                with store.engine.connect() as connection:
                    self.assertEqual(connection.execute(select(m.operations)).all(), [])
                self.assertEqual(store.count("job_postings"), 0)
            finally:
                store.engine.dispose()

    def test_selected_search_filters_scoring_without_discarding_captured_jobs(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-fixture-search-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'fixture.db').as_posix()}")
            try:
                store.migrate()
                store.bind_workspace_mode("FIXTURE")
                store.save_profile(dict(PROFILE))
                store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                    "email": "other@example.test"})
                save_search_profile(store, {
                    "id": "fixture-selected", "profile_id": "fixture", "name": "No matching jobs",
                    "include_titles": ["Astronaut"], "enabled": True,
                })
                with fixture_server() as origin:
                    app = create_app(store, origin, root, "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
                    with TestClient(app) as client:
                        queued = client.post("/discovery", json={
                            "profile_id": "fixture", "search_profile_id": "fixture-selected",
                            "request_id": "fixture-search-excludes-all",
                        })
                    self.assertEqual(queued.status_code, 202, queued.text)
                    with patch.dict(os.environ, {
                        "MJOBS_DATABASE_URL": store.url, "MJOBS_MOCK_ORIGIN": origin,
                        "MJOBS_OUTPUT_DIR": str(root), "MJOBS_OPERATION_MODE": "FIXTURE",
                    }):
                        result = execute_operation(queued.json()["operation_id"])
                    self.assertEqual(result["status"], "COMPLETED", result)
                    with store.engine.connect() as connection:
                        self.assertEqual(connection.execute(select(m.matches)).all(), [])
                    for title in ("Python Developer", "Data Engineer"):
                        save_search_profile(store, {
                            "id": "fixture-selected", "profile_id": "fixture", "name": title,
                            "include_titles": [title], "required_keywords": ["Python"],
                            "remote": "REQUIRED", "enabled": True,
                        })
                        with TestClient(app) as client:
                            queued = client.post("/discovery", json={
                                "profile_id": "fixture", "search_profile_id": "fixture-selected",
                                "request_id": "fixture-search-" + title.replace(" ", "-"),
                            })
                        self.assertEqual(queued.status_code, 202, queued.text)
                        with patch.dict(os.environ, {
                            "MJOBS_DATABASE_URL": store.url, "MJOBS_MOCK_ORIGIN": origin,
                            "MJOBS_OUTPUT_DIR": str(root), "MJOBS_OPERATION_MODE": "FIXTURE",
                        }):
                            result = execute_operation(queued.json()["operation_id"])
                        self.assertEqual(result["status"], "COMPLETED", result)
                        with TestClient(app) as client:
                            jobs = client.get("/jobs", params={
                                "profile_id": "fixture", "search_profile_id": "fixture-selected",
                            })
                        self.assertEqual(jobs.status_code, 200, jobs.text)
                        self.assertEqual([job["title"] for job in jobs.json()["jobs"]], [title])
                        reopened = Store(store.url)
                        try:
                            history = list_search_history(reopened, "fixture")["runs"][0]
                            self.assertEqual(history["filters"]["include_titles"], [title])
                            self.assertEqual(history["results"], 4)
                            self.assertEqual(list_search_history(reopened, "other")["runs"], [])
                        finally:
                            reopened.engine.dispose()
                with store.engine.connect() as connection:
                    self.assertEqual(len(connection.execute(select(m.matches)).all()), 2)
                    self.assertEqual(connection.execute(select(m.matches).where(
                        m.matches.c.profile_id == "other")).all(), [])
                    self.assertEqual(len(connection.execute(select(m.jobs)).all()), 2)
                    self.assertEqual(len(connection.execute(select(m.postings)).all()), 4)
            finally:
                store.engine.dispose()
