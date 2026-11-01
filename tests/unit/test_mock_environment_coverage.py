"""Literal source/company/ATS fixture contracts using only the local ASGI app."""
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
import pytest

from fixtures.mock_server import create_mock_app


PDF = b"%PDF-1.4\n% fictional upload fixture\n%%EOF\n"
ANSWER = "Controlled fictional test application."


def fields(application_id="onepage-fixture", **changes):
    return {
        "name": "Alex Fixture", "email": "alex@example.invalid",
        "country": "United States", "remote": "yes", "reviewed": "on",
        "application_id": application_id, "answer": ANSWER, "confirmed": "on",
        **changes,
    }


def uploads(**changes):
    return {
        "resume": ("resume.pdf", PDF, "application/pdf"),
        "cover": ("cover.pdf", PDF, "application/pdf"),
        **changes,
    }


def test_scenario_is_explicit_and_baseline_board_is_preserved():
    with pytest.raises(ValueError):
        create_mock_app(scenario="unrecognized")
    with TestClient(create_mock_app()) as client:
        rows = client.get("/board/jobs").json()["jobs"]
        assert [row["source_job_id"] for row in rows] == ["board-A", "board-A-syndicated"]
        assert [row["requisition"] for row in rows] == ["A", "A"]
        assert [row["salary_min"] for row in rows] == [80000, 90000]
        assert [row["remote_type"] for row in rows] == ["REMOTE", "HYBRID"]


def test_board_coverage_keeps_duplicates_distinct_jobs_and_unknowns_explicit():
    with TestClient(create_mock_app(scenario="coverage")) as client:
        rows = client.get("/board/jobs").json()["jobs"]
        assert len(rows) == 4
        assert len({row["source_job_id"] for row in rows}) == 4
        assert {row["requisition"] for row in rows} == {"A", "B", "C"}
        copies = [row for row in rows if row["requisition"] == "A"]
        assert len(copies) == 2
        assert copies[0]["application_url"] == copies[1]["application_url"]
        assert copies[0]["salary_min"] != copies[1]["salary_min"]
        hourly = next(row for row in rows if row["requisition"] == "B")
        assert hourly["salary_period"] == "HOUR"
        assert 0 < hourly["salary_min"] < hourly["salary_max"] < 1000
        assert hourly["remote_type"] == "ONSITE"
        unknown = next(row for row in rows if row["requisition"] == "C")
        assert unknown["company"] is None
        assert unknown["company_website"] is None
        assert unknown["salary_min"] is None and unknown["salary_max"] is None
        assert unknown["salary_currency"] is None
        assert unknown["salary_period"] is None
        assert unknown["remote_type"] == "UNKNOWN"
        for key in ("A", "B", "C"):
            redirect = client.get(f"/board/apply/{key}", follow_redirects=False)
            assert redirect.status_code in {302, 303, 307, 308}
            assert redirect.headers["location"] == f"/ats/apply/{key}"
        assert client.get("/_test/request-counts").json() == {"review": 0, "submit": 0}


def test_named_employer_homepage_careers_redirect_portal_and_distinct_jobs():
    with TestClient(create_mock_app()) as client:
        homepage = BeautifulSoup(client.get("/company").text, "html.parser")
        assert homepage.h1.get_text() == "Example Systems Inc."
        careers = homepage.find("a", rel="careers")["href"]
        response = client.get(careers)
        assert [r.headers.get("location") for r in response.history] == ["/ats/example"]
        portal = BeautifulSoup(response.text, "html.parser")
        jobs_link = portal.find("a", attrs={"data-provider": "mock-ats"})
        assert jobs_link["data-tenant"] == "example"
        rows = client.get(jobs_link["href"]).json()["jobs"]
        assert {row["company"] for row in rows} == {"Example Systems Inc."}
        assert {row["requisition"] for row in rows} == {"A", "B"}
        assert len({row["title"] for row in rows}) == 2
        assert len({row["application_url"] for row in rows}) == 2
        assert client.get("/_test/request-counts").json() == {"review": 0, "submit": 0}


def test_onepage_has_all_controls_while_default_review_remains_multipage():
    with TestClient(create_mock_app()) as client:
        single = BeautifulSoup(client.get("/ats/apply/A?flow=onepage").text, "html.parser")
        forms = single.find_all("form")
        assert len(forms) == 1
        assert forms[0]["action"] == "/ats/onepage/A"
        names = {control.get("name") for control in forms[0].select("input, select, textarea")}
        assert {"name", "email", "resume", "cover", "country", "remote", "reviewed",
                "application_id", "answer", "confirmed"} <= names
        assert forms[0].find("input", attrs={"name": "resume", "type": "file"})
        assert forms[0].find("input", attrs={"name": "cover", "type": "file"})
        assert forms[0].find("select", attrs={"name": "country"})
        assert forms[0].find("input", attrs={"name": "remote", "type": "radio"})
        assert forms[0].find("input", attrs={"name": "confirmed", "type": "checkbox"})
        assert forms[0].find("textarea", attrs={"name": "answer"})
        first = BeautifulSoup(client.get("/ats/apply/A").text, "html.parser").form
        assert first["action"] == "/ats/review/A"
        assert first.find(attrs={"name": "answer"}) is None
        response = client.post("/ats/review/A", data=fields(), files=uploads())
        assert response.status_code == 200
        second = BeautifulSoup(response.text, "html.parser").form
        assert second["action"] == "/ats/submit/A"
        assert second.find("textarea", attrs={"name": "answer"})
        assert client.get("/ats/receipts/onepage-fixture").json() == {"status": "not_found"}


