"""Privacy-preserving screenshot helper shared by application and CLI flows."""

from concurrent.futures import CancelledError
import json
import math
import time


DEFAULT_CAPTURE_TIMEOUT_MS = 5000
MAX_CAPTURE_TIMEOUT_MS = 30000

# Lazy Playwright selectors run within the screenshot's timeout. Separate
# page.evaluate calls have no timeout and may block on an unresponsive renderer.
_SENSITIVE_TEXT = r"\b(?:password|passwd|cookie|token|api\s+key|access\s+key|gender|sex|race|ethnicity|veteran|disability|sexual\s+orientation|marital\s+status|religion|pronouns?|date\s+of\s+birth|birth\s+date|social\s+security|ssn|national\s+id|work\s+authorization|citizenship|visa\s+status)\b"
_PRIVATE_VALUE = r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|\b(?:bearer\s+)?(?:gh[pousr]_[A-Z0-9_]{12,}|sk-[A-Z0-9_-]{12,}|xox[baprs]-[A-Z0-9-]{12,})\b|\beyJ[A-Z0-9_-]{5,}\.[A-Z0-9_-]{5,}\.[A-Z0-9_-]{5,}\b|\b(?:password|passwd|token|api[_ -]?key|access[_ -]?token|auth[_ -]?token|client[_ -]?secret|cookie)\s*[:=]\s*[^\s,;]+|(?<!\w)(?:\+?1[ .-]?)?(?:\(?\d{3}\)?[ .-]?)\d{3}[ .-]?\d{4}(?!\w)"
PRIVATE_VALUE_SELECTOR = f':text-matches({json.dumps(_PRIVATE_VALUE)}, "i"):not(body):not(html)'
SENSITIVE_REGION_SELECTOR = (
    f':text-matches({json.dumps(_SENSITIVE_TEXT)}, "i")'
    ' >> xpath=parent::*[not(self::body or self::html)]'
)

SCREENSHOT_REDACTION_SCRIPT = r"""
(() => {
  const sensitive = /\b(?:password|passwd|cookie|token|api\s+key|access\s+key|gender|sex|race|ethnicity|veteran|disability|sexual\s+orientation|marital\s+status|religion|pronouns?|date\s+of\s+birth|birth\s+date|social\s+security|ssn|national\s+id|work\s+authorization|citizenship|visa\s+status)\b/i;
  const value = /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|\b(?:bearer\s+)?(?:gh[pousr]_[A-Z0-9_]{12,}|sk-[A-Z0-9_-]{12,}|xox[baprs]-[A-Z0-9-]{12,})\b|\beyJ[A-Z0-9_-]{5,}\.[A-Z0-9_-]{5,}\.[A-Z0-9_-]{5,}\b|\b(?:password|passwd|token|api[_ -]?key|access[_ -]?token|auth[_ -]?token|client[_ -]?secret|cookie)\s*[:=]\s*[^\s,;]+|(?<!\w)(?:\+?1[ .-]?)?(?:\(?\d{3}\)?[ .-]?)\d{3}[ .-]?\d{4}(?!\w)/i;
  const mask = element => {
    if (element && element !== document.body && element !== document.documentElement) element.setAttribute('data-mjobs-sensitive-mask', '');
  };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const text = (node.nodeValue || '').trim();
    if (!text) continue;
    const element = node.parentElement;
    if (value.test(text)) mask(element);
    if (sensitive.test(text) && element.parentElement) mask(element.parentElement);
  }
})();
"""

SCREENSHOT_PRIVACY_STYLE = """
* {
  color: transparent !important;
  -webkit-text-fill-color: transparent !important;
  text-shadow: none !important;
  background-image: none !important;
}
*::before, *::after { content: none !important; }
img, picture, canvas, svg, video, iframe, object, embed {
  visibility: hidden !important;
}
"""


def capture_redacted_screenshot(page, path, *, timeout_ms=None, full_page=True,
                                deadline=None, cancel_event=None):
    """Capture private layout with a finite timeout and no DOM mutations.

    ``deadline`` uses time.monotonic(). Cancellation is cooperative before the
    single screenshot call; in-flight capture is bounded by its timeout. The
    owning browser supervisor remains responsible for process interruption.
    Capture is attempted once; native browser failures propagate unchanged.
    """
    timeout = DEFAULT_CAPTURE_TIMEOUT_MS if timeout_ms is None else timeout_ms
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("Screenshot timeout must be finite and positive")
    if (deadline is not None and (isinstance(deadline, bool)
            or not isinstance(deadline, (int, float)) or not math.isfinite(deadline))):
        raise ValueError("Screenshot deadline must be finite")

    def remaining_timeout():
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("Screenshot cancelled")
        remaining = min(timeout, MAX_CAPTURE_TIMEOUT_MS)
        if deadline is not None:
            remaining = min(remaining, (deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise TimeoutError("Screenshot deadline exceeded")
        return remaining

    remaining_timeout()
    masks = [
        page.locator("input"),
        page.locator("textarea"),
        page.locator("select"),
        page.locator("[contenteditable]:not([contenteditable='false'])"),
        page.locator("[role='textbox']"),
        page.locator("[role='combobox']"),
        page.locator("[data-sensitive]"),
        page.locator("[data-mjobs-sensitive-mask]"),
        page.locator(PRIVATE_VALUE_SELECTOR),
        page.locator(SENSITIVE_REGION_SELECTOR),
    ]
    page.screenshot(
        path=str(path), full_page=full_page, mask=masks, mask_color="#000000",
        style=SCREENSHOT_PRIVACY_STYLE, timeout=remaining_timeout(),
    )
