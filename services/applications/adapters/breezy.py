"""Review-first route and form metadata support for Breezy career pages.

Only public tenant job routes are recognized. Form inspection uses the shared
value-free analyzer; uploads, live interaction, and submission are unsupported.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from services.applications.form_analysis import analyze_form_page


_TENANT = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.I)
_POSITION = re.compile(
    r"(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-"
    r"(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)\Z", re.I | re.ASCII,
)
_CONTACT_PROMPTS = {
    "address", "city", "country", "email", "email address", "first name",
    "full name", "last name", "location", "mobile", "name", "phone",
    "phone number", "postal code", "state", "website", "zip code",
}
_SUPPORTED_INPUT_TYPES = {"text", "email", "tel"}
_CONTROL_STATE_SCRIPT = r"""controls => controls.map(element => {
  const ancestors = [];
  for (let node = element; node && node.nodeType === 1; node = node.parentElement) {
    ancestors.push(node);
  }
  const styles = ancestors.map(node => window.getComputedStyle(node));
  const hidden = ancestors.some((node, index) => {
    const style = styles[index];
    return node.hidden || node.inert || node.getAttribute('aria-hidden') === 'true' ||
      style.display === 'none' || style.visibility === 'hidden' ||
      style.visibility === 'collapse' || Number(style.opacity) === 0 ||
      style.contentVisibility === 'hidden' || style.pointerEvents === 'none';
  });
  const rect = element.getBoundingClientRect();
  const visible = !hidden && element.getClientRects().length > 0 &&
    rect.width > 0 && rect.height > 0 && rect.right > 0 && rect.bottom > 0 &&
    rect.left < window.innerWidth && rect.top < window.innerHeight;
  const disabled = element.matches(':disabled') || element.readOnly === true ||
    Boolean(element.closest('[aria-disabled="true"], [aria-readonly="true"]'));
  const inputType = element.tagName.toLowerCase() === 'input'
    ? (element.getAttribute('type') || 'text').toLowerCase() : null;
  return {
    user_visible: visible,
    user_editable: visible && !disabled && inputType !== 'hidden'
  };
})"""


class BreezyApplicationAdapter:
    """Recognize strict Breezy job links and inspect forms without acting on them."""

    provider = "breezy"

    def capabilities(self) -> dict[str, bool]:
        """Declare the narrow inspection-only boundary implemented here."""
        return {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "mock_fixture_upload": False,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        }

    def normalize_route(self, route: Mapping) -> dict | None:
        """Normalize tenant.breezy.hr job routes, rejecting ambiguous shapes."""
        if not isinstance(route, Mapping):
            return None
        provider, url = route.get("provider"), route.get("url")
        if not isinstance(provider, str) or provider.casefold() != self.provider:
            return None
        if not isinstance(url, str) or not url or len(url) > 2048 or "\\" in url:
            return None
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return None
        if (parsed.scheme.casefold() != "https" or port is not None
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment):
            return None
        if not parsed.netloc.isascii():
            return None
        raw_host = parsed.hostname or ""
        if not raw_host.isascii():
            return None
        host = raw_host.lower()
        suffix = ".breezy.hr"
        if not host.endswith(suffix):
            return None
        tenant = host[:-len(suffix)]
        if not _TENANT.fullmatch(tenant):
            return None
        parts = parsed.path.split("/")
        if len(parts) not in (3, 4) or parts[0] != "" or parts[1] != "p":
            return None
        if len(parts) == 4 and parts[3] != "apply":
            return None
        position = _POSITION.fullmatch(parts[2])
        if position is None:
            return None
        requisition = position.group("id").casefold()
        supplied_tenant = route.get("tenant")
        supplied_requisition = route.get("requisition")
        if supplied_tenant is not None and (
                not isinstance(supplied_tenant, str)
                or not supplied_tenant.isascii()
                or not _TENANT.fullmatch(supplied_tenant)
                or supplied_tenant.lower() != tenant):
            return None
        if supplied_requisition is not None and (
                not isinstance(supplied_requisition, str)
                or supplied_requisition.casefold() != requisition):
            return None
        slug = position.group("slug").casefold()
        return {
            "provider": self.provider,
            "tenant": tenant,
            "requisition": requisition,
            "url": f"https://{tenant}.breezy.hr/p/{requisition}-{slug}",
        }

    def analyze_page(self, page, *, check=None) -> dict:
        """Read form metadata only; no controls are filled, clicked, or submitted."""
        if check is not None:
            analysis = analyze_form_page(page, check=check)
        else:
            analysis = analyze_form_page(page)
        if (not isinstance(analysis, dict) or analysis.get("status") != "PASS"
                or not isinstance(analysis.get("fields"), list)
                or not analysis["fields"]):
            return analysis
        try:
            if check is not None:
                check()
            forms = page.locator("form")
            if forms.count() != 1:
                return analysis
            states = forms.first.locator("input, select, textarea").evaluate_all(
                _CONTROL_STATE_SCRIPT
            )
            if check is not None:
                check()
        except Exception:
            if check is not None:
                check()
            return analysis
        fields = analysis["fields"]
        if not isinstance(states, list) or len(states) != len(fields):
            return analysis
        for index, (field, state) in enumerate(zip(fields, states)):
            if (not isinstance(field, dict) or field.get("index") != index
                    or not isinstance(state, dict)
                    or type(state.get("user_visible")) is not bool
                    or type(state.get("user_editable")) is not bool):
                return analysis
        for field, state in zip(fields, states):
            field["user_visible"] = state["user_visible"]
            field["user_editable"] = state["user_editable"]
        return analysis

    @staticmethod
    def requires_human_review(analysis) -> bool:
        """Require review for unknown, sensitive, custom, or unsupported fields."""
        if (not isinstance(analysis, Mapping) or analysis.get("status") != "PASS"
                or not isinstance(analysis.get("fields"), list)
                or not analysis["fields"]):
            return True
        for field in analysis["fields"]:
            if not isinstance(field, Mapping):
                return True
            if field.get("tag") != "input":
                return True
            input_type = field.get("input_type") or "text"
            if (field.get("ambiguous") is not False
                    or field.get("sensitivity") != "STANDARD"
                    or field.get("user_visible") is not True
                    or field.get("user_editable") is not True
                    or not isinstance(input_type, str)
                    or input_type.casefold() not in _SUPPORTED_INPUT_TYPES
                    or not BreezyApplicationAdapter._known_contact_prompt(field)):
                return True
        return False

    @staticmethod
    def _known_contact_prompt(field: Mapping) -> bool:
        labels = field.get("labels")
        if not isinstance(labels, list) or not labels:
            return False

        prompts = []
        for label in labels:
            if not isinstance(label, str) or not label.strip():
                return False
            prompts.append(label)
        for key in ("placeholder", "aria_label"):
            value = field.get(key)
            if value is not None:
                if not isinstance(value, str):
                    return False
                if value.strip():
                    prompts.append(value)

        normalized = [re.sub(r"[^a-z0-9]+", " ", prompt.casefold()).strip()
                      for prompt in prompts]
        return bool(normalized) and all(prompt in _CONTACT_PROMPTS for prompt in normalized)
