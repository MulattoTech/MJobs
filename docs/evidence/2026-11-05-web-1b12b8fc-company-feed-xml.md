# MJ-REQ-018 / MJ-REQ-004: company-feed XML declaration enforcement

Run: web-20261105T024458Z-1b12b8fc-3b08-4780-ba5c-488849ee318d.
Requirements: [#19](https://github.com/MulattoTech/MJobs/issues/19) and
[#5](https://github.com/MulattoTech/MJobs/issues/5), both remaining **PARTIAL**.
Delivery route: existing [PR952](https://github.com/MulattoTech/MJobs/pull/952).
Source commit: b864f641cb17bd5b93de4bae24a59c2691f4b3b3.
Reproduced baseline: combined head 94cdcf3fd820d0af365c89b867ac91d8277015ca.

## Report, reproduction and correction

A separate session reported the company-feed parser boundary in
[coordination comment 6648818329](https://github.com/MulattoTech/MJobs/pull/952#issuecomment-6648818329).
I independently reproduced it on the unchanged combined candidate with a small
synthetic feed: the ordinary UTF-8 declaration was refused, while UTF-16LE/BE
byte patterns retained in a transport-decoded string were accepted and their
benign internal entity was expanded. No external request or amplification payload
was used. The related sitemap change is separately owned in PR959 and is not
part of this correction.

The existing text screen remains in place. An XML TreeBuilder document-type
callback now rejects declarations after the parser detects encoding, using the
existing fixed COMPANY_JOB_FEED_INVALID error boundary. No source endpoint,
destination-selection, media, byte/item limit, network authorization, retry,
deadline, dependency or schema policy was changed.

## Exact executed proof

Before product edits, the two new modules produced **14 failed results**:
12 failed declaration subcases and two connected-discovery failures.
The remaining result was 3 parent passes and 12 passing subtests, 2.21 seconds,
exit1. All failures were the missing expected rejection. There was no collection
or fixture error in this baseline.

After the correction and new-test style cleanup, the same two modules produced
**5 tests +24 subtests PASS**, zero failures/errors/skips, 1.94 seconds, exit0.

~~~text
python -B -m pytest -q -p no:cacheprovider --basetemp <owned-temp> --junitxml <owned-receipt> tests/unit/test_company_feed_xml_safety.py tests/integration/test_company_feed_xml_safety.py --tb=short
~~~

The unit matrix covers RSS and Atom, UTF-8 and detected UTF-16LE/BE, and empty,
internal and external document-type declarations. Positive controls retain ordinary
encoded feeds, predefined entities, legitimate job destinations and literal
declaration text inside a JSON Feed title.

Connected synthetic HTTP/discovery tests show refusal before a new source is
enrolled or its job page fetched. They preserve canonical job, route and posting
rows across database reopen when an existing source returns a declared feed.
A later ordinary encoded feed successfully imports a new legitimate job.
The tests use private temporary SQLite databases and a synthetic in-memory
transport; they do not contact employers or submit applications.

The full combined current-main execution and actual publication/CI revision are
recorded in the PR after the separately locked integration transaction. These
five methods are part of, not additive to, any broader combined suite.

## Integrity and preserved preparation failures

New-test Ruff PASS. The production module retains its single existing I001
diagnostic; the baseline/current code-message multisets match, with no
suppression or rule change. The first new-test lint pass reported I001 and
SIM117; import formatting and equivalent context-manager layout were corrected
before the successful test run. Assertions and input variants were unchanged.

A commit-preparation helper initially stripped the first Git porcelain status
column and refused its own scope check before staging. Reading the status
without stripping its leading columns corrected the helper. The resulting
commit contains exactly the intended production module and two new tests.

Catalogue, progress journal, dashboard freshness, whitespace and strict source
provenance PASS. The committed production/test bytes match the passing execution.
Native Windows, Python3.12, existing project dependencies read-only.

## Ownership and delivery limits

Claim refs/heads/webpro-claims/COMPANY-FEED-XML-DECLARATION-SAFETY
at b41172654ae6e1786bbea90d725dc784ab0f40e0 was acquired with HTTP201 and
exact readback at 2026-11-05T03:36:11.843433Z, then revalidated before edits and
commits. The 22-open-PR file comparison found only the already owned PR929/952
endpoint overlap. The source uses its own worktree and temporary resources.

This follow-up changes the endpoint blob from the earlier Atom review, so fresh
independent review of the guard and regressions is required. The alternate/media
logic and earlier component histories remain intact; prior green CI on 94cdcf3f
does not certify this source or a refreshed main candidate.

No canonical status/denominator, Project/Fleet, protected check or quota change.
No separate AI agent, heavy/browser slot, live provider, production outage,
authenticated browser or deployment was used for this proof. The broader XML
and external-operation parents are not accepted by this bounded correction.
No claim is made about unsupported-encoding behavior that did not reproduce.

Rollback is a protected revert of this source correction after assessing later
changes; no data restore is needed. Such a revert would reopen the reproduced
declaration gap. Merge and deployment require separate exact ownership, current
required checks and the existing guarded release workflow.
