"""
Workspace tools for the mission agent (core/agent.py).

Every file path is confined to the mission's workspace folder. Commands go
through core/safety.run_guarded, so risky ones wait for CONFIRM on the HUD and
destructive ones are refused. All functions are synchronous and run on the
mission's worker thread; each returns a string the model reads.
"""
from __future__ import annotations

import functools
import http.server
import json
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

MAX_READ = 60_000
MAX_OUT = 8_000


# ── declarations (OpenAI function-calling format) ────────────────────────────
def _fn(name, desc, props=None, req=()):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props or {}, "required": list(req)}}}


S = {"type": "string"}
WORKSPACE_TOOLS = [
    _fn("list_dir", "List files in a folder of the mission workspace.", {"path": S}),
    _fn("read_file", "Read a text file from the mission workspace.", {"path": S}, ["path"]),
    _fn("write_file", "Create or overwrite a file in the mission workspace (folders are created).",
        {"path": S, "content": S}, ["path", "content"]),
    _fn("edit_file", "Replace an exact snippet in a workspace file. `find` must match exactly once.",
        {"path": S, "find": S, "replace": S}, ["path", "find", "replace"]),
    _fn("run_command", "Run a shell command (PowerShell on Windows) in the workspace. Risky commands need "
        "the user's approval; destructive ones are refused.",
        {"command": S, "timeout_seconds": {"type": "integer"}}, ["command"]),
    _fn("fetch_url", "Download a web page and return its readable text (for research).", {"url": S}, ["url"]),
    _fn("preview_site", "Serve the workspace as a website and take screenshots at desktop and phone widths. "
        "With review=true a vision model critiques the design against the plan.",
        {"page": S, "review": {"type": "boolean"}, "focus": S}),
    _fn("delegate_coding", "Hand a coding job to Claude Code, which reads, writes and edits the files in this "
        "workspace itself and reports what it changed. Use it for real implementation work (a feature, a "
        "refactor, a bug fix, a whole page) — give it the full context, the files involved and the exact "
        "outcome you want. It cannot ask you questions, so be specific.",
        {"instruction": S, "files": S}, ["instruction"]),
    _fn("look_at_screen", "Capture the user's screen and answer a question about it.", {"question": S}, ["question"]),
    _fn("open_in_browser", "Open a workspace file or a URL in the user's browser.", {"target": S}, ["target"]),
]
CONTROL_TOOLS = [
    _fn("report_progress", "Tell the user briefly what you just finished or found (shown on the HUD; "
        "important updates are spoken).", {"message": S, "important": {"type": "boolean"}}, ["message"]),
    _fn("ask_user", "Pause and ask the user a question you cannot resolve yourself. Use sparingly.",
        {"question": S}, ["question"]),
    _fn("propose_flow", "DISCOVERY stage: submit your understanding of how the product will work — core idea, "
        "user journey step by step, screens/pages, features, data it stores, key mechanics, 2-3 technology "
        "stack options with a recommendation, and your open questions. The user reviews and discusses it "
        "before the detailed plan is written.",
        {"flow_markdown": S, "questions": {"type": "array", "items": S}}, ["flow_markdown"]),
    _fn("submit_plan", "Planning phase only: submit the complete plan (markdown) and the ordered build "
        "steps. The mission then waits for the user's approval.",
        {"plan_markdown": S, "steps": {"type": "array", "items": S}}, ["plan_markdown", "steps"]),
    _fn("complete_step", "Mark a build step done (0-based index) with a short note.",
        {"index": {"type": "integer"}, "note": S}, ["index"]),
    _fn("finish", "The mission (or this routine run) is complete. Give the final summary for the user.",
        {"summary": S}, ["summary"]),
]


