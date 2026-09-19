# Playbook: software / code project

## Planning
- Clarify the goal into user stories and acceptance criteria. Research libraries/APIs with web_search and
  fetch_url when unsure; prefer well-maintained, simple dependencies.
- Plan must include: summary, requirements checklist, architecture (modules/files and their
  responsibilities), data model, dependencies, how it will be run and tested, build steps, open questions.

## Build
- Create files in the workspace with write_file / edit_file. Keep functions small, typed, with error handling.
- Install dependencies and run with run_command (Python: `python -m pip install …`, `python main.py`).
- Write at least a smoke test and run it. Read errors, fix, re-run — iterate until it works.
- Finish with README.md (setup, usage) and REPORT.md, then `finish` with a short summary.
