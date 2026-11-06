"""Contained UI-only setup and proof for the optional step 9 source observation.

The wrapper composes around an operator-owned public-source containment callback.
It adds one absent Remotive policy through the local Sources UI, observes it beside
the primary We Work Remotely policy at first dispatch, then disables only the
source it created before the primary helper can collect anything.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from cli.acceptance_public_source import FINGERPRINT_FIELDS


class SourceSetupError(ValueError):
    pass


EXTRA_PROVIDER = "remotive"
PRIMARY_PROVIDER = "wwr"
POLICY = {
    "provider": EXTRA_PROVIDER,
    "tenant": "default",
    "enabled": True,
    "poll_interval": 604800,
    "concurrency": 1,
    "request_timeout": 10,
    "rate_limit": 1,
    "jitter_seconds": 0,
}


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _policy_sha(source):
    try:
        return _sha({key: source[key] for key in FINGERPRINT_FIELDS})
    except (KeyError, TypeError):
        raise SourceSetupError("SOURCE_POLICY_INVALID") from None


def _loopback_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise SourceSetupError("SOURCE_SETUP_LOOPBACK_ORIGIN_REQUIRED")
    return f"http://{parsed.netloc}"


def _source_rows(request):
    response = request("GET", "/sources")
    rows = response.get("sources") if isinstance(response, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise SourceSetupError("SOURCE_LIST_INVALID")
    return rows


def compose_owned_public_containment(owned_containment, *, base_url, baseline,
                                     acceptance_run_id, artifact_path,
                                     runtime_identity=None, request=None, page=None,
                                     execution_scope="LIVE_PUBLIC_READ_ONLY",
                                     utc_now=lambda: datetime.now(timezone.utc)):
    """Wrap an OwnedPublicContainment callable with the step 9 UI lifecycle.

    ``request`` is an optional loopback-only read/write transport for deterministic
    tests. Production callers omit it, so the wrapper uses an isolated HTTPX
    client. The UI route guard admits only same-origin GETs and PUT /sources.
    """
    origin = _loopback_origin(base_url)
    if (execution_scope not in {"LIVE_PUBLIC_READ_ONLY", "CONTROLLED_TRANSPORT"}
            or (request is not None or page is not None)
            and execution_scope != "CONTROLLED_TRANSPORT"
            or execution_scope == "LIVE_PUBLIC_READ_ONLY" and request is not None):
        raise SourceSetupError("SOURCE_SETUP_TRANSPORT_SCOPE_INVALID")
    if not callable(owned_containment) or not isinstance(baseline, dict):
        raise SourceSetupError("SOURCE_SETUP_CONFIGURATION_INVALID")
    if (baseline.get("external_submission_enabled") is not False
            or not isinstance(baseline.get("source"), dict)
            or not re.fullmatch(r"[a-f0-9]{40}", str(baseline["source"].get("commit", "")))
            or not all(isinstance(baseline.get(key), str) and baseline[key]
                       for key in ("profile_id", "application_id", "selected_application_job_id"))
            or not isinstance(acceptance_run_id, str) or not acceptance_run_id):
        raise SourceSetupError("SOURCE_SETUP_BASELINE_INVALID")
    selected_runtime = runtime_identity if runtime_identity is not None else baseline.get("runtime_identity")
    if not isinstance(selected_runtime, dict):
        raise SourceSetupError("SOURCE_SETUP_RUNTIME_IDENTITY_REQUIRED")
    artifact_path = Path(artifact_path).absolute()
    if artifact_path.exists() or not artifact_path.parent.is_dir():
        raise SourceSetupError("SOURCE_SETUP_ARTIFACT_DESTINATION_INVALID")

    class ComposedContainment:
        def __init__(self):
            self.page = page
            self.playwright = None
            self.browser = None
            self.extra_id = None
            self.enabled_policy_sha = None
            self.disabled_policy_sha = None
            self.extra_operation_ids = None
            self.extra_operation_readback_sha = None
            self.primary = None
            self.observation = None
            self.extra_disabled = False
            self._write_count = 0

        def _call_owned(self, stage, context):
            proof = owned_containment(stage, context)
            if not isinstance(proof, dict):
                raise SourceSetupError("SOURCE_CONTAINMENT_PROOF_INVALID")
            if stage != "release" and not (
                    proof.get("scheduler_contained") is True
                    and proof.get("source_policy_exclusive") is True
                    and proof.get("source_collection_exclusive") is True
                    and isinstance(proof.get("owner"), str) and proof["owner"]
                    and isinstance(proof.get("token"), str) and proof["token"]):
                raise SourceSetupError("SOURCE_CONTAINMENT_REQUIRED")
            return proof

        def _page(self):
            if self.page is not None:
                return self.page
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(headless=True)
            self.page = self.browser.new_page()
            return self.page

        def _route_guard(self, route):
            request = route.request
            parsed = urlsplit(request.url)
            allowed = (parsed.scheme == "http" and parsed.netloc == urlsplit(origin).netloc
                and request.method == "GET")
            if (parsed.scheme == "http" and parsed.netloc == urlsplit(origin).netloc
                    and request.method == "PUT" and parsed.path == "/sources"):
                try:
                    body = request.post_data_json
                except Exception:
                    body = None
                expected = dict(POLICY)
                if self.extra_id is not None:
                    expected["enabled"] = False
                allowed = isinstance(body, dict) and body == expected
            if allowed:
                route.fallback()
            else:
                route.abort()

        def _with_lease(self, context, action):
            self._call_owned("read", context)
            result = action()
            self._call_owned("read", context)
            return result

        def _open_sources(self, context):
            page_obj = self._page()
            page_obj.route("**/*", self._route_guard)
            self._with_lease(context, lambda: page_obj.goto(origin + "/", wait_until="domcontentloaded",
                                                            timeout=15000))
            nav = page_obj.get_by_role("navigation", name="Main navigation")
            self._with_lease(context, lambda: nav.get_by_role("button", name="Sources", exact=True).click())
            return page_obj

        def _create_extra(self, context):
            before = self._with_lease(context, lambda: _source_rows(request))
            if any(row.get("provider") == EXTRA_PROVIDER and row.get("tenant") == "default"
                   for row in before):
                raise SourceSetupError("EXTRA_SOURCE_MUST_BE_ABSENT")
            page_obj = self._open_sources(context)
            self._with_lease(context, lambda: page_obj.get_by_label("Source provider", exact=True)
                             .select_option(EXTRA_PROVIDER))
            self._with_lease(context, lambda: page_obj.get_by_label("Interval (minutes)", exact=True)
                             .fill("10080"))
            self._with_lease(context, lambda: page_obj.get_by_label("Request timeout (seconds)", exact=True)
                             .fill("10"))
            self._with_lease(context, lambda: page_obj.get_by_label("Rate limit (requests/minute)", exact=True)
                             .fill("1"))
            self._with_lease(context, lambda: page_obj.get_by_label("Source concurrency", exact=True).fill("1"))
            self._with_lease(context, lambda: page_obj.get_by_label("Schedule jitter (seconds)", exact=True)
                             .fill("0"))
            still_absent = self._with_lease(context, lambda: _source_rows(request))
            if any(row.get("provider") == EXTRA_PROVIDER and row.get("tenant") == "default"
                   for row in still_absent):
                raise SourceSetupError("EXTRA_SOURCE_APPEARED_BEFORE_CREATE")
            self._with_lease(context, lambda: page_obj.get_by_role("button", name="Add source", exact=True).click())
            rows = self._with_lease(context, lambda: _source_rows(request))
            matches = [row for row in rows if row.get("provider") == EXTRA_PROVIDER
                       and row.get("tenant") == "default"]
            if len(matches) != 1 or any(matches[0].get(key) != value for key, value in POLICY.items()):
                raise SourceSetupError("EXTRA_SOURCE_CONFIGURATION_UNCONFIRMED")
            source = matches[0]
            if source.get("last_attempt") is not None or source.get("last_success") is not None \
                    or source.get("jobs_seen") != 0:
                raise SourceSetupError("EXTRA_SOURCE_ALREADY_COLLECTED")
            self.extra_id = source.get("id")
            if not isinstance(self.extra_id, str) or not self.extra_id:
                raise SourceSetupError("EXTRA_SOURCE_ID_INVALID")
            self.enabled_policy_sha = _policy_sha(source)
            self._verify_extra_operations(context)

        def _verify_extra_operations(self, context):
            inventory = self._with_lease(context, lambda: request("GET", "/operations"))
            operations = inventory.get("operations") if isinstance(inventory, dict) else None
            if (not isinstance(operations, list) or len(operations) >= 100
                    or any(not isinstance(row, dict) for row in operations)):
                raise SourceSetupError("EXTRA_OPERATION_INVENTORY_INCOMPLETE")
            matches = []
            for operation in operations:
                params = operation.get("params") if isinstance(operation.get("params"), dict) else {}
                if operation.get("kind") == "DISCOVER" and params.get("source_id") == self.extra_id:
                    operation_id = operation.get("id")
                    if not isinstance(operation_id, str) or not operation_id:
                        raise SourceSetupError("EXTRA_OPERATION_ID_INVALID")
                    matches.append(operation_id)
            self.extra_operation_ids = sorted(set(matches))
            self.extra_operation_readback_sha = _sha(matches)
            if self.extra_operation_ids:
                raise SourceSetupError("EXTRA_SOURCE_COLLECTION_ALREADY_OBSERVED")

        def _disable_extra(self, context, rows):
            if not self.extra_id:
                return
            candidates = [row for row in rows if row.get("id") == self.extra_id]
            if len(candidates) != 1:
                raise SourceSetupError("EXTRA_SOURCE_OWNERSHIP_UNCONFIRMED")
            current = candidates[0]
            if (_policy_sha(current) != self.enabled_policy_sha or current.get("enabled") is not True
                    or current.get("last_attempt") is not None or current.get("last_success") is not None
                    or current.get("jobs_seen") != 0):
                raise SourceSetupError("EXTRA_SOURCE_POLICY_CHANGED")
            page_obj = self._page()
            section = page_obj.get_by_role("heading", name=current.get("name"), exact=True).locator(
                "xpath=ancestor::section[1]")
            if section.count() != 1:
                raise SourceSetupError("EXTRA_SOURCE_UI_CARD_AMBIGUOUS")
            def disable_from_ui():
                section.get_by_role("button", name="Disable", exact=True).click()
                section.get_by_role("button", name="Enable", exact=True).wait_for(state="visible", timeout=5000)
            self._with_lease(context, disable_from_ui)
            after = self._with_lease(context, lambda: _source_rows(request))
            matches = [row for row in after if row.get("id") == self.extra_id]
            if len(matches) != 1:
                raise SourceSetupError("EXTRA_SOURCE_READBACK_INVALID")
            disabled = matches[0]
            expected = dict(current)
            expected["enabled"] = False
            if (_policy_sha(disabled) != _policy_sha(expected) or disabled.get("enabled") is not False
                    or disabled.get("last_attempt") is not None or disabled.get("last_success") is not None
                    or disabled.get("jobs_seen") != 0):
                raise SourceSetupError("EXTRA_SOURCE_DISABLE_UNCONFIRMED")
            self.disabled_policy_sha = _policy_sha(disabled)
            if self.disabled_policy_sha == self.enabled_policy_sha:
                raise SourceSetupError("EXTRA_SOURCE_POLICY_TRANSITION_INVALID")
            self.extra_disabled = True

        def _observe_and_disable(self, context):
            if self.observation is not None:
                return
            if context.get("provider") != PRIMARY_PROVIDER:
                raise SourceSetupError("PRIMARY_PROVIDER_MISMATCH")
            primary_id = context.get("source_id")
            primary_hash = context.get("source_policy_sha256")
            if not isinstance(primary_id, str) or not isinstance(primary_hash, str) \
                    or not re.fullmatch(r"[a-f0-9]{64}", primary_hash):
                raise SourceSetupError("PRIMARY_SOURCE_LINEAGE_INVALID")
            page_obj = self._page()
            self._with_lease(context, lambda: page_obj.reload(wait_until="domcontentloaded", timeout=15000))
            nav = page_obj.get_by_role("navigation", name="Main navigation")
            self._with_lease(context, lambda: nav.get_by_role("button", name="Sources", exact=True).click())
            rows = self._with_lease(context, lambda: _source_rows(request))
            primary_rows = [row for row in rows if row.get("provider") == PRIMARY_PROVIDER
                            and row.get("tenant") == "default"]
            extra_rows = [row for row in rows if row.get("id") == self.extra_id]
            if (len(primary_rows) != 1 or len(extra_rows) != 1
                    or primary_rows[0].get("id") != primary_id
                    or primary_rows[0].get("enabled") is not True
                    or _policy_sha(primary_rows[0]) != primary_hash
                    or self.extra_id == primary_id or extra_rows[0].get("enabled") is not True):
                raise SourceSetupError("DISTINCT_ENABLED_SOURCES_UNCONFIRMED")
            names = [row.get("name") for row in rows]
            visible = bool(rows)
            for name in names:
                if not isinstance(name, str) or not name:
                    visible = False
                    break
                locator = page_obj.get_by_role("heading", name=name, exact=True)
                self._with_lease(context, lambda locator=locator: locator.wait_for(state="visible", timeout=5000))
                if locator.count() != 1:
                    visible = False
                    break
            enabled_providers = {row.get("provider") for row in rows if row.get("enabled") is True}
            multiple = (len(rows) >= 2 and sum(row.get("enabled") is True for row in rows) >= 2
                        and len(enabled_providers) >= 2 and PRIMARY_PROVIDER in enabled_providers
                        and EXTRA_PROVIDER in enabled_providers)
            self.primary = {"provider": PRIMARY_PROVIDER, "source_id": primary_id,
                            "policy_sha256": primary_hash}
            self.observation = {
                "step": 9,
                "status": "PASS" if visible and multiple else "PARTIAL",
                "evidence_kind": "PUBLIC_READ_ONLY",
                "run_id": acceptance_run_id,
                "source_commit": baseline["source"]["commit"],
                "observable_result": ("Two distinct public provider sources were visible and enabled before the primary collection."
                    if visible and multiple else "The UI did not prove two distinct enabled public provider sources."),
                "continuity": {"profile_id": baseline["profile_id"]},
                "checks": [
                    {"name": "source_rows_visible", "passed": visible},
                    {"name": "multiple_sources_enabled", "passed": multiple},
                ],
            }
            self._disable_extra(context, rows)
            self._verify_extra_operations(context)
            self._close_browser()

        def _publish_after_release(self):
            if (self.observation is None or not self.extra_disabled or not self.primary
                    or self.disabled_policy_sha is None):
                raise SourceSetupError("SOURCE_SETUP_INCOMPLETE")
            now = utc_now()
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
                raise SourceSetupError("SOURCE_SETUP_CLOCK_INVALID")
            artifact = {
                "schema_version": 1,
                "result": (self.observation["status"] if execution_scope == "LIVE_PUBLIC_READ_ONLY"
                           else "NOT_RUN"),
                "acceptance_run_id": acceptance_run_id,
                "timestamp": now.astimezone(timezone.utc).isoformat(),
                "source": baseline["source"],
                "target_origin": origin,
                "runtime_identity": selected_runtime,
                "profile_id": baseline["profile_id"],
                "application_id": baseline["application_id"],
                "job_id": baseline["selected_application_job_id"],
                "primary": self.primary,
                "owned_extra": {
                    "provider": EXTRA_PROVIDER,
                    "source_id": self.extra_id,
                    "enabled_policy_sha256": self.enabled_policy_sha,
                    "disabled_policy_sha256": self.disabled_policy_sha,
                    "disabled": True,
                },
                "extra_collection_operation_ids": self.extra_operation_ids,
                "extra_operation_readback_sha256": self.extra_operation_readback_sha,
                "acceptance_observations": [dict(self.observation,
                    status=(self.observation["status"] if execution_scope == "LIVE_PUBLIC_READ_ONLY"
                            else "NOT_RUN"),
                    observable_result=(self.observation["observable_result"] if execution_scope == "LIVE_PUBLIC_READ_ONLY"
                                       else "Controlled browser/API fixture only; live source criterion not run."))],
            }
            payload = (json.dumps(artifact, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            fd = os.open(artifact_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())

        def _close_browser(self):
            try:
                if self.browser is not None:
                    self.browser.close()
            finally:
                if self.playwright is not None:
                    self.playwright.stop()
                self.browser = self.playwright = None

        def __call__(self, stage, context):
            if stage == "release":
                # Renew and independently validate containment while the extra
                # policy is still held disabled, then delegate the actual release.
                if self.observation is None or not self.extra_disabled or not self.primary:
                    raise SourceSetupError("SOURCE_SETUP_INCOMPLETE")
                self._call_owned("finalize", context)
                rows = self._with_lease(context, lambda: _source_rows(request))
                self._verify_extra_operations(context)
                matches = [row for row in rows if row.get("id") == self.extra_id]
                if (not self.extra_disabled or len(matches) != 1 or matches[0].get("enabled") is not False
                        or _policy_sha(matches[0]) != self.disabled_policy_sha
                        or matches[0].get("last_attempt") is not None
                        or matches[0].get("last_success") is not None or matches[0].get("jobs_seen") != 0):
                    raise SourceSetupError("EXTRA_SOURCE_RELEASE_GUARD_FAILED")
                try:
                    result = self._call_owned("release", context)
                    self._publish_after_release()
                    return result
                finally:
                    self._close_browser()
            proof = self._call_owned(stage, context)
            try:
                if stage == "preflight" and self.extra_id is None:
                    self._create_extra(context)
                elif stage == "dispatch" and self.observation is None:
                    self._observe_and_disable(context)
            except Exception:
                self._close_browser()
                raise
            return proof

    if request is None:
        import httpx
        client = httpx.Client(base_url=origin, timeout=15, trust_env=False, follow_redirects=False)

        def loopback_request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        class Adapter:
            def __init__(self, composed):
                self.composed = composed

            def __call__(self, stage, context):
                try:
                    return self.composed(stage, context)
                finally:
                    if stage == "release":
                        client.close()
        request = loopback_request
        return Adapter(ComposedContainment())
    return ComposedContainment()
