"""Pure structural Greenhouse board evidence; no network or employer authority."""
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup


def greenhouse_board_tenants(page_url, html, *, tags=('a', 'iframe', 'script')):
    tenants = set()
    soup = BeautifulSoup(str(html or '')[:2_000_000], 'html.parser')
    for tag in soup.find_all(tags):
        raw = tag.get('href') if tag.name == 'a' else tag.get('src')
        if not isinstance(raw, str):
            continue
        url = urlsplit(urljoin(page_url, raw))
        if (url.scheme != 'https' or url.hostname not in {'boards.greenhouse.io', 'job-boards.greenhouse.io'}
                or url.username is not None or url.password is not None or url.port not in (None, 443)):
            continue
        parts = url.path.strip('/').split('/')
        if url.path in ('/embed/job_board', '/embed/job_board/js'):
            candidates = parse_qs(url.query).get('for', [])
            if len(candidates) != 1:
                raise ValueError('GREENHOUSE_BOARD_TENANT_INVALID')
        elif len(parts) == 1 or (len(parts) == 3 and parts[1] == 'jobs' and parts[2].isdigit()):
            candidates = [parts[0]]
        else:
            continue
        for candidate in candidates:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', candidate):
                raise ValueError('GREENHOUSE_BOARD_TENANT_INVALID')
            tenants.add(candidate.casefold())
    return tenants
