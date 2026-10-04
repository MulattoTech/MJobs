"""Connected browser acceptance against the actual local Compose deployment."""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
from urllib.parse import urlsplit

from docx import Document
from playwright.sync_api import sync_playwright, expect


def run(base_url, evidence_path):
    artifacts = Path("outputs/ui-e2e") / str(urlsplit(base_url).port)
    artifacts.mkdir(parents=True, exist_ok=True)
    actions = []
    with sync_playwright() as browser_api:
        browser = browser_api.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1050}, accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(45000)
        failures = []
        page.on("pageerror", lambda failure: failures.append(str(failure)))

        def navigation(name):
            page.get_by_role("navigation").get_by_role("button", name=name).click()

        def request(path):
            response = context.request.get(base_url + path)
            assert response.ok, (path, response.status)
            return response.json()

        def save_profile():
            page.get_by_role("button", name="Save profile", exact=True).click()
            expect(page.get_by_role("status").filter(has_text="Profile saved")).to_be_visible()

        def prepare():
            page.get_by_role("button", name="Prepare documents", exact=True).click()
            expect(page.get_by_role("heading", name="Application details", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="Run dry-run", exact=True)).to_be_enabled()

        def select_data_job():
            navigation("Jobs")
            page.get_by_label("Search jobs").fill("data")
            card = page.get_by_role("button").filter(has=page.get_by_role("heading", name="Data Engineer", exact=True))
            expect(card).to_have_count(1)
            card.click()
            expect(page.get_by_role("heading", name="Why this match?", exact=True)).to_be_visible()

        try:
            page.goto(base_url, wait_until="networkidle")
            expect(page.get_by_role("heading", name="Find your next opportunity.")).to_be_visible()
            page.get_by_role("button", name="Load fictional demo", exact=True).click()
            expect(page.get_by_role("status").filter(has_text="Fictional demo loaded")).to_be_visible()
            profile_id = page.get_by_label("Active profile").input_value()
            assert profile_id.startswith("demo-")
            select_data_job()
            expect(page.locator(".big-score")).to_have_text("100%")
            actions.append({"action": "Explicit fictional demo, connected search, match explanation and raw provenance", "result": "PASS"})

            navigation("Profile")
            page.get_by_label("I approve its use in documents").nth(1).uncheck()
            save_profile()
            select_data_job()
            expect(page.locator(".big-score")).to_have_text("50%")
            prepare()
            application = request("/applications?profile_id=" + profile_id)["applications"][0]
            identifier = application["id"]
            assert application["fact_ids"] == ["python"]
            first_documents = request("/applications/" + identifier + "/documents")["documents"]
            first_resume = next(row for row in first_documents if row["kind"] == "resume")
            assert first_resume['created_at'] and first_resume['generation_model'] == 'deterministic-fact-copy-v1'
            assert [fact['text'] for fact in first_resume['facts_used']] == ['Python']
            provenance = page.get_by_role('region', name='resume version 1', exact=True)
            provenance.get_by_text('Generation details and facts', exact=True).click()
            expect(provenance.get_by_text('Generation method: deterministic-fact-copy-v1', exact=True)).to_be_visible()
            with page.expect_download() as text_download:
                provenance.get_by_role('link', name='Download TXT', exact=True).click()
            text_bytes = Path(text_download.value.path()).read_bytes()
            assert hashlib.sha256(text_bytes).hexdigest() == first_resume['sha256']['txt']
            original = context.request.get(base_url + "/documents/" + first_resume["id"] + "/docx").body()
            text = "\n".join(row.text for row in Document(io.BytesIO(original)).paragraphs)
            assert "Python" in text and "SQL" not in text
            assert text_bytes.decode('utf-8').strip() == text
            actions.append({'action':'Original approved facts, generation metadata and checksum-verified TXT download visible through the browser', 'result':'PASS'})
            actions.append({"action": "Pending fact excluded from matching and downloaded factual DOCX", "result": "PASS"})

            navigation("Profile")
            page.get_by_label("I approve its use in documents").nth(1).check()
            save_profile()
            select_data_job()
            expect(page.locator(".big-score")).to_have_text("100%")
            prepare()
            revised = request("/applications/" + identifier)
            assert revised["document_version"] == 2 and set(revised["fact_ids"]) == {"python", "sql"}
            documents = request("/applications/" + identifier + "/documents")["documents"]
            assert len(documents) == 4
            old = context.request.get(base_url + "/documents/" + first_resume["id"] + "/docx").body()
            assert old == original
            with page.expect_download() as download:
                page.get_by_role("link", name="Download PDF", exact=True).first.click()
            pdf_path = artifacts / "fictional-document.pdf"
            download.value.save_as(pdf_path)
            assert pdf_path.read_bytes().startswith(b"%PDF")
            assert hashlib.sha256(pdf_path.read_bytes()).hexdigest() in {doc["sha256"]["pdf"] for doc in documents}
            actions.append({"action": "Approved profile revision generates new documents, preserves original bytes, and PDF downloads through UI", "result": "PASS"})

            page.get_by_role("button", name="Run dry-run", exact=True).click()
            expect(page.locator(".job-detail > .tag")).to_have_text("DRY_RUN_COMPLETE")
            dry = request("/applications/" + identifier)
            assert "STOP_BEFORE_SUBMIT" in dry["trace"] and "SUBMIT" not in dry["trace"]
            page.get_by_label("I approve one fictional mock submission").check()
            page.get_by_role("button", name="Submit to local mock", exact=True).click()
            expect(page.locator(".job-detail > .tag")).to_have_text("CONFIRMED")
            confirmed = request("/applications/" + identifier)
            assert confirmed["target"] == "MOCK" and confirmed["confirmation"]["submission_count"] == 1
            assert request("/health")["external_submission_enabled"] is False
            repeated = context.request.post(base_url + "/applications/" + identifier + "/mock-run", data={"submit": True}).json()
            assert repeated["state"] == "COMPLETED"
            assert request("/applications/" + identifier)["confirmation"] == confirmed["confirmation"]
            page.screenshot(path=str(artifacts / "application-confirmed.png"), full_page=True)
            actions.append({"action": "Browser dry-run stops before submission; explicit local mock receipt count remains one on repeat", "result": "PASS"})

            page.reload(wait_until="networkidle")
            expect(page.get_by_label("Active profile")).to_have_value(profile_id)
            navigation("Applications")
            page.get_by_role("button").filter(has=page.get_by_role("heading", name="Data Engineer", exact=True)).click()
            expect(page.locator(".job-detail > .tag")).to_have_text("CONFIRMED")
            navigation('Profile')
            library=page.get_by_role('region', name='Document library', exact=True)
            library.get_by_label('Document type', exact=True).select_option('CERTIFICATION')
            library.get_by_label('Document label', exact=True).fill('Synthetic certificate')
            library.get_by_label('Choose a document', exact=False).set_input_files(
                {'name':'synthetic-certificate.txt','mimeType':'text/plain','buffer':b'Synthetic library proof, not a career credential.'})
            library.get_by_role('button', name='Store document', exact=True).click()
            collection=library.get_by_role('region',name='Synthetic certificate collection',exact=True)
            expect(collection.get_by_role('link',name='Download original version 1',exact=True)).to_be_visible()
            stored=request('/profiles/'+profile_id+'/document-library')['uploaded']
            certificate=next(row for row in stored if row['document_kind']=='CERTIFICATION')
            with page.expect_download() as download:
                collection.get_by_role('link',name='Download original version 1',exact=True).click()
            assert hashlib.sha256(Path(download.value.path()).read_bytes()).hexdigest()==certificate['sha256']
            assert request('/profiles/'+profile_id+'/resumes')['resumes']==[]
            assert {row['fact_id'] for row in request('/profiles/'+profile_id)['facts']}=={'python','sql'}
            actions.append({'action':'Typed document library stores/downloads checksum-backed certification without adding resume-import entries or career facts','result':'PASS'})
            navigation("Activity")
            expect(page.get_by_role("heading", name="Service health", exact=True)).to_be_visible()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile overflow"
            page.screenshot(path=str(artifacts / "activity-mobile.png"), full_page=True)
            assert not failures, failures
            actions.append({"action": "Browser reload restores selected profile and application history; responsive Activity has no horizontal overflow or browser exceptions", "result": "PASS"})
        except BaseException:
            page.screenshot(path=str(artifacts / "failure.png"), full_page=True)
            raise
        finally:
            context.close()
            browser.close()
    evidence = {"result": "PASS", "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": "python -m cli.ui_e2e --base-url " + base_url, "target": "Connected React UI / PostgreSQL / three RQ queues / actual Chromium",
        "profile_id": profile_id, "application_id": identifier, "operation_id": repeated["operation_id"], "confirmation": confirmed["confirmation"], "external_submission_enabled": False,
        "actions": actions, "artifacts": [str(path) for path in artifacts.glob("*") if path.name != "failure.png"]}
    Path(evidence_path).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18700")
    parser.add_argument("--evidence", required=True)
    options = parser.parse_args()
    run(options.base_url, options.evidence)
