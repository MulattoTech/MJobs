# MJobs

**A local-first workspace for finding opportunities, reviewing career evidence, and preparing job applications.**

[![CI](https://github.com/MulattoTech/MJobs/actions/workflows/ci.yml/badge.svg)](https://github.com/MulattoTech/MJobs/actions/workflows/ci.yml)
[![Release acceptance](docs/PROGRESS_CARD.svg)](docs/PROGRESS_DASHBOARD.md)

> [!IMPORTANT]
> **Active development / public preview — not production-ready.** MJobs is designed to keep applicant data in a self-hosted workspace. External employer applications are **never submitted automatically** by the current release. Verify all information and submit real applications yourself. A green build is not proof that the entire requirements catalogue is complete.

![MJobs pipeline workbench with a fictional test profile](docs/assets/mjobs-pipeline-demo.png)

<details>
<summary>More actual application screenshots (fictional test workspace)</summary>

### Employer sources and provenance

![MJobs source relationship graph](docs/assets/mjobs-sources-demo.png)

### First-run setup

![MJobs first-run setup checklist](docs/assets/mjobs-dashboard-fixture.png)

Screenshots were captured from the running **FIXTURE** mode on October 11, 2026. Profiles are synthetic; some job listings are publicly sourced. No personal workspace was used.
</details>

## What MJobs does today

MJobs combines a React frontend, a Python/FastAPI backend, PostgreSQL, Redis-backed background workers, and browser-assisted **local testing**. Implemented components include:

- **Career profiles:** resume import, explicit review of sourced facts, approved answers, document storage and version history.
- **Job discovery:** supported public read-only job sources, employer-source registration and monitoring, raw source provenance, normalized job records and search.
- **Relevance and tracking:** saved search profiles, explainable matching components, shortlist/pipeline views, and auditable application history.
- **Document preparation:** factual resume and cover-letter generation, with controlled review and local artifact storage.
- **Application review:** ATS route detection, safe form analysis, manual review gates, and synthetic mock-application testing.
- **Recovery:** durable work operations and retries, service health checks, isolated personal/fixture environments, and local backup/restore tooling.

**Limits matter:** full ATS automation, automatic employer discovery, complete browser acceptance, and other requirements remain in progress. See the [evidence-backed progress dashboard](docs/PROGRESS_DASHBOARD.md); individual working slices do not imply full product acceptance.

## Quick start — Windows

**Requirements:** Windows with PowerShell and Docker Desktop running **Linux containers**. Keep Docker running during startup.

```powershell
git clone https://github.com/MulattoTech/MJobs.git
cd MJobs
.\Start-MJobs.ps1
```

The launcher builds and starts the isolated fictional test environment, applies migrations, waits for API/database/Redis/workers to become ready, and opens [http://127.0.0.1:18700](http://127.0.0.1:18700).

From the dashboard, load **fictional demo** data, explore Jobs and Sources, prepare sample documents, and use the controlled mock application workflow. Nothing is submitted to a real employer.

### Use your own career data

Run a **separate personal** installation instead of mixing your data with test fixtures:

```powershell
.\Start-MJobs.ps1 -Personal
```

Open [http://127.0.0.1:18701](http://127.0.0.1:18701). PERSONAL and FIXTURE modes use separate Compose projects and persistent volumes. Personal mode disables fictional actions and all real external submission.

**Preserve your existing configuration:** if upgrading an older installation, do not delete or regenerate its ignored `.env` or existing database/document volumes. An older encryption key may be needed to read previously saved credentials. Use the [workspace mode](docs/runbooks/WORKSPACE_MODE.md) and [backup/restore](docs/runbooks/LOCAL_BACKUP.md) runbooks before migrating.

For a step-by-step demo, diagnostics, prerequisites and known limitations, see [Local user testing](docs/USER_TESTING.md).

## Developer guide

| Task | Entry point |
|---|---|
| Local test setup | [Developer/Codespaces guide](docs/CODESPACES.md) |
| Source and runtime architecture | [Repository map](docs/REPO_MAP.md) |
| Existing test and release policy | [Verification instructions](AGENTS.md) |
| Current engineering state | [Session handoff](planning/SESSION_HANDOFF.md) |
| Requirement and product acceptance | [Progress dashboard](docs/PROGRESS_DASHBOARD.md) |
| Public CI modernization | [Modernization plan](docs/maintainers/PUBLIC_REPOSITORY_MODERNIZATION.md) |

For a pinned Python environment (Python 3.12), run `python -m pytest -q tests/unit` after installing `requirements.lock`. For the web app (Node.js 24):

```powershell
npm ci --prefix apps/web
npm test --prefix apps/web
npm run build --prefix apps/web
```

Automated GitHub checks validate fresh clones independently; a successful CI job cannot replace isolated user-facing acceptance tests.

## Safety, privacy and contributions

- Do not commit resumes, credentials, API tokens, local `.env` files, database backups, browser sessions or private test evidence.
- Third-party job postings are untrusted input. Real-world source access must respect provider rules, rate limits, access controls and manual authentication.
- Report security vulnerabilities **privately**, following [SECURITY.md](SECURITY.md). Use [CONTRIBUTING.md](CONTRIBUTING.md) for proposed changes and checks.
- This repository is publicly readable, but **public access is not a software license**. The project owner has not yet declared redistribution/modification terms; review license expectations before reusing or distributing the code.

MJobs is under active development. The project's acceptance numbers are intentionally conservative and tied to executed evidence, not an arbitrary completion percentage.
