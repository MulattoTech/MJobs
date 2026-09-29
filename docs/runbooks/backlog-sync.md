# Backlog synchronization

Run python bootstrap-tools/sync_backlog.py --repo MulattoTech/MJobs.
The tool creates missing marked issues and maps existing ones. It preserves
existing issue text rather than replacing newer work. Seed drift requires
review; create-only synchronization does not certify acceptance or completion.

The local mutation lock prevents two sync processes in this checkout. Never
start another sync from another host while this owner is active. If a crash
leaves a lock, --recover-stale-lock queries whether its recorded process is
dead and reclaims only a provably dead owner. Unreadable ownership is a blocker.

The map checkpoints every acknowledged creation. A pending_creation after a
failed request is ambiguous: reconcile the marker from GitHub before retrying.
If GitHub contains the marker, the next run recovers it. Otherwise inspect the
specific request and remote issue inventory before explicitly clearing the
pending marker. Do not blind-retry an ambiguous POST or bypass rate limits.
