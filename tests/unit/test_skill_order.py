"""Skill relevance must never move responsibilities between roles."""
import unittest
from services.documents import tailor
from services.documents.tailor import order_skills, emphasis


class SkillOrderTests(unittest.TestCase):
    def select_summary_rows(self,rows,job):
        implementation=getattr(tailor,'select_summary_rows',lambda rows,job:(rows,None))
        return implementation(rows,job)

    def test_summary_selection_uses_only_exact_skills_and_preserves_fact_text(self):
        rows=[
            {'fact_id':'summary-linux','kind':'summary','text':'Linux administrator with Python automation experience.'},
            {'fact_id':'summary-sales','kind':'summary','text':'Experienced retail sales associate.'},
            {'fact_id':'skill-python','kind':'skill','text':'Python'},
        ]
        selected,context=self.select_summary_rows(rows,{'skills':['Python']})
        self.assertEqual([row['fact_id'] for row in selected],['summary-linux','skill-python'])
        self.assertEqual([row['text'] for row in selected],[rows[0]['text'],rows[2]['text']])
        self.assertEqual(context,{'mode':'EXACT_TARGET_SKILL_SUMMARY_FACTS',
            'adjusted':True,
            'original_summary_fact_ids':['summary-linux','summary-sales'],
            'selected_summary_fact_ids':['summary-linux'],
            'omitted_summary_fact_ids':['summary-sales'],
            'matched_skills':{'summary-linux':['Python'],'summary-sales':[]}})

    def test_summary_falls_back_to_original_when_no_summary_fact_matches(self):
        rows=[
            {'fact_id':'summary','kind':'summary','text':'Pythonic approach to customer service.'},
            {'fact_id':'skill','kind':'skill','text':'Python'},
        ]
        selected,context=self.select_summary_rows(rows,{'skills':['Python']})
        self.assertEqual(selected,rows)
        self.assertFalse(context['adjusted'])
        self.assertEqual(context['selected_summary_fact_ids'],['summary'])
        self.assertEqual(context['omitted_summary_fact_ids'],[])

    def test_summary_selection_uses_exact_target_responsibility_when_no_skill_matches(self):
        duty='Manage backup systems across regional offices and document recovery plans'
        rows=[
            {'fact_id':'summary-backups','kind':'summary','text':f'Experienced administrator. {duty}.'},
            {'fact_id':'summary-sales','kind':'summary','text':'Experienced retail sales associate.'},
            {'fact_id':'skill-python','kind':'skill','text':'Python'},
        ]
        job={'id':'job-1','title':'Infrastructure manager','skills':['Python'],
             'description':f'Responsibilities:\n- {duty}\nRequirements:\n- Five years of experience.'}
        selected,context=self.select_summary_rows(rows,job)

        self.assertEqual([row['fact_id'] for row in selected],['summary-backups','skill-python'])
        self.assertEqual([row['text'] for row in selected],[rows[0]['text'],rows[2]['text']])
        self.assertEqual(context['mode'],'EXACT_TARGET_RESPONSIBILITY_PHRASE_SUMMARY_FACTS')
        self.assertTrue(context['adjusted'])
        self.assertEqual(context['selected_summary_fact_ids'],['summary-backups'])
        self.assertEqual(context['omitted_summary_fact_ids'],['summary-sales'])
        self.assertEqual(context['matched_responsibilities'],{'summary-backups':[duty.casefold()],
            'summary-sales':[]})
        self.assertEqual(context['target_responsibilities'],[duty.casefold()])
        self.assertEqual(context['source_fact_fingerprints']['summary-backups'],
            tailor.source_fact_fingerprint({**rows[0],'text':rows[0]['text']}))

    def test_summary_responsibility_near_match_keeps_all_summary_facts(self):
        rows=[
            {'fact_id':'summary-paraphrase','kind':'summary',
             'text':'Coordinated backup services for offices and wrote recovery procedures.'},
            {'fact_id':'summary-other','kind':'summary','text':'Experienced retail sales associate.'},
        ]
        job={'id':'job-2','title':'Infrastructure manager','skills':['Python'],
             'description':'Responsibilities:\n- Manage backup systems across regional offices and document recovery plans'}
        selected,context=self.select_summary_rows(rows,job)

        self.assertEqual(selected,rows)
        self.assertFalse(context['adjusted'])
        self.assertEqual(context['selected_summary_fact_ids'],['summary-paraphrase','summary-other'])
        self.assertEqual(context['omitted_summary_fact_ids'],[])

    def test_only_skill_slots_move_and_all_text_is_unchanged(self):
        rows=[{'fact_id':str(i),'kind':kind,'text':text} for i,(kind,text) in enumerate([
            ('skill','SQL'),('experience','Example | Engineer | 2020-2024'),
            ('responsibility','Maintained services.'),('skill','PowerShell'),
            ('technology_experience','Windows Server at Example'),('skill','Windows Server')])]
        ordered,context=order_skills(rows,{'id':'job','title':'Administrator','skills':['PowerShell','Windows Server']})
        self.assertEqual([r['fact_id'] for r in ordered],['3','1','2','5','4','0'])
        self.assertEqual({r['fact_id']:r['text'] for r in ordered},{r['fact_id']:r['text'] for r in rows})
        self.assertEqual(context['matched_skills'],{'0':[],'3':['PowerShell'],'4':['Windows Server'],'5':['Windows Server']})
        self.assertEqual(context['original_fact_ids'],['0','1','2','3','4','5'])
        self.assertEqual(context['ordered_fact_ids'],['3','1','2','5','4','0'])

    def test_aliases_boundaries_and_unmatched_skills_preserve_stable_order(self):
        rows=[{'fact_id':str(i),'kind':'skill','text':text} for i,text in enumerate(['JavaScript','Postgres','Python','SQL'])]
        ordered,context=order_skills(rows,{'id':'job','title':'Engineer','skills':['Java','PostgreSQL','SQL']})
        self.assertEqual([r['text'] for r in ordered],['Postgres','SQL','JavaScript','Python'])
        self.assertEqual(context['matched_skills']['0'],[])
        unchanged,_=order_skills(rows,{'id':'job','title':'Engineer','skills':[]})
        self.assertEqual(unchanged,rows)

    def test_bullet_emphasis_uses_only_exactly_matched_approved_responsibility_facts(self):
        rows=[
            {'fact_id':'python-bullet','kind':'responsibility','text':'Built Python services.'},
            {'fact_id':'unmatched-bullet','kind':'responsibility','text':'Maintained COBOL batch jobs.'},
            {'fact_id':'python-skill','kind':'skill','text':'Python'},
            {'fact_id':'python-tech','kind':'technology_experience','text':'Python at work'},
        ]
        job={'id':'job','title':'Engineer','skills':['Python']}
        self.assertEqual(emphasis(rows,job),['python-skill','python-tech'])
        self.assertEqual(emphasis(rows,job,include_responsibilities=True),
                         ['python-bullet','python-skill','python-tech'])
