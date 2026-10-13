"""Durable advisory scoring on the existing outbox, with no uncertain call replay."""
import hashlib
import json
import re
import time

from sqlalchemy import insert, select, update

from packages.persistence import models as m
from packages.persistence.store import identity, now
from services.ai.consumer import AIJobInput, AIJobEvaluation, _prompt
from services.ai.cost_controls import AICostPolicy
from services.ai.tasks import AITask, AITaskConfiguration, AITaskRouter

KIND = 'AI_EVALUATE_QUEUED'
EVALUATOR_VERSION = 1


def _digest(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


class DurableEvaluationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def status(row):
    return {key: row[key] for key in ('state', 'current_step', 'progress', 'error',
            'updated_at', 'completed_at', 'attempt_count', 'retry_count', 'cancel_requested')} | {
        'operation_id': row['id'], 'request_id': row['params']['request_id']}


def queue_evaluation(store, profile_id, request_id, search_profile_id=None, preview_fingerprint=None):
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', request_id):
        raise DurableEvaluationError('AI_EVALUATION_REQUEST_INVALID')
    policy = AICostPolicy(**(store.ai_cost_policy(profile_id) or {}))
    configuration = AITaskConfiguration.from_mapping(store.ai_task_settings(profile_id))
    selection = configuration.for_task(AITask.JOB_SCORING)
    from services.ai.selected_search_candidates import build_inputs
    snapshot, plan, _ = build_inputs(store, profile_id, policy, configuration, search_profile_id)
    if search_profile_id is not None and preview_fingerprint is None:
        raise DurableEvaluationError('AI_EVALUATION_PREVIEW_REQUIRED')
    if selection.provider == 'disabled':
        raise DurableEvaluationError('AI_PROVIDER_DISABLED')
    if preview_fingerprint is not None and preview_fingerprint != _digest(snapshot):
        raise DurableEvaluationError('AI_EVALUATION_PREVIEW_CHANGED')
    operation_id = identity(KIND, profile_id, request_id)
    # Only identifiers and a digest are public operation metadata. Job text and
    # checkpoints live in an owner-scoped table, never generic operation views.
    params = {'profile_id': profile_id, 'request_id': request_id, 'snapshot_digest': _digest(snapshot)}
    with store.engine.begin() as connection:
        if connection.dialect.name == 'sqlite':
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        elif connection.dialect.name == 'postgresql':
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        else:
            raise DurableEvaluationError('AI_EVALUATION_DATABASE_UNSUPPORTED')
        inserted = connection.execute(dialect_insert(m.operations).values(
            id=operation_id, kind=KIND, entity_id=profile_id, params=params,
            state='QUEUED', updated_at=now(), error=None,
        ).on_conflict_do_nothing(index_elements=[m.operations.c.id]).returning(m.operations.c.id)).scalar_one_or_none()
        row = connection.execute(select(m.operations).where(m.operations.c.id == operation_id)
                                 .with_for_update()).mappings().one()
        if row['kind'] != KIND or row['entity_id'] != profile_id or row['params'] != params:
            raise DurableEvaluationError('AI_EVALUATION_IDENTITY_CONFLICT')
        if inserted:
            connection.execute(insert(m.ai_evaluation_batches).values(
                id=operation_id, profile_id=profile_id, snapshot=snapshot,
                candidate_states={job_id: {'state': 'PENDING'} for job_id in plan.ai_candidate_ids}, created_at=now()))
        return {**status(row), 'replayed': inserted is None}


def owned_operation(store, profile_id, operation_id):
    with store.engine.connect() as connection:
        row = connection.execute(select(m.operations).where(m.operations.c.id == operation_id,
            m.operations.c.kind == KIND, m.operations.c.entity_id == profile_id)).mappings().one_or_none()
    if row is None:
        raise DurableEvaluationError('AI_EVALUATION_NOT_FOUND')
    return dict(row)


def read_evaluation(store, profile_id, operation_id):
    row = owned_operation(store, profile_id, operation_id)
    response = status(row)
    if row['state'] == 'COMPLETED':
        from services.ai.selected_search_candidates import evaluation_history
        runs = evaluation_history(store, profile_id, run_id=operation_id)
        result = next((run for run in runs if run['run_id'] == operation_id), None)
        if result is None:
            raise DurableEvaluationError('AI_EVALUATION_RESULT_UNAVAILABLE')
        response['result'] = {**result, 'documents_created': 0, 'document_candidate_ids': [],
                              'deterministic_matches_unchanged': True}
    return response


def _fence(connection, operation):
    row = connection.execute(select(m.operations).where(m.operations.c.id == operation['id'])
                             .with_for_update()).mappings().one()
    if (row['state'] != 'RUNNING' or row['cancel_requested'] or
            row['attempt_count'] != operation['attempt_count'] or
            not row['lease_expires_at'] or row['lease_expires_at'] <= now()):
        from workers.operations import OperationCancelled
        raise OperationCancelled('AI evaluation no longer owns its attempt')
    return row


def evaluate_for_operation(store, operation, check):
    with store.engine.connect() as connection:
        batch = connection.execute(select(m.ai_evaluation_batches).where(
            m.ai_evaluation_batches.c.id == operation['id'],
            m.ai_evaluation_batches.c.profile_id == operation['entity_id'])).mappings().one()
    snapshot = batch['snapshot']
    if (snapshot.get('evaluator_version') != EVALUATOR_VERSION
            or _digest(snapshot) != operation['params']['snapshot_digest']):
        raise DurableEvaluationError('AI_EVALUATION_INPUT_CHANGED')
    router = AITaskRouter(AITaskConfiguration.from_mapping(snapshot['configuration']))
    deadline = time.monotonic() + 120
    def remaining():
        seconds = min(check.remaining_seconds(), deadline - time.monotonic())
        if seconds <= 0:
            raise DurableEvaluationError('AI_TASK_TIMEOUT')
        return seconds
    class Cancellation:
        def is_set(self):
            from workers.operations import OperationCancelled, OperationTimedOut
            try:
                remaining()
                return False
            except (OperationCancelled, OperationTimedOut, DurableEvaluationError):
                return True
    evaluations = []
    for index, raw in enumerate(snapshot['candidates']):
        remaining()
        candidate = AIJobInput(**raw)
        with store.engine.begin() as connection:
            if connection.dialect.name == 'sqlite':
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            _fence(connection, operation)
            current_version = connection.execute(select(m.profiles.c.version).where(
                m.profiles.c.id == operation['entity_id']).with_for_update()).scalar_one()
            if current_version != snapshot['profile_version']:
                raise DurableEvaluationError('AI_EVALUATION_PROFILE_CHANGED')
            states = dict(connection.execute(select(m.ai_evaluation_batches.c.candidate_states).where(
                m.ai_evaluation_batches.c.id == operation['id'])).scalar_one())
            checkpoint = states[candidate.job_id]
            if checkpoint['state'] not in ('PENDING', 'CALL_STARTED', 'COMPLETED'):
                raise DurableEvaluationError('AI_EVALUATION_CHECKPOINT_INVALID')
            if checkpoint['state'] == 'CALL_STARTED':
                raise DurableEvaluationError('AI_PROVIDER_OUTCOME_UNKNOWN')
            if checkpoint['state'] == 'PENDING':
                states[candidate.job_id] = {'state': 'CALL_STARTED'}
                connection.execute(update(m.ai_evaluation_batches).where(
                    m.ai_evaluation_batches.c.id == operation['id']).values(candidate_states=states))
                connection.execute(update(m.operations).where(m.operations.c.id == operation['id']).values(
                    current_step='EVALUATING_CANDIDATES', progress=5 + int(85 * index / max(1, len(states))), updated_at=now()))
        if checkpoint['state'] == 'COMPLETED':
            evaluation = AIJobEvaluation.model_validate(checkpoint['result'])
        else:
            # Once CALL_STARTED is durable, a lost response is uncertain. A
            # later attempt never guesses whether the provider charged for it.
            from services.ai.providers import AIProviderError
            try:
                evaluation = router.generate_structured(task=AITask.JOB_SCORING,
                    prompt=_prompt(candidate), timeout_seconds=min(30, remaining()),
                    result_model=AIJobEvaluation, cancellation_event=Cancellation())
            except AIProviderError as error:
                if error.code == 'AI_PROVIDER_CANCELLED':
                    # Distinguish the persisted user cancellation from an
                    # expired operation/batch budget before classifying it.
                    remaining()
                raise
            if not isinstance(evaluation, AIJobEvaluation):
                raise DurableEvaluationError('AI_STRUCTURED_OUTPUT_INVALID')
            with store.engine.begin() as connection:
                if connection.dialect.name == 'sqlite':
                    connection.exec_driver_sql('BEGIN IMMEDIATE')
                _fence(connection, operation)
                states = dict(connection.execute(select(m.ai_evaluation_batches.c.candidate_states).where(
                    m.ai_evaluation_batches.c.id == operation['id'])).scalar_one())
                states[candidate.job_id] = {'state': 'COMPLETED', 'result': evaluation.model_dump()}
                connection.execute(update(m.ai_evaluation_batches).where(
                    m.ai_evaluation_batches.c.id == operation['id']).values(candidate_states=states))
        remaining()
        evaluations.append({'job_id': candidate.job_id, 'title': candidate.title, 'company': candidate.company,
            'deterministic_score': candidate.deterministic_score, **evaluation.model_dump()})
    return snapshot, evaluations


def complete_evaluation(store, connection, operation, result):
    snapshot, evaluations = result
    selection = snapshot['configuration']['job_scoring']
    return store.save_ai_evaluation_run(operation['entity_id'], snapshot['profile_version'],
        snapshot['policy'], selection['provider'], selection['model'], evaluations,
        connection=connection, run_id=operation['id'])
