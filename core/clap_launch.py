"""
Plumbing for "double clap launches Jarvis".

* A PID lock file (config/.jarvis.lock) makes Jarvis single-instance and tells
  the background clap launcher whether Jarvis is already running.
* A wake-request file (config/.wake_request) lets a second launch — or the
  launcher — ask the running copy to wake instead of opening another window.
* set_launcher_enabled() registers / unregisters clap_launcher.py to start at
  login (Windows: HKCU Run key; macOS/Linux: not yet — reported in the message)
  and starts / stops it right away.
"""
from __future__ import annotations

import atexit
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CFG = BASE / "config"
LOCK = CFG / ".jarvis.lock"
WAKE_REQ = CFG / ".wake_request"
LAUNCHER_PID = CFG / ".clap_launcher.pid"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "JARVIS_CLAP"
_NO_WINDOW = {"creationflags": 0x08000000} if platform.system() == "Windows" else {}


def _pid_alive(pid: int, needle: str) -> bool:
    try:
        import psutil
        p = psutil.Process(pid)
        return p.is_running() and any(needle in part for part in p.cmdline())
    except Exception:
        return False


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except Exception:
        return None


def is_jarvis_running() -> bool:
    pid = _read_pid(LOCK)
    return bool(pid) and _pid_alive(pid, "main.py")


def acquire_instance_lock() -> bool:
    """False when another Jarvis is already running."""
    CFG.mkdir(parents=True, exist_ok=True)
    pid = _read_pid(LOCK)
    if pid and pid != os.getpid() and _pid_alive(pid, "main.py"):
        return False
    LOCK.write_text(str(os.getpid()))

    def _release():
        try:
            if _read_pid(LOCK) == os.getpid():
                LOCK.unlink()
        except Exception:
            pass
    atexit.register(_release)
    return True


def request_wake() -> None:
    try:
        WAKE_REQ.write_text(str(time.time()))
    except Exception:
        pass


def watch_wake_requests(callback) -> None:
    """Poll for wake requests from a second launch or the clap launcher."""
    def loop():
        while True:
            time.sleep(0.5)
            if WAKE_REQ.exists():
                try:
                    WAKE_REQ.unlink()
                except Exception:
                    pass
                try:
                    callback()
                except Exception as e:
                    print(f"[Clap] wake request failed: {e}")
    threading.Thread(target=loop, daemon=True, name="wake-requests").start()


def _pythonw() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def launcher_command() -> list[str]:
    return [_pythonw(), str(BASE / "clap_launcher.py")]


def launcher_running() -> bool:
    pid = _read_pid(LAUNCHER_PID)
    return bool(pid) and _pid_alive(pid, "clap_launcher.py")


def _set_run_key(enabled: bool) -> bool:
    if platform.system() != "Windows":
        return False
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            cmd = " ".join(f'"{c}"' for c in launcher_command())
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass
    return True


def set_launcher_enabled(enabled: bool) -> str:
    """Register/unregister the clap launcher and start/stop it now."""
    try:
        ok = _set_run_key(enabled)
    except Exception as e:
        ok = False
        print(f"[Clap] could not update the login entry: {e}")
    if enabled:
        if not launcher_running():
            try:
                subprocess.Popen(launcher_command(), cwd=str(BASE), close_fds=True, **_NO_WINDOW)
            except Exception as e:
                return f"launcher failed to start: {e}"
        return ("Double clap will wake Jarvis, and launch it when closed "
                + ("(starts with Windows)." if ok else "(start-at-login unavailable on this OS)."))
    pid = _read_pid(LAUNCHER_PID)
    if pid and _pid_alive(pid, "clap_launcher.py"):
        try:
            import psutil
            psutil.Process(pid).terminate()
        except Exception:
            pass
    return "Clap launcher off."
