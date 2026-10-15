"""Snapshots of the public definitions used by deterministic output producers.

The digest identifies an implementation manifest, not a rendered prompt or a
promise of executable replay. No applicant inputs belong in this manifest.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
import re
import textwrap

from services.ai.prompt_versioning import PROMPT_TARGETS, PromptVersion


_ROOT = Path(__file__).resolve().parents[2]
_MAX_BYTES = 131072
_DIGEST = re.compile(r"[0-9a-f]{64}")
# Schema 1 producer/helper lists are a frozen readback contract. Future schemas
# must retain this validator so a new helper cannot invalidate stored revisions.
_DOCUMENT_HELPERS = {
    "resume": (
        "services.documents.generate", "services.documents.templates",
        "services.documents.tailor", "services.documents.page_fit",
        "services.career_profile.story_selection", "services.discovery.skill_taxonomy",
    ),
    "cover_letter": (
        "services.documents.generate", "services.documents.cover_letter",
        "services.career_profile.story_selection",
    ),
    "screening_answers": ("services.documents.answers",),
}
_PARSERS = {
    "services.discovery.sources.greenhouse.Greenhouse",
    "services.discovery.sources.lever.Lever",
    "services.discovery.sources.remotive.Remotive",
    "services.discovery.sources.remoteok.RemoteOK",
    "services.discovery.sources.wwr.WeWorkRemotely",
}
_PARSER_HELPERS = (
    "services.discovery.sources.structured_text", "services.discovery.search_semantics",
    "services.discovery.skill_taxonomy", "services.discovery.requirement_parsers",
    "services.discovery.negative_filters",
)
_ENTRYPOINTS = {
    "resume": ("services.documents.templates.resume_layout",
               "services.documents.templates.contact_header"),
    "cover_letter": ("services.documents.cover_letter.build_cover_letter",),
    "screening_answers": ("services.documents.answers.export_answers",),
}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _strings(value):
    return (isinstance(value, list) and len(value) <= 256
            and all(isinstance(item, str) and 0 < len(item) <= 200 for item in value))


def _string_table(value):
    return (isinstance(value, dict) and len(value) <= 256
            and all(isinstance(key, str) and 0 < len(key) <= 200
                    and isinstance(item, str) and 0 < len(item) <= 1000
                    for key, item in value.items()))


def _validate_constants(target, definitions):
    if target == "resume":
        constants = definitions[0]["constants"]
        contract = constants["contract"]
        valid = (isinstance(contract, list) and len(contract) == 3
                 and isinstance(contract[0], str) and _IDENTIFIER.fullmatch(contract[0])
                 and contract[1] in {"BASE", "TAILORED"} and type(contract[2]) is bool
                 and _string_table(constants["HEADINGS"])
                 and _strings(definitions[1]["constants"]["CONTACT_TEMPLATES"]))
    elif target == "cover_letter":
        constants = definitions[0]["constants"]
        limits = constants["LIMITS"]
        valid = (_strings(constants["STYLES"]) and _strings(constants["LENGTHS"])
                 and _string_table(constants["OPENINGS"]) and _string_table(constants["CLOSINGS"])
                 and set(constants["OPENINGS"]) == set(constants["STYLES"])
                 and set(constants["CLOSINGS"]) == set(constants["STYLES"])
                 and isinstance(limits, dict) and set(limits) == set(constants["LENGTHS"])
                 and all(isinstance(row, list) and len(row) == 3
                         and all(type(item) is int and 1 <= item <= 100 for item in row)
                         for row in limits.values()))
    else:
        valid = True  # Both other targets have strictly empty constants.
    if not valid:
        raise ValueError("PROVENANCE_DEFINITION_INVALID")


def _helpers(names):
    # Explicit module allowlists; never accept paths from applicant/source data.
    return [{"module": name, "sha256": hashlib.sha256(
        (_ROOT / (name.replace(".", "/") + ".py")).read_bytes()).hexdigest()}
        for name in names]


def _parser_helper_names(module):
    # Lever also uses Greenhouse.plain_text; the other adapter normalizers are
    # independent implementations with the common helpers listed above.
    inherited = ("services.discovery.sources.greenhouse",) if module == "services.discovery.sources.lever" else ()
    return (module, *inherited, *_PARSER_HELPERS)


def _definition(function, constants=None, *, entrypoint=None):
    return {"entrypoint": entrypoint or function.__module__ + "." + function.__qualname__,
            "source": textwrap.dedent(inspect.getsource(function)),
            "constants": constants or {}}


def _snapshot(target, template_id, model_id, execution_kind, definitions, helpers):
    manifest = {"target": target, "template_id": template_id, "model_id": model_id,
                "execution_kind": execution_kind, "definitions": definitions,
                "helpers": helpers}
    template = _json(manifest)
    digest = _digest(template)
    definition = PromptVersion(target=target, version="1.0.0-sha256." + digest,
        template=template, description="Public deterministic implementation manifest",
        variables=(), sha256=digest, created_at_utc="")
    snapshot = {"schema_version": 1, "kind": "IMPLEMENTATION_MANIFEST",
                "ai_executed": False, "definition": definition.to_dict()}
    # Enforce the same bounded, strict format on capture and historical readback.
    resolve_provenance(snapshot)
    return snapshot


def capture_document(target, template_id, model_id):
    """Bind the selected real template to its public definition, never defaults."""
    if target == "resume":
        from services.documents import templates
        contract = templates.CONTRACTS.get(template_id)
        if not contract or contract[0] != model_id:
            raise ValueError("PROVENANCE_TEMPLATE_UNKNOWN")
        definitions = [
            _definition(templates.resume_layout, {"contract": list(contract),
                        "HEADINGS": dict(templates.HEADINGS)}),
            _definition(templates.contact_header,
                        {"CONTACT_TEMPLATES": sorted(templates.CONTACT_TEMPLATES)}),
        ]
        kind = "DETERMINISTIC_TEMPLATE"
    elif target == "cover_letter":
        from services.documents import cover_letter
        if template_id != cover_letter.COVER_LETTER_TEMPLATE or model_id != "deterministic-job-evidence-v1":
            raise ValueError("PROVENANCE_TEMPLATE_UNKNOWN")
        definitions = [_definition(cover_letter.build_cover_letter, {
            "STYLES": list(cover_letter.STYLES), "LENGTHS": list(cover_letter.LENGTHS),
            "OPENINGS": dict(cover_letter._OPENINGS), "CLOSINGS": dict(cover_letter._CLOSINGS),
            "LIMITS": {key: list(value) for key, value in cover_letter._LIMITS.items()},
        })]
        kind = "DETERMINISTIC_TEMPLATE"
    elif target == "screening_answers":
        from services.documents import answers
        if template_id != "reviewed-application-answers-v1" or model_id != "manual-reviewed-answers-v1":
            raise ValueError("PROVENANCE_TEMPLATE_UNKNOWN")
        definitions = [_definition(answers.export_answers)]
        kind = "MANUAL_REVIEWED_EXPORT"
    else:
        raise ValueError("PROVENANCE_TARGET_UNKNOWN")
    return _snapshot(target, template_id, model_id, kind, definitions,
                     _helpers(_DOCUMENT_HELPERS[target]))


def capture_parser(adapter):
    """Capture the normalize callable actually invoked for a supported adapter."""
    producer = type(adapter).__module__ + "." + type(adapter).__qualname__
    if producer not in _PARSERS:
        return {"schema_version": 1, "status": "UNSUPPORTED_PRODUCER",
                "target": "job_parsing", "ai_executed": False}
    module = type(adapter).__module__
    return _snapshot("job_parsing", producer + ".normalize", "deterministic-source-parser",
                     "DETERMINISTIC_PARSER", [_definition(adapter.normalize,
                         entrypoint=producer + ".normalize")],
                     _helpers(_parser_helper_names(module)))


def resolve_provenance(snapshot):
    """Validate an immutable snapshot using only stored bytes, never current code.

    This is integrity checking, not a signature or attestation against a database
    writer who can replace both the definition and its content-derived identity.
    """
    try:
        return _resolve(snapshot)
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError):
        raise ValueError("PROVENANCE_METADATA_INVALID") from None


def _resolve(snapshot):
    if (not isinstance(snapshot, dict) or set(snapshot) != {
            "schema_version", "kind", "ai_executed", "definition"}
            or type(snapshot["schema_version"]) is not int or snapshot["schema_version"] != 1
            or snapshot["kind"] != "IMPLEMENTATION_MANIFEST" or snapshot["ai_executed"] is not False):
        raise ValueError("PROVENANCE_METADATA_INVALID")
    if len(_json(snapshot).encode("utf-8")) > _MAX_BYTES:
        raise ValueError("PROVENANCE_METADATA_TOO_LARGE")
    raw = snapshot["definition"]
    if (not isinstance(raw, dict) or set(raw) != {"target", "version", "template",
            "description", "variables", "sha256", "created_at_utc"}
            or any(not isinstance(raw[key], str) for key in raw if key != "variables")
            or raw["variables"] != [] or raw["created_at_utc"] != ""
            or raw["description"] != "Public deterministic implementation manifest"
            or raw["target"] not in PROMPT_TARGETS or not _DIGEST.fullmatch(raw["sha256"])):
        raise ValueError("PROVENANCE_METADATA_INVALID")
    if raw["version"] != "1.0.0-sha256." + raw["sha256"]:
        raise ValueError("PROVENANCE_VERSION_UNKNOWN")
    definition = PromptVersion.from_dict(raw)
    manifest = json.loads(definition.template)
    if (not isinstance(manifest, dict) or set(manifest) != {"target", "template_id", "model_id",
            "execution_kind", "definitions", "helpers"}
            or _json(manifest) != definition.template or manifest["target"] != definition.target
            or manifest["target"] not in PROMPT_TARGETS
            or any(not isinstance(manifest[key], str) or not _IDENTIFIER.fullmatch(manifest[key])
                   for key in ("target", "template_id", "model_id"))):
        raise ValueError("PROVENANCE_METADATA_INVALID")
    expected_kind = ("DETERMINISTIC_PARSER" if definition.target == "job_parsing" else
                     "MANUAL_REVIEWED_EXPORT" if definition.target == "screening_answers" else
                     "DETERMINISTIC_TEMPLATE")
    if manifest["execution_kind"] != expected_kind:
        raise ValueError("PROVENANCE_METADATA_INVALID")
    definitions = manifest["definitions"]
    if not isinstance(definitions, list) or not definitions or len(definitions) > 8:
        raise ValueError("PROVENANCE_METADATA_INVALID")
    for item in definitions:
        if (not isinstance(item, dict) or set(item) != {"entrypoint", "source", "constants"}
                or not isinstance(item["entrypoint"], str) or not item["entrypoint"]
                or not isinstance(item["source"], str) or not item["source"]
                or not isinstance(item["constants"], dict)):
            raise ValueError("PROVENANCE_METADATA_INVALID")
    if definition.target == "job_parsing":
        producer = manifest["template_id"].removesuffix(".normalize")
        if (producer not in _PARSERS or manifest["template_id"] != producer + ".normalize"
                or manifest["model_id"] != "deterministic-source-parser"):
            raise ValueError("PROVENANCE_PRODUCER_UNKNOWN")
        entrypoints = (producer + ".normalize",)
        expected_helpers = _parser_helper_names(producer.rsplit(".", 1)[0])
        constant_keys = (set(),)
    else:
        entrypoints = _ENTRYPOINTS[definition.target]
        expected_helpers = _DOCUMENT_HELPERS[definition.target]
        constant_keys = ({"contract", "HEADINGS"}, {"CONTACT_TEMPLATES"}) if definition.target == "resume" else (
            ({"STYLES", "LENGTHS", "OPENINGS", "CLOSINGS", "LIMITS"},)
            if definition.target == "cover_letter" else (set(),))
    if tuple(item["entrypoint"] for item in definitions) != entrypoints:
        raise ValueError("PROVENANCE_PRODUCER_UNKNOWN")
    for item, keys in zip(definitions, constant_keys):
        if set(item["constants"]) != keys:
            raise ValueError("PROVENANCE_DEFINITION_INVALID")
    _validate_constants(definition.target, definitions)
    helpers = manifest["helpers"]
    if not isinstance(helpers, list) or not helpers or len(helpers) > 32:
        raise ValueError("PROVENANCE_METADATA_INVALID")
    modules = []
    for helper in helpers:
        if (not isinstance(helper, dict) or set(helper) != {"module", "sha256"}
                or not isinstance(helper["module"], str)
                or helper["module"] not in expected_helpers
                or not isinstance(helper["sha256"], str) or not _DIGEST.fullmatch(helper["sha256"])):
            raise ValueError("PROVENANCE_METADATA_INVALID")
        modules.append(helper["module"])
    if len(modules) != len(set(modules)):
        raise ValueError("PROVENANCE_METADATA_INVALID")
    if tuple(modules) != expected_helpers:
        raise ValueError("PROVENANCE_HELPERS_INVALID")
    return {**manifest, "version": definition.version, "sha256": definition.sha256,
            "digest_kind": "implementation-manifest-sha256", "ai_executed": False}
