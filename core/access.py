"""
Access modes.

  full        (default) everything works as before; risky steps ask on the HUD.
  restricted  Jarvis may READ anything and EDIT code, but:
                • no git writes (commit, push, merge, rebase, reset, tag, stash…)
                • nothing at risk level 4+ runs (deletes, installs, kills,
                  registry, shutdown…) — refused outright, not even approvable
                • in missions it must list the project and read a file before
                  editing it, and must edit existing files surgically
                  (edit_file) instead of rewriting them
                • the project generator (dev_agent) is disabled

The mode is changed ONLY from the ⚙ control deck (a human at the PC) — there is
deliberately no voice or Telegram command for it, so the model cannot unlock
itself.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

CFG = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
MODES = ("full", "restricted")

GIT_WRITE = re.compile(
    r"\bgit\s+(?:-C\s+\S+\s+)?(?:commit|push|merge|rebase|reset|revert|tag|stash|cherry-pick|am|"
    r"clean|rm|mv|branch\s+-[dDmM]|checkout\s+-[bB]|switch\s+-[cC]|remote\s+(?:add|remove|set-url)|"
    r"config\s+--global|filter-branch|update-ref|gc|prune|restore\s+--staged|apply)\b",
    re.IGNORECASE)


def get_mode() -> str:
    try:
        v = str(json.loads(CFG.read_text(encoding="utf-8")).get("access_mode", "full")).lower()
    except Exception:
        v = "full"
    return v if v in MODES else "full"


def is_restricted() -> bool:
    return get_mode() == "restricted"


def set_mode(mode: str) -> str:
    from memory.config_manager import _save_flag
    m = mode if mode in MODES else "full"
    _save_flag("access_mode", m)
    return m


RESTRICTED_RULES = (
    "ACCESS MODE: RESTRICTED. You may read anything and change code, nothing else:\n"
    "- NEVER commit, push, merge, rebase, reset, stash or otherwise write to git (read-only git like "
    "status/diff/log is fine). Never delete files, install/uninstall software, kill processes or touch "
    "system settings — those are refused in this mode.\n"
    "- Before changing anything: list_dir the project and READ every file relevant to the change (and the "
    "files that import/use it) so you understand the structure, naming, style and patterns.\n"
    "- Make the SMALLEST clear change that does the job, in the existing style and structure. Prefer "
    "edit_file with exact snippets; do not rewrite whole files, reformat, rename, or add dependencies "
    "unless the plan says so. New files go where similar files already live.\n"
    "- After changing, re-read the edited parts and run the project's existing checks/tests if there are any."
)
