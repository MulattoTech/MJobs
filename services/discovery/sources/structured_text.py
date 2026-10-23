"""Bounded HTML-to-text conversion that keeps posting block boundaries."""
from html import unescape
import re

from bs4 import BeautifulSoup
from bs4.element import NavigableString


_HIDDEN_STYLE = re.compile(
    r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)"
    r"\s*(?:!\s*important\s*)?(?:;|$)",
    re.I,
)
_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "dd", "details", "div",
    "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "main",
    "nav", "ol", "p", "pre", "section", "table", "tbody", "td", "th",
    "thead", "tr", "ul",
}
_MAX_ENTITY_DECODE_PASSES = 8


def structured_text(value):
    """Return safe readable text while preserving headings, paragraphs, and lists."""
    value = str(value or "")[:500_000]
    for _ in range(_MAX_ENTITY_DECODE_PASSES):
        decoded = unescape(value)
        if decoded == value:
            break
        value = decoded
    else:
        if unescape(value) != value:
            return ""

    parsed = BeautifulSoup(value, "html.parser")
    for element in parsed(("script", "style", "noscript", "template")):
        element.decompose()
    for element in list(parsed.find_all(True)):
        if element.parent is None:
            continue
        aria_hidden = element.get("aria-hidden")
        style = element.get("style")
        if element.has_attr("hidden") or (
            isinstance(aria_hidden, str) and aria_hidden.strip().casefold() == "true"
        ) or (isinstance(style, str) and _HIDDEN_STYLE.search(style)):
            element.decompose()

    for element in list(parsed.find_all("br")):
        element.replace_with(NavigableString("\n"))

    for element in list(parsed.find_all("li")):
        element.insert_before(NavigableString("\n- "))
        element.insert_after(NavigableString("\n"))

    for element in list(parsed.find_all(_BLOCK_TAGS)):
        element.insert_before(NavigableString("\n"))
        element.insert_after(NavigableString("\n"))

    lines = []
    for line in parsed.get_text("", strip=False).splitlines():
        normalized = " ".join(line.split())
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)
