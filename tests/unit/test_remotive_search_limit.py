"""MJ-REQ-017.S08: caller result limits survive excess upstream responses."""
import httpx
import pytest

from services.discovery.sources.remotive import Remotive


def job(identifier):
    return {
        "id": identifier,
        "title": f"Synthetic Engineer {identifier}",
        "company_name": f"Synthetic Company {identifier}",
        "url": f"https://remotive.com/remote-jobs/software-dev/synthetic-{identifier}",
    }


def adapter_with_jobs(jobs):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"jobs": jobs})

    return Remotive(transport=httpx.MockTransport(respond)), requests


@pytest.mark.parametrize("limit", [1, 3])
def test_excess_upstream_results_are_capped_without_changing_order_or_query(limit):
    jobs = [job(identifier) for identifier in range(1, 13)]
    adapter, requests = adapter_with_jobs(jobs)

    batch = adapter.search(category="software-dev", search_term="python", limit=limit)

    assert [item.raw for item in batch.items] == jobs[:limit]
    assert [item.company.name for item in batch.items] == [row["company_name"] for row in jobs[:limit]]
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.host == "remotive.com"
    assert requests[0].url.path == "/api/remote-jobs"
    assert dict(requests[0].url.params) == {
        "category": "software-dev", "search": "python", "limit": str(limit),
    }
    assert batch.endpoint == "https://remotive.com/api/remote-jobs"
    assert batch.source_company is None
    assert batch.complete_inventory is False
    assert adapter.health() == "HEALTHY"


def test_non_object_rows_do_not_consume_the_result_limit():
    jobs = [job(identifier) for identifier in range(1, 5)]
    payload = [None, "malformed", 7, True, [], jobs[0], None, jobs[1], [], *jobs[2:]]
    adapter, requests = adapter_with_jobs(payload)

    batch = adapter.search(limit=3)

    assert [item.raw for item in batch.items] == jobs[:3]
    assert len(requests) == 1
    assert batch.complete_inventory is False


@pytest.mark.parametrize("count", [0, 2])
def test_fewer_results_than_requested_remain_unchanged(count):
    jobs = [job(identifier) for identifier in range(1, count + 1)]
    adapter, requests = adapter_with_jobs(jobs)

    batch = adapter.search(limit=3)

    assert [item.raw for item in batch.items] == jobs
    assert len(requests) == 1
    assert batch.complete_inventory is False


def test_unlimited_search_preserves_all_results_and_omits_limit_parameter():
    jobs = [job(identifier) for identifier in range(1, 13)]
    adapter, requests = adapter_with_jobs(jobs)

    batch = adapter.search(category="software-dev", search_term="python", limit=None)

    assert [item.raw for item in batch.items] == jobs
    assert len(requests) == 1
    assert dict(requests[0].url.params) == {"category": "software-dev", "search": "python"}
    assert batch.complete_inventory is False


@pytest.mark.parametrize("limit", [0, -1, True, False, 3.0, "3", [], {}],
                         ids=["zero", "negative", "true", "false", "float", "string", "list", "mapping"])
def test_invalid_limits_are_rejected_before_io(limit):
    adapter, requests = adapter_with_jobs([job(1)])

    with pytest.raises(ValueError, match="^SOURCE_INVALID_LIMIT$"):
        adapter.search(limit=limit)

    assert requests == []
