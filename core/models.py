"""
Multi-provider model registry and job routing.

Jarvis can hold API keys for many providers at once — Gemini plus anything that
speaks the OpenAI chat-completions protocol (OpenAI, Anthropic Claude, Groq,
OpenRouter, DeepSeek, Mistral, xAI Grok, Together, local Ollama / LM Studio, or
any custom endpoint) — and picks a model per *job* (role):

    chat    the brain when voice runs as a speech pipeline (no Gemini Live)
    fast    quick parsing / lookups           (gemini.FAST call sites)
    smart   summaries, documents, planning    (gemini.SMART call sites)
    code    code helper, dev agent
    vision  anything with an image in it
    search  web-search answers                (gemini.SEARCH call sites)
    stt     speech-to-text for the pipeline voice engine

Each role is "auto" (chosen from model tags) or pinned to "<provider_id>/<model>".
`candidates(role)` returns a ranked fallback list, so a model that fails or is
out of quota falls through to the next one.

Everything lives in config/api_keys.json under "providers" and "model_roles".
A legacy "gemini_api_key" is migrated into a Gemini provider automatically and
kept in sync, so code that still reads it keeps working.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import requests


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_FILE = _base_dir() / "config" / "api_keys.json"

ROLES = ("agent", "chat", "fast", "smart", "code", "vision", "search", "stt")
ROLE_LABELS = {
    "voice": ("VOICE ENGINE", "Live conversation"),
    "agent": ("AGENT", "Missions: research, plan, build"),
    "chat": ("CHAT", "Brain for pipeline voice & typed chat"),
    "fast": ("FAST", "Quick lookups, parsing"),
    "smart": ("SMART", "Summaries, documents, planning"),
    "code": ("CODE", "Code helper, dev agent"),
    "vision": ("VISION", "Screen and images"),
    "search": ("SEARCH", "Web search answers"),
    "stt": ("SPEECH-TO-TEXT", "Pipeline voice input"),
}

# Curated "top models" per provider. Shown before a key is tested; TEST replaces
# them with the provider's real /models list.
PRESETS: dict[str, dict] = {
    "gemini": {"label": "Google Gemini", "kind": "gemini", "base_url": "",
               "hint": "AIza…", "url": "https://aistudio.google.com/apikey",
               "models": ["gemini-pro-latest", "gemini-flash-latest", "gemini-flash-lite-latest"]},
    "openai": {"label": "OpenAI", "kind": "openai_compat", "base_url": "https://api.openai.com/v1",
               "hint": "sk-…", "url": "https://platform.openai.com/api-keys",
               "models": ["gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini", "o4-mini", "whisper-1"]},
    "anthropic": {"label": "Anthropic Claude", "kind": "openai_compat",
                  "base_url": "https://api.anthropic.com/v1",
                  "hint": "sk-ant-…", "url": "https://console.anthropic.com/settings/keys",
                  "models": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"]},
    "groq": {"label": "Groq", "kind": "openai_compat", "base_url": "https://api.groq.com/openai/v1",
             "hint": "gsk_…", "url": "https://console.groq.com/keys",
             "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "whisper-large-v3-turbo"]},
    "openrouter": {"label": "OpenRouter", "kind": "openai_compat", "base_url": "https://openrouter.ai/api/v1",
                   "hint": "sk-or-…", "url": "https://openrouter.ai/keys",
                   "models": ["openrouter/auto"]},
    "deepseek": {"label": "DeepSeek", "kind": "openai_compat", "base_url": "https://api.deepseek.com/v1",
                 "hint": "sk-…", "url": "https://platform.deepseek.com/api_keys",
                 "models": ["deepseek-chat", "deepseek-reasoner"]},
    "mistral": {"label": "Mistral", "kind": "openai_compat", "base_url": "https://api.mistral.ai/v1",
                "hint": "key…", "url": "https://console.mistral.ai/api-keys",
                "models": ["mistral-large-latest", "mistral-small-latest", "codestral-latest", "pixtral-large-latest"]},
    "xai": {"label": "xAI Grok", "kind": "openai_compat", "base_url": "https://api.x.ai/v1",
            "hint": "xai-…", "url": "https://console.x.ai",
            "models": ["grok-4", "grok-3-mini"]},
    "together": {"label": "Together AI", "kind": "openai_compat", "base_url": "https://api.together.xyz/v1",
                 "hint": "key…", "url": "https://api.together.ai/settings/api-keys",
                 "models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo"]},
    "ollama": {"label": "Ollama (local)", "kind": "openai_compat", "base_url": "http://localhost:11434/v1",
               "hint": "no key needed", "url": "https://ollama.com", "local": True,
               "models": ["llama3.2"]},
    "lmstudio": {"label": "LM Studio (local)", "kind": "openai_compat", "base_url": "http://localhost:1234/v1",
                 "hint": "no key needed", "url": "https://lmstudio.ai", "local": True,
                 "models": []},
    "custom": {"label": "Custom (OpenAI-compatible)", "kind": "openai_compat", "base_url": "",
               "hint": "key (optional)", "url": "", "models": []},
}
PRESET_ORDER = ("gemini", "openai", "anthropic", "groq", "openrouter", "deepseek",
                "mistral", "xai", "together", "ollama", "lmstudio", "custom")

# Sensible preference when a role is on "auto" and several providers qualify.
_PROVIDER_PREF = {"agent": ("anthropic", "openai", "gemini", "xai", "deepseek", "mistral", "openrouter", "groq"),
                  "smart": ("anthropic", "openai", "gemini", "xai", "deepseek", "mistral", "groq", "openrouter"),
                  "code": ("anthropic", "deepseek", "openai", "mistral", "xai", "gemini", "groq"),
                  "vision": ("gemini", "openai", "anthropic", "mistral", "xai"),
                  "fast": ("groq", "gemini", "openai", "anthropic", "mistral", "deepseek"),
                  "chat": ("groq", "openai", "anthropic", "gemini", "deepseek", "xai", "mistral"),
                  "search": ("gemini", "openai", "anthropic", "groq"),
                  "stt": ("groq", "openai")}


# ── tags ─────────────────────────────────────────────────────────────────────
def guess_tags(provider_id: str, model: str) -> list[str]:
    m = model.lower()
    tags: list[str] = []
    if "whisper" in m or "transcrib" in m:
        return ["stt"] if provider_id != "gemini" else ["other"]
    if any(k in m for k in ("embed", "tts", "dall-e", "image", "moderation", "guard", "computer-use",
                            "robotics", "aqa", "learnlm", "veo", "imagen", "lyria", "search-preview",
                            "realtime", "audio-preview")):
        return ["other"]
    if any(k in m for k in ("coder", "codestral", "code", "deepseek", "claude", "gpt-4.1", "grok-4")):
        tags.append("code")
    if any(k in m for k in ("4o", "4.1", "vision", "llava", "pixtral", "gemini", "claude", "grok-4", "gpt-5", "llama-4")):
        tags.append("vision")
    if any(k in m for k in ("mini", "flash", "lite", "8b", "instant", "haiku", "small", "nano", "turbo")):
        tags.append("fast")
    if any(k in m for k in ("o3", "o4", "r1", "reasoner", "opus", "pro", "large", "70b", "sonnet", "grok-4", "gpt-5", "4.1")):
        tags.append("reasoning")
    if "live" in m or "native-audio" in m:
        tags = ["live"]
    return tags or ["chat"]


def _version(model: str) -> float:
    """Newer first: '-latest' aliases win, then the highest version number
    (providers retire old models, so the newest is the safest default)."""
    m = model.lower()
    if "latest" in m:
        return 99.0
    import re as _re
    v = _re.search(r"(\d+(?:\.\d+)?)", m.split("/")[-1])
    try:
        return float(v.group(1)) if v else 0.0
    except ValueError:
        return 0.0


# ── config ───────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_cache: tuple[float, dict] | None = None


def _read() -> dict:
    global _cache
    try:
        mt = CONFIG_FILE.stat().st_mtime
    except OSError:
        return {}
    with _lock:
        if _cache and _cache[0] == mt:
            return _cache[1]
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        _cache = (mt, data)
        return data


def providers() -> list[dict]:
    """Configured providers, with the legacy Gemini key folded in."""
    data = _read()
    provs = [dict(p) for p in (data.get("providers") or []) if isinstance(p, dict)]
    legacy = str(data.get("gemini_api_key") or "").strip()
    if legacy and not any(p.get("kind") == "gemini" for p in provs):
        provs.insert(0, {"id": "gemini", "preset": "gemini", "label": "Google Gemini", "kind": "gemini",
                         "base_url": "", "api_key": legacy,
                         "models": list(PRESETS["gemini"]["models"])})
    out = []
    for p in provs:
        if p.get("enabled", True) is False:
            continue
        if p.get("kind") != "gemini" and not p.get("base_url"):
            continue
        if not p.get("api_key") and not PRESETS.get(p.get("preset", ""), {}).get("local") \
                and p.get("preset") != "custom":
            continue
        out.append(p)
    return out


def roles() -> dict:
    r = dict(_read().get("model_roles") or {})
    return {k: r.get(k, "auto") for k in ("voice",) + ROLES}


def save(provs: list[dict], role_map: dict) -> None:
    """Merge providers + roles into the config file, keeping every other key."""
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data["providers"] = provs
    data["model_roles"] = role_map
    gem = next((p for p in provs if p.get("kind") == "gemini" and p.get("api_key")), None)
    if gem:
        data["gemini_api_key"] = gem["api_key"]
    elif "gemini_api_key" in data and not any(p.get("kind") == "gemini" for p in provs):
        data.pop("gemini_api_key", None)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
    global _cache
    with _lock:
        _cache = None
    try:  # one-shot Gemini calls cache the key; make them see the new one
        from core import gemini as _g
        _g.api_key(refresh=True)
    except Exception:
        pass


def has_gemini() -> bool:
    return any(p.get("kind") == "gemini" and p.get("api_key") for p in providers())


def has_any() -> bool:
    return bool(providers())


def voice_engine() -> str:
    """'gemini_live' when a Gemini key exists and the user has not chosen the
    pipeline; otherwise 'pipeline'."""
    v = roles().get("voice", "auto")
    if v == "pipeline":
        return "pipeline"
    return "gemini_live" if has_gemini() else "pipeline"


def all_models() -> list[tuple[dict, str, list[str]]]:
    out = []
    for p in providers():
        tagmap = p.get("tags") or {}
        for m in p.get("models") or []:
            out.append((p, m, tagmap.get(m) or guess_tags(p.get("preset", p["id"]), m)))
    return out


def find(ref: str) -> tuple[dict, str] | None:
    if not ref or "/" not in ref:
        return None
    pid, model = ref.split("/", 1)
    for p in providers():
        if p.get("id") == pid:
            return p, model
    return None


def candidates(role: str) -> list[tuple[dict, str]]:
    """Ranked (provider, model) list for a job: pinned first, then by tag."""
    out: list[tuple[dict, str]] = []
    pinned = find(roles().get(role, "auto"))
    if pinned:
        out.append(pinned)
    want = {"agent": ("reasoning", "code", "chat"),
            "chat": ("chat", "reasoning", "fast", "code", "vision"),
            "fast": ("fast",), "smart": ("reasoning",), "code": ("code",),
            "vision": ("vision",), "search": ("reasoning", "fast", "chat"),
            "stt": ("stt",)}.get(role, ("chat",))
    pref = _PROVIDER_PREF.get(role, ())
    scored = []
    for p, m, tags in all_models():
        if "other" in tags or "live" in tags:
            continue
        if role == "stt" and ("stt" not in tags or p.get("kind") == "gemini"):
            continue
        if role != "stt" and "stt" in tags:
            continue
        hit = sum(1 for t in want if t in tags)
        if hit == 0 and role in ("vision", "stt"):
            continue
        pr = p.get("preset", p.get("id"))
        rank = pref.index(pr) if pr in pref else len(pref)
        scored.append((-hit, rank, -_version(m), p, m))
    scored.sort(key=lambda s: (s[0], s[1], s[2]))
    for _h, _r, _v, p, m in scored:
        if (p, m) not in out:
            out.append((p, m))
    return out


def describe(role: str) -> str:
    c = candidates(role)
    if not c:
        return "none"
    p, m = c[0]
    return f"{p.get('preset', p['id'])} / {m}"


# ── network helpers ──────────────────────────────────────────────────────────
GEMINI_OPENAI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


def _base(p: dict) -> str:
    if p.get("kind") == "gemini":
        return GEMINI_OPENAI_URL
    return p.get("base_url", "").rstrip("/")


def _headers(p: dict) -> dict:
    h = {"Content-Type": "application/json"}
    key = p.get("api_key") or ""
    if key:
        h["Authorization"] = f"Bearer {key}"
    if p.get("preset") == "anthropic":
        h["x-api-key"] = key
        h["anthropic-version"] = "2023-06-01"
    if p.get("preset") == "openrouter":
        h["X-Title"] = "Jarvis Mark LIV"
    return h


def fetch_models(p: dict, timeout: float = 12.0) -> list[str]:
    """Ask the provider for its model list. Raises with a readable message."""
    if p.get("kind") == "gemini":
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"key": p.get("api_key", ""), "pageSize": 200}, timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(_err(r))
        names = [m["name"].split("/", 1)[-1] for m in r.json().get("models", [])
                 if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        keep = [n for n in names if n.startswith("gemini") and "tts" not in n and "embedding" not in n]
        return sorted(set(keep), key=lambda n: (("live" in n), n))
    url = p.get("base_url", "").rstrip("/") + "/models"
    r = requests.get(url, headers=_headers(p), timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(_err(r))
    js = r.json()
    items = js.get("data", js if isinstance(js, list) else [])
    names = [str(i.get("id")) for i in items if isinstance(i, dict) and i.get("id")]
    return sorted(set(names))


def _err(r) -> str:
    try:
        js = r.json()
        msg = js.get("error", js)
        if isinstance(msg, dict):
            msg = msg.get("message") or json.dumps(msg)[:160]
        return f"HTTP {r.status_code}: {str(msg)[:160]}"
    except Exception:
        return f"HTTP {r.status_code}: {r.text[:160]}"


_cool: dict[str, float] = {}


def cooling(p: dict, model: str) -> bool:
    until = _cool.get(f"{p.get('id')}/{model}", 0)
    return until > time.monotonic()


def cool(p: dict, model: str, seconds: int = 300) -> None:
    _cool[f"{p.get('id')}/{model}"] = time.monotonic() + seconds


def chat(p: dict, model: str, messages: list, timeout: float = 60.0, **extra) -> dict:
    """One OpenAI-style /chat/completions call. Returns the raw JSON."""
    body = {"model": model, "messages": messages}
    body.update({k: v for k, v in extra.items() if v is not None})
    url = _base(p) + "/chat/completions"
    r = requests.post(url, headers=_headers(p), json=body, timeout=timeout)
    if r.status_code == 429:
        cool(p, model)
    elif r.status_code == 404 or "no longer available" in r.text[:400].lower():
        cool(p, model, 24 * 3600)
    if r.status_code != 200:
        raise RuntimeError(_err(r))
    return r.json()


def transcribe(p: dict, model: str, wav_bytes: bytes, timeout: float = 60.0) -> str:
    url = p.get("base_url", "").rstrip("/") + "/audio/transcriptions"
    h = _headers(p)
    h.pop("Content-Type", None)
    r = requests.post(url, headers=h, files={"file": ("speech.wav", wav_bytes, "audio/wav")},
                      data={"model": model}, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(_err(r))
    return (r.json().get("text") or "").strip()
