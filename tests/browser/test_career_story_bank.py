"""Connected FIXTURE browser acceptance for profile-scoped career stories."""
from copy import deepcopy
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / "apps/web/dist/index.html").is_file(), "Build apps/web before browser acceptance")
class CareerStoryBankBrowserTests(unittest.TestCase):
    def test_fixture_story_create_revision_review_and_profile_scoped_reload(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-career-story-browser-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'stories.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")

            profile = deepcopy(PROFILE)
            profile["facts"] = [
                {
                    "id": "story-fact-approved",
                    "text": "Maintained synthetic Linux services for four test sites.",
                    "kind": "accomplishment",
                    "source": "Synthetic fixture work history",
                    "verified": True,
                    "user_approved": True,
                    "confidence": 100,
                },
                {
                    "id": "story-fact-unapproved",
                    "text": "Unverified synthetic cloud migration claim.",
                    "kind": "accomplishment",
                    "source": "Unreviewed synthetic note",
                    "verified": False,
                    "user_approved": False,
                    "confidence": 20,
                },
            ]
            store.save_profile(profile)
            other_profile = deepcopy(profile)
            other_profile.update({"id": "other", "name": "Other Synthetic Profile"})
            other_profile["facts"] = [dict(profile["facts"][0], id="other-approved-fact")]
            store.save_profile(other_profile)

            sock = bound_browser_socket()
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            app = create_app(
                store,
                "http://127.0.0.1:1",
                root,
                "redis://127.0.0.1:1/0",
                operation_mode="FIXTURE",
            )
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Career-story isolated API did not start")
                    time.sleep(0.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    external_requests = []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external_requests.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.goto(origin, wait_until="domcontentloaded")
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    self.open_story_bank(page)

                    bank = page.get_by_role("region", name="Career story bank", exact=True)
                    bank.get_by_role("button", name="New career story", exact=True).click()
                    self.fill_story(bank, "Synthetic service recovery", "Reduced repeat incidents")
                    bank.get_by_role(
                        "checkbox",
                        name="Link approved fact: Maintained synthetic Linux services for four test sites.",
                        exact=True,
                    ).check()
                    bank.get_by_role("checkbox", name="Allow use: Resume", exact=True).check()
                    with page.expect_response(
                        lambda response: response.url.endswith("/profiles/fixture/career-stories")
                        and response.request.method == "POST"
                    ) as create_response:
                        bank.get_by_role("button", name="Save as new revision", exact=True).click()
                    self.assertIn(create_response.value.status, (200, 201))
                    revision_one = bank.get_by_role("article", name="Revision 1", exact=True)
                    expect(revision_one).to_contain_text("Synthetic service recovery")
                    expect(revision_one).to_contain_text("STORY REVIEW STATUS: PENDING")
                    expect(revision_one).to_contain_text("UNAVAILABLE FOR REUSE")
                    expect(revision_one).to_contain_text("Synthetic fixture work history")

                    # A different saved profile must read back an empty, isolated bank.
                    page.evaluate("localStorage.setItem('mjobs-profile', 'other')")
                    page.reload(wait_until="domcontentloaded")
                    self.open_story_bank(page)
                    other_bank = page.get_by_role("region", name="Career story bank", exact=True)
                    expect(other_bank).to_contain_text("No career stories yet.")
                    expect(other_bank).not_to_contain_text("Synthetic service recovery")

                    # Reloading the original profile reads the saved revision from the API.
                    page.evaluate("localStorage.setItem('mjobs-profile', 'fixture')")
                    page.reload(wait_until="domcontentloaded")
                    self.open_story_bank(page)
                    bank = page.get_by_role("region", name="Career story bank", exact=True)
                    bank.get_by_role("button", name="Synthetic service recovery", exact=True).click()
                    revision_one = bank.get_by_role("article", name="Revision 1", exact=True)
                    expect(revision_one).to_contain_text("STORY REVIEW STATUS: PENDING")
                    expect(revision_one).to_contain_text("Situation")
                    expect(revision_one).to_contain_text("A synthetic service had recurring incidents.")

                    revision_one.get_by_role(
                        "button", name="Edit Revision 1 as new revision", exact=True
                    ).click()
                    bank.get_by_label("Result", exact=True).fill("Reduced repeat incidents")
                    with page.expect_response(
                        lambda response: response.url.endswith("/versions")
                        and response.request.method == "POST"
                    ) as revise_response:
                        bank.get_by_role("button", name="Save as new revision", exact=True).click()
                    self.assertIn(revise_response.value.status, (200, 201))

                    revision_one = bank.get_by_role("article", name="Revision 1", exact=True)
                    revision_two = bank.get_by_role("article", name="Revision 2", exact=True)
                    expect(revision_one).to_contain_text("STORY REVIEW STATUS: PENDING")
                    expect(revision_one).to_contain_text("Result")
                    expect(revision_one).to_contain_text("Documented the recurring fault pattern.")
                    expect(revision_two).to_contain_text("Reduced repeat incidents")
                    expect(revision_two).to_contain_text("STORY REVIEW STATUS: PENDING")
                    expect(revision_two).to_contain_text("UNAVAILABLE FOR REUSE")

                    revision_two.get_by_role(
                        "checkbox",
                        name="I reviewed Revision 2 and confirm it is accurate",
                        exact=True,
                    ).check()
                    with page.expect_response(
                        lambda response: response.url.endswith("/review")
                        and response.request.method == "POST"
                    ) as review_response:
                        revision_two.get_by_role(
                            "button", name="Review this revision", exact=True
                        ).click()
                    self.assertEqual(review_response.value.status, 200)
                    expect(revision_two).to_contain_text("STORY REVIEW STATUS: APPROVED")
                    expect(revision_two).to_contain_text("AVAILABLE FOR REUSE")
                    expect(revision_one).to_contain_text("STORY REVIEW STATUS: PENDING")
                    expect(revision_one).to_contain_text("UNAVAILABLE FOR REUSE")

                    # A later edit creates a separate pending revision; prior review does not transfer.
                    revision_two.get_by_role(
                        "button", name="Edit Revision 2 as new revision", exact=True
                    ).click()
                    bank.get_by_label("Action", exact=True).fill(
                        "Added a synthetic health check and a clear on-call runbook."
                    )
                    with page.expect_response(
                        lambda response: response.url.endswith("/versions")
                        and response.request.method == "POST"
                    ) as third_response:
                        bank.get_by_role("button", name="Save as new revision", exact=True).click()
                    self.assertIn(third_response.value.status, (200, 201))
                    revision_three = bank.get_by_role("article", name="Revision 3", exact=True)
                    expect(revision_two).to_contain_text("STORY REVIEW STATUS: APPROVED")
                    expect(revision_three).to_contain_text("STORY REVIEW STATUS: PENDING")
                    expect(revision_three).to_contain_text("UNAVAILABLE FOR REUSE")

                    # Revoking/changing its linked fact makes the saved revision stale.
                    changed_profile = deepcopy(profile)
                    changed_profile["facts"][0]["text"] = "Changed synthetic service history."
                    changed_profile["facts"][0]["verified"] = False
                    changed_profile["facts"][0]["user_approved"] = False
                    changed_profile["expected_version"] = store.profile("fixture")["version"]
                    store.save_profile(changed_profile)
                    page.reload(wait_until="domcontentloaded")
                    self.open_story_bank(page)
                    bank = page.get_by_role("region", name="Career story bank", exact=True)
                    bank.get_by_role("button", name="Synthetic service recovery", exact=True).click()
                    revision_two = bank.get_by_role("article", name="Revision 2", exact=True)
                    revision_three = bank.get_by_role("article", name="Revision 3", exact=True)
                    expect(revision_two).to_contain_text("STORY REVIEW STATUS: APPROVED")
                    expect(revision_two).to_contain_text("UNAVAILABLE FOR REUSE")
                    expect(revision_three).to_contain_text("FACT NOT VERIFIED")
                    expect(revision_three).to_contain_text(
                        "This fact changed after the saved revision; its link is stale."
                    )
                    revision_three.get_by_role(
                        "checkbox",
                        name="I reviewed Revision 3 and confirm it is accurate",
                        exact=True,
                    ).check()
                    with page.expect_response(
                        lambda response: response.url.endswith("/review")
                        and response.request.method == "POST"
                    ) as stale_review:
                        revision_three.get_by_role(
                            "button", name="Review this revision", exact=True
                        ).click()
                    self.assertEqual(stale_review.value.status, 422)
                    expect(bank.get_by_role("alert")).to_contain_text(
                        "Every selected fact must be verified, approved, and supported"
                    )
                    expect(revision_three).to_contain_text("STORY REVIEW STATUS: PENDING")
                    expect(revision_three).to_contain_text("UNAVAILABLE FOR REUSE")

                    # The same current unapproved fact is rejected on direct story creation.
                    rejected = context.request.post(
                        origin + "/profiles/fixture/career-stories",
                        data={
                            "kind": "STAR_STORY",
                            "title": "Unapproved synthetic claim",
                            "text": "A claim with no approved evidence.",
                            "fact_ids": ["story-fact-unapproved"],
                            "tags": [],
                            "allowed_uses": ["INTERVIEW_PREPARATION"],
                        },
                    )
                    self.assertEqual(rejected.status, 422)
                    self.assertIn(
                        rejected.json().get("detail", {}).get("code"),
                        {"FACTS_UNAVAILABLE", "FACT_NOT_APPROVED"},
                    )
                    self.assertFalse(external_requests, "FIXTURE browser attempted an external request")
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())

    @staticmethod
    def open_story_bank(page):
        navigation = page.get_by_role("navigation")
        tab = navigation.get_by_role(
            "button",
            name=re.compile(r"^(?:career stories|career story bank|story bank)$", re.IGNORECASE),
        )
        tab.click()
        expect(page.get_by_role("region", name="Career story bank", exact=True)).to_be_visible()

    @staticmethod
    def fill_story(bank, title, result):
        bank.get_by_label("Story title", exact=True).fill(title)
        bank.get_by_label("Situation", exact=True).fill("A synthetic service had recurring incidents.")
        bank.get_by_label("Task", exact=True).fill("Improve service recovery without changing user data.")
        bank.get_by_label("Action", exact=True).fill("Documented the recurring fault pattern.")
        bank.get_by_label("Result", exact=True).fill(result)


if __name__ == "__main__":
    unittest.main()
