# Playbook: software / code project

## Planning
- Clarify the goal into user stories and acceptance criteria. Research libraries/APIs with web_search and
  fetch_url when unsure; prefer well-maintained, simple dependencies.
- Plan must include: summary, requirements checklist, architecture (modules/files and their
  responsibilities), data model with exact fields, dependencies, how it will be run and tested, a file map,
  build steps with acceptance criteria per step, and open questions.
- Write it so a separate coder can implement it without asking anything or exploring the repo.

## Build
- If delegate_coding is available, give it the implementation work (with the plan, the files and the
  acceptance criteria) and review the result; otherwise write the files yourself.
- Create files in the workspace with write_file / edit_file. Keep functions small, typed, with error handling.
- Install dependencies and run with run_command (Python: `python -m pip install …`, `python main.py`).
- Write at least a smoke test and run it. Read errors, fix, re-run — iterate until it works.
- Finish with README.md (setup, usage) and REPORT.md, then `finish` with a short summary.

## Changing an existing project
- Start by mapping it: list_dir, read the README, entry points, config and every file the change touches or
  that calls into it. Note the conventions (naming, folder layout, error handling, test style).
- Plan the smallest change that fits those conventions; say which files change and why.
- Make surgical edits with edit_file; keep formatting and structure; add files only where similar files live.
- Run the project's existing tests/linters if present. Never commit — the user reviews the diff.
