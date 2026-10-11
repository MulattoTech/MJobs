"""Owner-scoped comparison admission and readback on the existing worker outbox."""
import re
from sqlalchemy import insert, select, update
from sqlalchemy.exc import NoResultFound
from packages.persistence import models as m
from packages.persistence.store import identity, now
from services.documents.comparison import ComparisonUnavailable, read_generated_snapshot, tailored_context
from services.documents.history import application_documents
from services.documents.uploads import UploadIntegrityError, read_resume
from services.documents.uploaded_comparison import compare_uploaded_resume
from services.documents import tailor

KIND = 'UPLOADED_RESUME_COMPARE_QUEUED'
COMPARATOR_VERSION = 1


class DurableComparisonError(ValueError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def inputs(store, output_dir, profile_id, application_id, base_revision_id, tailored_document_id):
    """Bind immutable revisions and validate bytes without extracting the upload."""
    try:
        if store.application(application_id)['profile_id'] != profile_id:
            raise DurableComparisonError('COMPARISON_NOT_FOUND')
        with store.engine.connect() as connection:
            base = connection.execute(select(m.resume_revisions).where(
                m.resume_revisions.c.id == base_revision_id,
                m.resume_revisions.c.profile_id == profile_id,
                m.resume_revisions.c.document_kind == 'BASE_RESUME',
            )).mappings().one_or_none()
        target = next((row for row in application_documents(store, application_id)
                       if row['id'] == tailored_document_id), None)
        if base is None or target is None or target.get('profile_id') != profile_id:
            raise DurableComparisonError('COMPARISON_NOT_FOUND')
        profile = store.profile(profile_id, target['profile_version'])
        tailored_context(target, tailor.eligible_rows(profile))
        read_generated_snapshot(store, output_dir, target)
        upload, _ = read_resume(store, output_dir, profile_id, base['upload_id'])
        return {'profile_id': profile_id, 'application_id': application_id,
            'base_revision_id': base_revision_id, 'base_version': base['version'],
            'upload_id': upload['id'], 'upload_sha256': upload['sha256'],
            'tailored_document_id': tailored_document_id, 'target_version': target['version'],
            'target_sha256': target['sha256'], 'target_profile_version': target['profile_version'],
            'comparator_version': COMPARATOR_VERSION}
    except DurableComparisonError:
        raise
    except NoResultFound:
        raise DurableComparisonError('COMPARISON_NOT_FOUND') from None
    except ComparisonUnavailable as error:
        raise DurableComparisonError('COMPARISON_INPUT_UNAVAILABLE', str(error)) from None
    except (UploadIntegrityError, OSError, ValueError, KeyError, TypeError):
        raise DurableComparisonError('COMPARISON_INPUT_UNAVAILABLE') from None


def status(operation):
    return {key: operation[key] for key in ('state', 'current_step', 'progress', 'error',
            'updated_at', 'completed_at', 'attempt_count', 'retry_count')} | {
        'operation_id': operation['id'], 'request_id': operation['params']['request_id']}


def queue_comparison(store, output_dir, profile_id, application_id, base_revision_id,
                     tailored_document_id, request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', request_id):
        raise DurableComparisonError('COMPARISON_REQUEST_INVALID')
    params = {**inputs(store, output_dir, profile_id, application_id, base_revision_id,
                       tailored_document_id), 'request_id': request_id, 'queued_at': now()}
    operation_id = identity(KIND, profile_id, {'request_id': request_id})
    with store.engine.begin() as connection:
        if connection.dialect.name == 'sqlite':
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        elif connection.dialect.name == 'postgresql':
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        else:
            raise DurableComparisonError('COMPARISON_DATABASE_UNSUPPORTED')
        inserted = connection.execute(dialect_insert(m.operations).values(
            id=operation_id, kind=KIND, entity_id=application_id, params=params,
            state='QUEUED', error=None, updated_at=now(),
        ).on_conflict_do_nothing(index_elements=[m.operations.c.id]))
        current = connection.execute(select(m.operations).where(
            m.operations.c.id == operation_id).with_for_update()).mappings().one()
        if (current['kind'] != KIND or current['entity_id'] != application_id
                or {k:v for k,v in current['params'].items() if k != 'queued_at'} != {k:v for k,v in params.items() if k != 'queued_at'}):
            raise DurableComparisonError('COMPARISON_IDENTITY_CONFLICT')
        return {**status(current), 'replayed': inserted.rowcount == 0}


def owned_operation(store, profile_id, application_id, operation_id):
    with store.engine.connect() as connection:
        operation = connection.execute(select(m.operations).where(
            m.operations.c.id == operation_id, m.operations.c.kind == KIND,
            m.operations.c.entity_id == application_id,
        )).mappings().one_or_none()
    if (operation is None or not isinstance(operation['params'], dict)
            or operation['params'].get('profile_id') != profile_id
            or store.application(application_id)['profile_id'] != profile_id):
        raise DurableComparisonError('COMPARISON_NOT_FOUND')
    return dict(operation)


def read_comparison(store, output_dir, profile_id, application_id, base_revision_id,
                    tailored_document_id, operation_id=None):
    expected = inputs(store, output_dir, profile_id, application_id, base_revision_id, tailored_document_id)
    if operation_id is None:
        with store.engine.connect() as connection:
            operation_id = connection.execute(select(m.operations.c.id).where(
                m.operations.c.kind == KIND, m.operations.c.entity_id == application_id,
                m.operations.c.params['profile_id'].as_string() == profile_id,
                m.operations.c.params['base_revision_id'].as_string() == base_revision_id,
                m.operations.c.params['tailored_document_id'].as_string() == tailored_document_id,
            ).order_by(m.operations.c.params['queued_at'].as_string().desc(), m.operations.c.id.desc()).limit(1)).scalar_one_or_none()
    operation = owned_operation(store, profile_id, application_id, operation_id)
    if {key: value for key, value in operation['params'].items() if key not in ('request_id', 'queued_at')} != expected:
        raise DurableComparisonError('COMPARISON_INPUT_CHANGED')
    response = status(operation)
    if operation['state'] == 'COMPLETED':
        with store.engine.connect() as connection:
            result = connection.execute(select(m.uploaded_comparison_results.c.result).where(
                m.uploaded_comparison_results.c.id == operation_id,
                m.uploaded_comparison_results.c.profile_id == profile_id,
                m.uploaded_comparison_results.c.application_id == application_id,
            )).scalar_one_or_none()
        if result is None:
            raise DurableComparisonError('COMPARISON_RESULT_UNAVAILABLE')
        response['result'] = result
    return response


class _CancellationCheck:
    def __init__(self, check):
        self.check = check

    def is_set(self):
        self.check()
        return False


def compare_for_operation(store, output_dir, operation, check):
    params = operation['params']
    if (operation['kind'] != KIND or operation['entity_id'] != params.get('application_id')
            or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', params.get('request_id', ''))):
        raise DurableComparisonError('COMPARISON_OPERATION_INVALID')
    def stage(step, progress):
        check()
        with store.engine.begin() as connection:
            changed = connection.execute(update(m.operations).where(
                m.operations.c.id == operation['id'], m.operations.c.kind == KIND,
                m.operations.c.state == 'RUNNING', m.operations.c.cancel_requested.is_(False),
                m.operations.c.attempt_count == operation['attempt_count'],
                m.operations.c.lease_expires_at > now(),
            ).values(current_step=step, progress=progress, updated_at=now()))
            if changed.rowcount != 1:
                from workers.operations import OperationCancelled
                raise OperationCancelled('Comparison no longer owns its attempt')
    stage('VALIDATING_INPUTS', 5)
    expected = inputs(store, output_dir, params['profile_id'], params['application_id'],
                      params['base_revision_id'], params['tailored_document_id'])
    if {key: value for key, value in params.items() if key not in ('request_id', 'queued_at')} != expected:
        raise DurableComparisonError('COMPARISON_INPUT_CHANGED')
    check()
    stage('EXTRACTING_AND_COMPARING', 20)
    result = compare_uploaded_resume(store, output_dir, params['application_id'],
        params['base_revision_id'], params['tailored_document_id'], _CancellationCheck(check))
    check()
    stage('VALIDATING_RESULT', 90)
    if inputs(store, output_dir, params['profile_id'], params['application_id'],
              params['base_revision_id'], params['tailored_document_id']) != expected:
        raise DurableComparisonError('COMPARISON_INPUT_CHANGED')
    return result


def complete_result(connection, operation, result):
    """Insert only inside the worker's fenced completion transaction."""
    current = connection.execute(select(m.operations).where(
        m.operations.c.id == operation['id']).with_for_update()).mappings().one()
    if (current['state'] != 'COMPLETED' or current['cancel_requested']
            or current['attempt_count'] != operation['attempt_count'] or current['kind'] != KIND):
        return False
    connection.execute(insert(m.uploaded_comparison_results).values(
        id=operation['id'], profile_id=operation['params']['profile_id'],
        application_id=operation['params']['application_id'], result=result, created_at=now()))
    return True