@pytest.mark.parametrize("missing", [
    "name", "email", "country", "remote", "reviewed", "application_id", "answer", "confirmed",
    "resume", "cover",
])
def test_onepage_server_rejects_each_missing_required_field_without_receipt(tmp_path, missing):
    data, files = fields(), uploads()
    data.pop(missing, None)
    files.pop(missing, None)
    receipt_file = tmp_path / "receipts.json"
    with TestClient(create_mock_app(receipt_file)) as client:
        response = client.post("/ats/onepage/A", data=data, files=files)
        assert response.status_code == 422
        assert 'role="alert"' in response.text
        assert client.get("/ats/receipts/onepage-fixture").json() == {"status": "not_found"}
    assert not receipt_file.exists()


@pytest.mark.parametrize("field", ["resume", "cover"])
@pytest.mark.parametrize("content", [b"not a PDF", b"%PDF-" + b"x" * 2_000_000],
                         ids=["invalid-magic", "over-limit"])
def test_onepage_rejects_invalid_or_oversized_upload_without_receipt(tmp_path, field, content):
    with TestClient(create_mock_app(tmp_path / "receipts.json")) as client:
        response = client.post("/ats/onepage/A", data=fields(),
                               files=uploads(**{field: (field + ".pdf", content, "application/pdf")}))
        assert response.status_code == 422
        assert client.get("/ats/receipts/onepage-fixture").json() == {"status": "not_found"}


def test_onepage_rejects_wrong_answer_and_active_conditional_field_on_server(tmp_path):
    with TestClient(create_mock_app(tmp_path / "receipts.json")) as client:
        wrong = client.post("/ats/onepage/A", data=fields(answer="Unapproved invented answer"), files=uploads())
        assert wrong.status_code == 422
        missing = client.post("/ats/onepage/A?dynamic=known", data=fields(), files=uploads())
        assert missing.status_code == 422
        assert client.get("/ats/receipts/onepage-fixture").json() == {"status": "not_found"}
        inactive = client.post("/ats/onepage/A?dynamic=known",
                               data=fields("inactive-condition", country="Canada"), files=uploads())
        assert inactive.status_code == 200
        assert client.get("/ats/receipts/inactive-condition").json()["submission_count"] == 1


def test_onepage_receipt_is_durable_idempotent_and_cannot_be_reused_for_other_job(tmp_path):
    path = tmp_path / "receipts.json"
    with TestClient(create_mock_app(path)) as client:
        first = client.post("/ats/onepage/A?dynamic=known", data=fields(region="Oklahoma"), files=uploads())
        assert first.status_code == 200
        saved = client.get("/ats/receipts/onepage-fixture").json()
        assert saved["requisition"] == "A" and saved["submission_count"] == 1
        assert saved["reference_number"] in first.text
        assert saved["confirmation_url"].endswith("/ats/onepage/A?dynamic=known")
    with TestClient(create_mock_app(path)) as client:
        replay = client.post("/ats/onepage/A?dynamic=known", data=fields(region="Oklahoma"), files=uploads())
        assert replay.status_code == 200
        assert client.get("/ats/receipts/onepage-fixture").json() == saved
        conflicting = client.post("/ats/onepage/B", data=fields(), files=uploads())
        assert conflicting.status_code == 409
        assert client.get("/ats/receipts/onepage-fixture").json() == saved


def test_multipage_uses_same_validation_and_rejects_cross_job_receipt_reuse(tmp_path):
    with TestClient(create_mock_app(tmp_path / "receipts.json")) as client:
        failed = client.post("/ats/review/B", data=fields(), files=uploads(resume=("r.pdf", b"invalid", "application/pdf")))
        assert failed.status_code == 422
        assert client.get("/_test/request-counts").json()["submit"] == 0
        reviewed = client.post("/ats/review/B", data=fields(), files=uploads())
        assert reviewed.status_code == 200
        submitted = client.post("/ats/submit/B", data=fields("multipage-fixture"))
        assert submitted.status_code == 200
        saved = client.get("/ats/receipts/multipage-fixture").json()
        assert saved["requisition"] == "B"
        assert client.post("/ats/submit/A", data=fields("multipage-fixture")).status_code == 409
        assert client.get("/ats/receipts/multipage-fixture").json() == saved


def test_restored_receipt_without_requisition_cannot_claim_another_application(tmp_path):
    import json

    path = tmp_path / "receipts.json"
    unbound = {"onepage-fixture": {"reference_number": "MOCK-unbound"}}
    path.write_text(json.dumps(unbound), encoding="utf-8")
    with TestClient(create_mock_app(path)) as client:
        response = client.post("/ats/onepage/A", data=fields(), files=uploads())
        assert response.status_code == 409
        assert json.loads(path.read_text(encoding="utf-8")) == unbound
