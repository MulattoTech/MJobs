"""Numbered, sanitized UI evidence for the supported MJ-REQ-292 UI slice."""

import re


OBSERVATION_STATUSES = {"PASS", "PARTIAL", "FAIL", "NOT_RUN", "BLOCKED"}
EVIDENCE_KINDS = {"CONTROLLED_FIXTURE", "PUBLIC_READ_ONLY"}
CONTINUITY_KEYS = {
    "profile_id", "application_id", "operation_id", "request_id", "job_id",
    "document_id", "document_ids",
}
OBSERVABLE_RESULT_MAX_LENGTH = 240
CHECKS_MAX_LENGTH = 12
_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SAFE_CHECK_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SAFE_OBSERVABLE_RESULT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,:;()/_-]*\Z")
_SAFE_CONTINUITY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SAFE_SHA = re.compile(r"[a-f0-9]{40}\Z")
_SUPPORTED_STEP_IDS = frozenset((*range(2, 32), *range(32, 36), *range(36, 54)))
REQUIRED_CHECKS_BY_STEP = {
    2: frozenset({"dashboard_heading_visible"}),
    3: frozenset({"first_run_wizard_completed_with_persisted_configuration"}),
    4: frozenset({"resume_upload_and_extraction_visible"}),
    5: frozenset({"extracted_suggestions_remain_unapproved"}),
    6: frozenset({"corrected_fact_read_back", "corrected_fact_remains_pending"}),
    7: frozenset({"approved_fact_set_matches_review"}),
    8: frozenset({"remote_it_search_read_back", "remote_data_search_read_back"}),
    9: frozenset({"source_rows_visible", "multiple_sources_enabled"}),
    10: frozenset({"discovery_operation_completed", "search_scoped_job_readback"}),
    11: frozenset({"same_profile_public_readonly_jobs_observed"}),
    12: frozenset({"matching_job_visible", "nonmatching_job_hidden"}),
    13: frozenset({"filtered_job_visible", "other_title_hidden"}),
    14: frozenset({"match_sort_order"}),
    15: frozenset({"salary_range_visible", "salary_filter_retains_jobs"}),
    16: frozenset({"remote_filter_returned_a_remote_job", "remote_job_type_visible",
                   "employer_remote_restrictions_available"}),
    17: frozenset({"canonical_duplicate_job_available", "duplicate_count_matches_readback"}),
    18: frozenset({"canonical_job_detail_visible"}),
    19: frozenset({"source_copy_ids_read_back", "source_copy_panels_visible"}),
    20: frozenset({"verified_ats_authority_visible", "preferred_route_readback"}),
    21: frozenset({"application_route_visible"}),
    22: frozenset({"visible_numeric_match_score",
                   "selected_job_requirement_explanation_visible"}),
    23: frozenset({"previously_unknown_employer_observed"}),
    24: frozenset({"employer_discovery_ran_automatically"}),
    25: frozenset({"official_employer_domain_verified"}),
    26: frozenset({"employer_careers_page_found"}),
    27: frozenset({"employer_ats_tenant_identified"}),
    28: frozenset({"employer_source_enrolled_automatically"}),
    29: frozenset({"employer_openings_enumerated"}),
    30: frozenset({"employer_only_opening_observed"}),
    31: frozenset({"employer_duplicate_sources_collapsed"}),
    32: frozenset({"strong_match_available", "saved_status_readback"}),
    33: frozenset({"tailored_resume_uses_selected_approved_facts", "omitted_fact_visible_and_excluded"}),
    34: frozenset({"generated_base_visible", "tailored_version_visible",
                   "omission_and_change_summary_visible"}),
    35: frozenset({"cover_letter_generated", "cross_job_uniqueness_compared"}),
    36: frozenset({"application_prepared_readback", "profile_job_pins_match"}),
    37: frozenset({"selected_route_matches_application", "route_bound_to_job"}),
    38: frozenset({"supported_adapter_matches_provider", "provider_matches_route"}),
    39: frozenset({"playwright_form_opened", "selected_application_loaded"}),
    40: frozenset({"contact_fields_match_verified_profile", "contact_fields_readback_verified"}),
    41: frozenset({"resume_upload_matches_document", "resume_sha256_matches"}),
    42: frozenset({"cover_letter_upload_matches_document", "cover_letter_sha256_matches"}),
    43: frozenset({"approved_answers_match_questions", "approved_answers_readback_verified"}),
    44: frozenset({"uncertain_question_paused", "submit_not_attempted"}),
    45: frozenset({"explicit_answer_approval_persisted", "approval_bound_to_question"}),
    46: frozenset({"reviewed_application_resumed", "stale_approval_rejected"}),
    47: frozenset({"current_form_validated", "profile_document_pins_match"}),
    48: frozenset({"dry_run_receipt_persisted", "no_external_post_recorded"}),
    49: frozenset({"owned_mock_received_submit", "external_submission_absent"}),
    50: frozenset({"authoritative_confirmation_detected", "confirmation_count_one"}),
    51: frozenset({"history_receipt_persisted", "repeated_readback_idempotent"}),
    52: frozenset({"tracker_matches_application", "tracker_survives_reload"}),
    53: frozenset({"audit_sequence_complete", "audit_scope_matches_application",
                   "audit_values_redacted"}),
}


