"""Deterministic advisory job-risk signals preserve evidence and unknowns."""
from copy import deepcopy
import unittest

from services.discovery.job_risk_signals import evaluate_job_risk


def _job(**overrides):
    job = {
        "id": "job-current",
        "title": "Systems Administrator",
        "company": "Aster Labs",
        "company_id": "company-aster",
        "company_domain": "aster.example",
        "company_identity": "VERIFIED",
        "description": "Maintain production Linux systems and document changes.",
        "application_url": "https://jobs.aster.example/apply/role-1",
        "attributes": {
            "salary_min": 90_000,
            "salary_max": 120_000,
            "salary_period": "YEAR",
            "salary_currency": "USD",
        },
        "field_provenance": {
            "salary_min": {"source": "EMPLOYER_POSTING", "confidence": 0.94},
            "salary_max": {"source": "EMPLOYER_POSTING", "confidence": 0.94},
            "description": {"source": "JOB_POSTING", "confidence": 0.98},
            "application_url": {"source": "JOB_POSTING", "confidence": 0.98},
            "company_identity": {"source": "VERIFIED_COMPANY_DOMAIN", "confidence": 0.99},
        },
    }
    job.update(overrides)
    return job


class JobRiskSignalTests(unittest.TestCase):
    def test_unrealistic_salary_is_advisory_and_cites_salary_provenance(self):
        job = _job(attributes={
            "salary_min": 18_000_000,
            "salary_max": 22_000_000,
            "salary_period": "YEAR",
            "salary_currency": "USD",
        })

        result = evaluate_job_risk(job)

        warning = next(row for row in result["warnings"]
                       if row["code"] == "UNREALISTIC_SALARY")
        self.assertEqual(result["label"], "Potential Risk")
        self.assertEqual(warning["level"], "ADVISORY")
        self.assertEqual(warning["evidence"]["field"], "salary_max")
        self.assertEqual(warning["evidence"]["source"], "EMPLOYER_POSTING")
        self.assertEqual(warning["evidence"]["confidence"], 0.94)
        self.assertIn("22,000,000", warning["evidence"]["text"])
        self.assertNotIn("fraudulent", warning["message"].casefold())

    def test_payment_request_and_messaging_app_only_interview_keep_exact_text_evidence(self):
        description = (
            "Candidates must pay a $250 equipment fee before onboarding. "
            "The interview is exclusively via Telegram."
        )
        job = _job(description=description)

        result = evaluate_job_risk(job)

        by_code = {row["code"]: row for row in result["warnings"]}
        self.assertIn("PAYMENT_REQUEST", by_code)
        self.assertIn("MESSAGING_APP_ONLY_INTERVIEW", by_code)
        for code in ("PAYMENT_REQUEST", "MESSAGING_APP_ONLY_INTERVIEW"):
            evidence = by_code[code]["evidence"]
            self.assertEqual(evidence["field"], "description")
            self.assertEqual(evidence["source"], "JOB_POSTING")
            self.assertEqual(evidence["confidence"], 0.98)
            self.assertTrue(evidence["text"])
        self.assertIn("equipment fee", by_code["PAYMENT_REQUEST"]["evidence"]["text"])
        self.assertIn("exclusively via Telegram", by_code[
            "MESSAGING_APP_ONLY_INTERVIEW"]["evidence"]["text"])

    def test_suspicious_application_domain_is_sanitized_and_has_route_provenance(self):
        job = _job(
            application_url="https://192.0.2.42/apply/role?token=private#form",
        )

        result = evaluate_job_risk(job)

        warning = next(row for row in result["warnings"]
                       if row["code"] == "SUSPICIOUS_DOMAIN")
        self.assertEqual(warning["evidence"]["field"], "application_url")
        self.assertEqual(warning["evidence"]["source"], "JOB_POSTING")
        self.assertEqual(warning["evidence"]["confidence"], 0.98)
        self.assertIn("192.0.2.42", warning["evidence"]["text"])
        self.assertNotIn("private", repr(result))

    def test_unknown_employer_identity_is_explicit_and_not_inferred_from_name(self):
        job = _job(company_identity="UNKNOWN", field_provenance={})

        result = evaluate_job_risk(job)

        warning = next(row for row in result["warnings"]
                       if row["code"] == "EMPLOYER_IDENTITY_UNKNOWN")
        self.assertEqual(warning["evidence"], {
            "field": "company_identity",
            "text": "UNKNOWN",
            "source": "UNKNOWN",
            "confidence": None,
        })
        self.assertEqual(result["signals_by_code"]["EMPLOYER_IDENTITY_UNKNOWN"]["state"], "WARN")

    def test_exact_long_description_reuse_only_warns_for_distinct_known_employers(self):
        description = (
            "This position operates a large production environment with documented "
            "change control, incident response, capacity planning, scheduled maintenance, "
            "secure access reviews, and clear service ownership across several teams. "
            "The administrator maintains Linux and Windows systems, improves monitoring, "
            "coordinates recovery exercises, and records operational procedures for review."
        )
        job = _job(description=description)
        unrelated = _job(
            id="job-other", company="Beryl Systems", company_id="company-beryl",
            company_domain="beryl.example", description=description,
        )

        result = evaluate_job_risk(job, comparison_jobs=[unrelated])

        warning = next(row for row in result["warnings"]
                       if row["code"] == "TEXT_REUSED_ACROSS_EMPLOYERS")
        self.assertEqual(warning["evidence"]["field"], "description")
        self.assertEqual(warning["evidence"]["source"], "JOB_POSTING")
        self.assertEqual(warning["evidence"]["confidence"], 0.98)
        self.assertEqual(warning["evidence"]["compared_job_id"], "job-other")

        same_employer = _job(id="job-same-employer", description=description)
        short_generic = _job(id="job-short", description="We value teamwork.")
        self.assertNotIn("TEXT_REUSED_ACROSS_EMPLOYERS", {
            row["code"] for row in evaluate_job_risk(job, comparison_jobs=[same_employer])["warnings"]
        })
        self.assertNotIn("TEXT_REUSED_ACROSS_EMPLOYERS", {
            row["code"] for row in evaluate_job_risk(job, comparison_jobs=[short_generic])["warnings"]
        })

    def test_missing_evidence_remains_unknown_and_clear_posting_has_no_risk_label(self):
        job = {
            "id": "job-unknown",
            "company": "Aster Labs",
            "company_identity": "VERIFIED",
            "description": "",
            "attributes": {},
        }
        original = deepcopy(job)

        result = evaluate_job_risk(job)

        self.assertIsNone(result["label"])
        self.assertEqual(job, original)
        self.assertEqual(result["warnings"], [])
        unknown_codes = {
            "UNREALISTIC_SALARY", "PAYMENT_REQUEST", "MESSAGING_APP_ONLY_INTERVIEW",
            "SUSPICIOUS_DOMAIN", "TEXT_REUSED_ACROSS_EMPLOYERS",
        }
        for code in unknown_codes:
            self.assertEqual(result["signals_by_code"][code]["state"], "UNKNOWN")
        self.assertEqual(result["signals_by_code"]["EMPLOYER_IDENTITY_UNKNOWN"]["state"], "CLEAR")

        clear = evaluate_job_risk(_job())
        self.assertIsNone(clear["label"])
        self.assertEqual(clear["warnings"], [])

    def test_payment_negation_and_nonexclusive_chat_channel_do_not_warn(self):
        description = (
            "We never ask applicants to pay a fee. Interviews are available by email "
            "or video call; our team also uses WhatsApp for ordinary scheduling."
        )
        result = evaluate_job_risk(_job(description=description))
        codes = {row["code"] for row in result["warnings"]}
        self.assertNotIn("PAYMENT_REQUEST", codes)
        self.assertNotIn("MESSAGING_APP_ONLY_INTERVIEW", codes)

    def test_later_payment_request_is_not_hidden_by_an_earlier_negated_fee(self):
        job = _job(description=(
            "We never charge applicants an application fee. Applicants must pay an "
            "equipment fee before onboarding."
        ))

        result = evaluate_job_risk(job)

        self.assertIn("PAYMENT_REQUEST", {row["code"] for row in result["warnings"]})

    def test_suspicious_listing_domain_is_checked_when_application_route_is_unknown(self):
        job = _job(
            application_url=None,
            listing_url="https://tinyurl.com/role?token=private",
        )

        result = evaluate_job_risk(job)

        warning = next(row for row in result["warnings"]
                       if row["code"] == "SUSPICIOUS_DOMAIN")
        self.assertEqual(warning["evidence"]["field"], "listing_url")
        self.assertEqual(warning["evidence"]["text"], "tinyurl.com")
        self.assertNotIn("private", repr(result))

    def test_messaging_app_only_warning_accepts_app_before_exclusivity_marker(self):
        result = evaluate_job_risk(_job(description=(
            "All interviews take place through WhatsApp exclusively."
        )))
        self.assertIn(
            "MESSAGING_APP_ONLY_INTERVIEW",
            {row["code"] for row in result["warnings"]},
        )


if __name__ == "__main__":
    unittest.main()
