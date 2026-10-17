"""Review-first route and form metadata boundary for Workable applications.

This adapter recognizes public Workable job routes and reuses the shared
value-free form analyzer. Unknown prompts and challenge-protected controls
require manual review. It has no live form action or submission path.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from services.applications.adapters.lever import _known_contact_field
from services.applications.form_analysis import analyze_form_page


_WORKABLE_HOST = "apply.workable.com"
_TENANT = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z", re.I | re.ASCII)
_REQUISITION = re.compile(r"[a-z0-9][a-z0-9_-]{4,39}\Z", re.I | re.ASCII)
_CHALLENGE_MARKERS = re.compile(
    r"\b(captcha|recaptcha|hcaptcha|turnstile|challenge|cloudflare|bot verification|human verification)\b",
    re.I,
)
_CHALLENGE_SELECTOR = (
    'iframe[src*="captcha" i], iframe[title*="captcha" i], '
    '[class*="captcha" i], [id*="captcha" i], '
    'iframe[src*="turnstile" i], iframe[src*="challenges.cloudflare.com" i], '
    '[class*="turnstile" i], [id*="turnstile" i], '
    '[class*="cf-chl-" i], [id*="cf-chl-" i], [data-sitekey]'
)
_INTERACTIVE_INPUT_TYPES = {
    "text", "email", "tel", "url", "search", "number", "date",
    "datetime-local", "time", "month", "week", "checkbox", "radio", "password",
}
_CONTROL_INTERACTIVITY_SCRIPT = r"""nodes => nodes.map(node => {
  const tag = node.tagName.toLowerCase();
  const inputType = tag === 'input'
    ? (node.getAttribute('type') || 'text').toLowerCase()
    : null;
  const normalized = value => {
    if (value === null) return null;
    const result = String(value).replace(/\s+/g, ' ').trim();
    return result || null;
  };
  const rect = node.getBoundingClientRect();
  const viewport = window.visualViewport;
  const viewportLeft = viewport ? viewport.offsetLeft : 0;
  const viewportTop = viewport ? viewport.offsetTop : 0;
  const viewportRight = viewportLeft + (viewport ? viewport.width : document.documentElement.clientWidth);
  const viewportBottom = viewportTop + (viewport ? viewport.height : document.documentElement.clientHeight);
  let visible = node.getClientRects().length > 0
    && rect.width > 0 && rect.height > 0
    && rect.left >= viewportLeft && rect.top >= viewportTop
    && rect.right <= viewportRight && rect.bottom <= viewportBottom;
  for (let ancestor = node; visible && ancestor; ancestor = ancestor.parentElement) {
    const style = getComputedStyle(ancestor);
    if (ancestor.hidden || ancestor.getAttribute('aria-hidden') === 'true'
        || ancestor.getAttribute('aria-disabled') === 'true'
        || style.display === 'none' || style.visibility === 'hidden'
        || style.visibility === 'collapse' || style.contentVisibility === 'hidden'
        || Number(style.opacity) === 0 || style.pointerEvents === 'none'
        || style.clipPath !== 'none' || style.clip !== 'auto'
        || style.maskImage !== 'none' || style.filter !== 'none') {
      visible = false;
    }
    if (ancestor !== node) {
      const ancestorRect = ancestor.getBoundingClientRect();
      const clipLeft = ancestorRect.left + ancestor.clientLeft;
      const clipTop = ancestorRect.top + ancestor.clientTop;
      const clipRight = clipLeft + ancestor.clientWidth;
      const clipBottom = clipTop + ancestor.clientHeight;
      if ((['hidden', 'clip', 'scroll', 'auto'].includes(style.overflowX)
           && (rect.left < clipLeft || rect.right > clipRight))
          || (['hidden', 'clip', 'scroll', 'auto'].includes(style.overflowY)
              && (rect.top < clipTop || rect.bottom > clipBottom))) {
        visible = false;
      }
    }
  }
  if (visible) {
    const samplePoints = [0.25, 0.5, 0.75];
    for (const xFraction of samplePoints) {
      for (const yFraction of samplePoints) {
        const hit = document.elementFromPoint(
          rect.left + rect.width * xFraction,
          rect.top + rect.height * yFraction
        );
        if (!hit || (hit !== node && !node.contains(hit))) {
          visible = false;
          break;
        }
      }
      if (!visible) break;
    }
  }
  const interactiveTypes = new Set([
    'text', 'email', 'tel', 'url', 'search', 'number', 'date',
    'datetime-local', 'time', 'month', 'week', 'checkbox', 'radio', 'password'
  ]);
  const inert = node.closest('[inert]') !== null;
  const interactive = !inert && (tag === 'select' || tag === 'textarea'
    || (tag === 'input' && interactiveTypes.has(inputType)));
  const editable = !inert && !node.matches(':disabled')
    && !((tag === 'input' || tag === 'textarea') && node.readOnly);
  return {
    tag,
    input_type: inputType,
    name: normalized(node.getAttribute('name')),
    id: normalized(node.getAttribute('id')),
    visible,
    editable,
    interactive
  };
})"""


class WorkableApplicationAdapter:
    """Recognize Workable job routes and inspect forms without acting on them."""

    provider = "workable"

    def capabilities(self) -> dict[str, bool]:
        """Declare only route and review behavior implemented in this slice."""
        return {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "mock_fixture_upload": False,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        }

    def analyze_page(self, page, *, check=None) -> dict:
        """Analyze value-free controls and detect common embedded challenge widgets."""
        if check is not None:
            analysis = analyze_form_page(page, check=check)
        else:
            analysis = analyze_form_page(page)
        if analysis.get("status") != "PASS":
            return analysis
        if check is not None:
            check()
        try:
            challenge_count = page.locator(_CHALLENGE_SELECTOR).count()
        except Exception:
            if check is not None:
                check()
            return {"status": "UNKNOWN", "reason": "CHALLENGE_CHECK_UNAVAILABLE", "fields": []}
        if check is not None:
            check()
        if challenge_count:
            return {"status": "UNKNOWN", "reason": "CHALLENGE_PROTECTED_FORM", "fields": []}
        if check is not None:
            check()
        try:
            control_states = page.locator(
                "form input, form select, form textarea"
            ).evaluate_all(_CONTROL_INTERACTIVITY_SCRIPT)
        except Exception:
            if check is not None:
                check()
            return {"status": "UNKNOWN", "reason": "CONTROL_INTERACTIVITY_UNAVAILABLE", "fields": []}
        if check is not None:
            check()
        fields = analysis.get("fields")
        if not isinstance(fields, list) or not isinstance(control_states, list) \
                or len(fields) != len(control_states):
            return {"status": "UNKNOWN", "reason": "CONTROL_INTERACTIVITY_COUNT_CHANGED", "fields": []}
        enriched_fields = []
        for field, state in zip(fields, control_states):
            if not isinstance(field, Mapping) or not isinstance(state, Mapping):
                return {"status": "UNKNOWN", "reason": "CONTROL_INTERACTIVITY_METADATA_INVALID", "fields": []}
            field_type = field.get("input_type")
            if isinstance(field_type, str):
                field_type = field_type.casefold()
            identity = (
                field.get("tag") == state.get("tag")
                and field_type == state.get("input_type")
                and field.get("name") == state.get("name")
                and field.get("id") == state.get("id")
            )
            if not identity:
                return {"status": "UNKNOWN", "reason": "FORM_CONTROL_METADATA_CHANGED", "fields": []}
            if any(type(state.get(key)) is not bool
                   for key in ("visible", "editable", "interactive")):
                return {"status": "UNKNOWN", "reason": "CONTROL_INTERACTIVITY_METADATA_INVALID", "fields": []}
            enriched = dict(field)
            enriched.update({key: state[key] for key in ("visible", "editable", "interactive")})
            enriched_fields.append(enriched)
        result = dict(analysis)
        result["fields"] = enriched_fields
        return result

    def normalize_route(self, route: Mapping) -> dict | None:
        """Normalize an allowlisted Workable posting URL or reject it."""
        if not isinstance(route, Mapping):
            return None
        provider = route.get("provider")
        url = route.get("url")
        if (not isinstance(provider, str) or not provider.isascii()
                or provider.casefold() != self.provider):
            return None
        if not isinstance(url, str) or not url or len(url) > 2048:
            return None
        if any(ord(character) <= 0x20 or ord(character) == 0x7f for character in url):
            return None
        try:
            parsed = urlsplit(url)
            if not parsed.netloc.isascii() or not parsed.path.isascii():
                return None
            port = parsed.port
        except ValueError:
            return None
        if (parsed.scheme.casefold() != "https"
                or (parsed.hostname or "").casefold() != _WORKABLE_HOST
                or parsed.username is not None or parsed.password is not None
                or port is not None or parsed.fragment):
            return None

        parts = parsed.path.split("/")
        if len(parts) == 5 and parts[-1] == "":
            parts = parts[:-1]
        if len(parts) != 4 or parts[0] != "" or parts[2] != "j":
            return None
        tenant, requisition = parts[1], parts[3]
        if not _TENANT.fullmatch(tenant) or not _REQUISITION.fullmatch(requisition):
            return None
        tenant, requisition = tenant.casefold(), requisition.casefold()
        supplied_tenant = route.get("tenant")
        supplied_requisition = route.get("requisition")
        if supplied_tenant is not None and (
                not isinstance(supplied_tenant, str)
                or not supplied_tenant.isascii()
                or supplied_tenant.casefold() != tenant):
            return None
        if supplied_requisition is not None and (
                not isinstance(supplied_requisition, str)
                or not supplied_requisition.isascii()
                or supplied_requisition.casefold() != requisition):
            return None
        return {
            "provider": self.provider,
            "tenant": tenant,
            "requisition": requisition,
            "url": f"https://{_WORKABLE_HOST}/{tenant}/j/{requisition}",
        }

    @staticmethod
    def requires_human_review(analysis) -> bool:
        """Require review for uncertainty, sensitive data, challenges, or custom prompts."""
        if (not isinstance(analysis, Mapping) or analysis.get("status") != "PASS"
                or not isinstance(analysis.get("fields"), list)
                or not analysis["fields"]):
            return True
        for field in analysis["fields"]:
            if (not isinstance(field, Mapping)
                    or field.get("ambiguous") is not False
                    or field.get("sensitivity") != "STANDARD"
                    or field.get("visible") is not True
                    or field.get("editable") is not True
                    or field.get("interactive") is not True
                    or not _known_contact_field(field)):
                return True
            tag = field.get("tag")
            input_type = field.get("input_type")
            if tag == "input":
                if not isinstance(input_type, str) or input_type.casefold() not in _INTERACTIVE_INPUT_TYPES:
                    return True
            elif tag not in {"select", "textarea"} or input_type is not None:
                return True
            metadata = " ".join(
                value for key, value in field.items()
                if key in {"name", "id", "placeholder", "aria_label", "label"}
                and isinstance(value, str)
            )
            labels = field.get("labels")
            if isinstance(labels, list):
                metadata += " " + " ".join(value for value in labels if isinstance(value, str))
            normalized_metadata = re.sub(r"[^a-z0-9]+", " ", metadata.casefold())
            if _CHALLENGE_MARKERS.search(normalized_metadata):
                return True
        return False
