import unittest
from services.documents.comparison import compare_facts


class ResumeComparisonUnitTests(unittest.TestCase):
    def test_duplicate_unicode_text_uses_fact_identity_and_additions_do_not_fake_moves(self):
        one={'fact_id':'one','kind':'skill','text':'Δοκιμή'}
        two={**one,'fact_id':'two'}
        duty={'fact_id':'duty','kind':'responsibility','text':'Maintained services 2020-2024'}
        result=compare_facts([one,two,duty],[two,one,duty],{'one':['Example']})
        self.assertEqual([r['fact_id'] for r in result['reordered']],['two','one'])
        self.assertEqual(result['bullet_changes'],[])
        self.assertEqual(result['emphasized_skills'][0]['fact_id'],'one')
        inserted={'fact_id':'new','kind':'summary','text':'Approved summary'}
        result=compare_facts([one,two,duty],[inserted,one,two,duty],{})
        self.assertEqual(result['reordered'],[])
        self.assertEqual(result['summary_changes'][0]['after'],inserted)
