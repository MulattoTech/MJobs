"""Runtime health, deployment, and configuration routes."""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy import text

from services.discovery.pipeline import MockClient
from services.runtime_health import read_runtime_health, REQUIRED_QUEUES
from workers.operations import QUEUES, limit


def create_runtime_router(
    *,
    store,
    mock_origin,
    redis_url,
    operation_mode,
    deploy_sha,
    submission_policy,
    browser_profile,
    notifications_enabled,
    redis_client=Redis,
):
    router = APIRouter()
    expected_schema_heads = None

    @router.get("/health")
    def health():
        with store.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {
            "status": "ok",
            "check": "liveness",
            "mode": "LOCAL_FIRST",
            "operation_mode": operation_mode,
            "external_submission_enabled": False,
        }

    @router.get("/ready")
    @router.get("/readiness")
    def readiness():
        nonlocal expected_schema_heads
        checks = {
            "configuration": False,
            "database": False,
            "queue": False,
            "worker": False,
            "scheduler": False,
            "workers": {queue: False for queue in REQUIRED_QUEUES},
        }
        if store.engine.dialect.name == "postgresql":
            checks["schema"] = False
        if operation_mode == "FIXTURE":
            checks["mock"] = False
        try:
            store.assert_workspace_mode(operation_mode)
            checks["configuration"] = True
            with store.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                checks["database"] = True
                if store.engine.dialect.name == "postgresql":
                    from alembic.config import Config
                    from alembic.runtime.migration import MigrationContext
                    from alembic.script import ScriptDirectory

                    if expected_schema_heads is None:
                        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
                        expected_schema_heads = set(
                            ScriptDirectory.from_config(config).get_heads()
                        )
                    checks["schema"] = (
                        set(MigrationContext.configure(connection).get_current_heads())
                        == expected_schema_heads
                    )
        except Exception:
            pass
        redis = redis_client.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            checks["queue"] = bool(redis.ping())
            runtime = read_runtime_health(redis)
            checks["workers"] = runtime["workers"]
            checks["worker"] = all(runtime["workers"].values())
            checks["scheduler"] = runtime["scheduler"]
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
        ready = all(value for key, value in checks.items() if key != "workers")
        return JSONResponse(
            {
                "status": "ready" if ready else "degraded",
                "checks": checks,
                "db_backend": store.engine.dialect.name,
            },
            status_code=200 if ready else 503,
        )

    @router.get("/__deployment")
    def deployment():
        version = (Path(__file__).resolve().parents[2] / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
        return {
            "commit": deploy_sha or None,
            "version": version,
            "operation_mode": operation_mode,
        }

    @router.get("/settings")
    def settings():
        return {
            "mode": "LOCAL_FIRST",
            "operation_mode": operation_mode,
            "external_submission_enabled": False,
            "submission_limits": {
                "hourly": submission_policy.hourly_limit,
                "daily": submission_policy.daily_limit,
                "company": submission_policy.company_limit,
            },
            "browser_workers": limit("APPLY_MOCK") if operation_mode == "FIXTURE" else 0,
            "worker_limits": {kind: limit(kind) for kind in QUEUES},
            "notifications": {"enabled": notifications_enabled, "delivery": "IN_APP"},
            "browser_profile": {
                "name": browser_profile,
                "scope": "PER_APPLICATION",
                "existing_sessions": "PINNED",
            },
        }

    return router
