"""Domain evidence writes and locked reads share a normalized host boundary."""
import tempfile
import unittest
from pathlib import Path

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery import domains
from services.company_discovery.domains import record_company_domain


class DomainEvidenceLockTests(unittest.TestCase):
    def test_locked_domain_read_includes_all_owners_and_unverified_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(f"sqlite:///{(Path(directory) / 'domains.db').as_posix()}")
            try:
                store.migrate()
                with store.engine.begin() as connection:
                    for company_id in ('a', 'b'):
                        upsert(connection, m.companies, dict(id=company_id, name=company_id, domain=''))
                record_company_domain(store, 'b', 'employer.example', 'PUBLIC_METADATA', ['observed'])
                record_company_domain(store, 'a', 'employer.example', 'VERIFIED_EXISTING_DOMAIN', ['independent'])
                with store.engine.begin() as connection:
                    connection.exec_driver_sql('BEGIN IMMEDIATE')
                    self.assertTrue(hasattr(domains, 'lock_domain_evidence'))
                    rows = domains.lock_domain_evidence(connection, 'https://www.employer.example/')
                self.assertEqual([row['id'] for row in rows], sorted(row['id'] for row in rows))
                self.assertEqual({row['company_id']: row['verified'] for row in rows},
                                 {'a': True, 'b': False})
            finally:
                store.engine.dispose()


if __name__ == '__main__':
    unittest.main()
