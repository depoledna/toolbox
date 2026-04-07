"""Shared bash command risk classification patterns.

Used by both the MCP bash tool and the PreToolUse hook.
Underscore prefix keeps this from being auto-discovered as a tool.
"""

import re

# fmt: off

HIGH_PATTERNS = [
    # ── File/directory destruction ──
    (r"\brm\s+.*-[^\s]*r.*-[^\s]*f\b.*\s+[/~.*]",     "rm -rf on dangerous path"),
    (r"\brm\s+.*-[^\s]*f.*-[^\s]*r\b.*\s+[/~.*]",     "rm -fr on dangerous path"),
    (r"\brm\s+-[^\s]*rf[^\s]*\s+['\"]?[/~.*]",         "rm -rf on dangerous path"),
    (r"\brm\s+-[^\s]*fr[^\s]*\s+['\"]?[/~.*]",         "rm -fr on dangerous path"),
    (r"\brm\s+--recursive\s+--force\b",                  "rm --recursive --force"),
    (r"\brm\s+--force\s+--recursive\b",                  "rm --force --recursive"),
    (r"\bshred\b",                                        "shred (unrecoverable file destruction)"),
    (r"\bwipefs\b",                                       "wipefs (filesystem signature wipe)"),

    # ── Disk operations ──
    (r"\bdd\s+.*(?:if|of)=",                              "dd disk operation"),
    (r"\bmkfs\b",                                         "filesystem creation"),
    (r"\bmke2fs\b",                                       "filesystem creation"),
    (r"\bfdisk\b",                                        "partition editing"),
    (r"\bgdisk\b",                                        "GPT partition editing"),
    (r"\bparted\b",                                       "partition editing"),
    (r"\bbadblocks\s+-w\b",                               "destructive disk test"),
    (r"\bblkdiscard\b",                                   "disk sector discard"),
    (r">\s*/dev/[shr]?disk",                              "write to block device"),
    (r">\s*/dev/[sh]d",                                   "write to block device"),

    # ── macOS disk operations ──
    (r"\bdiskutil\s+(erase|zero|secure|partition)",       "diskutil destructive operation"),
    (r"\bdiskutil\s+apfs\s+(delete|erase)",               "diskutil APFS destruction"),

    # ── System control ──
    (r"\bshutdown\b",                                     "system shutdown"),
    (r"\breboot\b",                                       "system reboot"),
    (r"\bhalt\b",                                         "system halt"),
    (r"\bpoweroff\b",                                     "system poweroff"),
    (r"\binit\s+[06]\b",                                  "init halt/reboot"),
    (r"\bsystemctl\s+(poweroff|reboot|halt)\b",          "systemd shutdown"),
    (r"\bkill\s+-9\s+-1\b",                               "kill all user processes"),
    (r"\bkillall\s+-9\b",                                 "kill all processes by name"),

    # ── Permission/ownership on system paths ──
    (r"\bchmod\s+.*-R\s+\d+\s+/",                        "recursive chmod on root path"),
    (r"\bchmod\s+\d+\s+.*-R\s+/",                        "recursive chmod on root path"),
    (r"\bchown\s+.*-R\s+\S+\s+/",                        "recursive chown on root path"),

    # ── Git destructive ──
    (r"\bgit\s+push\b",                                   "git push"),
    (r"\bgit\s+push\s+\S+\s+\+\s*(main|master)\b",      "force push via + to main/master"),
    (r"\bgit\s+clean\s+-[^\s]*f[^\s]*d",                 "git clean -fd (deletes untracked files)"),
    (r"\bgit\s+reflog\s+expire\b",                       "git reflog expiry (destroys safety net)"),

    # ── Fork bombs / resource exhaustion ──
    (r"\(\)\s*\{[^}]*\|[^}]*&\s*\}\s*;",                "fork bomb pattern"),
    (r"\byes\s*>\s*/dev/null\b",                          "CPU exhaustion loop"),

    # ── macOS system ──
    (r"\bcsrutil\s+(disable|clear)\b",                    "disable System Integrity Protection"),
    (r"\bdefaults\s+delete\s+NSGlobalDomain\b",          "delete global macOS preferences"),
    (r"\bdefaults\s+delete\s+-g\b",                       "delete global macOS preferences"),
    (r"\blaunchctl\s+(bootout|disable)\s+system/",       "unload/disable system daemon"),
    (r"\bnvram\s+-c\b",                                   "clear all NVRAM"),
    (r"\bspctl\s+--master-disable\b",                     "disable Gatekeeper"),
    (r"\bfdesetup\s+disable\b",                           "disable FileVault encryption"),
    (r"\bdscl\s+\.\s+-delete\b",                          "delete macOS user/group"),

    # ── Dangerous pipes / obfuscation ──
    (r"\bcurl\b.*\|\s*(ba)?sh\b",                         "pipe remote script to shell"),
    (r"\bwget\b.*\|\s*(ba)?sh\b",                         "pipe remote script to shell"),
    (r"\bbase64\s+-d\s*\|\s*(ba)?sh\b",                  "decode and execute hidden command"),
    (r"\beval\b.*\$\(",                                   "eval with command substitution"),
    (r"\bhistory\s*\|\s*(ba)?sh\b",                      "re-execute command history"),
    (r"\bcrontab\s+-r\b",                                 "remove all cron jobs"),
]

MEDIUM_PATTERNS = [
    # ── Git operations ──
    (r"\bgit\s+reset\s+--hard\b",                        "git reset --hard"),
    (r"\bgit\s+checkout\s+--\s*\.",                       "git checkout -- . (discard changes)"),
    (r"\bgit\s+restore\s+\.",                             "git restore . (discard changes)"),
    (r"\bgit\s+branch\s+-D\b",                           "git branch -D (force delete)"),
    (r"\bgit\s+stash\s+(drop|clear)\b",                  "git stash drop/clear"),

    # ── File operations ──
    (r"\brm\s+-r\b",                                      "recursive file deletion"),
    (r"\bmv\s+-f\b",                                      "force move (overwrite)"),
    (r"\bcp\s+-[^\s]*f",                                  "force copy (overwrite)"),
    (r"\bchmod\s+-R\b",                                   "recursive permission change"),

    # ── Package managers ──
    (r"\bbrew\s+(install|remove|uninstall|upgrade)\b",   "brew package change"),
    (r"\bpip\s+(install|uninstall)\b",                    "pip package change"),
    (r"\bnpm\s+(install|uninstall)\s+-g\b",              "npm global package change"),
    (r"\bnpm\s+uninstall\b",                              "npm uninstall"),

    # ── macOS preferences ──
    (r"\bdefaults\s+(write|delete)\b",                    "macOS defaults change"),
    (r"\blaunchctl\s+(load|unload)\b",                    "launchctl service change"),

    # ── Containers / orchestration ──
    (r"\bdocker\s+(rm|rmi|system\s+prune)\b",            "docker remove/prune"),
    (r"\bkubectl\s+(delete|apply)\b",                     "kubectl change"),
]

# fmt: on


def classify(command: str) -> tuple[str, str | None]:
    """Classify command risk level. Returns (level, reason)."""
    for pattern, reason in HIGH_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return "high", reason
    for pattern, reason in MEDIUM_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return "medium", reason
    return "low", None
