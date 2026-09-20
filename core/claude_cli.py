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


def run(prompt: str, cwd: str = "", tools: str = "read_only", timeout: int = TIMEOUT,
        cli: str = "") -> str:
    """Run Claude Code headlessly and return its final text.

    tools: 'read_only'  answer/write code as text, no file changes (default)
           'edit'       may read/write/edit files in cwd
           'full'       may also run shell commands (only in Full access mode)
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
                raise RuntimeError(str(js.get("result") or js.get("error"))[:300])
            return str(js.get("result") or js.get("text") or out)
    except json.JSONDecodeError:
        pass
    return out


def chat(messages: list, timeout: int = TIMEOUT, cli: str = "") -> dict:
    """OpenAI-shaped reply so core/models.chat can treat this like any provider."""
    text = run(_flatten(messages), tools="read_only", timeout=timeout, cli=cli)
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}
