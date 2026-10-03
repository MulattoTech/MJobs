"""A stale domain proof cannot authorize an automatically enabled source."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery import flywheel
from services.company_discovery.domains import record_company_domain
from services.company_discovery.public_client import PublicCompanyClient, PublicFetchError


class VerifiedEnrollmentTests(unittest.TestCase):
    def test_domain_revoked_after_feed_fetch_cannot_enable_new_source(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(f"sqlite:///{(Path(directory) / 'test.db').as_posix()}")
            try:
                store.migrate()
                with store.engine.begin() as connection:
                    upsert(connection, m.companies, {'id': 'verified-employer', 'name': 'Employer Example', 'domain': ''})
                domain = record_company_domain(store, 'verified-employer', 'employer.example',
                    'VERIFIED_EXISTING_DOMAIN', ['independent employer record'])
                pages = {
                    '/robots.txt': 'User-agent: *\nAllow: /',
                    '/': '<html>Employer Example<a rel="careers" href="/careers">Careers</a></html>',
                    '/careers': '<html>Employer Example<link rel="alternate" type="application/feed+json" href="/jobs.json"></html>',
                    '/jobs.json': '{"version":"https://jsonfeed.org/version/1.1","items":[]}',
                }

                def fetch(client, url, deadline):
                    request = httpx.Request('GET', url)
                    return httpx.Response(200, text=pages[request.url.path], request=request)

                parse_feed = flywheel.parse_public_job_feed

                def revoke_after_fetch(body, strategy):
                    jobs = parse_feed(body, strategy)
                    with store.engine.begin() as connection:
                        connection.execute(update(m.company_domains).where(
                            m.company_domains.c.id == domain['id']).values(verified=False))
                    return jobs

                candidate = {'company_name': 'Employer Example',
                             'company_website': 'https://employer.example/'}
                with patch.object(PublicCompanyClient, '_request_once', fetch), patch.object(
                        flywheel, 'parse_public_job_feed', side_effect=revoke_after_fetch):
                    client = PublicCompanyClient(candidate['company_website'])
                    try:
                        with self.assertRaises(PublicFetchError):
                            flywheel.discover_company_source(store, candidate, client)
                    finally:
                        client.close()
                with store.engine.connect() as connection:
                    enabled = connection.execute(select(m.company_sources.c.id).where(
                        m.company_sources.c.enabled.is_(True))).all()
                self.assertEqual(enabled, [])

                record_company_domain(store, 'verified-employer', 'employer.example',
                    'VERIFIED_EXISTING_DOMAIN', ['fresh independent employer record'])
                with patch.object(PublicCompanyClient, '_request_once', fetch):
                    client = PublicCompanyClient(candidate['company_website'])
                    try:
                        result = flywheel.discover_company_source(store, candidate, client)
                    finally:
                        client.close()
                self.assertEqual(result['company']['company_id'], 'verified-employer')
                with store.engine.connect() as connection:
                    source = connection.execute(select(m.company_sources)).mappings().one()
                self.assertTrue(source['enabled'])
                self.assertEqual(source['confidence'], 0.88)
            finally:
                store.engine.dispose()


if __name__ == '__main__':
    unittest.main()
