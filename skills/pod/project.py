"""The project's own rules, read from the target branch so a change cannot relax its own gate."""

from pathlib import Path

from . import gitio, yamlsubset

FILE = ".pod/config.yaml"
SCHEMA = "pod-project/v1"
METHODS = ("merge", "squash", "rebase")
STRICT = {"review": True, "final_tree_review": False, "merge_method": "merge", "required_checks": None}


def rules(cwd: Path, ref: str) -> dict:
    """Review requirement, merge method and an optional list of required checks.

    No file means the project set no rule. A file this version cannot read is not treated as
    permission: review is then required, and the problem is reported.
    """
    text = gitio.show(cwd, ref, FILE)
    if text is None:
        return {**STRICT, "review": False, "source": "none"}
    try:
        blocks = yamlsubset.parse(text)
        value = {name: yamlsubset.scalar(block.value) for name, block in blocks.items() if name != "required_checks"}
        if value.get("schema") != SCHEMA:
            raise ValueError(f"schema is {value.get('schema')!r}, expected {SCHEMA}")
        review = value.get("review", "none")
        method = value.get("merge_method", "merge")
        final = value.get("final_tree_review", False)
        if review not in ("required", "none") or method not in METHODS or not isinstance(final, bool):
            raise ValueError("review must be required or none, merge_method merge, squash or rebase, "
                             "final_tree_review true or false")
        required = yamlsubset.sequence(blocks["required_checks"]) if "required_checks" in blocks else None
        if required is not None and any(not isinstance(name, str) for name in required):
            raise ValueError("required_checks must list check names")
        return {"review": review == "required", "final_tree_review": final, "merge_method": method,
                "required_checks": required or None, "source": f"{FILE} at {ref[:12]}"}
    except ValueError as exc:
        return {**STRICT, "source": f"{FILE} at {ref[:12]}", "problem": str(exc)}
