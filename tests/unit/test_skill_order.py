"""Skill relevance must never move responsibilities between roles."""
import unittest
from services.documents.tailor import order_skills


class SkillOrderTests(unittest.TestCase):
    def test_only_skill_slots_move_and_all_text_is_unchanged(self):
        rows=[{'fact_id':str(i),'kind':kind,'text':text} for i,(kind,text) in enumerate([
            ('skill','SQL'),('experience','Example | Engineer | 2020-2024'),
            ('responsibility','Maintained services.'),('skill','PowerShell'),
            ('technology_experience','Windows Server at Example'),('skill','Windows Server')])]
        ordered,context=order_skills(rows,{'id':'job','title':'Administrator','skills':['PowerShell','Windows Server']})
        self.assertEqual([r['fact_id'] for r in ordered],['3','1','2','5','4','0'])
        self.assertEqual({r['fact_id']:r['text'] for r in ordered},{r['fact_id']:r['text'] for r in rows})
        self.assertEqual(context['matched_skills'],{'0':[],'3':['PowerShell'],'5':['Windows Server']})
        self.assertEqual(context['original_fact_ids'],['0','1','2','3','4','5'])
        self.assertEqual(context['ordered_fact_ids'],['3','1','2','5','4','0'])

    def test_aliases_boundaries_and_unmatched_skills_preserve_stable_order(self):
        rows=[{'fact_id':str(i),'kind':'skill','text':text} for i,text in enumerate(['JavaScript','Postgres','Python','SQL'])]
        ordered,context=order_skills(rows,{'id':'job','title':'Engineer','skills':['Java','PostgreSQL','SQL']})
        self.assertEqual([r['text'] for r in ordered],['Postgres','SQL','JavaScript','Python'])
        self.assertEqual(context['matched_skills']['0'],[])
        unchanged,_=order_skills(rows,{'id':'job','title':'Engineer','skills':[]})
        self.assertEqual(unchanged,rows)
