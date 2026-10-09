"""Profile-scoped, read-only API for deterministic job risk signals."""
from __future__ import annotations

from collections.abc import Mapping
import re

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m


_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_MAX_REASON = 500
_MAX_SOURCE = 120
_MAX_OBSERVED = 320
_MAX_COMPARISON_JOBS = 200


def _error(status: int, code: str):
    raise HTTPException(status_code=status, detail={"code": code})


def _service_evaluator():
    # Lazy import lets root compose this router on branches that have also
    # integrated the service dependency without coupling API module loading to it.
    from services.discovery.job_risk_signals import evaluate_job_risk

    return evaluate_job_risk


def _bounded_text(value, *, maximum: int, default: str | None = None):
    if not isinstance(value, str):
        return default
    value = " ".join(value.split())[:maximum]
    return value or default


def _response_signals(result):
    if not isinstance(result, Mapping):
        _error(502, "RISK_SIGNAL_RESULT_INVALID")
    warnings = result.get("warnings")
    service_signals = result.get("signals")
    if not isinstance(warnings, list) or not isinstance(service_signals, list):
        _error(502, "RISK_SIGNAL_RESULT_INVALID")

    signals = []
    warning_codes = []
    for warning in warnings:
        if not isinstance(warning, Mapping):
            _error(502, "RISK_SIGNAL_RESULT_INVALID")
        code = warning.get("code")
        level = warning.get("level")
        reason = _bounded_text(warning.get("message"), maximum=_MAX_REASON)
        evidence = warning.get("evidence")
        if (not isinstance(code, str) or not _CODE.fullmatch(code)
                or level != "ADVISORY" or reason is None
                or not isinstance(evidence, Mapping)):
            _error(502, "RISK_SIGNAL_RESULT_INVALID")
        warning_codes.append(code)
        evidence_source = _bounded_text(
            evidence.get("source"), maximum=_MAX_SOURCE,
            default=_bounded_text(evidence.get("field"), maximum=_MAX_SOURCE,
                                  default="UNKNOWN"),
        )
        observed_value = _bounded_text(
            evidence.get("text"), maximum=_MAX_OBSERVED,
        )
        signals.append({
            "code": code,
            "label": "Potential Risk",
            "severity": "ADVISORY",
            "reason": reason,
            "evidence_source": evidence_source,
            "observed_value": observed_value,
        })

    unknowns = []
    warning_signal_codes = []
    seen_signal_codes = set()
    for signal in service_signals:
        if not isinstance(signal, Mapping):
            _error(502, "RISK_SIGNAL_RESULT_INVALID")
        state = signal.get("state")
        if state not in {"WARN", "CLEAR", "UNKNOWN"}:
            _error(502, "RISK_SIGNAL_RESULT_INVALID")
        code = signal.get("code")
        if not isinstance(code, str) or not _CODE.fullmatch(code) or code in seen_signal_codes:
            _error(502, "RISK_SIGNAL_RESULT_INVALID")
        seen_signal_codes.add(code)
        if state == "WARN":
            warning_signal_codes.append(code)
        if state != "UNKNOWN":
            continue
        reason = _bounded_text(signal.get("reason"), maximum=_MAX_REASON,
                               default="Evidence is insufficient.")
        unknowns.append({"code": code, "reason": reason})
    if (len(set(warning_codes)) != len(warning_codes)
            or set(warning_codes) != set(warning_signal_codes)
            or len(warning_codes) != len(warning_signal_codes)):
        _error(502, "RISK_SIGNAL_RESULT_INVALID")
    return signals, unknowns


def _scoped_comparison_jobs(store, profile_id: str, profile_version: str,
                            current_job_id: str) -> tuple[dict, ...]:
    statement = (
        select(
            m.jobs.c.id,
            m.jobs.c.title,
            m.jobs.c.description,
            m.jobs.c.skills,
            m.jobs.c.attributes,
            m.jobs.c.remote_type,
            m.jobs.c.location,
            m.jobs.c.company_id,
            m.companies.c.name.label("company"),
            m.companies.c.domain.label("company_domain"),
        )
        .join(m.matches, m.matches.c.job_id == m.jobs.c.id)
        .join(m.companies, m.companies.c.id == m.jobs.c.company_id)
        .where(
            m.matches.c.profile_id == profile_id,
            m.matches.c.profile_version == profile_version,
            m.jobs.c.id != current_job_id,
        )
        .order_by(m.jobs.c.id)
        .limit(_MAX_COMPARISON_JOBS)
    )
    with store.engine.connect() as connection:
        rows = connection.execute(statement).mappings()
        return tuple(dict(row) for row in rows)


def create_job_risk_signals_router(store, *, evaluator=None) -> APIRouter:
    """Build a router over local persistence and a pure deterministic evaluator."""
    router = APIRouter()
    evaluate = evaluator or _service_evaluator()

    @router.get("/profiles/{profile_id}/jobs/{job_id}/risk-signals")
    def read_job_risk_signals(profile_id: str, job_id: str):
        try:
            profile = store.profile(profile_id)
        except NoResultFound:
            _error(404, "PROFILE_NOT_FOUND")
        match = store.engine.connect()
        try:
            scoped_match = match.execute(select(m.matches.c.job_id).where(
                m.matches.c.profile_id == profile_id,
                m.matches.c.profile_version == profile["version"],
                m.matches.c.job_id == job_id,
            )).first()
        finally:
            match.close()
        if scoped_match is None:
            _error(404, "JOB_NOT_FOUND")
        try:
            job = store.job(job_id, profile_id)
        except (NoResultFound, ValueError):
            _error(404, "JOB_NOT_FOUND")

        # Pass only the fields the pure service evaluates. In particular, do not
        # forward application, document, audit, or lifecycle history records.
        scoped_job = {
            key: job.get(key)
            for key in (
                "id", "title", "company", "company_id", "company_domain",
                "description", "attributes", "application_url",
                "field_provenance", "salary_min", "salary_max",
                "salary_currency", "salary_period", "company_identity",
            )
        }
        try:
            comparison_jobs = _scoped_comparison_jobs(
                store, profile_id, profile["version"], job_id,
            )
            result = evaluate(scoped_job, comparison_jobs=comparison_jobs)
            signals, unknowns = _response_signals(result)
        except HTTPException:
            raise
        except Exception as exc:
            # Evaluation is advisory. An unavailable evaluator must not turn into
            # a job rejection or an unbounded retry/remote fallback.
            raise HTTPException(
                status_code=503,
                detail={"code": "RISK_EVALUATION_UNAVAILABLE"},
            ) from exc
        return {
            "profile_id": profile_id,
            "job_id": job_id,
            "signals": signals,
            "unknowns": unknowns,
        }

    return router
