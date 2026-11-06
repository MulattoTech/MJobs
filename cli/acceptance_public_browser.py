"""Read-only browser observation joining collected public jobs to the UI."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import parse_qs, quote, urlsplit

import httpx
from playwright.sync_api import expect, sync_playwright


_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_JOBS = 5
_ENDPOINTS = {
    "wwr": "https://weworkremotely.com/remote-jobs.rss",
    "remotive": "https://remotive.com/api/remote-jobs",
}


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _identity(value: object, name: str) -> str:
    _require(isinstance(value, str) and _SAFE_ID.fullmatch(value) is not None,
             f"PUBLIC_BROWSER_{name.upper()}_INVALID")
    return value


def _loopback_origin(value: str) -> str:
    parsed = urlsplit(value)
    _require(parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
             and parsed.username is None and parsed.password is None and not parsed.query
             and not parsed.fragment and parsed.path in ("", "/"), "PUBLIC_BROWSER_LOOPBACK_REQUIRED")
    return f"{parsed.scheme}://{parsed.netloc}"


def _runtime_projection(value: dict) -> dict:
    """Keep only immutable hashes; omit local paths, container names and secrets."""
    _require(isinstance(value, dict), "PUBLIC_BROWSER_RUNTIME_IDENTITY_INVALID")
    projection = {}
    for key in ("source_manifest_sha256", "dockerfile_sha256"):
        item = value.get(key)
        if item is not None:
            _require(isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item),
                     "PUBLIC_BROWSER_RUNTIME_IDENTITY_INVALID")
            projection[key] = item
    for key in ("images", "frontend_assets"):
        item = value.get(key)
        if item is not None:
            _require(isinstance(item, dict) and len(item) <= 32,
                     "PUBLIC_BROWSER_RUNTIME_IDENTITY_INVALID")
            normalized = {}
            for name, digest in item.items():
                _require(isinstance(name, str) and len(name) <= 80
                         and isinstance(digest, str)
                         and (re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                              or re.fullmatch(r"[0-9a-f]{64}", digest)),
                         "PUBLIC_BROWSER_RUNTIME_IDENTITY_INVALID")
                normalized[name] = digest
            projection[key] = normalized
    _require(projection, "PUBLIC_BROWSER_RUNTIME_IDENTITY_INVALID")
    return projection


def _public_inputs(base_url: str, baseline: dict, public_artifact: dict,
                   runtime_identity: dict) -> tuple[str, list[str], dict]:
    origin = _loopback_origin(base_url)
    _require(isinstance(baseline, dict) and isinstance(public_artifact, dict),
             "PUBLIC_BROWSER_INPUT_INVALID")
    _require(public_artifact.get("result") == "PASS"
             and public_artifact.get("external_submission_enabled") is False
             and public_artifact.get("source") == baseline.get("source")
             and isinstance(public_artifact.get("source"), dict)
             and isinstance(public_artifact["source"].get("commit"), str)
             and re.fullmatch(r"[0-9a-f]{40}", public_artifact["source"]["commit"])
             and _SAFE_ID.fullmatch(str(public_artifact.get("acceptance_run_id", "")))
             and public_artifact.get("acceptance_run_id"),
             "PUBLIC_BROWSER_SOURCE_RECEIPT_INVALID")
    identifiers = ("profile_id", "application_id", "operation_id", "request_id")
    for key in identifiers:
        _identity(baseline.get(key), key)
        _require(public_artifact.get(key) == baseline[key], "PUBLIC_BROWSER_BASELINE_CHANGED")
    selected_job = _identity(public_artifact.get("selected_application_job_id"),
                             "selected_application_job_id")
    if baseline.get("selected_application_job_id") is not None:
        _require(baseline["selected_application_job_id"] == selected_job,
                 "PUBLIC_BROWSER_SELECTED_JOB_CHANGED")
    jobs = public_artifact.get("observed_real_job_ids")
    observations = public_artifact.get("observations")
    _require(isinstance(jobs, list) and 1 <= len(jobs) <= _MAX_JOBS
             and all(isinstance(value, str) and _SAFE_ID.fullmatch(value) for value in jobs)
             and len(set(jobs)) == len(jobs) and selected_job not in jobs
             and isinstance(observations, list) and len(observations) == len(jobs),
             "PUBLIC_BROWSER_JOB_SET_INVALID")
    by_id = {row.get("job_id"): row for row in observations if isinstance(row, dict)}
    _require(set(by_id) == set(jobs), "PUBLIC_BROWSER_PROVENANCE_SET_INVALID")
    source_id = _identity(public_artifact.get("source_id"), "source_id")
    endpoint = public_artifact.get("endpoint")
    provider = public_artifact.get("provider")
    _require(provider in _ENDPOINTS and endpoint == _ENDPOINTS.get(provider),
             "PUBLIC_BROWSER_SOURCE_INVALID")
    for job_id, row in by_id.items():
        _require(row.get("source_id") == source_id and row.get("source_endpoint") == endpoint
                 and row.get("fictional") is False and row.get("native_posting_id") is not None
                 and isinstance(row.get("postings"), list) and row["postings"],
                 "PUBLIC_BROWSER_PROVENANCE_INVALID")
    return origin, jobs, {
        "profile_id": baseline["profile_id"],
        "application_id": baseline["application_id"],
        "operation_id": baseline["operation_id"],
        "request_id": baseline["request_id"],
        "selected_application_job_id": selected_job,
        "collection_operation_id": _identity(public_artifact.get("collection_operation_id"),
                                              "collection_operation_id"),
        "collection_request_id": _identity(public_artifact.get("collection_request_id"),
                                            "collection_request_id"),
        "acceptance_run_id": public_artifact["acceptance_run_id"],
        "source": public_artifact["source"],
        "source_id": source_id,
        "provider": provider,
        "endpoint": endpoint,
        "execution_scope": public_artifact.get("execution_scope"),
        "observations": by_id,
        "runtime_identity": _runtime_projection(runtime_identity),
    }


def _api_json(client: httpx.Client, path: str) -> dict:
    response = client.get(path)
    response.raise_for_status()
    payload = response.json()
    _require(isinstance(payload, dict), "PUBLIC_BROWSER_API_SHAPE_INVALID")
    return payload


def _state_snapshot(client: httpx.Client, profile_id: str, application_id: str) -> dict:
    profile = _api_json(client, f"/profiles/{profile_id}")
    application = _api_json(client, f"/profiles/{profile_id}/applications/{application_id}")
    documents = _api_json(client, f"/profiles/{profile_id}/applications/{application_id}/documents")
    _require(profile.get("id") == profile_id and application.get("id") == application_id
             and application.get("profile_id") == profile_id, "PUBLIC_BROWSER_BASELINE_READBACK_INVALID")
    return {
        "profile_sha256": _digest(profile),
        "application_sha256": _digest(application),
        "documents_sha256": _digest(documents),
        "profile_version": profile.get("version"),
        "application_job_id": application.get("job_id"),
        "application_route_id": application.get("route_id"),
        "document_ids": sorted(row.get("id") for row in documents.get("documents", [])),
    }


def _restriction(postings: list[dict], provider: str) -> dict | None:
    """Return a source-authored geography restriction, never inferred eligibility."""
    for posting in postings:
        raw = posting.get("raw")
        if not isinstance(raw, dict):
            continue
        if provider == "remotive":
            value = raw.get("candidate_required_location")
            field = "candidate_required_location"
        else:
            value = raw.get("region")
            field = "region"
        if (isinstance(value, str) and value.strip()
                and value.strip().casefold() not in {"remote", "anywhere in the world", "worldwide"}):
            return {"source_field": field, "source_value": value.strip(),
                    "source_endpoint": posting.get("source")}
    return None


def _join_postings(job: dict, observation: dict, provider: str) -> list[dict]:
    proof_rows = observation.get("postings")
    _require(isinstance(proof_rows, list) and 1 <= len(proof_rows) <= 20,
             "PUBLIC_BROWSER_PROVENANCE_SET_INVALID")
    proved = {row.get("posting_id"): row for row in proof_rows
              if isinstance(row, dict)}
    _require(len(proved) == len(proof_rows), "PUBLIC_BROWSER_PROVENANCE_SET_INVALID")
    joined = []
    for posting in job.get("postings") or []:
        proof = proved.get(posting.get("id"))
        raw = posting.get("raw")
        if not proof or not isinstance(raw, dict):
            continue
        _require(posting.get("source") == observation.get("source_endpoint")
                 and posting.get("fetched_at") == proof.get("fetched_at")
                 and _digest(raw) == proof.get("raw_sha256"),
                 "PUBLIC_BROWSER_POSTING_SNAPSHOT_MISMATCH")
        snapshots = {row.get("id"): row for row in posting.get("snapshots", [])
                     if isinstance(row, dict)}
        expected_snapshots = proof.get("snapshots")
        _require(isinstance(expected_snapshots, list) and 1 <= len(expected_snapshots) <= 20,
                 "PUBLIC_BROWSER_PROVENANCE_SET_INVALID")
        for expected in expected_snapshots:
            actual = snapshots.get(expected.get("id"))
            _require(actual is not None and actual.get("sha256") == expected.get("sha256")
                     and expected.get("sha256") == proof.get("raw_sha256"),
                     "PUBLIC_BROWSER_POSTING_SNAPSHOT_MISMATCH")
        if provider == "remotive":
            _require(str(raw.get("id")) == str(observation.get("native_posting_id"))
                     and raw.get("title") == job.get("title")
                     and raw.get("company_name") == job.get("company"),
                     "PUBLIC_BROWSER_SOURCE_FIELDS_MISMATCH")
        else:
            _require(str(posting.get("source_job_id")) == str(observation.get("native_posting_id")),
                     "PUBLIC_BROWSER_SOURCE_FIELDS_MISMATCH")
            _require(isinstance(raw.get("title"), str) and job.get("title") in raw["title"]
                     and job.get("company") in raw["title"],
                     "PUBLIC_BROWSER_SOURCE_FIELDS_MISMATCH")
        joined.append(posting)
    _require(joined, "PUBLIC_BROWSER_POSTING_SNAPSHOT_MISMATCH")
    return joined


def run(base_url: str, baseline: dict, public_artifact: dict, *,
        runtime_identity: dict) -> dict:
    """Open collected jobs in the local UI without making or enabling writes.

    The browser only visits the loopback app and permits GET/HEAD traffic.
    All output is bounded to the source receipt's five-job cap and contains
    field visibility booleans plus hashes, not profile content or posting text.
    """
    origin, job_ids, receipt = _public_inputs(base_url, baseline, public_artifact,
                                               runtime_identity)
    profile_id = receipt["profile_id"]
    application_id = receipt["application_id"]
    observations = receipt.pop("observations")
    public_checks = []
    external_requests = []
    blocked_writes = []
    page_errors = []
    with httpx.Client(base_url=origin, timeout=10, trust_env=False,
                      follow_redirects=False) as api:
        before = _state_snapshot(api, profile_id, application_id)
        _require(before["profile_version"] == public_artifact.get("profile_version")
                 and before["application_job_id"] == receipt["selected_application_job_id"]
                 and before["application_route_id"] == public_artifact.get("selected_application_route_id")
                 and (baseline.get("selected_application_route_id") is None
                      or before["application_route_id"] == baseline["selected_application_route_id"]),
                 "PUBLIC_BROWSER_BASELINE_SELECTION_CHANGED")
        jobs = {}
        for job_id in job_ids:
            job = _api_json(api, f"/jobs/{job_id}?profile_id={profile_id}")
            attrs = job.get("attributes") or {}
            evidence = observations[job_id]
            _require(job.get("id") == job_id and job_id != receipt["selected_application_job_id"]
                     and attrs.get("fictional") is False and attrs.get("provider") == receipt["provider"]
                     and attrs.get("source_id") == receipt["source_id"]
                     and attrs.get("native_posting_id") == evidence["native_posting_id"]
                     and isinstance(job.get("title"), str) and isinstance(job.get("company"), str),
                     "PUBLIC_BROWSER_JOB_READBACK_INVALID")
            joined = _join_postings(job, evidence, receipt["provider"])
            jobs[job_id] = {"detail": job, "source_postings": joined}

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1365, "height": 1000})
            local_origin = urlsplit(origin).netloc

            def local_read_only(route):
                request = route.request
                if urlsplit(request.url).netloc != local_origin:
                    external_requests.append(request.url)
                    route.abort()
                elif request.method not in {"GET", "HEAD"}:
                    blocked_writes.append({"method": request.method,
                                           "path": urlsplit(request.url).path})
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", local_read_only)
            page = context.new_page()
            page.set_default_timeout(10_000)
            page.add_init_script(
                "localStorage.setItem('mjobs-profile', " + json.dumps(profile_id) + ");"
            )
            page.on("pageerror", lambda error: page_errors.append(str(error)[:200]))
            page.goto(origin, wait_until="domcontentloaded")
            profile_select = page.get_by_label("Active profile", exact=True)
            expect(profile_select).to_have_value(profile_id)
            page.get_by_role("navigation").get_by_role(
                "button", name=re.compile(r"^Jobs\b")
            ).click()
            expect(page.locator(".job-list")).to_be_visible()

            for job_id in job_ids:
                job_record = jobs[job_id]
                job = job_record["detail"]
                source_postings = job_record["source_postings"]
                title = job["title"]
                company = job["company"]
                card = page.locator("button.job-card").filter(has_text=company).filter(has_text=title)
                expect(card).to_have_count(1)
                expect(card).to_be_visible()
                expect(card.locator(".eyebrow")).to_have_text(company)
                expect(card.locator(".job-card-title")).to_have_text(title)
                provenance = card.get_by_label("Job route provenance", exact=True)
                expect(provenance).to_be_visible()
                source_label = urlsplit(receipt["endpoint"]).netloc + urlsplit(receipt["endpoint"]).path
                provenance_text = provenance.inner_text()
                provider_text = str(job.get("provider") or "")
                source_visible = source_label in provenance_text
                provider_visible = bool(provider_text) and provider_text in provenance_text
                expect(provenance).to_contain_text(source_label)
                expect(provenance).to_contain_text("APPLICATION PROVIDER")
                expect(provenance).to_contain_text(provider_text)
                expected_detail_path = "/jobs/" + quote(job_id, safe="")

                def selected_detail_response(response):
                    parsed = urlsplit(response.url)
                    return (response.request.method == "GET" and parsed.netloc == local_origin
                            and parsed.path == expected_detail_path
                            and parse_qs(parsed.query).get("profile_id") == [profile_id])

                with page.expect_response(selected_detail_response) as selected_readback:
                    card.click()
                detail_readback = selected_readback.value
                _require(detail_readback.status == 200, "PUBLIC_BROWSER_SELECTED_JOB_READBACK_FAILED")
                selected_job = detail_readback.json()
                _require(selected_job.get("id") == job_id
                         and selected_job.get("title") == title
                         and selected_job.get("company") == company
                         and (selected_job.get("attributes") or {}).get("native_posting_id")
                             == (job.get("attributes") or {}).get("native_posting_id")
                         and (selected_job.get("attributes") or {}).get("source_id") == receipt["source_id"],
                         "PUBLIC_BROWSER_SELECTED_JOB_READBACK_MISMATCH")
                selected_postings = _join_postings(selected_job, observations[job_id],
                                                   receipt["provider"])
                detail = page.locator("section.job-detail")
                expect(detail.locator("h2").first).to_have_text(title)
                expect(detail.locator(".eyebrow").first).to_have_text(company)
                location_text = str(job.get("location") or "")
                remote_type = str(job.get("remote_type") or "")
                detail_text = detail.inner_text()
                location_visible = bool(location_text) and location_text in detail_text
                remote_type_visible = bool(remote_type) and remote_type in detail_text
                expect(detail).to_contain_text(location_text)
                expect(detail).to_contain_text(remote_type)
                native_id = str((job.get("attributes") or {}).get("native_posting_id"))
                source_evidence = detail.locator(".provenance").filter(
                    has_text=source_postings[0].get("source", ""))
                expect(source_evidence).to_have_count(len(source_postings))
                expect(source_evidence.first).to_be_visible()
                expect(source_evidence.first).to_contain_text(source_postings[0].get("source", ""))
                source_urls = {posting.get("source_url") for posting in source_postings
                               if isinstance(posting.get("source_url"), str)}
                visible_hrefs = {
                    source_evidence.nth(index).locator("a").nth(link_index).get_attribute("href")
                    for index in range(source_evidence.count())
                    for link_index in range(source_evidence.nth(index).locator("a").count())
                    if source_evidence.nth(index).locator("a").nth(link_index).is_visible()
                }
                source_link_visible = bool(source_urls) and source_urls.issubset(visible_hrefs)
                expected_hashes = {snapshot["sha256"] for posting in source_postings
                                   for snapshot in posting.get("snapshots", [])}
                source_evidence_text = source_evidence.all_inner_texts()
                source_hash_visible = bool(expected_hashes) and all(
                    any(f"SHA256 {digest}" in text for text in source_evidence_text)
                    for digest in expected_hashes
                )
                restriction = _restriction(source_postings, receipt["provider"])
                restriction_visible = bool(restriction and restriction["source_value"] in detail_text)
                check_map = {
                    "active_profile_matches": profile_select.input_value() == profile_id,
                    "job_card_title_visible": card.locator(".job-card-title").text_content() == title,
                    "job_card_company_visible": card.locator(".eyebrow").text_content() == company,
                    "job_detail_title_visible": detail.locator("h2").first.text_content() == title,
                    "job_detail_company_visible": detail.locator(".eyebrow").first.text_content() == company,
                    "source_provider_visible": source_visible,
                    "application_provider_visible": provider_visible,
                    "canonical_job_id_bound_to_selected_detail_readback": selected_job.get("id") == job_id,
                    "native_posting_id_bound_to_source_snapshot": bool(selected_postings),
                    "source_reference_link_visible": source_link_visible,
                    "snapshot_hash_visible": source_hash_visible,
                    "location_visible": location_visible,
                    "remote_type_visible": remote_type_visible,
                    "source_remote_restriction_visible": restriction_visible,
                }
                public_checks.append({
                    "job_id": job_id,
                    "title": title[:200],
                    "company": company[:200],
                    "provider": provider_text[:80],
                    "source_id": receipt["source_id"],
                    "source_endpoint": receipt["endpoint"],
                    "native_posting_id": native_id[:128],
                    "selected_job_api_readback_sha256": _digest({
                        "id": selected_job["id"], "title": selected_job["title"],
                        "company": selected_job["company"],
                        "native_posting_id": (selected_job.get("attributes") or {}).get("native_posting_id"),
                        "source_posting_ids": [posting["id"] for posting in selected_postings],
                    }),
                    "checks": check_map,
                    "restriction": ({**restriction, "visible": restriction_visible,
                        "claim_scope": "SOURCE_REPORTED_LOCATION_ONLY; NOT_WORK_AUTHORIZATION"}
                        if restriction else None),
                })
            context.close()
            browser.close()

        after = _state_snapshot(api, profile_id, application_id)

    _require(before == after, "PUBLIC_BROWSER_BASELINE_MUTATED")
    _require(not external_requests, "PUBLIC_BROWSER_EXTERNAL_EGRESS_ATTEMPTED")
    _require(not page_errors, "PUBLIC_BROWSER_PAGE_ERROR")

    required_11 = (
        "active_profile_matches", "job_card_title_visible", "job_card_company_visible",
        "job_detail_title_visible", "job_detail_company_visible", "source_provider_visible",
        "application_provider_visible", "canonical_job_id_bound_to_selected_detail_readback",
        "native_posting_id_bound_to_source_snapshot", "source_reference_link_visible",
        "snapshot_hash_visible",
    )
    visible_11 = all(all(row["checks"][name] for name in required_11)
                     for row in public_checks)
    any_restriction = any(row["restriction"] and row["restriction"]["visible"]
                          for row in public_checks)
    source_scope = receipt["execution_scope"] == "LIVE_PUBLIC_READ_ONLY"
    step11_status = "PASS" if visible_11 and source_scope else (
        "PARTIAL" if not visible_11 else "NOT_RUN")
    step16_status = "PASS" if any_restriction and source_scope else (
        "PARTIAL" if not any_restriction else "NOT_RUN")
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    evidence = {
        "schema_version": 1,
        "result": "PASS",
        "timestamp_utc": timestamp,
        "evidence_kind": "PUBLIC_BROWSER_READ_ONLY",
        **{key: receipt[key] for key in (
            "acceptance_run_id", "profile_id", "application_id", "operation_id", "request_id",
            "collection_operation_id", "collection_request_id", "selected_application_job_id",
            "source", "source_id", "provider", "endpoint", "execution_scope",
        )},
        "observed_real_job_ids": job_ids,
        "runtime_identity": receipt["runtime_identity"],
        "runtime_identity_sha256": _digest(receipt["runtime_identity"]),
        "browser": {"loopback_only": True, "get_head_only": True,
                    "external_requests": 0, "blocked_write_attempts": len(blocked_writes),
                    "blocked_write_paths": blocked_writes[:20], "page_errors": 0},
        "baseline_readback": {
            "profile_version": before["profile_version"],
            "selected_application_job_id": before["application_job_id"],
            "selected_application_route_id": before["application_route_id"],
            "document_ids": before["document_ids"],
            "unchanged": True,
            "before_sha256": _digest(before),
            "after_sha256": _digest(after),
        },
        "observations": public_checks,
        "acceptance_observations": [
            {"step": 11, "status": step11_status,
             "checks": {"public_jobs_visible_in_same_profile_ui": visible_11},
             "continuity": {key: receipt[key] for key in
                            ("profile_id", "application_id", "operation_id", "request_id",
                             "selected_application_job_id")}},
            {"step": 16, "status": step16_status,
             "checks": {"source_remote_restriction_visible": any_restriction},
             "continuity": {key: receipt[key] for key in
                            ("profile_id", "application_id", "operation_id", "request_id",
                             "selected_application_job_id")}},
        ],
    }
    evidence["sha256"] = _digest(evidence)
    return evidence
