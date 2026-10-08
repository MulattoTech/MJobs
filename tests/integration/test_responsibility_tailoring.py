"""Responsibility-phrase resume selection must stay exact and explainable."""
import unittest
from sqlalchemy import update

from packages.persistence import models as m
from services.discovery.pipeline import discover
from services.documents.comparison import ComparisonUnavailable, compare_resumes, tailored_context
from services.documents.generate import prepare
from services.documents.history import application_documents
from fixtures.mock_server import fixture_server
from tests.integration import test_document_provenance as fixtures
from tests.integration.test_spine import PROFILE


class ResponsibilityPhraseSelectionTests(unittest.TestCase):
    def test_specific_multiword_target_duty_selects_fact_without_skill_label(self):
        from services.documents.tailor import select_bullet_rows

        facts = [
            {"fact_id": "duty", "kind": "responsibility", "text": "Maintain nightly database backups for four clinics."},
            {"fact_id": "other", "kind": "experience", "text": "Coordinated a server refresh."},
        ]
        job = {"skills": [], "description": "Responsibilities:\n- Maintain nightly database backups for four clinics.\nQualifications:\n- Experience with SQL."}

        selected, context = select_bullet_rows(facts, job)

        self.assertEqual([row["fact_id"] for row in selected], ["duty"])
        self.assertEqual(context["mode"], "EXACT_TARGET_RESPONSIBILITY_PHRASE_BULLET_FACTS")
        self.assertEqual(context["matched_responsibilities"]["duty"], ["maintain nightly database backups for four clinics"])

    def test_generic_one_word_and_ambiguous_terms_do_not_select_or_omit(self):
        from services.documents.tailor import select_bullet_rows

        facts = [
            {"fact_id": "generic", "kind": "responsibility", "text": "Worked with teams."},
            {"fact_id": "one-word", "kind": "experience", "text": "Monitored servers."},
            {"fact_id": "ambiguous", "kind": "project", "text": "Managed access and security."},
        ]
        job = {"skills": [], "description": "Responsibilities:\n- Work with teams.\n- Monitor servers.\n- Manage access and security.\nQualifications:\n- Any background."}

        selected, context = select_bullet_rows(facts, job)

        self.assertEqual([row["fact_id"] for row in selected], ["generic", "one-word", "ambiguous"])
        self.assertFalse(context["adjusted"])
        self.assertEqual(context["omitted_bullet_fact_ids"], [])

    def test_responsibility_phrase_is_added_alongside_an_unrelated_exact_skill_match(self):
        from services.documents.tailor import select_bullet_rows

        facts = [
            {"fact_id": "duty", "kind": "responsibility", "text": "Maintain nightly database backups for four clinics."},
            {"fact_id": "skill", "kind": "experience", "text": "Configured Kubernetes clusters."},
            {"fact_id": "other", "kind": "project", "text": "Coordinated a server refresh."},
        ]
        job = {"skills": ["Kubernetes"], "description":
            "Responsibilities:\n- Maintain nightly database backups for four clinics."}

        selected, context = select_bullet_rows(facts, job)

        self.assertEqual([row["fact_id"] for row in selected], ["duty", "skill"])
        self.assertEqual(context["mode"], "EXACT_TARGET_SKILL_AND_RESPONSIBILITY_PHRASES")
        self.assertEqual(context["selected_reasons"]["duty"], ["EXACT_TARGET_RESPONSIBILITY_PHRASE"])
        self.assertEqual(context["selected_reasons"]["skill"], ["EXACT_TARGET_SKILL"])


class ResponsibilityPhraseDurabilityTests(unittest.TestCase):
    setUp = fixtures.DocumentProvenanceTests.setUp
    tearDown = fixtures.DocumentProvenanceTests.tearDown

    def test_exact_pair_persists_phrase_selection_and_comparison_after_reopen(self):
        source = PROFILE["facts"][0]
        facts = [
            dict(source, id="nightly-duty", kind="responsibility",
                 text="Maintain nightly database backups for four clinics."),
            dict(source, id="unrelated-duty", kind="experience", text="Coordinated a server refresh."),
        ]
        self.store.save_profile(dict(PROFILE, facts=facts))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job = self.store.jobs()[0]
        description = ("Systems Administrator\nResponsibilities:\n"
            "- Maintain nightly database backups for four clinics.\n"
                       "Qualifications:\n- Experience with SQL.")
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == job["id"]).values(
                skills=["Kubernetes"], description=description))
        application_id = prepare(self.store, job["id"], "fixture", self.root)
        prepare(self.store, job["id"], "fixture", self.root, tailor_resume=True)
        rows = application_documents(self.store, application_id)
        base = next(row for row in rows if row["kind"] == "resume" and row["version"] == 1)
        target = next(row for row in rows if row["kind"] == "resume" and row["version"] == 2)
        self.assertEqual(target["fact_ids"], ["nightly-duty"])
        adjustment = target["generation_context"]["bullet_adjustment"]
        self.assertEqual(adjustment["matched_responsibilities"]["nightly-duty"],
                         ["maintain nightly database backups for four clinics"])
        self.assertEqual(adjustment["omitted_bullet_fact_ids"], ["unrelated-duty"])
        profile_before = self.store.profile("fixture", target["profile_version"])

        reopened = type(self.store)(self.store.url)
        try:
            history = application_documents(reopened, application_id)
            saved = next(row for row in history if row["id"] == target["id"])
            self.assertTrue(saved["responsibility_context_valid"])
            self.assertEqual(saved["generation_context"]["bullet_adjustment"], adjustment)
            self.assertEqual(saved["fact_ids"], ["nightly-duty"])
            self.assertEqual(reopened.profile("fixture", target["profile_version"]), profile_before)
            result = compare_resumes(reopened, self.root, application_id, base["id"], target["id"])
            self.assertEqual([row["fact_id"] for row in result["omitted"]], ["unrelated-duty"])
            self.assertEqual(result["added"], [])
            tampered = dict(saved, generation_context={**saved["generation_context"],
                "bullet_adjustment": {**adjustment, "matched_responsibilities":{"nightly-duty":[]}}})
            with self.assertRaises(ComparisonUnavailable):
                tailored_context(tampered, [dict(row) for row in saved["facts_used"] + [
                    dict(source, fact_id="unrelated-duty", kind="experience", text="Coordinated a server refresh.")]])
            stale_target=dict(saved,job_id='different-job')
            with self.assertRaises(ComparisonUnavailable):
                tailored_context(stale_target, [dict(row) for row in saved["facts_used"] + [
                    dict(source, fact_id="unrelated-duty", kind="experience", text="Coordinated a server refresh.")]])
            stale_phrases=dict(saved,generation_context={**saved['generation_context'],
                'bullet_adjustment':{**adjustment,'target_responsibilities':['administer entirely different systems']}})
            with self.assertRaises(ComparisonUnavailable):
                tailored_context(stale_phrases, [dict(row) for row in saved["facts_used"] + [
                    dict(source, fact_id="unrelated-duty", kind="experience", text="Coordinated a server refresh.")]])
            from services.documents.history import application_documents as read_history
            with reopened.engine.begin() as connection:
                connection.execute(update(m.documents).where(m.documents.c.id==target['id']).values(
                    generation_context=tampered['generation_context']))
            rejected=next(row for row in read_history(reopened,application_id) if row['id']==target['id'])
            self.assertFalse(rejected['responsibility_context_valid'])
        finally:
            reopened.engine.dispose()
