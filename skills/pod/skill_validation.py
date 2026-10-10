"""Deterministic validation and parity checks for the Pod skill bundle."""

from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from .bundle import (BUNDLE_FILES, BUNDLE_TEXT, MAX_SKILL, bundle_root, canonical,
                     frontmatter, installed_files, version)
from .errors import PodError

FRONTMATTER_FIELDS = {"name", "description", "license", "compatibility", "metadata"}
REQUIRED_FRONTMATTER = {"name", "description"}
FORBIDDEN_FRONTMATTER = ("context", "model", "effort", "agent", "allowed-tools",
                         "disable-model-invocation", "user-invocable")
REFERENCES = tuple(name for name in BUNDLE_FILES if name.startswith("references/"))
FORBIDDEN_FORMS = (
    (re.compile(r"python3?\s+[^\n`]*scripts/pod\.py"), "a long Python skill-helper command"),
    (re.compile(r"python3?\s+-m\s+pod\b"), "a `python -m pod` invocation"),
    (re.compile(r"`pod\.internal"), "a bare `pod.internal` reference"),
    (re.compile(r"npx\s+(?:--yes\s+)?skills(?:@[^\s]+)?\s+add\b"),
     "a manual skills-CLI installation command"),
)
REQUIRED_FORMS = ("pod config --json", "pod internal <op> --input FILE", "~/.local/bin/pod",
                  "https://raw.githubusercontent.com/j3w1/pod/main/install.sh")
MAX_SKILL_LINES = 500
MAX_SKILL_WORDS = 400
MAX_REFERENCE_WORDS = 450
AUTHORING_REFERENCES = ("references/seal-template.md", "references/pes-template.md",
                       "references/eel-template.md")
EXECUTION_REFERENCES = tuple(name for name in REFERENCES if name not in AUTHORING_REFERENCES)
MAX_EXECUTION_WORDS = 2000
MAX_AUTHORING_WORDS = 1350
MAX_DELIVERY_WORDS = 2000
SECTIONS = ("Roles", "Done when", "Boundaries", "Helpers", "Read when needed")
DELIVERY_FILES = ("SKILL.md", "references/issue-intake.md", "references/planning.md",
                  "references/orca-boundary.md", "references/models.md",
                  "references/verification.md", "references/governor.md")
SOURCE_PREFIX = "https://github.com/j3w1/pod"


def _skill_text(root: Path) -> str:
    text = (root / "SKILL.md").read_text(encoding="utf-8")
    if len(text.encode()) > MAX_SKILL:
        raise PodError("invalid_skill", "SKILL.md is oversized")
    if len(text.splitlines()) > MAX_SKILL_LINES:
        raise PodError("invalid_skill", "SKILL.md exceeds its line budget")
    return text


def _check_metadata(metadata: dict) -> None:
    present = set(metadata)
    if present - FRONTMATTER_FIELDS or REQUIRED_FRONTMATTER - present:
        raise PodError("invalid_skill", "SKILL.md frontmatter has missing or unsupported fields")
    for name in FORBIDDEN_FRONTMATTER:
        if name in metadata:
            raise PodError("invalid_skill", f"SKILL.md must not set {name}")
    if metadata["name"] != "pod":
        raise PodError("invalid_skill", "Skill name must be pod")
    description = metadata["description"]
    if (not isinstance(description, str) or len(description) > 250
            or "Use when" not in description or "\n" in description):
        raise PodError("invalid_skill", "Skill description must contain Use when within 250 characters")
    compatibility = metadata.get("compatibility")
    if compatibility is not None and (not isinstance(compatibility, str) or len(compatibility) > 500
                                      or not compatibility.strip() or "\n" in compatibility):
        raise PodError("invalid_skill", "Skill compatibility must be one plain line")
    license_name = metadata.get("license")
    if license_name is not None and (not isinstance(license_name, str) or len(license_name) > 200):
        raise PodError("invalid_skill", "Skill license is invalid")
    declared = metadata.get("metadata")
    if declared is None:
        raise PodError("invalid_skill", "Skill metadata must declare its source")
    if not isinstance(declared, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                             for key, value in declared.items()):
        raise PodError("invalid_skill", "Skill metadata must map strings to strings")
    if "version" in declared:
        raise PodError("invalid_skill", "The version lives only in VERSION, not in SKILL.md")
    if not str(declared.get("source", "")).startswith(SOURCE_PREFIX):
        raise PodError("invalid_skill", "Skill metadata source must name this repository")


