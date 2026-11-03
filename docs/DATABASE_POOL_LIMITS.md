# Database pool capacity waits

Store queue pools allow a caller to wait up to **one second** for a free
connection. The existing capacity remains five persistent connections plus ten
overflow connections per Store engine. This policy applies to every Store
caller, including ordinary reads, writes, and worker control checks.

When all connections remain occupied, a control check reports
`OPERATION_CONTROL_UNAVAILABLE`. An unavailable authority read does not establish
that the user cancelled an operation and does not authorize further work.
Existing caller transactions and checked-out connections retain their ownership.
If capacity returns within the wait limit, the waiting caller acquires a
connection and reads current durable authority normally.

Worker tests release their fixture-held connections before finalization, then
verify that a reopened operation is BLOCKED with the fixed unavailable error,
without a cancellation flag, retry, or successful side effect. Finalization while
the pool remains exhausted is **not** established by those tests.

The timeout is selected once at engine construction using SQLAlchemy's supported
`pool_timeout` option. It does not mutate a shared pool around individual requests,
add background connection threads, increase capacity, or automatically retry a
write. SQLite memory URLs retain the pool class selected by the pinned dialect
and their existing database identity and transaction ownership.

## Separate limits and remaining work

The one-second limit covers **waiting for pool capacity**. It is not a total
deadline for `engine.connect()`: new connection establishment, DNS, pre-ping,
reconnection, network black holes, rollback/reset, and arbitrary callbacks require
separate treatment. PostgreSQL's configured connect timeout and SQLite's normal
15-second busy timeout are unchanged. Control SQL keeps its separate 100 ms limit.

Under sustained overload, ordinary requests may fail sooner than the previous
30-second capacity wait. Investigate held/slow transactions and connection leaks;
do not interpret the shorter wait as added database capacity or permission to
replay a non-idempotent request.

MJ-REQ-004 remains PARTIAL. Tests use isolated file-backed SQLite and a disposable
PostgreSQL schema, with real saturation of all fifteen pool slots, bounded joined
fixture threads, preserved uncommitted writes, pool recreation, recovery after
capacity returns, and three SQLite memory URL forms.

Reference: [SQLAlchemy connection pooling](https://docs.sqlalchemy.org/en/21/core/pooling.html).
