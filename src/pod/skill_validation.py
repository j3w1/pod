"""Deterministic validation for the repository's canonical Pod skill."""

from __future__ import annotations

import argparse
from pathlib import Path
import re

import yaml

from .errors import PodError
from .setup import SKILL_FILES


def validate_skill(root: Path) -> dict:
    if root.is_symlink() or not root.is_dir():
        raise PodError("invalid_skill", "Skill root must be a regular directory")
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink() or (path.exists() and not path.is_file() and not path.is_dir()):
            raise PodError("invalid_skill", "Skill tree contains a redirected or special node")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != set(SKILL_FILES):
        raise PodError("invalid_skill", "Canonical skill file inventory is incomplete or has extras")
    skill = (root / "SKILL.md").read_text(encoding="utf-8")
    if len(skill.encode()) > 64 * 1024 or not skill.startswith("---\n"):
        raise PodError("invalid_skill", "SKILL.md frontmatter is missing or oversized")
    parts = skill.split("---\n", 2)
    if len(parts) != 3:
        raise PodError("invalid_skill", "SKILL.md frontmatter is not bounded")
    try:
        metadata = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        raise PodError("invalid_skill", "SKILL.md frontmatter is invalid YAML") from exc
    if (not isinstance(metadata, dict) or set(metadata) != {"name", "description"}
            or metadata.get("name") != "pod"
            or not isinstance(metadata.get("description"), str)
            or not metadata["description"].strip() or len(metadata["description"]) > 1024):
        raise PodError("invalid_skill", "Skill name and description are invalid")
    references = set(re.findall(r"\]\((references/[^)]+\.md)\)", parts[2]))
    if references != {name for name in SKILL_FILES if name.startswith("references/")}:
        raise PodError("invalid_skill", "SKILL.md must link every canonical reference exactly")
    for name in references:
        data = (root / name).read_text(encoding="utf-8")
        if not data.startswith("# ") or len(data.encode()) > 64 * 1024:
            raise PodError("invalid_skill", f"{name} is missing a bounded title")
    return {"status": "valid", "name": "pod", "files": sorted(actual)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pod.skill_validation")
    parser.add_argument("skill", type=Path)
    args = parser.parse_args(argv)
    try:
        validate_skill(args.skill)
    except (OSError, UnicodeError, PodError) as exc:
        print(f"Skill validation failed: {exc}")
        return 1
    print("Skill is valid!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
