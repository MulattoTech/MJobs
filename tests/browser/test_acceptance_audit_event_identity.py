"""The ordered Step 53 browser check distinguishes identically labeled audit rows."""

import pytest
from playwright.sync_api import sync_playwright

from cli.ui_e2e import expect_audit_event_row


def test_audit_row_with_identical_labels_must_match_the_bound_event_id():
    selected_id = "a" * 32
    other_id = "b" * 32
    title = "Fictional mock application outcome confirmed"
    event_type = "APPLICATION_CONFIRMED"
    labels = f"<strong>{title}</strong> · Data Engineer at Example Systems Inc. · {event_type}"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(offline=True)
            page.set_content(
                '<ol aria-label="Audit history">'
                f'<li data-audit-event-id="{other_id}">{labels}</li>'
                f'<li data-audit-event-id="{selected_id}">{labels}</li></ol>')
            items = page.get_by_role("list", name="Audit history", exact=True).get_by_role("listitem")
            assert items.nth(0).inner_text() == items.nth(1).inner_text()
            event = {"id": selected_id, "index": 1, "type": event_type}
            expect_audit_event_row(items, event, title)
            with pytest.raises(AssertionError, match="data-audit-event-id"):
                expect_audit_event_row(items, {**event, "index": 0}, title)
        finally:
            browser.close()
