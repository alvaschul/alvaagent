import os

# ---------------- autonomy: permissions ----------------
# The agent can run shell commands, edit files and manage skills. Everything
# outside the project folder (or risky) goes through rt.on_permission, which
# the REPL wires to an interactive y/N prompt. Headless (no hook) defaults to
# DENY, unless ALVA_AUTO_APPROVE=1 is set (attended/automated runs).

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# commands that are safe to run without asking
# NOTE: `env` is intentionally NOT here - `env` executes its arguments
# (`env sh -c '...'`, `env -S '...'`) and would let a quoted destructive
# command bypass the risk scan.
_READONLY_PREFIXES = (
    # everyday inspection (single commands only - any `|`, `>`, `&&`, `;`
    # or shell metachar is rejected earlier in classify_command)
    "ls", "cat", "pwd", "whoami", "echo", "date", "which", "find",
    "head", "tail", "grep", "stat", "df", "du", "free", "uname",
    "wc", "readlink", "basename", "dirname", "file", "tree", "ps",
    "id", "hostname", "uptime", "who", "cal", "printf", "realpath", "cd",
    "sort", "uniq", "cut", "tr", "fmt", "paste", "join", "comm",
    "diff", "cmp", "md5sum", "sha256sum", "strings",
    # dev loop: the agent edits its own source, so its own tooling must not nag
    "python3 --version", "python3 -V", "python3 -m py_compile",
    "python3 -m pyflakes", "python3 -m json.tool", "python3 test_tui.py",
    # git read-only inspection (NOT git add/commit/push/pull/branch/reset)
    "git status", "git diff", "git log", "git --version", "git branch",
    "git remote -v", "git show", "git blame", "git ls-files",
    # archive listing only (extract/write variants are NOT allowlisted)
    "tar -tf", "unzip -l", "zipinfo",
    # Termux read-only queries
    "termux-battery-status", "termux-clipboard-get",
)

# anything containing these is treated as mutating/risky -> ask the user
_RISKY_TOKENS = frozenset({
    "rm", "mv", "cp", "mkdir", "touch", "chmod", "chown",
    "chattr", "sudo", "su", "apt", "apt-get", "pkg", "pip", "npm",
    "kill", "pkill", "killall", "reboot", "shutdown", "poweroff", "mkfs",
    "dd", "wget", "curl", "git push", "git commit",
    "git reset", "git clean", "git checkout", "git branch",
    "systemctl", "service", "mount", "umount", "fdisk", "tee",
    # command interpreters/executors: `sh -c '...'` runs arbitrary (quoted)
    # commands, so they can never be treated as read-only.
    "sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish", "pwsh",
    "eval", "exec", "source", "command", "xargs", "nohup", "env",
})
_RISKY_OPERATORS = frozenset({">", ">>", "|", "&&", "||", "&", ";"})


def _tokenize_shell(cmd):
    """Simple shell-aware tokenizer. Splits on whitespace and quoted strings."""
    tokens = []
    i = 0
    while i < len(cmd):
        if cmd[i] in ('"', "'"):
            q = cmd[i]
            i += 1
            while i < len(cmd) and cmd[i] != q:
                i += 1
            i += 1
            tokens.append("QUOTED")
        elif cmd[i] in ('>', '|', ';', '&'):
            op = cmd[i]
            if i + 1 < len(cmd) and cmd[i:i+2] in ('>>', '&&', '||', '2>'):
                op = cmd[i:i+2]
                i += 1
            tokens.append(op)
            i += 1
        elif not cmd[i].isspace():
            start = i
            while i < len(cmd) and not cmd[i].isspace() and cmd[i] not in ('>', '|', ';', '&', '"', "'"):
                i += 1
            tokens.append(cmd[start:i])
        else:
            i += 1
    return tokens


def classify_command(cmd):
    """allow / ask / deny for a shell command (token-aware, safe default)."""
    c = cmd.strip()
    if not c:
        return "deny"
    # Shell metacharacters that enable command substitution / sub-shells are
    # never needed by the allowlisted read-only commands -> always ask.
    # (e.g. ``echo $(touch /tmp/x)`` and ``echo `touch /tmp/x` `` must NOT
    # pass as read-only - they execute arbitrary commands.)
    if any(ch in c for ch in "$`(){}"):
        return "ask"
    tokens = _tokenize_shell(c)
    # Risky operators anywhere -> ask
    if any(t in _RISKY_OPERATORS for t in tokens):
        return "ask"
    # Check EVERY token against the risky-command set, not just the first word
    # (e.g. ``env X=1 rm -rf /`` previously slipped through as "allow").
    words = [t for t in tokens if t != "QUOTED"]
    if any(w in _RISKY_TOKENS for w in words):
        return "ask"
    # find is allowlisted for searches but its destructive flags (-delete,
    # -exec/-execdir/-ok) turn it into a wipe - treat them as risky.
    if words and words[0] == "find":
        for w in words:
            if w.startswith("-delete") or w.startswith("-exec") \
               or w.startswith("-execdir") or w.startswith("-ok") \
               or w.startswith("-fprint"):
                return "ask"
    # Read-only commands: exact word-boundary match against the allowlist
    # (``catastrophe --version`` must NOT match the ``cat`` entry).
    if any(c == p or c.startswith(p + " ") for p in _READONLY_PREFIXES):
        return "allow"
    return "ask"


def classify_file_action(rt, path):
    """allow / ask for a file action (reads and writes prompt outside the
    project or the runtime's data dir)."""
    real = os.path.realpath(os.path.expanduser(str(path)))
    for base in (os.path.realpath(PROJECT_DIR), os.path.realpath(rt.data_dir)):
        if real == base or real.startswith(base + os.sep):
            return "allow"
    return "ask"


def request_permission(rt, desc):
    """Resolve a permission request: session cache -> env override -> hook.

    The approved-set and the hook live on the Runtime (`rt.approved` /
    `rt.on_permission`) instead of module globals. Descriptions approved this
    session run again WITHOUT prompting (exact-match, in-memory only - nothing
    survives a restart). Reset any time with rt.approved.clear()."""
    if os.environ.get("ALVA_AUTO_APPROVE") == "1":
        return True
    if desc in rt.approved:
        return True
    if rt.on_permission is not None:
        ok = rt.on_permission(desc)
        if ok:
            rt.approved.add(desc)  # remember for the rest of this session
        return ok
    return False  # headless default: deny
