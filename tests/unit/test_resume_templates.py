"""Heading structure is fixed, versioned and independent of factual wording."""
import unittest
from services.documents.templates import (BASE_TEMPLATE, BASE_MODEL, TAILORED_TEMPLATE, TAILORED_MODEL,
    CONTRACTS, contract_role, resume_layout)
from services.documents.validation import validate_assertions
from services.documents.errors import PreparationError
from services.documents.resume_templates import (
    list_resume_templates,
    select_resume_template,
)


class ResumeTemplateTests(unittest.TestCase):
    def profile(self):
        return {'name':'Example','email':'example@example.invalid','facts':[
            dict(fact_id='a',kind='skill',text='Skills',verified=True,user_approved=True,confidence=100,source='synthetic'),
            dict(fact_id='b',kind='skill',text='Skills',verified=True,user_approved=True,confidence=100,source='synthetic'),
            dict(fact_id='c',kind='unknown',text='Arbitrary approved text',verified=True,user_approved=True,confidence=100,source='synthetic')]}

    def test_duplicate_heading_shaped_facts_are_preserved_and_validated_exactly(self):
        profile=self.profile();ids=['a','b','c']
        expected=['Example','example@example.invalid','Skills','Skills','Skills','Additional Information','Arbitrary approved text']
        for template in (BASE_TEMPLATE,TAILORED_TEMPLATE):
            self.assertEqual(resume_layout(profile,profile['facts'],template),(expected,{2,5}))
            validate_assertions('resume',expected,profile,ids,template=template)
            for invalid in (expected[:3]+expected[4:],expected+['Expert'],expected[:5]+['Experience']+expected[6:]):
                with self.subTest(template=template,invalid=invalid),self.assertRaises(PreparationError):
                    validate_assertions('resume',invalid,profile,ids,template=template)
        with self.assertRaises(PreparationError):
            validate_assertions('resume',expected,profile,['a','a','c'],template=BASE_TEMPLATE)

    def test_legacy_layout_and_model_dispatch_do_not_reinterpret_history(self):
        profile=self.profile()
        for template in ('verified-facts-v1','approved-skill-order-v1'):
            paragraphs,headings=resume_layout(profile,profile['facts'],template)
            self.assertEqual(paragraphs,['Example','example@example.invalid','Skills','Skills','Arbitrary approved text'])
            self.assertEqual(headings,set())
            validate_assertions('resume',paragraphs,profile,['a','b','c'],template=template)
            self.assertIsNotNone(contract_role(template,None,allow_legacy_unrecorded=True))
        self.assertEqual(contract_role(BASE_TEMPLATE,BASE_MODEL),'BASE')
        for template,model in ((BASE_TEMPLATE,None),(BASE_TEMPLATE,'deterministic-fact-copy-v1'),('invented',BASE_MODEL)):
            self.assertIsNone(contract_role(template,model,allow_legacy_unrecorded=True))
        with self.assertRaises(PreparationError):resume_layout(profile,profile['facts'],'invented')

    def test_responsibility_selection_has_immutable_v5_identity_and_preserves_v4_history(self):
        self.assertEqual((TAILORED_TEMPLATE, TAILORED_MODEL), (
            'approved-target-relevant-bullets-summary-skill-responsibility-and-bullet-emphasis-v5',
            'deterministic-target-relevant-bullets-summary-skill-responsibility-order-v4'))
        self.assertEqual(contract_role(TAILORED_TEMPLATE, TAILORED_MODEL), 'TAILORED')
        self.assertEqual(contract_role(
            'approved-target-relevant-bullets-summary-skill-and-bullet-emphasis-v4',
            'deterministic-target-relevant-bullets-summary-skill-order-v3'), 'TAILORED')
        self.assertEqual(contract_role(
            'approved-contact-target-relevant-bullets-summary-skill-and-bullet-emphasis-v4',
            'deterministic-contact-target-relevant-bullets-summary-skill-order-v3'), 'TAILORED')
        self.assertEqual(contract_role(
            'approved-contact-target-relevant-bullets-summary-skill-responsibility-and-bullet-emphasis-v5',
            'deterministic-contact-target-relevant-bullets-summary-skill-responsibility-order-v4'), 'TAILORED')
        self.assertIsNone(contract_role(TAILORED_TEMPLATE, 'deterministic-target-relevant-bullets-summary-skill-order-v3'))


class ResumeTemplateCatalogTests(unittest.TestCase):
    def test_catalog_contains_multiple_declarative_ats_safe_templates(self):
        templates = list_resume_templates()

        self.assertGreaterEqual(len(templates), 3)
        self.assertEqual(len({row['template_id'] for row in templates}), len(templates))
        for row in templates:
            with self.subTest(template_id=row['template_id']):
                self.assertIn(row['template_id'], CONTRACTS)
                self.assertTrue(row['ats_safe'])
                self.assertTrue(row['version'])
                self.assertEqual(row['layout']['column_count'], 1)
                self.assertFalse(row['layout']['graphics'])
                self.assertFalse(row['layout']['tables'])
                self.assertTrue(row['layout']['text_only'])
                self.assertEqual(len(row['layout']['section_order']),
                                 len(set(row['layout']['section_order'])))
                self.assertIn('experience', row['layout']['section_order'])
                self.assertNotIn('facts', row)

    def test_explicit_selection_returns_current_definition_without_mutable_aliases(self):
        catalog = list_resume_templates()
        requested = catalog[0]

        selected = select_resume_template(requested['template_id'], requested['version'])

        self.assertEqual(selected['template_id'], requested['template_id'])
        self.assertEqual(selected['version'], requested['version'])
        self.assertEqual(selected['selection_mode'], 'EXPLICIT')
        self.assertTrue(selected['preview_only'])
        selected['layout']['section_order'].append('invented-section')
        self.assertNotIn('invented-section', select_resume_template(
            requested['template_id'], requested['version'])['layout']['section_order'])

    def test_malformed_unknown_and_unversioned_selections_fail_closed(self):
        with self.assertRaises(PreparationError) as malformed:
            select_resume_template(['classic'], '1')
        self.assertEqual(malformed.exception.code, 'RESUME_TEMPLATE_SELECTION_INVALID')

        with self.assertRaises(PreparationError) as unknown:
            select_resume_template('unknown-template', '1')
        self.assertEqual(unknown.exception.code, 'RESUME_TEMPLATE_SELECTION_INVALID')

        with self.assertRaises(PreparationError) as unversioned:
            select_resume_template('verified-headings-v1', None)
        self.assertEqual(unversioned.exception.code, 'RESUME_TEMPLATE_VERSION_REQUIRED')

    def test_stale_template_version_is_rejected(self):
        current = list_resume_templates()[0]

        with self.assertRaises(PreparationError) as stale:
            select_resume_template(current['template_id'], 'previous-version')

        self.assertEqual(stale.exception.code, 'RESUME_TEMPLATE_VERSION_STALE')
