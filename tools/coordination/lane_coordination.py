from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

LANES = ("codex", "antigravity", "ollama")
SCHEMA_VERSION = 1


def repo_root() -> Path:
    out = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
    return Path(out)


def common_dir(root: Path) -> Path:
    raw = subprocess.check_output(["git", "-C", str(root), "rev-parse", "--git-common-dir"], text=True).strip()
    p = Path(raw)
    if not p.is_absolute():
        p = (root / p).resolve()
    return p


def bus_dir(root: Path) -> Path:
    p = common_dir(root) / "mjobs-coordination"
    p.mkdir(parents=True, exist_ok=True)
    return p


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def claims_path(root: Path) -> Path:
    return bus_dir(root) / "claims.json"


def load_claims(root: Path) -> dict:
    path = claims_path(root)
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "claims": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_claims(root: Path, data: dict) -> None:
    path = claims_path(root)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def claims_lock(root: Path):
    """Serialize claim read/modify/write transactions across local processes."""
    path = bus_dir(root) / "claims.lock"
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def lock_dir(root: Path) -> Path:
    return bus_dir(root) / "integration.lock"


def lock_owner_file(root: Path) -> Path:
    return lock_dir(root) / "owner.json"


def read_lock(root: Path):
    p = lock_owner_file(root)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "UNREADABLE", "path": str(p)}


