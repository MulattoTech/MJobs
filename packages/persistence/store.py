"""Transactional store with immutable fact versions and deterministic identity."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

from sqlalchemy import (
    JSON, DateTime, String, Text, and_, case, cast, create_engine, event, func, literal, literal_column, or_,
    select, text, update,
)
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from packages.domain.safety import Fact
from packages.domain.profile import ContactDetails, JobPreferences
from packages.domain.operation_mode import WorkspaceModeError, parse_operation_mode
from packages.persistence import models as m
from services.normalization.jobs import canonical_job_view, raw_posting_view
from services.discovery.search_semantics import EMPLOYMENT_TYPES, effective_job_timestamp
from services.discovery.skill_taxonomy import skill_label


class ProfileVersionConflict(Exception):
    """A profile changed after the caller's snapshot was loaded."""


def now():
    return datetime.now(timezone.utc).isoformat()


def identity(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def upsert(connection, table, data, replace=False):
    insert = {"postgresql": postgres_insert, "sqlite": sqlite_insert}.get(connection.dialect.name)
    if insert is None:
        raise ValueError("Unsupported persistence backend")
    statement = insert(table).values(**data)
    statement = statement.on_conflict_do_update(index_elements=[table.c.id], set_=data) if replace else statement.on_conflict_do_nothing(index_elements=[table.c.id])
    connection.execute(statement)


class Store:
    def __init__(self, url):
        self.url = url
        options = {"connect_args": {"check_same_thread": False, "timeout": 15}} if url.startswith("sqlite:") else {"connect_args": {"connect_timeout": 10}}
        self.engine = create_engine(url, pool_pre_ping=True, **options)
        if url.startswith("sqlite:"):
            @event.listens_for(self.engine, "connect")
            def foreign_keys(dbapi_connection, _):
                dbapi_connection.execute("PRAGMA foreign_keys=ON")

    def migrate(self):
        """Test-only schema setup; deployment uses versioned Alembic migrations."""
        if not self.url.startswith("sqlite:"):
            raise ValueError("Use alembic upgrade head for PostgreSQL")
        m.metadata.create_all(self.engine)

    @staticmethod
    def _business_tables():
        return sorted((table for table in m.metadata.tables.values() if table is not m.workspace_identity),
                      key=lambda table: table.name)

    @staticmethod
    def _workspace_snapshot(connection):
        digest = hashlib.sha256(b"MJobs workspace snapshot v1\0")
        counts = {}
        for table in Store._business_tables():
            count = 0
            digest.update(table.name.encode("utf-8") + b"\0")
            for row in connection.execute(select(table).order_by(table.c.id)).mappings():
                digest.update(json.dumps(dict(row), sort_keys=True, default=str, ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8") + b"\0")
                count += 1
            counts[table.name] = count
        return {"fingerprint": digest.hexdigest(), "counts": counts}

    def workspace_status(self):
        """Return a sanitized inventory and fingerprint for explicit legacy review."""
        with self.engine.connect() as connection:
            row = connection.execute(select(m.workspace_identity).where(
                m.workspace_identity.c.id == "workspace")).mappings().one_or_none()
            snapshot = self._workspace_snapshot(connection)
        return {"state": "ACTIVE" if row else "UNBOUND", "operation_mode": row["operation_mode"] if row else None,
                **snapshot}

    def adopt_legacy_workspace_mode(self, mode, expected_fingerprint):
        """Bind reviewed legacy data only when its complete snapshot still matches."""
        mode = parse_operation_mode(mode)
        if not isinstance(expected_fingerprint, str) or len(expected_fingerprint) != 64 or any(
                character not in "0123456789abcdef" for character in expected_fingerprint.lower()):
            raise ValueError("Expected a 64-character workspace snapshot fingerprint")
        with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL lock_timeout = '10s'"))
                connection.execute(text("SELECT pg_advisory_xact_lock(73041516369901)"))
                for table in self._business_tables():
                    connection.execute(text(f'LOCK TABLE "{table.name}" IN SHARE MODE'))
            elif connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                raise ValueError("Unsupported persistence backend")
            if connection.execute(select(m.workspace_identity.c.id).where(
                    m.workspace_identity.c.id == "workspace")).first():
                raise WorkspaceModeError("WORKSPACE_ALREADY_BOUND", "Database already has a durable operation mode")
            snapshot = self._workspace_snapshot(connection)
            if snapshot["fingerprint"] != expected_fingerprint.lower():
                raise WorkspaceModeError("WORKSPACE_SNAPSHOT_CHANGED", "Legacy data changed after inspection; inspect again before binding")
            upsert(connection, m.workspace_identity, {"id": "workspace", "operation_mode": mode, "bound_at": now()})
            return {"state": "ACTIVE", "operation_mode": mode, **snapshot}

    def bind_workspace_mode(self, mode):
        """Bind an empty database once; never infer a legacy database's mode."""
        mode = parse_operation_mode(mode)
        with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SELECT pg_advisory_xact_lock(73041516369901)"))
            elif connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                raise ValueError("Unsupported persistence backend")
            row = connection.execute(select(m.workspace_identity).where(
                m.workspace_identity.c.id == "workspace")).mappings().one_or_none()
            if row is None:
                for table in m.metadata.sorted_tables:
                    if table is not m.workspace_identity and connection.execute(
                            select(table.c.id).limit(1)).first():
                        raise WorkspaceModeError("WORKSPACE_MODE_UNBOUND",
                            "Existing data has no durable operation-mode identity; preserve it for review and use a separate Compose project")
                upsert(connection, m.workspace_identity, {"id": "workspace", "operation_mode": mode, "bound_at": now()})
                row = connection.execute(select(m.workspace_identity).where(
                    m.workspace_identity.c.id == "workspace")).mappings().one()
            if row["operation_mode"] != mode:
                raise WorkspaceModeError("WORKSPACE_MODE_MISMATCH",
                    f"Database is bound to {row['operation_mode']}; requested {mode}. Use a separate Compose project")
            return dict(row)

    def assert_workspace_mode(self, mode):
        """Read-only guard before claiming an operation or exposing data."""
        mode = parse_operation_mode(mode)
        with self.engine.connect() as connection:
            row = connection.execute(select(m.workspace_identity.c.operation_mode).where(
                m.workspace_identity.c.id == "workspace")).scalar_one_or_none()
        if row is None:
            raise WorkspaceModeError("WORKSPACE_MODE_UNBOUND", "Database has no durable operation-mode identity")
        if row != mode:
            raise WorkspaceModeError("WORKSPACE_MODE_MISMATCH",
                f"Database is bound to {row}; requested {mode}. Use a separate Compose project")

    def save_profile(self, profile, expected_version=None, require_expected_version=False):
        if not all(isinstance(profile.get(field), str) and profile[field].strip() for field in ("id", "name", "email")):
            raise ValueError("Profile identity/contact fields are required")
        if not isinstance(profile.get("facts"), list) or len(profile["facts"]) > 200:
            raise ValueError("Provide up to 200 explicit career facts")
        profile = dict(profile)
        # Older clients omit these fields. Preserve existing values on their updates.
        with self.engine.connect() as connection:
            current = connection.execute(select(m.profiles.c.version).where(m.profiles.c.id == profile['id'])).scalar_one_or_none()
        if current is not None and require_expected_version and expected_version is None:
            raise ProfileVersionConflict("Profile version is required for updates; reload before saving")
        if expected_version is not None and expected_version != current:
            raise ProfileVersionConflict("Profile changed since this editor was loaded; reload before saving")
        previous = self.profile(profile['id']) if current else {}
        profile['contact'] = ContactDetails.model_validate(profile.get('contact', previous.get('contact', {}))).model_dump()
        profile['preferences'] = JobPreferences.model_validate(profile.get('preferences', previous.get('preferences', {}))).model_dump()
        preferences_changed = current is not None and previous.get('preferences', {}) != profile['preferences']
        verified = [Fact(**{key: row[key] for key in ("id", "text", "source", "verified", "user_approved", "confidence")}) for row in profile["facts"]]
        if len({fact.id for fact in verified}) != len(verified):
            raise ValueError("Duplicate fact identity")
        answers = profile.get("answers", previous.get("answers", []))
        if not isinstance(answers, list) or len(answers) > 200:
            raise ValueError("Provide up to 200 reusable answers")
        old_answers = {row["id"]: row for row in previous.get("answers", [])}
        normalized_answers = []
        for answer in answers:
            if not isinstance(answer, dict):
                raise ValueError("Reusable answers must be objects")
            validated = {key: answer[key] for key in ("id", "text", "source", "verified", "user_approved", "confidence")}
            Fact(**validated)
            if answer.get("question") is not None:
                if not isinstance(answer["question"], str) or not answer["question"].strip() or len(answer["question"]) > 2000:
                    raise ValueError("Reusable answer questions must be nonempty and at most 2000 characters")
                validated["question"] = answer["question"]
            old = old_answers.get(validated["id"])
            substantive = ("question", "text", "source", "confidence")
            if old and any(old.get(key) != validated.get(key) for key in substantive):
                validated["verified"] = False
                validated["user_approved"] = False
            normalized_answers.append(validated)
        answers = normalized_answers
        profile["answers"] = answers
        version = identity(profile)
        same_match_inputs = bool(current) and self._match_input_projection(previous) == self._match_input_projection(profile)
        with self.engine.begin() as connection:
            if current is not None:
                # Atomic compare-and-lock prevents an overlapping full-profile
                # save from committing a snapshot based on an older approval.
                claimed = connection.execute(update(m.profiles).where(
                    m.profiles.c.id == profile['id'], m.profiles.c.version == current,
                ).values(version=current))
                if claimed.rowcount != 1:
                    raise ProfileVersionConflict("Profile changed while this save was being prepared; reload before saving")
            upsert(connection, m.profiles, {"id": profile["id"], "name": profile["name"], "email": profile["email"], "version": version}, replace=True)
            if len({answer["id"] for answer in answers}) != len(answers):
                raise ValueError("Duplicate approved answer identity")
            upsert(connection, m.profile_versions, {"id": identity(profile["id"], version), "profile_id": profile["id"], "version": version,
                   "contact": {"name": profile["name"], "email": profile["email"],
                               "contact": profile['contact'], "preferences": profile['preferences']}, "answers": answers})
            for row, fact in zip(profile["facts"], verified):
                data = asdict(fact)
                data.update(id=identity(profile["id"], version, fact.id), fact_id=fact.id,
                            kind=row.get("kind", "skill"), profile_id=profile["id"], version=version)
                upsert(connection, m.facts, data)
            if same_match_inputs and previous["version"] != version:
                prior_matches = connection.execute(select(m.matches).where(
                    m.matches.c.profile_id == profile["id"],
                    m.matches.c.profile_version == previous["version"])).mappings().all()
                for row in prior_matches:
                    data = dict(row)
                    data.update(id=identity(row["job_id"], profile["id"], version), profile_version=version)
                    upsert(connection, m.matches, data)
                if prior_matches:
                    self._audit(connection, profile["id"], "MATCHES_SNAPSHOT_REBOUND",
                                {"from_profile_version": previous["version"], "to_profile_version": version,
                                 "count": len(prior_matches)})
            self._audit(connection, profile["id"], "PROFILE_SAVED", {"version": version})
            if preferences_changed:
                self._audit(connection, profile["id"], "SETTING_CHANGED", {})
        return version

    @staticmethod
    def _match_input_projection(profile):
        facts = [{
            "id": row.get("fact_id", row.get("id")),
            "kind": row.get("kind", "skill"),
            "text": row["text"],
            "source": row["source"],
            "verified": row["verified"],
            "user_approved": row["user_approved"],
            "confidence": row["confidence"],
        } for row in profile.get("facts", [])]
        facts.sort(key=lambda row: row["id"])
        # Exclusions govern generated career text, not job relevance. Editing
        # them must preserve the existing matching snapshot without rescoring.
        preferences = {key: value for key, value in profile.get("preferences", {}).items()
                      if key != "avoid_terms"}
        return {"facts": facts, "preferences": preferences}

    def profile(self, profile_id, version=None, connection=None):
        if connection is not None:
            return self._profile_on(connection, profile_id, version)
        with self.engine.connect() as owned_connection:
            return self._profile_on(owned_connection, profile_id, version)

    @staticmethod
    def _profile_on(connection, profile_id, version=None):
        row = connection.execute(select(m.profiles).where(m.profiles.c.id == profile_id)).mappings().one()
        data = dict(row)
        data["version"] = version or data["version"]
        snapshot = connection.execute(select(m.profile_versions).where(m.profile_versions.c.id == identity(profile_id, data["version"]))).mappings().one()
        data.update(snapshot["contact"])
        data.setdefault('contact', ContactDetails().model_dump())
        data.setdefault('preferences', JobPreferences().model_dump())
        data["answers"] = snapshot["answers"]
        data["facts"] = [dict(fact) for fact in connection.execute(select(m.facts).where(m.facts.c.profile_id == profile_id, m.facts.c.version == data["version"])).mappings()]
        return data

    def profiles(self):
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(m.profiles).order_by(m.profiles.c.name)).mappings()]

    def ai_task_settings(self, profile_id):
        """Return explicitly stored non-secret selections for one existing profile."""
        self.profile(profile_id)
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(m.ai_task_settings.c.task, m.ai_task_settings.c.provider,
                       m.ai_task_settings.c.model)
                .where(m.ai_task_settings.c.profile_id == profile_id)
                .order_by(m.ai_task_settings.c.task)
            ).mappings()
            return {row["task"]: {"provider": row["provider"], "model": row["model"]}
                    for row in rows}

    def save_ai_task_settings(self, profile_id, selections):
        """Persist a validated task-keyed mapping atomically for one profile."""
        self.profile(profile_id)
        if not isinstance(selections, dict):
            raise ValueError("AI_TASK_CONFIG_INVALID")
        timestamp = now()
        with self.engine.begin() as connection:
            current_rows = connection.execute(
                select(m.ai_task_settings.c.task, m.ai_task_settings.c.provider,
                       m.ai_task_settings.c.model)
                .where(m.ai_task_settings.c.profile_id == profile_id)
            ).mappings()
            current = {
                row["task"]: {"provider": row["provider"], "model": row["model"]}
                for row in current_rows
            }
            changed = False
            for task, selection in selections.items():
                if (not isinstance(task, str) or not isinstance(selection, dict)
                        or set(selection) != {"provider", "model"}):
                    raise ValueError("AI_TASK_CONFIG_INVALID")
                effective_current = current.get(
                    task, {"provider": "disabled", "model": None})
                changed = changed or selection != effective_current
                upsert(connection, m.ai_task_settings, {
                    "id": identity(profile_id, task),
                    "profile_id": profile_id,
                    "task": task,
                    "provider": selection["provider"],
                    "model": selection["model"],
                    "updated_at": timestamp,
                }, replace=True)
            if changed:
                self._audit(connection, profile_id, "SETTING_CHANGED", {})
        return self.ai_task_settings(profile_id)

    def provider_credentials(self, profile_id):
        """Return credential metadata only; encrypted values stay in the store boundary."""
        self.profile(profile_id)
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(m.provider_credentials.c.provider, m.provider_credentials.c.created_at,
                       m.provider_credentials.c.updated_at, m.provider_credentials.c.last_tested_at,
                       m.provider_credentials.c.last_test_status, m.provider_credentials.c.rotation_count)
                .where(m.provider_credentials.c.profile_id == profile_id)
                .order_by(m.provider_credentials.c.provider)
            ).mappings()
            return [dict(row, configured=True, masked="********") for row in rows]

    def encrypted_provider_credential(self, profile_id, provider):
        """Return ciphertext only to the server-side credential service."""
        self.profile(profile_id)
        with self.engine.connect() as connection:
            row = connection.execute(select(m.provider_credentials.c.encrypted_secret).where(
                m.provider_credentials.c.profile_id == profile_id,
                m.provider_credentials.c.provider == provider)).first()
        return row[0] if row else None

    def save_provider_credential(self, profile_id, provider, encrypted_secret):
        """Insert or rotate one encrypted profile/provider credential atomically."""
        if not isinstance(encrypted_secret, bytes) or not encrypted_secret:
            raise ValueError("CREDENTIAL_INPUT_INVALID")
        self.profile(profile_id)
        timestamp = now()
        credential_id = identity(profile_id, provider)
        with self.engine.begin() as connection:
            existing = connection.execute(select(m.provider_credentials).where(
                m.provider_credentials.c.profile_id == profile_id,
                m.provider_credentials.c.provider == provider)).mappings().one_or_none()
            upsert(connection, m.provider_credentials, {
                "id": credential_id,
                "profile_id": profile_id,
                "provider": provider,
                "encrypted_secret": encrypted_secret,
                "created_at": existing["created_at"] if existing else timestamp,
                "updated_at": timestamp,
                "last_tested_at": None,
                "last_test_status": "NOT_TESTED",
                "rotation_count": int(existing["rotation_count"] + 1) if existing else 0,
            }, replace=True)
        return next(item for item in self.provider_credentials(profile_id) if item["provider"] == provider)

    def record_provider_credential_test(self, profile_id, provider, status):
        """Persist only a bounded result code and timestamp from a connection check."""
        if status not in ("CONNECTED", "FAILED"):
            raise ValueError("CREDENTIAL_TEST_RESULT_INVALID")
        timestamp = now()
        with self.engine.begin() as connection:
            result = connection.execute(update(m.provider_credentials).where(
                m.provider_credentials.c.profile_id == profile_id,
                m.provider_credentials.c.provider == provider).values(
                    last_tested_at=timestamp, last_test_status=status))
            if result.rowcount != 1:
                return None
        return next(item for item in self.provider_credentials(profile_id) if item["provider"] == provider)

    def delete_provider_credential(self, profile_id, provider):
        """Delete a credential for its owning profile without returning ciphertext."""
        self.profile(profile_id)
        with self.engine.begin() as connection:
            result = connection.execute(m.provider_credentials.delete().where(
                m.provider_credentials.c.profile_id == profile_id,
                m.provider_credentials.c.provider == provider))
        return result.rowcount == 1

    def ai_cost_policy(self, profile_id):
        """Return the explicitly stored funnel policy for one profile, if set."""
        self.profile(profile_id)
        with self.engine.connect() as connection:
            row = connection.execute(
                select(m.ai_cost_policies).where(
                    m.ai_cost_policies.c.profile_id == profile_id)
            ).mappings().first()
        if row is None:
            return None
        return {key: row[key] for key in (
            "minimum_score", "max_input_candidates", "max_ai_candidates",
            "max_document_candidates")}

    def save_ai_cost_policy(self, profile_id, policy):
        """Persist deterministic AI-work limits atomically for one profile."""
        self.profile(profile_id)
        fields = ("minimum_score", "max_input_candidates", "max_ai_candidates",
                  "max_document_candidates")
        if not isinstance(policy, dict) or set(policy) != set(fields):
            raise ValueError("AI_FUNNEL_POLICY_INVALID")
        values = {key: policy[key] for key in fields}
        with self.engine.begin() as connection:
            row = connection.execute(select(m.ai_cost_policies).where(
                m.ai_cost_policies.c.profile_id == profile_id).with_for_update()
            ).mappings().one_or_none()
            current = ({key: row[key] for key in fields} if row is not None else {
                "minimum_score": 75,
                "max_input_candidates": 5000,
                "max_ai_candidates": 75,
                "max_document_candidates": 20,
            })
            upsert(connection, m.ai_cost_policies, {
                "id": identity(profile_id, "ai-cost-policy"),
                "profile_id": profile_id,
                **values,
                "updated_at": now(),
            }, replace=True)
            if values != current:
                self._audit(connection, profile_id, "SETTING_CHANGED", {})
        return values

    def save_ai_evaluation_run(self, profile_id, profile_version, policy,
                               task_provider, task_model, evaluations):
        """Atomically persist bounded advisory evaluations for the profile snapshot."""
        policy_fields = {"minimum_score", "max_input_candidates", "max_ai_candidates",
                         "max_document_candidates"}
        if (not isinstance(profile_version, str) or not profile_version
                or not isinstance(policy, dict) or set(policy) != policy_fields
                or any(type(value) is not int for value in policy.values())
                or not 0 <= policy["minimum_score"] <= 100
                or not 1 <= policy["max_input_candidates"] <= 100000
                or not 0 <= policy["max_ai_candidates"] <= 75
                or not 0 <= policy["max_document_candidates"] <= min(20, policy["max_ai_candidates"])
                or not isinstance(task_provider, str) or not task_provider or len(task_provider) > 64
                or not isinstance(task_model, str) or not task_model or len(task_model) > 128
                or not isinstance(evaluations, list) or len(evaluations) > 75):
            raise ValueError("AI_EVALUATION_INPUT_INVALID")
        values = []
        seen_job_ids = set()
        for item in evaluations:
            if not isinstance(item, dict) or set(item) != {
                    "job_id", "title", "company", "deterministic_score", "category", "rationale"}:
                raise ValueError("AI_EVALUATION_INPUT_INVALID")
            if (not isinstance(item["job_id"], str) or not item["job_id"]
                    or len(item["job_id"]) > 200 or item["job_id"] in seen_job_ids
                    or not isinstance(item["title"], str) or len(item["title"]) > 512
                    or not isinstance(item["company"], str) or len(item["company"]) > 512
                    or type(item["deterministic_score"]) is not int
                    or not 0 <= item["deterministic_score"] <= 100
                    or item["category"] not in ("STRONG", "REVIEW", "WEAK")
                    or not isinstance(item["rationale"], str)
                    or not item["rationale"].strip() or len(item["rationale"]) > 1000):
                raise ValueError("AI_EVALUATION_INPUT_INVALID")
            seen_job_ids.add(item["job_id"])
            values.append(item)

        run_id = identity(profile_id, "ai-evaluation-run", uuid4().hex)
        created_at = now()
        with self.engine.begin() as connection:
            current_version = connection.execute(
                select(m.profiles.c.version).where(m.profiles.c.id == profile_id)
                .with_for_update()
            ).scalar_one()
            if current_version != profile_version:
                raise ValueError("AI_EVALUATION_PROFILE_CHANGED")
            upsert(connection, m.ai_evaluation_runs, {
                "id": run_id,
                "profile_id": profile_id,
                "profile_version": profile_version,
                "policy": policy,
                "task_provider": task_provider,
                "task_model": task_model,
                "evaluated_candidate_count": len(values),
                "created_at": created_at,
            })
            for item in values:
                upsert(connection, m.ai_job_evaluations, {
                    "id": identity(run_id, item["job_id"]),
                    "run_id": run_id,
                    "profile_id": profile_id,
                    "job_id": item["job_id"],
                    "title": item["title"],
                    "company": item["company"],
                    "deterministic_score": item["deterministic_score"],
                    "category": item["category"],
                    "rationale": item["rationale"],
                    "created_at": created_at,
                })
        return {"run_id": run_id, "profile_version": profile_version,
                "policy": policy, "task_provider": task_provider,
                "task_model": task_model,
                "evaluated_candidate_count": len(values), "created_at": created_at}

    def ai_evaluation_history(self, profile_id, limit=20):
        """Read the newest persisted advisory evaluations for one profile."""
        self.profile(profile_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("AI_EVALUATION_HISTORY_LIMIT_INVALID")
        with self.engine.connect() as connection:
            runs = [dict(row) for row in connection.execute(
                select(m.ai_evaluation_runs).where(
                    m.ai_evaluation_runs.c.profile_id == profile_id
                ).order_by(m.ai_evaluation_runs.c.created_at.desc(),
                           m.ai_evaluation_runs.c.id.desc()).limit(limit)
            ).mappings()]
            if not runs:
                return []
            run_ids = [row["id"] for row in runs]
            evaluations = [dict(row) for row in connection.execute(
                select(m.ai_job_evaluations).where(
                    m.ai_job_evaluations.c.profile_id == profile_id,
                    m.ai_job_evaluations.c.run_id.in_(run_ids),
                ).order_by(m.ai_job_evaluations.c.deterministic_score.desc(),
                           m.ai_job_evaluations.c.id)
            ).mappings()]
            reviewed = {(row["run_id"], row["job_id"]) for row in connection.execute(
                select(m.ai_document_reviews.c.run_id, m.ai_document_reviews.c.job_id).where(
                    m.ai_document_reviews.c.profile_id == profile_id,
                    m.ai_document_reviews.c.run_id.in_(run_ids),
                )
            ).mappings()}
        by_run = {run_id: [] for run_id in run_ids}
        for row in evaluations:
            by_run[row["run_id"]].append({key: row[key] for key in (
                "job_id", "title", "company", "deterministic_score", "category", "rationale")}
                | {"document_reviewed": (row["run_id"], row["job_id"]) in reviewed})
        return [{
            "run_id": row["id"],
            "profile_version": row["profile_version"],
            "policy": row["policy"],
            "task_provider": row["task_provider"],
            "task_model": row["task_model"],
            "evaluated_candidate_count": row["evaluated_candidate_count"],
            "created_at": row["created_at"],
            "evaluations": by_run[row["id"]],
        } for row in runs]

    def review_ai_document_candidates(self, profile_id, run_id, job_ids):
        """Persist explicit review for at most the run's document-candidate cap."""
        if (not isinstance(run_id, str) or not run_id or len(run_id) > 128
                or not isinstance(job_ids, list) or not 1 <= len(job_ids) <= 20
                or any(not isinstance(job_id, str) or not job_id or len(job_id) > 128
                       for job_id in job_ids)
                or len(set(job_ids)) != len(job_ids)):
            raise ValueError("AI_DOCUMENT_REVIEW_INPUT_INVALID")
        reviewed_at = now()
        with self.engine.begin() as connection:
            run = connection.execute(
                select(m.ai_evaluation_runs).where(
                    m.ai_evaluation_runs.c.id == run_id,
                    m.ai_evaluation_runs.c.profile_id == profile_id,
                ).with_for_update()
            ).mappings().first()
            if run is None:
                raise ValueError("AI_EVALUATION_RUN_NOT_FOUND")
            current_version = connection.execute(
                select(m.profiles.c.version).where(m.profiles.c.id == profile_id)
            ).scalar_one()
            if current_version != run["profile_version"]:
                raise ValueError("AI_EVALUATION_PROFILE_CHANGED")
            allowed = set(connection.execute(
                select(m.ai_job_evaluations.c.job_id).where(
                    m.ai_job_evaluations.c.profile_id == profile_id,
                    m.ai_job_evaluations.c.run_id == run_id,
                )
            ).scalars())
            if not set(job_ids).issubset(allowed):
                raise ValueError("AI_DOCUMENT_CANDIDATE_NOT_EVALUATED")
            existing = set(connection.execute(
                select(m.ai_document_reviews.c.job_id).where(
                    m.ai_document_reviews.c.profile_id == profile_id,
                    m.ai_document_reviews.c.run_id == run_id,
                )
            ).scalars())
            new_ids = set(job_ids) - existing
            cap = int(run["policy"]["max_document_candidates"])
            if len(existing) + len(new_ids) > cap:
                raise ValueError("AI_DOCUMENT_CANDIDATE_LIMIT")
            for job_id in job_ids:
                if job_id in new_ids:
                    upsert(connection, m.ai_document_reviews, {
                        "id": identity(run_id, job_id, "ai-document-review"),
                        "profile_id": profile_id,
                        "run_id": run_id,
                        "job_id": job_id,
                        "reviewed_at": reviewed_at,
                    })
        return {"run_id": run_id, "reviewed_job_ids": job_ids,
                "reviewed_candidate_count": len(existing | set(job_ids)),
                "max_document_candidates": cap}

    def require_reviewed_ai_document_candidate(self, profile_id, run_id, job_id):
        """Guard document preparation against stale, cross-profile, or unreviewed AI runs."""
        with self.engine.connect() as connection:
            run = connection.execute(
                select(m.ai_evaluation_runs.c.profile_version).where(
                    m.ai_evaluation_runs.c.id == run_id,
                    m.ai_evaluation_runs.c.profile_id == profile_id,
                )
            ).first()
            if run is None:
                raise ValueError("AI_EVALUATION_RUN_NOT_FOUND")
            current_version = connection.execute(
                select(m.profiles.c.version).where(m.profiles.c.id == profile_id)
            ).scalar_one()
            if current_version != run.profile_version:
                raise ValueError("AI_EVALUATION_PROFILE_CHANGED")
            reviewed = connection.execute(
                select(m.ai_document_reviews.c.id).where(
                    m.ai_document_reviews.c.profile_id == profile_id,
                    m.ai_document_reviews.c.run_id == run_id,
                    m.ai_document_reviews.c.job_id == job_id,
                )
            ).first()
            if reviewed is None:
                raise ValueError("AI_DOCUMENT_CANDIDATE_NOT_REVIEWED")

    def jobs(self, profile_id=None, q="", remote_type=None, provider=None, sort="score", limit=200,
             title="", company="", location="", salary_min=None, salary_max=None,
             min_match_score=None, experience_level=None, employment_type=None, skill_id=None,
             education_requirement=None, posted_after=None, posted_before=None):
        profile = self.profile(profile_id) if profile_id else None
        with self.engine.connect() as connection:
            counts = select(m.postings.c.job_id, func.count().label("posting_count")).group_by(m.postings.c.job_id).subquery()
            display_routes = select(m.routes.c.job_id, func.min(m.routes.c.id).label("route_id")).group_by(m.routes.c.job_id).subquery()
            statement = select(m.jobs, m.companies.c.name.label("company"), m.routes.c.provider, m.routes.c.url.label("application_url"),
                counts.c.posting_count, m.matches.c.score.label("match_score"), m.matches.c.components.label("match_components"), m.matches.c.missing,
                m.applications.c.state.label("application_state"), m.applications.c.id.label("application_id")).select_from(
                m.jobs.join(m.companies, m.jobs.c.company_id == m.companies.c.id)
                .join(display_routes, display_routes.c.job_id == m.jobs.c.id)
                .join(m.routes, m.routes.c.id == display_routes.c.route_id)
                .outerjoin(counts, counts.c.job_id == m.jobs.c.id)
                .outerjoin(m.matches, (m.matches.c.job_id == m.jobs.c.id) & (m.matches.c.profile_id == profile_id) &
                           (m.matches.c.profile_version == (profile["version"] if profile else "")))
                .outerjoin(m.applications, (m.applications.c.job_id == m.jobs.c.id) & (m.applications.c.profile_id == profile_id)))
            if q:
                if self.engine.dialect.name == "postgresql":
                    search_config = literal_column("'simple'::regconfig")
                    job_search_text = (
                        func.coalesce(m.jobs.c.title, literal("")) + literal(" ") +
                        func.coalesce(m.jobs.c.description, literal("")) + literal(" ") +
                        func.coalesce(cast(m.jobs.c.skills, Text), literal("")) + literal(" ") +
                        func.coalesce(m.jobs.c.location, literal(""))
                    )
                    search_query = func.websearch_to_tsquery(search_config, q)
                    job_matches = func.to_tsvector(search_config, job_search_text).bool_op("@@")(search_query)
                    company_matches = func.to_tsvector(
                        search_config, func.coalesce(m.companies.c.name, literal(""))
                    ).bool_op("@@")(search_query)
                    statement = statement.where(or_(job_matches, company_matches))
                else:
                    statement = statement.where(
                        m.jobs.c.title.icontains(q, autoescape=True) |
                        m.companies.c.name.icontains(q, autoescape=True) |
                        m.jobs.c.description.icontains(q, autoescape=True) |
                        cast(m.jobs.c.skills, String).icontains(q, autoescape=True) |
                        m.jobs.c.location.icontains(q, autoescape=True)
                    )
            if title:
                statement = statement.where(m.jobs.c.title.icontains(title, autoescape=True))
            if company:
                statement = statement.where(m.companies.c.name.icontains(company, autoescape=True))
            if location:
                statement = statement.where(m.jobs.c.location.icontains(location, autoescape=True))
            if salary_min is not None or salary_max is not None:
                attributes = m.jobs.c.attributes
                salary_currency = attributes["salary_currency"].as_string()
                salary_period = attributes["salary_period"].as_string()
                if self.engine.dialect.name == "postgresql":
                    low_is_number = func.json_typeof(attributes["salary_min"]) == "number"
                    high_is_number = func.json_typeof(attributes["salary_max"]) == "number"
                else:
                    low_is_number = func.json_type(attributes, "$.salary_min").in_(("integer", "real"))
                    high_is_number = func.json_type(attributes, "$.salary_max").in_(("integer", "real"))
                offer_min = case((low_is_number, attributes["salary_min"].as_float()), else_=None)
                offer_max = case((high_is_number, attributes["salary_max"].as_float()), else_=None)
                statement = statement.where(salary_currency == "USD", salary_period == "YEAR")
                if salary_min is not None:
                    statement = statement.where(high_is_number, offer_max >= salary_min)
                if salary_max is not None:
                    statement = statement.where(low_is_number, offer_min <= salary_max)
            if min_match_score is not None:
                # The outer join is restricted to the active profile version above;
                # this predicate also fails closed for unscored jobs and runs before LIMIT.
                statement = statement.where(m.matches.c.score >= min_match_score)
            if experience_level:
                # Exact normalized-value matching excludes absent and unrecognized metadata before LIMIT.
                statement = statement.where(m.jobs.c.attributes["experience_level"].as_string() == experience_level)
            if employment_type:
                # Known choices match their normalized value exactly. UNKNOWN also
                # covers missing and unrecognized source metadata before LIMIT.
                reported_employment_type = m.jobs.c.attributes["employment_type"].as_string()
                if employment_type == "UNKNOWN":
                    known_types = EMPLOYMENT_TYPES - {"UNKNOWN"}
                    statement = statement.where(or_(
                        reported_employment_type.is_(None),
                        reported_employment_type.notin_(known_types),
                    ))
                else:
                    statement = statement.where(reported_employment_type == employment_type)
            if skill_id:
                label = skill_label(skill_id)

                def has_exact_skill(array_column, expected, json_path=None):
                    if self.engine.dialect.name == "postgresql":
                        array_value = array_column[json_path] if json_path else array_column
                        is_array = func.json_typeof(array_value) == "array"
                        safe_array = case((is_array, array_value), else_=cast(literal("[]"), JSON))
                        values = func.json_array_elements_text(safe_array).table_valued("value").alias()
                    else:
                        type_check = (func.json_type(array_column, json_path) if json_path
                                      else func.json_type(array_column))
                        array_value = (func.json_extract(array_column, json_path) if json_path
                                       else array_column)
                        safe_array = case((type_check == "array", array_value), else_=literal("[]"))
                        values = func.json_each(safe_array).table_valued("key", "value").alias()
                    return select(literal(1)).select_from(values).where(
                        values.c.value == expected).correlate(m.jobs).exists()

                statement = statement.where(or_(
                    has_exact_skill(m.jobs.c.attributes, skill_id, "$.skill_ids")
                    if self.engine.dialect.name == "sqlite" else has_exact_skill(m.jobs.c.attributes, skill_id, "skill_ids"),
                    has_exact_skill(m.jobs.c.skills, label),
                ))
            if education_requirement:
                # Parser classifications are matched exactly; missing, malformed, and future values fail closed.
                statement = statement.where(
                    m.jobs.c.attributes["education_requirement"].as_string() == education_requirement)
            if posted_after is not None:
                reported_at = m.jobs.c.attributes["reported_posted_at"].as_string()
                if self.engine.dialect.name == "postgresql":
                    # Third-party JSON is untrusted input. Accept only the normalized
                    # ISO timestamps produced by source adapters; PostgreSQL also
                    # considers relative strings such as "now" valid timestamps.
                    iso_timestamp = reported_at.op("~")(
                        r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?([Zz]|\+00:00)$")
                    valid_posted_at = and_(
                        iso_timestamp,
                        func.pg_input_is_valid(reported_at, "timestamp with time zone"),
                    )
                    parsed_posted_at = case(
                        (valid_posted_at, cast(reported_at, DateTime(timezone=True))), else_=None)
                    cutoff = posted_after
                    upper_bound = posted_before
                else:
                    # The modifier explicitly normalizes overflow dates on SQLite
                    # versions that otherwise preserve them (for example Feb 30).
                    parsed_posted_at = func.datetime(reported_at, "+0 days")
                    iso_timestamp = reported_at.op("GLOB")(
                        "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T"
                        "[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*")
                    timezone_suffix = or_(
                        reported_at.op("GLOB")("*+00:00"),
                        reported_at.op("GLOB")("*Z"),
                        reported_at.op("GLOB")("*z"),
                    )
                    # Require the normalized whole-second value to round-trip to
                    # the original prefix, so impossible calendar dates are unknown.
                    round_trips = func.strftime("%Y-%m-%dT%H:%M:%S", parsed_posted_at) == func.substr(reported_at, 1, 19)
                    parsed_posted_at = case(
                        (and_(iso_timestamp, timezone_suffix, round_trips), parsed_posted_at), else_=None)
                    cutoff = posted_after.strftime("%Y-%m-%d %H:%M:%S")
                    upper_bound = posted_before.strftime("%Y-%m-%d %H:%M:%S") if posted_before else None
                # Posting time is source-reported. first_seen_at records collection
                # time and must never make an undated job appear newly posted.
                statement = statement.where(parsed_posted_at >= cutoff)
                if upper_bound is not None:
                    statement = statement.where(parsed_posted_at <= upper_bound)
            if remote_type:
                if remote_type == "REMOTE":
                    statement = statement.where(m.jobs.c.remote_type.in_(("REMOTE", "REMOTE_WORLDWIDE", "REMOTE_US", "REMOTE_COUNTRY", "REMOTE_SPECIFIC_STATES")))
                else:
                    statement = statement.where(m.jobs.c.remote_type == remote_type)
            if provider:
                statement = statement.where(m.jobs.c.id.in_(select(m.routes.c.job_id).where(m.routes.c.provider == provider)))
            if sort == "score":
                statement = statement.order_by(m.matches.c.score.desc().nulls_last(), m.jobs.c.title, m.jobs.c.id)
            elif sort == "title":
                statement = statement.order_by(m.jobs.c.title, m.jobs.c.id)
            elif sort == "company":
                company_name = func.trim(func.coalesce(m.companies.c.name, literal("")))
                statement = statement.order_by(
                    case((company_name == "", 1), else_=0), func.lower(company_name), m.jobs.c.id
                )
            elif sort == "newest":
                rows = [canonical_job_view(dict(row)) for row in connection.execute(statement).mappings()]
                dated = [(effective_job_timestamp(row), row) for row in rows]
                dated_rows = [(timestamp, row) for timestamp, row in dated if timestamp is not None]
                unknown_rows = sorted((row for timestamp, row in dated if timestamp is None),
                                      key=lambda row: row["id"])
                dated_rows.sort(key=lambda item: item[1]["id"])
                dated_rows.sort(key=lambda item: item[0], reverse=True)
                return [row for _, row in dated_rows[:limit]] + unknown_rows[:max(0, limit - len(dated_rows))]
            elif sort in {"salary_high", "salary_low"}:
                attributes = m.jobs.c.attributes
                currency_is_usd = attributes["salary_currency"].as_string() == "USD"
                period_is_year = attributes["salary_period"].as_string() == "YEAR"
                if self.engine.dialect.name == "postgresql":
                    low_type = func.json_typeof(attributes["salary_min"])
                    high_type = func.json_typeof(attributes["salary_max"])
                    low_is_number = func.json_typeof(attributes["salary_min"]) == "number"
                    high_is_number = func.json_typeof(attributes["salary_max"]) == "number"
                else:
                    low_type = func.json_type(attributes, "$.salary_min")
                    high_type = func.json_type(attributes, "$.salary_max")
                    low_is_number = low_type.in_(("integer", "real"))
                    high_is_number = high_type.in_(("integer", "real"))
                low_amount = attributes["salary_min"].as_float()
                high_amount = attributes["salary_max"].as_float()
                # Missing and explicit JSON null are valid one-sided endpoints.
                # Any supplied non-number or negative endpoint invalidates the
                # entire record, even if the opposite endpoint looks comparable.
                low_present = low_type.is_not(None) & (low_type != "null")
                high_present = high_type.is_not(None) & (high_type != "null")
                low_valid = case((low_is_number & (low_amount >= 0), True), else_=False)
                high_valid = case((high_is_number & (high_amount >= 0), True), else_=False)
                endpoints_valid = ~(low_present & ~low_valid) & ~(high_present & ~high_valid)
                # A malformed/inverted band is not a comparable salary. For a valid
                # band, use the highest disclosed endpoint; if max is absent, use min.
                valid_band = ~(low_valid & high_valid) | (low_amount <= high_amount)
                comparable = currency_is_usd & period_is_year & endpoints_valid & (low_valid | high_valid) & valid_band
                disclosed = case((high_valid, high_amount), (low_valid, low_amount), else_=None)
                sortable_salary = case((comparable, disclosed), else_=None)
                statement = statement.order_by(
                    case((comparable, 0), else_=1),
                    sortable_salary.desc().nulls_last() if sort == "salary_high" else sortable_salary.asc().nulls_last(),
                    m.jobs.c.id,
                )
            return [canonical_job_view(dict(row)) for row in connection.execute(statement.limit(limit)).mappings()]

    def job(self, job_id, profile_id=None):
        profile = self.profile(profile_id) if profile_id else None
        with self.engine.connect() as connection:
            data = canonical_job_view(dict(connection.execute(select(m.jobs, m.companies.c.name.label("company"))
                .join(m.companies, m.jobs.c.company_id == m.companies.c.id).where(m.jobs.c.id == job_id)).mappings().one()))
            routes = [dict(row) for row in connection.execute(select(m.routes).where(m.routes.c.job_id == job_id).order_by(m.routes.c.id)).mappings()]
            if not routes:
                raise ValueError("APPLICATION_ROUTE_UNAVAILABLE")
            data["routes"] = routes
            data["provider"] = routes[0]["provider"]
            data["application_url"] = routes[0]["url"]
            resolution = (data.get('attributes') or {}).get('source_resolution') or {}
            if 'selected_application_url' in resolution:
                data['application_url'] = resolution['selected_application_url']
                data['provider'] = data['attributes']['provider']
            match = connection.execute(select(m.matches).where(m.matches.c.job_id == job_id, m.matches.c.profile_id == profile_id,
                m.matches.c.profile_version == (profile["version"] if profile else ""))).mappings().one_or_none()
            data["match"] = dict(match) if match else None
            data["postings"] = []
            data['description_revisions'] = [dict(row) for row in connection.execute(
                select(m.job_description_revisions).where(m.job_description_revisions.c.job_id == job_id)
                .order_by(m.job_description_revisions.c.sequence)).mappings()]
            data['lifecycle_history'] = [dict(row) for row in connection.execute(
                select(m.job_lifecycle_observations).where(m.job_lifecycle_observations.c.job_id == job_id)
                .order_by(m.job_lifecycle_observations.c.observed_at, m.job_lifecycle_observations.c.id)).mappings()]
            for row in connection.execute(select(m.postings).where(m.postings.c.job_id == job_id)).mappings():
                posting = raw_posting_view(dict(row))
                posting["snapshots"] = [dict(snapshot) for snapshot in connection.execute(select(m.posting_snapshots.c.id, m.posting_snapshots.c.sha256,
                    m.posting_snapshots.c.retrieved_at).where(m.posting_snapshots.c.posting_id == row["id"])).mappings()]
                data["postings"].append(posting)
            return data

    def applications(self, profile_id=None):
        with self.engine.connect() as connection:
            statement = select(m.applications, m.jobs.c.title, m.companies.c.name.label("company"), m.routes.c.url.label("application_url")).join(m.jobs,
                m.jobs.c.id == m.applications.c.job_id).join(m.companies, m.companies.c.id == m.jobs.c.company_id).join(m.routes, m.routes.c.id == m.applications.c.route_id)
            if profile_id:
                statement = statement.where(m.applications.c.profile_id == profile_id)
            return [dict(row) for row in connection.execute(statement.order_by(m.applications.c.created_at.desc()).limit(200)).mappings()]

    def count(self, name):
        table = m.metadata.tables[name]
        with self.engine.connect() as connection:
            return connection.execute(select(func.count()).select_from(table)).scalar_one()

    def application(self, application_id):
        with self.engine.connect() as connection:
            return dict(connection.execute(select(m.applications).where(m.applications.c.id == application_id)).mappings().one())

    def transition(self, application_id, expected, state, step=None, confirmation=None):
        with self.engine.begin() as connection:
            row = connection.execute(select(m.applications).where(m.applications.c.id == application_id).with_for_update()).mappings().one()
            if row["state"] not in expected:
                raise ValueError(f"Unsafe application transition from {row['state']}")
            trace = [*row["trace"], *([step] if step else [])]
            values = {"state": state, "trace": trace}
            if confirmation is not None:
                values["confirmation"] = confirmation
            changed = connection.execute(update(m.applications).where(m.applications.c.id == application_id, m.applications.c.state == row["state"]).values(**values))
            if changed.rowcount != 1:
                raise ValueError("Application state changed concurrently")
            self._audit(connection, application_id, state, {"step": step})

    def _audit(self, connection, entity, action, payload):
        connection.execute(m.audit.insert().values(id=uuid4().hex, entity_id=entity, action=action, payload=payload, timestamp=now()))
