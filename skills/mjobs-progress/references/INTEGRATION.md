# MJobs progress sources and safety

Canonical counters come from planning/CAPABILITY_MATRIX.json and planning/REFERENCE_GAP_MATRIX.json, verified against planning/ISSUE_SEED.jsonl and the current state in planning/IMPLEMENTATION_STATE.json. The committed docs/PROGRESS_CARD.json is a tiny generated cache of those counts; docs/PROGRESS_CARD.svg is GitHub's visual progress bar; docs/PROGRESS_DASHBOARD.md is the full human-readable report.

Invoke the installed skill by asking “Show MJobs progress” in ChatGPT. ChatGPT Web skill upload availability depends on plan/workspace permissions. When Skills > Create > Upload from your computer is available, upload the ZIP containing the mjobs-progress directory and SKILL.md. The connected GitHub and Remote Desktop Commander apps remain necessary for private current-state reads; the ZIP cannot grant access to those apps or run a background sync.

Do not modify main when another Codex root holds the integration lock. A local branch can be hundreds of commits ahead of GitHub; show source provenance and avoid confusing published with local progress.
