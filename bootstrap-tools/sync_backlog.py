#!/usr/bin/env python3
"""Idempotently create the MJobs GitHub issue catalogue using the gh CLI."""
from __future__ import annotations
import argparse, json, os, pathlib, subprocess, tempfile, time
from contextlib import contextmanager

ROOT = pathlib.Path(__file__).resolve().parents[1]
SEED = ROOT / "planning" / "ISSUE_SEED.jsonl"
LABELS = ROOT / "planning" / "LABELS.json"
MAP = ROOT / "planning" / "GITHUB_ISSUE_MAP.json"

def run(args, check=True):
    try:
        p = subprocess.run(args, text=True, encoding="utf-8", capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise SystemExit("BLOCKED_EXTERNAL: PROCESS_TIMEOUT; reconcile remote state before retry") from None
    if check and p.returncode:
        raise SystemExit(f"Command failed ({p.returncode}): {' '.join(args)}\n{p.stderr}")
    return p

def gh_json(args):
    return json.loads(run(["gh", *args]).stdout or "null")

def seed():
    return [json.loads(x) for x in SEED.read_text(encoding="utf-8").splitlines() if x.strip()]

def ensure_labels(repo):
    present = {row["name"] for row in gh_json(["api", "--paginate", f"repos/{repo}/labels"])}
    for x in json.loads(LABELS.read_text(encoding="utf-8")):
        if x["name"] in present:
            continue
        run(["gh","label","create",x["name"],"--repo",repo,"--description",x["description"],
             "--color","BFD4F2"])

def existing(repo):
    out, page = {}, 1
    while True:
        rows = gh_json(["api","--method","GET",f"repos/{repo}/issues","-f","state=all","-f","per_page=100","-f",f"page={page}"])
        if not rows: break
        for issue in rows:
            if "pull_request" in issue: continue
            body = issue.get("body") or ""
            start = body.find("<!-- mj-requirement:")
            if start >= 0:
                end = body.find("-->", start)
                if end > start:
                    rid = body[start+len("<!-- mj-requirement:"):end].strip()
                    if rid in out:
                        raise SystemExit(f"BLOCKED_EXTERNAL: duplicate requirement marker {rid}")
                    out[rid] = issue["number"]
        if len(rows) < 100: break
        page += 1
    return out

def write_map(repo, mapping, path=MAP, pending=None):
    data = {"schema_version": 1, "repository": repo, "issues": dict(sorted(mapping.items()))}
    if pending is not None:
        data["pending_creation"] = pending
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tf:
        temporary = pathlib.Path(tf.name)
        json.dump(data, tf, indent=2)
        tf.write("\n")
        tf.flush()
        os.fsync(tf.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

def pid_alive(pid):
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87
        try:
            status = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(status)):
                return True
            return status.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

@contextmanager
def mutation_lock(path, recover=False):
    path = pathlib.Path(path)
    if path.exists() and recover:
        contents = path.read_text(encoding="utf-8")
        try:
            owner = int(contents)
        except ValueError:
            raise SystemExit("BLOCKED_ENVIRONMENT: SYNC_LOCKED; invalid owner, preserve for inspection") from None
        if not pid_alive(owner) and path.read_text(encoding="utf-8") == contents:
            path.unlink()
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SystemExit(f"BLOCKED_ENVIRONMENT: SYNC_LOCKED; inspect owner in {path}") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)

def synchronize(repo, items, path=MAP, delay=1.5, labels=False):
    path = pathlib.Path(path)
    mapping = existing(repo)
    if path.exists():
        try:
            prior = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(prior, dict) or not isinstance(prior.get("repository"), str)
                    or not isinstance(prior.get("issues"), dict)
                    or any(not isinstance(rid, str) or type(number) is not int or number <= 0
                           for rid, number in prior["issues"].items())):
                raise ValueError("Invalid map schema")
        except (ValueError, TypeError):
            raise SystemExit("BLOCKED_ENVIRONMENT: INVALID_MAP; preserve existing map for inspection") from None
        if prior["repository"] != repo:
            raise SystemExit("Map belongs to a different repository")
        pending = prior.get("pending_creation")
        if pending and pending not in mapping:
            raise SystemExit(f"BLOCKED_EXTERNAL: ISSUE_CREATION_UNCONFIRMED {pending}; verify GitHub before clearing pending_creation")
        for rid, number in prior["issues"].items():
            if mapping.get(rid) != number:
                raise SystemExit(f"BLOCKED_EXTERNAL: remote issue map drift for {rid}")
    write_map(repo, mapping, path)
    if labels:
        ensure_labels(repo)
    created = 0
    for item in items:
        if item["id"] in mapping:
            continue
        write_map(repo, mapping, path, pending=item["id"])
        number = create_issue(repo, item)
        mapping[item["id"]] = number
        write_map(repo, mapping, path)
        created += 1
        print(f"created {item['id']} -> #{number}", flush=True)
        time.sleep(delay)
    return created, mapping

def create_issue(repo, item):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as tf:
        tf.write(item["body"])
        body_path = pathlib.Path(tf.name)
    try:
        cmd=["gh","issue","create","--repo",repo,"--title",item["title"],"--body-file",str(body_path)]
        for label in item.get("labels",[]): cmd += ["--label",label]
        url=run(cmd).stdout.strip()
        return int(url.rstrip("/").split("/")[-1])
    finally:
        body_path.unlink(missing_ok=True)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--repo",required=True)
    ap.add_argument("--dry-run",action="store_true")
    ap.add_argument("--recover-stale-lock",action="store_true",help="Recover only a provably dead local sync owner")
    a=ap.parse_args()
    run(["gh","auth","status"])
    items=seed()
    if a.dry_run:
        print(f"Would synchronize {len(items)} catalogue issues into {a.repo}.")
        return
    with mutation_lock(MAP.with_suffix(".lock"), recover=a.recover_stale_lock):
        created, mapping = synchronize(a.repo, items, labels=True)
    print(f"Done. {created} created; {len(mapping)} total mapped. Wrote {MAP}")

if __name__=="__main__":
    main()
