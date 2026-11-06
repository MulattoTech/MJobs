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
            self.policy_checks = []
            self.operation_checks = []
            self.additional_owner_checks = []
            self.extra_enabled = False
            self.extra_bound = False
            self.additional_declared = False
            self.primary = None
            self.observation = None
            self.extra_disabled = False
            self._write_count = 0

        def _call_owned(self, stage, context):
            if (not isinstance(context, dict) or context.get("provider") != PRIMARY_PROVIDER
                    or not isinstance(context.get("base_url"), str)
                    or context["base_url"].rstrip("/") != origin
                    or context.get("acceptance_run_id") not in {None, acceptance_run_id}):
                raise SourceSetupError("PRIMARY_CONTAINMENT_CONTEXT_MISMATCH")
            owner_context = dict(context)
            if self.additional_declared:
                owner_context["additional_source"] = ({
                    "provider": EXTRA_PROVIDER,
                    "tenant": "default",
                    "source_id": self.extra_id,
                    "enabled_policy_sha256": self.enabled_policy_sha,
                    "disabled_policy_sha256": self.disabled_policy_sha,
                    "enabled": self.extra_enabled,
                } if self.extra_id is not None else {
                    "provider": EXTRA_PROVIDER,
                    "tenant": "default",
                    "source_id": None,
                    "enabled_policy_sha256": None,
                    "disabled_policy_sha256": None,
                    "enabled": False,
                })
            proof = owned_containment(stage, owner_context)
            if not isinstance(proof, dict):
                raise SourceSetupError("SOURCE_CONTAINMENT_PROOF_INVALID")
            if stage == "release" and proof.get("released") is not True:
                raise SourceSetupError("SOURCE_CONTAINMENT_RELEASE_UNCONFIRMED")
            if stage != "release" and not (
                    proof.get("scheduler_contained") is True
                    and proof.get("source_policy_exclusive") is True
                    and proof.get("source_collection_exclusive") is True
                    and isinstance(proof.get("owner"), str) and proof["owner"]
                    and isinstance(proof.get("token"), str) and proof["token"]):
                raise SourceSetupError("SOURCE_CONTAINMENT_REQUIRED")
            return proof

        def _additional_event(self, event, context, *, enabled):
            callback = getattr(owned_containment, "additional_source", None)
            if not callable(callback):
                if execution_scope == "LIVE_PUBLIC_READ_ONLY":
                    raise SourceSetupError("ADDITIONAL_SOURCE_CONTAINMENT_UNAVAILABLE")
                return None
            event_context = dict(context,
                additional_source_id=self.extra_id,
                additional_enabled_policy_sha256=self.enabled_policy_sha,
                additional_disabled_policy_sha256=self.disabled_policy_sha,
                additional_enabled=enabled)
            proof = callback(event, event_context)
            if (execution_scope == "LIVE_PUBLIC_READ_ONLY"
                    and (not isinstance(proof, dict)
                         or proof.get("additional_source_policy_verified") is not True
                         or proof.get("additional_source_id") != self.extra_id
                         or proof.get("additional_source_enabled") is not enabled)):
                raise SourceSetupError("ADDITIONAL_SOURCE_OWNERSHIP_NOT_CONFIRMED")
            if execution_scope == "LIVE_PUBLIC_READ_ONLY":
                self.additional_owner_checks.append({"stage": event, "verified": True,
                    "source_id": self.extra_id, "enabled": enabled,
                    "lease_expires_at": proof.get("lease_expires_at")})
            return proof

        def _source_policy_sha(self, source):
            calculate = getattr(owned_containment, "policy_sha256", None)
            value = calculate(source) if callable(calculate) else _policy_sha(source)
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                raise SourceSetupError("SOURCE_POLICY_FINGERPRINT_INVALID")
            return value

        def _bind_additional_policy(self, context):
            binder = getattr(owned_containment, "bind_additional_source", None)
            if not callable(binder):
                if execution_scope == "LIVE_PUBLIC_READ_ONLY":
                    raise SourceSetupError("ADDITIONAL_SOURCE_CONTAINMENT_UNAVAILABLE")
                return
            proof = binder(policy=dict(POLICY), primary_context=dict(context))
            if (execution_scope == "LIVE_PUBLIC_READ_ONLY"
                    and (not isinstance(proof, dict)
                         or proof.get("additional_source_absent") is not True
                         or proof.get("provider") != EXTRA_PROVIDER
                         or proof.get("tenant") != "default")):
                raise SourceSetupError("ADDITIONAL_SOURCE_ABSENCE_NOT_CONFIRMED")
            if execution_scope == "LIVE_PUBLIC_READ_ONLY":
                self.additional_owner_checks.append({"stage": "bind_absent", "verified": True,
                    "provider": EXTRA_PROVIDER, "tenant": "default",
                    "lease_expires_at": proof.get("lease_expires_at")})
            self.additional_declared = True

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
            if self.extra_bound:
                self._additional_event("check", context, enabled=self.extra_enabled)
            result = action()
            self._call_owned("read", context)
            if self.extra_bound:
                self._additional_event("check", context, enabled=self.extra_enabled)
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
            settings = self._with_lease(context, lambda: request("GET", "/settings"))
            health = self._with_lease(context, lambda: request("GET", "/health"))
            if (settings.get("operation_mode") != "FIXTURE"
                    or settings.get("external_submission_enabled") is not False
                    or health.get("external_submission_enabled") is not False):
                raise SourceSetupError("SOURCE_SETUP_FIXTURE_MODE_REQUIRED")
            before = self._with_lease(context, lambda: _source_rows(request))
            if any(row.get("provider") == EXTRA_PROVIDER and row.get("tenant") == "default"
                   for row in before):
                raise SourceSetupError("EXTRA_SOURCE_MUST_BE_ABSENT")
            self.policy_checks.append({"stage": "preflight_absent", "provider": EXTRA_PROVIDER,
                "tenant": "default", "present": False})
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
            self.policy_checks.append({"stage": "immediately_before_create", "provider": EXTRA_PROVIDER,
                "tenant": "default", "present": False})
            self._call_owned("read", context)
            page_obj.get_by_role("button", name="Add source", exact=True).click()
            page_obj.get_by_text("Source saved. Create a search profile below to enable scheduled discovery.",
                                 exact=True).wait_for(state="visible", timeout=5000)
            rows = _source_rows(request)
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
            self.enabled_policy_sha = self._source_policy_sha(source)
            self.policy_checks.append({"stage": "created_enabled", "source_id": self.extra_id,
                "policy_sha256": self.enabled_policy_sha, "enabled": True})
            self.extra_enabled = True
            if callable(getattr(owned_containment, "additional_source", None)):
                self._additional_event("created", context, enabled=True)
                self.extra_bound = True
            elif execution_scope == "LIVE_PUBLIC_READ_ONLY":
                raise SourceSetupError("ADDITIONAL_SOURCE_CONTAINMENT_UNAVAILABLE")
            self._call_owned("read", context)
            self._verify_extra_operations(context, "created_before_primary_collection")

        def _verify_extra_operations(self, context, stage):
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
            self.operation_checks.append({"stage": stage, "inventory_count": len(operations),
                "matching_operation_ids": list(self.extra_operation_ids),
                "matching_ids_sha256": self.extra_operation_readback_sha})
            if self.extra_operation_ids:
                raise SourceSetupError("EXTRA_SOURCE_COLLECTION_ALREADY_OBSERVED")

        def _disable_extra(self, context, rows):
            if not self.extra_id:
                return
            candidates = [row for row in rows if row.get("id") == self.extra_id]
            if len(candidates) != 1:
                raise SourceSetupError("EXTRA_SOURCE_OWNERSHIP_UNCONFIRMED")
            current = candidates[0]
            if (self._source_policy_sha(current) != self.enabled_policy_sha or current.get("enabled") is not True
                    or current.get("last_attempt") is not None or current.get("last_success") is not None
                    or current.get("jobs_seen") != 0):
                raise SourceSetupError("EXTRA_SOURCE_POLICY_CHANGED")
            self.policy_checks.append({"stage": "before_disable", "source_id": self.extra_id,
                "policy_sha256": self.enabled_policy_sha, "enabled": True})
            page_obj = self._page()
            card_text = f"{current.get('provider')} / {current.get('tenant')}"
            section = page_obj.locator("section.panel").filter(has_text=card_text)
            if section.count() != 1:
                raise SourceSetupError("EXTRA_SOURCE_UI_CARD_AMBIGUOUS")
            def disable_from_ui():
                self._call_owned("read", context)
                if self.extra_bound:
                    self._additional_event("before_disable", context, enabled=True)
                section.get_by_role("button", name="Disable", exact=True).click()
                section.get_by_role("button", name="Enable", exact=True).wait_for(state="visible", timeout=5000)
            disable_from_ui()
            after = _source_rows(request)
            matches = [row for row in after if row.get("id") == self.extra_id]
            if len(matches) != 1:
                raise SourceSetupError("EXTRA_SOURCE_READBACK_INVALID")
            disabled = matches[0]
            expected = dict(current)
            expected["enabled"] = False
            if (self._source_policy_sha(disabled) != self._source_policy_sha(expected) or disabled.get("enabled") is not False
                    or disabled.get("last_attempt") is not None or disabled.get("last_success") is not None
                    or disabled.get("jobs_seen") != 0):
                raise SourceSetupError("EXTRA_SOURCE_DISABLE_UNCONFIRMED")
            self.disabled_policy_sha = self._source_policy_sha(disabled)
            self.policy_checks.append({"stage": "disabled_readback", "source_id": self.extra_id,
                "policy_sha256": self.disabled_policy_sha, "enabled": False})
            if self.disabled_policy_sha == self.enabled_policy_sha:
                raise SourceSetupError("EXTRA_SOURCE_POLICY_TRANSITION_INVALID")
            self.extra_disabled = True
            self.extra_enabled = False
            if self.extra_bound:
                self._additional_event("disabled", context, enabled=False)
            self._call_owned("read", context)

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
                    or self._source_policy_sha(primary_rows[0]) != primary_hash
                    or self.extra_id == primary_id or extra_rows[0].get("enabled") is not True):
                raise SourceSetupError("DISTINCT_ENABLED_SOURCES_UNCONFIRMED")
            visible = bool(rows)
            for row in rows:
                provider, tenant = row.get("provider"), row.get("tenant")
                if not isinstance(provider, str) or not isinstance(tenant, str):
                    visible = False
                    break
                card = page_obj.locator("section.panel").filter(has_text=f"{provider} / {tenant}")
                self._with_lease(context, lambda card=card: card.first.wait_for(state="visible", timeout=5000))
                if card.count() != 1:
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
            self._verify_extra_operations(context, "disabled_before_primary_collection")
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
                "trust_boundary": {
                    "target": "ISOLATED_LOCAL_FIXTURE",
                    "source_policy_ownership": "OPERATOR_COOPERATIVE_EXCLUSIVE",
                    "source_policy_atomic_cas": False,
                    "description": "An operator-owned lease and designated callback bind this exact new source; API source-policy writes do not provide atomic compare-and-swap.",
                },
                "extra_policy_readbacks": self.policy_checks,
                "extra_operation_readbacks": self.operation_checks,
                "additional_owner_checks": self.additional_owner_checks,
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
                self._verify_extra_operations(context, "release")
                matches = [row for row in rows if row.get("id") == self.extra_id]
                if (not self.extra_disabled or len(matches) != 1 or matches[0].get("enabled") is not False
                        or self._source_policy_sha(matches[0]) != self.disabled_policy_sha
                        or matches[0].get("last_attempt") is not None
                        or matches[0].get("last_success") is not None or matches[0].get("jobs_seen") != 0):
                    raise SourceSetupError("EXTRA_SOURCE_RELEASE_GUARD_FAILED")
                self.policy_checks.append({"stage": "release", "source_id": self.extra_id,
                    "policy_sha256": self.disabled_policy_sha, "enabled": False})
                try:
                    if self.extra_bound:
                        self._additional_event("release", context, enabled=False)
                    result = self._call_owned("release", context)
                    self._publish_after_release()
                    return result
                finally:
                    self._close_browser()
            proof = self._call_owned(stage, context)
            try:
                if stage == "preflight" and self.extra_id is None:
                    self._bind_additional_policy(context)
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