def _check_version(root: Path) -> None:
    try:
        text = (root / "VERSION").read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise PodError("invalid_skill", "The bundle VERSION is unreadable") from exc
    if re.fullmatch(r"\d+\.\d+\.\d+\n", text) is None:
        raise PodError("invalid_skill", "VERSION must hold one MAJOR.MINOR.PATCH line")


def _cross_reference(data: str) -> bool:
    """Catch inline and definition links, including extensionless reference targets."""
    targets = re.findall(r"\]\(\s*<?([^\s)>]+)", data)
    targets += re.findall(r"(?m)^\s{0,3}\[[^]\n]+\]:\s*<?([^\s>]+)", data)
    stems = {Path(name).stem for name in REFERENCES}
    for target in targets:
        path = unquote(urlsplit(target).path).rstrip("/")
        basename = path.rsplit("/", 1)[-1]
        if basename.removesuffix(".md") in stems or path.endswith(".md"):
            return True
    return False


def _check_body(root: Path, text: str, body: str) -> dict[str, int]:
    if len(text.split()) > MAX_SKILL_WORDS:
        raise PodError("invalid_skill", "SKILL.md exceeds its word budget")
    if tuple(re.findall(r"^## (.+)$", body, re.M)) != SECTIONS:
        raise PodError("invalid_skill", "SKILL.md must have exactly the five router sections")
    for pattern, described in FORBIDDEN_FORMS:
        if pattern.search(body):
            raise PodError("invalid_skill", f"SKILL.md must not instruct {described}")
    for form in REQUIRED_FORMS:
        if form not in body:
            raise PodError("invalid_skill", f"SKILL.md must show the {form} helper form")
    links = re.findall(r"\]\(((?:references|scripts)/[^)]+)\)", body)
    if set(links) != set(REFERENCES) or len(links) != len(REFERENCES):
        raise PodError("invalid_skill", "SKILL.md must link every bundled reference exactly once and no other")
    router = body.split("## Read when needed\n", 1)[-1]
    triggered = re.findall(r"^- ([^:\n]+): \[[^\]\n]+\]\((references/[^)\n]+)\)\.$", router, re.M)
    if ([link for _, link in triggered] != links
            or len({trigger.casefold() for trigger, _ in triggered}) != len(triggered)):
        raise PodError("invalid_skill", "Every reference link needs its own read-when trigger")
    for name in links:
        if name not in BUNDLE_FILES:
            raise PodError("invalid_skill", f"SKILL.md links {name}, which is not in the bundle")
    counts = {"SKILL.md": len(text.split())}
    for name in REFERENCES:
        data = (root / name).read_text(encoding="utf-8")
        if not data.startswith("# ") or len(data.encode()) > MAX_SKILL:
            raise PodError("invalid_skill", f"{name} is missing a bounded title")
        words = len(data.split())
        counts[name] = words
        if words > MAX_REFERENCE_WORDS:
            raise PodError("invalid_skill", f"{name} exceeds its word budget")
        if _cross_reference(data):
            raise PodError("invalid_skill", f"{name} must not link another reference")
        for pattern, described in FORBIDDEN_FORMS:
            if pattern.search(data):
                raise PodError("invalid_skill", f"{name} must not instruct {described}")
    counts["references_combined"] = sum(counts[name] for name in REFERENCES)
    counts["execution_references"] = sum(counts[name] for name in EXECUTION_REFERENCES)
    counts["authoring_templates"] = sum(counts[name] for name in AUTHORING_REFERENCES)
    counts["delivery_path"] = sum(counts[name] for name in DELIVERY_FILES)
    counts["installed_guidance"] = counts["SKILL.md"] + counts["references_combined"]
    if counts["execution_references"] > MAX_EXECUTION_WORDS:
        raise PodError("invalid_skill", "Skill execution references exceed their combined word budget")
    if counts["authoring_templates"] > MAX_AUTHORING_WORDS:
        raise PodError("invalid_skill", "Skill authoring templates exceed their combined word budget")
    if counts["delivery_path"] > MAX_DELIVERY_WORDS:
        raise PodError("invalid_skill", "Skill delivery path exceeds its word budget")
    return counts


def _check_launcher(root: Path) -> None:
    text = (root / "scripts" / "pod.py").read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise PodError("invalid_skill", "The bundled launcher does not parse") from exc
    for node in tree.body:
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for name in names:
            if name.split(".")[0] in ("pod", "yaml"):
                raise PodError("invalid_skill",
                               "The launcher must not import pod or yaml before its prerequisite check")


