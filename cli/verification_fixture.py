"""Private connected-verifier child. Creates its own fixture/API/database only.

Uses the same PROFILE, fixture_server, API and browser adapter as the existing
mock_e2e and browser acceptance harnesses. No user target or database argument.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

from cli.verification_checks import CHECKS, exit_status


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def exercise(report, root, timeout):
    active = CHECKS[0]
    deadline = time.monotonic() + timeout - 0.5

    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError("connected fixture deadline")

    check.remaining_seconds = lambda: max(0, deadline - time.monotonic())

    def passed(name, reason):
        check()
        report["checks"][name] = {"status": "PASS", "reason": reason}

    store = None
    try:
        import httpx
        from fastapi.testclient import TestClient
        from sqlalchemy import select
        from apps.api.main import create_app
        from fixtures.mock_server import fixture_server
        from packages.persistence import models as m
        from packages.persistence.store import Store
        from services.ats_detection.detector import detect_ats
        from services.company_discovery.flywheel import discover_company_source
        from services.discovery.pipeline import MockClient
        from services.discovery.sources.registry import source_adapter
        from services.applications.mock_adapter import apply_mock
        from tests.integration.test_spine import PROFILE
        from workers.tasks import execute_operation

        # Registry resolves a real adapter which executes both GETs using an
        # isolated synthetic HTTP transport. No network provider is contacted.
        requests = []

        def source_response(request):
            requests.append(request.url.path)
            require(request.method == "GET" and request.url.host == "boards-api.greenhouse.io",
                    "unexpected registry request")
            if request.url.path == "/v1/boards/fixture/jobs":
                return httpx.Response(200, json={"jobs": [{"id": 288, "title": "Synthetic role"}]})
            require(request.url.path == "/v1/boards/fixture", "unexpected registry route")
            return httpx.Response(200, json={"name": "Synthetic employer"})

        adapter = source_adapter({"provider": "greenhouse", "tenant": "fixture", "rate_limit": 600},
                                 transport=httpx.MockTransport(source_response), check=check)
        batch = adapter.search()
        require(len(batch.items) == 1 and batch.items[0].raw["id"] == 288
                and set(requests) == {"/v1/boards/fixture", "/v1/boards/fixture/jobs"},
                "registry did not execute synthetic adapter collection")
        report["observations"]["registry_requests"] = sorted(requests)
        passed(active, "registry selected Greenhouse and executed two isolated synthetic HTTP GETs")

        db = f"sqlite:///{(root / 'fixture.db').as_posix()}"
        store = Store(db)
        store.migrate()
        store.bind_workspace_mode("FIXTURE")
        profile = dict(PROFILE, id="verification-" + report["invocation_id"])
        receipt_file = root / "receipts.json"
        with fixture_server(receipt_file=receipt_file) as origin:
            require(urlsplit(origin).hostname == "127.0.0.1", "fixture must bind loopback")
            with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as fixture:
                require(fixture.get(origin + "/health").json() == {"status": "ok", "mode": "MOCK"},
                        "owned fixture service identity mismatch")
                # Only this child's environment is changed. Worker calls cannot
                # select inherited personal stores, providers or queues.
                os.environ.update(MJOBS_DATABASE_URL=db, MJOBS_MOCK_ORIGIN=origin,
                                  MJOBS_OUTPUT_DIR=str(root), MJOBS_OPERATION_MODE="FIXTURE",
                                  MJOBS_REDIS_URL="redis://127.0.0.1:1/0")
                app = create_app(store, origin, root, "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
                with TestClient(app) as api:
                    def call(method, path, **kwargs):
                        check()
                        response = api.request(method, path, **kwargs)
                        response.raise_for_status()
                        return response.json()

                    def execute(queued):
                        operation = execute_operation(queued["operation_id"])
                        require(operation.get("status") == "COMPLETED", "isolated operation did not complete")
                        require(call("GET", "/operations/" + queued["operation_id"])["state"] == "COMPLETED",
                                "operation durable readback did not complete")

                    health = call("GET", "/health")
                    settings = call("GET", "/settings")
                    require(health.get("operation_mode") == settings.get("operation_mode") == "FIXTURE"
                            and health.get("external_submission_enabled") is False
                            and settings.get("external_submission_enabled") is False,
                            "owned API fixture admission failed")
                    report["observations"]["fixture_origin"] = origin
                    call("PUT", "/profiles", json=profile)
                    active = "mock global source"
                    execute(call("POST", "/discovery", json={"profile_id": profile["id"]}))
                    jobs = call("GET", "/jobs", params={"profile_id": profile["id"]})["jobs"]
                    require({job["requisition"] for job in jobs} == {"A", "B"}
                            and store.count("job_postings") == 4, "mock discovery inventory mismatch")
                    report["observations"]["profile_id"] = profile["id"]
                    report["observations"]["canonical_jobs"] = len(jobs)
                    passed(active, "API discovery operation executed; two jobs and four postings persisted")

                    active = "mock company discovery"
                    transport = MockClient(origin, check=check)
                    try:
                        company = discover_company_source(store, {
                            "company_name": "Example Systems Inc.", "company_website": origin + "/company",
                        }, transport, check=check, profile_id=profile["id"])
                    finally:
                        transport.close()
                    require(company["provider"] == "mock-ats" and company["tenant"] == "example"
                            and company["jobs_enumerated"] == 2 and store.count("company_job_sources") == 1,
                            "company discovery did not prove and persist fixture feed")
                    passed(active, "company resolver fetched owned careers/ATS feed and persisted one source with two jobs")

                    active = "ATS detector"
                    html = fixture.get(origin + "/ats/example").text
                    unknown = detect_ats(origin + "/ats/example", html)
                    detected = detect_ats(origin + "/careers", '<script src="https://jobs.lever.co/fixture"></script>')
                    require(unknown.status == "NO_MATCH" and detected.status == "MATCH" and detected.provider == "lever",
                            "detector synthetic positive/negative controls failed")
                    passed(active, "real detector matched synthetic Lever signature and rejected mock-only page with NO_MATCH; no provider fetch")

                    active = "Document engine healthy"
                    job = next(job for job in jobs if job["requisition"] == "B")
                    preparation = call("POST", "/applications", json={"job_id": job["id"], "profile_id": profile["id"]})
                    execute(preparation)
                    app_id = preparation["application_id"]
                    app_path = "/profiles/" + profile["id"] + "/applications/" + app_id
                    application = call("GET", app_path)
                    require(application["state"] == "PREPARED" and application["target"] == "MOCK",
                            "fixture document preparation failed")
                    with store.engine.connect() as connection:
                        documents = list(connection.execute(select(m.documents).where(
                            m.documents.c.application_id == app_id)).mappings())
                    require({row["kind"] for row in documents} == {"resume", "cover-letter"}, "missing generated documents")
                    from docx import Document
                    from pypdf import PdfReader
                    for row in documents:
                        for extension in ("pdf", "docx"):
                            path = root / app_id / row["files"][extension]
                            require(hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"][extension],
                                    "generated document hash mismatch")
                            text = (" ".join(page.extract_text() for page in PdfReader(path).pages)
                                    if extension == "pdf" else " ".join(p.text for p in Document(path).paragraphs))
                            require("Python" in text, "generated document lost approved fixture fact")
                    passed(active, "API preparation executed; generated resume/letter PDF and DOCX bytes parsed and hashes verified")

                    active = "Browser worker healthy"
                    from playwright.sync_api import sync_playwright, Error as PlaywrightError
                    try:
                        with sync_playwright() as playwright:
                            if not Path(playwright.chromium.executable_path).is_file():
                                report["checks"][active]["reason"] = "Playwright Chromium executable unavailable; no installation attempted"
                                return
                            browser = playwright.chromium.launch(headless=True)
                            browser.close()
                    except (PermissionError, FileNotFoundError):
                        report["checks"][active]["reason"] = "local browser process dependency or spawn permission unavailable"
                        return
                    except PlaywrightError as error:
                        if "Executable doesn't exist" in str(error) or "Host system is missing dependencies" in str(error):
                            report["checks"][active]["reason"] = "local browser executable or system dependency unavailable"
                            return
                        raise
                    passed(active, "isolated Playwright Chromium launched; production worker heartbeat is not covered")

                    active = "Dry-run mode functional"
                    dry = call("POST", app_path + "/mock-run", json={"submit": False})
                    execute(dry)
                    dry_result = call("GET", app_path)
                    counts = fixture.get(origin + "/_test/request-counts").json()
                    require(dry_result["state"] == "DRY_RUN_COMPLETE" and counts["review"] == 1 and counts["submit"] == 0
                            and fixture.get(origin + "/ats/receipts/" + app_id).json() == {"status": "not_found"},
                            "dry run submitted or failed to complete form review")
                    report["observations"]["dry_run_submission_count"] = counts["submit"]
                    passed(active, "API mock-run filled/reviewed in Chromium and stopped before submit; no receipt and zero submissions")
                    active = "mock application fill"
                    require(all(step in dry_result["trace"] for step in ("FILL_CONTACT", "VALIDATE")),
                            "browser fill checkpoints missing")
                    passed(active, "browser validation and mock server review succeeded after standard/custom field fill")
                    active = "mock upload"
                    require("UPLOAD_RESUME" in dry_result["trace"],
                            "upload checkpoints missing")
                    from services.applications.attempt_history import read_attempt_history
                    attempts = read_attempt_history(store, profile["id"], app_id)["attempts"]
                    uploads = attempts[0]["verified_uploads"]
                    require({item["kind"] for item in uploads} == {"resume", "cover-letter"}
                            and all(any(item["document_id"] == doc["id"]
                                        and item["sha256"] == doc["sha256"]["pdf"] for doc in documents)
                                    for item in uploads), "browser upload byte provenance missing")
                    passed(active, "real browser selected generated files; mock server accepted multipart review and adapter verified bytes")

                    active = "mock confirmation"
                    submit = call("POST", app_path + "/mock-run", json={"submit": True})
                    execute(submit)
                    confirmed = call("GET", app_path)
                    receipt = fixture.get(origin + "/ats/receipts/" + app_id).json()
                    require(confirmed["state"] == "CONFIRMED" and receipt["submission_count"] == 1
                            and confirmed["confirmation"]["receipt"] == receipt["receipt"]
                            and receipt["confirmation_url"] == origin + "/ats/submit/B",
                            "mock confirmation not corroborated by owned fixture receipt")
                    passed(active, "API mock submit executed in Chromium; confirmation matches owned mock receipt")

                    active = "duplicate-submission protection"
                    again = call("POST", app_path + "/mock-run", json={"submit": True})
                    require(again["operation_id"] == submit["operation_id"], "retry operation identity changed")
                    replay = apply_mock(store, app_id, origin, root, submit=True, check=check)
                    require(replay["confirmation"] == confirmed["confirmation"]
                            and fixture.get(origin + "/_test/request-counts").json()["submit"] == 1,
                            "retry bypassed duplicate submission fence")
                    passed(active, "API retry reused operation; real adapter replay preserved confirmation and submit count stayed one")

                    active = "durable readback"
                    reopened = Store(db)
                    try:
                        restored = reopened.application(app_id)
                        require(restored["state"] == "CONFIRMED" and restored["confirmation"] == confirmed["confirmation"]
                                and reopened.count("applications") == 1, "fresh store lost confirmation or duplicated application")
                    finally:
                        reopened.engine.dispose()
                    require(json.loads(receipt_file.read_text())[app_id]["submission_count"] == 1,
                            "mock receipt not durable")
                    report["observations"].update(application_id=app_id, persisted_state=restored["state"], submission_count=1)
                    passed(active, "fresh database connection and on-disk mock receipt retain one confirmed application")
    except ModuleNotFoundError:
        report["checks"][active] = {"status": "NOT_RUN", "reason": "required local Python dependency unavailable"}
    except Exception as error:
        # Do not disclose arbitrary library, SQL or browser errors in public output.
        report["checks"][active] = {"status": "FAIL", "reason": "executed synthetic check failed (" + type(error).__name__ + ")"}
    finally:
        if store is not None:
            store.engine.dispose()


def main():
    request, output, timeout = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])
    report = json.loads(request.read_text(encoding="utf-8"))
    root = request.parent / "fixture"
    root.mkdir()
    report["workspace"] = str(root)
    report["runtime"] = {"pid": os.getpid(), "parent_pid": os.getppid(), "python": sys.version.split()[0], "platform": sys.platform,
                         "database": "owned temporary SQLite", "execution": "API + inline durable operation executor"}
    versions = {}
    for package in ("httpx", "fastapi", "SQLAlchemy", "playwright", "python-docx", "pypdf", "reportlab"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "unavailable"
    report["runtime"]["dependencies"] = versions
    exercise(report, root, timeout)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    output.write_text(json.dumps(report), encoding="utf-8")
    return exit_status(report)


if __name__ == "__main__":
    sys.exit(main())
