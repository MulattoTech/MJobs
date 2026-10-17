"""Workday's supported fixture remains local and submission-free."""
from services.applications.controlled_dry_run import template


def test_workday_uses_versioned_local_document_fixture():
    fixture = template("workday")
    assert fixture["template_id"] == "workday-local-v2"
    assert [page["id"] for page in fixture["pages"]] == ["contact", "documents"]
    assert 'type="file"' in fixture["document_html"]
    assert "myworkdayjobs.com" not in fixture["html"] + fixture["document_html"]
    assert "submit" not in fixture["document_html"]
