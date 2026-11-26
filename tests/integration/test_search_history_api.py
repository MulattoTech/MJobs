"""Profile-scoped search-history API contract tests."""
from __future__ import annotations

import http.client
from pathlib import Path
import socket
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.search_history import create_search_history_router
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.search_history import record_search_history
from tests.integration.test_spine import PROFILE


class SearchHistoryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-search-history-api-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'history.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "email": "other@example.test"})
        self.newest = self._run("fixture-newest", "fixture", "2026-10-08T12:00:00Z")
        self.older = self._run("fixture-older", "fixture", "2026-10-07T12:00:00Z")
        self.older["status"] = "FAILED"
        self.older["errors"] = ["Synthetic fixture source timed out."]
        self.other = self._run("other-only", "other", "2026-10-08T11:00:00Z")
        self.lister_calls = []
        self.search_profile_calls = []
        self.invalid_cursor = False
        self.scope_mismatch = None
        app = FastAPI()
        app.include_router(create_search_history_router(
            self.store, history_lister=self._list_history,
        ))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_search_history_router_is_mounted_by_application_factory(self):
        from apps.api.main import create_app

        app = create_app(
            self.store, "http://127.0.0.1:1", self.root,
            operation_mode="FIXTURE",
        )
        with TestClient(app) as client:
            response = client.get("/profiles/fixture/search-history")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "profile_id": "fixture", "items": [], "next_cursor": None,
        })

    def test_real_history_route_keeps_padded_saved_searches_distinct(self):
        from apps.api.main import create_app

        app = create_app(self.store, "http://127.0.0.1:1", self.root, operation_mode="FIXTURE")
        search_ids = (" infra ", "infra", " " + "\U0001f680" * 126 + " ")
        with TestClient(app) as client:
            for index, search_id in enumerate(search_ids):
                saved = client.put("/search-profiles", json={
                    "id": search_id, "profile_id": "fixture", "name": f"Search {index}",
                })
                self.assertEqual(saved.status_code, 200, saved.text)
                record_search_history(self.store, {
                    **self.newest, "run_id": f"receipt-{index}", "search_profile_id": search_id,
                })
            route = "/profiles/fixture/search-history"
            history = client.get(route)
            self.assertEqual(history.status_code, 200, history.text)
            self.assertEqual({item["search_profile_id"] for item in history.json()["items"]}, set(search_ids))
            for index, search_id in enumerate(search_ids):
                with self.subTest(search_id=search_id):
                    scoped = client.get(route, params={"limit": 1, "search_profile_id": search_id})
                    self.assertEqual(scoped.status_code, 200, scoped.text)
                    self.assertEqual([(item["id"], item["search_profile_id"]) for item in scoped.json()["items"]],
                                     [(f"receipt-{index}", search_id)])
            for bad_id in (" ", "x\x00", "x\x7f", "x" * 129):
                self.assertEqual(client.get(route, params={"search_profile_id": bad_id}).status_code, 422)

    def test_real_history_route_paginates_long_unicode_profiles_with_exact_scope(self):
        from apps.api.main import create_app

        app = create_app(self.store, "http://127.0.0.1:1", self.root, operation_mode="FIXTURE")
        search_id = " " + "\U0001f680" * 126 + " "
        with TestClient(app) as client:
            for index, profile_id in enumerate(("\u00e9" * 40, "\U0001f680" * 128, " other ")):
                with self.subTest(profile_length=len(profile_id), padded=profile_id != profile_id.strip()):
                    self.store.save_profile({**PROFILE, "id": profile_id, "email": f"profile-{index}@example.test"})
                    for run in (self.newest, self.older):
                        record_search_history(self.store, {
                            **run, "profile_id": profile_id, "search_profile_id": search_id,
                        })
                    route = f"/profiles/{profile_id}/search-history"
                    for scope in ({"search_profile_id": search_id}, {}):
                        first = client.get(route, params={"limit": 1, **scope})
                        self.assertEqual(first.status_code, 200, first.text)
                        self.assertEqual(first.json()["items"][0]["id"], self.newest["run_id"])
                        cursor = first.json()["next_cursor"]
                        self.assertIsInstance(cursor, str)
                        second = client.get(route, params={"limit": 1, "cursor": cursor, **scope})
                        self.assertEqual(second.status_code, 200, second.text)
                        self.assertEqual(second.json()["items"][0]["id"], self.older["run_id"])
                        self.assertIsNone(second.json()["next_cursor"])
                        if scope:
                            malformed = client.get(route, params={"cursor": cursor + "!", **scope})
                            self.assertEqual(malformed.status_code, 422, malformed.text)
                            self.assertEqual(malformed.json()["detail"]["code"], "SEARCH_HISTORY_CURSOR_INVALID")
                            for wrong_route, wrong_scope in (
                                (route, {"search_profile_id": search_id.strip()}),
                                (route, {}),
                                ("/profiles/other/search-history", scope),
                            ):
                                rejected = client.get(wrong_route, params={"cursor": cursor, **wrong_scope})
                                self.assertEqual(rejected.status_code, 422, rejected.text)
                                self.assertEqual(rejected.json()["detail"]["code"], "SEARCH_HISTORY_CURSOR_INVALID")

    def _run(self, run_id, profile_id, finished_at):
        return {
            "run_id": run_id,
            "profile_id": profile_id,
            "source": "fixture-feed",
            "query": "Systems Administrator",
            "filters": {"include_titles": ["Systems Administrator"]},
            "started_at": finished_at[:10] + "T11:55:00Z",
            "finished_at": finished_at,
            "status": "COMPLETED",
            "results": 12,
            "new_jobs": 3,
            "duplicates": 9,
            "errors": [],
        }

    def _list_history(self, store, profile_id, *, limit=50, cursor=None, search_profile_id=None):
        self.lister_calls.append((store, profile_id, limit, cursor))
        if search_profile_id is not None:
            self.search_profile_calls.append((store, profile_id, search_profile_id))
            runs = [run for run in (self.newest, self.older, self.other)
                    if run["profile_id"] == profile_id
                    and run.get("search_profile_id") == search_profile_id]
            return {"profile_id": profile_id, "runs": runs[:limit], "next_cursor": None}
        if self.invalid_cursor:
            raise ValueError("SEARCH_HISTORY_CURSOR_INVALID")
        if self.scope_mismatch == "envelope":
            return {
                "profile_id": "other",
                "runs": [self.other],
                "next_cursor": None,
            }
        if self.scope_mismatch == "run":
            return {
                "profile_id": profile_id,
                "runs": [self.other],
                "next_cursor": None,
            }
        if cursor == "page-two":
            runs = [self.older] if profile_id == "fixture" else [self.other]
            return {"profile_id": profile_id, "runs": runs, "next_cursor": None}
        runs = [self.newest] if profile_id == "fixture" else [self.other]
        return {"profile_id": profile_id, "runs": runs, "next_cursor": "page-two"}

    def _snapshot(self):
        with self.store.engine.connect() as connection:
            return {
                table.name: [dict(row) for row in connection.execute(
                    select(table),
                ).mappings()]
                for table in m.metadata.sorted_tables
            }

    def _get_without_network(self, url):
        with patch.object(socket, "create_connection",
                          side_effect=AssertionError("network access attempted")), \
                patch("urllib.request.urlopen",
                      side_effect=AssertionError("network access attempted")), \
                patch.object(http.client.HTTPConnection, "connect",
                             side_effect=AssertionError("network access attempted")):
            return self.client.get(url)

    def test_profile_scoped_newest_first_pagination_and_read_only(self):
        before = self._snapshot()

        first = self._get_without_network(
            "/profiles/fixture/search-history?limit=1",
        )
        second = self._get_without_network(
            "/profiles/fixture/search-history?limit=1&cursor=page-two",
        )

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), {
            "profile_id": "fixture",
            "items": [{
                "id": "fixture-newest",
                "profile_id": "fixture",
                "source": "fixture-feed",
                "query": "Systems Administrator",
                "filters": {"include_titles": ["Systems Administrator"]},
                "started_at": "2026-10-08T11:55:00Z",
                "finished_at": "2026-10-08T12:00:00Z",
                "status": "COMPLETED",
                "results": 12,
                "new_jobs": 3,
                "duplicates": 9,
                "errors": [],
            }],
            "next_cursor": "page-two",
        })
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["items"], [{
            "id": "fixture-older",
            "profile_id": "fixture",
            "source": "fixture-feed",
            "query": "Systems Administrator",
            "filters": {"include_titles": ["Systems Administrator"]},
            "started_at": "2026-10-07T11:55:00Z",
            "finished_at": "2026-10-07T12:00:00Z",
            "status": "FAILED",
            "results": 12,
            "new_jobs": 3,
            "duplicates": 9,
            "errors": ["Synthetic fixture source timed out."],
        }])
        self.assertIsNone(second.json()["next_cursor"])
        self.assertEqual([call[1:] for call in self.lister_calls], [
            ("fixture", 1, None),
            ("fixture", 1, "page-two"),
        ])
        self.assertEqual(self._snapshot(), before)

    def test_profile_isolation_missing_profile_and_invalid_limit(self):
        before = self._snapshot()

        other = self._get_without_network("/profiles/other/search-history")
        missing = self._get_without_network("/profiles/missing/search-history")
        max_limit = self._get_without_network("/profiles/fixture/search-history?limit=100")
        too_small = self._get_without_network("/profiles/fixture/search-history?limit=0")
        too_large = self._get_without_network("/profiles/fixture/search-history?limit=101")

        self.assertEqual(other.status_code, 200, other.text)
        self.assertEqual(other.json()["profile_id"], "other")
        self.assertEqual([item["id"] for item in other.json()["items"]], ["other-only"])
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertEqual(missing.json()["detail"], {"code": "SEARCH_HISTORY_PROFILE_NOT_FOUND"})
        self.assertEqual(max_limit.status_code, 200, max_limit.text)
        self.assertEqual(too_small.status_code, 422, too_small.text)
        self.assertEqual(too_large.status_code, 422, too_large.text)
        self.assertEqual([call[2] for call in self.lister_calls], [50, 100])
        self.assertEqual(self._snapshot(), before)

    def test_unknown_metrics_remain_null_in_the_api_projection(self):
        self.newest["duplicates"] = None
        response = self._get_without_network("/profiles/fixture/search-history?limit=1")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["items"][0]["duplicates"])

    def test_saved_search_identity_is_optional_in_api_projection(self):
        self.newest["search_profile_id"] = "remote-it-ok"
        response = self._get_without_network("/profiles/fixture/search-history?limit=1")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"][0]["search_profile_id"], "remote-it-ok")

    def test_exact_saved_search_receipt_lookup_is_profile_scoped_and_before_limit(self):
        self.newest["search_profile_id"] = "fixture-search"
        latest = self._get_without_network(
            "/profiles/fixture/search-history?limit=1&search_profile_id=fixture-search",
        )
        foreign = self._get_without_network(
            "/profiles/other/search-history?limit=1&search_profile_id=fixture-search",
        )

        self.assertEqual(latest.status_code, 200, latest.text)
        self.assertEqual([item["id"] for item in latest.json()["items"]], ["fixture-newest"])
        self.assertEqual(latest.json()["items"][0]["search_profile_id"], "fixture-search")
        self.assertEqual(foreign.status_code, 200, foreign.text)
        self.assertEqual(foreign.json()["items"], [])
        self.assertEqual(self.search_profile_calls, [
            (self.store, "fixture", "fixture-search"),
            (self.store, "other", "fixture-search"),
        ])

    def test_invalid_cursor_and_service_scope_mismatch_fail_closed(self):
        before = self._snapshot()
        self.invalid_cursor = True
        invalid = self._get_without_network(
            "/profiles/fixture/search-history?cursor=not-a-cursor",
        )
        self.invalid_cursor = False
        self.scope_mismatch = "envelope"
        mismatched = self._get_without_network("/profiles/fixture/search-history")
        self.scope_mismatch = "run"
        mismatched_run = self._get_without_network("/profiles/fixture/search-history")

        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(invalid.json()["detail"], {"code": "SEARCH_HISTORY_CURSOR_INVALID"})
        self.assertEqual(mismatched.status_code, 500, mismatched.text)
        self.assertEqual(mismatched.json()["detail"], {"code": "SEARCH_HISTORY_SCOPE_MISMATCH"})
        self.assertNotIn("other-only", mismatched.text)
        self.assertEqual(mismatched_run.status_code, 500, mismatched_run.text)
        self.assertEqual(mismatched_run.json()["detail"], {"code": "SEARCH_HISTORY_SCOPE_MISMATCH"})
        self.assertNotIn("other-only", mismatched_run.text)
        self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
