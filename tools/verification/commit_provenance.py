"""Validate machine-readable authorship and acceptance trailers on AI source commits.

Usage: python -m tools.verification.commit_provenance [--commit HEAD] [--strict]
Human commits without AI-Agent are exempt by default; --strict checks all
source commits and is appropriate for AI-only PRs. AI markers anywhere in the
body require validation, but only Git's final trailer block supplies metadata.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess
import sys

EXTENSIONS={".py",".ps1",".go",".js",".jsx",".ts",".tsx",".java",
            ".rb",".php",".cs",".sh",".sql",".css",".html"}
KEYS=("AI-Agent","AI-Model","Requirement","Validation","UTC-Date")
AGENT_NAMES={"chatgpt", "codex", "openai", "claude", "agent", "copilot", "antigravity", "gemini", "agy"}

def git(root:Path,*args:str)->str:
    p=subprocess.run(["git","-C",str(root),*args],capture_output=True,
                     text=True,encoding="utf-8",errors="replace",check=False)
    if p.returncode:
        raise RuntimeError(p.stderr[-400:])
    return p.stdout

def changed_sources(root:Path,commit:str)->list[str]:
    paths=git(root,"show","--format=","--name-only",commit).splitlines()
    return [p for p in paths if Path(p).suffix.lower() in EXTENSIONS
            and not p.startswith(("docs/","planning/","prompts/"))]

def validate(root:Path,commit:str,*,strict:bool=False)->tuple[bool,str]:
    files=changed_sources(root,commit)
    if not files:
        return True,"PASS: no source-code changes in this commit"
    body=git(root,"show","-s","--format=%B",commit)
    author=git(root,"show","-s","--format=%an",commit).strip()
    # A misplaced body marker must not turn an AI change into a human exemption.
    is_ai=bool(re.search(r"(?im)^AI-Agent:",body) or
               any(word in author.lower() for word in AGENT_NAMES))
    if not is_ai and not strict:
        return True,"SKIP: human-authored source commit; AI trailers not required"
    # Git owns trailer boundaries and continuation semantics. Searching the whole
    # message incorrectly accepts separated paragraphs in squash commit bodies.
    parsed=subprocess.run(["git","-C",str(root),"interpret-trailers","--parse"],
                          input=body,capture_output=True,text=True,encoding="utf-8",
                          errors="replace",check=False)
    if parsed.returncode:
        raise RuntimeError("Git could not parse commit trailers")
    trailers={}
    for key in KEYS:
        match=re.findall(rf"(?im)^{re.escape(key)}:[ \t]*(.*)$",parsed.stdout)
        if match: trailers[key]=match[-1].strip()
    missing=[key for key in KEYS if not trailers.get(key)]
    if missing:
        return False,"FAIL: missing commit trailers: "+", ".join(missing)
    try:
        stamp=datetime.fromisoformat(trailers["UTC-Date"].replace("Z","+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset()!=timezone.utc.utcoffset(stamp):
            raise ValueError("not UTC")
    except ValueError:
        return False,"FAIL: UTC-Date must be a valid UTC ISO-8601 timestamp"
    if len(trailers["Requirement"])>240 or len(trailers["Validation"])>600:
        return False,"FAIL: provenance trailers exceed length limits"
    return True,"PASS: provenance complete for "+str(len(files))+" source files"

def main()->int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--commit",default="HEAD")
    parser.add_argument("--strict",action="store_true")
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[2]
    try:
        success,message=validate(root,args.commit,strict=args.strict)
    except (OSError,RuntimeError,ValueError) as exc:
        print("FAIL: cannot verify commit: "+str(exc))
        return 1
    print(message)
    return 0 if success else 1

if __name__=="__main__":
    sys.exit(main())
