"""Read or bump the project version in pyproject.toml."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
VERSION_LINE = re.compile(
    r'^(version\s*=\s*")(\d+)\.(\d+)\.(\d+)(")\s*$',
    re.MULTILINE,
)


def project_span(text: str) -> tuple[int, int]:
    marker = "[project]"
    start = text.find(marker)
    if start == -1:
        raise SystemExit("pyproject.toml has no [project] table")
    body = start + len(marker)
    next_table = re.search(r"(?m)^\[", text[body:])
    end = body + next_table.start() if next_table else len(text)
    return body, end


def find_version(text: str) -> re.Match[str]:
    body, end = project_span(text)
    match = VERSION_LINE.search(text, body, end)
    if match is None:
        raise SystemExit("pyproject.toml [project] table has no version")
    return match


def current_version(text: str) -> str:
    match = find_version(text)
    return f"{match.group(2)}.{match.group(3)}.{match.group(4)}"


def bump_patch(text: str) -> tuple[str, str]:
    match = find_version(text)
    major = int(match.group(2))
    minor = int(match.group(3))
    patch = int(match.group(4))
    new = f"{major}.{minor}.{patch + 1}"
    replacement = f"{match.group(1)}{new}{match.group(5)}"
    updated = text[: match.start()] + replacement + text[match.end() :]
    return updated, new


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read or bump the project patch version.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--print",
        action="store_true",
        dest="print_only",
        help="Print the current version.",
    )
    group.add_argument("--bump", action="store_true", help="Raise the patch number and print it.")
    args = parser.parse_args(argv)

    text = PYPROJECT.read_text(encoding="utf-8")
    if args.print_only:
        print(current_version(text))
        return 0

    updated, new = bump_patch(text)
    PYPROJECT.write_text(updated, encoding="utf-8", newline="\n")
    print(new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
