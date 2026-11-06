"""Immutable reviewed-input contract for owned Greenhouse/Lever fixtures.

This contract is a fixture admission prerequisite, not submission authorization.
The caller owns the isolated browser context, blocks egress before navigation,
and serves only its trusted fixture. ``template`` is the exact UTF-8 serialized
DOM at review time (``page.content()``); execution must compare that DOM again.
Only identities and digests belong in durable state. Values remain transient.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from services.applications.adapters.greenhouse import GreenhouseApplicationAdapter
from services.applications.adapters.lever import LeverApplicationAdapter
from services.applications.form_analysis import analyze_controls
from services.applications.upload_integrity import UploadSnapshot, snapshot_bytes
from workers.operations import (
    OperationCancelled,
    OperationControlUnavailable,
    OperationTimedOut,
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_FIELD_KEYS = frozenset({"index", "tag", "labels", "name", "id", "placeholder", "aria_label",
                       "input_type", "nearby_text", "section_heading", "fieldset_legend",
                       "options", "ambiguous", "sensitivity"})
_DERIVED_KEYS = frozenset({"index", "ambiguous", "sensitivity"})
_MAX_BYTES = 1_000_000


class OwnedATSFixtureError(ValueError):
    """Opaque fixed error codes; never include page or profile content."""


@dataclass(frozen=True)
class DocumentBinding:
    kind: str
    document_id: str
    filename_sha256: str
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ReviewedFieldBinding:
    field_key: str
    field_sha256: str
    answer_id: str
    answer_sha256: str


@dataclass(frozen=True)
class OwnedATSFixtureContract:
    application_id: str
    provider: str
    route_url: str
    route_sha256: str
    origin: str
    page_url: str
    template_sha256: str
    analysis_sha256: str
    profile_id: str
    profile_version: int
    profile_sha256: str
    document_version: int
    documents: tuple[DocumentBinding, ...]
    reviewed_fields: tuple[ReviewedFieldBinding, ...]


def _fail(code):
    raise OwnedATSFixtureError(code)


def _identifier(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        _fail("OWNED_ATS_IDENTITY_INVALID")
    return value


def _version(value):
    if type(value) is not int or value < 1:
        _fail("OWNED_ATS_VERSION_INVALID")
    return value


def _digest(value):
    try:
        content = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        _fail("OWNED_ATS_METADATA_INVALID")
    if len(content) > _MAX_BYTES:
        _fail("OWNED_ATS_METADATA_INVALID")
    return hashlib.sha256(content).hexdigest()


def _template_digest(template):
    if isinstance(template, str):
        try:
            template = template.encode("utf-8")
        except UnicodeError:
            _fail("OWNED_ATS_TEMPLATE_INVALID")
    if not isinstance(template, bytes) or not 0 < len(template) <= _MAX_BYTES:
        _fail("OWNED_ATS_TEMPLATE_INVALID")
    return hashlib.sha256(template).hexdigest()


def _origin(origin, page_url):
    try:
        if (not isinstance(origin, str) or origin != origin.strip()
                or any(ord(char) < 33 for char in origin) or "\\" in origin):
            raise ValueError
        parsed = urlsplit(origin)
        address = ipaddress.ip_address(parsed.hostname or "")
        if (parsed.scheme != "http" or not address.is_loopback
                or parsed.port is None or parsed.port < 1
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment
                or origin != f"http://{parsed.netloc}"):
            raise ValueError
        if (not isinstance(page_url, str) or not page_url.startswith(origin + "/")
                or page_url != page_url.strip() or len(page_url) > 2048
                or any(ord(char) < 33 for char in page_url) or "\\" in page_url):
            raise ValueError
        page = urlsplit(page_url)
        if (page.scheme != parsed.scheme or page.netloc != parsed.netloc
                or page.fragment or page.query or not page.path):
            raise ValueError
    except (TypeError, ValueError):
        _fail("OWNED_ATS_ORIGIN_INVALID")


def _adapter(provider):
    if provider == "greenhouse":
        return GreenhouseApplicationAdapter()
    if provider == "lever":
        return LeverApplicationAdapter()
    _fail("OWNED_ATS_PROVIDER_UNSUPPORTED")


def _fields(analysis):
    if (not isinstance(analysis, dict) or set(analysis) != {"status", "reason", "fields"}
            or analysis.get("status") != "PASS" or analysis.get("reason") is not None
            or not isinstance(analysis.get("fields"), list) or not 1 <= len(analysis["fields"]) <= 100):
        _fail("OWNED_ATS_FORM_REVIEW_REQUIRED")
    controls = []
    for field in analysis["fields"]:
        if not isinstance(field, dict) or set(field) != _FIELD_KEYS:
            _fail("OWNED_ATS_FORM_REVIEW_REQUIRED")
        controls.append({key: value for key, value in field.items() if key not in _DERIVED_KEYS})
    # Re-derive sensitivity/ambiguity; a supplied PASS cannot override metadata.
    if analyze_controls(controls) != analysis:
        _fail("OWNED_ATS_FORM_REVIEW_REQUIRED")
    fields = {}
    for field in analysis["fields"]:
        key = field.get("name") or field.get("id")
        _identifier(key)
        if key in fields or field["sensitivity"] != "STANDARD" or field["ambiguous"] is not False:
            _fail("OWNED_ATS_FORM_REVIEW_REQUIRED")
        fields[key] = field
    return fields


def _review_bindings(fields, profile, reviewed_fields):
    if not isinstance(reviewed_fields, (list, tuple)) or len(reviewed_fields) > len(fields):
        _fail("OWNED_ATS_FIELD_REVIEW_REQUIRED")
    answers = profile.get("answers", [])
    if not isinstance(answers, list):
        _fail("OWNED_ATS_FIELD_REVIEW_REQUIRED")
    bindings = {}
    for review in reviewed_fields:
        if not isinstance(review, dict) or set(review) != {"field_key", "answer_id"}:
            _fail("OWNED_ATS_FIELD_REVIEW_REQUIRED")
        key, answer_id = review["field_key"], review["answer_id"]
        _identifier(key)
        _identifier(answer_id)
        if key not in fields or key in bindings:
            _fail("OWNED_ATS_FIELD_REVIEW_REQUIRED")
        field = fields[key]
        candidates = [row for row in answers if isinstance(row, dict) and row.get("id") == answer_id]
        if len(candidates) != 1:
            _fail("OWNED_ATS_FIELD_REVIEW_REQUIRED")
        answer = candidates[0]
        prompts = field["labels"] or [field["aria_label"] or field["placeholder"] or key]
        question = answer.get("question")
        if (answer.get("verified") is not True or answer.get("user_approved") is not True
                or not isinstance(answer.get("text"), str) or not answer["text"].strip()
                or not isinstance(question, str)
                or any(" ".join(prompt.split()) != " ".join(question.split()) for prompt in prompts)
                or (field["input_type"] or "").casefold() == "file"):
            _fail("OWNED_ATS_FIELD_REVIEW_REQUIRED")
        bindings[key] = ReviewedFieldBinding(key, _digest(field), answer_id, _digest(answer))
    # Apply the stricter existing contact-prompt policy to both fixture providers.
    policy = LeverApplicationAdapter()
    for key, field in fields.items():
        if policy.requires_human_review({"status": "PASS", "fields": [field]}) and key not in bindings:
            _fail("OWNED_ATS_FIELD_REVIEW_REQUIRED")
    return tuple(bindings[key] for key in sorted(bindings))


def freeze_fixture(*, workspace_mode, application_id, provider, route, origin, page_url,
                   template, analysis, profile, document_version, documents, reviewed_fields=()):
    """Freeze explicitly reviewed fixture inputs; perform no browser/network I/O.

    ``reviewed_fields`` maps exact field keys to approved profile answer IDs.
    It proves a binding only: existing adapter review restrictions still apply.
    The integration owner must persist this result at deliberate review, then
    supply fresh authoritative inputs to ``validate_fixture`` at execution.
    """
    if workspace_mode != "FIXTURE":
        _fail("OWNED_ATS_FIXTURE_MODE_REQUIRED")
    adapter = _adapter(provider)
    normalized = adapter.normalize_route(route)
    if normalized is None:
        _fail("OWNED_ATS_ROUTE_UNSUPPORTED")
    _origin(origin, page_url)
    if not isinstance(profile, dict):
        _fail("OWNED_ATS_PROFILE_INVALID")
    fields = _fields(analysis)
    if not isinstance(documents, (list, tuple)) or len(documents) != 2:
        _fail("OWNED_ATS_DOCUMENTS_INVALID")
    bound_documents = {}
    for item in documents:
        if (not isinstance(item, UploadSnapshot) or item.kind not in {"resume", "cover-letter"}
                or item.kind in bound_documents):
            _fail("OWNED_ATS_DOCUMENTS_INVALID")
        try:
            verified = snapshot_bytes(item.kind, item.document_id, item.filename, item.media_type,
                                      item.content, item.sha256)
        except ValueError:
            _fail("OWNED_ATS_DOCUMENTS_INVALID")
        bound_documents[item.kind] = DocumentBinding(verified.kind, verified.document_id,
            _digest(verified.filename), verified.media_type, len(verified.content), verified.sha256)
    return OwnedATSFixtureContract(
        _identifier(application_id), provider, normalized["url"], _digest(route), origin, page_url,
        _template_digest(template), _digest(analysis), _identifier(profile.get("id")),
        _version(profile.get("version")), _digest(profile), _version(document_version),
        tuple(bound_documents[kind] for kind in ("resume", "cover-letter")),
        _review_bindings(fields, profile, reviewed_fields))


def validate_fixture(contract, **current):
    """Reject changed inputs; accept a frozen contract or its JSON ``asdict`` payload.

    The return value is always a newly validated immutable contract, allowing
    a worker to consume a durable review without reconstructing trust from the
    current inputs. Compare canonical JSON to distinguish booleans and numbers.
    """
    if not isinstance(contract, (OwnedATSFixtureContract, dict)):
        _fail("OWNED_ATS_REVIEW_STALE")
    try:
        actual = freeze_fixture(**current)
        expected = asdict(contract) if isinstance(contract, OwnedATSFixtureContract) else contract
        matches = _digest(asdict(actual)) == _digest(expected)
    except (OwnedATSFixtureError, TypeError):
        _fail("OWNED_ATS_REVIEW_STALE")
    if not matches:
        _fail("OWNED_ATS_REVIEW_STALE")
    return actual


def upload_fixture_documents(page, contract, *, current, check=None):
    """Select exact prepared bytes using the real adapter on an already owned page.

    ``current`` contains freshly loaded arguments for ``freeze_fixture``. The
    caller must block external traffic before loading the trusted page; this
    helper neither navigates, fills answers, clicks nor submits. It checks the
    live DOM, origin and page/frame isolation at every adapter checkpoint.
    A reviewed custom binding never overrides the adapter's own review policy.
    """
    if not isinstance(current, dict):
        _fail("OWNED_ATS_REVIEW_STALE")
    contract = validate_fixture(contract, **current)
    adapter = _adapter(contract.provider)

    def guard():
        if check is not None:
            check()
        validate_fixture(contract, **current)
        if (page.url != contract.page_url or page.context.pages != [page]
                or page.frames != [page.main_frame]
                or _template_digest(page.content()) != contract.template_sha256):
            _fail("OWNED_ATS_REVIEW_STALE")

    remaining = getattr(check, "remaining_seconds", None)
    if callable(remaining):
        guard.remaining_seconds = remaining
    try:
        guard()
        analysis = adapter.analyze_page(page, check=guard)
        if _digest(analysis) != contract.analysis_sha256:
            _fail("OWNED_ATS_REVIEW_STALE")
        validation = adapter.validate_page(page, analysis, check=guard)
        if validation.get("status") != "PASS":
            _fail("OWNED_ATS_ADAPTER_REVIEW_REQUIRED")
        documents = tuple(current["documents"])
        manifest = adapter.upload_mock_fixture_documents(page, analysis, documents, check=guard)
        guard()
        timeout = {"timeout": max(1, min(5000, int(remaining() * 1000)))} if callable(remaining) else {}
        if page.locator("form").evaluate("form => form.checkValidity()", **timeout) is not True:
            _fail("OWNED_ATS_ADAPTER_REVIEW_REQUIRED")
        guard()
    except (OwnedATSFixtureError, OperationCancelled, OperationControlUnavailable, OperationTimedOut):
        raise
    except ValueError:
        _fail("OWNED_ATS_ADAPTER_REVIEW_REQUIRED")
    except Exception:  # noqa: BLE001 - Browser errors can contain applicant values.
        if check is not None:
            check()
        _fail("OWNED_ATS_BROWSER_UNAVAILABLE")
    return {"context": "OWNED_ATS_FIXTURE", "provider": contract.provider,
            "template_sha256": contract.template_sha256, "verification": "BROWSER_SELECTED_BYTES",
            "documents": manifest, "external_submission_enabled": False}
