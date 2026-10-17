"""Review-first route and form metadata support for Breezy career pages.

Only public tenant job routes are recognized. Form inspection uses the shared
value-free analyzer; uploads, live interaction, and submission are unsupported.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from services.applications.form_analysis import analyze_controls, analyze_form_page


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
_SAFE_LABEL_METADATA_SCRIPT = r"""(form, approvedLabels) => {
  const approved = new Set(approvedLabels);
  const hasRenderedText = node => Boolean(node && String(node.textContent || '').trim());
  const hasVisibleColor = value => {
    const color = String(value || '').trim().toLowerCase();
    if (!color || color === 'transparent') return false;
    const alphaMatch = color.match(/(?:rgba|hsla)\([^)]*,\s*([\d.]+%?)\s*\)$/) ||
      color.match(/\/\s*([\d.]+%?)\s*\)$/);
    if (!alphaMatch) return true;
    const alphaText = alphaMatch[1];
    const alpha = alphaText.endsWith('%')
      ? Number(alphaText.slice(0, -1)) / 100 : Number(alphaText);
    return Number.isFinite(alpha) && alpha >= 0.1;
  };
  const labelIsVisible = label => {
    const ancestors = [];
    for (let node = label; node && node.nodeType === 1; node = node.parentElement) {
      ancestors.push(node);
    }
    const styles = ancestors.map(node => window.getComputedStyle(node));
    let visible = label.getClientRects().length > 0;
    let opacity = 1;
    const rect = label.getBoundingClientRect();
    const visibleBounds = {left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom};
    let transformedClip = false;
    for (let index = 0; index < ancestors.length; index += 1) {
      const node = ancestors[index];
      const style = styles[index];
      opacity *= Number(style.opacity);
      if (!hasVisibleColor(style.color) ||
          !hasVisibleColor(style.webkitTextFillColor || style.color)) visible = false;
      if (node.hidden || node.inert || node.getAttribute('aria-hidden') === 'true' ||
          style.display === 'none' || style.visibility === 'hidden' ||
          style.visibility === 'collapse' || style.contentVisibility === 'hidden' ||
          Number(style.opacity) < 0.1 || style.clipPath !== 'none' || style.clip !== 'auto' ||
          ['paint', 'content', 'strict'].some(value => style.contain.split(/\s+/).includes(value))) {
        visible = false;
      }
      const clipsX = ['hidden', 'clip', 'scroll', 'auto'].includes(style.overflowX);
      const clipsY = ['hidden', 'clip', 'scroll', 'auto'].includes(style.overflowY);
      if (index > 0 && (clipsX || clipsY)) {
        const ancestorRect = node.getBoundingClientRect();
        for (let transformIndex = index; transformIndex < styles.length; transformIndex += 1) {
          const candidate = styles[transformIndex];
          if (candidate.transform !== 'none' ||
              !['none', '1'].includes(candidate.scale) ||
              !['none', '0deg'].includes(candidate.rotate) ||
              !['none', '0px'].includes(candidate.translate) ||
              !['1', 'normal'].includes(candidate.zoom)) transformedClip = true;
        }
        const left = ancestorRect.left + node.clientLeft;
        const top = ancestorRect.top + node.clientTop;
        if (clipsX) {
          visibleBounds.left = Math.max(visibleBounds.left, left);
          visibleBounds.right = Math.min(visibleBounds.right, left + node.clientWidth);
        }
        if (clipsY) {
          visibleBounds.top = Math.max(visibleBounds.top, top);
          visibleBounds.bottom = Math.min(visibleBounds.bottom, top + node.clientHeight);
        }
      }
    }
    const centerX = (visibleBounds.left + visibleBounds.right) / 2;
    const centerY = (visibleBounds.top + visibleBounds.bottom) / 2;
    const nodes = document.querySelectorAll('*');
    // An incomplete bounded scan is not evidence that the label is visible.
    if (nodes.length > 5000) return false;
    const pointerNoneOverlap = Array.from(nodes).some(node => {
      if (node === label || node.contains(label) || label.contains(node)) return false;
      const style = window.getComputedStyle(node);
      if (style.pointerEvents !== 'none' || node.hidden || style.display === 'none' ||
          style.visibility === 'hidden' || style.visibility === 'collapse' ||
          Number(style.opacity) < 0.1) return false;
      const box = node.getBoundingClientRect();
      return box.left <= centerX && box.right >= centerX &&
        box.top <= centerY && box.bottom >= centerY;
    });
    const hitTarget = document.elementFromPoint(centerX, centerY);
    const hitVisible = hitTarget === label || label.contains(hitTarget);
    const baseVisible = visible && !transformedClip && !pointerNoneOverlap && opacity >= 0.1 &&
      hitVisible &&
      visibleBounds.right - visibleBounds.left > 1 &&
      visibleBounds.bottom - visibleBounds.top > 1 &&
      visibleBounds.right > 0 && visibleBounds.bottom > 0 &&
      visibleBounds.left < window.innerWidth && visibleBounds.top < window.innerHeight;
    if (!baseVisible) return false;

    if (label.querySelectorAll('*').length > 100) return false;
    const textWalker = document.createTreeWalker(label, NodeFilter.SHOW_TEXT);
    let inspectedTextNodes = 0;
    while (textWalker.nextNode()) {
      const textNode = textWalker.currentNode;
      if (!String(textNode.nodeValue || '').trim()) continue;
      inspectedTextNodes += 1;
      if (inspectedTextNodes > 100) return false;
      let textOpacity = 1;
      let node = textNode.parentElement;
      while (node) {
        const style = window.getComputedStyle(node);
        textOpacity *= Number(style.opacity);
        if (!hasVisibleColor(style.color) ||
            !hasVisibleColor(style.webkitTextFillColor || style.color) ||
            node.hidden || node.inert || node.getAttribute('aria-hidden') === 'true' ||
            style.display === 'none' || style.visibility === 'hidden' ||
            style.visibility === 'collapse' || style.contentVisibility === 'hidden' ||
            Number(style.opacity) < 0.1 || style.clipPath !== 'none' || style.clip !== 'auto' ||
            ['paint', 'content', 'strict'].some(value => style.contain.split(/\s+/).includes(value)) ||
            ['hidden', 'clip', 'scroll', 'auto'].includes(style.overflowX) ||
            ['hidden', 'clip', 'scroll', 'auto'].includes(style.overflowY)) return false;
        if (node === label) break;
        node = node.parentElement;
      }
      if (node !== label || textOpacity < 0.1) return false;
      const range = document.createRange();
      range.selectNode(textNode);
      const textRects = Array.from(range.getClientRects());
      if (!textRects.length || textRects.some(rect =>
          rect.width <= 1 || rect.height <= 1 || rect.right <= 0 || rect.bottom <= 0 ||
          rect.left >= window.innerWidth || rect.top >= window.innerHeight ||
          rect.right <= visibleBounds.left || rect.left >= visibleBounds.right ||
          rect.bottom <= visibleBounds.top || rect.top >= visibleBounds.bottom)) return false;
    }
    return true;
  };
  const controls = Array.from(form.querySelectorAll('input, select, textarea')).slice(0, 101);
  return controls.map(element => {
    const tag = element.tagName.toLowerCase();
    const fieldset = element.closest('fieldset');
    let section = element.parentElement;
    let heading = null;
    while (section && section !== form.parentElement) {
      if (section.matches('section,article,main,form')) {
        heading = section.querySelector('h1,h2,h3,h4,h5,h6');
        if (heading) break;
      }
      section = section.parentElement;
    }
    const legend = fieldset && fieldset.querySelector('legend');
    return {
      tag,
      rendered_labels: Array.from(element.labels || []).slice(0, 21).map(label => {
        const visible = labelIsVisible(label);
        const text = visible ? String(label.textContent || '') : '';
        const hasText = Boolean(text.trim());
        let canonicalLabel = null;
        if (hasText && visible && /^[\x00-\x7F]*$/.test(text)) {
          const normalized = text.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
          if (approved.has(normalized)) canonicalLabel = normalized;
        }
        return {
          canonical_label: canonicalLabel,
          has_text: hasText,
          visible
        };
      }),
      name: element.getAttribute('name'),
      id: element.getAttribute('id'),
      placeholder: element.getAttribute('placeholder'),
      aria_label: element.getAttribute('aria-label'),
      input_type: tag === 'input' ? (element.getAttribute('type') || 'text') : null,
      nearby_text: null,
      section_heading: null,
      fieldset_legend: null,
      unsafe_text_context: hasRenderedText(heading) || hasRenderedText(legend),
      options: []
    };
  });
}"""
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
  const hitX = rect.left + rect.width / 2;
  const hitY = rect.top + rect.height / 2;
  const allNodes = document.querySelectorAll('*');
  const pointerNoneOverlap = allNodes.length > 5000 || Array.from(allNodes).some(node => {
    if (node === element || node.contains(element) || element.contains(node)) return false;
    const style = window.getComputedStyle(node);
    if (style.pointerEvents !== 'none' || node.hidden || style.display === 'none' ||
        style.visibility === 'hidden' || style.visibility === 'collapse' ||
        Number(style.opacity) < 0.1) return false;
    const box = node.getBoundingClientRect();
    return box.left <= hitX && box.right >= hitX && box.top <= hitY && box.bottom >= hitY;
  });
  const hitTestVisible = (hitTarget === element || element.contains(hitTarget)) &&
    !pointerNoneOverlap;
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
        if (isinstance(analysis, dict) and analysis.get("status") == "UNKNOWN"
                and analysis.get("reason") == "UNSAFE_TEXT_CONTEXT"):
            analysis = self._reanalyze_recognized_labels(page, check=check)
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
    def _reanalyze_recognized_labels(page, *, check=None) -> dict:
        """Keep only whitelisted associated prompt labels; never read control values."""
        def unknown(reason):
            return {"status": "UNKNOWN", "reason": reason, "fields": []}

        try:
            if check is not None:
                check()
            forms = page.locator("form")
            if forms.count() != 1:
                return unknown("AMBIGUOUS_FORM")
            timeout_kwargs = {}
            if check is not None:
                remaining = getattr(check, "remaining_seconds", None)
                if callable(remaining):
                    timeout_kwargs["timeout"] = max(1, min(10000, int(remaining() * 1000)))
            controls = forms.first.evaluate(
                _SAFE_LABEL_METADATA_SCRIPT, sorted(_CONTACT_PROMPTS), **timeout_kwargs
            )
            if check is not None:
                check()
        except Exception:
            if check is not None:
                check()
            return unknown("DOM_ANALYSIS_FAILED")

        if not isinstance(controls, list):
            return unknown("INVALID_CONTROL_LIST")
        for control in controls:
            if not isinstance(control, dict):
                return unknown("INVALID_CONTROL_METADATA")
            rendered_labels = control.pop("rendered_labels", None)
            if not isinstance(rendered_labels, list) or len(rendered_labels) > 20:
                return unknown("INVALID_LABEL_METADATA")
            safe_labels = []
            for rendered in rendered_labels:
                if (not isinstance(rendered, dict)
                        or type(rendered.get("has_text")) is not bool
                        or type(rendered.get("visible")) is not bool):
                    return unknown("INVALID_LABEL_METADATA")
                normalized = rendered.get("canonical_label")
                if normalized is not None and (
                        not isinstance(normalized, str)
                        or normalized not in _CONTACT_PROMPTS):
                    return unknown("INVALID_LABEL_METADATA")
                if not rendered["has_text"]:
                    continue
                if not rendered["visible"]:
                    return unknown("NO_VISIBLE_ASSOCIATED_LABEL")
                if normalized is None:
                    return unknown("UNRECOGNIZED_RENDERED_LABEL")
                if normalized not in safe_labels:
                    safe_labels.append(normalized)
            if rendered_labels and not safe_labels:
                return unknown("NO_VISIBLE_ASSOCIATED_LABEL")
            control["labels"] = safe_labels
        return analyze_controls(controls)

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
        visible_bounds = coordinates.copy()
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
            if region["clips_horizontally"]:
                visible_bounds["left"] = max(visible_bounds["left"], bounds["left"])
                visible_bounds["right"] = min(visible_bounds["right"], bounds["right"])
            if region["clips_vertically"]:
                visible_bounds["top"] = max(visible_bounds["top"], bounds["top"])
                visible_bounds["bottom"] = min(visible_bounds["bottom"], bounds["bottom"])
            if (visible_bounds["right"] <= visible_bounds["left"]
                    or visible_bounds["bottom"] <= visible_bounds["top"]):
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
