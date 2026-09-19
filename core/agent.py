"""
Mission agent + supervisor — Jarvis working on its own, in the background.

MissionControl (one per app) owns a supervisor thread that picks up missions
from core/missions.py and runs each on its own worker thread (max 2 at once):

  planning   research with tools, then submit_plan → awaiting_plan_approval
             (nothing is built until the user approves or revises the plan)
  executing  build step by step with tools, complete_step, self-review, finish
  routine    scheduled job: run quickly, finish, reschedule

The loop is think → act → observe with an OpenAI-style tool-calling model from
the AGENT job (core/models.py). It checkpoints the conversation into the
mission after every step, so a restart resumes mid-task. Tool sources:
Jarvis's own actions/plugins (via the registries), the workspace tools
(core/agent_tools.py) and the control tools (report_progress, ask_user,
submit_plan, complete_step, finish).

The app talks to it through hooks:
  announce(text, important)   HUD log; important ones are spoken when idle
  changed(mission)            HUD / dashboard refresh
  notify(title, message)      desktop toast
  show_plan(mission)          Mission Brief overlay with APPROVE / REVISE
"""
from __future__ import annotations

import json
import platform
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from core import models
from core.agent_tools import (CONTROL_TOOL_NAMES, CONTROL_TOOLS, WORKSPACE_TOOL_NAMES,
                              WORKSPACE_TOOLS, Workspace)
from core.missions import ACTIVE, Mission, MissionStore, next_run

PLAYBOOKS = Path(__file__).resolve().parent / "playbooks"
MAX_CONCURRENT = 2
STEP_BUDGET = 60             # tool calls per run before pausing for the user
MAX_TOOL_OUT = 7000
_SKIP_REGISTRY = {"dev_agent"}   # the agent does this itself, in the workspace


def _playbook(kind: str) -> str:
    try:
        return (PLAYBOOKS / f"{kind}.md").read_text(encoding="utf-8")
    except Exception:
        return (PLAYBOOKS / "general.md").read_text(encoding="utf-8")


class _Stop(Exception):
    pass


