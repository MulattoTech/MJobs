"""Discover a validated Codex model dropdown from the installed CLI only."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any

MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,119}$")
CODEX_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
RUNNER = {"codex": "codex"}
AGENT_PRESETS = {"codex": [
    "General Developer", "Application Architect", "ATS Adapter Engineer",
    "Automation Specialist", "Recovery Engineer", "Safety and Reliability Engineer",
    "Test Engineer",
]}


def _choice(model_id: str, label: str, efforts: list[str], default: str,
            *, embedded: bool = False) -> dict[str, Any]:
    return {"id": model_id, "label": label, "efforts": efforts,
            "default_effort": default, "effort_embedded": embedded}


class ModelCatalog:
    def __init__(self, supervisor: Any, *, ttl_seconds: float = 180.0):
        self.supervisor = supervisor
        self.ttl = ttl_seconds
        self.cache_path = supervisor.bus / "mission-model-catalog.json"
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._disk_cache: dict[str, dict[str, Any]] = {}
        if self.cache_path.is_file():
            try:
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("version") == 1:
                    self._disk_cache = {"codex": dict((data.get("lanes") or {}).get("codex") or {})}
            except (OSError, ValueError, TypeError):
                pass

    @staticmethod
    def _run(args: list[str], cwd: Path, timeout: float = 22.0) -> str:
        result = subprocess.run(
            args, cwd=str(cwd), check=False, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            raise RuntimeError("Installed Codex CLI model discovery returned an error.")
        return result.stdout

    def _discover(self, lane: str) -> tuple[list[dict[str, Any]], str]:
        if lane != "codex":
            raise ValueError("Only Codex models are supported")
        executable = self.supervisor._runner_prefix("codex")
        text = self._run(executable + ["debug", "models"], Path.home())
        data = json.loads(text)
        models: list[dict[str, Any]] = []
        for item in data.get("models", []):
            if not isinstance(item, dict) or item.get("visibility") != "list":
                continue
            mid = str(item.get("slug") or "")
            if not MODEL_ID.fullmatch(mid):
                continue
            efforts = [str(x.get("effort")) for x in item.get("supported_reasoning_levels", [])
                       if isinstance(x, dict) and x.get("effort") in CODEX_EFFORTS]
            efforts = list(dict.fromkeys(efforts))
            if not efforts:
                continue
            default = str(item.get("default_reasoning_level") or "high")
            if default not in efforts:
                default = efforts[0]
            models.append(_choice(mid, str(item.get("display_name") or mid), efforts, default))
        if not models:
            raise RuntimeError("Installed Codex CLI returned no selectable models.")
        return models, "live_codex_catalog"

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "lanes": {"codex": self._disk_cache.get("codex", {})}}
        tmp = self.cache_path.with_name(self.cache_path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.cache_path)

    def get(self, lane: str, *, refresh: bool = False) -> dict[str, Any]:
        if lane != "codex":
            raise ValueError("Only Codex models are supported")
        with self._lock:
            now = time.monotonic()
            cached = self._cache.get(lane)
            if cached and not refresh and now - cached[0] < self.ttl:
                return dict(cached[1])
            try:
                models, source = self._discover(lane)
                stamp = datetime.now(timezone.utc).isoformat()
                result = {"lane": lane, "models": models, "source": source,
                          "discovered_at": stamp, "stale": False, "warning": "",
                          "agent_presets": list(AGENT_PRESETS[lane])}
                self._disk_cache[lane] = {"models": models, "source": source,
                                          "discovered_at": stamp}
                self._save_cache()
            except (ValueError, RuntimeError, FileNotFoundError, OSError,
                    subprocess.TimeoutExpired, json.JSONDecodeError):
                stored = self._disk_cache.get(lane) or {}
                models = stored.get("models") or []
                result = {"lane": lane, "models": models,
                          "source": "cached_catalog" if models else "unavailable",
                          "discovered_at": stored.get("discovered_at"), "stale": True,
                          "warning": ("Live CLI discovery unavailable. Last known models shown; "
                                      "availability must be confirmed at launch."
                                      if models else "Codex model discovery failed. Check CLI and authentication."),
                          "agent_presets": list(AGENT_PRESETS[lane])}
            self._cache[lane] = (now, result)
            return dict(result)

    def validate(self, lane: str, model_id: str, effort: str) -> dict[str, Any]:
        catalog = self.get(lane)
        item = next((m for m in catalog["models"] if m["id"] == model_id), None)
        if item is None:
            raise ValueError("Select an available model from the CLI dropdown; "
                             "refresh the list if Codex was recently updated.")
        if effort not in item["efforts"]:
            raise ValueError("The selected model does not support that reasoning setting.")
        return {"model": item, "catalog": catalog}
