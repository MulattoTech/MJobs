"""Bounded notification scans independent of discovery/application work."""
from __future__ import annotations

from services.notifications.contract import DeliveryResult
from services.notifications.events import read_event_page


def run_once(store, config, journal, provider):
    if not config.enabled:
        return {"enabled": False, "queued": 0, "attempted": 0}
    activated_at, cursor = journal.scan_position()
    events, next_cursor = read_event_page(
        store, config.profile_id, activated_at=activated_at,
        after_id=cursor, limit=config.batch_size,
    )
    queued = journal.enqueue_page(events, expected_cursor=cursor, next_cursor=next_cursor)
    # At most one POST per tick. The journal also reserves a global one-second
    # admission gap across instances sharing this exact state file, not a
    # guarantee about actual wire-request spacing under process descheduling.
    claimed = journal.claim_next()
    attempted = 0
    outcome = None
    if claimed is not None:
        event, token = claimed
        attempted = 1
        try:
            result = provider.send(event)
        except Exception:
            # An unexpected failure may occur after server persistence.
            # Never expose the provider exception or automatically replay.
            result = DeliveryResult("UNKNOWN", "NOTIFICATION_PROVIDER_UNCERTAIN")
        journal.finish(event.event_key, token, result)
        outcome = result.code
    journal.mark_tick()
    return {"enabled": True, "queued": queued, "attempted": attempted, "outcome": outcome}
