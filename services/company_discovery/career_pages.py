"""Extract same-origin career-page candidates from employer pages."""
from __future__ import annotations

import json
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


CAREER_TERMS = ("career", "job", "employment", "work with us", "open roles", "join us")
COMMON_CAREER_PATHS = (
    "/careers", "/jobs", "/careers/jobs", "/jobs/search", "/work-with-us",
    "/join-us", "/join-our-team", "/employment", "/opportunities",
)


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


def common_career_routes(homepage_url):
    """Return low-confidence same-origin guesses for subsequent content checks."""
    if not _origin(homepage_url):
        return []
    return [urljoin(homepage_url, path) for path in COMMON_CAREER_PATHS]
