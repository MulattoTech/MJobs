import unittest

from services.ats_detection.detector import detect_ats


class ATSDetectionTests(unittest.TestCase):
    def test_recognizes_custom_careers_domain_from_embedded_greenhouse_board(self):
        result = detect_ats(
            "https://careers.acme.example/jobs",
            '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>',
        )

        self.assertEqual(result.status, "MATCH")
        self.assertEqual(result.provider, "greenhouse")
        self.assertEqual(result.visible_careers_url, "https://careers.acme.example/jobs")
        self.assertIn("SCRIPT_URL", result.evidence)
        self.assertGreaterEqual(result.confidence, 0.7)
        self.assertFalse(result.application_supported)

    def test_accepts_explicit_provider_configuration_and_structured_metadata(self):
        configured = detect_ats(
            "https://careers.acme.example/jobs",
            '<div data-ats-provider="Greenhouse"></div>',
        )
        metadata = detect_ats(
            "https://careers.acme.example/jobs",
            '<script type="application/ld+json">{"@type":"Organization","atsProvider":"Workday"}</script>',
        )

        self.assertEqual(configured.provider, "greenhouse")
        self.assertIn("EMBEDDED_CONFIGURATION", configured.evidence)
        self.assertEqual(metadata.provider, "workday")
        self.assertIn("STRUCTURED_METADATA", metadata.evidence)

    def test_uses_redirect_chain_and_vendor_url_structure(self):
        result = detect_ats(
            "https://careers.acme.example/jobs",
            "",
            redirect_chain=["https://acme.wd5.myworkdayjobs.com/External/job/US/Analyst_R123"],
        )

        self.assertEqual(result.status, "MATCH")
        self.assertEqual(result.provider, "workday")
        self.assertIn("REDIRECT_HOSTNAME", result.evidence)
        self.assertIn("URL_STRUCTURE", result.evidence)

    def test_conflicting_vendor_signals_require_review(self):
        result = detect_ats(
            "https://jobs.lever.co/acme",
            '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>',
        )

        self.assertEqual(result.status, "AMBIGUOUS")
        self.assertIsNone(result.provider)
        self.assertGreaterEqual(len(result.candidates), 2)

    def test_unknown_custom_domain_is_not_guessed(self):
        result = detect_ats("https://careers.acme.example/jobs", "<h1>Careers</h1>")

        self.assertEqual(result.status, "NO_MATCH")
        self.assertIsNone(result.provider)
        self.assertEqual(result.evidence, ())

    def test_generic_career_paths_and_visible_prose_are_not_ats_signatures(self):
        result = detect_ats(
            "https://careers.acme.example/careers/jobs",
            "<main>Read about Greenhouse jobs and Workday careers.</main>",
        )

        self.assertEqual(result.status, "NO_MATCH")

    def test_generic_vendor_words_in_classes_and_data_attributes_are_not_enough(self):
        result = detect_ats(
            "https://careers.acme.example/recruiting/",
            '<div class="paylocity-recruiting greenhouse-jobs">Careers</div>',
        )

        self.assertEqual(result.status, "NO_MATCH")

    def test_vendor_named_job_routes_need_independent_vendor_evidence(self):
        for url in (
            "https://careers.acme.example/recruiting/",
            "https://careers.acme.example/jobs/12345",
        ):
            with self.subTest(url=url):
                self.assertEqual(detect_ats(url).status, "NO_MATCH")

    def test_generic_form_action_paths_are_supporting_evidence_only(self):
        for action in ("/recruiting/", "/careers/jobs/"):
            with self.subTest(action=action):
                result = detect_ats(
                    "https://careers.acme.example/jobs",
                    f'<form action="{action}"><button>Apply</button></form>',
                )
                self.assertEqual(result.status, "NO_MATCH")

    def test_recognizes_all_declared_major_ats_hostname_families(self):
        examples = {
            "greenhouse": "https://boards.greenhouse.io/acme",
            "lever": "https://jobs.lever.co/acme",
            "ashby": "https://jobs.ashbyhq.com/acme",
            "smartrecruiters": "https://jobs.smartrecruiters.com/acme",
            "workable": "https://apply.workable.com/acme",
            "workday": "https://acme.myworkdayjobs.com/en-US/External",
            "jobvite": "https://jobs.jobvite.com/acme",
            "bamboohr": "https://acme.bamboohr.com/careers/list.php",
            "recruitee": "https://acme.recruitee.com/o/jobs",
            "teamtailor": "https://acme.teamtailor.com/jobs",
            "breezy": "https://acme.breezy.hr/",
            "icims": "https://careers-acme.icims.com/jobs",
            "oracle_taleo": "https://acme.taleo.net/careersection/",
            "successfactors": "https://acme.successfactors.eu/career",
            "dayforce": "https://acme.dayforcehcm.com/CandidatePortal",
            "adp_recruiting": "https://jobs.adp.com/acme",
            "jazzhr": "https://apply.jazz.co/acme",
            "applicantpro": "https://acme.applicantpro.com/jobs",
            "ukg": "https://acme.ultipro.com/careers",
            "paycom": "https://careers.paycomonline.net/",
            "paylocity": "https://careers.paylocity.com/acme/jobs",
            "neogov": "https://acme.governmentjobs.com/careers/acme",
        }

        for provider, url in examples.items():
            with self.subTest(provider=provider):
                result = detect_ats(url)
                self.assertEqual(result.status, "MATCH")
                self.assertEqual(result.provider, provider)
                self.assertIn("HOSTNAME", result.evidence)


if __name__ == "__main__":
    unittest.main()
