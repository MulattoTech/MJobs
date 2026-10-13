"""Connected blocked-route guidance, read-only reload and explicit new dry-run."""
from copy import deepcopy

from playwright.sync_api import expect
from sqlalchemy import select

from packages.persistence import models as m
from services.applications.attempt_history import start_attempt, finish_attempt
from services.documents.generate import prepare
from tests.browser import test_mock_run_controls as flow


class RouteOwnershipBrowserTests(flow.MockRunBrowserHarness):
    def test_blocked_route_survives_reload_and_needs_new_reviewed_intent(self):
        profile = deepcopy(flow.PROFILE)
        profile['id'] = 'other-synthetic-profile'
        self.store.save_profile(profile)
        owner_id = prepare(self.store, self.job['id'], profile['id'], self.root)
        owner = self.store.application(owner_id)
        with self.store.engine.connect() as connection:
            route = dict(connection.execute(select(m.routes).where(
                m.routes.c.id == owner['route_id'])).mappings().one())
            docs = list(connection.execute(select(m.documents).where(
                m.documents.c.application_id == owner_id)).mappings())
        attempt = start_attempt(self.store, owner, route, docs, request_id='other-owner')
        self.panel.get_by_role('button', name='Run dry-run', exact=True).click()
        expect(self.panel).to_contain_text('QUEUED')
        blocked = self.operations()[0]['id']
        self.run_worker(blocked, expected_status='BLOCKED', refresh_panel=False)
        self.panel.get_by_role('button', name='Refresh run status', exact=True).click()
        expect(self.panel.get_by_role('alert')).to_contain_text('Another application run owns this route')
        self.assertEqual(self.counts(), {'review': 0, 'submit': 0})
        self.open_application()
        expect(self.panel.get_by_role('alert')).to_contain_text('Restarting does not clear unfinished ownership')
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.operations()), 1)
        finish_attempt(self.store, attempt['id'], owner, error=RuntimeError('Synthetic browser closed'))
        self.panel.get_by_role('button', name='Start a new reviewed run', exact=True).click()
        self.panel.get_by_role('button', name='Run dry-run', exact=True).click()
        expect(self.panel).to_contain_text('QUEUED')
        second = next(r['id'] for r in self.operations() if r['id'] != blocked)
        self.run_worker(second)
        self.open_application()
        expect(self.panel).to_contain_text('COMPLETED')
        self.assertEqual(self.counts(), {'review': 1, 'submit': 0})
        self.assertEqual(len(self.posts), 2)
        self.assertFalse(self.external)
