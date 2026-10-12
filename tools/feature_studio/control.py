"""Local-first Feature Studio: durable drafts, safe repo inventory, review plans.

No remote write, agent launch, or automatic main merge occurs in this module.
SQLite database lives outside the Git checkout in the user's home data directory.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone
from typing import Any

from .validate import STUDIO, validate_spec

REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")
SAFE_ID = re.compile(r"^FS-[A-Z0-9-]{1,60}$")
DEFAULT_DB = Path(os.getenv("LOCALAPPDATA") or (Path.home() / ".local" / "share")) / "MJobs" / "feature-studio.sqlite3"


def command(args: list[str], *, timeout: int = 20) -> str:
    proc = subprocess.run(args, text=True, capture_output=True, encoding="utf-8",
                          errors="replace", timeout=timeout, check=False,
                          stdin=subprocess.DEVNULL)
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout)[:300] or "Command failed")
    return proc.stdout


def utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class StudioStore:
    def __init__(self, db: Path = DEFAULT_DB):
        self.db = Path(db)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.schema = json.loads((STUDIO / "schema.v1.json").read_text(encoding="utf-8"))
        with self.connect() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS specifications(
                repo TEXT NOT NULL, spec_id TEXT NOT NULL, revision INTEGER NOT NULL,
                digest TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(repo,spec_id));
            CREATE TABLE IF NOT EXISTS events(
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
                repo TEXT NOT NULL, spec_id TEXT NOT NULL, revision INTEGER NOT NULL,
                digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests(
                idempotency_key TEXT PRIMARY KEY, repo TEXT NOT NULL,
                spec_id TEXT NOT NULL, revision INTEGER NOT NULL, digest TEXT NOT NULL);
            """)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def digest(spec: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def save(self, spec: dict[str, Any], *, expected_revision: int | None, key: str) -> dict[str, Any]:
        errors = validate_spec(spec, self.schema)
        if errors:
            raise ValueError("; ".join(errors)[:400])
        if not key or len(key) > 128 or not re.fullmatch(r"[a-zA-Z0-9_.:-]+", key):
            raise ValueError("A safe idempotency key is required")
        if not SAFE_ID.fullmatch(spec["id"]) or not REPO_NAME.fullmatch(spec["repository"]):
            raise ValueError("Invalid feature or repo identity")
        if spec["lifecycle"] != "DRAFT" or spec["execution"]["spec_review_approved"] or spec["execution"]["agent_execution_authorized"]:
            raise ValueError("This pilot only saves unapproved drafts")
        value = json.dumps(spec, sort_keys=True)
        digest = self.digest(spec)
        with self.lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            repeat = conn.execute("SELECT * FROM requests WHERE idempotency_key=?", (key,)).fetchone()
            if repeat:
                if repeat["repo"] != spec["repository"] or repeat["spec_id"] != spec["id"] or repeat["digest"] != digest:
                    raise ValueError("Idempotency key reused with different content")
                return {"repo": repeat["repo"], "id": repeat["spec_id"], "revision": repeat["revision"], "digest": digest, "replayed": True}
            old = conn.execute("SELECT revision FROM specifications WHERE repo=? AND spec_id=?", (spec["repository"],spec["id"])).fetchone()
            existing = old["revision"] if old else 0
            if expected_revision != existing:
                raise ValueError(f"Revision conflict: expected {existing}, got {expected_revision}")
            revision = existing + 1
            conn.execute("INSERT INTO specifications(repo,spec_id,revision,digest,payload,updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT(repo,spec_id) DO UPDATE SET revision=excluded.revision,digest=excluded.digest,payload=excluded.payload,updated_at=excluded.updated_at",
                         (spec["repository"],spec["id"],revision,digest,value,utc()))
            conn.execute("INSERT INTO requests VALUES (?,?,?,?,?)",(key,spec["repository"],spec["id"],revision,digest))
            conn.execute("INSERT INTO events(timestamp,actor,action,repo,spec_id,revision,digest) VALUES (?,?,?,?,?,?,?)",
                         (utc(),"local-user","save-draft",spec["repository"],spec["id"],revision,digest))
        return {"repo": spec["repository"], "id": spec["id"], "revision": revision, "digest": digest, "replayed": False}

    def list(self, repo: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            query = "SELECT repo,spec_id,revision,digest,updated_at FROM specifications"
            if repo:
                rows=conn.execute(query+" WHERE repo=? ORDER BY updated_at DESC LIMIT 200",(repo,)).fetchall()
            else:
                rows=conn.execute(query+" ORDER BY updated_at DESC LIMIT 200").fetchall()
            return [dict(row) for row in rows]

    def get(self, repo: str, spec_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row=conn.execute("SELECT * FROM specifications WHERE repo=? AND spec_id=?",(repo,spec_id)).fetchone()
            return dict(row) | {"spec":json.loads(row["payload"])} if row else None

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM events ORDER BY event_id DESC LIMIT ?",(min(limit,200),))]


def inventory(owner: str = "MulattoTech") -> list[dict[str, Any]]:
    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", owner):
        raise ValueError("Invalid owner")
    output = command(["gh","repo","list",owner,"--limit","1000","--json","nameWithOwner,isArchived,visibility,defaultBranchRef"])
    return [{"repository":x["nameWithOwner"], "archived":x["isArchived"],
             "visibility":x["visibility"], "default_branch":(x.get("defaultBranchRef") or {}).get("name")}
            for x in json.loads(output)]


def probe(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """Ground architecture guesses in tracked paths. No code execution."""
    root=root.resolve()
    tracked=command(["git","-C",str(root),"ls-files"],timeout=15).splitlines()
    ranked=[]
    terms=set(re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{3,}",spec["title"].lower()+" "+spec["problem"].lower()))
    ignored={"feature","studio","this","test","with","from","that","should","user","have","will"}
    terms-=ignored
    for path in tracked:
        lowered=path.lower().replace("\\","/")
        score=sum(3 for term in terms if term in lowered)
        if spec["scope"] in {"full","frontend"} and lowered.startswith("apps/web/"):score+=1
        if spec["scope"] in {"full","backend"} and lowered.startswith("apps/api/"):score+=1
        if lowered.startswith(("docs/spec/","tests/")):score+=1
        if score:ranked.append((score,path))
    ranked.sort(key=lambda p:(-p[0],p[1]))
    return {"tracked_files":len(tracked),"candidates":[{"path":p,"score":s} for s,p in ranked[:30]],
            "questions":[
                "Which existing API and data contracts must remain compatible?",
                "What exact observable evidence will establish each acceptance criterion?",
                "Are there related in-flight branches or migrations that must serialize?",
                "Which read-only tests can establish the baseline before editing?"],
            "confidence":"heuristic: candidate paths require human/agent inspection",
            "execution_started":False}


def probe_remote(repository: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Inspect a selected, accessible GitHub repository without running its code."""
    from tempfile import TemporaryDirectory
    if not REPO_NAME.fullmatch(repository) or repository != spec["repository"]:
        raise ValueError("Repository name and specification must match")
    # Allow only repositories returned by the caller's authenticated GitHub CLI.
    available={entry["repository"] for entry in inventory(repository.split("/")[0])
               if not entry["archived"]}
    if repository not in available:
        raise ValueError("Repository is not accessible or is archived")
    with TemporaryDirectory(prefix="feature-studio-probe-") as temp:
        command(["gh","repo","clone",repository,temp,"--","--depth","1"],timeout=70)
        result=probe(Path(temp),spec)
    result["repository"]=repository
    result["source"]="authenticated shallow clone; inspected only tracked filenames"
    return result


def plan(spec: dict[str, Any], discovery: dict[str, Any]) -> dict[str, Any]:
    paths=[c["path"] for c in discovery.get("candidates",[])]
    scopes=[]
    for label,prefix in (("backend","apps/api/"),("frontend","apps/web/"),("tests","tests/"),("documentation","docs/")):
        selected=[x for x in paths if x.startswith(prefix)]
        if selected:scopes.append({"workstream":label,"candidate_paths":selected,"exclusive_owner_required":True})
    if not scopes:scopes=[{"workstream":"discovery","candidate_paths":[],"exclusive_owner_required":True}]
    return {"spec_id":spec["id"],"repository":spec["repository"],"workstreams":scopes,
            "execution_enabled":False,"approval_required":True,
            "notes":"Planning only. A human reviews file ownership, dependencies and CI before any agent is launched."}
