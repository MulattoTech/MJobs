# Settings transfer

In Profile, download settings JSON, select it on the target installation, review
the category counts, and confirm replacement of the selected profile's settings.
Career facts, documents, credentials, source history, and application history are
outside this transfer.

The version 2 document transfers saved-search criteria, discovery schedules, and
public source configuration. Each `scheduled_search` automation rule references
a saved search by name and has a boolean `enabled` plus an integer
`interval_seconds` from 300 through 604800. These are the existing durable
discovery schedules; they cannot submit applications. The search and its source
must also be enabled before the scheduler can enqueue discovery.

Each source association carries `source_ids` and a `sources` list with canonical
`id`, `provider`, `tenant`, `enabled`, `poll_interval`, `concurrency`,
`request_timeout`, `rate_limit`, and `jitter_seconds`. Admission reuses the source
configuration validator without provider traffic. Missing sources are created
in the same transaction as profile replacement. Existing shared sources must
match every imported policy field, so an import never overwrites their settings
or affects another profile. Review a mismatch in Sources before trying again.

Version 2 defines schedules only in `automation_rules`, avoiding duplicate
definitions. Version 1 files are migrated when you confirm import; per-search
`scheduler` fields and automation rules are accepted, and when both occur they
must agree. Re-export produces version 2 without changing the schedule. Files
with only source IDs require those sources to exist on the target. Unsupported
versions are rejected; changing a version number by hand is not a migration.

Invalid rules,
unknown fields, missing sources, policy conflicts, and malformed search criteria
reject the entire import. No partial source creation or profile replacement is
committed. Re-export after import or reload to verify the saved configuration.
