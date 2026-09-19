"""
Command / code safety screen.

Every tool that runs code or commands the *model* wrote (code_helper, the dev
agent, desktop automation) passes that text through `run_guarded()` first.

`assess()` rates it on a 0–7 scale, like a permission level:

    0      nothing notable                         → runs
    1–3    worth logging (network, file writes)     → runs, logged
    4–6    needs a human: deletes, kills, installs,
           registry, shutdown, force-push, e-mail  → CONFIRM banner on the HUD
    7      destructive with no legitimate use from
           a voice assistant: format a drive, wipe
           a system folder, disable the antivirus  → blocked, never runs

Approval reuses core/confirm.py, so the go-ahead comes from a button the user
presses — the model cannot forge it. The patterns are deliberately
conservative: a false "needs approval" costs one click; a miss can cost a disk.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

# (level, compiled pattern, reason)
_RULES: list[tuple[int, re.Pattern, str]] = []


def _r(level: int, pattern: str, reason: str) -> None:
    _RULES.append((level, re.compile(pattern, re.IGNORECASE | re.MULTILINE), reason))


# ── 7: blocked ───────────────────────────────────────────────────────────────
_ROOTS = r"""(?:[A-Z]:\\?(?:["'\s]|$)|[A-Z]:\\(?:Windows|Program Files|Users)\b|/(?:\s|$|\*)|~/?(?:\s|$)|\$env:(?:SystemRoot|windir|USERPROFILE|HOMEDRIVE)|%(?:SystemRoot|windir|USERPROFILE)%|Path\.home\(\)\s*\)|os\.path\.expanduser\(\s*["']~["']\s*\)\s*\))"""
_r(7, r"\bformat(?:\.com)?\s+[A-Z]:", "formats a drive")
_r(7, r"\bFormat-Volume\b|\bClear-Disk\b|\bInitialize-Disk\b", "wipes a disk")
_r(7, r"\bdiskpart\b", "runs diskpart (disk partitioning)")
_r(7, r"\bbcdedit\b|\bbootrec\b", "changes the boot configuration")
_r(7, r"\bmkfs(?:\.\w+)?\b|\bdd\s+[^\n]*\bof=/dev/", "writes raw to a disk device")
_r(7, r":\(\)\s*\{\s*:\|:&\s*\};:", "fork bomb")
_r(7, r"\brm\s+-[a-z]*r[a-z]*f?[a-z]*\s+(?:--no-preserve-root\s+)?" + _ROOTS, "recursively deletes a system or home root")
_r(7, r"\b(?:del|erase)\s+(?:/[a-z]\s+)*/s\b[^\n]*" + _ROOTS, "mass-deletes from a drive or system folder")
_r(7, r"\b(?:rd|rmdir)\s+(?:/[a-z]\s+)*/s\b[^\n]*" + _ROOTS, "removes a drive or system folder tree")
_r(7, r"\bRemove-Item\b[^\n]*-Recurse[^\n]*" + _ROOTS, "recursively removes a drive, system or home folder")
_r(7, r"\bshutil\.rmtree\(\s*" + _ROOTS, "deletes a drive, system or home folder tree")
_r(7, r"\breg(?:\.exe)?\s+delete\s+HK(?:LM|EY_LOCAL_MACHINE)\b", "deletes machine-wide registry keys")
_r(7, r"\bvssadmin\b[^\n]*\bdelete\b|\bwmic\b[^\n]*shadowcopy[^\n]*delete", "deletes system restore points")
_r(7, r"\bcipher\s+/w\b", "wipes free disk space")
_r(7, r"Set-MpPreference[^\n]*-Disable\w*\s+\$?true|\bDisableAntiSpyware\b", "disables Windows Defender")
_r(7, r"\bnetsh\s+advfirewall\s+set\s+\w+\s+state\s+off", "turns off the firewall")
_r(7, r"\btakeown\b[^\n]*/r\b[^\n]*" + _ROOTS, "takes ownership of system files")

# ── 4–6: needs approval ──────────────────────────────────────────────────────
_r(6, r"\bgit\s+push\b[^\n]*(?:--force\b|-f\b|--force-with-lease)", "force-pushes git history")
_r(6, r"\bgit\s+(?:reset\s+--hard|clean\s+-[a-z]*f)", "discards git work")
_r(6, r"\bshutdown\b|\bRestart-Computer\b|\bStop-Computer\b|\breboot\b", "shuts down or restarts the computer")
_r(6, r"\breg(?:\.exe)?\s+(?:add|delete)\b|\bSet-ItemProperty\s+[^\n]*HK(?:LM|CU):|\bwinreg\.(?:SetValue|DeleteKey|DeleteValue)", "edits the registry")
_r(5, r"\brm\s+-[a-z]*r|\bRemove-Item\b|\b(?:del|erase|rd|rmdir)\s|\bshutil\.rmtree\(|\bos\.(?:remove|unlink|rmdir|removedirs)\(|\.unlink\(|\.rmdir\(", "deletes files or folders")
_r(5, r"\btaskkill\b|\bStop-Process\b|\bkill\s+-9\b|\bpsutil\.[^\n]*\.(?:kill|terminate)\(|\bos\.kill\(", "kills running processes")
_r(5, r"\bpip\s+uninstall\b|\bwinget\s+uninstall\b|\bchoco\s+uninstall\b", "uninstalls software")
_r(5, r"\bsmtplib\b|\bsend_?mail\b|\bgmail\b[^\n]*send", "sends e-mail")
_r(5, r"-EncodedCommand\b|\bFromBase64String\b[^\n]*(?:iex|Invoke-Expression)", "runs hidden (encoded) commands")
_r(5, r"\bSet-ExecutionPolicy\b", "changes the PowerShell execution policy")
_r(4, r"\b(?:winget|choco|npm\s+i(?:nstall)?\s+-g)\s+install\b|\bwinget\s+install\b", "installs software system-wide")
_r(4, r"\bnetsh\b|\bSet-NetAdapter\b|\bDisable-NetAdapter\b", "changes network settings")
_r(4, r"\bschtasks\b|\bRegister-ScheduledTask\b|\bcrontab\b", "creates scheduled tasks")
_r(4, r"\bInvoke-Expression\b|\biex\s*\(|\bcurl\b[^\n]*\|\s*(?:ba)?sh\b|\biwr\b[^\n]*\|\s*iex", "downloads and runs a script")
# ── 1–3: logged only ─────────────────────────────────────────────────────────
_r(2, r"\brequests\.(?:post|put|delete)\(|\burllib\.request\b|\bhttpx\b", "makes network requests")
_r(1, r"\bopen\([^\n]*['\"][wa]b?['\"]|\.write_text\(|\.write_bytes\(", "writes files")

APPROVAL_LEVEL = 4
BLOCK_LEVEL = 7


@dataclass
class Assessment:
    level: int = 0
    reasons: list[str] = field(default_factory=list)
    matches: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.level >= BLOCK_LEVEL

    @property
    def needs_approval(self) -> bool:
        return APPROVAL_LEVEL <= self.level < BLOCK_LEVEL

    def summary(self) -> str:
        return "; ".join(dict.fromkeys(self.reasons)) or "nothing risky"


def assess(text: str) -> Assessment:
    a = Assessment()
    if not text:
        return a
    for level, pat, reason in _RULES:
        m = pat.search(text)
        if m:
            a.reasons.append(reason)
            a.matches.append(m.group(0)[:80])
            a.level = max(a.level, level)
    a.reasons.sort(key=lambda r: -next(l for l, _p, rr in _RULES if rr == r))
    return a


def _audit(msg: str) -> None:
    try:
        from core.jlog import get_logger
        get_logger("safety").warning(msg)
    except Exception:
        print(f"[Safety] {msg}")


def run_guarded(key: str, title: str, text: str, run: Callable[[], str]) -> str:
    """Screen `text` (the code / command about to run) and then:
    block it, park it behind the HUD confirmation, or run it now."""
    a = assess(text)
    from core import access
    if access.is_restricted():
        why = ("writes to git" if access.GIT_WRITE.search(text or "") else
               a.summary() if a.level >= APPROVAL_LEVEL else "")
        if why:
            _audit(f"RESTRICTED-BLOCK ({a.level}/7) {title}: {why}")
            return (f"[RESTRICTED_MODE] Refused: this {why}, which is not allowed in Restricted access mode. "
                    f"Do the task without it (code edits are fine), or tell the user it needs Full access "
                    f"(switched only from the ⚙ control deck).")
    if a.blocked:
        _audit(f"BLOCKED ({a.level}/7) {title}: {a.summary()} :: {a.matches[:3]}")
        return (f"[SECURITY_BLOCKED] I refused to run this because it {a.summary()}. "
                f"That is on my blocklist and cannot be approved. Tell the user in one short "
                f"sentence and suggest a safer way if there is one.")
    if a.needs_approval:
        _audit(f"APPROVAL ({a.level}/7) {title}: {a.summary()}")
        from core import confirm
        if confirm.pending_title():
            return "Another action is already waiting for confirmation on the HUD. Nothing was done."
        preview = text.strip().splitlines()
        preview = "\n".join(preview[:6]) + ("\n…" if len(preview) > 6 else "")
        detail = f"Risk {a.level}/7 — it {a.summary()}.\n{preview}"
        return confirm.request(key, title, detail[:600], run)
    if a.level:
        _audit(f"allowed ({a.level}/7) {title}: {a.summary()}")
    return run()
