# Bounded Fact Review Evidence Git Lookup

## Objective

Close an unbounded local Git subprocess in the fact-review verification command under open P0 issue #5 / MJ-REQ-004. Preserve exact-source evidence while ensuring a stalled or failed Git command cannot hang the verifier or leak raw diagnostics.

## Acceptance behavior

- Resolve `HEAD` with shell-free `subprocess.run`, a five-second timeout, captured stdout, discarded stderr, and a bounded 40/64-hex revision check.
- Timeout, process launch failure, nonzero exit, and malformed SHA output receive stable safe error categories.
- The verifier still writes an inspectable evidence JSON record when Git resolution fails, marks the result FAIL, records no source commit, and returns nonzero.
- Raw stderr, repository paths, and command diagnostics are never copied into evidence.

## Implementation and validation

1. Add unit oracles for the exact subprocess configuration, timeout classification, and evidence output on a simulated Git timeout.
2. Replace the unbounded `check_output` call with the bounded helper; do not alter the browser oracle acceptance rules.
3. Run focused verifier unit tests, the four-case fact-review browser oracle, repository validators, and the full local suite.
4. Record exact candidate hashes, commands, timestamps, failure/skips, and limitations; keep issue #5 OPEN/PARTIAL.

## Boundaries

GitHub Actions, hosted evidence, and hosted review remain NOT_RUN by policy. The source commit field remains mandatory for a passing verifier receipt.
