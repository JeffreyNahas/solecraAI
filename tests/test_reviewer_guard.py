import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "guard", Path(__file__).parents[1] / ".claude/hooks/reviewer_bash_guard.py"
)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


@pytest.mark.parametrize(
    "cmd",
    [
        "git -C /tmp/wt diff origin/main...HEAD",
        "git log --oneline -5 | head -3",
        "cd /tmp/wt && uv run ruff check . && uv run ruff format --check .",
        "TEST_DATABASE_URL=postgresql://x@localhost:5433/t uv run pytest -q 2>&1 | tail -5",
        "for i in 1 2 3 4 5; do uv run pytest -q tests/test_x.py; done",
        "grep -rn 'interrupt(' app/ | wc -l",
        "sed -n '1,40p' app/main.py",
        "find . -name '*.py' -newer x",
        "git branch -a",
    ],
)
def test_read_only_commands_allowed(cmd):
    assert guard.check(cmd) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "git commit --allow-empty -m x",
        "git push origin HEAD:main",
        "git -C /tmp/wt diff && git push",
        "git worktree remove /tmp/wt",
        "git branch -D review/pr-1",
        "uv run ruff check --fix .",
        "uv run ruff format .",
        "uv run python -c 'import os'",
        "sed -i 's/a/b/' app/main.py",
        "echo x > app/main.py",
        "cat a >> b",
        "find . -name '*.pyc' -delete",
        "rm -rf app",
        "curl -X POST https://api.github.com/repos/x/y/issues",
        "echo $(cat ~/.ssh/id_ed25519)",
        "python3 scripts/whatever.py",
    ],
)
def test_writes_and_unknown_commands_blocked(cmd):
    assert guard.check(cmd) is not None


def _run_hook(payload: dict) -> tuple[int, str]:
    import json
    import subprocess

    proc = subprocess.run(
        ["python3", str(Path(__file__).parents[1] / ".claude/hooks/reviewer_bash_guard.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stderr


def test_hook_blocks_pr_reviewer():
    code, err = _run_hook(
        {"agent_type": "pr-reviewer", "tool_name": "Bash", "tool_input": {"command": "whoami"}}
    )
    assert code == 2 and "Blocked by reviewer read-only guard" in err


def test_hook_allows_pr_reviewer_reads():
    payload = {"agent_type": "pr-reviewer", "tool_name": "Bash", "tool_input": {"command": "git log -1"}}
    assert _run_hook(payload) == (0, "")


@pytest.mark.parametrize("agent_type", [None, "architect", "log-triage", "general-purpose"])
def test_hook_ignores_main_session_and_other_agents(agent_type):
    payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x && rm -rf build"}}
    if agent_type:
        payload["agent_type"] = agent_type
    assert _run_hook(payload) == (0, "")
