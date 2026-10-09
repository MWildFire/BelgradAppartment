"""Reject new Ruff diagnostics without conflating them with the inherited lint backlog."""

from __future__ import annotations

import collections
import json
import os
import subprocess
import tempfile
from pathlib import Path


def diagnostics(root: Path, files: list[str]) -> collections.Counter:
    root = root.resolve()
    existing = [name for name in files if (root / name).is_file()]
    if not existing:
        return collections.Counter()
    result = subprocess.run(
        ["ruff", "check", "--output-format=json", *existing],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr)
    return collections.Counter(
        (str(Path(row["filename"]).relative_to(root)), row["code"], row["message"])
        for row in json.loads(result.stdout)
    )


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    base = os.environ.get("LINT_BASE", "75f870c")
    files = subprocess.check_output(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", base, "--", "*.py"],
        cwd=root,
        text=True,
    ).splitlines()
    files += subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "*.py"],
        cwd=root,
        text=True,
    ).splitlines()
    files = sorted(set(files))
    with tempfile.TemporaryDirectory(prefix="otz-lint-") as work:
        before = Path(work)
        for name in ["pyproject.toml", *files]:
            content = subprocess.run(
                ["git", "show", f"{base}:{name}"], cwd=root, capture_output=True, check=False
            )
            if content.returncode == 0:
                target = before / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content.stdout)
        added = diagnostics(root, files) - diagnostics(before, files)
    for (name, code, message), count in sorted(added.items()):
        print(f"{name}: {code} {message} (+{count})")
    print(f"New lint diagnostics: {sum(added.values())}")
    return int(bool(added))


if __name__ == "__main__":
    raise SystemExit(main())
