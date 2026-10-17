"""Review-first route and form metadata support for Breezy career pages.

Only public tenant job routes are recognized. Form inspection uses the shared
value-free analyzer; uploads, live interaction, and submission are unsupported.
"""
from __future__ import annotations

import math
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
  const rect = element.getBoundingClientRect();
  const paintStyles = ancestors.map((node, index) => ({
    clip_path: styles[index].clipPath,
    clip: styles[index].clip,
    contain: styles[index].contain,
    opacity: styles[index].opacity
  }));
  const overflowClipRegions = [];
  let clipGeometryTransformed = false;
  for (let index = 1; index < ancestors.length; index += 1) {
    const current = ancestors[index];
    const style = styles[index];
    const clipsHorizontally = ['hidden', 'clip', 'scroll', 'auto']
      .includes(style.overflowX);
    const clipsVertically = ['hidden', 'clip', 'scroll', 'auto']
      .includes(style.overflowY);
    if (!clipsHorizontally && !clipsVertically) continue;

    const ancestorRect = current.getBoundingClientRect();
    for (let transformIndex = index; transformIndex < styles.length;
         transformIndex += 1) {
      const transformStyle = styles[transformIndex];
      if (transformStyle.transform !== 'none' ||
          !['none', '1'].includes(transformStyle.scale) ||
          !['none', '0deg'].includes(transformStyle.rotate) ||
          !['none', '0px'].includes(transformStyle.translate) ||
          !['1', 'normal'].includes(transformStyle.zoom)) {
        clipGeometryTransformed = true;
      }
    }
    const clipLeft = ancestorRect.left + current.clientLeft;
    const clipTop = ancestorRect.top + current.clientTop;
    overflowClipRegions.push({
      clips_horizontally: clipsHorizontally,
      clips_vertically: clipsVertically,
      left: clipLeft,
      top: clipTop,
      right: clipLeft + current.clientWidth,
      bottom: clipTop + current.clientHeight
    });
  }
  const hidden = ancestors.some((node, index) => {
    const style = styles[index];
    return node.hidden || node.inert || node.getAttribute('aria-hidden') === 'true' ||
      style.display === 'none' || style.visibility === 'hidden' ||
      style.visibility === 'collapse' || Number(style.opacity) < 0.1 ||
      style.contentVisibility === 'hidden' || style.pointerEvents === 'none';
  });
  const visible = !hidden && element.getClientRects().length > 0 &&
    rect.width > 1 && rect.height > 1 && rect.right > 0 && rect.bottom > 0 &&
    rect.left < window.innerWidth && rect.top < window.innerHeight;
  const hitTarget = document.elementFromPoint(
    rect.left + rect.width / 2, rect.top + rect.height / 2
  );
  const hitTestVisible = hitTarget === element || element.contains(hitTarget);
  const disabled = element.matches(':disabled') || element.readOnly === true ||
    Boolean(element.closest('[aria-disabled="true"], [aria-readonly="true"]'));
  const inputType = element.tagName.toLowerCase() === 'input'
    ? (element.getAttribute('type') || 'text').toLowerCase() : null;
  return {
    user_visible: visible,
    hit_test_visible: hitTestVisible,
    user_editable: visible && hitTestVisible && !disabled && inputType !== 'hidden',
    paint_styles: paintStyles,
    control_rect: {
      left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom
    },
    overflow_clip_regions: overflowClipRegions,
    clip_geometry_transformed: clipGeometryTransformed
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
            timeout_kwargs = {}
            if check is not None:
                remaining = getattr(check, "remaining_seconds", None)
                if callable(remaining):
                    timeout_kwargs["timeout"] = max(
                        1, min(10000, int(remaining() * 1000))
                    )
            states = forms.first.locator("input, select, textarea").evaluate_all(
                _CONTROL_STATE_SCRIPT, **timeout_kwargs
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
        evidence = []
        for index, (field, state) in enumerate(zip(fields, states)):
            if not isinstance(field, dict) or field.get("index") != index:
                return analysis
            state_evidence = self._visible_editable_evidence(state)
            if state_evidence is None:
                return analysis
            evidence.append(state_evidence)
        for field, (user_visible, user_editable) in zip(fields, evidence):
            field["user_visible"] = user_visible
            field["user_editable"] = user_editable
        return analysis

    @staticmethod
    def _visible_editable_evidence(state):
        """Validate geometry and paint evidence before trusting DOM visibility."""
        if (not isinstance(state, dict)
                or type(state.get("user_visible")) is not bool
                or type(state.get("hit_test_visible")) is not bool
                or type(state.get("user_editable")) is not bool
                or type(state.get("clip_geometry_transformed")) is not bool):
            return None
        paint_styles = state.get("paint_styles")
        if not isinstance(paint_styles, list) or not paint_styles:
            return None
        visible = state["user_visible"] and state["hit_test_visible"]
        effective_opacity = 1.0
        for style in paint_styles:
            if not isinstance(style, dict):
                return None
            clip_path, clip, contain = (
                style.get("clip_path"), style.get("clip"), style.get("contain")
            )
            if (not isinstance(clip_path, str) or not isinstance(clip, str)
                    or not isinstance(contain, str)):
                return None
            if clip_path != "none" or clip != "auto":
                visible = False
            containment = set(contain.casefold().split())
            if containment & {"paint", "content", "strict"}:
                visible = False
            try:
                opacity = float(style.get("opacity"))
            except (OverflowError, TypeError, ValueError):
                return None
            if not math.isfinite(opacity):
                return None
            if opacity < 0 or opacity > 1:
                return None
            effective_opacity *= opacity
        if effective_opacity < 0.1:
            visible = False

        rect = state.get("control_rect")
        regions = state.get("overflow_clip_regions")
        if not isinstance(rect, dict) or not isinstance(regions, list):
            return None

        def finite_number(value):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            try:
                number = float(value)
            except OverflowError:
                return None
            return number if math.isfinite(number) else None

        coordinates = {key: finite_number(rect.get(key))
                       for key in ("left", "top", "right", "bottom")}
        if any(value is None for value in coordinates.values()):
            return None
        if state["clip_geometry_transformed"] and regions:
            visible = False
        for region in regions:
            if (not isinstance(region, dict)
                    or type(region.get("clips_horizontally")) is not bool
                    or type(region.get("clips_vertically")) is not bool):
                return None
            bounds = {key: finite_number(region.get(key))
                      for key in ("left", "top", "right", "bottom")}
            if any(value is None for value in bounds.values()):
                return None
            if (region["clips_horizontally"]
                    and (coordinates["right"] <= bounds["left"]
                         or coordinates["left"] >= bounds["right"])):
                visible = False
            if (region["clips_vertically"]
                    and (coordinates["bottom"] <= bounds["top"]
                         or coordinates["top"] >= bounds["bottom"])):
                visible = False
        return visible, visible and state["user_editable"]

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
        if not isinstance(labels, list):
            return False

        prompts = []
        if not labels:
            # The shared value-free projection suppresses rendered label text;
            # an explicit accessible label is the only safe fallback.
            aria_label = field.get("aria_label")
            if not isinstance(aria_label, str) or not aria_label.strip():
                return False
            prompts.append(aria_label)
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
