"""Explicit recovery from private, durable, original-scope native tree proof."""
from datetime import datetime
import re

from sqlalchemy import select, update
from packages.persistence import models as m
from packages.persistence.store import now
from services.applications.browser_tree_certificate import _identity
from services.applications.browser_tree_ipc import _status, validate_binding


def confirmed_tree_closure(row):
    """Validate stored authority; no public wire proof or PID-derived closure."""
    try:
        owner = row['recovery_metadata']['manual_auth_owner']
        observer = owner['tree_observer']
        api = owner['api_process']
        if (owner.get('api_process_identified') is not True or not _identity(api)
                or not _identity(observer) or observer['host'] != api['host']
                or not isinstance(owner.get('tree_retention'), str)
                or not re.fullmatch('[0-9a-f]{64}', owner['tree_retention'])):
            return False
        binding = validate_binding({'operation_id': row['id'], 'application_id': row['entity_id'],
            'attempt_id': owner['attempt_id'], 'profile_id': owner['profile_id'],
            'owner_digest': owner['token_sha256']})
        proof = owner['tree_closure']
        if not isinstance(proof, dict) or set(proof) != {
                'version', 'binding', 'observer', 'accounting', 'observed_at'}:
            return False
        observed = datetime.fromisoformat(proof['observed_at'])
        return (type(proof['version']) is int and proof['version'] == 1
            and proof['binding'] == binding and proof['observer'] == observer
            and _status(proof['accounting']) == 'EXITED'
            and proof['accounting']['terminated'] == 0 and observed.tzinfo is not None)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def recover_browser_capacity(store, profile_id, application_id, operation_id):
    """Release only an explicitly selected, proven closed, departed owner.

    Closure is not sign-in success. No application, attempt, queue or browser
    action occurs; retry requires a separate request after this transaction.
    """
    from services.applications.browser_process import owner_process_status
    from workers.operations import _lock, OperationActionError

    with store.engine.begin() as connection:
        _lock(connection, 'application_automation')
        application = connection.execute(select(m.applications).where(
            m.applications.c.id == application_id).with_for_update()).mappings().one_or_none()
        if application is None or application['profile_id'] != profile_id:
            raise OperationActionError('BROWSER_RECOVERY_NOT_FOUND')
        row = connection.execute(select(m.operations).where(
            m.operations.c.id == operation_id).with_for_update()).mappings().one_or_none()
        if row is None or row['kind'] != 'MANUAL_AUTH' or row['entity_id'] != application_id:
            raise OperationActionError('BROWSER_RECOVERY_NOT_FOUND')
        if not confirmed_tree_closure(row):
            raise OperationActionError('BROWSER_RECOVERY_NOT_READY')
        owner = row['recovery_metadata']['manual_auth_owner']
        attempt = connection.execute(select(m.attempts).where(
            m.attempts.c.application_id == application_id).order_by(
                m.attempts.c.started_at.desc(), m.attempts.c.id.desc()).limit(1)
        ).mappings().one_or_none()
        if (application['target'] != 'MOCK' or application['state'] != 'AUTH_REQUIRED'
                or owner['profile_id'] != profile_id or attempt is None
                or attempt['id'] != owner['attempt_id'] or attempt['state'] != 'AUTH_REQUIRED'):
            raise OperationActionError('BROWSER_RECOVERY_SCOPE_CHANGED')
        result = {'status': 'BROWSER_CAPACITY_RECOVERED', 'operation_id': operation_id,
                  'application_id': application_id, 'attempt_id': owner['attempt_id']}
        if row['state'] == 'FAILED' and row['error'] == 'BROWSER_OWNER_RECOVERED':
            return result
        held = row['state'] == 'RUNNING' or (row['state'] == 'BLOCKED'
            and row['error'] == 'BROWSER_RELEASE_UNCONFIRMED')
        if (not held
                or owner_process_status(owner['api_process']) != 'EXITED'):
            raise OperationActionError('BROWSER_RECOVERY_NOT_READY')
        timestamp = now()
        connection.execute(update(m.operations).where(m.operations.c.id == operation_id,
            m.operations.c.state == row['state'], m.operations.c.error == row['error']
        ).values(state='FAILED', error='BROWSER_OWNER_RECOVERED', lease_expires_at=None,
                 completed_at=timestamp, updated_at=timestamp, current_step='FINALIZING'))
        store._audit(connection, operation_id, 'MANUAL_AUTH_BROWSER_RECOVERED',
                     {'profile_id': profile_id, 'attempt_id': owner['attempt_id']})
        return result
