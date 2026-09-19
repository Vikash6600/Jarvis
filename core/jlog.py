"""
Structured logging.

Everything Jarvis logs lands in logs/jarvis.jsonl as one JSON object per line:

    {"ts": "2026-09-20T01:12:03.418+05:30", "level": "INFO", "module": "route",
     "thread": "MainThread", "message": "smart → groq/llama-3.3-70b-versatile"}

The file rotates at 10 MB and keeps 10 old files (≈100 MB worst case).

`install()` also tees stdout/stderr into the log, so the hundreds of existing
print("[JARVIS] …") / print("[Route] …") lines are captured without touching
them — the bracketed tag becomes the `module`. Under pythonw, where there is no
console, this is the only record of what happened.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "jarvis.jsonl"
_TAG = re.compile(r"^\s*\[([A-Za-z][\w .-]{0,24})\]\s*")
_installed = False
_lock = threading.Lock()


class _JsonFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        rec = {"ts": datetime.fromtimestamp(r.created).astimezone().isoformat(timespec="milliseconds"),
               "level": r.levelname, "module": r.name.removeprefix("jarvis."),
               "thread": r.threadName, "message": r.getMessage()}
        if r.exc_info:
            rec["exc"] = self.formatException(r.exc_info)
        extra = getattr(r, "meta", None)
        if extra:
            rec["meta"] = extra
        return json.dumps(rec, ensure_ascii=False, default=str)


def _root() -> logging.Logger:
    lg = logging.getLogger("jarvis")
    with _lock:
        if not lg.handlers:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            h = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024,
                                                     backupCount=10, encoding="utf-8")
            h.setFormatter(_JsonFormatter())
            lg.addHandler(h)
            lg.setLevel(logging.DEBUG)
            lg.propagate = False
    return lg


def get_logger(module: str) -> logging.Logger:
    _root()
    return logging.getLogger(f"jarvis.{module}")


class _Tee:
    """File-like wrapper: passes writes through and logs each complete line."""

    def __init__(self, stream, level: int):
        self._s = stream
        self._level = level
        self._buf = ""

    def write(self, data):
        if self._s is not None:
            try:
                self._s.write(data)
            except Exception:
                pass
        self._buf += data
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip()
            if line:
                m = _TAG.match(line)
                mod = m.group(1).strip().lower() if m else ("stderr" if self._level >= logging.WARNING else "stdout")
                msg = line[m.end():] if m else line
                lvl = self._level
                if "❌" in msg or msg.startswith(("Error", "ERR")) or "Traceback" in msg:
                    lvl = logging.ERROR
                elif "⚠" in msg:
                    lvl = max(lvl, logging.WARNING)
                try:
                    get_logger(mod).log(lvl, msg)
                except Exception:
                    pass
        return len(data)

    def flush(self):
        if self._s is not None:
            try:
                self._s.flush()
            except Exception:
                pass

    def isatty(self):
        return bool(self._s and getattr(self._s, "isatty", lambda: False)())

    def __getattr__(self, name):
        return getattr(self._s, name)


def install() -> None:
    """Tee stdout/stderr into the JSON log. Idempotent."""
    global _installed
    if _installed:
        return
    _installed = True
    sys.stdout = _Tee(sys.stdout, logging.INFO)
    sys.stderr = _Tee(sys.stderr, logging.WARNING)
    get_logger("app").info("Jarvis starting", extra={"meta": {"argv": sys.argv}})
