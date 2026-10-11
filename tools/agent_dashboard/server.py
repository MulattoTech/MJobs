from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .supervisor import AgentSupervisor, LANES
from .mission_control import MissionControl, LaunchRequest
from .work_history import WorkHistory


class LanePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = None
    reasoning: str | None = None
    auto_continue: bool | None = None
    prompt_file: str | None = None
    prompt_override: str | None = None
    gpu_offload_percent: int | None = Field(default=None, ge=0, le=100)


def create_app(root: Path | None = None, *, mission_instance: bool = False) -> FastAPI:
    repo = (root or Path.cwd()).resolve()
    supervisor = AgentSupervisor(repo, mission_instance=mission_instance)
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
    mission = MissionControl(supervisor)
    app.state.mission_control = mission
    history = WorkHistory(repo)
    app.state.work_history = history

    @app.get("/api/launcher/health")
    def launcher_health():
        return {
            "service": "mjobs-mission-control", "version": 8, "status": "ready",
            "root": str(repo), "prompt_preview": True, "approval_required": True,
            "history_available": True, "history_refresh_seconds": 5,
            "visible_terminal": True, "subagent_controls": True,
            "prompt_via_stdin": True, "console_output_binding": "CONOUT$",
            "python_environment_preflight": True,
            "accurate_partial_state": True,
            "readable_console_summary": True, "crlf_summary_fix": True,
        }

    def deny_legacy_process_controls():
        if mission_instance:
            raise HTTPException(
                403, "Legacy controls are disabled in Mission Control. "
                     "Use Read prompt, approve the effective prompt, then Launch CLI."
            )

    @app.get("/api/launcher/options")
    def launcher_options():
        return mission.options()

    @app.post("/api/launcher/models/refresh")
    def refresh_models(request: Request):
        mission.require_token(request, mission.token)
        return mission.refresh_models()

    @app.get("/api/progress")
    def release_progress():
        return mission.progress()

    @app.get("/api/history")
    def recent_work_history(limit: int = Query(default=50, ge=1, le=100),
                            refresh: bool = False):
        """Read-only local Git + separate Codex session activity. No API/model calls."""
        try:
            return history.snapshot(limit=limit, force=refresh)
        except (RuntimeError, OSError, ValueError):
            raise HTTPException(503, "Work history is unavailable. Try refreshing again.") from None

    @app.get("/api/launcher/profiles/{slug}")
    def launcher_profile(slug: str):
        from .mission_control import PROFILES
        if slug not in PROFILES:
            raise HTTPException(404, "Unknown profile")
        path = repo / PROFILES[slug]["path"]
        if not path.is_file():
            raise HTTPException(404, "Profile file missing")
        return {"profile": slug, "content": path.read_text(encoding="utf-8")}

    @app.post("/api/launcher/{lane}/preview")
    def launcher_preview(lane: str, options: LaunchRequest, request: Request):
        # Same authenticated origin as launch, but without any worktree,
        # configuration, process or model-execution side effects.
        mission.require_token(request, mission.token)
        try:
            return mission.preview(lane, options)
        except (ValueError, RuntimeError, OSError):
            raise HTTPException(409, "Prompt preview failed. Check the selected settings and try again.") from None

    @app.post("/api/launcher/{lane}/launch")
    def launcher_launch(lane: str, options: LaunchRequest, request: Request):
        mission.require_token(request, mission.token)
        try:
            return mission.launch(lane, options)
        except (ValueError, RuntimeError, OSError):
            raise HTTPException(409, "Launch failed. Refresh the models and review the prompt before trying again.") from None

    @app.post("/api/launcher/{lane}/terminal-window")
    def launcher_terminal_window(lane: str, request: Request):
        """Show live or last stdout; NEVER creates another coding worker."""
        mission.require_token(request, mission.token)
        if lane not in ("codex", "antigravity", "claude"):
            raise HTTPException(404, "Unknown CLI lane")
        try:
            return supervisor.open_log_console(lane)
        except (ValueError, RuntimeError, OSError):
            raise HTTPException(409, "Terminal output is unavailable. Check the lane state and try again.") from None

    @app.post("/api/launcher/{lane}/stop")
    def launcher_stop(lane: str, request: Request):
        mission.require_token(request, mission.token)
        try:
            return mission.stop(lane)
        except (ValueError, RuntimeError, OSError):
            raise HTTPException(409, "Stop failed. Check the lane state before trying again.") from None

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
        deny_legacy_process_controls()
        try:
            return supervisor.start(lane)
        except KeyError:
            raise HTTPException(404, "unknown lane")
        except Exception:
            raise HTTPException(409, "Start failed. Check the lane configuration before trying again.") from None

    @app.post("/api/lanes/{lane}/stop")
    def stop(lane: str):
        deny_legacy_process_controls()
        try:
            return supervisor.stop(lane)
        except KeyError:
            raise HTTPException(404, "unknown lane")

    @app.post("/api/lanes/{lane}/restart")
    def restart(lane: str):
        deny_legacy_process_controls()
        try:
            return supervisor.restart(lane)
        except KeyError:
            raise HTTPException(404, "unknown lane")
        except Exception:
            raise HTTPException(409, "Restart failed. Check the lane state and configuration before trying again.") from None

    @app.patch("/api/lanes/{lane}/config")
    def config(lane: str, patch: LanePatch):
        deny_legacy_process_controls()
        try:
            values: dict[str, Any] = {
                key: value for key, value in patch.model_dump().items() if value is not None
            }
            return supervisor.update_config(lane, values)
        except KeyError:
            raise HTTPException(404, "unknown lane")


    @app.websocket("/ws/lanes/{lane}/terminal")
    async def terminal_stream(websocket: WebSocket, lane: str):
        if lane not in LANES:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        positions: dict[str, int] = {}
        current_run = "__unset__"
        last_heartbeat = 0.0
        try:
            while True:
                info = supervisor.terminal_sources(lane)
                run_id = info.get("run_id")
                if run_id != current_run:
                    current_run = run_id
                    positions = {
                        source["name"]: int(source.get("start_offset") or 0)
                        for source in info.get("sources", [])
                    }
                    await websocket.send_text(json.dumps({
                        "type": "run",
                        "run_id": run_id,
                        "status": info.get("status"),
                    }))
                sent = False
                for source in info.get("sources", []):
                    name = source["name"]
                    directory = (supervisor.native_log_dir if name == "native"
                                 else supervisor.log_dir if name == "stdout" else None)
                    if directory is None:
                        continue
                    path = supervisor._confined_log_path(source["path"], directory)
                    if path is None:
                        continue
                    if not path.exists():
                        continue
                    pos = positions.get(name, int(source.get("start_offset") or 0))
                    try:
                        size = path.stat().st_size
                        if pos > size:
                            pos = int(source.get("start_offset") or 0)
                        if size > pos:
                            with path.open("rb") as handle:
                                handle.seek(pos)
                                chunk = handle.read(min(131072, size - pos))
                            positions[name] = pos + len(chunk)
                            await websocket.send_text(json.dumps({
                                "type": "data",
                                "source": name,
                                "data": chunk.decode("utf-8", errors="replace"),
                            }))
                            sent = True
                    except OSError:
                        continue
                now = asyncio.get_running_loop().time()
                if not sent and (now - last_heartbeat) >= 1.0:
                    await websocket.send_text(json.dumps({
                        "type": "heartbeat",
                        "status": info.get("status"),
                    }))
                    last_heartbeat = now
                if not sent:
                    await asyncio.sleep(0.18)
        except WebSocketDisconnect:
            return

    @app.post("/api/stop-all")
    def stop_all():
        deny_legacy_process_controls()
        return {lane: supervisor.stop(lane) for lane in LANES}

    @app.exception_handler(KeyError)
    async def key_error(_, exc: KeyError):
        return JSONResponse({"detail": "Requested dashboard resource was not found."}, status_code=404)

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="MJobs parallel-agent control dashboard")
    parser.add_argument("--root", default=".")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=18704, type=int)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--mission-control", action="store_true", help="use isolated Mission Control settings and runtime")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        parser.error("Agent dashboard is loopback-only; use 127.0.0.1 or localhost")
    app = create_app(Path(args.root), mission_instance=args.mission_control)
    if not args.no_open:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{args.port}/")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
