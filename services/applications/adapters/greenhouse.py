"""Review-first capability boundary for Greenhouse application routes.

This adapter identifies normalized public job routes and extracts only the
existing form analyzer's allowlisted DOM metadata. Document selection is limited
to verified synthetic files on loopback fixtures; public ATS forms are never
interacted with or submitted.
"""
from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from services.applications.form_analysis import analyze_form_page
from services.applications.upload_integrity import (
    UploadSnapshot, attach_verified_files, snapshot_bytes,
)


_GREENHOUSE_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io"}
_TENANT = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z", re.I)
_REQUISITION = re.compile(r"[0-9]{1,20}\Z")


class GreenhouseApplicationAdapter:
    """Recognize Greenhouse job routes and inspect forms without acting on them."""

    provider = "greenhouse"

    def capabilities(self) -> dict[str, bool]:
        """Return only behavior implemented and safe in this adapter slice."""
        return {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "mock_fixture_upload": True,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        }

    def normalize_route(self, route: Mapping) -> dict | None:
        """Normalize a public Greenhouse job URL; reject every other origin/shape."""
        if not isinstance(route, Mapping):
            return None
        provider = route.get("provider")
        url = route.get("url")
        if not isinstance(provider, str) or provider.casefold() != self.provider:
            return None
        if not isinstance(url, str) or not url or len(url) > 2048:
            return None
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return None
        if (parsed.scheme.casefold() != "https"
                or (parsed.hostname or "").casefold() not in _GREENHOUSE_HOSTS
                or parsed.username is not None or parsed.password is not None
                or port not in (None, 443)):
            return None
        parts = parsed.path.split("/")
        if len(parts) != 4 or parts[0] != "" or parts[2] != "jobs":
            return None
        tenant, requisition = parts[1], parts[3]
        if not _TENANT.fullmatch(tenant) or not _REQUISITION.fullmatch(requisition):
            return None
        tenant = tenant.casefold()
        supplied_tenant = route.get("tenant")
        supplied_requisition = route.get("requisition")
        if supplied_tenant is not None and (
                not isinstance(supplied_tenant, str)
                or supplied_tenant.casefold() != tenant):
            return None
        if supplied_requisition is not None and str(supplied_requisition) != requisition:
            return None
        return {
            "provider": self.provider,
            "tenant": tenant,
            "requisition": requisition,
            "url": f"https://boards.greenhouse.io/{tenant}/jobs/{requisition}",
        }

    def analyze_page(self, page) -> dict:
        """Read one loaded form using the shared value-free DOM analyzer."""
        return analyze_form_page(page)

    def validate_page(self, page, analysis=None) -> dict:
        """Check native form validity without reading or returning applicant values."""
        current = self.analyze_page(page)
        if analysis is not None and current != analysis:
            return {"status": "UNKNOWN", "reason": "FORM_CHANGED"}
        if self.requires_human_review(current):
            return {"status": "REVIEW_REQUIRED", "reason": "FORM_REQUIRES_HUMAN_REVIEW"}
        try:
            validity = page.locator(
                'form input:not([type="file"]), form select, form textarea'
            ).evaluate_all(
                "nodes => nodes.map(node => ({"
                "validates: Boolean(node.willValidate), "
                "valid: Boolean(node.validity && node.validity.valid)"
                "}))"
            )
        except Exception:
            # Browser failures can include page data; return only a fixed reason.
            return {"status": "UNKNOWN", "reason": "FORM_VALIDATION_UNAVAILABLE"}
        non_file_fields = [field for field in current["fields"]
                           if (field.get("input_type") or "").casefold() != "file"]
        if not isinstance(validity, list) or len(validity) != len(non_file_fields):
            return {"status": "UNKNOWN", "reason": "FORM_CONTROL_COUNT_CHANGED"}
        if any(not isinstance(item, Mapping)
               or (item.get("validates") is True and item.get("valid") is not True)
               for item in validity):
            return {"status": "REVIEW_REQUIRED", "reason": "FORM_FIELDS_INVALID"}
        return {"status": "PASS", "reason": None}

    def upload_mock_fixture_documents(self, page, analysis, snapshots):
        """Select verified resume and letter bytes only on a loopback fixture page.

        This helper never uploads to a public ATS. It rejects uncertain forms,
        unsupported file controls, changed DOMs, and documents outside the
        prepared resume/cover-letter contract. Selection is verified by reading
        the browser File objects back and hashing their exact bytes.
        """
        parsed = None
        try:
            parsed = urlsplit(page.url)
            host = parsed.hostname
            is_loopback = isinstance(host, str) and host.casefold() == "localhost"
            if host:
                try:
                    is_loopback = is_loopback or ipaddress.ip_address(host).is_loopback
                except ValueError:
                    pass
        except (AttributeError, ValueError):
            is_loopback = False
        if (parsed is None or parsed.scheme.casefold() != "http" or not is_loopback
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("GREENHOUSE_FIXTURE_ORIGIN_REQUIRED")

        current = self.analyze_page(page)
        if current != analysis or self.requires_human_review(current):
            raise ValueError("GREENHOUSE_FORM_REQUIRES_HUMAN_REVIEW")
        validation = self.validate_page(page, current)
        if validation["status"] != "PASS":
            raise ValueError("GREENHOUSE_REQUIRED_FIELDS_INVALID")
        if not isinstance(snapshots, (list, tuple)) or len(snapshots) != 2:
            raise ValueError("GREENHOUSE_FIXTURE_DOCUMENT_SET_INVALID")

        by_kind = {}
        for item in snapshots:
            if not isinstance(item, UploadSnapshot) or item.kind not in {"resume", "cover-letter"}:
                raise ValueError("GREENHOUSE_FIXTURE_DOCUMENT_SET_INVALID")
            if item.kind in by_kind:
                raise ValueError("GREENHOUSE_FIXTURE_DOCUMENT_SET_INVALID")
            try:
                verified = snapshot_bytes(item.kind, item.document_id, item.filename,
                                          item.media_type, item.content, item.sha256)
            except ValueError:
                raise ValueError("GREENHOUSE_FIXTURE_DOCUMENT_INVALID") from None
            by_kind[item.kind] = verified
        if set(by_kind) != {"resume", "cover-letter"}:
            raise ValueError("GREENHOUSE_FIXTURE_DOCUMENT_SET_INVALID")

        fields = current["fields"]
        file_fields = [field for field in fields
                       if (field.get("input_type") or "").casefold() == "file"]
        locator = page.locator('form input[type="file"]')
        if len(file_fields) != 2 or locator.count() != len(file_fields):
            raise ValueError("GREENHOUSE_FILE_FIELDS_UNSUPPORTED")

        targets = {}
        for file_index, field in enumerate(file_fields):
            identity = " ".join(str(field.get(key) or "") for key in
                                 ("name", "id", "aria_label", "placeholder")).casefold()
            compact = re.sub(r"[^a-z0-9]+", " ", identity).strip()
            if re.search(r"\b(resume|cv)\b", compact):
                kind = "resume"
            elif re.search(r"\bcover\s*letter\b", compact):
                kind = "cover-letter"
            else:
                raise ValueError("GREENHOUSE_FILE_FIELD_UNRECOGNIZED")
            if kind in targets:
                raise ValueError("GREENHOUSE_FILE_FIELD_AMBIGUOUS")
            targets[kind] = file_index
        if set(targets) != set(by_kind):
            raise ValueError("GREENHOUSE_FILE_FIELDS_UNSUPPORTED")

        manifest = []
        for kind in ("resume", "cover-letter"):
            manifest.extend(attach_verified_files(
                locator.nth(targets[kind]), [by_kind[kind]],
            ))
        return manifest

    @staticmethod
    def requires_human_review(analysis) -> bool:
        """Fail closed for unknown, malformed, ambiguous, or sensitive fields."""
        if not isinstance(analysis, Mapping) or analysis.get("status") != "PASS":
            return True
        fields = analysis.get("fields")
        if not isinstance(fields, list) or not fields:
            return True
        for field in fields:
            if not isinstance(field, Mapping):
                return True
            if field.get("ambiguous") is not False or field.get("sensitivity") != "STANDARD":
                return True
        return False
