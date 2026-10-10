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
    from .console_presentation import latest_run_excerpt, LiveDisplay
else:
    from console_io import bind_visible_console
    from console_presentation import latest_run_excerpt, LiveDisplay


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--lane", required=True, choices=("codex", "antigravity", "claude", "ollama"))
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--raw", action="store_true",
                        help="Show full unfiltered CLI transcript instead of readable activity")
    args=parser.parse_args()
    try:
        bind_visible_console(f"MJobs | {args.lane.upper()} | LIVE output viewer")
    except (OSError, RuntimeError) as exc:
        # There is no useful console if this fails; never spawn another agent.
        return 3
    print(f"MJobs | {args.lane.upper()} | Existing run output", flush=True)
    print("Read-only transcript viewer. No new AI worker was started.", flush=True)
    print("Press Ctrl+C to close this viewer without stopping the coding agent.", flush=True)
    print(f"Following: {args.log}", flush=True)
    if not args.raw:
        print("This is a summarized Codex exec log, not the interactive Codex TUI.", flush=True)
        print("Source diffs are omitted here; the complete raw log remains on disk.", flush=True)
    print("", flush=True)
    try:
        with args.log.open("rb") as stream:
            size = stream.seek(0,os.SEEK_END)
            start_at = max(0,size-750000)
            stream.seek(start_at,os.SEEK_SET)
            if start_at:
                stream.readline()  # Don't display the middle of an old line.
            recent = stream.read().decode("utf-8",errors="replace")
            if args.raw:
                print("\n".join(recent.splitlines()[-130:]), flush=True)
            else:
                print(latest_run_excerpt(recent), flush=True)
            display = LiveDisplay()
            last_message=time.monotonic()
            while True:
                block=stream.read()
                if block:
                    delta=block.decode("utf-8",errors="replace")
                    visible=delta if args.raw else display.feed(delta)
                    if visible:
                        print(visible,end="",flush=True)
                    last_message=time.monotonic()
                else:
                    if not args.raw and time.monotonic()-last_message>45:
                        print("[Waiting for new Codex output; no new coding agent was started.]",
                              flush=True)
                        last_message=time.monotonic()
                    time.sleep(0.45)
    except KeyboardInterrupt:
        print("\nClosing log viewer; coding agent unaffected.",flush=True)
        return 0
    except OSError as exc:
        print(f"Cannot tail log: {type(exc).__name__}",file=sys.stderr,flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
