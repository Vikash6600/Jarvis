"""
Unified memory — one place every part of Jarvis writes to and reads from.

Voice, missions and Telegram used to remember separately (facts in
memory/long_term.json, mission logs, the Telegram transcript), so nothing
carried across. This store keeps the shared, durable part in memory/mind.json:

    fact      something true about the user or their setup
    pref      how they like things done
    lesson    what worked / failed on a real mission ("Gemini free tier runs out
              of Pro quota — pin Flash for long builds")
    event     a milestone worth recalling ("built the Bean There site, 6 steps")

`recall(query)` scores entries by word overlap, tag match and recency, so a
mission about a website gets the website lessons. `digest()` is the short block
injected into the voice session's system prompt and Telegram's context; the
mission agent gets `brief_for(goal)` before it plans, so it starts each job
knowing what the last ones taught it.

`distil(mission)` runs after a mission ends: one cheap model call turns its log
into at most three durable lessons.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

STORE = Path(__file__).resolve().parent.parent / "memory" / "mind.json"
KINDS = ("fact", "pref", "lesson", "event")
MAX_ENTRIES = 400
_STOP = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "is", "are", "be", "it",
         "this", "that", "my", "your", "i", "you", "we", "at", "by", "as", "from", "was", "were", "do"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if w not in _STOP}


@dataclass
class Entry:
    id: str
    kind: str
    text: str
    tags: list = field(default_factory=list)
    source: str = ""
    created: float = field(default_factory=time.time)
    used: int = 0

    def score(self, qw: set[str], now: float) -> float:
        ew = _words(self.text) | {t.lower() for t in self.tags}
        overlap = len(qw & ew) / (len(qw) or 1)
        age_days = (now - self.created) / 86400
        recency = 1.0 / (1.0 + age_days / 30)
        weight = {"lesson": 1.25, "pref": 1.15, "fact": 1.0, "event": 0.8}.get(self.kind, 1.0)
        return (overlap * 2 + recency * 0.5 + min(self.used, 5) * 0.05) * weight


class Mind:
    def __init__(self, path: Path = STORE):
        self.path = path
        self._lock = threading.RLock()
        self._e: list[Entry] = []
        try:
            for d in json.loads(path.read_text(encoding="utf-8")).get("entries", []):
                self._e.append(Entry(**{k: v for k, v in d.items() if k in Entry.__dataclass_fields__}))
        except Exception:
            pass

    # ── writing ──────────────────────────────────────────────────────────────
    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"entries": [asdict(e) for e in self._e]}, indent=1, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def remember(self, kind: str, text: str, tags=None, source: str = "") -> Entry | None:
        text = (text or "").strip()
        if len(text) < 8:
            return None
        with self._lock:
            tw = _words(text)
            for e in self._e:                       # skip near-duplicates
                ew = _words(e.text)
                if tw and len(tw & ew) / len(tw | ew or {1}) > 0.7:
                    e.created = time.time()
                    self._save()
                    return e
            en = Entry(id=f"{int(time.time() * 1000) % 10**9:x}", kind=kind if kind in KINDS else "fact",
                       text=text[:600], tags=[str(t)[:24].lower() for t in (tags or [])][:6], source=source)
            self._e.append(en)
            if len(self._e) > MAX_ENTRIES:          # drop the least useful, oldest first
                self._e.sort(key=lambda x: (x.used, x.created))
                self._e = self._e[len(self._e) - MAX_ENTRIES:]
            self._save()
            return en

    def forget(self, entry_id: str) -> bool:
        with self._lock:
            n = len(self._e)
            self._e = [e for e in self._e if e.id != entry_id]
            self._save()
            return len(self._e) < n

    # ── reading ──────────────────────────────────────────────────────────────
    def recall(self, query: str, limit: int = 6, kinds=None) -> list[Entry]:
        qw = _words(query)
        now = time.time()
        with self._lock:
            pool = [e for e in self._e if not kinds or e.kind in kinds]
            ranked = sorted(pool, key=lambda e: -e.score(qw, now))[:limit]
            for e in ranked:
                e.used += 1
            if ranked:
                self._save()
            return [e for e in ranked if e.score(qw, now) > 0.25]

    def all(self) -> list[Entry]:
        with self._lock:
            return list(self._e)

    def digest(self, limit: int = 10) -> str:
        """Short block for a system prompt: the most useful durable memory."""
        now = time.time()
        with self._lock:
            ranked = sorted(self._e, key=lambda e: -((1.0 / (1 + (now - e.created) / 86400 / 30))
                                                     + min(e.used, 5) * 0.1
                                                     + {"lesson": 0.6, "pref": 0.5}.get(e.kind, 0.0)))[:limit]
        if not ranked:
            return ""
        return "What you have learned so far:\n" + "\n".join(f"- ({e.kind}) {e.text}" for e in ranked)

    def brief_for(self, goal: str, limit: int = 8) -> str:
        hits = self.recall(goal, limit=limit)
        if not hits:
            return ""
        return ("Relevant memory from earlier work (apply it, don't repeat past mistakes):\n"
                + "\n".join(f"- ({e.kind}) {e.text}" for e in hits))


_mind: Mind | None = None


def mind() -> Mind:
    global _mind
    if _mind is None:
        _mind = Mind()
    return _mind


def remember(kind: str, text: str, tags=None, source: str = ""):
    return mind().remember(kind, text, tags=tags, source=source)


def recall(query: str, limit: int = 6):
    return mind().recall(query, limit=limit)


def brief_for(goal: str) -> str:
    return mind().brief_for(goal)


def digest(limit: int = 10) -> str:
    return mind().digest(limit)


def distil(mission, log=print) -> int:
    """Turn a finished mission into at most three durable lessons."""
    from core import gemini
    lines = [e["msg"] for e in mission.log][-40:]
    prompt = (
        "You keep the long-term memory of an AI assistant. From this finished job, extract at most 3 SHORT, "
        "durable lessons worth remembering for future jobs: what the user prefers, what worked, what failed and "
        "why, and any environment/tooling facts (quotas, versions, paths, stack choices). Skip anything "
        "one-off or obvious. Reply as JSON: {\"lessons\": [{\"kind\": \"lesson|pref|fact\", \"text\": \"…\", "
        "\"tags\": [\"…\"]}]}\n\n"
        f"JOB: {mission.title}\nKIND: {mission.kind}\nGOAL: {mission.goal}\n"
        f"OUTCOME: {mission.status} — {mission.result[:600]}\nLOG:\n" + "\n".join(lines)[:6000])
    data = gemini.as_json(prompt, tier="fast", timeout_ms=45_000, default=None)
    items = (data or {}).get("lessons") if isinstance(data, dict) else None
    n = 0
    for it in (items or [])[:3]:
        if isinstance(it, dict) and it.get("text"):
            tags = list(it.get("tags") or []) + [mission.kind]
            if remember(str(it.get("kind") or "lesson"), str(it["text"]), tags=tags, source=f"mission #{mission.id}"):
                n += 1
    if n:
        log(f"[Mind] learned {n} lesson(s) from #{mission.id}")
    return n
