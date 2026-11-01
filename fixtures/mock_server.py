"""Explicit fictional local ATS/aggregator fixtures, never production job data."""
from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape
import hashlib
import json
from pathlib import Path
import socket
import threading
import time
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import UploadFile
import uvicorn


def create_mock_app(receipt_file=None, *, scenario="baseline"):
    if not isinstance(scenario, str) or scenario not in {"baseline", "coverage"}:
        raise ValueError("Unknown fictional fixture scenario")
    app = FastAPI(title="MJobs fictional mock ATS")
    receipts = json.loads(Path(receipt_file).read_text(encoding="utf-8")) if receipt_file and Path(receipt_file).exists() else {}
    request_counts = {"review": 0, "submit": 0}
    supporting_uploads = []
    lock = threading.Lock()

    def jobs(origin):
        return [{"source_job_id": f"employer-{key}", "requisition": key, "tenant": "example",
                 "company": "Example Systems Inc.", "company_website": origin + "/company",
                 "title": "Python Developer" if key == "A" else "Data Engineer",
                 "description": "<p>Required: Python and SQL. Responsibilities: Build and support Python and SQL systems.</p>", "skills": ["Python", "SQL"],
                 "location": "United States", "remote_type": "REMOTE", "salary_min": 80000,
                 "salary_max": 110000, "salary_currency": "USD", "salary_period": "YEAR",
                 "application_url": origin + "/ats/apply/" + key} for key in ("A", "B")]

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "MOCK"}

    @app.get("/board/jobs")
    def board(request: Request):
        row = jobs(str(request.base_url).rstrip("/"))[0]
        origin = str(request.base_url).rstrip("/")
        rows = [dict(row, source_job_id="board-A", application_url=origin + "/board/apply/A"),
                dict(row, source_job_id="board-A-syndicated", salary_min=90000, remote_type="HYBRID", application_url=origin + "/board/apply/A")]
        if scenario == "coverage":
            rows.extend([
                dict(jobs(origin)[1], source_job_id="board-B", salary_min=40,
                     salary_max=55, salary_period="HOUR", remote_type="ONSITE",
                     application_url=origin + "/board/apply/B"),
                dict(row, source_job_id="board-C", requisition="C", tenant=None,
                     company=None, company_website=None, title="Unattributed support role",
                     salary_min=None, salary_max=None, salary_currency=None,
                     salary_period=None, remote_type="UNKNOWN",
                     application_url=origin + "/board/apply/C"),
            ])
        return {"jobs": rows}

    @app.get("/board/apply/{requisition}")
    def application_redirect(requisition: str):
        return RedirectResponse("/ats/apply/" + quote(requisition, safe=""))

    @app.get("/company", response_class=HTMLResponse)
    def company():
        return '<h1>Example Systems Inc.</h1><a rel="careers" href="/company/careers">Careers</a>'

    @app.get("/company/careers")
    def careers():
        return RedirectResponse("/ats/example")

    @app.get("/ats/example", response_class=HTMLResponse)
    def tenant():
        return '<h1>Example Systems jobs</h1><a data-provider="mock-ats" data-tenant="example" href="/ats/example/jobs">Jobs</a>'

    @app.get("/ats/example/jobs")
    def employer(request: Request):
        return {"jobs": jobs(str(request.base_url).rstrip("/"))}

    @app.get("/ats/questions/{requisition}")
    def questions(requisition: str):
        if requisition not in ("A", "B"):
            return HTMLResponse("Question metadata is unavailable", status_code=404)
        return {
            "provider": "mock-ats", "tenant": "example", "requisition": requisition,
            "revision": 1,
            "questions": [
                {"id": "name", "label": "Name", "required": True,
                 "requirements": {"profile_paths": ["name"]}},
                {"id": "email", "label": "Email", "required": True,
                 "requirements": {"profile_paths": ["email"]}},
                {"id": "resume", "label": "Resume", "required": True,
                 "requirements": {"document_kinds": ["resume"]}},
                {"id": "cover-letter", "label": "Cover letter", "required": True,
                 "requirements": {"document_kinds": ["cover-letter"]}},
                {"id": "country", "label": "Country", "required": True,
                 "requirements": {"answer_ids": ["country"]}},
                {"id": "remote", "label": "Remote role", "required": True,
                 "requirements": {"answer_ids": ["remote"]}},
                {"id": "fixture_question", "label": "Approved fixture answer", "required": True,
                 "requirements": {"answer_ids": ["fixture_question"]}},
                {"id": "application-id", "label": "Application ID", "required": True,
                 "requirements": {"application_context": ["application_id"]}},
                {"id": "optional_note", "label": "Optional note", "required": False,
                 "requirements": {"answer_ids": ["optional_note"]}},
                {"id": "region", "label": "State or province", "required": False,
                 "tag": "select", "options": ["Oklahoma", "Texas"],
                 "condition": {"question_id": "country", "equals": "United States"},
                 "requirements": {"answer_ids": ["state"]}},
            ],
        }

    @app.get("/ats/apply/{requisition}", response_class=HTMLResponse)
    def application(requisition: str, request: Request, challenge: bool = False,
                    dynamic: str | None = None, auth_expired: bool = False,
                    flow: str | None = None):
        # Route encoding preserves identity; HTML encoding protects the attribute
        # boundary independently, even if the route representation changes.
        requisition_path = escape(quote(requisition, safe=""), quote=True)
        if flow not in {None, "onepage"}:
            return HTMLResponse('<p role="alert">Unknown fictional application flow</p>', status_code=422)
        one_page = flow == "onepage"
        if challenge:
            return '<h1 data-intervention="CAPTCHA">Manual challenge required</h1>'
        if auth_expired and request.cookies.get("mock_session") != "fixture-authenticated":
            return (f'<h1 data-auth-required="true">Sign in required</h1>'
                    f'<a href="/ats/login/{requisition_path}">Sign in manually</a>')
        dynamic_mode = dynamic if dynamic in {"known", "unknown"} else None
        dynamic_field = ""
        if dynamic_mode == "known":
            dynamic_field = '''<label for="region">State or province</label>
                <select id="region" name="region" data-mjobs-dynamic-field
                        data-mjobs-question-id="region">
                  <option value="">Choose</option>
                  <option>Oklahoma</option><option>Texas</option>
                </select>'''
        elif dynamic_mode == "unknown":
            dynamic_field = '''<label for="unlisted">Additional required field</label>
                <input id="unlisted" name="unlisted" data-mjobs-dynamic-field
                       data-mjobs-question-id="unlisted">'''
        review_query = escape(f"?dynamic={dynamic_mode}", quote=True) if dynamic_mode else ""
        form_route = "onepage" if one_page else "review"
        extra_country = '<option>Canada</option>' if dynamic_mode or one_page else ""
        submission_fields = '''<label>Application ID<input name="application_id" required></label>
        <label>Approved fixture answer<textarea name="answer" required></textarea></label>
        <label><input name="confirmed" type="checkbox" required>Confirm fictional test submission</label>''' if one_page else ""
        return f'''<h1>Mock ATS application {escape(requisition)}</h1>
        <form action="/ats/{form_route}/{requisition_path}{review_query}" method="post" enctype="multipart/form-data">
        <label>Name<input name="name" required maxlength="200"></label>
        <label>Email<input name="email" type="email" required></label>
        <label>Resume<input name="resume" type="file" accept=".pdf" required></label>
        <label>Cover letter<input name="cover" type="file" accept=".pdf" required></label>
        <label>Supporting documents<input name="supporting" type="file" accept=".pdf,.docx,.txt" multiple></label>
        <label for="country">Country</label><select id="country" name="country" required><option value="">Choose</option><option>United States</option>{extra_country}</select>
        {f'<section id="conditional-field" hidden>{dynamic_field}</section>' if dynamic_mode else ''}
        <label>Remote role<input type="radio" name="remote" value="yes" required>Yes</label>
        <label><input name="reviewed" type="checkbox" required>Documents reviewed</label>
        {submission_fields}
        <button type="submit">{'Submit mock application' if one_page else 'Review application'}</button></form>
        {f'''<script>
        document.getElementById('country').addEventListener('change', event => {{
          const visible = event.target.value === 'United States';
          const section = document.getElementById('conditional-field');
          const field = section.querySelector('[data-mjobs-dynamic-field]');
          section.hidden = !visible;
          field.required = visible;
          if (!visible) field.value = '';
        }});
        </script>''' if dynamic_mode else ''}'''

    @app.get("/ats/login/{requisition}", response_class=HTMLResponse)
    def login_page(requisition: str):
        if requisition not in ("A", "B"):
            return HTMLResponse("Unknown fixture application", status_code=404)
        return f'''<h1>Fictional mock ATS sign in</h1>
        <form method="post" action="/ats/login/{requisition}">
          <label>Fixture username<input name="username" required></label>
          <label>Fixture password<input name="password" type="password" required></label>
          <button type="submit">Sign in</button>
        </form>'''

    @app.post("/ats/login/{requisition}")
    async def login(requisition: str, request: Request):
        if requisition not in ("A", "B"):
            return HTMLResponse("Unknown fixture application", status_code=404)
        form = await request.form()
        authenticated = bool(form.get("username") and form.get("password"))
        await form.close()
        if not authenticated:
            return HTMLResponse("Fixture sign in is required", status_code=401)
        response = RedirectResponse(
            f"/ats/apply/{requisition}?auth_expired=true", status_code=303)
        response.set_cookie("mock_session", "fixture-authenticated", max_age=3600,
                            httponly=True, samesite="strict")
        return response

    def validation_error(message="Validation failed", *, status_code=422):
        return HTMLResponse(f'<p role="alert">{escape(message)}</p>', status_code=status_code)

    async def validate_document_fields(form, dynamic_mode):
        """Shared fictional form validation; never publish a receipt here."""
        if not all(form.get(field) for field in ("name", "email", "resume", "cover", "country", "remote", "reviewed")):
            return "Validation failed", []
        active_condition = form.get("country") == "United States"
        if active_condition and dynamic_mode == "known" and form.get("region") != "Oklahoma":
            return "Dependent field validation failed", []
        if active_condition and dynamic_mode == "unknown" and not form.get("unlisted"):
            return "Dependent field validation failed", []
        for field in ("resume", "cover"):
            uploaded = form.get(field)
            if not isinstance(uploaded, UploadFile):
                return "Invalid document", []
            data = await uploaded.read(2_000_001)
            if len(data) > 2_000_000 or not data.startswith(b"%PDF-"):
                return "Invalid document", []
        received_supporting = []
        for uploaded in form.getlist("supporting"):
            if not isinstance(uploaded, UploadFile):
                return "Invalid supporting document", []
            filename = uploaded.filename or ""
            if not filename:
                continue
            data = await uploaded.read(10 * 1024 * 1024 + 1)
            extension = filename.rsplit(".", 1)[-1].casefold()
            valid = (
                len(data) <= 10 * 1024 * 1024
                and ((extension == "pdf" and data.startswith(b"%PDF-"))
                     or (extension == "docx" and data.startswith(b"PK"))
                     or (extension == "txt" and bool(data)))
            )
            if not valid:
                return "Invalid supporting document", []
            received_supporting.append({
                "filename": filename,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
        return None, received_supporting

    def submission_identity(form):
        if (not all(form.get(key) for key in ("application_id", "answer", "confirmed"))
                or not isinstance(form.get("application_id"), str)
                or form.get("answer") != "Controlled fictional test application."):
            return None
        return form["application_id"]

    def publish_receipt(requisition, application_id, request_url):
        """Preserve one durable fictional receipt for a bound application identity."""
        reference_number = "MOCK-" + application_id[:16]
        confirmation_text = (
            f"Thank you for applying. Application received. Application reference: {reference_number}"
        )
        with lock:
            existing = receipts.get(application_id)
            if existing is not None and existing.get("requisition") != requisition:
                return validation_error("Application identity belongs to another fictional requisition", status_code=409)
            if existing is None:
                receipts[application_id] = {
                    "application_id": application_id, "requisition": requisition,
                    "receipt": reference_number,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "confirmation_url": str(request_url),
                    "confirmation_text": confirmation_text,
                    "reference_number": reference_number, "submission_count": 1,
                }
                if receipt_file:
                    path = Path(receipt_file)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_suffix(".tmp")
                    try:
                        temporary.write_text(json.dumps(receipts), encoding="utf-8")
                        temporary.replace(path)
                    except Exception:
                        receipts.pop(application_id, None)
                        temporary.unlink(missing_ok=True)
                        raise
            receipt = dict(receipts[application_id])
        # Encode at rendering time, including references restored from receipt_file.
        reference_html = escape(receipt["reference_number"], quote=True)
        return (f'<h1 data-confirmation="{reference_html}">Thank you for applying.</h1>'
                f'<p>Application received.</p><p>Application reference: '
                f'{reference_html}</p>')

    @app.post("/ats/review/{requisition}", response_class=HTMLResponse)
    async def review(requisition: str, request: Request):
        with lock:
            request_counts["review"] += 1
        form = await request.form()
        try:
            error, received_supporting = await validate_document_fields(form, request.query_params.get("dynamic"))
            if error:
                return validation_error(error)
        finally:
            await form.close()
        with lock:
            supporting_uploads.extend(received_supporting)
        # A fictional question, supplied only by an explicitly approved test answer.
        return f'''<h1>Review mock application</h1><p data-validated="true">Documents validated</p>
        <form method="post" action="/ats/submit/{escape(quote(requisition, safe=""), quote=True)}">
        <label>Application ID<input name="application_id" required></label>
        <label>Approved fixture answer<textarea name="answer" required></textarea></label>
        <label><input name="confirmed" type="checkbox" required>Confirm fictional test submission</label>
        <button type="submit">Submit mock application</button></form>'''

    @app.post("/ats/submit/{requisition}", response_class=HTMLResponse)
    async def submit(requisition: str, request: Request):
        with lock:
            request_counts["submit"] += 1
        form = await request.form()
        try:
            application_id = submission_identity(form)
            if application_id is None:
                return validation_error()
            return publish_receipt(requisition, application_id, request.url)
        finally:
            await form.close()

    @app.post("/ats/onepage/{requisition}", response_class=HTMLResponse)
    async def onepage(requisition: str, request: Request):
        with lock:
            request_counts["submit"] += 1
        form = await request.form()
        try:
            error, received_supporting = await validate_document_fields(form, request.query_params.get("dynamic"))
            if error:
                return validation_error(error)
            application_id = submission_identity(form)
            if application_id is None:
                return validation_error()
            with lock:
                supporting_uploads.extend(received_supporting)
            return publish_receipt(requisition, application_id, request.url)
        finally:
            await form.close()

    @app.get("/ats/receipts/{application_id}")
    def receipt(application_id: str):
        with lock:
            return receipts.get(application_id, {"status": "not_found"})

    @app.get("/_test/request-counts")
    def test_request_counts():
        with lock:
            return dict(request_counts)

    @app.get("/_test/supporting-uploads")
    def test_supporting_uploads():
        with lock:
            return {"uploads": list(supporting_uploads)}

    return app


@contextmanager
def fixture_server(receipt_file=None, *, scenario="baseline"):
    app = create_mock_app(receipt_file, scenario=scenario)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("Fixture server failed to start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("Fixture server failed to stop")


if __name__ == "__main__":
    import os
    uvicorn.run(create_mock_app(os.environ.get("MOCK_RECEIPT_FILE")), host="0.0.0.0", port=8090)
