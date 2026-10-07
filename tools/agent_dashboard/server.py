from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .supervisor import AgentSupervisor, LANES


class LanePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = None
    reasoning: str | None = None
    auto_continue: bool | None = None
    prompt_file: str | None = None
    prompt_override: str | None = None
    gpu_offload_percent: int | None = Field(default=None, ge=0, le=100)


def create_app(root: Path | None = None) -> FastAPI:
    repo = (root or Path.cwd()).resolve()
    supervisor = AgentSupervisor(repo)
    static_dir = Path(__file__).parent / "static"

    app = FastAPI(
        title="MJobs Agent Control",
        description="Loopback-only supervisor for MJobs parallel implementation lanes.",
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.state.supervisor = supervisor

    @app.get("/")
    def index():
        return FileResponse(static_dir / "index.html")

    @app.get("/api/state")
    def state():
        return supervisor.snapshot()

    @app.get("/api/lanes/{lane}/log")
    def log(lane: str, lines: int = Query(default=200, ge=1, le=1000)):
        try:
            return {"lane": lane, "log": supervisor.log_tail(lane, lines)}
        except KeyError:
            raise HTTPException(404, "unknown lane")

    @app.post("/api/lanes/{lane}/start")
    def start(lane: str):
        try:
            return supervisor.start(lane)
        except KeyError:
            raise HTTPException(404, "unknown lane")
        except Exception as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/lanes/{lane}/stop")
    def stop(lane: str):
        try:
            return supervisor.stop(lane)
        except KeyError:
            raise HTTPException(404, "unknown lane")

    @app.post("/api/lanes/{lane}/restart")
    def restart(lane: str):
        try:
            return supervisor.restart(lane)
        except KeyError:
            raise HTTPException(404, "unknown lane")
        except Exception as exc:
            raise HTTPException(409, str(exc))

    @app.patch("/api/lanes/{lane}/config")
    def config(lane: str, patch: LanePatch):
        try:
            values: dict[str, Any] = {
                key: value for key, value in patch.model_dump().items() if value is not None
            }
            return supervisor.update_config(lane, values)
        except KeyError:
            raise HTTPException(404, "unknown lane")

    @app.post("/api/stop-all")
    def stop_all():
        return {lane: supervisor.stop(lane) for lane in LANES}

    @app.exception_handler(KeyError)
    async def key_error(_, exc: KeyError):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="MJobs parallel-agent control dashboard")
    parser.add_argument("--root", default=".")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=18704, type=int)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        parser.error("Agent dashboard is loopback-only; use 127.0.0.1 or localhost")
    app = create_app(Path(args.root))
    if not args.no_open:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{args.port}/")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
