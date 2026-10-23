import unittest

from services.ats_detection.detector import detect_ats


class ATSDetectionTests(unittest.TestCase):
    def test_invalid_or_deceptive_direct_and_redirect_hosts_are_not_evidence(self):
        for url in (
            "https://jobs.lever.co.attacker.example/acme",
            "https://notlever.co/acme",
            "https://jobs.lever.co@attacker.example/acme",
            "https://user:password@jobs.lever.co/acme",
            "https://jobs.lever.co:invalid/acme",
            "https://jobs.lever.co:65536/acme",
            "https://[invalid/acme",
            "javascript://jobs.lever.co/acme",
            "file://jobs.lever.co/acme",
        ):
            for redirect in (False, True):
                with self.subTest(url=url, redirect=redirect):
                    result = (detect_ats("https://acme.example/careers", redirect_chain=[url])
                              if redirect else detect_ats(url))
                    self.assertEqual(result.status, "NO_MATCH")
                    self.assertIsNone(result.provider)
                    self.assertEqual(result.confidence, 0.0)
                    self.assertEqual(result.candidates, ())
                    self.assertFalse(result.application_supported)

    def test_regional_lever_board_links_identify_custom_careers_pages(self):
        for host in ("jobs.lever.co", "jobs.eu.lever.co"):
            for tag in ("a", "iframe"):
                with self.subTest(host=host, tag=tag):
                    attribute = "href" if tag == "a" else "src"
                    result = detect_ats("https://acme.example/careers",
                                        f'<{tag} {attribute}="https://{host}/acme"></{tag}>')
                    self.assertEqual(result.status, "MATCH")
                    self.assertEqual(result.provider, "lever")
                    self.assertIn("BOARD_LINK", result.evidence)
                    self.assertFalse(result.application_supported)

    def test_same_lever_slug_in_different_regions_is_not_one_board(self):
        result = detect_ats("https://acme.example/careers",
                            '<a href="https://jobs.lever.co/acme">Global</a>'
                            '<a href="https://jobs.eu.lever.co/acme">EU</a>')
        self.assertEqual(result.status, "NO_MATCH")
        self.assertIsNone(result.provider)
        self.assertFalse(result.application_supported)

    def test_duplicate_weak_paths_do_not_raise_confidence_to_a_match(self):
        result = detect_ats("https://acme.example/recruiting/",
                            '<form action="/recruiting/"></form>' * 20,
                            redirect_chain=["https://acme.example/recruiting/"] * 20)
        self.assertEqual(result.status, "NO_MATCH")
        self.assertEqual(result.candidates, ())
        self.assertFalse(result.application_supported)

    def test_repeated_configuration_cannot_resolve_conflicting_providers(self):
        result = detect_ats("https://acme.example/careers",
                            '<div data-ats-provider="Lever"></div>' * 20
                            + '<div data-ats-provider="Greenhouse"></div>')
        self.assertEqual(result.status, "AMBIGUOUS")
        self.assertIsNone(result.provider)
        self.assertEqual({item.provider for item in result.candidates}, {"lever", "greenhouse"})
        self.assertEqual(len({item.confidence for item in result.candidates}), 1)
        self.assertFalse(result.application_supported)

    def test_malformed_metadata_does_not_override_independent_board_evidence(self):
        result = detect_ats("https://acme.example/careers",
                            '<script type="application/ld+json">{"atsProvider":</script>'
                            '<a href="https://jobs.lever.co/acme">Jobs</a>')
        self.assertEqual(result.status, "MATCH")
        self.assertEqual(result.provider, "lever")
        self.assertEqual(result.evidence, ("BOARD_LINK",))
        self.assertFalse(result.application_supported)

    def test_deceptive_script_urls_do_not_establish_provider_evidence(self):
        for url in (
            "https://jobs.lever.co.attacker.example/widget.js",
            "https://attacker.example/assets/jobs.lever.co/widget.js",
            "https://attacker.example/widget.js?source=jobs.lever.co",
            "https://jobs.lever.co@attacker.example/widget.js",
            "https://attacker.example/widget.js#jobs.lever.co",
            "https://jobs.lever.co:invalid/widget.js",
            "javascript://jobs.lever.co/widget.js",
            "https://attacker.example/api.lever.co/v0/postings/acme",
        ):
            with self.subTest(url=url):
                result = detect_ats("https://acme.example/careers",
                                    f'<script src="{url}"></script>')
                self.assertEqual(result.status, "NO_MATCH")
                self.assertIsNone(result.provider)
                self.assertFalse(result.application_supported)

    def test_valid_provider_scripts_remain_evidence_without_authorizing_apply(self):
        for url in (
            "https://jobs.lever.co/acme/widget.js",
            "https://widget.lever.co/acme.js",
            "https://api.lever.co/v0/postings/acme?mode=json",
        ):
            with self.subTest(url=url):
                result = detect_ats("https://acme.example/careers",
                                    f'<script src="{url}"></script>')
                self.assertEqual(result.status, "MATCH")
                self.assertEqual(result.provider, "lever")
                self.assertIn("SCRIPT_URL", result.evidence)
                self.assertFalse(result.application_supported)

    def test_script_query_cannot_introduce_another_provider(self):
        result = detect_ats("https://acme.example/careers",
                            '<script src="https://jobs.lever.co/acme.js?next=ashbyhq.com"></script>')
        self.assertEqual(result.status, "MATCH")
        self.assertEqual(result.provider, "lever")
        self.assertEqual({item.provider for item in result.candidates}, {"lever"})
        self.assertFalse(result.application_supported)

    def test_official_greenhouse_links_and_iframes_are_structural_evidence(self):
        for markup in (
            '<a href="https://job-boards.greenhouse.io/acme">Open jobs</a>',
            '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme"></iframe>',
        ):
            with self.subTest(markup=markup):
                result = detect_ats('https://acme.example/careers', markup)
                self.assertEqual(result.status, 'MATCH')
                self.assertEqual(result.provider, 'greenhouse')
                self.assertIn('BOARD_LINK', result.evidence)
                self.assertFalse(result.application_supported)

    def test_untrusted_or_non_board_hyperlinks_do_not_identify_greenhouse(self):
        for url in (
            'https://greenhouse.io/about',
            'https://boards.greenhouse.io.attacker.example/acme',
            'http://boards.greenhouse.io/acme',
            'https://user@boards.greenhouse.io/acme',
            'https://boards.greenhouse.io:444/acme',
            'https://boards.greenhouse.io/acme%2fother',
            'https://boards.greenhouse.io/embed/job_board?for=acme&for=other',
        ):
            with self.subTest(url=url):
                self.assertEqual(detect_ats('https://acme.example/careers',
                    '<a href="' + url + '">Jobs</a>').status, 'NO_MATCH')

    def test_different_linked_boards_do_not_establish_unambiguous_evidence(self):
        result = detect_ats('https://acme.example/careers',
            '<a href="https://boards.greenhouse.io/acme">Jobs</a>'
            '<a href="https://boards.greenhouse.io/other">Other jobs</a>')
        self.assertEqual(result.status, 'NO_MATCH')

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
