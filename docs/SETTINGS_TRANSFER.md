# Settings transfer

In Profile, download settings JSON, select it on the target installation, review
the category counts, and confirm replacement of the selected profile's settings.
Career facts, documents, credentials, source history, and application history are
outside this transfer.

The version 1 document transfers saved-search criteria, discovery schedules, and
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

Legacy version 1 files with only source IDs require those sources to exist on
the target. Legacy per-search `scheduler` fields remain accepted and exported.
When both scheduler representations occur, they must agree. Invalid rules,
unknown fields, missing sources, policy conflicts, and malformed search criteria
reject the entire import. No partial source creation or profile replacement is
committed. Re-export after import or reload to verify the saved configuration.
