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


FLOW_KINDS = ("website", "code", "research")


def needs_flow(m: Mission) -> bool:
    """Builds get a discovery stage first: agree the flow, then plan the build."""
    return m.kind in FLOW_KINDS and not m.flow_approved


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
    def create(self, goal: str, kind: str = "general", schedule: str = "", title: str = "",
               workspace: str = "") -> Mission:
        m = self.store.create(goal, kind=kind, title=title, schedule=schedule, workspace=workspace)
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
        if m.stage == "flow":
            self.store.update(m, flow_approved=True, stage="", status="planning", history=[],
                              phase_note="writing the detailed plan")
            self._say(m, "Flow agreed — now writing the detailed build plan.", True)
            return f"Flow for #{m.id} approved; writing the detailed plan next."
        self.store.update(m, status="executing", history=[], phase_note="building")
        self._say(m, "Plan approved — building now.", True)
        return f"Plan for #{m.id} approved; building in the background."

    def revise(self, m: Mission, feedback: str) -> str:
        m.plan_feedback.append(feedback.strip())
        what = "flow" if m.stage == "flow" else "plan"
        self.store.update(m, status="planning", stage="", history=[], phase_note=f"revising the {what}")
        self._say(m, f"Revising the {what}: {feedback.strip()}")
        return f"Revising the {what} for #{m.id}."

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
        from core import access
        parts.append(access.RESTRICTED_RULES if access.is_restricted() else
                     "ACCESS MODE: FULL. Risky commands still ask the user on the HUD; destructive ones are refused.")
        try:
            from core import claude_cli
            if claude_cli.available() and phase != "routine":
                parts.append(
                    "CODING BUDGET: Claude Code (delegate_coding) is the best coder here but its allowance is "
                    "LIMITED, unlike your own model. Spend it well:\n"
                    "- Delegate ONE call per build step (or fewer, batching related files) — never one call per "
                    "file, never for trivial edits.\n"
                    "- Put the whole spec for that step INSIDE the instruction (files, exact behaviour, design "
                    "tokens, acceptance criteria) so Claude never has to explore the project or ask.\n"
                    "- Name only the few files it must read.\n"
                    "- Do everything else yourself: research, decisions, copy, small edits, config, reviewing "
                    "screenshots, fixing one-line problems, and all reporting.\n"
                    "- Pass the model and effort the plan's EXECUTION PLAN chose for that step (default "
                    "sonnet/medium; haiku/low for mechanical edits; opus or high+ only for genuinely hard work).\n"
                    "- If it says its allowance is spent, carry on writing the code yourself from the plan.")
        except Exception:
            pass
        if m.project:
            parts.append("This mission works INSIDE AN EXISTING PROJECT (the workspace). Before planning or "
                         "changing anything, list the project and read the files that matter so you follow its "
                         "existing structure, conventions and patterns. Change as little as needed, clearly.")
        if m.answer:
            parts.append(f"The user's latest answer to your question: {m.answer}")
        try:
            from core import mind
            brief = mind.brief_for(f"{m.goal} {m.kind}")
            if brief:
                parts.append(brief)
        except Exception:
            pass
        if phase == "planning" and needs_flow(m):
            parts.append(
                "PHASE: DISCOVERY (before any plan). Understand the idea deeply first:\n"
                "1. Research what the user refers to (e.g. a game, app or style they name — what it is, its "
                "signature mechanics and look), and 3-5 comparable products (web_search, fetch_url). Save notes.\n"
                "2. Work out HOW THE PRODUCT WORKS: the core idea and loop, the user journey step by step from "
                "first visit to daily use, every screen/page and what is on it, features (must-have vs later), "
                "data it stores and where, key mechanics/rules with numbers (e.g. XP, levels, ranks, streaks), "
                "and edge cases.\n"
                "3. Give 2-3 technology stack options (e.g. static HTML/JS + localStorage; React/Vite; Next.js + "
                "Supabase/Firebase) with pros/cons, cost, hosting, and a recommendation for THIS user.\n"
                "4. List the open questions only the user can answer.\n"
                "If something fundamental is unclear, use ask_user (it reaches the user on Telegram too) — but "
                "prefer proposing sensible defaults in the flow and asking in its questions list.\n"
                "Then call propose_flow. Do NOT write the build plan or build anything yet.")
            if m.flow:
                parts.append("YOUR PREVIOUS FLOW DRAFT:\n" + m.flow[:10000])
            if m.plan_feedback:
                parts.append("THE USER'S FEEDBACK / ANSWERS ON THE FLOW (apply all of them):\n- "
                             + "\n- ".join(m.plan_feedback))
        elif phase == "planning":
            parts.append(
                "PHASE: PLANNING. Research what you still need (web_search, fetch_url), save notes, then call "
                "submit_plan with a COMPLETE, IMPLEMENTATION-READY plan. Do NOT build anything yet.\n"
                "The plan is the single source of truth a separate coder will build from WITHOUT asking "
                "questions or exploring, so it must leave no decision open. It must contain:\n"
                "- Summary, goals, audience, and the requirements checklist (mark assumptions).\n"
                "- The agreed flow: journey, screens/sections and what is on each.\n"
                "- DESIGN TOKENS where visual: exact hex palette with roles, font families and sizes/scale, "
                "spacing/radius rules, and the named motion/interactions.\n"
                "- FILE MAP: every file to create or change, one line each saying what it holds.\n"
                "- DATA: shapes/schemas, storage, and the exact keys/fields.\n"
                "- CONTENT: the real copy (or exactly where it comes from) — never 'lorem ipsum' or 'TBD'.\n"
                "- BUILD STEPS: 6-12 steps; each step names the files it touches and its ACCEPTANCE CRITERIA "
                "(what must be true when it is done).\n"
                "- EXECUTION PLAN: a small table saying, per step, WHO builds it (me = your own model, free / "
                "Claude Code = limited allowance), and for Claude steps WHICH MODEL and EFFORT: "
                "haiku = small mechanical edits, sonnet = normal implementation (default), opus = only genuinely "
                "hard architecture; effort low|medium|high|xhigh|max = how long it may think. Budget the whole "
                "build to at most 8 Claude calls and say the expected total.\n"
                "- Open questions only for things genuinely needing the user.\n"
                "Be precise and compact: specifics over prose, no filler, no repetition.")
            if m.flow:
                parts.append("AGREED FLOW (the plan must implement exactly this, with the chosen stack):\n"
                             + m.flow[:12000])
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

    def _tools(self, phase: str, m: Mission | None = None) -> list:
        from core.pipeline_session import openai_tools
        disc = m is not None and phase == "planning" and needs_flow(m)
        ctrl = [t for t in CONTROL_TOOLS
                if not (t["function"]["name"] == "propose_flow" and not disc)
                and not (t["function"]["name"] == "submit_plan" and (phase != "planning" or disc))
                and not (t["function"]["name"] == "complete_step" and phase != "executing")
                and not (t["function"]["name"] == "ask_user" and phase == "routine")]
        reg = [t for t in openai_tools(self._decls)
               if t["function"]["name"] not in WORKSPACE_TOOL_NAMES | CONTROL_TOOL_NAMES | _SKIP_REGISTRY]
        return WORKSPACE_TOOLS + ctrl + reg

    def _chat(self, msgs, tools, role: str = "agent") -> dict:
        last = None
        seen = set()
        cands = []
        for r in (role, "agent", "smart", "chat"):
            for pm in models.candidates(r):
                key = (pm[0].get("id"), pm[1])
                if key not in seen:
                    seen.add(key)
                    cands.append(pm)
        for prov, model in cands:
            if models.cooling(prov, model) or prov.get("kind") == "cli":
                continue            # the CLI agent has no tool-calling; it is used via delegate_coding
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
        from core import router
        status = {"planning": "planning", "executing": "executing", "routine": "executing"}[phase]
        role = router.for_phase(m.kind, phase)
        _r, _p, _mod, _why = router.pick(m.goal, role=role, exclude_cli=True)
        print(f"[Route] mission #{m.id} {phase} → {(_p or {}).get('preset', '?')}/{_mod} ({_why})")
        tools = self._tools(phase, m)
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
            msg = self._chat(msgs, tools, role)
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
            self._say(m, f"Question for you: {q} — reply here or on Telegram.", True)
            self._hook("notify", "Jarvis has a question", q)
            return "waiting for the user's answer", True
        if name == "propose_flow":
            flow = str(args.get("flow_markdown", ""))
            qs = [str(q) for q in (args.get("questions") or []) if str(q).strip()][:12]
            if qs:
                flow += "\n\n## Questions for you\n" + "\n".join(f"{i + 1}. {q}" for i, q in enumerate(qs))
            (Path(m.workspace) / "FLOW.md").write_text(flow, encoding="utf-8")
            self.store.update(m, flow=flow, stage="flow", status="awaiting_plan_approval",
                              phase_note="flow ready for discussion", history=[])
            self._say(m, f"I've mapped out how {m.title} would work — the flow, the screens and the tech stack "
                         f"options{', plus ' + str(len(qs)) + ' questions for you' if qs else ''}. Let's discuss it "
                         f"before I write the detailed plan.", True)
            self._hook("show_plan", m)
            self._hook("notify", "Flow ready to discuss", m.title)
            return "flow submitted; waiting for the user", True
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
        # Remember what this job taught, for the next one.
        def _learn(mm=m):
            try:
                from core import mind
                mind.remember("event", f"{mm.title}: {summary[:200]}", tags=[mm.kind], source=f"mission #{mm.id}")
                mind.distil(mm)
            except Exception as e:
                print(f"[Mind] {e}")
        threading.Thread(target=_learn, daemon=True, name=f"learn-{m.id}").start()
        self._hook("notify", "Mission complete", f"{m.title}: {summary[:200]}")


def _trim(hist: list) -> list:
    """Keep the conversation small but never start on an orphaned tool reply."""
    h = hist[-40:] if len(hist) > 40 else list(hist)
    while h and h[0].get("role") == "tool":
        h.pop(0)
    if h and h[0].get("role") != "user":
        h.insert(0, {"role": "user", "content": "Continue."})
    return h
