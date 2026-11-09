"""Independent REQ111 verification; immutable main, synthetic provider transport only."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
from unittest.mock import patch

EXPECTED_HEAD = "971ba931418fc271e7a652e27c288e1776448b6d"
CANARY = "REQ111_RAW_RESPONSE_CANARY"


def utc():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert head == EXPECTED_HEAD, (head, EXPECTED_HEAD)
    assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True).strip()
    sys.path.insert(0, str(repo))

    from services.ai.consumer import AIJobEvaluation
    from services.ai.structured_output import (
        StructuredOutputError, output_schema_for, parse_structured_output,
    )
    from services.documents.ai_resume import ResumeProposal
    from packages.persistence import models as m
    from packages.persistence.store import Store
    from sqlalchemy import select
    from tests.integration.test_profile_ai_credentials import ProfileAICredentialTests
    from workers.tasks import execute_operation

    report = {
        "requirement": "MJ-REQ-111", "head": head,
        "started_utc": utc(), "parser_cases": [], "worker_cases": [],
        "provider_transport": "httpx.MockTransport, real OpenAI adapter/router/worker",
        "live_provider_calls": 0, "network_guard": "socket.create_connection and socket.getaddrinfo denied",
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }

    good = {"category": "REVIEW", "rationale": "Schema boundary verified."}
    valid = [
        ("typed_job_evaluation", json.dumps(good), AIJobEvaluation),
        ("typed_nested_resume", '{"rewrites":[{"fact_id":"approved-fact","text":"A factual proposal."}]}', ResumeProposal),
    ]
    for name, raw, model in valid:
        value = parse_structured_output(raw, model)
        assert isinstance(value, model)
        report["parser_cases"].append({"name": name, "result": "PASS", "expected": "typed result"})

    bad = [
        ("root_array", "[]", AIJobEvaluation, "AI_STRUCTURED_OUTPUT_INVALID"),
        ("root_scalar", "null", AIJobEvaluation, "AI_STRUCTURED_OUTPUT_INVALID"),
        ("non_text", good, AIJobEvaluation, "AI_STRUCTURED_OUTPUT_INVALID"),
        ("duplicate_key", '{"category":"REVIEW","category":"STRONG","rationale":"' + CANARY + '"}', AIJobEvaluation, "AI_STRUCTURED_OUTPUT_INVALID"),
        ("nested_duplicate_key", '{"rewrites":[{"fact_id":"a","fact_id":"b","text":"' + CANARY + '"}]}', ResumeProposal, "AI_STRUCTURED_OUTPUT_INVALID"),
        ("nonstandard_nan", '{"category":"REVIEW","rationale":NaN}', AIJobEvaluation, "AI_STRUCTURED_OUTPUT_INVALID"),
        ("nonstandard_infinity", '{"category":"REVIEW","rationale":Infinity}', AIJobEvaluation, "AI_STRUCTURED_OUTPUT_INVALID"),
        ("trailing_json", json.dumps(good) + "{}", AIJobEvaluation, "AI_STRUCTURED_OUTPUT_INVALID"),
        ("markdown_fence", "```json\n" + json.dumps(good) + "\n```", AIJobEvaluation, "AI_STRUCTURED_OUTPUT_INVALID"),
        ("extra_root", json.dumps({**good, "extra": CANARY}), AIJobEvaluation, "AI_STRUCTURED_OUTPUT_INVALID"),
        ("wrong_rationale_type", '{"category":"REVIEW","rationale":true}', AIJobEvaluation, "AI_STRUCTURED_OUTPUT_INVALID"),
        ("invalid_enum", json.dumps({**good, "category": "APPROVED"}), AIJobEvaluation, "AI_STRUCTURED_OUTPUT_INVALID"),
        ("empty_rationale", json.dumps({**good, "rationale": " "}), AIJobEvaluation, "AI_STRUCTURED_OUTPUT_INVALID"),
        ("rationale_limit", json.dumps({**good, "rationale": "x" * 1001}), AIJobEvaluation, "AI_STRUCTURED_OUTPUT_INVALID"),
        ("extra_nested", '{"rewrites":[{"fact_id":"a","text":"' + CANARY + '","approved":true}]}', ResumeProposal, "AI_STRUCTURED_OUTPUT_INVALID"),
        ("empty_nested_list", '{"rewrites":[]}', ResumeProposal, "AI_STRUCTURED_OUTPUT_INVALID"),
        ("raw_size_limit", " " * 100001, AIJobEvaluation, "AI_STRUCTURED_OUTPUT_TOO_LARGE"),
    ]
    for name, raw, model, expected in bad:
        try:
            parse_structured_output(raw, model)
        except StructuredOutputError as error:
            assert error.code == expected, (name, error.code)
            assert str(error) == expected and CANARY not in str(error), name
        else:
            raise AssertionError(f"Invalid structured output was accepted: {name}")
        report["parser_cases"].append({"name": name, "result": "PASS", "expected": expected})

    def closed_schemas(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
            for value in node.values():
                closed_schemas(value)
        elif isinstance(node, list):
            for value in node:
                closed_schemas(value)

    for model in (AIJobEvaluation, ResumeProposal):
        closed_schemas(output_schema_for(model))
    report["closed_production_schemas"] = "PASS"

    worker_cases = [
        ("valid_control", good, True),
        ("extra_root", {**good, "extra": CANARY}, False),
        ("wrong_type", {"category": "REVIEW", "rationale": True, "job_evidence": []}, False),
        ("invalid_enum", {"category": "APPROVED", "rationale": CANARY}, False),
        ("rationale_limit", {"category": "REVIEW", "rationale": CANARY + "x" * 1001}, False),
        ("extra_nested", {**good, "job_evidence": [{"field": "description", "quote": "Python SQL synthetic private sentinel.", "extra": CANARY}]}, False),
        ("quote_count_limit", {**good, "job_evidence": [{"field": "description", "quote": "Python SQL synthetic private sentinel."}] * 4}, False),
    ]
    for name, payload, valid_case in worker_cases:
        fixture = ProfileAICredentialTests()
        try:
            with patch.object(socket, "create_connection", side_effect=AssertionError("Unexpected network connection")), \
                    patch.object(socket, "getaddrinfo", side_effect=AssertionError("Unexpected DNS lookup")):
                fixture.setUp()
                fixture.response_body = payload
                operation_id = fixture.queue()
                outcome = execute_operation(operation_id)
                assert len(fixture.requests) == 1, name
                sent = json.loads(fixture.requests[0].content)
                response_format = sent["response_format"]
                assert response_format["type"] == "json_schema", response_format
                assert response_format["json_schema"]["strict"] is True
                assert response_format["json_schema"]["schema"] == output_schema_for(AIJobEvaluation)
                history = fixture.store.ai_evaluation_history("fixture")
                if valid_case:
                    assert outcome["status"] == "COMPLETED", outcome
                    assert len(history) == 1
                else:
                    assert outcome["status"] == "BLOCKED" and outcome["error_category"] == "AI_ERROR", outcome
                    assert history == []
                    public = fixture.client.get("/operations/" + operation_id)
                    assert public.status_code == 200 and public.json()["error"] == "AI_ERROR"
                    with fixture.store.engine.connect() as connection:
                        audit = connection.execute(select(m.audit.c.payload)).scalars().all()
                    visible = json.dumps({"outcome": outcome, "operation": public.json(), "audit": audit})
                    assert CANARY not in visible, name
                replay = execute_operation(operation_id)
                assert replay["status"] == "ALREADY_CLAIMED_OR_CAPACITY", replay
                assert len(fixture.requests) == 1
                reopened = Store(fixture.case.url)
                try:
                    reopened_history = reopened.ai_evaluation_history("fixture")
                    assert len(reopened_history) == (1 if valid_case else 0), name
                    with reopened.engine.connect() as connection:
                        state = connection.execute(select(m.operations.c.state).where(m.operations.c.id == operation_id)).scalar_one()
                    assert state == ("COMPLETED" if valid_case else "BLOCKED"), state
                finally:
                    reopened.engine.dispose()
                report["worker_cases"].append({
                    "name": name, "result": "PASS", "durable_state": state,
                    "provider_calls": 1, "replay_calls": 0,
                    "reopened_history_count": len(reopened_history),
                    "exact_schema_sent": True,
                })
        finally:
            fixture.doCleanups()

    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == EXPECTED_HEAD
    assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True).strip()
    report["finished_utc"] = utc()
    report["result"] = "PASS"
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"result": "PASS", "parser_cases": len(report["parser_cases"]), "worker_cases": len(report["worker_cases"]), "head": head}))


if __name__ == "__main__":
    main()
