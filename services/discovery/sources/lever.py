"""Bounded public Lever GET inventory, with instance-qualified source identity."""
import math
import re
import time
from urllib.parse import urlsplit

from services.discovery.sources.greenhouse import Greenhouse, plain_text
from services.discovery.sources.errors import SourceError
from services.discovery.sources.contract import SearchBatch, SourceCompany, SourceItem
from services.discovery.sources.structured_text import structured_text
from services.discovery.search_semantics import (
    classify_location, normalize_employment_type, normalize_experience_level,
    normalize_title, parse_education_requirement, parse_salary,
)
from services.discovery.skill_taxonomy import extract_skills
from services.discovery.requirement_parsers import parse_job_requirements
from services.discovery.negative_filters import parse_negative_conditions


_SITE = re.compile(r'[a-z0-9][a-z0-9_-]{0,79}\Z', re.I)
_POSTING = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z', re.I)
_INSTANCES = {'global': ('api.lever.co', 'jobs.lever.co'),
              'eu': ('api.eu.lever.co', 'jobs.eu.lever.co')}
PAGE_SIZE = 100
MAX_PAGES = 10
MAX_INVENTORY_SECONDS = 120


def tenant_identity(value):
    if not isinstance(value, str):
        raise ValueError('SOURCE_INVALID_TENANT')
    parts = value.casefold().split(':')
    if len(parts) != 2 or parts[0] not in _INSTANCES or not _SITE.fullmatch(parts[1]):
        raise ValueError('SOURCE_INVALID_TENANT')
    return ':'.join(parts)


def endpoint(tenant):
    instance, site = tenant_identity(tenant).split(':')
    return f'https://{_INSTANCES[instance][0]}/v0/postings/{site}'


def listing_url(tenant, url, *, identifier=None):
    """Return the canonical listing only for the exact regional site and UUID."""
    instance, site = tenant_identity(tenant).split(':')
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parsed = urlsplit(url)
        parts = parsed.path.split('/')
        if (parsed.scheme != 'https' or parsed.hostname != _INSTANCES[instance][1]
                or parsed.username is not None or parsed.password is not None
                or parsed.port is not None or parsed.fragment
                or len(parts) not in (3,4) or parts[:2] != ['',site]
                or (len(parts)==4 and parts[3]!='apply') or not _POSTING.fullmatch(parts[2])
                or (identifier is not None and parts[2].casefold()!=identifier)):
            return None
    except ValueError:
        return None
    return f'https://{_INSTANCES[instance][1]}/{site}/{parts[2].casefold()}'


