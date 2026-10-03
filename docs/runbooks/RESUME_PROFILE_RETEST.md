# Resume-driven profile retest

Branch: `codex/resume-driven-profile`. Requirements: MJ-REQ-088 through MJ-REQ-094.
Evidence: `docs/evidence/2026-10-03-resume-driven-profile.json`.

## Exact VS Code workflow

1. Open `C:\Tools\Dev\MJobs` on this branch. Run **Terminal → Run Task →
   MJobs: Live Feedback - Fixture**. This rebuilds the fixture application and
   opens the recorder/browser at `http://127.0.0.1:18700`.
2. Click **New profile**. Enter a fictional **Full name** and **Email**, leave
   career facts empty, and click **Save profile**.
3. In **Resume history**, choose a synthetic text-based PDF, DOCX, or UTF-8 TXT.
   Set **Resume collection** to **New resume**, enter a **Resume label**, and
   click **Store resume**. Local synthetic samples are available at
   `outputs/resume-profile-sample.pdf` and `outputs/resume-profile-sample-v2.pdf`
   after this development iteration.
4. Click **Extract and review**. Read the suggestions. Contact replacements
   start unchecked; select only those you want to use. Select desired facts,
   then click **Add selected to profile editor**.
5. Review/edit the populated fields. Imported career facts must show **Pending**
   with both approval boxes unchecked. Save and reload; the pending facts and
   contact fields should persist. Re-extracting the same file must not duplicate
   existing facts or remove their review decisions.
6. Fill **Contact details** and **Preferences**, including multiple phone lines,
   employment types, work arrangements, shifts and commute limits. Explicitly
   verify and approve only correct career facts, then **Save profile**.
   Reload and check the values. Preferences are stored; they do not yet apply
   automatically to job searches.
7. Upload a changed resume, selecting the existing named **Resume collection**.
   Both **Version 1** and **Version 2** should appear, with working
   **Download original** links. A repeated upload of the same bytes to that
   collection should reuse the version.
8. End the recorder and record any discrepancy. No real external application
   submission is needed for this workflow.

Use **MJobs: Live Feedback - Personal** at `http://127.0.0.1:18701` when testing
with your own resume instead of fictional fixture data.

## Current limits

Name/email are entered before the first upload. Extraction is deterministic and
may require corrections for unusual layouts. Scanned PDFs report **NEEDS OCR**;
OCR and LinkedIn profile import are not implemented. LinkedIn can be saved as a
contact URL. Full structured work-history extraction, advanced document types,
and automatic use of preferences remain open.
