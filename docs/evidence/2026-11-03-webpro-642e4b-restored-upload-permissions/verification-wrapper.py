"""Bind the existing focused unittest driver to the owned restore source."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys

sys.path.insert(0, os.getcwd())
from cli import local_backup
path = Path(local_backup.__file__).resolve()
binding = {"module": "cli.local_backup", "path": str(path),
           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
(output / "restore-source-binding.json").write_text(json.dumps(binding, indent=2) + "\n", encoding="utf-8")
print("RESTORE_SOURCE_BINDING", json.dumps(binding), flush=True)
runpy.run_path("docs/evidence/2026-11-03-webpro-642e4b-resume-upload-privacy/focused-test-driver-v2.py",
              run_name="__main__")
