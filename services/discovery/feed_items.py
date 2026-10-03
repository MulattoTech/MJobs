"""One acceptance/identity rule shared by feed import and link sampling."""
from dataclasses import dataclass
import hashlib
import json
from urllib.parse import urljoin, urlsplit

from services.discovery.sources.greenhouse import plain_text


def _application_url(item, strategy, endpoint):
    value = item.get('url') or item.get('external_url') if strategy == 'PUBLIC_JSON' else item.get('link')
    value = str(value or '').strip()
    if not value:
        return None
    target = urljoin(endpoint, value)
    try:
        parsed = urlsplit(target)
        if (parsed.scheme.casefold() not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        parsed.port
    except ValueError:
        return None
    return target if len(target) <= 2000 else None


@dataclass(frozen=True)
class FeedItem:
    title: str
    application_url: str
    external_key: str
    raw_sha256: str


def describe_feed_item(item, strategy, endpoint):
    if not isinstance(item, dict):
        return None
    title = plain_text(item.get('title'))[:500]
    url = _application_url(item, strategy, endpoint)
    if not title or url is None:
        return None
    key = str(item.get('id') or item.get('guid') or url).strip() or url
    raw = json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return FeedItem(title, url, key, hashlib.sha256(raw.encode()).hexdigest())
