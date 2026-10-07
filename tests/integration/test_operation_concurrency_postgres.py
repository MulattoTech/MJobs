"""Cross-process operation-category admission against an explicit disposable PostgreSQL URL."""
from concurrent.futures import ThreadPoolExecutor
import os
import unittest
from uuid import uuid4
from unittest.mock import patch

from packages.persistence import models as m
from packages.persistence.store import Store
from workers.dispatch import enqueue_operation
from workers.operations import OperationConcurrencyLimit, admit_inline, claim


POSTGRES_URL = os.environ.get("MJOBS_TEST_POSTGRES_URL")


@unittest.skipUnless(POSTGRES_URL, "Set MJOBS_TEST_POSTGRES_URL to an isolated disposable PostgreSQL database")
class PostgreSQLConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(POSTGRES_URL)
        m.metadata.create_all(cls.store.engine)

    @classmethod
    def tearDownClass(cls):
        cls.store.engine.dispose()

    def test_cross_kind_parallel_claims_respect_one_shared_category_cap(self):
        suffix = uuid4().hex
        operation_ids = [
            enqueue_operation(self.store, "COMPANY_DISCOVER", "company-" + suffix, {"request": "a"}),
            enqueue_operation(self.store, "COMPANY_SOURCE_REFRESH", "source-" + suffix, {"request": "b"}),
            enqueue_operation(self.store, "COMPANY_DISCOVER", "company-" + suffix, {"request": "c"}),
        ]
        self.addCleanup(self._release_test_operations, operation_ids)
        with patch.dict(os.environ, {"MJOBS_COMPANY_DISCOVERY_CONCURRENCY": "1"}):
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(lambda identifier: claim(self.store, identifier), operation_ids))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_parallel_application_claims_respect_one_shared_category_cap(self):
        suffix = uuid4().hex
        operation_ids = [
            enqueue_operation(self.store, "APPLY_MOCK", "application-" + suffix, {"request": str(index)})
            for index in range(2)
        ]
        self.addCleanup(self._release_test_operations, operation_ids)

        def claim_with_independent_store(identifier):
            store = Store(POSTGRES_URL)
            try:
                return claim(store, identifier)
            finally:
                store.engine.dispose()

        with patch.dict(os.environ, {"MJOBS_APPLICATION_AUTOMATION_CONCURRENCY": "1"}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(claim_with_independent_store, operation_ids))

        self.assertEqual(sum(result is not None for result in results), 1)
        with self.store.engine.connect() as connection:
            rows = list(connection.execute(m.operations.select().where(
                m.operations.c.id.in_(operation_ids))).mappings())
        self.assertEqual(sorted(row["state"] for row in rows), ["QUEUED", "RUNNING"])
        self.assertEqual(sorted(row["attempt_count"] for row in rows), [0, 1])

    def test_source_collection_parallel_claims_respect_one_shared_category_cap(self):
        suffix = uuid4().hex
        operation_ids = [
            enqueue_operation(self.store, "DISCOVER", "source-" + suffix, {"request": str(index)})
            for index in range(2)
        ]
        self.addCleanup(self._release_test_operations, operation_ids)

        def claim_with_independent_store(identifier):
            store = Store(POSTGRES_URL)
            try:
                return claim(store, identifier)
            finally:
                store.engine.dispose()

        with patch.dict(os.environ, {"MJOBS_SOURCE_COLLECTION_CONCURRENCY": "1"}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(claim_with_independent_store, operation_ids))

        self.assertEqual(sum(result is not None for result in results), 1)
        with self.store.engine.connect() as connection:
            rows = list(connection.execute(m.operations.select().where(
                m.operations.c.id.in_(operation_ids))).mappings())
        self.assertEqual(sorted(row["state"] for row in rows), ["QUEUED", "RUNNING"])
        self.assertEqual(sorted(row["attempt_count"] for row in rows), [0, 1])

    def test_parallel_ai_admissions_respect_one_shared_category_cap(self):
        suffix = uuid4().hex

        def admit_with_independent_store(index):
            store = Store(POSTGRES_URL)
            try:
                try:
                    return admit_inline(store, "AI_EVALUATE", f"synthetic-ai-{suffix}-{index}")
                except OperationConcurrencyLimit:
                    return None
            finally:
                store.engine.dispose()

        with patch.dict(os.environ, {"MJOBS_AI_CONCURRENCY": "1"}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                operation_ids = list(pool.map(admit_with_independent_store, range(2)))

        admitted = [identifier for identifier in operation_ids if identifier is not None]
        self.addCleanup(self._release_test_operations, admitted)
        self.assertEqual(len(admitted), 1)
        with self.store.engine.connect() as connection:
            rows = list(connection.execute(m.operations.select().where(
                m.operations.c.id.in_(admitted))).mappings())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "AI_EVALUATE")
        self.assertEqual(rows[0]["state"], "RUNNING")
        self.assertEqual(rows[0]["attempt_count"], 1)

    def test_document_generation_cap_is_shared_between_inline_and_worker_operations(self):
        suffix = uuid4().hex
        worker_id = enqueue_operation(
            self.store, "PREPARE", "synthetic-document-" + suffix, {"request": suffix})

        def claim_worker():
            store = Store(POSTGRES_URL)
            try:
                return claim(store, worker_id)
            finally:
                store.engine.dispose()

        def admit_inline_document():
            store = Store(POSTGRES_URL)
            try:
                try:
                    return admit_inline(store, "RESUME_EXTRACT", "synthetic-resume-" + suffix)
                except OperationConcurrencyLimit:
                    return None
            finally:
                store.engine.dispose()

        inline_id = None
        try:
            with patch.dict(os.environ, {"MJOBS_DOCUMENT_GENERATION_CONCURRENCY": "1"}):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    worker_future = pool.submit(claim_worker)
                    inline_future = pool.submit(admit_inline_document)
                    worker_claim = worker_future.result()
                    inline_id = inline_future.result()

            self.assertEqual(int(worker_claim is not None) + int(inline_id is not None), 1)
            ids = [worker_id] + ([inline_id] if inline_id else [])
            with self.store.engine.connect() as connection:
                rows = list(connection.execute(m.operations.select().where(
                    m.operations.c.id.in_(ids))).mappings())
            self.assertEqual(sum(row["state"] == "RUNNING" for row in rows), 1)
            if inline_id:
                self.assertEqual(sorted(row["state"] for row in rows), ["QUEUED", "RUNNING"])
                self.assertEqual(sorted(row["attempt_count"] for row in rows), [0, 1])
            else:
                self.assertEqual([(row["kind"], row["state"], row["attempt_count"])
                                  for row in rows], [("PREPARE", "RUNNING", 1)])
        finally:
            self._release_test_operations([worker_id] + ([inline_id] if inline_id else []))

    def _release_test_operations(self, operation_ids):
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.update().where(
                m.operations.c.id.in_(operation_ids)).values(
                    state="CANCELLED", cancel_requested=True, lease_expires_at=None))


if __name__ == "__main__":
    unittest.main()
