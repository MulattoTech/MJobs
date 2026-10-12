"""Compose original-scope private host launch and creator-independent tree proof.

This internal service never releases capacity or changes application history.
Its caller must finish the owning operation or leave uncertain capacity held.
"""
import json
from copy import deepcopy
from pathlib import Path
import sys
from time import monotonic, sleep

from sqlalchemy import select

from packages.persistence import models as m
from services.applications.browser_tree import BrowserTreeJob
from services.applications.browser_tree_certificate import _scope
from services.applications.browser_tree_ipc import BrowserTreeObserver, _status
from services.applications.manual_auth import (
    _resolve_auth_attempt, _manual_login_url, MANUAL_AUTH_WINDOW_CLOSED,
    OPEN_UNAVAILABLE, STALE,
)
from services.discovery.pipeline import MockClient


def _confirm_unlaunched_rollback(store, operation_id, owner_token, binding, observer, query):
    """Confirm the acknowledged observer's durable compensation, never a launch."""
    try:
        def current():
            with store.engine.connect() as connection:
                return connection.execute(select(m.operations).where(
                    m.operations.c.id == operation_id)).mappings().one()
        before = current()
        if _scope(before, owner_token) != binding or before['state'] != 'RUNNING':
            return False
        expected = deepcopy(before['recovery_metadata'])
        owner = expected['manual_auth_owner']
        if (owner.get('tree_observer') != observer.identity
                or not isinstance(owner.get('tree_retention'), str)
                or len(owner['tree_retention']) != 64
                or any(value not in '0123456789abcdef' for value in owner['tree_retention'])
                or 'tree_closure' in owner
                or query.accounting() != {'total': 0, 'active': 0, 'terminated': 0}):
            return False
        del owner['tree_observer']
        del owner['tree_retention']
        observer.close()
        after = current()
        return (_scope(after, owner_token) == binding and after['state'] == 'RUNNING'
                and after['recovery_metadata'] == expected
                and query.accounting() == {'total': 0, 'active': 0, 'terminated': 0})
    except Exception:
        return False


def open_isolated_manual_auth_window(*, store, operation_id, application_id,
                                    attempt_id, mock_origin, output_dir,
                                    owner_token, budget_seconds, check=None):
    """Enroll outside the Job before its only private fixture host launch."""
    if type(budget_seconds) is not int or not 1 <= budget_seconds <= 900:
        return STALE
    with store.engine.connect() as connection:
        row = connection.execute(select(m.operations).where(
            m.operations.c.id == operation_id)).mappings().one_or_none()
    binding = _scope(row, owner_token)
    if (binding is None or row['state'] != 'RUNNING'
            or binding['application_id'] != application_id
            or binding['attempt_id'] != attempt_id):
        return STALE
    resolved = _resolve_auth_attempt(store, application_id, attempt_id, output_dir)
    if resolved is None:
        return STALE
    if sys.platform != 'win32':
        return OPEN_UNAVAILABLE
    route, _ = resolved
    job = query = observer = client = None
    launch_attempted = False
    try:
        client = MockClient(mock_origin, check=check)
        if not client.allowed(route['url']) or _manual_login_url(mock_origin, route['requisition'], client) is None:
            return STALE
        if check is not None:
            check()
        payload = json.dumps({'schema': 1, 'output_dir': str(Path(output_dir).resolve()),
            'application_id': application_id, 'mock_origin': mock_origin,
            'requisition': route['requisition'], 'budget_seconds': budget_seconds}).encode('utf-8')
        if len(payload) > 2048:
            return STALE
        job = BrowserTreeJob()
        query = job.query_handle()
        observer = BrowserTreeObserver(query, binding, store=store, owner_token=owner_token)
        if check is not None:
            check()
        # Repo-owned module and installed runtime only. No API secrets/profile/
        # operation token enter command arguments or the host environment.
        bootstrap = ('import site,runpy;site.addsitedir(' +
            repr(str(Path(sys.prefix) / 'Lib/site-packages')) +
            ");import sys;sys.argv=['host'];runpy.run_module('services.applications.manual_auth_host',run_name='__main__')")
        launch_attempted = True
        job.launch([sys._base_executable, '-c', bootstrap],
                         cwd=str(Path(__file__).resolve().parents[2]), private_input=payload)
        deadline = monotonic() + budget_seconds + 20
        while monotonic() < deadline:
            if check is not None:
                check()
            code = job.host_exit_code()
            if code != 259:  # STILL_ACTIVE is never closure evidence.
                reply = observer.observe(binding)
                if (code == 0 and reply.get('binding') == binding
                        and reply.get('status') == 'EXITED'
                        and _status(reply.get('accounting')) == 'EXITED'
                        and reply['accounting']['terminated'] == 0):
                    return MANUAL_AUTH_WINDOW_CLOSED
                # The host may exit before its final browser descendant. Keep
                # the original admission until fresh full-tree proof arrives,
                # within the same cancellation/deadline checks and time bound.
                if (code == 0 and reply.get('binding') == binding
                        and reply.get('status') == 'LIVE'
                        and _status(reply.get('accounting')) == 'LIVE'
                        and reply['accounting']['terminated'] == 0):
                    sleep(.05)
                    continue
                return 'CLOSE_UNCONFIRMED'
            sleep(.05)
        return 'CLOSE_UNCONFIRMED'
    except Exception:
        if job is None:
            return OPEN_UNAVAILABLE
        if (not launch_attempted and observer is not None
                and _confirm_unlaunched_rollback(store, operation_id, owner_token,
                    binding, observer, query)):
            return OPEN_UNAVAILABLE
        # Failed ACK/launch or unconfirmed rollback retains capacity.
        return 'CLOSE_UNCONFIRMED'
    finally:
        for resource in (observer, query, job, client):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
