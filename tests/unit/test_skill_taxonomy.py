import unittest

from services.discovery.skill_taxonomy import (
    canonical_skill_id,
    extract_skills,
    normalize_skill_list,
)


class SkillTaxonomyTests(unittest.TestCase):
    def test_aliases_map_to_canonical_ids_and_preserve_original_wording(self):
        data = extract_skills("Manage Microsoft Active Directory, M365, MECM, VMware and Rubrik.")
        self.assertEqual(
            data["ids"],
            ["ACTIVE_DIRECTORY", "MICROSOFT_365", "MECM", "VMWARE", "RUBRIK"],
        )
        originals = {row["id"]: row["original"] for row in data["mentions"]}
        self.assertEqual(originals["ACTIVE_DIRECTORY"], "Microsoft Active Directory")
        self.assertEqual(originals["MICROSOFT_365"], "M365")

    def test_short_aliases_avoid_common_word_false_positives(self):
        self.assertNotIn("ACTIVE_DIRECTORY", extract_skills("Create an ad for the role.")["ids"])
        self.assertIn("ACTIVE_DIRECTORY", extract_skills("Administer AD and Group Policy.")["ids"])
        self.assertNotIn("GO", extract_skills("We go to production weekly.")["ids"])
        self.assertIn("GO", extract_skills("Build services in Go and Python.")["ids"])

    def test_search_profile_skill_values_accept_aliases_or_ids(self):
        self.assertEqual(
            normalize_skill_list(["M365", "ACTIVE_DIRECTORY", "Office 365", "Rubrik"]),
            ["MICROSOFT_365", "ACTIVE_DIRECTORY", "RUBRIK"],
        )
        self.assertEqual(canonical_skill_id("SCCM"), "MECM")
        with self.assertRaisesRegex(ValueError, "Unknown normalized skill"):
            normalize_skill_list(["ImaginaryTechnology"])


if __name__ == "__main__":
    unittest.main()