class MissionControl:
    def __init__(self, store: MissionStore, run_tool=None, tool_decls=None, hooks: dict | None = None,
                 user_name: str = "sir"):
        self.store = store
        self._run_tool = run_tool              # (name, args) -> str   Jarvis actions/plugins
        self._decls = tool_decls or []         # Gemini-style declarations for those tools
        self.hooks = hooks or {}
        self.user = user_name
        self._running: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── hooks ────────────────────────────────────────────────────────────────
    def _hook(self, name, *a):
        fn = self.hooks.get(name)
        if fn:
            try:
                fn(*a)
            except Exception as e:
                print(f"[Missions] hook {name} failed: {e}")

    def _say(self, m: Mission, text: str, important: bool = False):
        self.store.log(m, text)
        self._hook("announce", f"[#{m.id}] {text}", important)

    # ── supervisor ───────────────────────────────────────────────────────────
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self.store.on_change(lambda m: self._hook("changed", m))
        self._thread = threading.Thread(target=self._supervise, daemon=True, name="mission-supervisor")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _supervise(self):
        while not self._stop.is_set():
            try:
                now = time.time()
                for m in self.store.all():
                    if m.status == "scheduled" and m.next_run and m.next_run <= now:
                        self.store.update(m, status="executing", history=[], phase_note="routine run")
                    if m.status in ACTIVE:
                        self._maybe_run(m)
            except Exception as e:
                print(f"[Missions] supervisor: {e}")
            self._stop.wait(2.0)

    def _maybe_run(self, m: Mission):
        with self._lock:
            self._running = {k: t for k, t in self._running.items() if t.is_alive()}
            if m.id in self._running or len(self._running) >= MAX_CONCURRENT:
                return
            t = threading.Thread(target=self._work, args=(m,), daemon=True, name=f"mission-{m.id}")
            self._running[m.id] = t
            t.start()

    def running_ids(self) -> set[str]:
        with self._lock:
            return {k for k, t in self._running.items() if t.is_alive()}

    # ── public actions (called from voice tools / UI / Telegram) ─────────────
    def create(self, goal: str, kind: str = "general", schedule: str = "", title: str = "") -> Mission:
        m = self.store.create(goal, kind=kind, title=title, schedule=schedule)
        if m.schedule:
            nr = next_run(m.schedule)
            first = time.time() if "every" in m.schedule.lower() or "hourly" in m.schedule.lower() else nr
            self.store.update(m, status="scheduled", next_run=first or time.time())
            self._say(m, f"Routine scheduled: {m.title} ({m.schedule}).")
        else:
            self.store.update(m, status="planning", phase_note="researching")
            self._say(m, f"Mission started: {m.title}. Researching and planning first.")
        return m

    def approve(self, m: Mission, changes: str = "") -> str:
        if m.status != "awaiting_plan_approval":
            return f"Mission #{m.id} is {m.status}, not waiting for plan approval."
        if changes.strip():
            return self.revise(m, changes)
        self.store.update(m, status="executing", history=[], phase_note="building")
        self._say(m, "Plan approved — building now.", True)
        return f"Plan for #{m.id} approved; building in the background."

    def revise(self, m: Mission, feedback: str) -> str:
        m.plan_feedback.append(feedback.strip())
        self.store.update(m, status="planning", history=[], phase_note="revising the plan")
        self._say(m, f"Revising the plan: {feedback.strip()}")
        return f"Revising the plan for #{m.id}."

    def answer(self, m: Mission, text: str) -> str:
        if m.status != "waiting_user":
            return f"Mission #{m.id} is not waiting for an answer."
        resume = m.phase_note if m.phase_note in ("planning", "executing") else "executing"
        hist = list(m.history)
        hist.append({"role": "user", "content": f"The user answered your question: {text}"})
        self.store.update(m, status=resume, answer=text, pending_question="", history=hist)
        return f"Thanks — #{m.id} continues."

    def pause(self, m: Mission) -> str:
        if m.status in ("done", "cancelled", "failed"):
            return f"#{m.id} is already {m.status}."
        self.store.update(m, status="paused", phase_note=m.status)
        return f"#{m.id} paused."

    def resume(self, m: Mission) -> str:
        if m.status not in ("paused", "failed"):
            return f"#{m.id} is {m.status}."
        back = m.phase_note if m.phase_note in ("planning", "executing", "scheduled") else \
            ("executing" if m.plan else "planning")
        self.store.update(m, status=back)
        return f"#{m.id} resumed."

    def cancel(self, m: Mission) -> str:
        self.store.update(m, status="cancelled")
        return f"#{m.id} cancelled."

    # ── the worker ───────────────────────────────────────────────────────────
    def _work(self, m: Mission):
        ws = Workspace(m.workspace, log=lambda s: None)
        try:
            phase = "routine" if m.kind == "routine" else ("planning" if m.status == "planning" else "executing")
            self._loop(m, ws, phase)
        except _Stop:
            pass
        except Exception as e:
            traceback.print_exc()
            self.store.update(m, status="failed", phase_note=m.status, result=str(e)[:300])
            self._say(m, f"Mission failed: {e}", True)
            self._hook("notify", "Mission failed", f"{m.title}: {e}")
        finally:
            ws.close()

    def _system(self, m: Mission, phase: str) -> str:
        now = datetime.now().strftime("%A %d %B %Y, %H:%M")
        parts = [
            f"You are J.A.R.V.I.S., Tony-Stark-grade personal AI, working AUTONOMOUSLY in the background on a "
            f"mission for your user (address them as '{self.user}'). The user is not watching every step: "
            f"work independently, make sensible decisions, and only use ask_user for things only they can "
            f"answer. Keep report_progress messages short and meaningful (milestones, not every action).",
            f"Now: {now}. OS: {platform.system()} {platform.release()}. Mission workspace (all file tools are "
            f"relative to it): {m.workspace}",
            f"MISSION #{m.id}: {m.goal}",
            _playbook("routine" if phase == "routine" else m.kind),
        ]
        if phase == "planning":
            parts.append("PHASE: PLANNING. Research first (web_search, fetch_url), save notes, then call "
                         "submit_plan with the complete markdown plan and the ordered build steps. Do NOT "
                         "build anything yet — the user approves the plan first.")
            if m.plan:
                parts.append("PREVIOUS PLAN:\n" + m.plan[:12000])
            if m.plan_feedback:
                parts.append("THE USER ASKED FOR THESE CHANGES (apply all of them):\n- "
                             + "\n- ".join(m.plan_feedback))
        elif phase == "executing":
            steps = "\n".join(f"{i}. [{s.get('status', 'todo')}] {s.get('title')}" for i, s in enumerate(m.steps))
            parts.append("PHASE: EXECUTING the approved plan. Continue from the first unfinished step. Call "
                         "complete_step after each step; when everything is done and reviewed, call finish.")
            parts.append("APPROVED PLAN:\n" + (m.plan or "(no plan — work directly from the goal)")[:12000])
            parts.append("STEPS:\n" + (steps or "(none)"))
        else:
            prev = [e["msg"] for e in m.log if e["msg"].startswith("Result:")][-3:]
            parts.append("PHASE: ROUTINE RUN #%d. Previous results:\n%s" % (m.runs + 1, "\n".join(prev) or "(first run)"))
        return "\n\n".join(parts)

    def _tools(self, phase: str) -> list:
        from core.pipeline_session import openai_tools
        ctrl = [t for t in CONTROL_TOOLS
                if not (t["function"]["name"] == "submit_plan" and phase != "planning")
                and not (t["function"]["name"] == "complete_step" and phase != "executing")
                and not (t["function"]["name"] == "ask_user" and phase == "routine")]
        reg = [t for t in openai_tools(self._decls)
               if t["function"]["name"] not in WORKSPACE_TOOL_NAMES | CONTROL_TOOL_NAMES | _SKIP_REGISTRY]
        return WORKSPACE_TOOLS + ctrl + reg

    def _chat(self, msgs, tools) -> dict:
        last = None
        cands = models.candidates("agent") or models.candidates("smart") or models.candidates("chat")
        for prov, model in cands:
            if models.cooling(prov, model):
                continue
            try:
                js = models.chat(prov, model, msgs, timeout=180, tools=tools, tool_choice="auto")
                return (js.get("choices") or [{}])[0].get("message") or {}
            except Exception as e:
                last = e
                print(f"[Missions] {prov.get('id')}/{model}: {str(e)[:160]}")
        raise RuntimeError(f"no AI model could run the mission ({last})")

    def _check(self, m: Mission, phase_status: str):
        cur = self.store.get(m.id)
        if cur is None or cur.status in ("cancelled", "paused") or \
                (phase_status and cur.status not in (phase_status, "waiting_approval")):
            raise _Stop()

    def _loop(self, m: Mission, ws: Workspace, phase: str):
        status = {"planning": "planning", "executing": "executing", "routine": "executing"}[phase]
        tools = self._tools(phase)
        hist = list(m.history) or [{"role": "user", "content": "Begin." if phase != "executing"
                                    else "Continue the build from the first unfinished step."}]
        steps_used = 0
        while True:
            self._check(m, status)
            if steps_used >= STEP_BUDGET:
                self.store.update(m, status="paused", phase_note=status, history=hist[-60:])
                self._say(m, "I've used this session's step budget — say 'resume mission' to let me continue.", True)
                return
            msgs = [{"role": "system", "content": self._system(m, phase)}] + _trim(hist)
            msg = self._chat(msgs, tools)
            calls = msg.get("tool_calls") or []
            text = (msg.get("content") or "").strip() if isinstance(msg.get("content"), str) else ""
            if not calls:
                # a plain answer without tools: nudge once, then treat as finish
                hist.append({"role": "assistant", "content": text or "(no reply)"})
                if phase == "planning" and not m.plan:
                    hist.append({"role": "user", "content": "Call submit_plan with the full plan and steps."})
                    steps_used += 1
                    continue
                self._finish(m, text or "Done.", phase)
                return
            hist.append({"role": "assistant", "content": text or None, "tool_calls": calls})
            for c in calls:
                fn = c.get("function") or {}
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                steps_used += 1
                self.store.update(m, iterations=m.iterations + 1, phase_note=f"{name}")
                out, stop = self._dispatch(m, ws, phase, name, args)
                hist.append({"role": "tool", "tool_call_id": c.get("id"), "content": out[:MAX_TOOL_OUT]})
                if stop:
                    self.store.update(m, history=hist[-60:])
                    return
            self.store.update(m, history=hist[-60:])

    def _dispatch(self, m: Mission, ws: Workspace, phase: str, name: str, args: dict) -> tuple[str, bool]:
        # ── control tools ─────────────────────────────────────────────────────
        if name == "report_progress":
            self._say(m, str(args.get("message", ""))[:400], bool(args.get("important")))
            return "reported", False
        if name == "ask_user":
            q = str(args.get("question", ""))[:500]
            self.store.update(m, status="waiting_user", pending_question=q,
                              phase_note="planning" if phase == "planning" else "executing")
            self._say(m, f"Question: {q}", True)
            self._hook("notify", "Jarvis has a question", q)
            return "waiting for the user's answer", True
        if name == "submit_plan":
            plan = str(args.get("plan_markdown", ""))
            steps = [{"title": str(s)[:160], "status": "todo"} for s in (args.get("steps") or [])][:30]
            (Path(m.workspace) / "PLAN.md").write_text(plan, encoding="utf-8")
            self.store.update(m, plan=plan, steps=steps, status="awaiting_plan_approval",
                              phase_note="plan ready", history=[])
            self._say(m, f"The plan for {m.title} is ready — {len(steps)} steps. Please review it on the HUD "
                         f"and approve or tell me what to change.", True)
            self._hook("show_plan", m)
            self._hook("notify", "Plan ready for approval", m.title)
            return "plan submitted; waiting for approval", True
        if name == "complete_step":
            i = int(args.get("index", -1))
            if 0 <= i < len(m.steps):
                m.steps[i]["status"] = "done"
                self.store.update(m, steps=m.steps)
                note = str(args.get("note", "")).strip()
                self._say(m, f"Step {i + 1}/{len(m.steps)} done: {m.steps[i]['title']}" + (f" — {note}" if note else ""))
                return "ok", False
            return f"no step {i}", False
        if name == "finish":
            self._finish(m, str(args.get("summary", "Done.")), phase)
            return "finished", True
        # ── workspace tools ───────────────────────────────────────────────────
        if name in WORKSPACE_TOOL_NAMES:
            if name == "run_command":
                return self._run_command_waiting(m, ws, args), False
            try:
                out = ws.call(name, args, plan=m.plan)
                if name == "preview_site":
                    self.store.update(m, artifacts=list(dict.fromkeys(m.artifacts + ["review/"])))
                return out, False
            except Exception as e:
                return f"{name} failed: {e}", False
        # ── Jarvis's own tools ────────────────────────────────────────────────
        if self._run_tool is None:
            return f"unknown tool {name}", False
        try:
            return str(self._run_tool(name, args)), False
        except Exception as e:
            return f"{name} failed: {e}", False

    def _run_command_waiting(self, m: Mission, ws: Workspace, args: dict) -> str:
        """run_command, but if it needs the user's CONFIRM, wait for the answer."""
        from core import confirm
        from core.safety import run_guarded
        box, ev, sync = {}, threading.Event(), [True]
        cmd = str(args.get("command", ""))

        def go():
            r = ws._exec(cmd, int(args.get("timeout_seconds") or 120))
            box["r"] = r
            ev.set()
            return r if sync[0] else "command finished"
        res = run_guarded("mission_cmd", f"#{m.id}: {cmd[:60]}", cmd, go)
        sync[0] = False
        if not res.startswith("[CONFIRMATION_PENDING]"):
            return res
        prev = m.status
        self.store.update(m, status="waiting_approval", phase_note=prev)
        self._say(m, f"Waiting for your approval on the HUD: {cmd[:80]}", True)
        try:
            while not ev.wait(0.5):
                if not confirm.pending_title() and not ev.wait(1.5):
                    return "The user did not approve that command, so it was not run. Choose another way or ask the user."
                cur = self.store.get(m.id)
                if cur and cur.status == "cancelled":
                    raise _Stop()
            return box.get("r", "(no output)")
        finally:
            cur = self.store.get(m.id)
            if cur and cur.status == "waiting_approval":
                self.store.update(m, status=prev)

    def _finish(self, m: Mission, summary: str, phase: str):
        if phase == "routine":
            nr = next_run(m.schedule) or (time.time() + 3600)
            self.store.log(m, f"Result: {summary[:600]}")
            self.store.update(m, status="scheduled", runs=m.runs + 1, history=[], next_run=nr,
                              result=summary[:1000], phase_note="waiting for next run")
            self._hook("announce", f"[#{m.id}] {m.title}: {summary[:300]}", False)
            return
        (Path(m.workspace) / "REPORT.md").write_text(f"# {m.title}\n\n{summary}\n", encoding="utf-8") \
            if not (Path(m.workspace) / "REPORT.md").exists() else None
        self.store.update(m, status="done", result=summary[:2000], phase_note="complete", history=[])
        self._say(m, f"Mission complete — {summary[:500]}", True)
        self._hook("notify", "Mission complete", f"{m.title}: {summary[:200]}")


def _trim(hist: list) -> list:
    """Keep the conversation small but never start on an orphaned tool reply."""
    h = hist[-40:] if len(hist) > 40 else list(hist)
    while h and h[0].get("role") == "tool":
        h.pop(0)
    if h and h[0].get("role") != "user":
        h.insert(0, {"role": "user", "content": "Continue."})
    return h
