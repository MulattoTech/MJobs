"""Visible or headless supervised MJobs agent runner.

This helper accepts the exact approved prompt on standard input to the REAL
CLI (Codex exec -). It never uses cmd.exe quoting for the prompt, never
publishes Git changes, and cannot spawn agents on its own except the one
already approved CLI invocation recorded in its local manifest.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

if __package__:
    from .console_io import bind_visible_console, set_console_title
else:
    from console_io import bind_visible_console, set_console_title


def _echo(text: str, logfile) -> None:
    logfile.write(text)
    logfile.flush()
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except (OSError, UnicodeError):
        pass


def run_manifest(manifest: Path) -> int:
    """Run exactly the manifest-specified program and mirror output to the log."""
    with manifest.open("r", encoding="utf-8") as stream:
        settings: dict[str, Any] = json.load(stream)
    # The complete prompt may contain private repo/job context. Remove the
    # short-lived manifest once this child owns the immutable in-memory data.
    manifest.unlink(missing_ok=True)
    command = settings.get("argv")
    prompt = settings.get("prompt")
    workspace = Path(settings["workspace"])
    log_path = Path(settings["log"])
    visible = settings.get("mode") == "terminal"
    hold_seconds = int(settings.get("hold_seconds", 25 if visible else 0))
    if not isinstance(command, list) or not command or not all(
        isinstance(x, str) and x for x in command
    ):
        raise ValueError("Invalid command list in supervised agent manifest")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Approved prompt missing from supervised agent manifest")
    if not workspace.is_dir():
        raise ValueError("Claimed agent worktree no longer exists")
    if not 0 <= hold_seconds <= 60:
        raise ValueError("Invalid visible-console linger")
    stdin_mode = settings.get("lane") == "codex"
    if stdin_mode and command[-1] != "-":
        raise ValueError("Codex must read the reviewed prompt from stdin")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", errors="replace", buffering=1) as log:
        if visible:
            try:
                bind_visible_console(
                    "MJobs | " + str(settings.get("lane", "agent")).upper() + " | LIVE Coding CLI"
                )
            except (OSError, RuntimeError) as exc:
                # Do not launch a paid model against a blank output window.
                log.write("VISIBLE_CONSOLE_BIND_FAILED: " + str(exc)[:260] + "\n")
                return 3
        _echo(
            "\n=== MJobs supervised agent: " + str(settings.get("lane")) +
            " | " + ("VISIBLE CONSOLE" if visible else "HEADLESS") + " ===\n"
            "Workspace: " + str(workspace) + "\n"
            "Approved prompt: " + str(len(prompt)) + " characters " +
            ("via stdin" if stdin_mode else "via CLI argument") + "\n"
            "Command: " + " ".join(
                "<approved prompt via stdin>" if (stdin_mode and arg == "-")
                else "<approved prompt>" if arg == prompt else arg
                for arg in command
            ) + "\n"
            "This is the actual coding CLI process, not a duplicate log replay.\n\n",
            log,
        )
        if visible and os.name == "nt":
            try:
                import ctypes
                hwnd = ctypes.windll.kernel32.GetConsoleWindow()
                shown = bool(ctypes.windll.user32.IsWindowVisible(hwnd)) if hwnd else False
                _echo(f"Console window handle: {hwnd}; visible: {shown}\n", log)
            except (AttributeError, OSError):
                _echo("Console handle diagnostic unavailable.\n", log)
        process = None
        try:
            process = subprocess.Popen(
                command, cwd=str(workspace),
                stdin=subprocess.PIPE if stdin_mode else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            if visible:
                # The npm .cmd wrapper may set the title to cmd.exe when started.
                set_console_title(
                    "MJobs | " + str(settings.get("lane", "agent")).upper() + " | LIVE Coding CLI"
                )
            assert process.stdout is not None
            if stdin_mode:
                assert process.stdin is not None
                # A bare "-" Codex argument reads the entire approved text from stdin.
                # Unlike Windows cmd.exe /c, this preserves every newline and quote.
                try:
                    process.stdin.write(prompt)
                    process.stdin.flush()
                finally:
                    process.stdin.close()
            with process.stdout as output:
                for line in output:
                    _echo(line, log)
            code = process.wait()
            _echo(
                f"\n=== CLI EXIT {code} ===\n"
                + ("Terminal will remain visible briefly for inspection.\n" if hold_seconds else ""),
                log,
            )
            if hold_seconds:
                time.sleep(hold_seconds)
            return int(code)
        except BaseException as exc:
            _echo("\n=== RUNNER ERROR " + type(exc).__name__ +
                  ": " + str(exc)[:350] + " ===\n", log)
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    return run_manifest(args.manifest)


if __name__ == "__main__":
    raise SystemExit(main())
