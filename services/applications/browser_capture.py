"""Privacy-preserving screenshot helper shared by application and CLI flows."""

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


def capture_redacted_screenshot(page, path, *, timeout_ms=None, full_page=True):
    """Capture layout while hiding page text, controls, and embedded media."""
    page.evaluate(SCREENSHOT_REDACTION_SCRIPT)
    masks = [
        page.locator("input"),
        page.locator("textarea"),
        page.locator("select"),
        page.locator("[contenteditable]:not([contenteditable='false'])"),
        page.locator("[role='textbox']"),
        page.locator("[role='combobox']"),
        page.locator("[data-sensitive]"),
        page.locator("[data-mjobs-sensitive-mask]"),
    ]
    try:
        options = {
            "path": str(path), "full_page": full_page, "mask": masks,
            "mask_color": "#000000", "style": SCREENSHOT_PRIVACY_STYLE,
        }
        if timeout_ms is not None:
            options["timeout"] = timeout_ms
        page.screenshot(**options)
    finally:
        page.evaluate(
            "document.querySelectorAll('[data-mjobs-sensitive-mask]')"
            ".forEach(node => node.removeAttribute('data-mjobs-sensitive-mask'))"
        )
