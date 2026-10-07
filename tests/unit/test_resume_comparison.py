import unittest
from services.documents.comparison import compare_facts


class ResumeComparisonUnitTests(unittest.TestCase):
    def test_user_reviewed_project_and_accomplishment_rewrites_are_source_bound(self):
        import hashlib
        from services.documents.tailor import apply_saved_user_rewrites, validate_user_rewrites
        from services.documents.errors import PreparationError

        facts = [
            {"fact_id": "project", "kind": "project",
             "text": "Built a local-first recovery harness for 3 environments in 2021.",
             "verified": True, "user_approved": True, "confidence": 95,
             "source": "User verified"},
            {"fact_id": "achievement", "kind": "accomplishment",
             "text": "Reduced recovery time by 25% in 2022.",
             "verified": True, "user_approved": True, "confidence": 95,
             "source": "User verified"},
        ]
        rewrites = [
            {"fact_id": "project", "text": "Created a recovery harness for 3 environments in 2021.", "reviewed": True},
            {"fact_id": "achievement", "text": "Cut recovery time by 25% in 2022.", "reviewed": True},
        ]

        rewritten, provenance = validate_user_rewrites(facts, ["project", "achievement"], rewrites)

        self.assertEqual([row["text"] for row in rewritten], [item["text"] for item in rewrites])
        self.assertEqual([row["fact_kind"] for row in provenance], ["project", "accomplishment"])
        self.assertEqual([row["source_text_sha256"] for row in provenance], [
            hashlib.sha256(row["text"].encode()).hexdigest() for row in facts
        ])
        self.assertEqual(apply_saved_user_rewrites(facts, ["project", "achievement"], provenance), rewritten)
        self.assertEqual([row["text"] for row in facts], [
            "Built a local-first recovery harness for 3 environments in 2021.",
            "Reduced recovery time by 25% in 2022.",
        ])
        with self.assertRaises(PreparationError) as unselected:
            validate_user_rewrites(facts, ["project"], [rewrites[1]])
        self.assertEqual(unselected.exception.code, "RESUME_REPHRASE_FACT_INVALID")

    def test_user_reviewed_summary_adjustment_is_source_bound_and_fact_safe(self):
        import hashlib
        from services.documents.tailor import apply_saved_user_rewrites, validate_user_rewrites
        from services.documents.errors import PreparationError

        facts = [{"fact_id": "summary", "kind": "summary",
                  "text": "Systems engineer with 12 years of Linux experience, 2012-2024.",
                  "verified": True, "user_approved": True, "confidence": 95,
                  "source": "User verified"}]
        rewritten, provenance = validate_user_rewrites(facts, ["summary"], [{
            "fact_id": "summary",
            "text": "Linux systems engineer with 12 years of experience, 2012-2024.",
            "reviewed": True,
        }])

        self.assertEqual(rewritten[0]["text"], "Linux systems engineer with 12 years of experience, 2012-2024.")
        self.assertEqual(provenance[0]["fact_kind"], "summary")
        self.assertEqual(provenance[0]["source_text_sha256"],
                         hashlib.sha256(facts[0]["text"].encode()).hexdigest())
        self.assertEqual(apply_saved_user_rewrites(facts, ["summary"], provenance), rewritten)
        self.assertEqual(facts[0]["text"], "Systems engineer with 12 years of Linux experience, 2012-2024.")

        with self.assertRaises(PreparationError) as review_error:
            validate_user_rewrites(facts, ["summary"], [{
                "fact_id": "summary", "text": "Linux systems engineer, 2012-2024.", "reviewed": False,
            }])
        self.assertEqual(review_error.exception.code, "RESUME_REPHRASE_REVIEW_REQUIRED")
        with self.assertRaises(PreparationError) as changed_error:
            validate_user_rewrites(facts, ["summary"], [{
                "fact_id": "summary", "text": "Linux systems engineer with 20 years, 2012-2024.", "reviewed": True,
            }])
        self.assertEqual(changed_error.exception.code, "RESUME_REPHRASE_FACTS_CHANGED")

    def test_user_rephrase_is_reviewed_source_bound_and_preserves_numeric_claims(self):
        import hashlib
        from services.documents.tailor import validate_user_rephrases
        from services.documents.errors import PreparationError

        facts = [{"fact_id": "duty", "kind": "responsibility",
                  "text": "Built Python services for 4 clinics in 2020-2022.",
                  "verified": True, "user_approved": True, "confidence": 95,
                  "source": "User verified"}]
        selected = ["duty"]
        rewritten, provenance = validate_user_rephrases(facts, selected, [{
            "fact_id": "duty",
            "text": "Developed Python services for 4 clinics in 2020-2022.",
            "reviewed": True,
        }])

        self.assertEqual(rewritten[0]["text"], "Developed Python services for 4 clinics in 2020-2022.")
        self.assertEqual(provenance[0]["fact_id"], "duty")
        self.assertEqual(provenance[0]["source_text_sha256"],
                         hashlib.sha256(facts[0]["text"].encode()).hexdigest())
        self.assertTrue(provenance[0]["reviewed"])
        self.assertEqual(facts[0]["text"], "Built Python services for 4 clinics in 2020-2022.")

        with self.assertRaises(PreparationError) as review_error:
            validate_user_rephrases(facts, selected, [{
                "fact_id": "duty", "text": "Developed Python services for 4 clinics in 2020-2022.",
                "reviewed": False,
            }])
        self.assertEqual(review_error.exception.code, "RESUME_REPHRASE_REVIEW_REQUIRED")
        with self.assertRaises(PreparationError) as changed_error:
            validate_user_rephrases(facts, selected, [{
                "fact_id": "duty", "text": "Developed Python services for 40 clinics in 2020-2022.",
                "reviewed": True,
            }])
        self.assertEqual(changed_error.exception.code, "RESUME_REPHRASE_FACTS_CHANGED")

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