class Lever(Greenhouse):
    """Reuse the existing bounded/cancellable transport and source policy."""
    def __init__(self, tenant, **kwargs):
        key = tenant_identity(tenant)
        self.instance, self.site = key.split(':')
        super().__init__(self.site, **kwargs)
        self.tenant = key

    def _request_url(self, suffix):
        return endpoint(self.tenant) + suffix

    def search(self):
        parent_check = self.check
        deadline = time.monotonic() + MAX_INVENTORY_SECONDS

        def bounded_check():
            parent_check()
            if time.monotonic() >= deadline:
                raise SourceError('SOURCE_TIME_LIMIT')

        def remaining_seconds():
            bounded_check()
            remaining = deadline - time.monotonic()
            parent_remaining = getattr(parent_check, 'remaining_seconds', None)
            return min(remaining, parent_remaining()) if callable(parent_remaining) else remaining

        bounded_check.remaining_seconds = remaining_seconds
        self.check = bounded_check
        try:
            return self._search_inventory()
        finally:
            self.check = parent_check

    def _search_inventory(self):
        self._reset_request_status()
        items, seen = [], set()
        company = SourceCompany(('lever',self.tenant), self.site,
                                f'https://{_INSTANCES[self.instance][1]}/{self.site}')
        for page in range(MAX_PAGES):
            self.check()
            payload = self._get('', {'mode':'json','skip':page*PAGE_SIZE,'limit':PAGE_SIZE})
            if not isinstance(payload,list) or len(payload)>PAGE_SIZE:
                raise SourceError('SOURCE_INVALID_FORMAT')
            for raw in payload:
                normalized = self.normalize(raw)
                identifier = normalized['requisition']
                if identifier in seen:
                    raise SourceError('SOURCE_DUPLICATE_JOB_ID')
                seen.add(identifier)
                items.append(SourceItem(raw,company))
            self.check()
            if len(payload)<PAGE_SIZE:
                return SearchBatch(endpoint(self.tenant),tuple(items),company,complete_inventory=True)
        # A full last page does not prove exhaustion. Never reconcile removals
        # or publish a partial inventory after the item/page budget is exhausted.
        raise SourceError('SOURCE_JOB_LIMIT')

    def fetch(self, job_id):
        if not isinstance(job_id,str) or not _POSTING.fullmatch(job_id):
            raise ValueError('SOURCE_INVALID_JOB_ID')
        self._reset_request_status()
        raw = self._get('/'+job_id.casefold(), {'mode':'json'})
        if self.normalize(raw)['requisition'] != job_id.casefold():
            raise SourceError('SOURCE_JOB_ID_MISMATCH')
        return raw

    def normalize(self, raw):
        if (not isinstance(raw,dict) or not isinstance(raw.get('id'),str)
                or not _POSTING.fullmatch(raw['id']) or not isinstance(raw.get('text'),str)
                or not raw['text'].strip() or not isinstance(raw.get('categories',{}),dict)):
            raise SourceError('SOURCE_INVALID_JOB')
        identifier = raw['id'].casefold()
        url = listing_url(self.tenant,raw.get('hostedUrl'),identifier=identifier)
        if not url or (raw.get('applyUrl') is not None and
                       not listing_url(self.tenant,raw['applyUrl'],identifier=identifier)):
            raise SourceError('COMPANY_ATS_JOB_TENANT_MISMATCH')
        title = plain_text(raw['text'])[:500]
        sections = [raw.get('description') or raw.get('descriptionPlain') or '']
        lists = raw.get('lists',[])
        if not isinstance(lists,list) or any(not isinstance(row,dict) for row in lists):
            raise SourceError('SOURCE_INVALID_JOB')
        sections += [str(row.get('text') or '')+' '+str(row.get('content') or '') for row in lists[:50]]
        sections.append(raw.get('additional') or raw.get('additionalPlain') or '')
        description = structured_text(' '.join(str(part)[:500_000] for part in sections))[:50000]
        categories = raw.get('categories',{})
        location = plain_text(categories.get('location') or 'Unknown')[:1000]
        facts = classify_location(location,description)
        workplace = str(raw.get('workplaceType') or '').casefold()
        if workplace in {'remote','hybrid','on-site'}:
            facts['remote_type'] = {'remote':'REMOTE','hybrid':'HYBRID','on-site':'ONSITE'}[workplace]
        combined = title+' '+description
        skills = extract_skills(combined)
        experience = normalize_experience_level(title)
        titles = normalize_title(title)
        education = parse_education_requirement(description)
        requirements = parse_job_requirements(description)
        salary = parse_salary(description,'EMPLOYER_PROVIDED')
        pay = raw.get('salaryRange')
        if isinstance(pay,dict):
            low,high = pay.get('min'),pay.get('max')
            if (all(type(v) in (int,float) and math.isfinite(v) and 0<=v<1e9 for v in (low,high))
                    and low<=high):
                salary.update(salary_min=low,salary_max=high,
                    salary_period={'per-year':'YEAR','per-hour':'HOUR','year':'YEAR','hour':'HOUR'}.get(pay.get('interval'),'UNKNOWN'),
                    currency=str(pay.get('currency') or 'Unknown')[:8],salary_raw=pay)
        attrs = dict(provider='lever',tenant=self.tenant,lever_site=self.site,lever_instance=self.instance,
            native_posting_id=identifier,native_job_id=identifier,lifecycle_claim='ACTIVE',
            employment_type=normalize_employment_type(str(categories.get('commitment') or combined)),
            experience_level=experience['level'],experience_level_raw=experience['raw'],
            normalized_title=titles['normalized_title'],title_family=titles['title_family'],
            skill_ids=skills['ids'],skill_mentions=skills['mentions'],skill_evidence='description_mentions',
            education_requirement=education['classification'],education_requirement_raw=education['raw'],
            experience_requirements=requirements['experience'],certifications=requirements['certifications'],
            clearance=requirements['clearance'],work_authorization=requirements['work_authorization'],
            negative_conditions=parse_negative_conditions(description,requirements['clearance']),
            salary_min=salary['salary_min'],salary_max=salary['salary_max'],salary_period=salary['salary_period'],
            salary_currency=salary['currency'],salary_source=salary['salary_source'],salary_raw=salary['salary_raw'],
            reported_posted_at=None,remote_regions=facts['remote_regions'],remote_indicated=facts['remote_indicated'],
            country=facts['country'],state=facts['state'],city=facts['city'],
            submission_capability='MANUAL_ONLY',fictional=False,application_url_status='PROVIDER_SUPPLIED')
        return dict(requisition=identifier,title=title,description=description,skills=skills['labels'],
                    location=location,remote_type=facts['remote_type'],application_url=url,attributes=attrs)
