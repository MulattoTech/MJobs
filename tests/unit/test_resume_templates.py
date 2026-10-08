"""Heading structure is fixed, versioned and independent of factual wording."""
import unittest
from services.documents.templates import (BASE_TEMPLATE, BASE_MODEL, TAILORED_TEMPLATE, TAILORED_MODEL,
    CONTRACTS, contract_role, resume_layout)
from services.documents.validation import validate_assertions
from services.documents.errors import PreparationError


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
