"""Generate, privately review, and explicitly accept fact-linked AI resume wording."""
from __future__ import annotations

import argparse
import json
import os
import sys

from sqlalchemy.exc import NoResultFound

from packages.persistence.store import Store
from services.ai.tasks import AITask, AITaskConfiguration
from services.documents import ai_resume, tailor
from services.documents.errors import PreparationError


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("context", "draft", "review", "prepare"):
        command = commands.add_parser(name)
        command.add_argument("--profile-id", required=True)
        command.add_argument("--output-dir", required=True,
                             help="protected existing document storage root")
        if name in ("context", "draft"):
            command.add_argument("--job-id", required=True)
        if name == "draft":
            command.add_argument("--fact-id", action="append", required=True)
            command.add_argument("--request-id", required=True)
            command.add_argument("--expected-profile-version", required=True)
            command.add_argument("--expected-job-fingerprint", required=True)
            command.add_argument("--timeout-seconds", type=float, default=30)
            command.add_argument("--consent-send-selected-facts", action="store_true",
                                 help="explicitly send selected facts to the configured provider")
        if name in ("review", "prepare"):
            command.add_argument("--draft-id", required=True)
        if name == "prepare":
            command.add_argument("--reviewed-sha256", required=True)
            command.add_argument("--reviewed-fact-id", action="append", required=True)
            command.add_argument("--confirm-reviewed", action="store_true",
                                 help="attest that every proposed sentence preserves the source facts")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    database_url = os.environ.get("MJOBS_DATABASE_URL")
    if not database_url:
        parser.error("MJOBS_DATABASE_URL must be supplied through protected environment configuration")
    store = None
    try:
        store = Store(database_url)
        if store.workspace_status()["state"] != "ACTIVE":
            raise PreparationError("AI_RESUME_WORKSPACE_UNBOUND", "AI_RESUME_WORKSPACE_UNBOUND")
        if args.command == "context":
            profile = store.profile(args.profile_id)
            job = store.job(args.job_id, args.profile_id)
            selection = AITaskConfiguration.from_mapping(
                store.ai_task_settings(args.profile_id)).for_task(AITask.RESUME_TAILORING)
            candidates = [
                row for row in tailor.eligible_rows(profile)
                if row["kind"] in tailor.REWRITABLE_RESUME_FACT_KINDS
                and (row.get("allowed_uses") is None or "RESUME" in row["allowed_uses"])
            ]
            result = {
                "expected_profile_version": profile["version"],
                "expected_job_fingerprint": tailor.target_fingerprint(job),
                "provider": selection.provider, "model": selection.model,
                "credential_source": "RUNTIME_CONFIGURATION",
                "facts": [{key: row[key] for key in ("fact_id", "kind", "text", "source")}
                          for row in candidates],
            }
        elif args.command == "draft":
            result = ai_resume.request_draft(
                store, args.profile_id, args.job_id, args.output_dir,
                request_id=args.request_id, selected_fact_ids=args.fact_id,
                expected_profile_version=args.expected_profile_version,
                expected_job_fingerprint=args.expected_job_fingerprint,
                consent=args.consent_send_selected_facts,
                timeout_seconds=args.timeout_seconds,
            )
        elif args.command == "review":
            result = ai_resume.review_draft(
                store, args.profile_id, args.draft_id, args.output_dir)
        else:
            result = ai_resume.prepare_reviewed(
                store, args.profile_id, args.draft_id, args.output_dir,
                reviewed_sha256=args.reviewed_sha256,
                reviewed_fact_ids=args.reviewed_fact_id,
                confirm_reviewed=args.confirm_reviewed,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except PreparationError as error:
        print(json.dumps({"code": error.code}), file=sys.stderr)
    except NoResultFound:
        print('{"code": "AI_RESUME_NOT_FOUND"}', file=sys.stderr)
    except KeyboardInterrupt:
        print('{"code": "AI_RESUME_INTERRUPTED_CALL_NOT_REPLAYABLE"}', file=sys.stderr)
        return 130
    except Exception:  # noqa: BLE001 - never expose private CLI exception details
        # Never echo credentials, database URLs, source facts or provider bodies.
        print('{"code": "AI_RESUME_COMMAND_FAILED"}', file=sys.stderr)
    finally:
        if store is not None:
            store.engine.dispose()
    return 2


if __name__ == "__main__":
    sys.exit(main())
