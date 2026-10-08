"""Extract same-origin career-page candidates from employer pages."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


CAREER_TERMS = ("career", "job", "employment", "work with us", "open roles", "join us")
COMMON_CAREER_PATHS = (
    "/careers", "/jobs", "/careers/jobs", "/jobs/search", "/work-with-us",
    "/join-us", "/join-our-team", "/employment", "/opportunities",
)
SITEMAP_NAMESPACE = "http://www.sitemaps.org/schemas/sitemap/0.9"
MAX_SITEMAP_BYTES = 2 * 1024 * 1024
MAX_SITEMAP_ENTRIES = 10_000
_DOCTYPE_OR_ENTITY = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


def _origin(url):
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return None
        return parts.scheme.casefold(), parts.hostname.casefold().rstrip("."), parts.port
    except ValueError:
        return None


def internal_career_links(page_url, html):
    """Find navigation/footer/structured/sitemap links, restricted to page origin."""
    soup = BeautifulSoup(html or "", "html.parser")
    page_origin = _origin(page_url)
    if not page_origin:
        return []
    candidates = []
    for link in soup.find_all(["a", "link"]):
        href = link.get("href")
        label = " ".join((link.get_text(" ", strip=True), str(link.get("rel") or ""),
                          str(link.get("type") or ""), str(link.get("aria-label") or ""))).casefold()
        if not href or not any(term in label for term in CAREER_TERMS):
            continue
        target = urljoin(page_url, href)
        if _origin(target) == page_origin:
            candidates.append(target)
    # Sitemap XML entries do not have anchors; accept only career-related same-origin <loc>s.
    if "<urlset" in (html or "").casefold() or "<sitemapindex" in (html or "").casefold():
        for loc in soup.find_all("loc"):
            target = loc.get_text(strip=True)
            if any(term in target.casefold() for term in CAREER_TERMS) and _origin(target) == page_origin:
                candidates.append(target)
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, ValueError):
            continue
        stack = payload if isinstance(payload, list) else [payload]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                for key, value in item.items():
                    if key in {"url", "sameAs", "workLocation"}:
                        values = value if isinstance(value, list) else [value]
                        for raw in values:
                            if isinstance(raw, str) and any(term in raw.casefold() for term in CAREER_TERMS):
                                target = urljoin(page_url, raw)
                                if _origin(target) == page_origin:
                                    candidates.append(target)
                    elif isinstance(value, (dict, list)):
                        stack.append(value)
    return list(dict.fromkeys(candidates))


def sitemap_locations(page_url, html) -> list[str]:
    """Return advertised, same-origin sitemap URLs from an HTML page.

    Explicit ``rel=sitemap`` links are preferred. A conventional same-origin
    sitemap XML link is also accepted when its URL itself clearly identifies
    it as a sitemap. This helper only extracts URLs; callers own safe fetching.
    """
    page_origin = _origin(page_url)
    if not page_origin or not isinstance(html, str):
        return []
    if len(html.encode("utf-8", errors="replace")) > MAX_SITEMAP_BYTES:
        return []

    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for link in soup.find_all(["a", "link"]):
        href = link.get("href")
        if not isinstance(href, str) or not href.strip():
            continue
        rel = link.get("rel") or []
        if isinstance(rel, str):
            rel = rel.split()
        rel_sitemap = any(str(token).casefold() == "sitemap" for token in rel)
        try:
            target = urljoin(page_url, href.strip())
            path = urlsplit(target).path.casefold()
        except ValueError:
            continue
        conventional_sitemap = "sitemap" in path and path.endswith((".xml", ".xml.gz"))
        if not rel_sitemap and not conventional_sitemap:
            continue
        if _origin(target) == page_origin:
            candidates.append(target)
    return list(dict.fromkeys(candidates))


def parse_sitemap(sitemap_url, xml_text) -> dict:
    """Parse a bounded sitemap urlset or index, returning same-origin links.

    Malformed, unsupported, oversized, credentialed, or non-HTTP(S) inputs
    fail closed with empty result lists. Sitemap fetching is deliberately
    outside this pure parser.
    """
    empty = {"career_pages": [], "child_sitemaps": []}
    sitemap_origin = _origin(sitemap_url)
    if not sitemap_origin or not isinstance(xml_text, str):
        return empty
    try:
        encoded = xml_text.encode("utf-8", errors="strict")
    except UnicodeError:
        return empty
    if len(encoded) > MAX_SITEMAP_BYTES or _DOCTYPE_OR_ENTITY.search(xml_text):
        return empty
    try:
        root = ET.fromstring(encoded)
    except (ET.ParseError, ValueError, UnicodeError):
        return empty

    root_name = root.tag
    namespace = ""
    if isinstance(root_name, str) and root_name.startswith("{") and "}" in root_name:
        namespace, root_name = root_name[1:].split("}", 1)
    if root_name not in {"urlset", "sitemapindex"} or namespace not in {"", SITEMAP_NAMESPACE}:
        return empty

    result = {"career_pages": [], "child_sitemaps": []}
    item_name = "url" if root_name == "urlset" else "sitemap"
    target_name = "career_pages" if root_name == "urlset" else "child_sitemaps"
    allowed_namespace = "{" + namespace + "}" if namespace else ""
    count = 0
    for item in root:
        if item.tag != allowed_namespace + item_name:
            return empty
        count += 1
        if count > MAX_SITEMAP_ENTRIES:
            return empty
        locs = [child for child in item if child.tag == allowed_namespace + "loc"]
        if len(locs) != 1 or not isinstance(locs[0].text, str):
            return empty
        raw = locs[0].text.strip()
        if not raw:
            continue
        try:
            parts = urlsplit(raw)
            # Sitemap <loc> values must be absolute URLs. Relative values are
            # ambiguous and are not interpreted as targets to fetch.
            if not parts.scheme or not parts.netloc:
                continue
            if _origin(raw) != sitemap_origin:
                continue
        except ValueError:
            continue
        if target_name == "career_pages" and not any(term in raw.casefold() for term in CAREER_TERMS):
            continue
        result[target_name].append(raw)

    result[target_name] = list(dict.fromkeys(result[target_name]))
    return result


def common_career_routes(homepage_url):
    """Return low-confidence same-origin guesses for subsequent content checks."""
    if not _origin(homepage_url):
        return []
    return [urljoin(homepage_url, path) for path in COMMON_CAREER_PATHS]
