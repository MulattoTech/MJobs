import unittest
from types import SimpleNamespace

from services.company_discovery.source_host_locks import lock_company_source_hosts


class _Connection:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self):
        self.calls = []

    def execute(self, statement, params):
        self.calls.append(params["source_host_key"])


class CompanySourceHostLockTests(unittest.TestCase):
    def test_locks_sorted_related_suffixes_and_skips_public_suffix(self):
        connection = _Connection()

        hosts = lock_company_source_hosts(connection, [
            "https://careers.employer.example/jobs",
            "https://www.other.example/openings",
        ])

        self.assertEqual(hosts, [
            "careers.employer.example", "employer.example", "other.example",
        ])
        self.assertEqual(len(connection.calls), len(hosts))

    def test_unusable_values_do_not_create_broad_locks(self):
        connection = _Connection()

        self.assertEqual(lock_company_source_hosts(connection, ["localhost", "", "https://127.0.0.1"]), [])
        self.assertEqual(connection.calls, [])


if __name__ == "__main__":
    unittest.main()