def validate_run_id(run_id):
    if not isinstance(run_id, str) or not _SAFE_RUN_ID.fullmatch(run_id):
        raise ValueError("run_id must be a bounded identifier")
    return run_id


def visible_numeric_match_score(rendered_text, expected_score):
    """Compare the visible percent number with API data despite integer formatting."""
    if (not isinstance(rendered_text, str)
            or not isinstance(expected_score, (int, float))
            or isinstance(expected_score, bool)):
        return False
    match = re.match(r"\s*(\d+(?:\.\d+)?)%", rendered_text)
    if match is None:
        return False
    try:
        return abs(float(match.group(1)) - float(expected_score)) < 1e-9
    except (OverflowError, ValueError):
        return False


def current_match_readback(detail, *, job_id, profile_version_before,
                           profile_version_after):
    """Accept only the selected job's score from an unchanged profile version."""
    if (not isinstance(detail, dict) or detail.get("id") != job_id
            or not isinstance(profile_version_before, str)
            or not profile_version_before
            or profile_version_after != profile_version_before):
        return False
    match = detail.get("match")
    score = match.get("score") if isinstance(match, dict) else None
    return (isinstance(score, (int, float)) and not isinstance(score, bool)
            and 0 <= score <= 100)


def build_observation(*, step, status, observable_result, evidence_kind,
                      source_commit, run_id, continuity=None, checks=None):
    """Validate and project one step observation onto the runner-owned schema."""
    if (not isinstance(step, int) or isinstance(step, bool)
            or step not in _SUPPORTED_STEP_IDS):
        raise ValueError("unsupported MJ-REQ-292 step")
    if status not in OBSERVATION_STATUSES:
        raise ValueError("invalid observation status")
    if evidence_kind not in EVIDENCE_KINDS:
        raise ValueError("invalid evidence kind")
    if not isinstance(source_commit, str) or not _SAFE_SHA.fullmatch(source_commit):
        raise ValueError("source_commit must be a full lowercase commit SHA")
    validate_run_id(run_id)
    if (not isinstance(observable_result, str)
            or not 1 <= len(observable_result) <= OBSERVABLE_RESULT_MAX_LENGTH
            or not _SAFE_OBSERVABLE_RESULT.fullmatch(observable_result)
            or "://" in observable_result or "@" in observable_result):
        raise ValueError("observable_result must be bounded sanitized text")
    if checks is None:
        checks = []
    if not isinstance(checks, list) or len(checks) > CHECKS_MAX_LENGTH:
        raise ValueError("checks must be a bounded list")
    safe_checks = []
    names = set()
    for check in checks:
        if (not isinstance(check, dict) or set(check) != {"name", "passed"}
                or not isinstance(check["name"], str)
                or not _SAFE_CHECK_NAME.fullmatch(check["name"])
                or not isinstance(check["passed"], bool)
                or check["name"] in names):
            raise ValueError("checks require unique safe identifier names and boolean outcomes")
        names.add(check["name"])
        safe_checks.append({"name": check["name"], "passed": check["passed"]})
    required_names = REQUIRED_CHECKS_BY_STEP.get(step)
    if required_names is not None and names != required_names:
        raise ValueError("step checks must exactly match its registered check names")
    if status == "PASS" and (not safe_checks or not all(row["passed"] for row in safe_checks)):
        raise ValueError("PASS requires nonempty successful assertions")
    if step == 11 and status == "PASS" and evidence_kind != "PUBLIC_READ_ONLY":
        raise ValueError("step 11 requires PUBLIC_READ_ONLY evidence")
    if continuity is None:
        continuity = {}
    if not isinstance(continuity, dict) or set(continuity) - CONTINUITY_KEYS:
        raise ValueError("continuity contains unsupported fields")
    safe_continuity = {}
    for key, value in continuity.items():
        if key == "document_ids":
            if (not isinstance(value, list) or not 1 <= len(value) <= 10
                    or any(not isinstance(item, str)
                           or not _SAFE_CONTINUITY_ID.fullmatch(item) for item in value)
                    or len(set(value)) != len(value)):
                raise ValueError("document_ids must contain 1 to 10 unique bounded identifiers")
            safe_continuity[key] = list(value)
            continue
        if (not isinstance(value, str) or not _SAFE_CONTINUITY_ID.fullmatch(value)):
            raise ValueError("continuity values must be bounded opaque identifiers")
        safe_continuity[key] = value
    if status == "PASS" and step in {*range(17, 23), *range(32, 36)}:
        if "job_id" not in safe_continuity:
            raise ValueError("passing job observations require job_id continuity")
    if status == "PASS" and step in {33, 34, 35} and "application_id" not in safe_continuity:
        raise ValueError("passing application observations require application_id continuity")
    if status == "PASS" and step in {33, 34} and "document_ids" not in safe_continuity:
        raise ValueError("passing document observations require document_ids continuity")
    if status == "PASS" and step == 34 and len(safe_continuity.get("document_ids", [])) < 2:
        raise ValueError("step 34 requires base and tailored document identifiers")
    if status == "PASS" and step == 35 and "document_id" not in safe_continuity:
        raise ValueError("step 35 requires document_id continuity")
    return {
        "step": step,
        "status": status,
        "observable_result": observable_result,
        "evidence_kind": evidence_kind,
        "source_commit": source_commit,
        "run_id": run_id,
        "continuity": safe_continuity,
        "checks": safe_checks,
    }


