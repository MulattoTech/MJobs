"""Team 17 adversarial browser coverage for public provider review flows."""
import unittest
from unittest.mock import patch

from tests.browser import test_lever_form_review as lever_flow


# This markup is only returned by the synthetic public-page transport in the
# connected Lever review test. It is never navigated to or submitted to a live
# provider. The overlay and hidden conditional field model a dynamic sign-in
# gate appearing over an otherwise familiar contact form.
ADVERSARIAL_FORM = b"""<!doctype html>
<html><body>
<form>
  <label for="first_name">First name</label>
  <input id="first_name" name="first_name" type="text" value="PRIVATE-DECOY-APPLICANT">
  <label for="gender">Gender</label>
  <select id="gender" name="gender"><option value="private">PRIVATE-DECOY-GENDER</option></select>
  <section hidden>
    <label for="visa_sponsorship">Will you need sponsorship?</label>
    <input id="visa_sponsorship" name="visa_sponsorship" type="text" value="PRIVATE-DECOY-ANSWER">
    <input id="dynamic_response" name="dynamic_response" type="not-a-real-input-type">
  </section>
</form>
<div role="dialog" aria-modal="true" style="position:fixed;inset:0;z-index:9999;background:white">
  <h1>Sign in to continue</h1>
  <label for="overlay_password">Password</label>
  <input id="overlay_password" name="password" type="password" value="PRIVATE-DECOY-PASSWORD">
</div>
</body></html>"""


class FleetT17ProviderAdversarialBrowserTests(unittest.TestCase):
    def test_overlaid_dynamic_provider_form_stays_review_only_and_reads_back(self):
        # Reuse the existing connected route -> public-review API -> durable
        # worker -> browser reload path. Only the synthetic provider response
        # changes; provider adapter unit behavior is not duplicated here.
        harness = lever_flow.LeverFormReviewBrowserTests(
            "test_persisted_lever_route_inspects_safely_and_reads_back_after_reload"
        )
        private_markers = (*lever_flow.PRIVATE_MARKERS, b"PRIVATE-DECOY-ANSWER",
                           b"PRIVATE-DECOY-PASSWORD")
        with patch.object(lever_flow, "FORM_FIXTURE", ADVERSARIAL_FORM), \
                patch.object(lever_flow, "PRIVATE_MARKERS", private_markers):
            harness._assert_persisted_region_review(lever_flow.LEVER_ROUTE)


if __name__ == "__main__":
    unittest.main()
