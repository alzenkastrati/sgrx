#!/usr/bin/env python3
"""Install the SGRX Agent Skill into supported user-level discovery paths."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Iterable


TARGETS = {
    "shared": Path(".agents") / "skills" / "sgrx",
    "codex": Path(".codex") / "skills" / "sgrx",
    "claude": Path(".claude") / "skills" / "sgrx",
    "cline": Path(".cline") / "skills" / "sgrx",
}

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store")


def skill_source() -> Path:
    return Path(__file__).resolve().parents[1]


def install_skill(
    source: Path,
    home: Path,
    targets: Iterable[str],
    *,
    dry_run: bool = False,
) -> list[tuple[str, Path, str]]:
    source = source.expanduser().resolve()
    home = home.expanduser().resolve()
    if not (source / "SKILL.md").is_file():
        raise ValueError(f"not an Agent Skill directory: {source}")

    results: list[tuple[str, Path, str]] = []
    for target in dict.fromkeys(targets):
        if target not in TARGETS:
            raise ValueError(f"unsupported target: {target}")
        destination = (home / TARGETS[target]).resolve()
        if destination == source:
            results.append((target, destination, "already-installed"))
            continue
        if source in destination.parents:
            raise ValueError(f"refusing to copy the skill inside itself: {destination}")
        if dry_run:
            results.append((target, destination, "planned"))
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, dirs_exist_ok=True, ignore=_IGNORE)
        results.append((target, destination, "installed"))
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install SGRX for Codex, Claude Code, Cline, and shared Agent Skills clients."
    )
    parser.add_argument(
        "--target",
        action="append",
        choices=tuple(TARGETS),
        help="Install only this target; repeat for several. The default installs every target.",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help="Override the user home directory used for installation.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show destinations without copying files.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    targets = args.target or list(TARGETS)
    for target, destination, status in install_skill(
        skill_source(), args.home, targets, dry_run=args.dry_run
    ):
        print(f"{status}: {target} -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
