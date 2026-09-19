"""
Background double-clap launcher.

Runs quietly (pythonw, no window) when ⚙ → DOUBLE CLAP is set to
"WAKE + LAUNCH", and at Windows login via the HKCU Run key JARVIS_CLAP.

  * Jarvis NOT running → listens to the microphone; a double clap starts
    `main.py --wake`, which comes up already listening.
  * Jarvis running     → closes the microphone and just checks every 2 s; the
    in-app detector handles waking, so there is never a double trigger.

It exits on its own if the clap mode is changed away from "launch".
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from core import clap_launch            # noqa: E402
from core.clap import ClapDetector      # noqa: E402

CFG_FILE = BASE / "config" / "api_keys.json"


def _cfg() -> dict:
    try:
        return json.loads(CFG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _log(msg: str) -> None:
    try:
        with open(BASE / "config" / "clap_launcher.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def _launch_jarvis() -> None:
    _log("double clap → launching Jarvis")
    subprocess.Popen([clap_launch._pythonw(), str(BASE / "main.py"), "--wake"],
                     cwd=str(BASE), close_fds=True, **clap_launch._NO_WINDOW)


def main() -> None:
    if clap_launch.launcher_running():
        return
    clap_launch.LAUNCHER_PID.parent.mkdir(parents=True, exist_ok=True)
    clap_launch.LAUNCHER_PID.write_text(str(os.getpid()))
    _log("launcher started")
    import sounddevice as sd

    fired = threading.Event()
    while True:
        cfg = _cfg()
        if str(cfg.get("clap_mode", "off")) != "launch":
            _log("clap mode is no longer 'launch' — exiting")
            break
        if clap_launch.is_jarvis_running():
            time.sleep(2)
            continue
        fired.clear()
        det = ClapDetector(on_detect=fired.set, sensitivity=str(cfg.get("clap_sensitivity", "medium")),
                           logger=_log)
        try:
            with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=1024,
                                callback=lambda indata, *_: det.feed(indata[:, 0])):
                det.start()
                while not fired.is_set():
                    time.sleep(0.25)
                    if clap_launch.is_jarvis_running():
                        break
        except Exception as e:
            _log(f"microphone unavailable: {e}")
            time.sleep(10)
            continue
        finally:
            det.stop()
        if fired.is_set() and not clap_launch.is_jarvis_running():
            _launch_jarvis()
            for _ in range(60):                  # give it up to 30 s to take the lock
                if clap_launch.is_jarvis_running():
                    break
                time.sleep(0.5)
    try:
        clap_launch.LAUNCHER_PID.unlink()
    except Exception:
        pass


if __name__ == "__main__":
    main()
