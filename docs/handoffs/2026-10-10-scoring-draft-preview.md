# Draft ranking preview checkpoint

- Issue #118 / MJ-REQ-117 remains PARTIAL; this bounded preview slice passed local acceptance.
- Branch `chatgpt/preference-weighting-20261010`; worktree `C:/Tools/Dev/MJobs-WebMatching-20261010`.
- Tested source `1cbfc585f7445f35d6109cba51676b95aae7f688`; tree `4282530889b18c211e75fdea160fcb5705628c35`; predecessor `94639160433e2a9ce787279ebeab0c70e0c51808`.
- Preview current/draft score and rank changes before saving. No table writes, external calls or application side effects. Exact constraints and source/test hashes are in `docs/evidence/2026-10-10-scoring-draft-preview.json`.
- Final combined regression: 130 Python/browser tests plus 67 subtests; 583 frontend tests in 101 files; build and coordination/diff checks PASS. Earlier checkpoint test counts overlap and must not be summed as unique tests.
- Hosted/delegated review, PostgreSQL, real ATS and full Python suite NOT_RUN. Advanced workspace filters fail closed; preview is a bounded diagnostic sample, not a submission shortlist.
- Main and canonical progress untouched. Integration belongs to the existing lock owner; TRACKER_REFRESH_PENDING. Publication receipt is recorded in issue #118 after non-force push/readback.
- Next dependency-ready correction: review probe found approved negative skill/experience text counted as positive skill evidence. Fix under a new exact helper/test claim while retaining ownership of the existing matching modules.
