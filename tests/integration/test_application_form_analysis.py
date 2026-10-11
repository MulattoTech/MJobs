"""Safe connection between local mock questionnaire preflight and form metadata analysis."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import select

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.mock_adapter import _preflight
from services.discovery.pipeline import MockClient, discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class ApplicationFormAnalysisIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'form-analysis.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(PROFILE)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def prepared_context(self, origin):
        discover(self.store, origin, "fixture")
        job = next(row for row in self.store.jobs() if row["requisition"] == "B")
        application_id = prepare(self.store, job["id"], "fixture", self.root)
        application = self.store.application(application_id)
        with self.store.engine.connect() as connection:
            route = dict(connection.execute(select(m.routes).where(
                m.routes.c.id == application["route_id"])).mappings().one())
        profile = self.store.profile(application["profile_id"], application["profile_version"])
        return application, route, profile

    def analyze(self, origin, application, route, profile, edit_questionnaire):
        client = MockClient(origin)
        original_get = MockClient.get

        def with_metadata(instance, url):
            response = original_get(instance, url)
            if "/ats/questions/" not in url:
                return response
            questionnaire = response.json()
            edit_questionnaire(questionnaire)
            return httpx.Response(response.status_code, json=questionnaire,
                                  request=response.request)

        try:
            with patch.object(MockClient, "get", with_metadata):
                return _preflight(self.store, application, route, profile,
                                  {"resume", "cover-letter"}, client)
        finally:
            client.close()

    def form_analysis_event(self, application_id, store=None):
        store = store or self.store
        with store.engine.connect() as connection:
            return dict(connection.execute(select(m.audit).where(
                m.audit.c.entity_id == application_id,
                m.audit.c.action == "APPLICATION_FORM_ANALYSIS",
            )).mappings().one())

    def test_mock_preflight_persists_only_allowlisted_form_metadata(self):
        with fixture_server() as origin:
            application, route, profile = self.prepared_context(origin)

            def add_metadata(questionnaire):
                country = next(row for row in questionnaire["questions"]
                               if row["id"] == "country")
                country.update({
                    "name": "country", "placeholder": "Choose a country",
                    "aria_label": "Country of residence", "input_type": "select",
                    "nearby_text": "Select one option",
                    "section_heading": "Contact details",
                    "fieldset_legend": "Applicant information",
                    "options": [
                        {"label": "United States", "value": "PRIVATE_OPTION_VALUE"},
                        {"label": "Canada", "value": "PRIVATE_OPTION_VALUE_2"},
                    ],
                    "value": "PRIVATE_CURRENT_VALUE",
                })

            result = self.analyze(origin, application, route, profile, add_metadata)

            self.assertEqual(result["result"], "PASS")
            reopened = Store(f"sqlite:///{(self.root / 'form-analysis.db').as_posix()}")
            try:
                analysis = self.form_analysis_event(application["id"], reopened)["payload"]
            finally:
                reopened.engine.dispose()
            country = next(row for row in analysis["fields"] if row["id"] == "country")
            self.assertEqual(country["labels"], ["Country"])
            self.assertEqual(country["name"], "country")
            self.assertEqual(country["placeholder"], "Choose a country")
            self.assertEqual(country["aria_label"], "Country of residence")
            self.assertEqual(country["input_type"], "select")
            self.assertEqual(country["nearby_text"], "Select one option")
            self.assertEqual(country["section_heading"], "Contact details")
            self.assertEqual(country["fieldset_legend"], "Applicant information")
            self.assertEqual(country["options"], ["United States", "Canada"])
            serialized = json.dumps(analysis)
            self.assertNotIn("PRIVATE_CURRENT_VALUE", serialized)
            self.assertNotIn("PRIVATE_OPTION_VALUE", serialized)
            with self.store.engine.connect() as connection:
                attempt_count = connection.execute(select(m.attempts.c.id).where(
                    m.attempts.c.application_id == application["id"])).all()
            self.assertEqual(attempt_count, [])

    def test_sensitive_question_metadata_is_classified_without_its_value(self):
        with fixture_server() as origin:
            application, route, profile = self.prepared_context(origin)

            def add_sensitive_metadata(questionnaire):
                question = next(row for row in questionnaire["questions"]
                                if row["id"] == "fixture_question")
                question.update({
                    "label": "Additional identifier", "aria_label": "Social Security number",
                    "input_type": "text", "value": "PRIVATE_SENSITIVE_VALUE",
                })

            result = self.analyze(origin, application, route, profile, add_sensitive_metadata)

            self.assertEqual(result["result"], "UNKNOWN")
            self.assertEqual(result["reason"], "UNMAPPED_REQUIRED_QUESTION")
            from services.applications.preflight import latest_preflight
            preflight = latest_preflight(self.store, application["id"])
            self.assertEqual(preflight["reason"], "UNMAPPED_REQUIRED_QUESTION")
            unmapped = next(row for row in preflight["mappings"]
                            if row["question_id"] == "fixture_question")
            self.assertEqual(unmapped["question"], "Additional identifier")
            self.assertEqual(unmapped["answer_ids"], [])
            self.assertEqual(unmapped["fact_ids"], [])
            self.assertNotIn("PRIVATE_SENSITIVE_VALUE", json.dumps(preflight))
            event = self.form_analysis_event(application["id"])
            field = next(row for row in event["payload"]["fields"]
                         if row["id"] == "fixture_question")
            self.assertEqual(field["sensitivity"], "SENSITIVE")
            self.assertNotIn("PRIVATE_SENSITIVE_VALUE", json.dumps(event["payload"]))
            self.assertEqual(httpx.get(origin + "/_test/request-counts").json(),
                             {"review": 0, "submit": 0})
            with self.store.engine.connect() as connection:
                attempt_count = connection.execute(select(m.attempts.c.id).where(
                    m.attempts.c.application_id == application["id"])).all()
            self.assertEqual(attempt_count, [])


if __name__ == "__main__":
    unittest.main()
