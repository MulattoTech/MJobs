"""Local-first product API; mock submission is explicit and external is off."""
from contextlib import asynccontextmanager
import hashlib
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from redis import Redis
from rq import Worker
from sqlalchemy import select, text
from sqlalchemy.exc import NoResultFound
from starlette.middleware.trustedhost import TrustedHostMiddleware

from packages.persistence import models as m
from packages.persistence.store import Store, identity
from packages.domain.operation_mode import parse_operation_mode, operation_mode_from_env
from services.discovery.pipeline import MockClient
from services.discovery.sources.collect import configure_source
from workers.dispatch import enqueue_operation
from workers.operations import QUEUES, request_cancel, limit


class FactInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=10000)
    source: str = Field(min_length=1, max_length=1000)
    verified: bool
    user_approved: bool
    confidence: float = Field(ge=0, le=100)


class CareerFactInput(FactInput):
    kind: Literal["skill", "experience", "education", "certification", "project", "accomplishment"] = "skill"


class ProfileInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    facts: list[CareerFactInput] = Field(min_length=1, max_length=200)
    answers: list[FactInput] = Field(default_factory=list, max_length=200)


class DiscoveryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1, max_length=128)
    request_id: str | None = Field(default=None, min_length=1, max_length=64)


class ApplicationInput(DiscoveryInput):
    job_id: str = Field(min_length=64, max_length=64)


class MockRunInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    submit: bool = False


class SourceInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    tenant: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    poll_interval: int = Field(default=300, ge=300, le=86400)


