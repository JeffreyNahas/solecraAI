"""Fail if code mapped in docs/doc-map.toml changed without its doc.

    uv run python -m scripts.check_docs [BASE_REF]      # default: origin/main

Skip for a PR by putting a line `docs: none` in any of its commit messages.
"""

import fnmatch
import subprocess
import sys
import tomllib
from pathlib import Path

MAP_FILE = Path("docs/doc-map.toml")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def missing_docs(changed: set[str], mapping: list[dict]) -> dict[str, list[str]]:
    """{doc: [changed code files]} for every doc that should have changed but didn't."""
    result: dict[str, list[str]] = {}
    for entry in mapping:
        doc = entry["doc"]
        hits = sorted(f for f in changed if any(fnmatch.fnmatch(f, p) for p in entry["code"]))
        if hits and doc not in changed:
            result[doc] = hits
    return result


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    changed = set(git("diff", "--name-only", f"{base}...HEAD").split())
    messages = git("log", f"{base}..HEAD", "--format=%B")
    if any(line.strip().lower() == "docs: none" for line in messages.splitlines()):
        print("docs check skipped (`docs: none` in a commit message)")
        return 0

    mapping = tomllib.loads(MAP_FILE.read_text())["map"]
    missing = missing_docs(changed, mapping)
    if not missing:
        print("docs check passed")
        return 0
    print("These docs need updating for the code changed in this branch:\n")
    for doc, files in missing.items():
        print(f"  {doc}\n    because of: {', '.join(files)}")
    print(
        "\nRun /update-docs in Claude Code, or add `docs: none` to a commit message if no update is needed."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
