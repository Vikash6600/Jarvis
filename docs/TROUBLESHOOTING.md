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