def acquire_lock(root: Path, lane: str) -> int:
    p = lock_dir(root)
    try:
        p.mkdir()
    except FileExistsError:
        owner = read_lock(root)
        print(json.dumps({"result": "BUSY", "owner": owner}, indent=2))
        return 2
    owner = {
        "schema_version": SCHEMA_VERSION,
        "lane": lane,
        "pid": os.getpid(),
        "acquired_at": utc_now(),
        "cwd": str(Path.cwd()),
        "main_sha": subprocess.check_output(["git", "-C", str(root), "rev-parse", "origin/main"], text=True).strip(),
    }
    lock_owner_file(root).write_text(json.dumps(owner, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": "ACQUIRED", "owner": owner}, indent=2))
    return 0


def release_lock(root: Path, lane: str) -> int:
    p = lock_dir(root)
    if not p.exists():
        print(json.dumps({"result": "ALREADY_FREE"}, indent=2))
        return 0
    owner = read_lock(root)
    if owner and owner.get("lane") != lane:
        print(json.dumps({"result": "NOT_OWNER", "owner": owner}, indent=2))
        return 2
    owner_file = lock_owner_file(root)
    if owner_file.exists():
        owner_file.unlink()
    p.rmdir()
    print(json.dumps({"result": "RELEASED", "lane": lane}, indent=2))
    return 0


def normalize_path(p: str) -> str:
    return p.replace("\\", "/").strip().lower().rstrip("/")


def paths_overlap(a: str, b: str) -> bool:
    a = normalize_path(a)
    b = normalize_path(b)
    a_base = a[:-3] if a.endswith("/**") else a
    b_base = b[:-3] if b.endswith("/**") else b
    if a == b:
        return True
    if a.endswith("/**") and (b == a_base or b.startswith(a_base + "/")):
        return True
    if b.endswith("/**") and (a == b_base or a.startswith(b_base + "/")):
        return True
    return False


def claim_key(lane: str, child_id: str | None = None) -> str:
    if child_id is None:
        return lane
    if lane != "codex":
        raise ValueError("child-scoped claims are currently supported only for the Codex lane")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", child_id):
        raise ValueError("child id must be 1-64 letters, digits, dots, underscores, or hyphens")
    return f"{lane}:{child_id}"


def claim_record(lane: str, issue: int, requirement: str, branch: str, worktree: str, paths: list[str], head: str | None = None, pid: int | None = None) -> dict:
    return {
        "lane": lane,
        "status": "ACTIVE",
        "issue": issue,
        "requirement_id": requirement,
        "branch": branch,
        "worktree": worktree,
        "paths": paths,
        "head": head,
        "claimed_at": utc_now(),
        "pid": pid,
    }


def put_claim(root: Path, key: str, record: dict) -> tuple[str, list[dict]]:
    with claims_lock(root):
        data = load_claims(root)
        current = data.setdefault("claims", {})
        if key in current:
            return "CLAIM_EXISTS", [{"claim_key": key, "issue": current[key].get("issue")}]
        conflicts = []
        requested_paths = record.get("paths", [])
        for other_key, other in current.items():
            if other.get("status") not in ("ACTIVE", "PAUSED_READY"):
                continue
            for requested in requested_paths:
                for existing in other.get("paths", []):
                    if paths_overlap(requested, existing):
                        conflicts.append({
                            "claim_key": other_key,
                            "lane": other.get("lane", other_key),
                            "requested": requested,
                            "claimed": existing,
                            "issue": other.get("issue"),
                        })
        if conflicts:
            return "CONFLICT", conflicts
        current[key] = record
        save_claims(root, data)
        return "CLAIMED", []


def release_claim_key(root: Path, key: str) -> dict | None:
    with claims_lock(root):
        data = load_claims(root)
        old = data.setdefault("claims", {}).pop(key, None)
        save_claims(root, data)
        return old


def claim(root: Path, args) -> int:
    try:
        key = claim_key(args.lane, getattr(args, "child_id", None))
    except ValueError as exc:
        print(json.dumps({"result": "INVALID", "reason": str(exc)}, indent=2))
        return 2
    record = claim_record(args.lane, args.issue, args.requirement, args.branch, args.worktree,
                          args.path or [], args.head, args.pid)
    result, conflicts = put_claim(root, key, record)
    if result != "CLAIMED":
        print(json.dumps({"result": result, "conflicts": conflicts}, indent=2))
        return 3 if result == "CONFLICT" else 2
    print(json.dumps({"result": result, "claim_key": key, "claim": record}, indent=2))
    return 0


def release_claim(root: Path, lane: str, child_id: str | None = None) -> int:
    try:
        key = claim_key(lane, child_id)
    except ValueError as exc:
        print(json.dumps({"result": "INVALID", "reason": str(exc)}, indent=2))
        return 2
    old = release_claim_key(root, key)
    print(json.dumps({"result": "RELEASED" if old else "ALREADY_FREE", "claim_key": key, "old": old}, indent=2))
    return 0


def seed_claims(root: Path) -> int:
    with claims_lock(root):
        data = load_claims(root)
        data.setdefault("claims", {})
        for lane in LANES:
            lane_file = root / "planning" / "lanes" / lane / "CLAIMS.json"
            if not lane_file.exists():
                continue
            obj = json.loads(lane_file.read_text(encoding="utf-8"))
            claims = obj.get("claims", [])
            if lane not in data["claims"] and claims:
                claim0 = claims[0]
                if claim0.get("status") in ("ACTIVE", "PAUSED_READY"):
                    data["claims"][lane] = {
                        **claim0,
                        "lane": lane,
                        "claimed_at": utc_now(),
                        "seeded_from": str(lane_file.relative_to(root)),
                    }
        save_claims(root, data)
    print(json.dumps({"result": "SEEDED", **data}, indent=2))
    return 0


def status(root: Path) -> int:
    main = subprocess.check_output(["git", "-C", str(root), "rev-parse", "origin/main"], text=True).strip()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "origin_main": main,
        "lock": read_lock(root),
        "claims": load_claims(root).get("claims", {}),
        "lane_files": {},
    }
    for lane in LANES:
        p = root / "planning" / "lanes" / lane / "CONTINUATION.json"
        if p.exists():
            payload["lane_files"][lane] = json.loads(p.read_text(encoding="utf-8"))
    print(json.dumps(payload, indent=2))
    return 0


def validate(root: Path) -> int:
    errors = []
    for lane in LANES:
        base = root / "planning" / "lanes" / lane
        for name in ("CONTINUATION.json", "IMPLEMENTATION_STATE.json", "PR_QUEUE.json", "CLAIMS.json", "SESSION_HANDOFF.md"):
            if not (base / name).exists():
                errors.append(f"missing {lane}/{name}")
        for name in ("CONTINUATION.json", "IMPLEMENTATION_STATE.json", "PR_QUEUE.json", "CLAIMS.json"):
            p = base / name
            if p.exists():
                try:
                    json.loads(p.read_text(encoding="utf-8"))
                except Exception as exc:
                    errors.append(f"invalid json {p}: {exc}")
    claims = load_claims(root).get("claims", {})
    active = [(lane, c) for lane, c in claims.items() if c.get("status") in ("ACTIVE", "PAUSED_READY")]
    for i, (lane_a, a) in enumerate(active):
        for lane_b, b in active[i + 1:]:
            for pa in a.get("paths", []):
                for pb in b.get("paths", []):
                    if paths_overlap(pa, pb):
                        errors.append(f"claim overlap {lane_a}:{pa} vs {lane_b}:{pb}")
    result = {"result": "PASS" if not errors else "FAIL", "errors": errors}
    print(json.dumps(result, indent=2))
    return 0 if not errors else 1


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="MJobs local parallel-lane coordination")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("status", "lock-status", "validate", "seed-claims"):
        sub.add_parser(name)

    a = sub.add_parser("acquire-lock")
    a.add_argument("--lane", required=True, choices=LANES)
    r = sub.add_parser("release-lock")
    r.add_argument("--lane", required=True, choices=LANES)

    c = sub.add_parser("claim")
    c.add_argument("--lane", required=True, choices=LANES)
    c.add_argument("--issue", type=int, required=True)
    c.add_argument("--requirement", required=True)
    c.add_argument("--branch", required=True)
    c.add_argument("--worktree", required=True)
    c.add_argument("--head")
    c.add_argument("--pid", type=int)
    c.add_argument("--child-id", help="stable per-child key (Codex only); legacy lane claims remain supported")
    c.add_argument("--path", action="append", default=[])

    rc = sub.add_parser("release-claim")
    rc.add_argument("--lane", required=True, choices=LANES)
    rc.add_argument("--child-id", help="release only this Codex child claim")
    return p


def main() -> int:
    args = parser().parse_args()
    root = repo_root()
    if args.cmd == "status":
        return status(root)
    if args.cmd == "lock-status":
        print(json.dumps({"lock": read_lock(root)}, indent=2))
        return 0
    if args.cmd == "validate":
        return validate(root)
    if args.cmd == "seed-claims":
        return seed_claims(root)
    if args.cmd == "acquire-lock":
        return acquire_lock(root, args.lane)
    if args.cmd == "release-lock":
        return release_lock(root, args.lane)
    if args.cmd == "claim":
        return claim(root, args)
    if args.cmd == "release-claim":
        return release_claim(root, args.lane, args.child_id)
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
