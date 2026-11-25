"""Strict browser fixture oracle for explicit, profile-scoped job opens."""
from datetime import datetime

from sqlalchemy import select

from packages.persistence import models as m


class VisitOracle:
    def __init__(self, test, store, origin, expected):
        self.test = test
        self.store = store
        self.origin = origin
        self.expected = list(expected)
        self.urls = [f'{origin}/profiles/{profile}/jobs/{job}/view'
                     for profile, job in self.expected]
        self.requests = []
        self.receipts = []
        test.assertEqual(self.rows(), [])

    def expect_open(self, profile, job):
        """Declare the next intended action before observing any network request."""
        self.test.assertEqual(len(self.receipts), len(self.expected))
        self.expected.append((profile, job))
        self.urls.append(f'{self.origin}/profiles/{profile}/jobs/{job}/view')

    def rows(self):
        with self.store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(m.profile_job_views)).mappings()]

    def admit(self, request):
        index = len(self.requests)
        if (index < len(self.urls) and request.method == 'POST'
                and request.url == self.urls[index] and request.post_data is None):
            self.requests.append(request.url)
            return True
        return False

    def response(self, page):
        url = self.urls[len(self.receipts)]
        return page.expect_response(lambda response:
                                    response.url == url and response.request.method == 'POST')

    def verify(self, response):
        index = len(self.receipts)
        self.test.assertEqual(response.status, 201)
        receipt = response.json()
        profile, job = self.expected[index]
        self.test.assertEqual((receipt['profile_id'], receipt['job_id']), (profile, job))
        self.test.assertIsNotNone(datetime.fromisoformat(receipt['viewed_at']).tzinfo)
        self.receipts.append(receipt)
        self.assert_persisted()

    def assert_persisted(self):
        orders = {}
        expected = {}
        for receipt in self.receipts:
            profile, job = receipt['profile_id'], receipt['job_id']
            orders[profile] = orders.get(profile, 0) + 1
            expected[(profile, job)] = (orders[profile], receipt['viewed_at'])
        rows = self.rows()
        actual = {(row['profile_id'], row['job_id']): (row['visit_order'], row['viewed_at'])
                  for row in rows}
        self.test.assertEqual(len(rows), len(expected))
        self.test.assertEqual(actual, expected)

    def finish(self):
        self.test.assertEqual(self.requests, self.urls)
        self.test.assertEqual(len(self.receipts), len(self.expected))
        self.assert_persisted()