def create_app(store, mock_origin, output_dir, redis_url="redis://127.0.0.1:6379/0", operation_mode="PERSONAL"):
    operation_mode = parse_operation_mode(operation_mode)
    if operation_mode == "FIXTURE":
        guard = MockClient(mock_origin)
        guard.close()
    app = FastAPI(title="MJobs", description="Local job discovery, factual preparation and tracking. External submission disabled.")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "api", "testserver"])

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Cross-origin mutation blocked"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site" and request.url.path.startswith(("/profiles", "/documents", "/applications", "/operations")):
            return JSONResponse({"detail": "Cross-site personal data access blocked"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.exception_handler(ValueError)
    async def invalid(_, error):
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.exception_handler(NoResultFound)
    async def missing(_, error):
        return JSONResponse({"detail": "Entity not found"}, status_code=404)

    @app.get("/health")
    def health():
        with store.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "check": "liveness", "mode": "LOCAL_FIRST", "operation_mode": operation_mode, "external_submission_enabled": False}

    @app.get("/readiness")
    def readiness():
        checks = {"database": False, "queue": False, "worker": False}
        if operation_mode == "FIXTURE":
            checks["mock"] = False
        try:
            with store.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            checks["database"] = True
        except Exception:
            pass
        redis = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            checks["queue"] = bool(redis.ping())
            active_queues = {name for worker in Worker.all(connection=redis) if worker.get_state() in ("idle", "busy") for name in worker.queue_names()}
            checks["worker"] = set(QUEUES.values()).issubset(active_queues)
        except Exception:
            pass
        finally:
            redis.close()
        if operation_mode == "FIXTURE":
            client = MockClient(mock_origin)
            try:
                client.client.timeout = 2
                checks["mock"] = client.get(mock_origin + "/health").json().get("status") == "ok"
            except Exception:
                pass
            finally:
                client.close()
        ready = all(checks.values())
        return JSONResponse({"status": "ready" if ready else "degraded", "checks": checks}, status_code=200 if ready else 503)

    def queued(kind, entity_id, params):
        operation_id = enqueue_operation(store, kind, entity_id, params)
        with store.engine.connect() as connection:
            state = connection.execute(select(m.operations.c.state).where(m.operations.c.id == operation_id)).scalar_one()
        return {"operation_id": operation_id, "state": state}

    @app.get("/settings")
    def settings():
        return {"mode": "LOCAL_FIRST", "operation_mode": operation_mode, "external_submission_enabled": False, "browser_workers": limit("APPLY_MOCK") if operation_mode == "FIXTURE" else 0, "worker_limits": {kind: limit(kind) for kind in QUEUES}}

    @app.get("/profiles")
    def profiles():
        return {"profiles": store.profiles()}

    @app.get("/profiles/{profile_id}")
    def profile_read(profile_id: str):
        return store.profile(profile_id)

    @app.put("/profiles")
    def profile(value: ProfileInput):
        return {"id": value.id, "version": store.save_profile(value.model_dump())}

    @app.post("/discovery", status_code=202)
    def discovery(value: DiscoveryInput):
        if operation_mode != "FIXTURE":
            raise HTTPException(409, "Fictional discovery is available only in verification mode")
        profile = store.profile(value.profile_id)
        return queued("DISCOVER", value.profile_id, {"profile_version": profile["version"], "request_id": value.request_id})

    @app.post("/matches", status_code=202)
    def matches(value: DiscoveryInput):
        profile = store.profile(value.profile_id)
        return queued("DISCOVER", value.profile_id, {"profile_version": profile["version"], "rescore_only": True, "request_id": value.request_id})

    @app.get("/sources")
    def sources():
        with store.engine.connect() as connection:
            return {"sources": [dict(row) for row in connection.execute(select(m.job_sources).order_by(m.job_sources.c.name)).mappings()]}

    @app.put("/sources")
    def source_configure(value: SourceInput):
        return {"id": configure_source(store, **value.model_dump())}

    @app.post("/sources/{source_id}/collect", status_code=202)
    def source_collect(source_id: str, value: DiscoveryInput):
        profile = store.profile(value.profile_id)
        with store.engine.connect() as connection:
            connection.execute(select(m.job_sources.c.id).where(m.job_sources.c.id == source_id)).one()
        return queued("DISCOVER", value.profile_id, {"source_id": source_id, "profile_version": profile["version"], "request_id": value.request_id})

    @app.get("/jobs")
    def jobs(profile_id: str | None = None, q: str = Query(default="", max_length=200),
             remote_type: Literal["REMOTE", "HYBRID", "ONSITE", "UNKNOWN"] | None = None,
             provider: Literal["mock-ats", "greenhouse"] | None = None, sort: Literal["score", "title"] = "score",
             limit: int = Query(default=200, ge=1, le=500)):
        return {"jobs": store.jobs(profile_id, q, remote_type, provider, sort, limit)}

    @app.get("/jobs/{job_id}")
    def job(job_id: str, profile_id: str | None = None):
        return store.job(job_id, profile_id)

    @app.get("/applications")
    def applications(profile_id: str | None = None):
        return {"applications": store.applications(profile_id)}

    @app.post("/applications", status_code=202)
    def application(value: ApplicationInput):
        profile = store.profile(value.profile_id)
        with store.engine.connect() as connection:
            connection.execute(select(m.jobs.c.id).where(m.jobs.c.id == value.job_id)).one()
            if operation_mode == "PERSONAL" and connection.execute(select(m.routes.c.id).where(
                    m.routes.c.job_id == value.job_id, m.routes.c.provider == "mock-ats")).first():
                raise HTTPException(409, "Fictional applications are unavailable in personal mode")
        operation = queued("PREPARE", value.profile_id, {"job_id": value.job_id, "profile_version": profile["version"], "request_id": value.request_id})
        return {**operation, "application_id": identity(value.job_id, value.profile_id)}

    @app.get("/applications/{application_id}")
    def application_read(application_id: str):
        data = store.application(application_id)
        with store.engine.connect() as connection:
            data["attempts"] = [dict(row) for row in connection.execute(select(m.attempts).where(m.attempts.c.application_id == application_id).order_by(m.attempts.c.started_at)).mappings()]
        return data

    @app.get("/applications/{application_id}/documents")
    def documents(application_id: str):
        store.application(application_id)
        with store.engine.connect() as connection:
            return {"documents": [dict(row) for row in connection.execute(select(m.documents).where(m.documents.c.application_id == application_id).order_by(m.documents.c.version.desc(), m.documents.c.kind)).mappings()]}

    @app.get("/documents/{document_id}/{format}")
    def download(document_id: str, format: Literal["pdf", "docx"]):
        with store.engine.connect() as connection:
            document = connection.execute(select(m.documents).where(m.documents.c.id == document_id)).mappings().one()
        root = Path(output_dir).resolve()
        app_root = (root / document["application_id"]).resolve()
        relative = document["files"].get(format)
        path = (app_root / relative).resolve() if isinstance(relative, str) else root
        if not app_root.is_relative_to(root) or not path.is_relative_to(app_root) or not path.is_file():
            raise HTTPException(409, "Document unavailable or path invalid; regenerate for review")
        if hashlib.sha256(path.read_bytes()).hexdigest() != document["sha256"].get(format):
            raise HTTPException(409, "Document integrity check failed; regenerate for review")
        media = "application/pdf" if format == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return FileResponse(path, media_type=media, filename=f"{document['kind']}-v{document['version']}.{format}")

    @app.post("/applications/{application_id}/mock-run", status_code=202)
    def mock_run(application_id: str, value: MockRunInput):
        if operation_mode != "FIXTURE":
            raise HTTPException(409, "Fictional application actions are available only in verification mode")
        if store.application(application_id)["target"] != "MOCK":
            raise HTTPException(409, "External submission is disabled; this application supports manual preparation only")
        return queued("APPLY_MOCK", application_id, {"submit": value.submit})

    @app.get("/operations")
    def operations(profile_id: str | None = None):
        with store.engine.connect() as connection:
            statement = select(m.operations)
            if profile_id:
                app_ids = select(m.applications.c.id).where(m.applications.c.profile_id == profile_id)
                statement = statement.where((m.operations.c.entity_id == profile_id) | m.operations.c.entity_id.in_(app_ids))
            return {"operations": [dict(row) for row in connection.execute(statement.order_by(m.operations.c.updated_at.desc()).limit(100)).mappings()]}

    @app.get("/operations/{operation_id}")
    def operation(operation_id: str):
        with store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(m.operations.c.id == operation_id)).mappings().one())

    @app.post("/operations/{operation_id}/cancel", status_code=202)
    def cancel(operation_id: str):
        result = request_cancel(store, operation_id)
        if result.get("reason") == "RECONCILIATION_REQUIRED":
            return JSONResponse(result, status_code=409)
        return result

    web = Path(__file__).resolve().parents[1] / "web" / "dist"
    if web.is_dir():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app


def create_from_env():
    operation_mode = operation_mode_from_env()
    store = Store(os.environ["MJOBS_DATABASE_URL"])
    if operation_mode == "PERSONAL":
        with store.engine.connect() as connection:
            fixture_route = connection.execute(select(m.routes.c.id).where(
                m.routes.c.provider == "mock-ats").limit(1)).first()
        if fixture_route:
            store.engine.dispose()
            raise RuntimeError("Personal operation cannot use a database containing fictional application routes; use a separate Compose project")
    app = create_app(store, os.environ["MJOBS_MOCK_ORIGIN"], Path(os.environ["MJOBS_OUTPUT_DIR"]), os.environ["MJOBS_REDIS_URL"], operation_mode=operation_mode)

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield
        finally:
            store.engine.dispose()
    app.router.lifespan_context = lifespan
    return app
