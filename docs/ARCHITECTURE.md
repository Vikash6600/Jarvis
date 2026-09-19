# Jarvis (Mark LIV fork) — Architecture

Keep this file current: when you add or change a module, update its row and the flows below.

## Process model

```
┌─────────────────────────── main.py (one process, single instance) ───────────────────────────┐
│ Qt main thread: ui.py MainWindow / HudCanvas / overlays                                        │
│ runner thread : asyncio loop → JarvisLive.run()                                                │
│                  ├─ voice engine: Gemini Live session  OR  core/pipeline_session.PipelineSession│
│                  ├─ _listen_audio (mic 16 kHz) → sleep gate → wake-word / clap detectors         │
│                  ├─ _receive_audio → transcripts, tool calls → _execute_tool → actions/plugins  │
│                  └─ _play_audio (24 kHz) → visemes / audio level → HUD                           │
│ worker threads: wake-word model, clap detector, confirm-gate jobs, radar poller, logging         │
└───────────────────────────────────────────────────────────────────────────────────────────────┘
clap_launcher.py (separate, optional, pythonw) — listens only while Jarvis is closed; starts main.py --wake
```

## Modules

| Module | Responsibility |
|---|---|
| `main.py` | `JarvisLive`: connection loop, mic/speaker streams, sleep gate (wake word + clap), tool dispatch, reconnects. `main()` installs logging, takes the instance lock, starts UI + runner. |
| `ui.py` | PyQt6 HUD: palette `C`, gradients `GRAD_*`, fonts `FONT_*`; `HudCanvas` (globe/face/core), `RadarWidget`, `MetricBar`, `LogWidget`, `ModelHubOverlay` (API keys + routing, also first-run setup), `ConfirmBanner`, control deck. `JarvisUI` is the thread-safe facade main.py talks to. |
| `core/globe.py` | `ParticleGlobe`: 3D particle sphere, rings, audio ring, satellites; drag/hover/click. |
| `core/avatar.py` | Holographic lip-synced head (alternative centrepiece). |
| `core/models.py` | Provider registry (Gemini + OpenAI-compatible presets), key test / model list, tags, per-job routing `candidates(role)`, `voice_engine()`, `chat()`, `transcribe()`. |
| `core/gemini.py` | One-shot model calls. `call()` → `_route()` sends a job to its configured model (OpenAI-compatible → `_openai_call`) or to the original Gemini ladder. |
| `core/pipeline_session.py` | Voice without Gemini Live: VAD → STT → chat model with tools → edge-tts. Mimics the Live session API. |
| `core/clap.py` | `ClapDetector` DSP (loud + sudden + bright + brief, 0.15–0.8 s pair). |
| `core/clap_launch.py` | Instance lock, wake-request file, HKCU Run registration for the launcher. |
| `core/safety.py` | `assess()` risk 0–7 and `run_guarded()` (block ≥7, HUD approval 4–6). |
| `core/confirm.py` | Human-in-the-loop gate: banner on the HUD, action runs only after CONFIRM. |
| `core/missions.py` | Mission store (`memory/missions.json`), states, routine schedule parser. |
| `core/agent.py` | `MissionControl`: supervisor thread (2 concurrent, resumes after restart) + tool-calling agent loop (AGENT model job); plan → approval → execute → review → finish; routines. |
| `core/agent_tools.py` | Workspace tools for missions: files (confined), guarded commands, fetch_url, preview_site (Playwright + vision review), look_at_screen, open_in_browser. |
| `core/playbooks/*.md` | Expert instructions per mission kind (website, code, research, general, routine). |
| `core/telegram_bridge.py` | Telegram long-poll bridge: pairing, commands, missions, chat replies, pushes. |
| `core/notify.py` | Desktop toasts. |
| `plugins/gmail.py`, `plugins/google_calendar.py`, `plugins/_google_core.py` | Google OAuth (desktop flow) + Gmail/Calendar tools; send/create go through the HUD confirm gate. |
| `plugins/telegram_remote.py` | Telegram settings (token, pairing) + `telegram_send` tool. |
| `core/jlog.py` | JSON-lines logging to `logs/jarvis.jsonl`, stdout/stderr tee. |
| `core/wake_word.py` | openwakeword "Hey Jarvis". |
| `actions/*.py` | Tools (`TOOL` dict + handler), auto-discovered by `core/action_loader.py`. Code-running tools (`code_helper`, `dev_agent`, `desktop`) go through `core/safety.run_guarded`. |
| `plugins/*.py` | Optional tools with settings (`PLUGIN` dict + `run`). |
| `memory/config_manager.py` | Settings in `config/api_keys.json` (git-ignored). |
| `dashboard/` | Remote phone dashboard (FastAPI). |

## Key flows

**Voice turn (Gemini Live)** — mic → `_listen_audio` → gate → `send_realtime_input` → Live → `_receive_audio` (audio → speaker, transcripts → log, tool_call → `_execute_tool` → `send_tool_response`).

**Voice turn (pipeline)** — same, but `PipelineSession` cuts utterances by energy, transcribes with the STT job, asks the CHAT job's model with the tool list, speaks with edge-tts.

**One-shot job** — action → `gemini.call(contents, tier)` → role (fast/smart/code/vision/search) → `models.candidates(role)` → first model that answers.

**Double clap** — asleep: mic frames → `ClapDetector.feed` → detector thread → `wake()` + globe ripple. Closed: `clap_launcher.py` → `main.py --wake`; a second launch writes `config/.wake_request` and exits.

**Mission** — voice `start_mission` → `MissionControl.create` → supervisor starts a worker → PLANNING (research with tools, `submit_plan`) → HUD Mission Brief + Telegram → user APPROVE/REVISE → EXECUTING (`complete_step`…, `preview_site` review) → `finish` → toast, spoken update when idle, Telegram. Tool calls in the voice session run off the receive loop (`_dispatch_tools`), so the conversation never blocks.

**Screen watch** — `_run_screen_watch` every 45 s (when enabled) → `_screen_glance` → VISION model → ALERT → spoken offer of help.

**Risky code** — tool → `run_guarded(text, run)` → blocked / HUD approval (`confirm.request`) / run.

## Config keys (`config/api_keys.json`)

`providers`, `model_roles`, `gemini_api_key` (legacy, kept in sync), `os_system`, `wake_word_enabled`, `clap_mode` (off/wake/launch), `clap_sensitivity`, `hud_style` (globe/face/core), `pipeline_voice` (edge-tts voice name), `screen_watch`, `plugin_config.telegram` (token, chat_id), `plugin_config.google` (client_secret path), plus the original Mark LIV keys. Missions live in `memory/missions.json`; workspaces in `~/Documents/Jarvis Missions/`; the Google token in `config/token_google.json`.
