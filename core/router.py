"""
Model router — which AI does this particular job.

core/models.py answers "which model is the CODE model". This picks the *job*
for a piece of work in the first place, then the healthiest model for it:

    classify(text)   → role: code | vision | search | smart | fast | chat | agent
    pick(text)       → (role, provider, model, why)  ready to call
    for_phase(kind, phase) → the role a mission phase should think with

How it decides, cheapest first:
  1. obvious signals in the text (code fences, "fix the bug", "what's on my
     screen", "latest news", "plan/strategy/compare", one-line factual asks);
  2. only when it is genuinely ambiguous and a cheap model is configured, one
     short classification call, cached by text hash;
  3. health: models cooling off (429, or retired 404s) are skipped, so a job
     never stalls on an exhausted provider — it lands on the next best one.

Everything stays overridable: a job pinned in ⚙ → AI MODELS always wins.
"""
from __future__ import annotations

import hashlib
import re
import threading

from core import models

_cache: dict[str, str] = {}
_lock = threading.Lock()

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("code", re.compile(r"```|\bdef \w+\(|\bclass \w+|\bimport \b|\bfunction\b|\bnpm\b|\bpip install\b|"
                        r"\b(refactor|debug|stack ?trace|traceback|compile|syntax error|unit test|"
                        r"pull request|merge conflict)\b|\b(fix|write|review|optimi[sz]e|explain)\b[^.?!]{0,30}"
                        r"\b(code|script|function|bug|error|app|website|component|api)\b", re.I)),
    ("vision", re.compile(r"\b(on (my|the) screen|what do you see|this (image|picture|screenshot)|"
                          r"look at (my|the)|read (this|the) (image|screenshot)|webcam|camera)\b", re.I)),
    ("search", re.compile(r"\b(latest|news|today'?s|current (price|weather|score)|who won|search (for|the web)|"
                          r"look up|according to)\b", re.I)),
    ("smart", re.compile(r"\b(plan|strategy|roadmap|architecture|compare|trade-?offs?|analy[sz]e|summar(y|ise|ize)|"
                         r"pros and cons|research|report|design (a|the) (system|schema)|brainstorm)\b", re.I)),
    ("fast", re.compile(r"^\s*(what|who|when|where|how much|how many|convert|translate|spell|define)\b[^.?!]{0,80}\??$",
                        re.I)),
]


def classify(text: str, allow_model: bool = True) -> str:
    """The job this text needs. Cheap signals first, a model only if needed."""
    t = (text or "").strip()
    if not t:
        return "chat"
    for role, pat in _PATTERNS:
        if pat.search(t):
            return role
    if len(t) < 120 and t.count(" ") < 18:
        return "fast"
    if not allow_model or not models.candidates("fast"):
        return "smart"
    key = hashlib.sha1(t[:400].encode("utf-8", "replace")).hexdigest()[:16]
    with _lock:
        if key in _cache:
            return _cache[key]
    from core import gemini
    ask = ("Classify this request for an AI assistant into exactly one word from: code, vision, search, "
           "smart, fast, chat. code = programming; vision = about an image/screen; search = needs current "
           "web facts; smart = reasoning, planning, long analysis; fast = short factual/simple; chat = "
           "conversation. Reply with the single word only.\n\nREQUEST: " + t[:800])
    role = (gemini.text(ask, tier="fast", timeout_ms=15_000, default="smart") or "smart").strip().lower()
    role = role.split()[0].strip(".,'\"") if role else "smart"
    if role not in ("code", "vision", "search", "smart", "fast", "chat"):
        role = "smart"
    with _lock:
        _cache[key] = role
        if len(_cache) > 500:
            _cache.clear()
    return role


def pick(text: str, role: str = "", exclude_cli: bool = False):
    """(role, provider, model, why) — the healthiest model for this job."""
    role = role or classify(text)
    tried_fallback = False
    for r in (role, "smart", "chat"):
        for prov, model in models.candidates(r):
            if exclude_cli and prov.get("kind") == "cli":
                continue
            if models.cooling(prov, model):
                continue
            why = (f"{role} job" if r == role else f"{role} job (no {role} model free, using {r})")
            if tried_fallback:
                why += " after a cooldown"
            return r, prov, model, why
        tried_fallback = True
    return role, None, "", f"no model available for a {role} job"


def for_phase(kind: str, phase: str) -> str:
    """Which job a mission phase should think with."""
    if phase == "routine":
        return "fast"
    if phase == "planning":
        return "agent"                     # research + planning: the strongest reasoner
    if kind in ("website", "code"):
        return "code"                      # building: a code-capable, tool-calling model
    return "agent"


def explain() -> str:
    """One line per job, for the HUD / a spoken answer."""
    rows = []
    for role in ("agent", "code", "smart", "fast", "vision", "search", "chat", "stt"):
        rows.append(f"{role}: {models.describe(role)}")
    return " · ".join(rows)
