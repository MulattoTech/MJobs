# Maintainer reference: Windows browser tree containment

**Pinned source:** services/applications/browser_tree.py
**Source commit:** 2f01b876762fcbdd70f11e69865a48257503af93
**Source blob:** 4ec065d06eab5a91635a06242713b53f0bd75a7d

This is a code-oriented reference to the pinned source, not a product acceptance claim. The module provides Windows process-tree launch containment and query handles. It explicitly does **not** release operation capacity; a separate recovery controller must bind observations to a durable operation/attempt/owner fence. Handle closure does not terminate a process; losing the last proof handle means the state is unknown (source lines 1–5).

## Public API and lifecycle

### BrowserTreeJob

Constructing BrowserTreeJob() loads the Windows kernel API and creates an unnamed Job Object (lines 213–230). On non-Windows, or when this native setup fails, construction raises ValueError("TREE_CONTAINMENT_UNAVAILABLE"); native details are suppressed. The Job Object is private/non-inherited. This type has no kill-on-close behavior.

Before launch, call query_handle() to duplicate a QUERY-only handle and retain it separately (lines 232–243). It returns a BrowserTreeQuery; failure is normalized to ValueError("TREE_QUERY_UNAVAILABLE"). The independent query handle is the retained tree observer; do not substitute a root PID.

launch(argv, *, cwd=None, private_input=None) accepts a nonempty list/tuple of NUL-free strings whose first element is an absolute executable path. Optional cwd is passed to CreateProcessW as a string; the function does not resolve or validate it. private_input, if provided, must be exactly bytes, length 1–2048; other values fail. The method returns the created host PID. The PID is an identifier, not retained identity.

Important retry boundary: _attempted is set before argument validation/native launch (lines 245–262). Any failed or ambiguous launch attempt consumes the one allowed attempt; callers must not replay it through the same job. Launch exceptions caught by the method are sanitized as ValueError("TREE_LAUNCH_UNAVAILABLE"), without executable, profile, or native error details.

The launch is admitted to the Job Object in the CreateProcessW call through PROC_THREAD_ATTRIBUTE_JOB_LIST, rather than assigned after process creation (lines 264–301). The host environment is an explicit allowlist of SYSTEMROOT, WINDIR, TEMP, TMP, LOCALAPPDATA, and USERPROFILE; it excludes PATH, proxies, import overrides, and other application variables (lines 18–27, 291–300). No inherited handles are enabled by default.

### BrowserTreeQuery

- accounting() returns {"total": int, "active": int, "terminated": int} from Job Object accounting, or None when the handle/API query is unavailable (lines 142–150).
- status() returns UNKNOWN when accounting is unavailable or the Job Object has no accounting members; otherwise LIVE when active is nonzero and EXITED when it is zero (lines 175–179). Treat UNKNOWN as unknown, not as process exit.
- contains(pid) accepts only an exact integer in 1…2³²−1. It returns True/False when process-open and job-membership queries work, otherwise None; it closes the temporary process handle (lines 181–194).
- limit_flags() returns the queried Job Object flags or None on unavailable handle/query (lines 196–204).
- duplicate_to(process_handle) requires an already-open target process handle represented as an exact positive integer within pointer width. It returns the duplicated target-local QUERY handle; on any error it raises ValueError("TREE_QUERY_TRANSFER_UNAVAILABLE") (lines 152–173). The caller must keep the target process handle open during transfer, deliver the handle through private authenticated IPC, and wait for recipient acknowledgement before depending on retained proof. This method does not consume the source handle or certify closure.
- close() is idempotent and closes the retained query handle only (lines 206–210).

BrowserTreeJob.host_exit_code() queries the original retained process handle, returning None when absent/query fails and the Windows exit-code value otherwise; 259 means the host is still active (lines 319–327). It reports the original host, not the entire descendant tree. BrowserTreeJob.close() closes its retained host and Job Object handles; it does not kill the host or descendants (lines 329–336).

## Private stdin transport

When private_input is supplied, _private_input_handles writes the bounded input fully to an anonymous pipe and closes the writer before launch, so the child receives EOF after the preloaded bytes; the child inherits the pipe read end and NUL device for stdin/stdout/stderr through an explicit handle list (lines 93–130, 281–289). This helper transports bytes; the dedicated host is responsible for validating the input schema (lines 95–103). Because Windows requires these handles to be briefly inheritable, other concurrent process launchers must use close_fds=True or their own explicit handle list (lines 100–103). Do not use this API as proof that another launcher is isolated.

## Safe fixture pattern

The test suite demonstrates the short-lived-host pattern: create the job, obtain a separate query handle, launch a child with an absolute executable, wait until the query reports EXITED, then close query and job in finally (see tests/unit/test_browser_host_status.py:44–68; the related containment fixture is tests/unit/test_browser_tree_containment.py:198–240). A production recovery observer must retain/transfer and acknowledge query authority before relying on tree evidence; merely calling close() does not stop work or release capacity.

Illustrative test-shaped example (the eventually helper is test-only):

~~~python
job = BrowserTreeJob()
query = job.query_handle()
try:
    job.launch([sys.executable, "-c", "raise SystemExit(7)"])
    eventually(lambda: query.status() == "EXITED")
finally:
    query.close()
    job.close()
~~~

## Error and proof boundaries

| Situation | Observable result |
|---|---|
| Non-Windows/native Job Object setup unavailable | constructor: ValueError("TREE_CONTAINMENT_UNAVAILABLE") |
| Query handle duplication unavailable | query_handle(): ValueError("TREE_QUERY_UNAVAILABLE") |
| Any invalid/failed/ambiguous launch | launch(): ValueError("TREE_LAUNCH_UNAVAILABLE"); no retry on that object |
| Native accounting/membership/limit observation unavailable | query returns None; status is UNKNOWN when accounting is unavailable |
| Query-handle transfer unavailable | duplicate_to(): ValueError("TREE_QUERY_TRANSFER_UNAVAILABLE") |
| Last retained proof handle is lost | no process-tree proof remains; module contract says state is UNKNOWN |

The module is a containment/observation primitive, not a recovery controller, kill switch, certificate writer, or permission to resume an application operation. The architecture guide already states that manual browser capacity is not released just because a lease expired and locates closure evidence in manual-auth recovery (docs/ARCHITECTURE.md:247–255). This reference supplements that system-level policy with the module-level call contract; it does not duplicate or supersede it.

## Evidence pointers (pinned source)

- API and errors: services/applications/browser_tree.py:133–243, 245–262, 319–336.
- Native launch/environment/handle rules: services/applications/browser_tree.py:18–27, 60–90, 93–130, 264–317.
- Unsupported runtime, sanitized environment, lost handles, failed/no-retry launch: tests/unit/test_browser_tree_containment.py:33–105.
- Tree membership, orphan retention, close behavior: tests/unit/test_browser_tree_containment.py:107–145, 198–240.
- Host identity and no-kill close: tests/unit/test_browser_host_status.py:12–68.
- Observer handoff/creator death: tests/unit/test_browser_tree_ipc.py:168–201.
- Enrollment before acknowledgement and persisted close observation: tests/unit/test_browser_tree_durable_bootstrap.py:28–55.

**Validation boundary:** Source blob and cited files were inspected at the pinned commit; no tests, builds, or CI were run for this documentation task. Windows-native tests are explicitly platform-gated in the cited test file.