class Workspace:
    def __init__(self, root: str, log=print):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._log = log
        self._server = None
        self._port = None
        self._read: set[str] = set()      # files read this run (restricted mode: read before edit)
        self._listed = False

    def _key(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix().lower()

    def _restricted_guard(self, p: Path, editing_existing: bool) -> str:
        from core import access
        if not access.is_restricted():
            return ""
        if not self._listed:
            return ("[RESTRICTED_MODE] First call list_dir('.') and read the relevant files to understand the "
                    "project structure, then make the change.")
        if editing_existing and self._key(p) not in self._read:
            return f"[RESTRICTED_MODE] Read {self._key(p)} with read_file before changing it."
        return ""

    def _p(self, rel: str) -> Path:
        p = (self.root / (rel or ".")).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(f"'{rel}' is outside the mission workspace")
        return p

    # ── files ────────────────────────────────────────────────────────────────
    def list_dir(self, path: str = ".") -> str:
        p = self._p(path)
        if not p.exists():
            return f"(no such folder: {path})"
        if p == self.root:
            self._listed = True
        rows = []
        for f in sorted(p.rglob("*")):
            if any(part.startswith(".") for part in f.relative_to(self.root).parts):
                continue
            if f.is_file():
                rows.append(f"{f.relative_to(self.root).as_posix()}  ({f.stat().st_size} B)")
            if len(rows) >= 300:
                rows.append("…")
                break
        return "\n".join(rows) or "(empty)"

    def read_file(self, path: str) -> str:
        p = self._p(path)
        if not p.is_file():
            return f"(no such file: {path})"
        self._read.add(self._key(p))
        t = p.read_text(encoding="utf-8", errors="replace")
        return t if len(t) <= MAX_READ else t[:MAX_READ] + f"\n… [truncated, {len(t)} chars]"

    def write_file(self, path: str, content: str) -> str:
        p = self._p(path)
        from core import access
        if access.is_restricted() and p.exists():
            return ("[RESTRICTED_MODE] Don't rewrite existing files — change them with edit_file "
                    "(exact snippet → replacement) so the change stays small and reviewable.")
        g = self._restricted_guard(p, editing_existing=False)
        if g:
            return g
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"wrote {p.relative_to(self.root).as_posix()} ({len(content)} chars)"

    def edit_file(self, path: str, find: str, replace: str) -> str:
        p = self._p(path)
        if not p.is_file():
            return f"(no such file: {path})"
        g = self._restricted_guard(p, editing_existing=True)
        if g:
            return g
        t = p.read_text(encoding="utf-8", errors="replace")
        n = t.count(find)
        if n != 1:
            return f"edit refused: the snippet occurs {n} times (must be exactly once)"
        p.write_text(t.replace(find, replace, 1), encoding="utf-8")
        return f"edited {path}"

    # ── commands ─────────────────────────────────────────────────────────────
    def run_command(self, command: str, timeout_seconds: int = 120) -> str:
        """Guarded run (the agent uses its own waiting variant of this)."""
        from core.safety import run_guarded
        return run_guarded("mission_cmd", f"Mission command: {command[:60]}", command,
                           lambda: self._exec(command, timeout_seconds))

    def _exec(self, command: str, timeout_seconds: int = 120) -> str:
        """Unscreened execution — only ever called behind core/safety.run_guarded."""
        shell = (["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
                 if sys.platform == "win32" else ["bash", "-lc", command])
        try:
            r = subprocess.run(shell, cwd=str(self.root), capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=max(5, min(int(timeout_seconds or 120), 900)))
            out = (r.stdout or "") + (("\nSTDERR:\n" + r.stderr) if r.stderr.strip() else "")
            out = out.strip() or "(no output)"
            return f"exit {r.returncode}\n" + (out[-MAX_OUT:] if len(out) > MAX_OUT else out)
        except subprocess.TimeoutExpired:
            return "timed out (it may be a long-running server — that is fine for previews)"

    # ── web ──────────────────────────────────────────────────────────────────
    def fetch_url(self, url: str) -> str:
        import requests
        from bs4 import BeautifulSoup
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (Jarvis research)"})
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "noscript", "svg"]):
            t.decompose()
        title = soup.title.get_text(strip=True) if soup.title else ""
        text = " ".join(soup.get_text(" ").split())
        return f"{title}\n{text[:MAX_OUT]}"

    def _serve(self) -> int:
        if self._server:
            return self._port
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        class _Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a, **k):
                pass
        handler = functools.partial(_Quiet, directory=str(self.root))
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self._port = port
        return port

    def preview_site(self, page: str = "index.html", review: bool = False, focus: str = "",
                     plan: str = "") -> str:
        from playwright.sync_api import sync_playwright
        port = self._serve()
        url = f"http://127.0.0.1:{port}/{(page or 'index.html').lstrip('/')}"
        out_dir = self._p("review")
        out_dir.mkdir(exist_ok=True)
        stamp = time.strftime("%H%M%S")
        shots, errors = [], []
        with sync_playwright() as pw:
            br = pw.chromium.launch()
            for name, w, h in (("desktop", 1440, 900), ("phone", 390, 844)):
                pg = br.new_page(viewport={"width": w, "height": h})
                pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.goto(url, wait_until="networkidle", timeout=45_000)
                pg.wait_for_timeout(1500)                    # let intro animations settle
                f = out_dir / f"{stamp}-{name}.png"
                pg.screenshot(path=str(f), full_page=True)
                shots.append(f)
                pg.close()
            br.close()
        res = [f"preview: {url}", "screenshots: " + ", ".join(s.relative_to(self.root).as_posix() for s in shots)]
        if errors:
            res.append("browser console errors:\n" + "\n".join(dict.fromkeys(errors))[:2000])
        if review:
            res.append("design review:\n" + self._vision_review(shots, focus, plan))
        return "\n".join(res)

    def _vision_review(self, shots, focus: str, plan: str) -> str:
        from google.genai import types
        from core import gemini
        parts = [("You are a senior web designer reviewing a build. Compare these screenshots "
                  "(desktop, then phone) with the plan. List concrete problems in priority order: "
                  "broken or empty sections, layout/overflow bugs, contrast/readability, spacing, "
                  "missing features from the plan, weak visual polish or animation opportunities. "
                  "Be specific (section + fix). Then give a 1-10 polish score.\n"
                  + (f"Focus: {focus}\n" if focus else "") + ("PLAN:\n" + plan[:4000] if plan else ""))]
        for s in shots:
            data = s.read_bytes()
            if len(data) > 4_500_000:           # keep full-page captures within model limits
                from PIL import Image
                import io
                im = Image.open(s)
                im.thumbnail((1440, 6000))
                buf = io.BytesIO(); im.save(buf, format="PNG"); data = buf.getvalue()
            parts.append(types.Part.from_bytes(data=data, mime_type="image/png"))
        return gemini.text(parts, tier=gemini.SMART, timeout_ms=90_000,
                           default="(no vision model answered — check the VISION job in AI MODELS)")

    def delegate_coding(self, instruction: str, files: str = "") -> str:
        from core import access, claude_cli
        if not claude_cli.available():
            return ("Claude Code is not installed here — write the code yourself with write_file/edit_file.")
        restricted = access.is_restricted()
        task = instruction + (f"\n\nFiles involved: {files}" if files else "")
        if restricted:
            task += ("\n\nRULES: this is an existing project in RESTRICTED mode. Read the relevant files first, "
                     "follow the existing structure and style, make the smallest clear change, do not run "
                     "commands, do not commit, do not delete files.")
        try:
            out = claude_cli.run(task, cwd=str(self.root), tools="edit" if restricted else "full")
        except Exception as e:
            msg = str(e)
            if "logged in" in msg.lower():
                return ("Claude Code is installed but not signed in — the user must run `claude` once in a "
                        "terminal and sign in. Write the code yourself with write_file/edit_file for now.")
            return f"Claude Code failed: {msg[:300]}. Write the code yourself instead."
        return out[:MAX_OUT]

    def look_at_screen(self, question: str) -> str:
        import io
        import mss
        from PIL import Image
        from google.genai import types
        from core import gemini
        with mss.MSS() if hasattr(mss, "MSS") else mss.mss() as sct:
            g = sct.grab(sct.monitors[1])
            im = Image.frombytes("RGB", g.size, g.rgb)
        im.thumbnail((1600, 1600))
        buf = io.BytesIO(); im.save(buf, format="JPEG", quality=80)
        return gemini.text([question, types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")],
                           tier=gemini.SMART, timeout_ms=60_000, default="(no vision model answered)")

    def open_in_browser(self, target: str) -> str:
        if target.startswith(("http://", "https://")):
            webbrowser.open(target)
            return f"opened {target}"
        p = self._p(target)
        if p.suffix.lower() in (".html", ".htm"):
            port = self._serve()
            url = f"http://127.0.0.1:{port}/{p.relative_to(self.root).as_posix()}"
            webbrowser.open(url)
            return f"opened {url}"
        webbrowser.open(p.as_uri())
        return f"opened {p}"

    def close(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass
            self._server = None

    def call(self, name: str, args: dict, plan: str = "") -> str:
        fn = getattr(self, name)
        if name == "preview_site":
            args = dict(args, plan=plan)
        return fn(**args)


WORKSPACE_TOOL_NAMES = {t["function"]["name"] for t in WORKSPACE_TOOLS}
CONTROL_TOOL_NAMES = {t["function"]["name"] for t in CONTROL_TOOLS}


def dumps(obj) -> str:
    return obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
