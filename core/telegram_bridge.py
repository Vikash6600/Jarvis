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
HISTORY = Path(__file__).resolve().parent.parent / "memory" / "telegram_chat.json"
MAX_HISTORY = 40
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
        try:
            self._hist = json.loads(HISTORY.read_text(encoding="utf-8"))[-MAX_HISTORY:]
        except Exception:
            self._hist = []

    def _remember(self, role: str, text: str) -> None:
        self._hist.append({"role": role, "text": text[:2000], "t": time.time()})
        self._hist = self._hist[-MAX_HISTORY:]
        try:
            HISTORY.parent.mkdir(parents=True, exist_ok=True)
            HISTORY.write_text(json.dumps(self._hist, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _transcript(self, n: int = 16) -> str:
        return "\n".join(("User: " if h["role"] == "user" else "Jarvis: ") + h["text"][:600]
                         for h in self._hist[-n:])

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
            self._remember("jarvis", text)
            threading.Thread(target=self.send, args=(f"🤖 {text}",), daemon=True).start()

    def send_plan(self, m) -> None:
        if getattr(m, "stage", "") == "flow":
            body = (m.flow or "")[:7000]
            msg = (f"🧭 FLOW TO DISCUSS — #{m.id} {m.title}\n\n{body}\n\n"
                   f"Answer the questions or tell me what to change (just reply normally). "
                   f"When it's right, say \"looks good\" or /approve {m.id} and I'll write the detailed plan.")
        else:
            body = (m.plan or "")[:7000]
            msg = (f"📋 PLAN READY — #{m.id} {m.title}\n\n{body}\n\n"
                   f"Reply with changes, or \"approve\" / /approve {m.id} to start building.")
        self._remember("jarvis", msg[:1500])
        self.send(msg)

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
        self._remember("user", text)
        out = self.reply(text)
        self._remember("jarvis", out)
        self.send(out)

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
        if cmd == "/do":
            return self._start(text[3:].strip(), None)
        return self._route(text)

    # ── understanding free-form messages ─────────────────────────────────────
    def _start(self, goal: str, d: dict | None) -> str:
        d = d or {}
        low = goal.lower()
        kind = d.get("kind") or ("website" if "website" in low or "site" in low or "landing page" in low else
                                 "research" if low.startswith("research") else
                                 "code" if " app" in low or "script" in low else "general")
        m = self.mc.create(goal, kind=kind if kind in ("website", "code", "research", "general") else "general",
                           title=str(d.get("title") or "")[:60])
        first = ("I'll research it and first send you how I understand the whole flow and the tech stack "
                 "options so we can discuss them" if kind in ("website", "code", "research")
                 else "I'll research and send you the plan for approval")
        return f"🛠 Mission #{m.id} — {m.title}. {first}."

    def _route(self, text: str) -> str:
        from core import gemini
        st = self.mc.store
        live = [m for m in st.all() if m.status not in ("done", "cancelled", "failed")][-8:]
        board = "\n".join(
            f"#{m.id} [{m.status}{'/flow' if m.stage == 'flow' else ''}] {m.title}"
            + (f" — QUESTION: {m.pending_question}" if m.pending_question else "") for m in live) or "(none)"
        prompt = (
            "You route messages the user sends to their AI assistant JARVIS on Telegram. Read the recent "
            "conversation and the missions, then decide what this new message is. Reply with JSON only:\n"
            '{"intent": "chat|start_mission|answer_question|discuss_review|approve_review|status", '
            '"mission_id": "id or empty", "goal": "for start_mission: a SELF-CONTAINED goal that restates '
            'everything the user wants, resolving words like it/that/this from the conversation (what to '
            'build, references like games or apps, features, style, constraints)", '
            '"kind": "website|code|research|general", "title": "short title"}\n'
            "- start_mission: they want something built, researched, planned or done that takes real work.\n"
            "- answer_question: they answer a mission's QUESTION.\n"
            "- discuss_review: they comment on, answer questions in, or ask for changes to a flow/plan that is "
            "under review (status awaiting_plan_approval).\n"
            "- approve_review: they agree/approve a flow or plan under review ('looks good', 'go ahead').\n"
            "- status: they ask how a mission is going. Otherwise chat.\n\n"
            f"MISSIONS:\n{board}\n\nRECENT CONVERSATION:\n{self._transcript()}\n\nNEW MESSAGE: {text}")
        d = gemini.as_json(prompt, tier="smart", timeout_ms=45_000, default=None)
        if not isinstance(d, dict):
            d = {}
        intent = str(d.get("intent") or "").lower()
        m = st.get(str(d.get("mission_id") or "")) if d.get("mission_id") else None
        if not intent:                                      # model unavailable → simple fallbacks
            waiting = st.latest(("waiting_user",))
            review = st.latest(("awaiting_plan_approval",))
            low = text.lower()
            if waiting:
                intent, m = "answer_question", waiting
            elif review and low.strip(" .!") in ("ok", "okay", "yes", "looks good", "approve", "go ahead", "approved"):
                intent, m = "approve_review", review
            elif review:
                intent, m = "discuss_review", review
            elif low.startswith(_TASK_WORDS):
                intent = "start_mission"
            else:
                intent = "chat"
        if intent == "start_mission":
            return self._start(str(d.get("goal") or text), d)
        if intent == "answer_question":
            m = m or st.latest(("waiting_user",))
            if m:
                return "✅ " + self.mc.answer(m, text)
        if intent in ("discuss_review", "approve_review"):
            m = m or st.latest(("awaiting_plan_approval",))
            if m:
                return "✅ " + (self.mc.approve(m) if intent == "approve_review" else self.mc.revise(m, text))
        if intent == "status" and (m or live):
            m = m or live[-1]
            d_, n_ = m.progress
            return f"{m.short()} · steps {d_}/{n_}\n" + "\n".join("• " + e["msg"] for e in m.log[-5:])
        summary = "\n".join(x.short() for x in st.all()[-6:]) or "none"
        chat = (f"You are J.A.R.V.I.S., the user's personal AI, replying on Telegram — concise, warm, helpful. "
                f"You can start background missions for real work; if they describe something to build, "
                f"offer to start it. Missions:\n{summary}\n\nConversation so far:\n{self._transcript()}\n\n"
                f"Reply to the user's last message.")
        return gemini.text(chat, tier="chat", timeout_ms=45_000,
                           default="I couldn't reach a model just now, sir.") or "…"
