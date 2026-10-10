"""Visible Windows console output binding for web-server-owned child processes.

CREATE_NEW_CONSOLE allocates a window, but Windows may preserve redirected
standard handles from a headless web server. Opening CONOUT$ in the child
is essential: stdout=None on subprocess.Popen does NOT guarantee console text.
"""
from __future__ import annotations

import os
import sys
import typing


def bind_visible_console(title: str) -> typing.TextIO:
    """Attach stdout/stderr to the NEW console's screen buffer, or fail closed.

    Must run IN the child after CREATE_NEW_CONSOLE. Does not attach to, redirect,
    or interfere with any other process' console or STDIN.
    """
    if os.name != "nt":
        raise RuntimeError("Visible terminal mode requires Windows")
    import ctypes

    kernel = ctypes.windll.kernel32
    hwnd = kernel.GetConsoleWindow()
    if not hwnd:
        raise OSError("No Windows console was allocated for this visible process")
    # Explicit fresh CONOUT$ HANDLE, not the inherited handles from Uvicorn.
    out = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
    try:
        kernel.SetConsoleOutputCP(65001)
        kernel.SetConsoleTitleW(title[:180])
        sys.stdout = out
        sys.stderr = out
        print("MJobs console connected. Live CLI output will appear here.", flush=True)
        return out
    except BaseException:
        out.close()
        raise


def set_console_title(title: str) -> None:
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(title[:180])