def _check_interface(root: Path) -> None:
    import yaml

    data = yaml.safe_load((root / "agents" / "openai.yaml").read_text(encoding="utf-8"))
    interface = data.get("interface") if isinstance(data, dict) else None
    if not isinstance(interface, dict) or interface.get("display_name") != "Pod":
        raise PodError("invalid_skill", "agents/openai.yaml must declare the Pod interface")
    short = interface.get("short_description")
    if not isinstance(short, str) or not short.strip() or len(short) > 100:
        raise PodError("invalid_skill", "agents/openai.yaml short_description is invalid")
    if set(data) != {"interface"} or set(interface) - {"display_name", "short_description"}:
        raise PodError("invalid_skill", "agents/openai.yaml has unsupported fields")


def _repository_version_link(root: Path, path: Path) -> bool:
    """In the repository, the bundle's VERSION is a link to the one root VERSION.

    The skills CLI copies the file it points at, so every installed copy carries a regular
    VERSION. No other link is allowed anywhere in the skill tree.
    """
    return (path.parent == root and path.name == "VERSION"
            and os.readlink(path) == "../../VERSION" and path.resolve().is_file())


def validate_skill(root: Path) -> dict:
    if root.is_symlink() or not root.is_dir():
        raise PodError("invalid_skill", "Skill root must be a regular directory")
    if root.name != "pod":
        raise PodError("invalid_skill", "Skill directory must be named pod")
    for path in root.rglob("*"):
        if path.is_symlink() and _repository_version_link(root, path):
            continue
        if path.is_symlink() or (path.exists() and not path.is_file() and not path.is_dir()):
            raise PodError("invalid_skill", "Skill tree contains a redirected or special node")
    actual = installed_files(root)
    if actual != set(BUNDLE_FILES):
        missing = sorted(set(BUNDLE_FILES) - actual)
        extra = sorted(actual - set(BUNDLE_FILES))
        raise PodError("invalid_skill",
                       f"Bundle inventory mismatch; missing {missing}, unexpected {extra}")
    _check_version(root)
    text = _skill_text(root)
    parsed = frontmatter(text)
    _check_metadata(parsed["metadata"])
    counts = _check_body(root, text, parsed["body"])
    _check_launcher(root)
    _check_interface(root)
    from .catalog import load as load_catalog
    load_catalog(root / "catalog.json")
    _check_observations(root)
    return {"status": "valid", "name": "pod", "version": version(), "files": sorted(actual),
            "word_counts": counts}


def _check_observations(root: Path) -> None:
    """The bundled observation fallback is a valid, bounded snapshot."""
    from .observations import validate as validate_observations
    raw = (root / "observations.json").read_bytes()
    if len(raw) > MAX_SKILL:
        raise PodError("invalid_skill", "Bundled observations exceed the bundle text limit")
    try:
        validate_observations(json.loads(raw))
    except ValueError as exc:
        raise PodError("invalid_skill", "Bundled observations are not valid JSON") from exc


def compare_bundle(actual: dict[str, bytes]) -> list[str]:
    source = canonical()
    differences = []
    for name in sorted(set(source) | set(actual)):
        if name not in actual:
            differences.append(f"missing {name}")
        elif name not in source:
            differences.append(f"unexpected {name}")
        elif actual[name] != source[name]:
            differences.append(f"differs {name}")
    return differences


def validate_installed(path: Path) -> dict:
    """Prove a placed copy carries the exact bundle bytes."""
    if not path.is_dir():
        raise PodError("bundle_parity", "Installed skill path is not a directory")
    entries = {}
    for name in installed_files(path):
        target = path / name
        if target.is_symlink() or not target.is_file():
            raise PodError("bundle_parity", f"Installed entry {name} is redirected")
        entries[name] = target.read_bytes()
    differences = compare_bundle(entries)
    if differences:
        raise PodError("bundle_parity", "Installed copy differs from the bundle: " + "; ".join(differences))
    return {"status": "valid", "installed": str(path), "files": sorted(entries)}


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
        result = validate_skill(args.skill if args.skill is not None else bundle_root())
    except (OSError, UnicodeError, PodError) as exc:
        print(f"Skill validation failed: {exc}")
        return 1
    print("Skill is valid!")
    for name, count in result["word_counts"].items():
        print(f"{name}: {count} words")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
