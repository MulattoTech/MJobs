"""MJ-REQ-017.S08: malformed source IDs are posting failures, not batch failures."""
import sys
import traceback

import pytest

from services.discovery.sources.errors import SourceError
from services.discovery.sources.remotive import Remotive


@pytest.mark.parametrize("native_id", ["\u00b2", "\u2460", "123\u00b2"],
                         ids=["superscript", "circled", "mixed-digits"])
def test_nondecimal_digits_are_rejected_with_safe_source_error(native_id):
    raw = {"id": native_id, "title": "Synthetic Engineer"}
    with pytest.raises(SourceError) as caught:
        Remotive().normalize(raw)
    assert caught.value.code == "SOURCE_INVALID_JOB"
    assert str(caught.value) == "SOURCE_INVALID_JOB"
    assert native_id not in "".join(traceback.format_exception(caught.value))
    assert raw["id"] == native_id


@pytest.fixture
def bounded_integer_conversion():
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(640)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(previous)


@pytest.mark.parametrize("native_id", ["9" * 641, 10 ** 640],
                         ids=["oversized-numeric-string", "oversized-integer"])
def test_integer_conversion_limits_are_classified(native_id, bounded_integer_conversion):
    raw = {"id": native_id, "title": "Synthetic Engineer"}
    with pytest.raises(SourceError) as caught:
        Remotive().normalize(raw)
    assert caught.value.code == "SOURCE_INVALID_JOB"
    assert str(caught.value) == "SOURCE_INVALID_JOB"
    assert "Exceeds the limit" not in "".join(traceback.format_exception(caught.value))
    assert raw["id"] == native_id


def test_numeric_conversion_boundary_remains_valid(bounded_integer_conversion):
    native_id = "9" * 640
    normalized = Remotive().normalize({"id": native_id, "title": "Synthetic Engineer"})
    assert normalized["requisition"] == native_id
    assert normalized["attributes"]["native_posting_id"] == int(native_id)


@pytest.mark.parametrize(("native_id", "expected"), [
    (1001, 1001),
    ("1001", 1001),
    ("001001", 1001),
    ("\u0661\u0660\u0660\u0661", 1001),
    ("opaque-posting-id", "opaque-posting-id"),
], ids=["integer", "numeric-string", "leading-zero", "decimal-unicode", "opaque"])
def test_supported_id_identity_and_raw_evidence_are_preserved(native_id, expected):
    raw = {"id": native_id, "title": "Synthetic Engineer"}
    normalized = Remotive().normalize(raw)
    assert normalized["requisition"] == str(expected)
    assert normalized["attributes"]["native_posting_id"] == expected
    assert normalized["attributes"]["native_job_id"] == expected
    assert raw["id"] == native_id


@pytest.mark.parametrize("native_id", [True, False, None, "", "  ", [], {}, 1.5])
def test_invalid_id_shapes_remain_rejected(native_id):
    with pytest.raises(SourceError, match="^SOURCE_INVALID_JOB$"):
        Remotive().normalize({"id": native_id, "title": "Synthetic Engineer"})
