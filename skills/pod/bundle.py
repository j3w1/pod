"""The Pod skill bundle: the package directory itself, with nothing listed by hand."""

from __future__ import annotations

from importlib.resources import files as _package_files
from pathlib import Path

from . import installer, yamlsubset
from .errors import PodError

MAX_TEXT = 64 * 1024


def bundle_root() -> Path:
    """The running bundle directory: the importable package and the skill are one tree."""
    return Path(str(_package_files("pod")))


def version() -> str:
    from . import __version__

    return __version__


def files(root: Path | None = None) -> list[str]:
    """Relative names of the bundle's files, derived from the directory."""
    return installer.bundle_files(bundle_root() if root is None else root)


def digest(root: Path | None = None) -> str | None:
    """The digest the installer records for a placed copy; None when the directory is not a bundle."""
    return installer.optional_digest(bundle_root() if root is None else root)


def frontmatter(text: str) -> dict:
    """SKILL.md front matter as plain values and one-level mappings, and the body after it."""
    parts = text.split("---\n", 2)
    if not text.startswith("---\n") or len(parts) != 3:
        raise PodError("invalid_skill", "SKILL.md front matter is missing or not closed")
    try:
        metadata = {key: yamlsubset.scalar(block.value) if block.value is not None else yamlsubset.mapping(block)
                    for key, block in yamlsubset.parse(parts[1].rstrip("\n")).items()}
    except ValueError as exc:
        raise PodError("invalid_skill", f"SKILL.md front matter must be plain one-line values ({exc})") from exc
    return {"metadata": metadata, "body": parts[2]}
