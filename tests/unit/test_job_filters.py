from services.discovery.search_semantics import filter_jobs


def _job(title, *, remote_type="REMOTE_SPECIFIC_STATES", regions=None, salary_min=None, salary_max=None):
    return {
        "id": title.lower().replace(" ", "-"),
        "title": title,
        "company": "Example Systems",
        "description": "",
        "skills": [],
        "location": "Remote",
        "remote_type": remote_type,
        "attributes": {
            "remote_indicated": remote_type.startswith("REMOTE"),
            "remote_regions": regions or [],
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_period": "YEAR" if salary_min is not None or salary_max is not None else "UNKNOWN",
        },
    }


def test_title_filter_keeps_only_requested_job_and_attaches_search_evidence():
    rows = [_job("Systems Administrator"), _job("Sales Director")]

    result = filter_jobs(rows, {"include_titles": ["Systems Administrator"]})

    assert [row["title"] for row in result] == ["Systems Administrator"]
    assert result[0]["search_evidence"]["matched"] is True


def test_remote_filter_enforces_location_restrictions():
    rows = [
        _job("Texas Support Engineer", regions=["TX"]),
        _job("California Support Engineer", regions=["CA"]),
        _job("Onsite Support Engineer", remote_type="ONSITE", regions=["TX"]),
    ]

    result = filter_jobs(rows, {"remote": "REQUIRED", "remote_regions": ["TX"]})

    assert [row["title"] for row in result] == ["Texas Support Engineer"]


def test_salary_filter_excludes_unknown_and_below_minimum_when_requested():
    rows = [
        _job("Known Salary Engineer", salary_min=120000, salary_max=150000),
        _job("Low Salary Engineer", salary_min=70000, salary_max=90000),
        _job("Unknown Salary Engineer"),
    ]

    result = filter_jobs(rows, {
        "salary_min": 100000,
        "salary_period": "YEAR",
        "salary_known_only": True,
        "salary_unknown_allowed": False,
    })

    assert [row["title"] for row in result] == ["Known Salary Engineer"]
