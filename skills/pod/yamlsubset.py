"""The small part of YAML that Pod's two preference files use, read with the standard library.

A file is top-level `key: value` lines. A key with no value owns the lines that follow it,
which are either `name: value` pairs or `- value` items. Anything else under a key is kept as
raw lines and never interpreted, so a file can carry keys this version does not use. Reading
never rewrites, and `Block.start`/`Block.end` let an editor change one line and leave every
other byte alone.
"""

from dataclasses import dataclass, field
import re

_KEY = re.compile(r"""^(?P<key>"[^"]*"|'[^']*'|[^\s#:'"\-\[\]{},&*!|>%@`][^:#]*?)\s*:(?:\s+(?P<value>.*))?$""")


@dataclass
class Block:
    key: str
    value: str | None          # text after the colon, comments removed; None when the key owns lines
    start: int                 # index of the key line
    end: int                   # index one past the block's last line
    lines: list[tuple[int, str]] = field(default_factory=list)  # (index, text) of owned lines


def _strip_comment(text: str) -> str:
    quote = None
    for position, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "#" and (position == 0 or text[position - 1] in " \t"):
            return text[:position].rstrip()
    return text.rstrip()


def _unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def scalar(text: str | None):
    """One plain or quoted value. Structures written inline are refused, not guessed."""
    if text is None:
        return None
    text = text.strip()
    if text in ("", "null", "~", "Null", "NULL"):
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    if text[0] in "[{&*!|>%@`":
        raise ValueError(f"unsupported value {text!r}")
    if text in ("true", "True"):
        return True
    if text in ("false", "False"):
        return False
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def parse(text: str) -> dict[str, Block]:
    """Top-level blocks by key. Raises ValueError naming the line for anything outside the subset."""
    blocks: dict[str, Block] = {}
    current: Block | None = None
    lines = text.split("\n")
    for index, raw in enumerate(lines):
        if "\t" in raw[:len(raw) - len(raw.lstrip())]:
            raise ValueError(f"line {index + 1}: tabs cannot indent")
        content = _strip_comment(raw)
        if not content.strip():
            continue
        if index == 0 and content.strip() == "---":
            continue
        owned = raw[0] in " " or content.startswith("- ") or content == "-"
        if owned:
            if current is None or current.value is not None:
                raise ValueError(f"line {index + 1}: unexpected indented line")
            current.lines.append((index, content))
            current.end = index + 1
            continue
        match = _KEY.match(content)
        if not match:
            raise ValueError(f"line {index + 1}: expected `key: value`")
        key = _unquote(match["key"].strip())
        if key in blocks:
            raise ValueError(f"line {index + 1}: {key} appears twice")
        value = match["value"]
        current = Block(key, value if value not in (None, "") else None, index, index + 1)
        blocks[key] = current
    return blocks


def mapping(block: Block) -> dict:
    """The block's `name: value` pairs, one level deep."""
    if block.value is not None:
        if block.value.strip() in ("{}", "null", "~"):
            return {}
        raise ValueError(f"line {block.start + 1}: {block.key} must own indented `name: value` lines")
    result: dict = {}
    for index, content in block.lines:
        match = _KEY.match(content.strip())
        if not match or content.lstrip().startswith("- "):
            raise ValueError(f"line {index + 1}: expected `name: value` under {block.key}")
        name = _unquote(match["key"].strip())
        if name in result:
            raise ValueError(f"line {index + 1}: {name} appears twice")
        result[name] = scalar(match["value"])
    return result


def sequence(block: Block) -> list:
    """The block's `- value` items."""
    if block.value is not None:
        if block.value.strip() in ("[]", "null", "~"):
            return []
        raise ValueError(f"line {block.start + 1}: {block.key} must own `- value` lines")
    result = []
    for index, content in block.lines:
        item = content.strip()
        if not item.startswith("- "):
            raise ValueError(f"line {index + 1}: expected `- value` under {block.key}")
        result.append(scalar(item[2:]))
    return result
