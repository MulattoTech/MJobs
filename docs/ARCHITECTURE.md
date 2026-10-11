# MJobs architecture and trust boundaries

MJobs is a self-hosted application with a React/TypeScript UI (`apps/web`), FastAPI endpoints (`apps/api`), domain and persistence packages, PostgreSQL, Redis queues, workers and local document storage. `compose.yml` describes the current runtime; `infra/docker/Dockerfile` builds it.

## Data and work

Career profiles carry sourced facts and explicit approval. Search profiles describe desired opportunities. Source postings preserve raw provenance while normalized jobs and application routes avoid conflating discovery with application destinations. Document and application operations preserve version/state information.

Long-running work belongs in durable operations and queues rather than the lifetime of a browser request. Discovery, document and application worker boundaries let a browser operation pause without requiring the entire pipeline to stop.

## Safety boundaries

- PERSONAL and FIXTURE workspaces use different databases/volumes and stored identities.
- CI uses synthetic inputs on disposable GitHub-hosted runners; it has no personal applicant data.
- External source content is untrusted. Discovery must validate URLs, redirects and private-network boundaries.
- Original documents and encrypted credentials remain local. Existing keys are not replaceable recovery shortcuts.
- External application submission remains off; controlled mock confirmation is not evidence of live-provider submission support.

## Acceptance is separate from architecture

Code presence, a running service or a passing unit test does not establish whole-feature completion. `planning/CAPABILITY_MATRIX.json` holds 366 original requirements; `planning/REFERENCE_GAP_MATRIX.json` separately holds 59 reference gaps. The compact index and dashboard are derived views.

See [the repository map](REPO_MAP.md), relevant specifications in `docs/spec/areas`, and [the automation guide](maintainers/AUTOMATION.md). Keep historic evidence intact; read it only when needed for a concrete acceptance question.
