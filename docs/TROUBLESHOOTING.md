# Troubleshooting

First look at `logs/jarvis.jsonl` (one JSON object per line; newest at the bottom). Filter by `"level": "ERROR"` or by `module` (`jarvis`, `route`, `pipeline`, `clap`, `safety`, `wake`).

| Symptom | Where | Likely cause → fix |
|---|---|---|
| Setup screen keeps appearing | `ui.py` `_check_config` | No provider saved or `os_system` missing → add a key in AI MODELS, press INITIALISE / SAVE. |
| "API key invalid — please re-enter" | `main.py` run loop | Gemini rejected the key (Live error 1007) → re-enter it in AI MODELS; TEST shows the exact error. |
| TEST says `HTTP 401` / `403` | `core/models.fetch_models` | Wrong key or provider → copy the key again from GET KEY ↗. |
| TEST fails for Ollama / LM Studio | `core/models.fetch_models` | Local server not running → start Ollama / LM Studio; check the base URL. |
| Voice silent with no Gemini key | `core/pipeline_session.py` | No STT model: add Groq or OpenAI (whisper) key, or `pip install faster-whisper` for local STT. Log: `no speech-to-text available`. |
| Pipeline says "couldn't reach any AI model" | `PipelineSession._chat` | Every CHAT candidate failed → see `[Pipeline]` lines in the log for the HTTP error. |
| A job used an unexpected model | `core/models.candidates` | AUTO picked by tags → pin the model for that job in AI MODELS → JOB → MODEL. Log line `[Route] role → provider/model`. |
| Double clap never wakes | `core/clap.py` | Mode is OFF or Jarvis is awake; raise SENSITIVITY; claps must be 0.15–0.8 s apart and not followed by a third. |
| Clap wakes on other sounds | `core/clap.py` | Lower SENSITIVITY to LOW. |
| Clap does not launch Jarvis | `clap_launcher.py` | Mode must be WAKE + LAUNCH; see `config/clap_launcher.log`; the launcher only listens while Jarvis is closed. |
| Second window will not open | `core/clap_launch.acquire_instance_lock` | By design — the running copy is woken instead. Stale lock is ignored automatically if that PID is gone. |
| "[SECURITY_BLOCKED]" reply | `core/safety.py` | The code matched a level-7 rule (format, system-folder wipe, Defender off…). Not approvable by design. |
| APPROVAL REQUIRED banner | `core/safety.py` → `core/confirm.py` | Risk 4–6 (delete, kill, install, registry, shutdown, force-push). Press CONFIRM within 90 s or CANCEL. |
| HUD globe missing | `ui.py` HudCanvas | `core/globe.py` failed to import → HUD falls back to the reactor core; check the log for the traceback. |
| Fonts look like Courier | `ui.load_fonts` | `assets/fonts/*.ttf` missing → re-clone or restore the folder. |
| Wrong mic / speaker | ⚙ → AUDIO DEVICES | Pick the device; Jarvis reconnects. |
| Mission stuck on "PLAN READY" | `core/agent.py` | By design — approve or revise it (HUD brief, voice "approve the plan", or Telegram /approve). |
| Mission paused "step budget" | `core/agent.py` STEP_BUDGET | Long jobs pause after 60 tool calls per run; say "resume the mission". |
| Mission failed "no AI model could run the mission" | `MissionControl._chat` | The AGENT job's models all errored — check `[Missions]` lines; pin a working model for AGENT in AI MODELS. |
| HTTP 404 "no longer available to new users" | Gemini | Google retired that model for new keys — AUTO prefers `-latest`; re-TEST the Gemini key to refresh the model list. |
| preview_site fails | `core/agent_tools.py` | Playwright browser missing → `.venv\Scripts\python -m playwright install chromium`. |
| Telegram silent | `core/telegram_bridge.py` | Token not set/paired → Plugin Settings → Telegram → CONNECT, then send the 6-digit code to the bot. Only the paired chat is answered. |
| Gmail/Calendar "isn't connected" | `plugins/_google_core.py` | Add the OAuth Desktop-app client_secret path in Plugin Settings → Google and press SIGN IN. |
| Screen watch never speaks | `main._run_screen_watch` | It only speaks for errors/dialogs, when awake and idle; check `WATCH:` lines in the log. |
| "Claude Code is installed but not signed in" | `core/claude_cli.py` | Run the CLI once in a terminal and sign in: `& "$env:APPDATA\Claude\claude-code\*\claude.exe"` then `/login`. |
| Coding jobs don't use Claude | AI MODELS | Press TEST on "Claude Code" (proves sign-in), then leave CODE on AUTO or pin claudecode/claude-code. |
| Restricted mode refuses something you want | ⚙ → ACCESS | Switch to FULL access (only from the control deck, never by voice). |
| Jarvis repeats an old mistake | `core/mind.py` | Lessons are distilled after each mission into memory/mind.json; say "remember that …" to add one directly. |
