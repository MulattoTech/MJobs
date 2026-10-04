import unittest
from services.documents.uploaded_comparison import source_items,MAX_COMPARISON_ITEMS
from services.documents.comparison import ComparisonUnavailable


class UploadedComparisonUnitTests(unittest.TestCase):
    def test_occurrences_and_unrecognized_structure_are_preserved_without_guessed_summary(self):
        items,headings,uncertain=source_items('Unclassified sentence\nΔοκιμή\nΔοκιμή\nSkills:\nPython; SQL\nResponsibilities\n- Maintain backups')
        self.assertEqual(headings,{'skill','responsibility'})
        self.assertEqual([row['kind'] for row in items[:3]],['unclassified']*3)
        self.assertEqual([row['text'] for row in items[:3]],['Unclassified sentence','Δοκιμή','Δοκιμή'])
        self.assertEqual(len({row['fact_id'] for row in items}),len(items))
        self.assertEqual(items[-1]['text'],'Maintain backups')
        self.assertEqual(uncertain,set())

    def test_unknown_heading_cannot_inherit_a_recognized_section(self):
        for heading,line in [('Responsibilities','Maintain backups'),('Skills','Python'),('Summary','Experienced engineer')]:
            with self.subTest(heading=heading):
                items,headings,uncertain=source_items(f'{heading}\n{line}\nAwards\nEmployee of the month')
                self.assertEqual([row['kind'] for row in items[-2:]],['unclassified','unclassified'])
        items,headings,uncertain=source_items('Responsibilities\nMaintain backups\nCustom Section:\nOther content')
        self.assertEqual(items[-1]['kind'],'unclassified')
        self.assertEqual(uncertain,{'responsibility'})

    def test_comparison_work_is_bounded_before_alignment(self):
        with self.assertRaises(ComparisonUnavailable):source_items('line\n'*(MAX_COMPARISON_ITEMS+1))
