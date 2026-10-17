"""Review-first boundary for public Workday application routes.

Only Workday's public career-host route shape is recognized. Form inspection
reuses the existing value-free Lever analyzer; uncertain prompts stay gated for
human review. Live public form interaction and submission remain unsupported.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from services.applications.adapters.lever import LeverApplicationAdapter
from services.applications.adapters.workday_forms import (
    WORKDAY_DOM_METADATA_SCRIPT,
    analyze_workday_dom_metadata,
    requires_workday_human_review,
)


_HOST = re.compile(
    r"(?P<tenant>[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\."
    r"(?P<cluster>wd[1-9][0-9]*)\.myworkdayjobs\.com\Z",
    re.I,
)
_LOCALE = re.compile(r"[a-z]{2,3}-[a-z]{2}\Z", re.I)
_BOARD = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z", re.I)
_PATH_PART = re.compile(r"[a-z0-9][a-z0-9._~-]{0,159}\Z", re.I)
_REQUISITION = re.compile(
    r"(?P<title>[a-z0-9][a-z0-9._~-]{0,139})_"
    r"(?P<id>(?:JR[0-9]{1,20}|R-?[0-9]{1,20}(?:-[0-9]{1,10})?"
    r"|[0-9]{1,20}(?:-[0-9]{1,10})?))\Z",
    re.I,
)


class WorkdayApplicationAdapter(LeverApplicationAdapter):
    """Normalize Workday job routes and reuse safe local form-review behavior."""

    provider = "workday"

    def capabilities(self) -> dict[str, bool]:
        """Declare supported review behavior and disabled public actions."""
        return {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "mock_fixture_upload": True,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        }

    def analyze_page(self, page, *, check=None) -> dict:
        """Analyze current rendered markup without projecting answer values."""
        if check is not None:
            check()
        try:
            timeout = 10000
            remaining = getattr(check, "remaining_seconds", None)
            if callable(remaining):
                timeout = max(1, min(10000, int(remaining() * 1000)))
            projection = (
                "root => (" + WORKDAY_DOM_METADATA_SCRIPT + ")"
                "(Array.from(root.ownerDocument.querySelectorAll('form')))"
            )
            metadata = page.locator("html").evaluate(
                projection,
                timeout=timeout,
            )
        except Exception:
            if check is not None:
                check()
            return {"provider": self.provider, "status": "UNKNOWN",
                    "reason": "PAGE_CONTENT_UNAVAILABLE", "fields": []}
        if check is not None:
            check()
        return analyze_workday_dom_metadata(metadata)

    def validate_page(self, page, analysis=None, *, check=None) -> dict:
        """Invalidate a reviewed question snapshot when rendered markup changes."""
        if check is not None:
            check()
        current = self.analyze_page(page, check=check)
        if current.get("status") == "UNKNOWN":
            return {"status": "UNKNOWN", "reason": current.get("reason", "FORM_UNKNOWN")}
        if analysis is not None and current != analysis:
            return {"status": "UNKNOWN", "reason": "FORM_CHANGED"}
        if requires_workday_human_review(current):
            return {"status": "REVIEW_REQUIRED", "reason": "FORM_REQUIRES_HUMAN_REVIEW"}
        return current

    def normalize_route(self, route: Mapping) -> dict | None:
        """Accept only strict tenant.wdN.myworkdayjobs.com job routes."""
        if not isinstance(route, Mapping):
            return None
        provider = route.get("provider")
        url = route.get("url")
        if not isinstance(provider, str) or provider.casefold() != self.provider:
            return None
        if not isinstance(url, str) or not url or len(url) > 2048 or "\\" in url:
            return None
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return None
        if (parsed.scheme.casefold() != "https"
                or parsed.username is not None or parsed.password is not None
                or port is not None or parsed.fragment):
            return None
        host_match = _HOST.fullmatch(parsed.hostname or "")
        if host_match is None:
            return None

        # Workday career links use /<locale>/<board>/job/<slug>_R<id>,
        # sometimes with one location segment between "job" and the slug.
        parts = parsed.path.split("/")
        if len(parts) not in (5, 6) or parts[0] != "":
            return None
        locale, board, route_kind, *job_parts = parts[1:]
        if (not _LOCALE.fullmatch(locale) or not _BOARD.fullmatch(board)
                or route_kind.casefold() != "job"
                or any(not _PATH_PART.fullmatch(part) for part in job_parts)):
            return None
        requisition_match = _REQUISITION.fullmatch(job_parts[-1])
        if requisition_match is None:
            return None
        requisition = requisition_match.group("id").upper()
        job_parts[-1] = f"{requisition_match.group('title')}_{requisition}"

        tenant = host_match.group("tenant").casefold()
        cluster = host_match.group("cluster").casefold()
        supplied_tenant = route.get("tenant")
        supplied_requisition = route.get("requisition")
        if supplied_tenant is not None and (
                not isinstance(supplied_tenant, str)
                or supplied_tenant.casefold() != tenant):
            return None
        if supplied_requisition is not None and (
                not isinstance(supplied_requisition, str)
                or supplied_requisition.upper() != requisition):
            return None

        canonical_parts = [
            locale[:locale.index("-")].lower(),
            locale[locale.index("-") + 1:].upper(),
        ]
        canonical_locale = "-".join(canonical_parts)
        canonical_path = "/".join([canonical_locale, board, "job", *job_parts])
        return {
            "provider": self.provider,
            "tenant": tenant,
            "requisition": requisition,
            "url": f"https://{tenant}.{cluster}.myworkdayjobs.com/{canonical_path}",
        }
