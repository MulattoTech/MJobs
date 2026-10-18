"""Reviewed company-feed limits; independent of global collector policy."""
from dataclasses import asdict, dataclass
from datetime import timedelta
import hashlib


@dataclass(frozen=True)
class MonitoringPolicy:
    request_timeout: int = 10
    rate_limit: int = 60
    concurrency: int = 1
    jitter_seconds: int = 0

    def __post_init__(self):
        # At 15 rpm, twenty fast request slots plus one 10s response fit
        # the 90s client cap. Lower rates can starve mandatory fetches/probes.
        for key, low, high in (('request_timeout', 2, 10), ('rate_limit', 15, 60),
                               ('concurrency', 1, 8), ('jitter_seconds', 0, 300)):
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError('MONITOR_POLICY_INVALID')

    def as_dict(self):
        return asdict(self)

    @classmethod
    def from_row(cls, row):
        return cls(**{key: row[key] for key in cls.__dataclass_fields__ if key in row})


def next_due(monitor, stamp, *, failures=0, interval=None):
    """Persist this result once; restarts never redraw or shorten the base delay."""
    base = monitor['poll_interval']
    # Retry's one-day ceiling must never shorten a reviewed normal cadence.
    delay = max(base, min(86400, base * 2 ** min(failures, 5))) if interval is None else interval
    jitter = monitor.get('jitter_seconds', 0)
    seed = f"{monitor['id']}:{monitor['revision']}:{stamp.isoformat()}:{delay}"
    extra = int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], 'big') % (jitter + 1)
    return stamp + timedelta(seconds=delay + extra)
