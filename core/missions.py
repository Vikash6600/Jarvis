"""
Mission store — the tasks Jarvis works on in the background.

A mission is a goal ("build a website for a coffee shop") that the agent
(core/agent.py) carries through phases:

    queued → researching → planning → awaiting_plan_approval → executing
           → reviewing → done
    side states: waiting_approval (a risky step is on the HUD), paused,
                 failed, cancelled

Routines are missions with a `schedule` ("every 30 min", "daily 08:00",
"weekdays 18:30"); they skip the plan-approval stop and run again at next_run.

Everything is persisted in memory/missions.json after every change (atomic
write, one lock), so a restart resumes exactly where the work stopped.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
STORE = BASE / "memory" / "missions.json"
WORK_ROOT = Path.home() / "Documents" / "Jarvis Missions"

ACTIVE = ("queued", "researching", "planning", "executing", "reviewing")
WAITING = ("awaiting_plan_approval", "waiting_approval", "waiting_user")
FINAL = ("done", "failed", "cancelled")
KINDS = ("general", "website", "code", "research", "routine")


def _now() -> float:
    return time.time()


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:40].rstrip("-") or "mission")


@dataclass
class Mission:
    id: str
    title: str
    goal: str
    kind: str = "general"
    status: str = "queued"
    phase_note: str = ""
    plan: str = ""
    plan_feedback: list = field(default_factory=list)
    stage: str = ""                                     # "flow" while the discovery flow is under review
    flow: str = ""                                      # discovery: how the product works, stack options
    flow_approved: bool = False
    project: bool = False                               # workspace is an existing project folder
    steps: list = field(default_factory=list)          # [{"title", "status"}]
    log: list = field(default_factory=list)            # [{"t", "msg"}]
    artifacts: list = field(default_factory=list)
    workspace: str = ""
    schedule: str = ""
    next_run: float = 0.0
    runs: int = 0
    iterations: int = 0
    history: list = field(default_factory=list)        # agent conversation checkpoint
    pending_question: str = ""
    answer: str = ""
    result: str = ""
    created: float = field(default_factory=_now)
    updated: float = field(default_factory=_now)

    @property
    def progress(self) -> tuple[int, int]:
        done = sum(1 for s in self.steps if s.get("status") == "done")
        return done, len(self.steps)

    def short(self) -> str:
        d, n = self.progress
        prog = f" {d}/{n}" if n else ""
        return f"#{self.id} {self.title} — {self.status}{prog}"


class MissionStore:
    def __init__(self, path: Path = STORE):
        self.path = path
        self._lock = threading.RLock()
        self._m: dict[str, Mission] = {}
        self._listeners: list = []
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        fields = set(Mission.__dataclass_fields__)
        for mid, d in (raw.get("missions") or {}).items():
            try:
                self._m[mid] = Mission(**{k: v for k, v in d.items() if k in fields})
            except Exception:
                pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = {"missions": {k: asdict(v) for k, v in self._m.items()}}
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def on_change(self, fn) -> None:
        self._listeners.append(fn)

    def _notify(self, m: Mission) -> None:
        for fn in list(self._listeners):
            try:
                fn(m)
            except Exception:
                pass

    # ── api ──────────────────────────────────────────────────────────────────
    def create(self, goal: str, kind: str = "general", title: str = "", schedule: str = "",
               workspace: str = "") -> Mission:
        with self._lock:
            mid = uuid.uuid4().hex[:4]
            while mid in self._m:
                mid = uuid.uuid4().hex[:4]
            title = (title or goal).strip().rstrip(".")
            title = title[:60] + ("…" if len(title) > 60 else "")
            if schedule:
                kind = "routine"
            existing = Path(workspace).expanduser() if workspace else None
            if existing is not None and existing.is_dir():
                ws = existing.resolve()
            else:
                ws = WORK_ROOT / f"{_slug(title)}-{mid}"
                ws.mkdir(parents=True, exist_ok=True)
            m = Mission(id=mid, title=title, goal=goal.strip(), kind=kind if kind in KINDS else "general",
                        workspace=str(ws), schedule=schedule.strip(), project=existing is not None and existing.is_dir())
            if m.schedule:
                m.next_run = next_run(m.schedule) or _now()
            self._m[mid] = m
            self._log(m, f"Mission created: {goal.strip()}")
            self._save()
        self._notify(m)
        return m

    def get(self, mid: str) -> Mission | None:
        with self._lock:
            mid = (mid or "").strip().lstrip("#").lower()
            if mid in self._m:
                return self._m[mid]
            # allow addressing by a word from the title
            hits = [m for m in self._m.values() if mid and mid in m.title.lower()]
            return hits[-1] if len(hits) >= 1 else None

    def all(self) -> list[Mission]:
        with self._lock:
            return sorted(self._m.values(), key=lambda m: m.created)

    def latest(self, statuses=None) -> Mission | None:
        ms = [m for m in self.all() if statuses is None or m.status in statuses]
        return ms[-1] if ms else None

    def update(self, m: Mission, **changes) -> Mission:
        with self._lock:
            for k, v in changes.items():
                setattr(m, k, v)
            m.updated = _now()
            self._save()
        self._notify(m)
        return m

    def _log(self, m: Mission, msg: str) -> None:
        m.log.append({"t": _now(), "msg": msg})
        m.log = m.log[-200:]

    def log(self, m: Mission, msg: str) -> None:
        with self._lock:
            self._log(m, msg)
            m.updated = _now()
            self._save()
        self._notify(m)

    def delete(self, m: Mission) -> None:
        with self._lock:
            self._m.pop(m.id, None)
            self._save()


# ── schedules ────────────────────────────────────────────────────────────────
_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def next_run(schedule: str, after: float | None = None) -> float | None:
    """Parse a simple schedule and return the next run time (epoch seconds).

    Supported: "every 30 min", "every 2 hours", "every 45 seconds",
    "hourly", "daily 08:00", "every day at 8am", "weekdays 18:30",
    "weekends 10:00", "mon,wed,fri 07:15", "monday 9:00"."""
    s = (schedule or "").strip().lower()
    if not s:
        return None
    base = datetime.fromtimestamp(after if after is not None else _now())
    m = re.search(r"every\s+(\d+)\s*(s|sec|second|m|min|minute|h|hr|hour|d|day)s?\b", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)[0]
        secs = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * max(1, n)
        return (base + timedelta(seconds=max(30, secs))).timestamp()
    if re.search(r"\bhourly\b|every\s+hour\b", s):
        return (base + timedelta(hours=1)).timestamp()
    tm = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if not tm:
        return None
    hh, mm = int(tm.group(1)), int(tm.group(2) or 0)
    if tm.group(3) == "pm" and hh < 12:
        hh += 12
    if tm.group(3) == "am" and hh == 12:
        hh = 0
    if hh > 23 or mm > 59:
        return None
    if "weekday" in s:
        days = {0, 1, 2, 3, 4}
    elif "weekend" in s:
        days = {5, 6}
    else:
        days = {d for k, d in _DAYS.items() if re.search(rf"\b{k}", s)} or set(range(7))
    for add in range(0, 8):
        cand = (base + timedelta(days=add)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand > base and cand.weekday() in days:
            return cand.timestamp()
    return None