def run_fixture_job_observations(page, request, profile_id, navigate, observe,
                                 record_observed_jobs=None):
    """Exercise and observe fixture-backed Jobs UI behavior, without live providers."""
    import time
    from urllib.parse import urlsplit

    from playwright.sync_api import expect

    def record(step, status, result, checks, *, continuity=None):
        observe(step, status, result, evidence_kind="CONTROLLED_FIXTURE",
                checks=checks, continuity=continuity or {"profile_id": profile_id})

    def refresh_profile_matches():
        profile_before = request("/profiles/" + profile_id)
        version_before = profile_before.get("version")
        refresh_button = page.get_by_role("button", name="Refresh matches", exact=True)
        expect(refresh_button).to_be_enabled()
        with page.expect_response(lambda response:
                response.request.method == "POST"
                and urlsplit(response.url).path == "/matches") as refresh_response:
            refresh_button.click()
        response = refresh_response.value
        assert response.status == 202, response.text()
        refresh_request = response.request.post_data_json
        assert refresh_request["profile_id"] == profile_id
        request_id = refresh_request.get("request_id")
        assert isinstance(request_id, str) and request_id
        operation_id = response.json().get("operation_id")
        assert isinstance(operation_id, str) and operation_id
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            operation = request("/operations/" + operation_id)
            if operation.get("state") in (
                    "COMPLETED", "FAILED", "BLOCKED", "CANCELLED"):
                break
            time.sleep(0.5)
        assert operation.get("state") == "COMPLETED", operation
        params = operation.get("params") or {}
        assert operation.get("kind") == "DISCOVER"
        assert operation.get("entity_id") == profile_id
        assert params.get("profile_version") == version_before
        assert params.get("rescore_only") is True
        assert params.get("request_id") == request_id
        expect(refresh_button).to_be_enabled()
        profile_after = request("/profiles/" + profile_id)
        assert profile_after.get("id") == profile_id
        assert profile_after.get("version") == version_before
        return version_before

    navigate("Jobs")
    matched_profile_version = refresh_profile_matches()
    navigate("Sources")
    source_rows = request("/sources").get("sources", [])
    visible_source_names = []
    for row in source_rows:
        name = row.get("name") if isinstance(row, dict) else None
        if name:
            source_heading = page.get_by_role("heading", name=name, exact=True)
            expect(source_heading).to_be_visible()
            source_badge = source_heading.locator("xpath=../..").locator("span.tag")
            if row.get("enabled") is True:
                expect(source_badge).not_to_have_text("DISABLED")
            else:
                expect(source_badge).to_have_text("DISABLED")
            visible_source_names.append(name)
    enabled_count = sum(row.get("enabled") is True for row in source_rows)
    multiple_sources = len(source_rows) >= 2 and enabled_count >= 2
    record(9, "PASS" if multiple_sources else "PARTIAL",
           "Multiple configured job sources are enabled." if multiple_sources
           else "The fixture does not expose multiple enabled job sources.",
           [{"name": "source_rows_visible", "passed": len(visible_source_names) == len(source_rows)},
            {"name": "multiple_sources_enabled", "passed": multiple_sources}])

    navigate("Jobs")
    page.get_by_label("Saved search profile", exact=True).select_option("")
    page.get_by_label("Search jobs", exact=True).fill("data")
    data_card = page.get_by_role("button", name="Example Systems Inc. Data Engineer", exact=True)
    python_card = page.get_by_role("button", name="Example Systems Inc. Python Developer", exact=True)
    expect(data_card).to_be_visible()
    expect(python_card).to_have_count(0)
    record(12, "PASS", "Text search narrowed the visible fixture job list.",
           [{"name": "matching_job_visible", "passed": data_card.count() == 1},
            {"name": "nonmatching_job_hidden", "passed": python_card.count() == 0}])

    page.get_by_label("Search jobs", exact=True).fill("")
    page.get_by_label("Job title", exact=True).fill("Python Developer")
    expect(python_card).to_be_visible()
    expect(data_card).to_have_count(0)
    record(13, "PASS", "Job title filter retained only the requested fixture title.",
           [{"name": "filtered_job_visible", "passed": python_card.count() == 1},
            {"name": "other_title_hidden", "passed": data_card.count() == 0}])

    page.get_by_label("Job title", exact=True).fill("")
    page.get_by_label("Sort", exact=True).select_option("title")
    page.get_by_label("Sort", exact=True).select_option("score")
    score_sorted_jobs = request("/jobs?profile_id=" + profile_id + "&sort=score").get("jobs", [])
    expected_titles = [row.get("title") for row in score_sorted_jobs]
    if expected_titles:
        expect(page.locator(".job-card-title")).to_have_text(expected_titles)
    visible_titles = page.locator(".job-card-title").all_text_contents()
    descending_scores = [row.get("match_score") for row in score_sorted_jobs]
    scores_are_known = all(isinstance(score, (int, float)) and not isinstance(score, bool)
                           for score in descending_scores)
    order_is_match = (bool(expected_titles) and scores_are_known and visible_titles == expected_titles
                      and all(left >= right for left, right in zip(
                          descending_scores, descending_scores[1:])))
    record(14, "PASS" if order_is_match else "PARTIAL",
           "Visible fixture jobs follow the match-score order from readback." if order_is_match
           else "The fixture list did not expose a stable match-score order.",
           [{"name": "match_sort_order", "passed": order_is_match}])

    page.get_by_label("Salary availability", exact=True).select_option("KNOWN_ONLY")
    page.get_by_label("Minimum annual salary (USD)", exact=True).fill("70000")
    page.get_by_label("Maximum annual salary (USD)", exact=True).fill("120000")
    page.get_by_label("Work location", exact=True).select_option("REMOTE")
    expect(python_card).to_be_visible()
    expect(data_card).to_be_visible()
    salary_filter_visible = python_card.count() == 1 and data_card.count() == 1
    remote_types = {row.get("remote_type") for row in request(
        "/jobs?profile_id=" + profile_id).get("jobs", [])}
    remote_visible = "REMOTE" in remote_types
    jobs = request("/jobs?profile_id=" + profile_id).get("jobs", [])
    if record_observed_jobs is not None:
        record_observed_jobs([row["id"] for row in jobs
                              if isinstance(row, dict) and isinstance(row.get("id"), str)])
    job_a = next((row for row in jobs if row.get("title") == "Python Developer"), None)
    if job_a is None:
        record(17, "PARTIAL", "Fixture has no Python Developer canonical job for duplicate-count review.",
               [{"name": "canonical_duplicate_job_available", "passed": False}])
        return
    detail = request("/jobs/" + job_a["id"] + "?profile_id=" + profile_id)
    detail_posting_count = len(detail.get("postings", []))
    list_posting_count = job_a.get("posting_count")
    visible_count = page.get_by_role(
        "button", name="Example Systems Inc. Python Developer", exact=True).count()
    card_text = python_card.inner_text()
    count_visible = (isinstance(list_posting_count, int)
                     and f"{list_posting_count} posting" in card_text
                     and detail_posting_count == list_posting_count)
    posting_count = detail_posting_count
    record(17, "PASS" if count_visible and posting_count > 1 else "PARTIAL",
           "The job card and readback expose the same duplicate posting count." if count_visible and posting_count > 1
           else "The fixture does not expose multiple postings with a visible matching count.",
           [{"name": "canonical_duplicate_job_available", "passed": visible_count == 1},
            {"name": "duplicate_count_matches_readback", "passed": count_visible and posting_count > 1}],
           continuity={"profile_id": profile_id, "job_id": job_a["id"]})

    page.get_by_label("Search jobs", exact=True).fill("")
    python_card.click()
    detail_region = page.locator("section.job-detail")
    expect(detail_region.get_by_role("heading", name="Python Developer", exact=True)).to_be_visible()
    expect(detail_region.get_by_role("heading", name="Why this match?", exact=True)).to_be_visible()
    detail = request("/jobs/" + job_a["id"] + "?profile_id=" + profile_id)
    is_canonical_detail = (detail.get("id") == job_a.get("id")
                           and detail.get("title") == "Python Developer")
    record(18, "PASS" if detail_region.count() == 1 and is_canonical_detail else "PARTIAL",
           "The canonical fixture job opened in the detail panel."
           if is_canonical_detail else "The selected job detail did not match the canonical fixture identity.",
           [{"name": "canonical_job_detail_visible",
             "passed": detail_region.count() == 1 and is_canonical_detail}],
           continuity={"profile_id": profile_id, "job_id": job_a["id"]})

    attributes = detail.get("attributes") or {}
    salary_min = detail.get("salary_min", attributes.get("salary_min"))
    salary_max = detail.get("salary_max", attributes.get("salary_max"))
    salary_source = detail.get("salary_source", attributes.get("salary_source"))
    salary_data_available = (isinstance(salary_min, (int, float))
                             and isinstance(salary_max, (int, float)))
    salary_label = ("Estimated salary range (not employer-confirmed)"
                    if salary_source == "ESTIMATED"
                    else "Source-reported salary range (not independently verified)")
    salary_visible = (salary_data_available
                      and detail_region.get_by_text(salary_label, exact=False).count() > 0)
    record(15, "PASS" if salary_visible and salary_filter_visible else "PARTIAL",
           "Salary range is visible and the annual salary filter retains eligible jobs."
           if salary_visible and salary_filter_visible
           else "Fixture salary fields or the salary-filtered list are unavailable.",
           [{"name": "salary_range_visible", "passed": salary_visible},
            {"name": "salary_filter_retains_jobs", "passed": salary_filter_visible}])
    remote_type_visible = (detail.get("remote_type") == "REMOTE"
                           and detail.get("location") == "United States")
    if remote_type_visible:
        expect(detail_region.get_by_text("United States · REMOTE", exact=True)).to_be_visible()
    record(16, "PARTIAL",
           "Remote type and location are shown; fixture data does not state employer remote restrictions.",
           [{"name": "remote_filter_returned_a_remote_job", "passed": remote_visible},
            {"name": "remote_job_type_visible", "passed": remote_type_visible},
            {"name": "employer_remote_restrictions_available", "passed": False}])
    postings = detail.get("postings", [])
    expected_sources = {"board-A", "board-A-syndicated", "employer-A"}
    observed_sources = {row.get("source_job_id") for row in postings}
    visible_postings = detail_region.locator(".provenance")
    expect(visible_postings).to_have_count(len(postings))
    source_links_match = True
    for posting, panel in zip(postings, visible_postings.all()):
        source_url = posting.get("source_url")
        if not source_url:
            source_links_match = False
            continue
        expect(panel.get_by_role(
            "link", name="Open saved reference ↗", exact=True)).to_have_attribute(
                "href", source_url)
    all_copies = (len(postings) == 3 and observed_sources == expected_sources
                  and visible_postings.count() == len(postings) and source_links_match)
    record(19, "PASS" if all_copies else "PARTIAL",
           "Every known fixture source copy is listed in the job detail." if all_copies
           else "The fixture does not expose the complete expected source-copy set.",
           [{"name": "source_copy_ids_read_back",
             "passed": len(postings) == 3 and observed_sources == expected_sources},
            {"name": "source_copy_panels_visible",
             "passed": visible_postings.count() == len(postings) and source_links_match}],
           continuity={"profile_id": profile_id, "job_id": job_a["id"]})

    routes = detail.get("routes", [])
    ats_route = next((row for row in routes
                      if isinstance(row, dict)
                      and row.get("authority") == "VERIFIED_ATS"
                      and row.get("provider") == "mock-ats"), None)
    authoritative = detail.get("provider") == "mock-ats" and ats_route is not None
    ui_authority_visible = False
    route_selector = detail_region.get_by_label("Choose the application route for this job", exact=True)
    if authoritative:
        route_selector.select_option(ats_route["id"])
        expect(route_selector).to_have_value(ats_route["id"])
        selected_option_text = route_selector.locator("option:checked").inner_text()
        ui_authority_visible = (ats_route["provider"] in selected_option_text
                                and "verified identity evidence" in selected_option_text)
        if ats_route.get("preferred_by_profile") is not True:
            preference_button = detail_region.get_by_role(
                "button", name="Prefer this route for this employer", exact=True)
            expect(preference_button).to_be_visible()
            preference_button.click()
        expect(detail_region.get_by_role(
            "button", name="Clear employer route preference", exact=True)).to_be_visible()
        updated = request("/jobs/" + job_a["id"] + "?profile_id=" + profile_id)
        route_is_preferred = any(row.get("id") == ats_route["id"]
                                 and row.get("preferred_by_profile") is True
                                 for row in updated.get("routes", []))
    else:
        route_is_preferred = False
    record(20, "PASS" if authoritative and ui_authority_visible and route_is_preferred else "PARTIAL",
           "The verified employer ATS route is visibly preferred and persists on readback."
           if authoritative and route_is_preferred
           else "The fixture does not expose a verified, preferred employer ATS route.",
           [{"name": "verified_ats_authority_visible", "passed": authoritative and ui_authority_visible},
            {"name": "preferred_route_readback", "passed": route_is_preferred}],
           continuity={"profile_id": profile_id, "job_id": job_a["id"]})

    route_visible = False
    if ats_route is not None:
        route_link_visible = False
        application_links = detail_region.get_by_role(
            "link", name="Open saved application link ↗", exact=True)
        for link in application_links.all():
            if link.get_attribute("href") == ats_route.get("url"):
                expect(link).to_be_visible()
                route_link_visible = True
        route_visible = (route_selector.count() == 1
                         and route_selector.input_value() == ats_route["id"]
                         and route_link_visible)
        if route_visible:
            expect(route_selector).to_have_value(ats_route["id"])
    record(21, "PASS" if route_visible else "PARTIAL",
           "The selected verified employer route is visible as a saved application link."
           if route_visible else "No selected authoritative application route is visible in the fixture.",
           [{"name": "application_route_visible", "passed": route_visible}],
           continuity={"profile_id": profile_id, "job_id": job_a["id"]})

    profile_after_route = request("/profiles/" + profile_id)
    route_profile_version = profile_after_route.get("version")
    if route_profile_version != matched_profile_version:
        matched_profile_version = refresh_profile_matches()
        expect(python_card).to_be_visible()
        python_card.click()
        detail = request("/jobs/" + job_a["id"] + "?profile_id=" + profile_id)
        profile_after_route = request("/profiles/" + profile_id)
    detail = request("/jobs/" + job_a["id"] + "?profile_id=" + profile_id)
    profile_after_match_readback = request("/profiles/" + profile_id)
    match_is_current = current_match_readback(
        detail, job_id=job_a["id"],
        profile_version_before=matched_profile_version,
        profile_version_after=profile_after_match_readback.get("version"))

    match = detail.get("match") or {}
    selected_job_matches = (detail.get("id") == job_a.get("id")
                            and detail.get("title") == job_a.get("title"))
    match_score = match.get("score")
    numeric_score = (match_is_current and isinstance(match_score, (int, float))
                     and not isinstance(match_score, bool) and 0 <= match_score <= 100)
    visible_numeric_score = False
    if numeric_score:
        score_badge = detail_region.locator(".big-score")
        expect(score_badge).to_be_visible()
        visible_numeric_score = visible_numeric_match_score(
            score_badge.inner_text(), match_score)
    requirements = (match.get("components") or {}).get("requirements") or []
    concrete_requirement = next((row for row in requirements
                                 if isinstance(row, dict) and row.get("skill")
                                 and isinstance(row.get("explanation"), str)
                                 and row["explanation"].strip()), None)
    requirement_visible = False
    if concrete_requirement is not None and selected_job_matches and match_is_current:
        explanation_list = detail_region.get_by_role("list", name="Skill match explanation", exact=True)
        expect(explanation_list).to_contain_text(str(concrete_requirement["skill"]))
        expect(explanation_list).to_contain_text(str(concrete_requirement["explanation"]))
        requirement_visible = True
    expect(detail_region.get_by_role("heading", name="Why this match?", exact=True)).to_be_visible()
    if match_is_current:
        expect(detail_region.get_by_text("Supported by approved facts:", exact=False)).to_be_visible()
    if match.get("components", {}).get("skill_evidence_coverage"):
        expect(detail_region.get_by_role(
            "group", name="Base skill evidence coverage", exact=True)).to_be_visible()
    match_explanation_visible = selected_job_matches and visible_numeric_score and requirement_visible
    record(22, "PASS" if match_explanation_visible else "PARTIAL",
           "The selected job score and requirement explanation are visible."
           if match_explanation_visible else "The fixture match score or requirement explanation is incomplete.",
           [{"name": "visible_numeric_match_score", "passed": visible_numeric_score},
            {"name": "selected_job_requirement_explanation_visible", "passed": requirement_visible}],
           continuity={"profile_id": profile_id, "job_id": job_a["id"]})

    page.get_by_label("Salary availability", exact=True).select_option("DEFAULT")
    page.get_by_label("Minimum annual salary (USD)", exact=True).fill("")
    page.get_by_label("Maximum annual salary (USD)", exact=True).fill("")
    page.get_by_label("Work location", exact=True).select_option("")
    page.get_by_label("Sort", exact=True).select_option("score")
    expect(python_card).to_be_visible()
    expect(data_card).to_be_visible()
