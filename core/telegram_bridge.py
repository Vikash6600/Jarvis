"""
Telegram remote — talk to Jarvis from anywhere.

Setup (⚙ → PLUGIN SETTINGS → Telegram): paste a bot token from @BotFather and
press CONNECT; Jarvis shows a 6-digit pairing code. Send that code to your bot
once — from then on the bot only listens to YOUR chat.

From the phone:
    /missions               list missions and routines
    /status [id]            progress of a mission
    /approve [id]           approve a plan (/revise id changes…  to change it)
    /answer id text         answer a mission's question
    /pause /resume /cancel id
    /do <goal>              start a mission ("build…", "research…" also work)
    anything else           a normal chat reply from Jarvis

Jarvis pushes important mission updates and plans that are ready for approval.
Long polling with plain `requests` — no extra dependency, no open port.
"""
from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path

import requests

API = "https://api.telegram.org"
CFG = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
NS = "telegram"
_TASK_WORDS = ("build ", "create ", "make ", "research ", "plan ", "design ", "write ", "develop ")


def _cfg() -> dict:
    try:
        return dict((json.loads(CFG.read_text(encoding="utf-8")).get("plugin_config") or {}).get(NS) or {})
    except Exception:
        return {}


def _save(values: dict) -> None:
    from memory.config_manager import save_plugin_config
    save_plugin_config(NS, values)


def connect(token: str, api: str = API) -> tuple[bool, str]:
    """Verify a bot token and create a pairing code (used by the settings button)."""
    token = (token or "").strip()
    if not token:
        return False, "Paste the bot token from @BotFather first."
    try:
        r = requests.get(f"{api}/bot{token}/getMe", timeout=15).json()
    except Exception as e:
        return False, f"Could not reach Telegram: {e}"
    if not r.get("ok"):
        return False, f"Telegram rejected the token: {r.get('description', 'unknown error')}"
    code = f"{random.randint(0, 999999):06d}"
    _save({"token": token, "pair_code": code, "bot": r["result"].get("username", "")})
    return True, f"Bot @{r['result'].get('username')} is live. Send {code} to it in Telegram to pair."


class TelegramBridge:
    def __init__(self, missions=None, log=print, api: str = API):
        self.mc = missions
        self._log = log
        self.api = api
        self._offset = 0
        self._stop = threading.Event()
        self._thread = None

    # ── outbound ─────────────────────────────────────────────────────────────
    def _call(self, method: str, **params):
        tok = _cfg().get("token")
        if not tok:
            return None
        try:
            return requests.post(f"{self.api}/bot{tok}/{method}", json=params, timeout=40).json()
        except Exception as e:
            print(f"[Telegram] {method}: {e}")
            return None

    def send(self, text: str) -> bool:
        chat = _cfg().get("chat_id")
        if not chat or not text:
            return False
        for i in range(0, len(text), 3900):
            self._call("sendMessage", chat_id=chat, text=text[i:i + 3900], disable_web_page_preview=True)
        return True

    def push(self, text: str, important: bool = False) -> None:
        if important:
            threading.Thread(target=self.send, args=(f"🤖 {text}",), daemon=True).start()

    def send_plan(self, m) -> None:
        body = (m.plan or "")[:7000]
        self.send(f"📋 PLAN READY — #{m.id} {m.title}\n\n{body}\n\n"
                  f"Reply /approve {m.id}  or  /revise {m.id} <changes>")

    # ── inbound ──────────────────────────────────────────────────────────────
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._poll, daemon=True, name="telegram")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _poll(self):
        while not self._stop.is_set():
            cfg = _cfg()
            if not cfg.get("token"):
                self._stop.wait(5)
                continue
            r = self._call("getUpdates", offset=self._offset, timeout=25)
            if not r or not r.get("ok"):
                self._stop.wait(5)
                continue
            for up in r.get("result", []):
                self._offset = up["update_id"] + 1
                try:
                    self._handle(up.get("message") or {}, cfg)
                except Exception as e:
                    print(f"[Telegram] handle: {e}")

    def _handle(self, msg: dict, cfg: dict):
        text = (msg.get("text") or "").strip()
        chat = str((msg.get("chat") or {}).get("id", ""))
        if not text or not chat:
            return
        if not cfg.get("chat_id"):
            if cfg.get("pair_code") and text == cfg["pair_code"]:
                _save({"chat_id": chat, "pair_code": ""})
                self._log("SYS: Telegram paired — you can now message Jarvis from your phone.")
                self.send("✅ Paired. I'm listening, sir. Try /missions or just talk to me.")
            return
        if chat != str(cfg["chat_id"]):
            return                                   # only the paired user
        self._log(f"You (Telegram): {text}")
        self.send(self.reply(text))

    def reply(self, text: str) -> str:
        mc = self.mc
        low = text.lower()
        parts = text.split(maxsplit=2)
        cmd = parts[0].lower() if parts and parts[0].startswith("/") else ""
        arg1 = parts[1] if len(parts) > 1 else ""
        rest = parts[2] if len(parts) > 2 else ""
        if cmd in ("/start", "/help"):
            return __doc__.split("From the phone:")[1].split("Jarvis pushes")[0].strip()
        if mc is None:
            return "Missions are not running yet."
        st = mc.store

        def pick(default_status=None):
            m = st.get(arg1) if arg1 else (st.latest(default_status) if default_status else st.latest())
            return m
        if cmd == "/missions":
            ms = [m for m in st.all() if m.status != "cancelled"][-12:]
            return "\n".join(m.short() for m in ms) or "No missions yet."
        if cmd == "/status":
            m = pick()
            if not m:
                return "No such mission."
            d, n = m.progress
            return f"{m.short()}\nsteps {d}/{n}\n" + "\n".join("• " + e["msg"] for e in m.log[-6:])
        if cmd == "/approve":
            m = pick(("awaiting_plan_approval",))
            return mc.approve(m, rest) if m else "No plan is waiting for approval."
        if cmd == "/revise":
            m = st.get(arg1)
            return mc.revise(m, rest) if m and rest else "Use: /revise <id> <changes>"
        if cmd == "/answer":
            m = st.get(arg1)
            return mc.answer(m, rest) if m and rest else "Use: /answer <id> <text>"
        if cmd in ("/pause", "/resume", "/cancel"):
            m = pick()
            return getattr(mc, cmd[1:])(m) if m else "No such mission."
        if cmd == "/do" or low.startswith(_TASK_WORDS):
            goal = text[3:].strip() if cmd == "/do" else text
            kind = "website" if "website" in low or "landing page" in low else \
                   "research" if low.startswith("research") else "code" if " app" in low or "script" in low else "general"
            m = mc.create(goal, kind=kind)
            return f"🛠 Mission #{m.id} started — researching and planning. I'll send the plan for approval."
        # conversation
        from core import gemini
        summary = "\n".join(m.short() for m in st.all()[-6:]) or "none"
        prompt = (f"You are J.A.R.V.I.S., the user's personal AI, replying on Telegram. Be concise and helpful. "
                  f"Current missions:\n{summary}\n\nUser: {text}")
        return gemini.text(prompt, tier="chat", timeout_ms=45_000,
                           default="I couldn't reach a model just now, sir.") or "…"
