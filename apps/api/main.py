"""Local-first product API; mock submission is explicit and external is off."""
from contextlib import asynccontextmanager
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from redis import Redis
from rq import Worker
from sqlalchemy import select, text
from sqlalchemy.exc import NoResultFound
from starlette.middleware.trustedhost import TrustedHostMiddleware

from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.discovery.pipeline import MockClient
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


def create_app(store, mock_origin, output_dir, redis_url="redis://127.0.0.1:6379/0"):
    guard = MockClient(mock_origin)
    guard.close()
    app = FastAPI(title="MJobs", description="Initial controlled mock product spine. External submission disabled.")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "api", "testserver"])

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Cross-origin mutation blocked"}, status_code=403)
        return await call_next(request)

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
        return {"status": "ok", "check": "liveness", "mode": "MOCK", "external_submission_enabled": False}

    @app.get("/readiness")
    def readiness():
        checks = {"database": False, "queue": False, "worker": False, "mock": False}
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
        return {"mode": "MOCK", "external_submission_enabled": False, "browser_workers": limit("APPLY_MOCK"), "worker_limits": {kind: limit(kind) for kind in QUEUES}}

    @app.put("/profiles")
    def profile(value: ProfileInput):
        return {"id": value.id, "version": store.save_profile(value.model_dump())}

    @app.post("/discovery", status_code=202)
    def discovery(value: DiscoveryInput):
        profile = store.profile(value.profile_id)
        return queued("DISCOVER", value.profile_id, {"profile_version": profile["version"], "request_id": value.request_id})

    @app.get("/jobs")
    def jobs(profile_id: str | None = None):
        return {"jobs": store.jobs(profile_id)}

    @app.post("/applications", status_code=202)
    def application(value: ApplicationInput):
        profile = store.profile(value.profile_id)
        with store.engine.connect() as connection:
            connection.execute(select(m.jobs.c.id).where(m.jobs.c.id == value.job_id)).one()
        operation = queued("PREPARE", value.profile_id, {"job_id": value.job_id, "profile_version": profile["version"]})
        return {**operation, "application_id": identity(value.job_id, value.profile_id)}

    @app.get("/applications/{application_id}")
    def application_read(application_id: str):
        return store.application(application_id)

    @app.post("/applications/{application_id}/mock-run", status_code=202)
    def mock_run(application_id: str, value: MockRunInput):
        store.application(application_id)
        return queued("APPLY_MOCK", application_id, {"submit": value.submit})

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

    return app


def create_from_env():
    store = Store(os.environ["MJOBS_DATABASE_URL"])
    app = create_app(store, os.environ["MJOBS_MOCK_ORIGIN"], Path(os.environ["MJOBS_OUTPUT_DIR"]), os.environ["MJOBS_REDIS_URL"])

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield
        finally:
            store.engine.dispose()
    app.router.lifespan_context = lifespan
    return app
