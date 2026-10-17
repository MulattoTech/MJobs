"""Review-first support for canonical Teamtailor career-site routes.

This adapter normalizes public job URLs and inspects an already-loaded page for
value-free form metadata. It never fills controls, uploads documents, or submits
applications. Unsupported or challenge-protected forms require manual review.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from services.applications.form_analysis import analyze_form_page


_TEAMTAILOR_HOST = re.compile(r"(?P<tenant>[a-z0-9][a-z0-9-]{0,62})\.teamtailor\.com\Z", re.I)
_JOB_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,119}\Z", re.I)
_CHALLENGE_SELECTORS = (
    '[class*="captcha" i]', '[id*="captcha" i]', 'iframe[src*="captcha" i]',
    '[class*="turnstile" i]', '[id*="turnstile" i]', '[data-sitekey]',
    '[data-cf-chl]', 'iframe[src*="challenge" i]',
)
_CONTROL_STATE_SCRIPT = r"""nodes => nodes.map(node => {
  const ancestors = [];
  for (let current = node; current && current.nodeType === 1;
       current = current.parentElement) ancestors.push(current);
  const styles = ancestors.map(current => getComputedStyle(current));
  const rect = node.getBoundingClientRect();
  const paintStyles = ancestors.map((current, index) => ({
    clip_path: styles[index].clipPath,
    clip: styles[index].clip,
    contain: styles[index].contain
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
    // clientWidth/clientHeight are CSS-pixel dimensions while rect coordinates
    // are viewport-space. A transform anywhere from this clipping ancestor to
    // the root can make those coordinate systems differ, so defer to a person.
    for (let transformIndex = index; transformIndex < styles.length;
         transformIndex += 1) {
      const transformStyle = styles[transformIndex];
      const hasNonIdentityTransform = transformStyle.transform !== 'none' ||
        !['none', '1'].includes(transformStyle.scale) ||
        !['none', '0deg'].includes(transformStyle.rotate) ||
        !['none', '0px'].includes(transformStyle.translate) ||
        !['1', 'normal'].includes(transformStyle.zoom);
      if (hasNonIdentityTransform) clipGeometryTransformed = true;
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
  const userVisible = !ancestors.some((current, index) => {
    const style = styles[index];
    return current.hidden || current.inert ||
      current.getAttribute('aria-hidden') === 'true' ||
      style.display === 'none' || style.visibility === 'hidden' ||
      style.visibility === 'collapse' || Number(style.opacity) === 0 ||
      style.contentVisibility === 'hidden' || style.pointerEvents === 'none';
  }) && node.getClientRects().length > 0 && rect.width > 1 && rect.height > 1 &&
    rect.right > 0 && rect.bottom > 0 && rect.left < window.innerWidth &&
    rect.top < window.innerHeight;
  const ariaDisabled = node.closest('[aria-disabled="true"]');
  const ariaReadonly = node.closest('[aria-readonly="true"]');
  const inputType = node.tagName.toLowerCase() === 'input'
    ? (node.getAttribute('type') || 'text').toLowerCase() : null;
  const supportedControl = ['input', 'select', 'textarea'].includes(
    node.tagName.toLowerCase()
  );
  return {
    user_visible: userVisible,
    user_editable: userVisible && supportedControl &&
      !node.matches(':disabled') && node.readOnly !== true &&
      !ariaDisabled && !ariaReadonly && inputType !== 'hidden',
    paint_styles: paintStyles,
    control_rect: {
      left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom
    },
    overflow_clip_regions: overflowClipRegions,
    clip_geometry_transformed: clipGeometryTransformed
  };
})"""
_KNOWN_CONTACT_IDENTIFIERS = {
    "address", "city", "country", "cover letter", "cv", "email",
    "email address", "family name", "first name", "full name", "github",
    "github profile", "given name", "last name", "linkedin",
    "linkedin profile", "linkedin url", "location", "mobile",
    "mobile phone", "name", "personal website", "phone", "phone number",
    "portfolio", "postal code", "preferred name", "resume", "state",
    "surname", "website", "zip code",
}
_USER_EDITABLE_INPUT_TYPES = {
    "text", "email", "tel", "url", "number", "date", "datetime-local",
    "time", "month", "week", "search", "checkbox", "radio",
}


class TeamtailorApplicationAdapter:
    """Recognize canonical Teamtailor job routes and inspect forms safely."""

    provider = "teamtailor"

    def capabilities(self) -> dict[str, bool]:
        """List only the route and value-free review behavior implemented here."""
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
        """Normalize only canonical tenant.teamtailor.com/jobs/{slug} URLs."""
        if not isinstance(route, Mapping):
            return None
        provider = route.get("provider")
        url = route.get("url")
        if (not isinstance(provider, str) or provider.casefold() != self.provider
                or not isinstance(url, str) or not url or len(url) > 2048):
            return None
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return None
        host = (parsed.hostname or "").casefold()
        match = _TEAMTAILOR_HOST.fullmatch(host)
        if (parsed.scheme.casefold() != "https" or match is None or port is not None
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment):
            return None
        parts = parsed.path.split("/")
        if len(parts) != 3 or parts[0] != "" or parts[1] != "jobs":
            return None
        slug = parts[2]
        if not _JOB_SLUG.fullmatch(slug):
            return None

        tenant = match.group("tenant").casefold()
        slug = slug.casefold()
        supplied_tenant = route.get("tenant")
        supplied_requisition = route.get("requisition")
        if (supplied_tenant is not None
                and (not isinstance(supplied_tenant, str)
                     or supplied_tenant.casefold() != tenant)):
            return None
        if (supplied_requisition is not None
                and (not isinstance(supplied_requisition, str)
                     or supplied_requisition.casefold() != slug)):
            return None
        return {
            "provider": self.provider,
            "tenant": tenant,
            "requisition": slug,
            "url": f"https://{tenant}.teamtailor.com/jobs/{slug}",
        }

    def analyze_page(self, page, *, check=None) -> dict:
        """Inspect a loaded form's safe metadata; challenge signals stop analysis."""
        if check is not None:
            check()
        try:
            for selector in _CHALLENGE_SELECTORS:
                if check is not None:
                    check()
                if page.locator(selector).count() > 0:
                    return {"status": "UNKNOWN", "reason": "CHALLENGE_PROTECTED", "fields": []}
        except Exception:
            if check is not None:
                check()
            return {"status": "UNKNOWN", "reason": "CHALLENGE_DETECTION_UNAVAILABLE", "fields": []}
        analysis = (analyze_form_page(page, check=check) if check is not None
                    else analyze_form_page(page))
        if not isinstance(analysis, Mapping) or analysis.get("status") != "PASS":
            return analysis
        fields = analysis.get("fields")
        if not isinstance(fields, list) or not fields:
            return {"status": "UNKNOWN", "reason": "CONTROL_STATE_UNAVAILABLE", "fields": []}

        try:
            if check is not None:
                check()
            timeout_kwargs = {}
            if check is not None:
                remaining = getattr(check, "remaining_seconds", None)
                if callable(remaining):
                    timeout_kwargs["timeout"] = max(1, min(10000, int(remaining() * 1000)))
            control_states = page.locator(
                "form input, form select, form textarea"
            ).evaluate_all(_CONTROL_STATE_SCRIPT, **timeout_kwargs)
            if check is not None:
                check()
        except Exception:
            if check is not None:
                check()
            return {"status": "UNKNOWN", "reason": "CONTROL_STATE_UNAVAILABLE", "fields": []}
        if (not isinstance(control_states, list) or len(control_states) != len(fields)
                or any(not isinstance(state, Mapping)
                       or type(state.get("user_visible")) is not bool
                       or type(state.get("user_editable")) is not bool
                       or type(state.get("clip_geometry_transformed")) is not bool
                       for state in control_states)):
            return {"status": "UNKNOWN", "reason": "CONTROL_STATE_UNAVAILABLE", "fields": []}
        for field, state in zip(fields, control_states):
            if not isinstance(field, dict):
                return {"status": "UNKNOWN", "reason": "CONTROL_STATE_UNAVAILABLE", "fields": []}
            user_visible = _is_effectively_user_visible(state)
            field["user_visible"] = user_visible
            field["user_editable"] = state["user_editable"] and user_visible
        return analysis

    @staticmethod
    def requires_human_review(analysis) -> bool:
        """Require manual review unless every field is a known standard contact field."""
        if (not isinstance(analysis, Mapping) or analysis.get("status") != "PASS"
                or not isinstance(analysis.get("fields"), list)
                or not analysis["fields"]):
            return True
        for field in analysis["fields"]:
            if (not isinstance(field, Mapping) or field.get("ambiguous") is not False
                    or field.get("sensitivity") != "STANDARD"
                    or field.get("user_visible") is not True
                    or field.get("user_editable") is not True
                    or not _known_contact_field(field)):
                return True
        return False


def _is_effectively_user_visible(state: Mapping) -> bool:
    """Fail closed unless DOM visibility and every paint clip are measurable."""
    if state.get("user_visible") is not True:
        return False

    paint_styles = state.get("paint_styles")
    control_rect = state.get("control_rect")
    clip_regions = state.get("overflow_clip_regions")
    if (not isinstance(paint_styles, list) or not paint_styles
            or not isinstance(control_rect, Mapping)
            or not isinstance(clip_regions, list)
            or type(state.get("clip_geometry_transformed")) is not bool):
        return False
    if clip_regions and state["clip_geometry_transformed"]:
        return False

    for style in paint_styles:
        if (not isinstance(style, Mapping)
                or not isinstance(style.get("clip_path"), str)
                or not isinstance(style.get("clip"), str)
                or not isinstance(style.get("contain"), str)
                or style["clip_path"].casefold() != "none"
                or style["clip"].casefold() != "auto"
                or {"paint", "strict", "content"}.intersection(
                    style["contain"].casefold().split()
                )):
            return False

    def coordinates(rect: Mapping):
        values = tuple(rect.get(key) for key in ("left", "top", "right", "bottom"))
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            try:
                if not math.isfinite(value):
                    return None
            except (OverflowError, TypeError):
                return None
        left, top, right, bottom = values
        if right <= left or bottom <= top:
            return None
        return left, top, right, bottom

    control_bounds = coordinates(control_rect)
    if control_bounds is None:
        return False
    control_left, control_top, control_right, control_bottom = control_bounds

    for clip_region in clip_regions:
        if (not isinstance(clip_region, Mapping)
                or type(clip_region.get("clips_horizontally")) is not bool
                or type(clip_region.get("clips_vertically")) is not bool):
            return False
        clip_bounds = coordinates(clip_region)
        if clip_bounds is None:
            return False
        clip_left, clip_top, clip_right, clip_bottom = clip_bounds
        if (clip_region["clips_horizontally"]
                and (control_left < clip_left or control_right > clip_right)):
            return False
        if (clip_region["clips_vertically"]
                and (control_top < clip_top or control_bottom > clip_bottom)):
            return False
    return True


def _known_contact_field(field: Mapping) -> bool:
    """Recognize a narrow set of contact/document prompts, never custom questions."""
    tag = field.get("tag")
    if tag == "input":
        input_type = field.get("input_type")
        if (not isinstance(input_type, str)
                or input_type.casefold() not in _USER_EDITABLE_INPUT_TYPES):
            return False
    elif tag == "select":
        if field.get("input_type") is not None:
            return False
    else:
        # Free-form text areas and unknown controls always need a person.
        return False

    labels = field.get("labels")
    visible_labels = []
    if isinstance(labels, list):
        visible_labels = [
            re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()
            for label in labels if isinstance(label, str) and label.strip()
        ]
    if visible_labels:
        return all(label in _KNOWN_CONTACT_IDENTIFIERS for label in visible_labels)
    for key in ("name", "id", "placeholder", "aria_label"):
        values = field.get(key)
        if isinstance(values, str):
            values = [values]
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str):
                    normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
                    if normalized in _KNOWN_CONTACT_IDENTIFIERS:
                        return True
    return False
