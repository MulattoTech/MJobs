# MJobs repository map

This is the implemented layout for MJ-REQ-281. Follow the existing boundaries
when extending the application; the original specification's directory tree was
an example, not a requirement to create unused packages or worker directories.

| Path | Ownership |
| --- | --- |
| [apps/api](../apps/api/) | FastAPI application, typed routes, request validation and API/UI mounting |
| [apps/web](../apps/web/) | React/TypeScript UI, client contracts and frontend tests |
| [packages/domain](../packages/domain/) | Shared domain policy, value types, operation mode and state contracts |
| [packages/persistence](../packages/persistence/) | SQLAlchemy schema and stores; durable state and provenance |
| [services/discovery](../services/discovery/) | Search scheduling, collection, normalization and source adapters |
| [services/company_discovery](../services/company_discovery/) | Employer identity, career discovery, source enrollment and monitoring |
| [services/ats_detection](../services/ats_detection/) | Pure provider/board evidence detection |
| [services/dedupe](../services/dedupe/) and [services/matching](../services/matching/) | Canonical job/source identity, route authority and explainable match scoring |
| [services/career_profile](../services/career_profile/), [services/answers](../services/answers/) and [services/learning](../services/learning/) | Verified profile facts, reviewed answer knowledge and feedback |
| [services/documents](../services/documents/) | Uploaded originals, extraction, generation, validation and artifact history |
| [services/applications](../services/applications/) | Provider review adapters, preparation, controlled dry runs and mock receipts |
| [services/ai](../services/ai/) and [services/agents](../services/agents/) | Configured model providers, bounded tools, proposals and traces |
| [services/operations](../services/operations/) and [services/tracking](../services/tracking/) | Workflow read models/actions, local email status and follow-up drafts |
| [services/credentials](../services/credentials/) and [services/privacy](../services/privacy/) | Credential protection and sanitized observations |
| [services/normalization](../services/normalization/) and [services/source_candidates](../services/source_candidates/) | Shared normalized values and candidate-source review |
| [workers](../workers/) | Flat runtime entry points, dispatch, durable operations/leases, retries and task execution |
| [migrations](../migrations/) | Versioned Alembic schema evolution and migration environment |
| [fixtures](../fixtures/) | Synthetic source/ATS service and fictional data |
| [tests](../tests/) | Unit, integration, browser and CI checks plus shared synthetic fixtures |
| [infra/docker](../infra/docker/), [infra/deploy](../infra/deploy/) and [infra/ci](../infra/ci/) | Runtime image, protected deployment/update procedures and CI infrastructure |
| [cli](../cli/) | Explicit operational commands, synthetic acceptance and guarded recovery |
| [tools](../tools/) | Development verification, CI, deployment checks and documentation build |
| [docs](./) | Specifications, current guides, decisions, runbooks and sanitized evidence |
| [planning](../planning/) | Canonical requirement/acceptance authorities and compact continuity records |

The root [compose.yml](../compose.yml) defines the runnable service topology.
Workers share the flat `workers/` package and select their category/queue at
startup; they do not require one Python subpackage per Compose service. The
controlled mock service is in `fixtures/`, not a separate `mock-services/` tree.
API and workers share persistence; the UI is not the durable workflow authority.

## Where to make a change

| Change | Start here |
| --- | --- |
| Add a read-only job provider | [Job source guide](ADDING_JOB_SOURCE.md) |
| Resolve a company and enumerate its career source | [Company source guide](ADDING_COMPANY_SOURCE.md) |
| Recognize and review an ATS application form | [Application adapter guide](ADDING_APPLICATION_ADAPTER.md) |
| Understand service, queue and trust boundaries | [Architecture](ARCHITECTURE.md) |
| Change schema or understand relationships | [Database guide](DATABASE.md) and a separately reviewed migration |
| Diagnose a failed operation | [Troubleshooting](TROUBLESHOOTING.md) |
| Run development checks and publish a change | [Development](DEVELOPMENT.md) and [AGENTS.md](../AGENTS.md) |

Implementation belongs in the narrowest responsible area. Provider quirks stay
behind source/application adapters; shared domain code must not import a
provider's browser behavior. Runtime outputs, credentials and private user data
stay outside tracked source. The shared Fleet implementation is maintained in
`MulattoTech/Fleet-Control-Hub`; MJobs retains only its validated source pin and
delegating integration, as specified in [AGENTS.md](../AGENTS.md).

## Area contracts

An area's `AREA.md` explains responsibilities, interfaces, owned requirements,
dependencies, validation commands, persistence, safety constraints and extension
instructions. Read the affected contract alongside current source and tests;
historical acceptance notes in an area document do not prove current test results.
