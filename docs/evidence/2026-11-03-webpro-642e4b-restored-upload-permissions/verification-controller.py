"""Owned offline restore verification; exact inputs and failed attempts are retained."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

WORK = Path(__file__).resolve().parents[1]
IMAGE = "sha256:b929faaf6cb5da407126085a812118c27aaa8ea7fad1596e830e9b4f6f63ebab"
OWNER = "webpro-20261103T031738Z-642e4b"
def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
def git(*args):
    return subprocess.run(["git", *args], cwd=WORK, capture_output=True, text=True, check=True).stdout.strip()
config_path = Path(sys.argv[1])
config = json.loads(config_path.read_text())
assert git("rev-parse", "HEAD") == config["expected_head"]
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
run = WORK / "outputs/verification" / ("webpro-642e4b-restore-" + config["label"] + "-" + stamp)
run.mkdir(parents=True, exist_ok=False)
inputs = run / "inputs"
inputs.mkdir()
source_paths = [
    "cli/local_backup.py",
    "tests/unit/test_restored_upload_privacy.py",
    "tests/unit/test_local_backup.py",
    "tests/unit/test_backup_config_snapshot.py",
    "tests/unit/test_backup_credential_restore.py",
    "tests/unit/test_data_export_bounds.py",
    "docs/evidence/2026-11-03-webpro-642e4b-resume-upload-privacy/focused-test-driver-v2.py",
    "outputs/run_restored_upload_checks.py",
    "outputs/run_restored_upload_capsule.py",
]
receipts = {"started_at": utc(), "label": config["label"], "head": git("rev-parse", "HEAD"),
            "tree": git("rev-parse", "HEAD^{tree}"), "tracked_diff_before": git("diff", "--stat"),
            "configuration": config, "inputs": {}, "commands": [], "linux": {}, "native": {}}
for relative in source_paths:
    path = WORK / relative
    data = path.read_bytes()
    copy = inputs / relative.replace("/", "__")
    copy.write_bytes(data)
    receipts["inputs"][relative] = {
        "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
        "git_blob": git("hash-object", "--path=" + relative, str(path)),
        "snapshot": str(copy.relative_to(run)),
    }
shutil.copyfile(config_path, inputs / "execution-config.json")
def command(argv, *, timeout=180, env=None):
    started = utc()
    process = subprocess.run(argv, cwd=WORK, capture_output=True, timeout=timeout, env=env)
    receipts["commands"].append({"argv": argv, "started_at": started, "finished_at": utc(),
                                "exit_code": process.returncode})
    return process
print("RUN_DIRECTORY", str(run), flush=True)
wrapper = "outputs/run_restored_upload_checks.py"
if config.get("linux", True):
    out = run / "linux"
    out.mkdir()
    name = "mjobs-642e4b-restore-" + config["label"] + "-" + stamp.lower()
    arguments = ["docker", "create", "--name", name,
                 "--label", "mjobs.owner=" + OWNER,
                 "--label", "mjobs.requirement=MJ-REQ-171",
                 "--label", "mjobs.role=restore-" + config["label"],
                 "--network", "none", "--user", "10001:10001",
                 "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                 "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777,size=256m",
                 "--mount", "type=bind,source=" + str(WORK) + ",target=/app,readonly",
                 "--mount", "type=bind,source=" + str(out) + ",target=/evidence",
                 "--workdir", "/app", "--env", "PYTHONDONTWRITEBYTECODE=1",
                 "--env", "PYTHONNOUSERSITE=1", IMAGE,
                 "python", wrapper, "/evidence", *config["selectors"]]
    made = command(arguments)
    (run / "linux-create.log").write_bytes(made.stdout + made.stderr)
    if made.returncode:
        raise RuntimeError("Owned test container creation failed; raw log retained")
    cid = made.stdout.decode().strip()
    inspected = command(["docker", "inspect", cid])
    info = json.loads(inspected.stdout)[0]
    assert info["Id"] == cid and info["Config"]["Labels"]["mjobs.owner"] == OWNER
    assert info["Config"]["Labels"]["mjobs.role"] == "restore-" + config["label"]
    receipts["linux"].update({"container_id": cid, "image_id": info["Image"],
                              "user": info["Config"]["User"], "network": info["HostConfig"]["NetworkMode"],
                              "rootfs_readonly": info["HostConfig"]["ReadonlyRootfs"],
                              "tmpfs": info["HostConfig"]["Tmpfs"],
                              "mounts": [{"destination": m["Destination"], "read_write": m["RW"]} for m in info["Mounts"]]})
    try:
        result = command(["docker", "start", "-a", cid], timeout=240)
        (run / "linux-console.log").write_bytes(result.stdout + result.stderr)
        receipts["linux"]["exit_code"] = result.returncode
        if (out / "execution.json").exists():
            receipts["linux"]["execution"] = json.loads((out / "execution.json").read_text())
        if (out / "restore-source-binding.json").exists():
            receipts["linux"]["restore_source_binding"] = json.loads((out / "restore-source-binding.json").read_text())
    except subprocess.TimeoutExpired as error:
        (run / "linux-console.log").write_bytes((error.stdout or b"") + (error.stderr or b""))
        receipts["linux"]["timeout"] = True
        command(["docker", "kill", cid], timeout=30)
    finally:
        gone = command(["docker", "rm", cid], timeout=30)
        absent = command(["docker", "inspect", cid], timeout=30)
        receipts["linux"]["cleanup"] = {"remove_exit_code": gone.returncode, "absence_inspect_exit_code": absent.returncode}
        assert gone.returncode == 0 and absent.returncode != 0
if config.get("native", False):
    out = run / "native"
    out.mkdir()
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(WORK))
    result = command([sys.executable, "-B", wrapper, str(out), *config["selectors"]], env=environment)
    (run / "native-console.log").write_bytes(result.stdout + result.stderr)
    receipts["native"]["exit_code"] = result.returncode
    if (out / "execution.json").exists():
        receipts["native"]["execution"] = json.loads((out / "execution.json").read_text())
    if (out / "restore-source-binding.json").exists():
        receipts["native"]["restore_source_binding"] = json.loads((out / "restore-source-binding.json").read_text())
    if config.get("native_pytest"):
        pytest_out = run / "native-existing"
        pytest_out.mkdir()
        result = command([sys.executable, "-B", "-m", "pytest", "-q", *config["native_pytest"],
                          "--junitxml=" + str(pytest_out / "tests.xml")], env=environment)
        (pytest_out / "console.log").write_bytes(result.stdout + result.stderr)
        receipts["native"]["existing_pytest_exit_code"] = result.returncode
receipts["finished_at"] = utc()
receipts["head_after"] = git("rev-parse", "HEAD")
receipts["inputs_unchanged"] = all(digest(WORK / relative) == value["sha256"] for relative, value in receipts["inputs"].items())
receipts["artifact_sha256"] = {str(path.relative_to(run)): digest(path) for path in sorted(run.rglob("*")) if path.is_file()}
receipt = run / "execution-summary.json"
receipt.write_text(json.dumps(receipts, indent=2) + "\n", encoding="utf-8")
print("RECEIPT", str(receipt), "SHA256", digest(receipt), flush=True)
for platform in ("linux", "native"):
    result = receipts[platform]
    if result:
        print(platform.upper(), json.dumps({"exit_code": result.get("exit_code"), "execution": result.get("execution"),
                                             "existing_pytest_exit_code": result.get("existing_pytest_exit_code"),
                                             "cleanup": result.get("cleanup")}), flush=True)
