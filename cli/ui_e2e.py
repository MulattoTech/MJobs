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

        def download_document(document_id, format):
            path = f"/profiles/{profile_id}/documents/{document_id}/{format}"
            response = context.request.get(base_url + path)
            assert response.ok, (path, response.status)
            return response.body()

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
            assert first_resume['created_at'] and first_resume['generation_model'] == 'deterministic-fact-copy-v2'
            assert [fact['text'] for fact in first_resume['facts_used']] == ['Python']
            provenance = page.get_by_role('region', name='resume version 1', exact=True)
            provenance.get_by_text('Generation details and facts', exact=True).click()
            expect(provenance.get_by_text('Generation method: deterministic-fact-copy-v2', exact=True)).to_be_visible()
            with page.expect_download() as text_download:
                provenance.get_by_role('link', name='Download TXT', exact=True).click()
            text_bytes = Path(text_download.value.path()).read_bytes()
            assert hashlib.sha256(text_bytes).hexdigest() == first_resume['sha256']['txt']
            original = download_document(first_resume["id"], "docx")
            text = "\n".join(row.text for row in Document(io.BytesIO(original)).paragraphs)
            assert "Python" in text and "SQL" not in text
            assert first_resume['template']=='verified-headings-v1'
            assert [p.text for p in Document(io.BytesIO(original)).paragraphs if p.style.name=='Heading 1']==['Skills']
            actions.append({'action':'ATS resume templates expose conventional headings in the browser-downloaded DOCX while preserving approved wording','result':'PASS'})
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
            old = download_document(first_resume["id"], "docx")
            assert old == original
            with page.expect_download() as download:
                page.get_by_role("link", name="Download PDF", exact=True).first.click()
            pdf_path = artifacts / "fictional-document.pdf"
            download.value.save_as(pdf_path)
            assert pdf_path.read_bytes().startswith(b"%PDF")
            assert hashlib.sha256(pdf_path.read_bytes()).hexdigest() in {doc["sha256"]["pdf"] for doc in documents}
            actions.append({"action": "Approved profile revision generates new documents, preserves original bytes, and PDF downloads through UI", "result": "PASS"})

            select_data_job()
            page.get_by_label('Order approved skills for this job',exact=True).check()
            prepare()
            tailored=next(row for row in request('/applications/'+identifier+'/documents')['documents'] if row['version']==3 and row['kind']=='resume')
            assert tailored['generation_model']=='deterministic-skill-order-v3'
            assert tailored['template']=='approved-skill-emphasis-v1'
            assert tailored['generation_context']['emphasized_fact_ids']
            expect(page.get_by_role('region',name='resume version 3',exact=True).get_by_text('Job-matched skill and technology facts in bold:',exact=False)).to_be_visible()
            emphasized_doc=Document(io.BytesIO(download_document(tailored['id'],'docx')))
            assert any(p.text=='Python' and all(r.bold for r in p.runs) for p in emphasized_doc.paragraphs)
            actions.append({'action':'Job-matched technology emphasis reaches worker, DOCX download and visible provenance','result':'PASS'})
            assert tailored['generation_context']['mode']=='APPROVED_SKILL_ORDER'
            assert set(tailored['fact_ids'])=={'python','sql'}
            expect(page.get_by_role('region',name='resume version 3',exact=True).get_by_text('Tailored resume: skill order',exact=False)).to_be_visible()
            actions.append({'action':'Explicit skill-order opt-in reaches the document worker and preserves approved facts with target snapshot and separate revision','result':'PASS'})

            comparison=page.get_by_role('region',name='Compare base and tailored resumes',exact=True)
            comparison.get_by_label('Base resume',exact=True).select_option(label='v2')
            comparison.get_by_role('button',name='Compare selected resumes',exact=True).click()
            result=comparison.get_by_role('region',name='Resume comparison result',exact=True)
            expect(result.get_by_role('region',name='Content omitted',exact=True)).to_contain_text('No content omitted.')
            expect(result.get_by_role('region',name='Summary changes',exact=True)).to_contain_text('No changes.')
            expect(result.get_by_role('heading',name='Generated base · v2',exact=True)).to_be_visible()
            expect(result.get_by_role('heading',name='Tailored resume · v3',exact=True)).to_be_visible()
            actions.append({'action':'Immutable generated base and tailored resume comparison shows recorded versions and unchanged facts through the UI','result':'PASS'})

            profile_before_comparison=request('/profiles/'+profile_id)
            uploaded_original_text=b'Summary\nOriginal synthetic summary\nSkills\nSQL; Python\nResponsibilities\nMaintained synthetic servers\n'
            uploaded_response=context.request.post(base_url+'/profiles/'+profile_id+'/resumes', data=uploaded_original_text,
                headers={'X-Filename':'comparison-base.txt','Content-Type':'text/plain'})
            assert uploaded_response.status==201, uploaded_response.text()
            uploaded_original=uploaded_response.json()
            comparison.get_by_role('button',name='Refresh base resumes',exact=True).click()
            comparison.get_by_label('Base resume',exact=True).select_option('u:'+uploaded_original['revision_id'])
            comparison.get_by_role('button',name='Compare selected resumes',exact=True).click()
            expect(result.get_by_role('heading',name='Uploaded base · v1',exact=True)).to_be_visible()
            expect(result.get_by_role('region',name='Summary changes',exact=True)).to_contain_text('Original synthetic summary')
            expect(result.get_by_role('region',name='Content omitted',exact=True)).to_contain_text('Maintained synthetic servers')
            expect(result.get_by_role('region',name='Original extraction provenance',exact=True)).to_contain_text('Native extracted text')
            with page.expect_download() as original_download:
                result.get_by_role('link',name='Download compared original',exact=True).click()
            assert Path(original_download.value.path()).read_bytes()==uploaded_original_text
            assert request('/profiles/'+profile_id)==profile_before_comparison
            actions.append({'action':'Uploaded original comparison preserves revision/hash and profile facts, displays changed summary and omitted content, and downloads exact source bytes','result':'PASS'})

            select_data_job()
            page.get_by_label('Order approved skills for this job',exact=True).check()
            page.get_by_label('Choose resume facts',exact=True).check()
            expect(page.get_by_label('Include SQL',exact=True)).to_be_checked()
            page.get_by_label('Include SQL',exact=True).uncheck()
            prepare()
            selected_documents=request('/applications/'+identifier+'/documents')['documents']
            selected_resume=next(row for row in selected_documents if row['version']==4 and row['kind']=='resume')
            selected_cover=next(row for row in selected_documents if row['version']==4 and row['kind']=='cover-letter')
            assert selected_resume['generation_model']=='deterministic-fact-selection-v2'
            assert selected_resume['fact_ids']==['python'] and set(selected_cover['fact_ids'])=={'python','sql'}
            assert selected_resume['generation_context']['omitted_fact_ids']==['sql']
            selected_history=page.get_by_role('region',name='resume version 4',exact=True)
            expect(selected_history.get_by_text('Tailored resume: selected facts',exact=False)).to_be_visible()
            with page.expect_download() as selected_download:
                selected_history.get_by_role('link',name='Download TXT',exact=True).click()
            selected_text=Path(selected_download.value.path()).read_text(encoding='utf-8')
            assert 'Python' in selected_text and 'SQL' not in selected_text
            assert 'SQL' in download_document(selected_cover['id'],'txt').decode('utf-8')
            comparison=page.get_by_role('region',name='Compare base and tailored resumes',exact=True)
            comparison.get_by_label('Base resume',exact=True).select_option(label='v2')
            comparison.get_by_role('button',name='Compare selected resumes',exact=True).click()
            expect(comparison.get_by_role('region',name='Content omitted',exact=True)).to_contain_text('skill: SQL')
            assert request('/profiles/'+profile_id)==profile_before_comparison
            actions.append({'action':'Explicit approved resume selection reaches the real document worker, omits SQL only from the resume, preserves profile and cover letter, and shows immutable omissions','result':'PASS'})

            navigation('Profile')
            page.get_by_label('city',exact=True).fill('Tulsa')
            save_profile()
            saved_contact_profile=request('/profiles/'+profile_id)
            select_data_job()
            page.get_by_label('Order approved skills for this job',exact=True).uncheck()
            contact_option=page.get_by_label('Include saved resume contact details',exact=True)
            expect(contact_option).not_to_be_checked();contact_option.check()
            prepare()
            contact_resume=next(row for row in request('/applications/'+identifier+'/documents')['documents'] if row['version']==5 and row['kind']=='resume')
            assert contact_resume['template']=='approved-contact-base-v1'
            assert 'Location: Tulsa' in contact_resume['generation_context']['contact_header']
            contact_history=page.get_by_role('region',name='resume version 5',exact=True)
            contact_history.get_by_text('Generation details and facts',exact=True).click()
            expect(contact_history.get_by_role('region',name='Saved resume contact header')).to_contain_text('Location: Tulsa')
            with page.expect_download() as contact_download:
                contact_history.get_by_role('link',name='Download TXT',exact=True).click()
            assert 'Location: Tulsa' in Path(contact_download.value.path()).read_text(encoding='utf-8')
            assert 'Location: Tulsa' not in download_document(selected_resume['id'],'txt').decode('utf-8')
            assert request('/profiles/'+profile_id)==saved_contact_profile
            actions.append({'action':'Explicit saved contact header reaches real document worker, history and download while prior revision and profile remain unchanged','result':'PASS'})

            page.get_by_role("button", name="Run dry-run", exact=True).click()
            expect(page.locator(".job-detail > .tag")).to_have_text("DRY_RUN_COMPLETE", timeout=15000)
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
            base_resumes_before_library=request('/profiles/'+profile_id+'/resumes')['resumes']
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
            assert request('/profiles/'+profile_id+'/resumes')['resumes']==base_resumes_before_library
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
