"""
Claude Code as a local provider — coding with your Claude subscription, no API key.

If the Claude Code CLI is installed (it ships with the Claude desktop app), Jarvis
can hand coding work to it:

  * as a MODEL — the "claudecode" provider answers ordinary prompts, so the CODE
    job (code_helper, dev_agent, and anything Jarvis routes as code) is written
    by Claude;
  * as a TOOL — missions can call `delegate_coding`, which runs Claude Code
    inside the mission workspace so it reads, writes and edits the files itself
    and reports what it changed.

Nothing here needs an API key: the CLI uses the login you already have. In
Restricted access mode Claude Code is given file tools only (no shell), so it
cannot commit, install or run anything.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

TIMEOUT = 900
USAGE_FILE = Path(__file__).resolve().parent.parent / "config" / "claude_usage.json"


class LimitReached(RuntimeError):
    """Claude's 5-hour or weekly allowance is spent — fall back to another model."""


def _record(js: dict) -> None:
    """Keep a small local tally so Jarvis can show and budget Claude usage."""
    try:
        import datetime
        day = datetime.date.today().isoformat()
        try:
            data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        d = data.setdefault(day, {"calls": 0, "input": 0, "output": 0, "cost": 0.0})
        u = js.get("usage") or {}
        d["calls"] += 1
        d["input"] += int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
        d["output"] += int(u.get("output_tokens") or 0)
        d["cost"] += float(js.get("total_cost_usd") or 0.0)
        for k in list(data)[:-14]:
            data.pop(k, None)                       # keep two weeks
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except Exception:
        pass


def usage_today() -> dict:
    try:
        import datetime
        return json.loads(USAGE_FILE.read_text(encoding="utf-8")).get(datetime.date.today().isoformat(), {})
    except Exception:
        return {}
_SEARCH = [
    Path(os.environ.get("APPDATA", "")) / "Claude" / "claude-code",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Claude" / "claude-code",
    Path.home() / ".local" / "bin",
    Path.home() / "AppData" / "Roaming" / "npm",
]


def find_cli() -> str:
    """Path to the claude executable, or '' when it isn't installed."""
    exe = shutil.which("claude")
    if exe:
        return exe
    for base in _SEARCH:
        try:
            if not base.exists():
                continue
            hits = sorted(base.glob("**/claude.exe")) or sorted(base.glob("claude*"))
            hits = [h for h in hits if h.is_file()]
            if hits:
                return str(sorted(hits, key=lambda p: p.parent.name)[-1])   # newest version folder
        except Exception:
            continue
    return ""


def available() -> bool:
    return bool(find_cli())


def signed_in(timeout: int = 60) -> tuple[bool, str]:
    """(ok, message) — proves the CLI is installed AND logged in."""
    if not find_cli():
        return False, "Claude Code is not installed (it ships with the Claude desktop app)."
    try:
        run("Reply with exactly: OK", timeout=timeout)
        return True, "Claude Code is signed in and ready."
    except Exception as e:
        msg = str(e)
        if "logged in" in msg.lower():
            return False, "Claude Code is installed but not signed in — press SIGN IN and type /login."
        return False, f"Claude Code error: {msg[:160]}"


def open_login_terminal() -> str:
    """Open a terminal running the CLI so the user can type /login once."""
    exe = find_cli()
    if not exe:
        return "Claude Code is not installed."
    try:
        if os.name == "nt":
            # No extra quoting: cmd's `start` mangles pre-quoted paths, and
            # subprocess already quotes arguments that need it.
            subprocess.Popen(["cmd", "/c", "start", "Claude Code sign-in", "cmd", "/k", exe],
                             shell=False, cwd=str(Path.home()))
        else:
            subprocess.Popen(["x-terminal-emulator", "-e", exe])
        return "A terminal is open — type /login and finish the sign-in, then press TEST."
    except Exception as e:
        return f"Could not open a terminal: {e}. Run this yourself: {exe}"


def _flatten(messages: list) -> str:
    out = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, list):
            content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
        if not content:
            continue
        out.append(str(content) if role == "user" else f"[{role}]\n{content}")
    return "\n\n".join(out)


MODELS = ("haiku", "sonnet", "opus")          # cheapest → strongest
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def run(prompt: str, cwd: str = "", tools: str = "read_only", timeout: int = TIMEOUT,
        cli: str = "", model: str = "", effort: str = "") -> str:
    """Run Claude Code headlessly and return its final text.

    tools:  'read_only'  answer/write code as text, no file changes (default)
            'edit'       may read/write/edit files in cwd
            'full'       may also run shell commands (only in Full access mode)
    model:  haiku | sonnet | opus (alias; empty = the CLI default)
    effort: low | medium | high | xhigh | max — how long it may think
    """
    exe = cli or find_cli()
    if not exe:
        raise RuntimeError("Claude Code CLI not found — install the Claude desktop app or `npm i -g "
                           "@anthropic-ai/claude-code`.")
    allowed = {"read_only": "Read,Glob,Grep",
               "edit": "Read,Glob,Grep,Write,Edit,MultiEdit,NotebookEdit",
               "full": "Read,Glob,Grep,Write,Edit,MultiEdit,NotebookEdit,Bash"}.get(tools, "Read,Glob,Grep")
    cmd = [exe, "-p", prompt, "--output-format", "json",
           "--allowed-tools", allowed,
           "--permission-mode", "acceptEdits" if tools != "read_only" else "default"]
    if model:
        cmd += ["--model", model]
        if model in ("opus", "sonnet"):
            cmd += ["--fallback-model", "sonnet" if model == "opus" else "haiku"]
    if effort in EFFORTS:
        cmd += ["--effort", effort]
    r = subprocess.run(cmd, cwd=cwd or None, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout,
                       creationflags=0x08000000 if os.name == "nt" else 0)
    out = (r.stdout or "").strip()
    if not out:
        raise RuntimeError((r.stderr or "Claude Code returned nothing").strip()[:300])
    try:
        js = json.loads(out)
        if isinstance(js, dict):
            if js.get("is_error"):
                err = str(js.get("result") or js.get("error"))[:300]
                if any(k in err.lower() for k in ("usage limit", "rate limit", "quota", "limit reached")):
                    raise LimitReached(err)
                raise RuntimeError(err)
            _record(js)
            return str(js.get("result") or js.get("text") or out)
    except json.JSONDecodeError:
        pass
    return out


def chat(messages: list, timeout: int = TIMEOUT, cli: str = "") -> dict:
    """OpenAI-shaped reply so core/models.chat can treat this like any provider."""
    text = run(_flatten(messages), tools="read_only", timeout=timeout, cli=cli)
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}
