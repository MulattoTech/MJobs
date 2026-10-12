"""Pure structural Greenhouse board evidence; no network or employer authority."""
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup


def lever_board_tenants(page_url, html, *, tags=('a','iframe','script')):
    """Region-qualified identities from exact public Lever board/listing URLs."""
    from services.discovery.sources.lever import tenant_identity
    tenants = set()
    soup = BeautifulSoup(str(html or '')[:2_000_000],'html.parser')
    for tag in soup.find_all(tags):
        raw = tag.get('href') if tag.name == 'a' else tag.get('src')
        if not isinstance(raw,str):
            continue
        url = urlsplit(urljoin(page_url,raw))
        instance = {'jobs.lever.co':'global','jobs.eu.lever.co':'eu'}.get(url.hostname)
        if (not instance or url.scheme!='https' or url.username is not None
                or url.password is not None or url.port is not None or url.fragment):
            continue
        parts = url.path.strip('/').split('/')
        if len(parts)==1 or (len(parts) in (2,3) and
                re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',parts[1],re.I)
                and (len(parts)==2 or parts[2]=='apply')):
            tenants.add(tenant_identity(instance+':'+parts[0]))
    return tenants


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
