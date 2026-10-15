"""MJ-REQ-018: untrusted provider payloads cannot widen Remotive's origin."""
import pytest

from services.discovery.sources.contract import SourceCapability
from services.discovery.sources.remotive import Remotive


UNSAFE_URLS = (
    "https://remotive.com.attacker.invalid/jobs/1",
    "https://notremotive.com/jobs/1",
    "https://attacker-remotive.com/jobs/1",
    "https://remotive.com@attacker.invalid/jobs/1",
    "https://attacker.invalid@remotive.com/jobs/1",
    "https://@remotive.com/jobs/1",
    "https://user:secret@remotive.com/jobs/1",
    "https:///jobs/1",
    "https://",
    "http://remotive.com/jobs/1",
    "//remotive.com/jobs/1",
    "javascript:alert(1)",
    "https://[remotive.com/jobs/1",
    "https://remotive.com:invalid/jobs/1",
    "https://remotive.com:65536/jobs/1",
    "https://remotive.com:444/jobs/1",
    "https://remotive.com\\@attacker.invalid/jobs/1",
    "https://remotive.com/\\attacker.invalid",
    "https://remo\ntive.com/jobs/1",
    " https://remotive.com/jobs/1",
    "https://remotive.com/jobs/1\x00",
    "https://jobs.remotive.com/jobs/1",
    "https://remotive.com./jobs/1",
    "", None, 123, {"url": "https://remotive.com/jobs/1"},
)


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_untrusted_url_is_unavailable_without_losing_job_or_raw_evidence(url):
    raw = {"id": 1, "title": "Python Engineer", "url": url}
    normalized = Remotive().normalize(raw)
    assert normalized["application_url"] == ""
    assert normalized["attributes"]["application_url_status"] == "UNAVAILABLE"
    assert normalized["attributes"]["submission_capability"] == "MANUAL_ONLY"
    assert normalized["title"] == "Python Engineer"
    assert raw["url"] == url


@pytest.mark.parametrize("url", (
    "https://remotive.com/remote-jobs/software-dev/engineer-1",
    "https://remotive.com/remote-jobs/engineer-1?source=api#apply",
    "https://REMOTIVE.COM/remote-jobs/engineer-1",
    "https://remotive.com:443/remote-jobs/engineer-1",
))
def test_canonical_https_urls_remain_provider_supplied_and_manual_only(url):
    adapter = Remotive()
    normalized = adapter.normalize({"id": 1, "title": "Python Engineer", "url": url})
    assert normalized["application_url"] == url
    assert normalized["attributes"]["application_url_status"] == "PROVIDER_SUPPLIED"
    assert normalized["attributes"]["submission_capability"] == "MANUAL_ONLY"
    assert not adapter.capabilities().supports(SourceCapability.DIRECT_APPLICATION)
    assert not adapter.capabilities().supports(SourceCapability.APPLICATION_STATUS)
