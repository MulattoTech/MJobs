"""Open a read-only LIVE Windows console for current/last MJobs CLI log.

Does not start another coding agent. Ctrl+C closes this viewer only.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time

if __package__:
    from .console_io import bind_visible_console
else:
    from console_io import bind_visible_console


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--lane", required=True, choices=("codex", "antigravity", "claude", "ollama"))
    parser.add_argument("--log", required=True, type=Path)
    args=parser.parse_args()
    try:
        bind_visible_console(f"MJobs | {args.lane.upper()} | LIVE output viewer")
    except (OSError, RuntimeError) as exc:
        # There is no useful console if this fails; never spawn another agent.
        return 3
    print(f"MJobs | {args.lane.upper()} | Existing run output", flush=True)
    print("Read-only transcript viewer. No new AI worker was started.", flush=True)
    print("Press Ctrl+C to close this viewer without stopping the coding agent.", flush=True)
    print(f"Following: {args.log}\n", flush=True)
    try:
        with args.log.open("rb") as stream:
            stream.seek(0,os.SEEK_END)
            end=stream.tell()
            stream.seek(max(0,end-22000),os.SEEK_SET)
            start=stream.read().decode("utf-8",errors="replace")
            print(start[-20000:],flush=True)
            while True:
                block=stream.read()
                if block:
                    print(block.decode("utf-8",errors="replace"),end="",flush=True)
                else:
                    time.sleep(0.45)
    except KeyboardInterrupt:
        print("\nClosing log viewer; coding agent unaffected.",flush=True)
        return 0
    except OSError as exc:
        print(f"Cannot tail log: {type(exc).__name__}",file=sys.stderr,flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
