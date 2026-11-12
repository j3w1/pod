"""Validate the Pod skill bundle, and prove a placed copy carries the same bytes."""

from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path
import re

from . import bundle, installer, yamlsubset
from .errors import PodError

FRONTMATTER_FIELDS = {"name", "description", "license", "compatibility", "metadata"}
# The files the installer, the launcher and an agent need before anything else can be checked.
ENTRY_FILES = ("SKILL.md", "VERSION", "__init__.py", "__main__.py", "installer.py", "scripts/pod.py",
               "agents/openai.yaml")
SOURCE_PREFIX = "https://github.com/j3w1/pod"
# A bundled file SKILL.md points at, written as a link or as a plain path.
NAMED = re.compile(r"(?<![\w/.-])((?:references|scripts)/[A-Za-z0-9_/-]+(?:\.[A-Za-z0-9]+)+)")


def _refuse(message: str) -> PodError:
    return PodError("invalid_skill", message)


def _check_tree(root: Path) -> list[str]:
    if root.is_symlink() or not root.is_dir() or root.name != "pod":
        raise _refuse("The skill root must be a regular directory named pod")
    for path in root.rglob("*"):
        if "__pycache__" in path.relative_to(root).parts:
            continue
        # In the repository the bundle's VERSION links to the one root VERSION; the skills CLI
        # copies what it points at, so a placed copy carries a regular file. No other link is allowed.
        version_link = (path.parent == root and path.name == "VERSION" and path.is_symlink()
                        and os.readlink(path) == "../../VERSION" and path.resolve().is_file())
        if (path.is_symlink() and not version_link) or not (path.is_file() or path.is_dir()):
            raise _refuse(f"{path.relative_to(root)} is a link or a special file")
        if path.is_file() and path.name != "VERSION" and path.suffix not in installer.BUNDLE_SUFFIXES + (".pyc", ".pyo"):
            raise _refuse(f"{path.relative_to(root)} is not a kind of file the bundle carries")
    names = bundle.files(root)
    missing = [name for name in ENTRY_FILES if name not in names]
    if missing:
        raise _refuse("The bundle is missing " + ", ".join(missing))
    return names


def _check_metadata(metadata: dict) -> None:
    if set(metadata) - FRONTMATTER_FIELDS or not {"name", "description"} <= set(metadata):
        raise _refuse("SKILL.md front matter has missing or unsupported fields")
    if metadata["name"] != "pod":
        raise _refuse("The skill name must be pod")
    description = metadata["description"]
    if not isinstance(description, str) or len(description) > 250 or "Use when" not in description:
        raise _refuse("The skill description must say Use when within 250 characters")
    for name, limit in (("compatibility", 500), ("license", 200)):
        value = metadata.get(name)
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > limit):
            raise _refuse(f"The skill {name} must be one plain line")
    declared = metadata.get("metadata")
    if not isinstance(declared, dict) or any(not isinstance(value, str) for value in declared.values()):
        raise _refuse("The skill metadata must declare its source")
    if "version" in declared:
        raise _refuse("The version lives only in VERSION, not in SKILL.md")
    if not str(declared.get("source", "")).startswith(SOURCE_PREFIX):
        raise _refuse("The skill metadata source must name this repository")


def _check_text(root: Path, names: list[str], body: str) -> None:
    """Every file SKILL.md names exists, and every reference is reachable from it."""
    named = set(NAMED.findall(body))
    for name in sorted(named):
        if name not in names:
            raise _refuse(f"SKILL.md names {name}, which is not in the bundle")
    for name in names:
        if name.endswith(".md") and len((root / name).read_bytes()) > bundle.MAX_TEXT:
            raise _refuse(f"{name} is oversized")
        if name.startswith("references/"):
            if name not in named:
                raise _refuse(f"{name} is not named in SKILL.md")
            if not (root / name).read_text(encoding="utf-8").startswith("# "):
                raise _refuse(f"{name} is missing its title")


def _check_python(root: Path, names: list[str]) -> None:
    for name in names:
        if not name.endswith(".py"):
            continue
        try:
            tree = ast.parse((root / name).read_text(encoding="utf-8"))
        except (SyntaxError, ValueError) as exc:
            raise _refuse(f"{name} does not parse") from exc
        if name == "scripts/pod.py":
            imported = [alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names]
            imported += [node.module or "" for node in tree.body if isinstance(node, ast.ImportFrom)]
            if any(module.split(".")[0] == "pod" for module in imported):
                raise _refuse("The launcher must not import pod before its prerequisite check")


def _check_interface(root: Path) -> None:
    try:
        blocks = yamlsubset.parse((root / "agents/openai.yaml").read_text(encoding="utf-8"))
        interface = yamlsubset.mapping(blocks["interface"]) if set(blocks) == {"interface"} else None
    except ValueError as exc:
        raise _refuse(f"agents/openai.yaml is not readable ({exc})") from exc
    if interface is None or set(interface) != {"display_name", "short_description"} or interface["display_name"] != "Pod":
        raise _refuse("agents/openai.yaml must declare exactly the Pod interface")
    short = interface["short_description"]
    if not isinstance(short, str) or not short.strip() or len(short) > 100:
        raise _refuse("agents/openai.yaml short_description is invalid")


def validate_skill(root: Path) -> dict:
    names = _check_tree(root)
    try:
        version = (root / "VERSION").read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise _refuse("The bundle VERSION is unreadable") from exc
    if re.fullmatch(r"\d+\.\d+\.\d+\n", version) is None:
        raise _refuse("VERSION must hold one MAJOR.MINOR.PATCH line")
    text = (root / "SKILL.md").read_text(encoding="utf-8")
    parsed = bundle.frontmatter(text)
    _check_metadata(parsed["metadata"])
    _check_text(root, names, parsed["body"])
    _check_python(root, names)
    _check_interface(root)
    return {"status": "valid", "name": "pod", "version": version.strip(), "files": names,
            "digest": installer.bundle_digest(root)}


def validate_installed(path: Path, source: Path | None = None) -> dict:
    """Prove a placed copy carries the exact bundle bytes, naming what differs."""
    source = bundle.bundle_root() if source is None else source
    if not path.is_dir() or path.is_symlink():
        raise PodError("bundle_parity", "The installed skill path is not a regular directory")
    placed, expected = set(bundle.files(path)), set(bundle.files(source))
    differences = [f"missing {name}" for name in sorted(expected - placed)]
    differences += [f"unexpected {name}" for name in sorted(placed - expected)]
    for name in sorted(placed & expected):
        if (path / name).is_symlink():
            differences.append(f"redirected {name}")
        elif (path / name).read_bytes() != (source / name).read_bytes():
            differences.append(f"differs {name}")
    if differences:
        raise PodError("bundle_parity", "The installed copy differs from the bundle: " + "; ".join(differences))
    return {"status": "valid", "installed": str(path), "files": sorted(placed), "digest": bundle.digest(path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pod.skill_validation")
    parser.add_argument("skill", type=Path, nargs="?", default=None)
    parser.add_argument("--installed", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        if args.installed is not None:
            validate_installed(args.installed)
            print("Installed skill matches the bundle!")
            return 0
        result = validate_skill(args.skill if args.skill is not None else bundle.bundle_root())
    except (OSError, UnicodeError, PodError, installer.InstallError) as exc:
        print(f"Skill validation failed: {exc}")
        return 1
    print(f"Skill is valid! Pod {result['version']}, {len(result['files'])} files, digest {result['digest'][:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
