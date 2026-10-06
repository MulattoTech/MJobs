from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
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
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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


def claim(root: Path, args) -> int:
    data = load_claims(root)
    current = data["claims"]
    requested_paths = args.path or []
    conflicts = []
    for other_lane, other in current.items():
        if other_lane == args.lane or other.get("status") not in ("ACTIVE", "PAUSED_READY"):
            continue
        for rp in requested_paths:
            for op in other.get("paths", []):
                if paths_overlap(rp, op):
                    conflicts.append({"lane": other_lane, "requested": rp, "claimed": op, "issue": other.get("issue")})
    if conflicts:
        print(json.dumps({"result": "CONFLICT", "conflicts": conflicts}, indent=2))
        return 3
    current[args.lane] = {
        "lane": args.lane,
        "status": "ACTIVE",
        "issue": args.issue,
        "requirement_id": args.requirement,
        "branch": args.branch,
        "worktree": args.worktree,
        "paths": requested_paths,
        "head": args.head,
        "claimed_at": utc_now(),
        "pid": args.pid,
    }
    save_claims(root, data)
    print(json.dumps({"result": "CLAIMED", "claim": current[args.lane]}, indent=2))
    return 0


def release_claim(root: Path, lane: str) -> int:
    data = load_claims(root)
    old = data["claims"].pop(lane, None)
    save_claims(root, data)
    print(json.dumps({"result": "RELEASED" if old else "ALREADY_FREE", "old": old}, indent=2))
    return 0


def seed_claims(root: Path) -> int:
    data = {"schema_version": SCHEMA_VERSION, "claims": {}}
    for lane in LANES:
        lane_file = root / "planning" / "lanes" / lane / "CLAIMS.json"
        if not lane_file.exists():
            continue
        obj = json.loads(lane_file.read_text(encoding="utf-8"))
        claims = obj.get("claims", [])
        if claims:
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
    c.add_argument("--path", action="append", default=[])

    rc = sub.add_parser("release-claim")
    rc.add_argument("--lane", required=True, choices=LANES)
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
        return release_claim(root, args.lane)
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
