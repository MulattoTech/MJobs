"""Bounded local mock discovery flywheel and strong route deduplication."""
from urllib.parse import urljoin, urlsplit
import os

from bs4 import BeautifulSoup
import httpx

from packages.domain.safety import Fact, select_verified_facts
from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert


class MockClient:
    def __init__(self, origin, check=None):
        parsed = urlsplit(origin)
        allowed_hosts = ("127.0.0.1", "localhost") + (("mock",) if os.environ.get("MJOBS_COMPOSE_MOCK") == "1" else ())
        if (parsed.scheme != "http" or parsed.hostname not in allowed_hosts
                or parsed.username or parsed.password or parsed.path not in ("", "/")):
            raise ValueError("Mock origin must be an explicit local fixture server")
        self.origin = origin.rstrip("/")
        self.client = httpx.Client(timeout=10, follow_redirects=False, trust_env=False)
        self.check = check or (lambda: None)

    def allowed(self, url):
        parsed = urlsplit(url)
        return (parsed.scheme, parsed.netloc) == (urlsplit(self.origin).scheme, urlsplit(self.origin).netloc) and not parsed.username and not parsed.password

    def get(self, url):
        for _ in range(4):
            self.check()
            if not self.allowed(url):
                raise ValueError("Untrusted external redirect blocked")
            response = self.client.get(url)
            if response.is_redirect:
                url = urljoin(url, response.headers["location"])
                continue
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise ValueError("Source response exceeds limit")
            return response
        raise ValueError("Too many source redirects")

    def close(self):
        self.client.close()


def discover(store, mock_origin, profile_id, check=None, profile_version=None):
    profile = store.profile(profile_id, profile_version)
    usable = []
    for row in profile["facts"]:
        fact = Fact(id=row["fact_id"], **{key: row[key] for key in ("text", "source", "verified", "user_approved", "confidence")})
        try:
            select_verified_facts([fact], [fact.id])
        except ValueError:
            continue
        if row["kind"] == "skill":
            usable.append(fact.text.casefold())
    client = MockClient(mock_origin, check=check)
    try:
        aggregator = client.get(mock_origin + "/board/jobs").json()["jobs"]
        sources = {}
        all_rows = [("mock-board", row) for row in aggregator]
        for row in aggregator:
            homepage = client.get(row["company_website"])
            link = BeautifulSoup(homepage.text, "html.parser").find("a", attrs={"rel": "careers"})
            if link is None:
                raise ValueError("No proven employer careers link")
            careers = client.get(urljoin(str(homepage.url), link["href"]))
            board = BeautifulSoup(careers.text, "html.parser").find("a", attrs={"data-provider": "mock-ats"})
            if board is None:
                raise ValueError("Unsupported ATS requires review")
            endpoint = urljoin(str(careers.url), board["href"])
            tenant = board["data-tenant"]
            company_id = identity(row["company_website"])
            sources[company_id] = {"company": row["company"], "domain": row["company_website"], "tenant": tenant, "endpoint": endpoint}
            if not any(source == endpoint for source, _ in all_rows):
                all_rows.extend((endpoint, employer) for employer in client.get(endpoint).json()["jobs"])
        with store.engine.begin() as connection:
            client.check()
            for company_id, source in sources.items():
                upsert(connection, m.companies, {"id": company_id, "name": source["company"], "domain": source["domain"]})
                upsert(connection, m.company_sources, {"id": identity(company_id, "mock-ats", source["tenant"]), "company_id": company_id,
                    "provider": "mock-ats", "tenant": source["tenant"], "endpoint": source["endpoint"], "discovered_automatically": True})
            for source, row in all_rows:
                company_id = identity(row["company_website"])
                if not client.allowed(row["application_url"]):
                    raise ValueError("External mock application URL blocked")
                job_id = identity(company_id, "mock-ats", row["tenant"], row["requisition"])
                job = {"id": job_id, "company_id": company_id, "requisition": row["requisition"], "title": row["title"].strip(),
                       "description": BeautifulSoup(row["description"], "html.parser").get_text(" ", strip=True),
                       "skills": row["skills"], "location": row["location"], "remote_type": row["remote_type"]}
                upsert(connection, m.jobs, job, replace=source != "mock-board")
                upsert(connection, m.routes, {"id": job_id, "job_id": job_id, "company_id": company_id, "provider": "mock-ats",
                    "tenant": row["tenant"], "requisition": row["requisition"], "url": row["application_url"]}, replace=source != "mock-board")
                upsert(connection, m.postings, {"id": identity(source, row["source_job_id"]), "job_id": job_id,
                    "source": source, "source_job_id": row["source_job_id"], "raw": row, "fetched_at": now()}, replace=True)
                required = {skill.casefold() for skill in row["skills"]}
                missing = sorted(required - set(usable))
                score = round(100 * len(required & set(usable)) / len(required), 2) if required else 0
                upsert(connection, m.matches, {"id": identity(job_id, profile_id, profile["version"]), "job_id": job_id,
                    "profile_id": profile_id, "profile_version": profile["version"], "score": score, "missing": missing,
                    "components": {"required_skill_coverage": score}}, replace=True)
            store._audit(connection, profile_id, "MOCK_DISCOVERY_COMPLETE", {"sources": len(sources), "raw_postings": len(all_rows)})
        return store.jobs(profile_id)
    finally:
        client.close()
