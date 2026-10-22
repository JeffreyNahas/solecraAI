#!/usr/bin/env python3
"""PreToolUse hook (registered in .claude/settings.json): the pr-reviewer agent may only run
read-only shell commands. Every other agent and the main session are not affected.

Claude Code puts `"agent_type": "pr-reviewer"` in the hook input for that agent's tool calls.
Hooks in an agent's own frontmatter were found NOT to be applied, hence the project-level hook.
Reads the hook JSON on stdin; exit 0 = allow, exit 2 = block (reason on stderr, shown to the agent).
The allowlist is deliberately small: reading code and git history, running lint in check mode and
tests. Anything else (writes, pushes, redirects, command substitution) is blocked.
"""

import json
import re
import shlex
import sys

GUARDED_AGENT = "pr-reviewer"
GIT_READ = {"diff", "log", "show", "status", "rev-parse", "ls-files", "grep", "blame", "branch", "merge-base"}
SIMPLE = {
    "cat",
    "head",
    "tail",
    "ls",
    "grep",
    "rg",
    "wc",
    "sort",
    "uniq",
    "cd",
    "pwd",
    "echo",
    "true",
    "diff",
    "find",
    "sed",
    "pytest",
    "for",
    "do",
    "done",
    "seq",
}
FIND_FORBIDDEN = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
SAFE_REDIRECTS = re.compile(r"\s*(2>&1|2>\s*/dev/null|>\s*/dev/null)")
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _segment_ok(words: list[str]) -> str | None:
    """None if allowed, else the reason."""
    while words and ENV_ASSIGN.match(words[0]):
        words = words[1:]
    if words and words[0] in ("do", "then"):
        words = words[1:]
    if not words:
        return None
    cmd, args = words[0], words[1:]
    if cmd == "git":
        while args and args[0] in ("-C", "-c", "--no-pager"):
            args = args[2:] if args[0] in ("-C", "-c") else args[1:]
        if not args or args[0] not in GIT_READ:
            return f"git {args[0] if args else ''} is not a read-only git command"
        if args[0] == "branch" and any(a in ("-d", "-D", "-m", "-M", "-c", "-C", "-f") for a in args):
            return "git branch may only list branches"
        return None
    if cmd == "uv":
        if args[:2] == ["run", "pytest"]:
            return None
        if args[:3] == ["run", "ruff", "check"] and not any(a.startswith("--fix") for a in args):
            return None
        if args[:3] == ["run", "ruff", "format"] and "--check" in args:
            return None
        return "only `uv run pytest`, `uv run ruff check` (no --fix), `uv run ruff format --check` allowed"
    if cmd == "sed":
        if "-n" not in args or any(a.startswith("-i") or a == "--in-place" for a in args):
            return "sed is allowed only as `sed -n ...` without -i"
        return None
    if cmd == "find" and FIND_FORBIDDEN & set(args):
        return "find may not delete or execute"
    if cmd in SIMPLE:
        return None
    return f"`{cmd}` is not on the reviewer's read-only allowlist"


def check(command: str) -> str | None:
    """None if the whole command is allowed, else the reason it is blocked."""
    if "`" in command or "$(" in command or "<(" in command or ">(" in command:
        return "command substitution is not allowed"
    stripped = SAFE_REDIRECTS.sub(" ", command)
    if ">" in stripped:
        return "output redirection is not allowed (read-only reviewer)"
    for segment in re.split(r"&&|\|\||;|\||\n", stripped):
        try:
            words = shlex.split(segment)
        except ValueError:
            return "could not parse the command"
        reason = _segment_ok(words)
        if reason:
            return reason
    return None


def main() -> int:
    data = json.load(sys.stdin)
    if data.get("agent_type") != GUARDED_AGENT or data.get("tool_name") != "Bash":
        return 0
    reason = check((data.get("tool_input") or {}).get("command", ""))
    if reason:
        print(f"Blocked by reviewer read-only guard: {reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
