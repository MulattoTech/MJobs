"""Bounded discovery and validation for first-party machine-readable job feeds."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


_FEED_TYPES = {
    "application/feed+json": "PUBLIC_JSON",
    "application/json": "PUBLIC_JSON",
    "application/rss+xml": "RSS",
    "application/atom+xml": "RSS",
}
_PREFERENCE = {"PUBLIC_JSON": 0, "RSS": 1}
_MAX_FEED_BYTES = 2_000_000
_MAX_JOBS = 1000


def _origin(url):
    try:
        parsed = urlsplit(str(url or ""))
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        port = parsed.port
        return parsed.scheme.casefold(), parsed.hostname.casefold().rstrip("."), port
    except (TypeError, ValueError):
        return None


def machine_readable_job_endpoints(page_url, html):
    """Return supported alternate feeds, same-origin only and best format first."""
    origin = _origin(page_url)
    if origin is None:
        return []
    found = {}
    soup = BeautifulSoup(str(html or "")[:2_000_000], "html.parser")
    for link in soup.find_all("link", href=True):
        rel = link.get("rel") or ()
        if isinstance(rel, str):
            rel = rel.split()
        if "alternate" not in {str(item).casefold() for item in rel}:
            continue
        strategy = _FEED_TYPES.get(str(link.get("type") or "").split(";")[0].strip().casefold())
        if strategy is None:
            continue
        try:
            target = urljoin(page_url, str(link["href"]).strip())
        except ValueError:
            continue
        target_origin = _origin(target)
        if target_origin is None:
            continue
        normalized_origin = lambda value: (value[0], value[1],
            None if value[2] == (80 if value[0] == "http" else 443) else value[2])
        if normalized_origin(target_origin) != normalized_origin(origin):
            continue
        existing = found.get(target)
        if existing is None or _PREFERENCE[strategy] < _PREFERENCE[existing["strategy"]]:
            found[target] = {"url": target, "strategy": strategy}
    return sorted(found.values(), key=lambda row: (_PREFERENCE[row["strategy"]], row["url"]))


def parse_public_job_feed(body, strategy):
    """Validate RSS/Atom or JSON Feed and return bounded, untrusted raw items."""
    if not isinstance(body, str) or len(body.encode("utf-8")) > _MAX_FEED_BYTES:
        raise ValueError("COMPANY_JOB_FEED_SIZE_INVALID")
    if strategy == "PUBLIC_JSON":
        try:
            payload = json.loads(body)
        except (TypeError, ValueError) as error:
            raise ValueError("COMPANY_JOB_FEED_INVALID") from error
        if (not isinstance(payload, dict)
                or not str(payload.get("version", "")).startswith("https://jsonfeed.org/version/")
                or not isinstance(payload.get("items"), list)):
            raise ValueError("COMPANY_JOB_FEED_INVALID")
        items = payload["items"]
        if len(items) > _MAX_JOBS:
            raise ValueError("COMPANY_JOB_FEED_JOB_LIMIT")
        if any(not isinstance(item, dict) or not str(item.get("title") or "").strip()
               or not str(item.get("url") or item.get("external_url") or "").strip()
               for item in items):
            raise ValueError("COMPANY_JOB_FEED_INVALID")
        return items
    if strategy != "RSS":
        raise ValueError("COMPANY_JOB_FEED_STRATEGY_INVALID")
    # DTDs and entities are unnecessary for public job feeds and can expand a
    # small response into a large document before the item limit is checked.
    if "<!doctype" in body.casefold() or "<!entity" in body.casefold():
        raise ValueError("COMPANY_JOB_FEED_INVALID")
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, ValueError) as error:
        raise ValueError("COMPANY_JOB_FEED_INVALID") from error
    root_name = root.tag.rsplit("}", 1)[-1].casefold()
    if root_name == "rss":
        items = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].casefold() == "item"]
    elif root_name == "feed":
        items = [node for node in root if node.tag.rsplit("}", 1)[-1].casefold() == "entry"]
    else:
        raise ValueError("COMPANY_JOB_FEED_INVALID")
    if len(items) > _MAX_JOBS:
        raise ValueError("COMPANY_JOB_FEED_JOB_LIMIT")
    normalized = []
    for item in items:
        row = {}
        for child in item:
            key = child.tag.rsplit("}", 1)[-1].casefold()
            if key == "link":
                row[key] = child.attrib.get("href") or child.text or ""
            else:
                row[key] = child.text or ""
        if not str(row.get("title") or "").strip() or not str(row.get("link") or "").strip():
            raise ValueError("COMPANY_JOB_FEED_INVALID")
        normalized.append(row)
    return normalized
