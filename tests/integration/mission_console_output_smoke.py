"""Windows GUI-console smoke without Codex, model credits, or workspace mutations.

Launches only TWO synthetic/local Python processes with redirected stdout.
Verifies text in the allocated console SCREEN BUFFER, not just existence of
a window. No AI agents or repository locks are touched.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parents[2] / "tools" / "agent_dashboard"
# Windows venv python.exe can be a launcher shim; terminate/await the ACTUAL
# interpreter to avoid orphaned descendants retaining temp fixture file handles.
REAL_PYTHON = str(getattr(sys, "_base_executable", sys.executable))


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("Left", ctypes.c_short), ("Top", ctypes.c_short),
        ("Right", ctypes.c_short), ("Bottom", ctypes.c_short),
    ]


class BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", COORD), ("dwCursorPosition", COORD),
        ("wAttributes", wintypes.WORD), ("srWindow", RECT),
        ("dwMaximumWindowSize", COORD),
    ]


def _read_screen(pid: int) -> dict:
    """Only invoked in a sacrificial diagnostic child, never in a real agent."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.FreeConsole()
    if not kernel.AttachConsole(pid):
        return {"error": "Cannot attach to synthetic console"}
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.ReadConsoleOutputCharacterW.argtypes = [
        wintypes.HANDLE, ctypes.c_wchar_p, wintypes.DWORD,
        COORD, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.GetConsoleScreenBufferInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(BUFFER_INFO),
    ]
    handle = kernel.CreateFileW("CONOUT$", 0xC0000000, 3, None, 3, 0, None)
    info = BUFFER_INFO()
    if not kernel.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
        return {"error": "Cannot read synthetic console screen"}
    rows = []
    width = min(145, info.srWindow.Right - info.srWindow.Left + 1)
    y0 = info.srWindow.Top
    for y in range(y0, min(info.srWindow.Bottom + 1, y0 + 38)):
        text = ctypes.create_unicode_buffer(width + 1)
        count = wintypes.DWORD()
        if kernel.ReadConsoleOutputCharacterW(
            handle, text, width, COORD(info.srWindow.Left, y), ctypes.byref(count)
        ):
            rows.append(text.value.rstrip())
    return {"text": "\n".join(rows), "cursor_y": info.dwCursorPosition.Y}


def _probe(pid: int, target: Path) -> int:
    target.write_text(json.dumps(_read_screen(pid)), encoding="utf-8")
    return 0


def _await_console(marker: str, pid: int, tmp: Path, wait_seconds: float = 9) -> str:
    diagnostic = tmp / f"console-{pid}.json"
    deadline = time.monotonic() + wait_seconds
    last = ""
    while time.monotonic() < deadline:
        try:
            subprocess.run(
                [REAL_PYTHON, __file__, "--probe", str(pid), str(diagnostic)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5, check=True,
            )
            data = json.loads(diagnostic.read_text(encoding="utf-8"))
            last = data.get("text") or data.get("error", "")
            if marker in last:
                return last
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        time.sleep(0.35)
    raise AssertionError(
        "Expected content did not appear in visible console buffer: "
        + repr(last[:350])
    )


def smoke() -> None:
    if os.name != "nt":
        raise SystemExit("Windows console smoke requires Windows")
    with tempfile.TemporaryDirectory(prefix="mjobs-console-verified-") as temp:
        root = Path(temp)
        synthetic = root / "stub.py"
        synthetic.write_text(
            "import sys,time\n"
            "prompt=sys.stdin.read()\n"
            "print('SYNTHETIC_CODEX_CONSOLE_MARKER_5847',flush=True)\n"
            "print('PROMPT_LENGTH='+str(len(prompt)),flush=True)\n"
            "time.sleep(2)\n",
            encoding="utf-8",
        )
        log = root / "worker.log"
        manifest = root / "intent.json"
        manifest.write_text(json.dumps({
            "lane": "codex",
            "argv": [REAL_PYTHON, str(synthetic), "-"],
            "workspace": str(root),
            "prompt": "Synthetic multiline\nsafe fixture\n",
            "mode": "terminal",
            "hold_seconds": 4,
            "log": str(log),
        }), encoding="utf-8")
        host = subprocess.Popen(
            [REAL_PYTHON, str(HERE / "terminal_host.py"),
             "--manifest", str(manifest)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        try:
            actual = _await_console("SYNTHETIC_CODEX_CONSOLE_MARKER_5847", host.pid, root)
            assert "MJobs supervised agent" in actual, "Host header is invisible"
            assert "PROMPT_LENGTH=33" in actual, "Synthetic stdin was truncated"
            assert host.wait(timeout=15) == 0, "Synthetic host failed"
            assert not manifest.exists(), "Prompt manifest remains on disk"
            assert "SYNTHETIC_CODEX_CONSOLE_MARKER_5847" in log.read_text(encoding="utf-8")
            print("VISIBLE_SUPERVISOR_CONSOLE_VERIFIED", flush=True)
        finally:
            if host.poll() is None:
                host.terminate()
                host.wait(timeout=10)

        viewer_log = root / "viewer.log"
        viewer_log.write_text(
            "USER_APPROVED_READONLY_LOG_WINDOW_MARKER_9199\n",
            encoding="utf-8",
        )
        viewer = subprocess.Popen(
            [REAL_PYTHON, str(HERE / "terminal_viewer.py"),
             "--lane", "codex", "--log", str(viewer_log)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        try:
            text = _await_console(
                "USER_APPROVED_READONLY_LOG_WINDOW_MARKER_9199", viewer.pid, root
            )
            assert "Read-only transcript viewer" in text
            print("VISIBLE_READONLY_VIEWER_VERIFIED", flush=True)
        finally:
            if viewer.poll() is None:
                viewer.terminate()
                viewer.wait(timeout=10)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--probe":
        raise SystemExit(_probe(int(sys.argv[2]), Path(sys.argv[3])))
    smoke()
