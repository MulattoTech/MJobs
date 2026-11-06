"""Question-prompt selection ignores nested unresolved-reason labels."""
from playwright.sync_api import sync_playwright


def _question_list(page):
    page.set_content("""
      <ul aria-label="Unresolved required questions">
        <li>
          <strong>Country</strong> (country)
          <p><strong>Unresolved reason:</strong> Persisted preflight is missing answers: country.</p>
          <button>Review this question</button>
        </li>
        <li>
          <strong>Approved fixture answer</strong> (fixture_question)
          <p><strong>Unresolved reason:</strong> Persisted preflight is missing answers: fixture_question.</p>
          <button>Review this question</button>
        </li>
      </ul>
    """)
    return page.get_by_role("list", name="Unresolved required questions", exact=True)


def test_primary_prompt_selection_ignores_nested_reason_strong():
    from cli.ui_e2e import exact_primary_question_row

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            unresolved = _question_list(page)
            row = exact_primary_question_row(unresolved, "Approved fixture answer")

            assert row.locator("strong").count() == 2
            assert row.locator(":scope > strong").inner_text().strip() == "Approved fixture answer"
            row.get_by_role("button", name="Review this question", exact=True).click()
        finally:
            browser.close()


def test_primary_prompt_selection_rejects_duplicate_exact_questions():
    from cli.ui_e2e import exact_primary_question_row

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            unresolved = _question_list(page)
            page.locator("ul").evaluate("ul => ul.insertAdjacentHTML('beforeend', `<li><strong>Approved fixture answer</strong> (duplicate)<p><strong>Unresolved reason:</strong> duplicate</p></li>`)")

            try:
                exact_primary_question_row(unresolved, "Approved fixture answer")
            except AssertionError:
                pass
            else:
                raise AssertionError("duplicate primary prompts must not select the first row")
        finally:
            browser.close()
