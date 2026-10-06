# Guided MJobs Product Review

This is the hands-on route for testing whether MJobs is useful as a product rather than merely whether it launches.

The review has three deliberately separate phases:

1. Walk through the product with Luna as a facilitator. Do not change code.
2. Turn your observations into granular GitHub issues. Do not fix them in the same triage turn.
3. Run later implementation turns against those issues in dependency/impact order.

The separation keeps your observations authoritative and prevents an agent from silently changing the application while you are still deciding what is wrong.

## Choose a review mode

Use PERSONAL when you want the most meaningful experience with your own local profile/resume/job data. Data remains on this computer and real external submission remains OFF.

Use FIXTURE when you want a disposable fictional walkthrough, especially for application/submission-adjacent behavior.

From VS Code use:

- MJobs: Guided Product Review - Personal
- MJobs: Guided Product Review - Fixture

The task starts the selected local MJobs stack and the private Development & Feedback page, writes an ignored outputs/live-feedback/ACTIVE_REVIEW.json marker, and opens the app plus feedback page. It does not start Codex or create GitHub issues.

## How to record feedback

Use the Development & Feedback page at http://127.0.0.1:18702.

For each distinct problem or missing capability, choose Review a core workflow and create one Ready for Codex report per distinct finding. Separating findings makes later GitHub issues narrow enough to implement and close honestly.

Record observable behavior rather than a guessed implementation cause. Private evidence paths may be referenced locally but must not be copied to GitHub.

Use impact consistently:

- blocks core workflow — you cannot complete the current meaningful task;
- major feature gap — the workflow completes only by bypassing an expected major capability;
- high friction — it works, but the experience is confusing/repetitive enough to matter;
- polish — the workflow is usable and this is refinement.

At the end, optionally use Decide what makes MJobs usable for me for an overall summary.

## Meaningful workflow sequence

Do these in order because later workflows depend on earlier data.

### 1. Profile and resume truth

Goal: create or select a profile that can safely drive the rest of MJobs.

Try:

1. Open Profile.
2. Import a PDF/DOCX/TXT resume, or inspect an existing one.
3. Run extraction and review what MJobs proposes.
4. Approve only facts you can verify.
5. Edit one approved fact and confirm it becomes pending until deliberately re-reviewed.
6. Save, reload, and confirm the reviewed state remains correct.

Look for missing career fields, confusing provenance or approval behavior, extracted facts that lose context, accidental auto-approval, or edits that silently change historical data.

### 2. Job intake and source usefulness

Goal: get a real opportunity into MJobs with enough trustworthy source context to act on it.

Try both paths when useful:

- collect from an already supported public source; and
- use Capture a job for a posting MJobs cannot collect directly.

Confirm title, company, location, description, source/reference URL, application URL, source provenance, and reload persistence.

Look for source coverage that prevents normal use, fields that cannot be captured/corrected, duplicate mistakes, confusing provenance, or bad application links.

### 3. Search, filtering, and match explanation

Goal: decide which opportunities deserve your time.

Try:

1. Search/filter by criteria you actually care about.
2. Save a search when appropriate.
3. Compare at least two materially different jobs.
4. Inspect the match explanation and missing/unknown evidence.
5. Change one relevant approved fact or filter and confirm the result updates.

Look for missing filters/sorts, opaque scores, required/preferred mistakes, ignored evidence, irrelevant jobs ranked too highly, or useful jobs hidden.

### 4. Resume and cover-letter preparation

Goal: produce materials you could realistically review and send yourself.

For one promising job:

1. Review approved facts selected for the resume.
2. Exercise ordering/emphasis/tailoring controls.
3. Generate or revise a job-specific resume.
4. Generate a cover letter using useful length/style controls.
5. Preview and download available formats.
6. Create a second revision and confirm the first remains available.
7. Compare revisions where available.

Look for generic output, missing fact controls, unsupported claims, weak job specificity, missing editing/review controls, or confusing version history.

### 5. Reusable application answers

Goal: prepare answers without allowing MJobs to invent sensitive or legal facts.

Try:

1. Add/review a reusable answer.
2. Open an application and enter an employer question.
3. Reuse an exact/appropriate saved answer.
4. Deliberately review it for that job.
5. Export/reopen the reviewed answer document.

Look for questions that cannot be represented, poor equivalent-question matching, unclear confidence/review, inferred sensitive/legal answers, or missing history/reuse controls.

### 6. Application tracking and follow-up

Goal: use MJobs as the durable record of what you are actually doing.

Try:

1. Save/hide/not-interested on jobs where appropriate.
2. Prepare an application without submitting it automatically.
3. Set a manual application status reflecting what you did outside MJobs.
4. Add notes.
5. Move through at least two later pipeline stages when sensible.
6. Reload and inspect chronological history and linked documents.

Look for missing statuses, no distinction between manual reporting and confirmed submission, weak history, missing notes/tasks/follow-up, or poor record linking.

### 7. Restart and continuity

Goal: prove this behaves like a persistent tool rather than a demo.

Close/reopen the browser and, when practical, stop/start the local app. Confirm the selected profile, saved searches, jobs, documents, answer exports, statuses, notes, and historical versions remain intact.

Record any data-loss, stale-state, confusing recovery, or startup problem immediately.

## What Luna should do during the walkthrough

Use prompts/VS_CODE_GUIDED_REVIEW.md.

Luna is a facilitator, not an implementer, during this phase. It should:

- verify you are testing the intended local build/mode;
- guide you through one workflow at a time;
- explain what the existing UI is expected to do when useful;
- help distinguish bug vs missing feature vs usability friction;
- remind you to create one Ready report per finding;
- keep a concise session summary;
- not edit code, create issues, merge branches, or reinterpret your feedback into fixes yet.

## After the walkthrough

Start a fresh Luna turn and use prompts/FEEDBACK_TO_GITHUB_ISSUES.md. That turn should create/update granular GitHub issues from the reports, remove duplicates, map requirements, and produce a dependency-aware issue order. It should not implement fixes.

Then start another fresh implementation turn with prompts/FIX_FEEDBACK_ISSUES.md. That turn should work the dependency-ready issue queue using the local-only budget policy and existing evidence system.
