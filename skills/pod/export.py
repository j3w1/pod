"""Export the records written by 0.21 to plain JSON, for audit. The source is only ever read.

Each 0.21 objective is a directory named by a 64-hex record key. Its JSON files are either
plain, plain with values moved out to digest-addressed chunks, or an envelope whose root is a
tree of pages. One exported document per directory inlines all of that, so it needs no other
file to read. Nothing here is used by coordination, and the new core never reads this format.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from . import paths
from .errors import PodError

SCHEMA = "pod-export/v1"
ENVELOPE = "pod-history-record/v1"
PAGE = "pod-revision-page/v1"
MARKER = "$pod_history"
UNRESOLVED = "$pod_export_unresolved"
RECORD_KEY = re.compile(r"[a-f0-9]{64}")
CHUNKS = Path("history") / "sha256"
FAULTS = (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError)
ATTEMPTS = 3  # a live 0.21 writer may replace a record and reclaim its pages while it is read


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("not a regular file")
        return stream.read()


class _Record:
    """Resolves one objective directory. Every chunk is checked against its address."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.problems: list[str] = []
        self.used: set[str] = set()

    def _chunk(self, address) -> bytes:
        if not isinstance(address, str) or not RECORD_KEY.fullmatch(address):
            raise ValueError("chunk address is not a SHA-256 digest")
        raw = _read(self.directory / CHUNKS / (address + ".json"))
        if _sha(raw) != address:
            raise ValueError(f"chunk {address[:12]} differs from its address")
        self.used.add(address)
        return raw

    def _unresolved(self, stored, where: str, exc: Exception):
        reason = f"{type(exc).__name__}: {exc}"
        self.problems.append(f"{where}: {reason}")
        return {UNRESOLVED: {"reason": reason, "stored": stored}}

    def _value(self, marker):
        """A value stored apart from its record, as one or more parts."""
        reference = marker[MARKER]
        parts = []
        for address in reference["parts"]:
            raw = self._chunk(address)
            if reference.get("encoding") != "json":
                raw = base64.b64decode(json.loads(raw)["data"], validate=True)
            parts.append(raw)
        payload = b"".join(parts)
        if len(payload) != reference["bytes"] or _sha(payload) != reference["sha256"]:
            raise ValueError("stored value differs from its digest")
        return json.loads(payload)

    def _entries(self, reference, depth: int = 0):
        raw = self._chunk(reference["sha256"])
        page = json.loads(raw)
        if (depth > 64 or len(raw) != reference["bytes"] or page.get("schema") != PAGE
                or page.get("kind") != reference["kind"] or page.get("level") != reference["level"]):
            raise ValueError("page differs from its reference")
        for entry in page["entries"]:
            if page["level"]:
                yield from self._entries(entry, depth + 1)
            else:
                yield entry

    def _descriptor(self, stored, where: str):
        """One node of a page tree: an inline value, a value stored apart, or a paged container."""
        try:
            if not isinstance(stored, dict) or len(stored) != 1:
                raise ValueError("malformed value descriptor")
            if "inline" in stored:
                return stored["inline"]
            if "value" in stored:
                return self._value(stored["value"])
            entries = list(self._entries(stored["ref"]))
            pairs = [(key, child) for key, child in entries] if stored["ref"]["kind"] == "dict" else None
        except FAULTS as exc:
            return self._unresolved(stored, where, exc)
        if pairs is not None:
            return {key: self._descriptor(child, f"{where}/{key}") for key, child in pairs}
        return [self._descriptor(child, f"{where}/{index}") for index, child in enumerate(entries)]

    def _walk(self, value, where: str):
        """A plain record: replace each stored-apart marker in it; never look inside what it returns."""
        if isinstance(value, dict):
            if set(value) == {MARKER}:
                try:
                    return self._value(value)
                except FAULTS as exc:
                    return self._unresolved(value, where, exc)
            return {key: self._walk(item, f"{where}/{key}") for key, item in value.items()}
        if isinstance(value, list):
            return [self._walk(item, f"{where}/{index}") for index, item in enumerate(value)]
        return value

    def file(self, name: str) -> dict:
        try:
            raw = _read(self.directory / name)
        except FAULTS as exc:
            return self._unresolved(None, name, exc)
        entry = {"sha256": _sha(raw), "bytes": len(raw)}
        try:
            stored = json.loads(raw)
        except FAULTS as exc:
            return {**entry, **self._unresolved(None, name, exc)}
        if isinstance(stored, dict) and stored.get("schema") == ENVELOPE:
            entry["format"] = ENVELOPE
            entry["envelope"] = {key: stored.get(key) for key in ("kind", "revision", "source")}
            entry["value"] = self._descriptor(stored.get("root"), name)
            for part in ("maintenance", "attribution", "facts"):
                item = stored.get(part)
                entry[part] = self._descriptor(item, f"{name}:{part}") if isinstance(item, dict) else item
        else:
            entry["value"] = self._walk(stored, name)
        return entry


def _document(directory: Path) -> dict:
    record = _Record(directory)
    files, other, present = {}, [], 0
    for parent, folders, names in os.walk(directory):
        folders.sort()
        relative = Path(parent).relative_to(directory)
        for name in sorted(names):
            path = relative / name
            if relative == CHUNKS:
                present += 1
            elif name.endswith(".json"):
                files[str(path)] = record.file(str(path))
            else:
                try:
                    raw = _read(directory / path)
                    other.append({"path": str(path), "bytes": len(raw), "sha256": _sha(raw)})
                except FAULTS as exc:
                    other.append({"path": str(path), **record._unresolved(None, str(path), exc)})
    return {"schema": SCHEMA, "record": directory.name, "complete": not record.problems,
            "problems": record.problems, "files": files, "other_files": other,
            "chunks": {"present": present, "used": len(record.used)}}


def _stable(directory: Path) -> dict:
    """Read again while a problem coincides with the record files changing underneath the read."""
    before = None
    for _ in range(ATTEMPTS):
        document = _document(directory)
        digests = {name: entry.get("sha256") for name, entry in document["files"].items()}
        if document["complete"] or digests == before:
            break
        before = digests
    return document


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".export-")
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            json.dump(value, stream, indent=1)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise


def _named(document: dict) -> dict:
    """The objective and repository, where the record itself states them."""
    context = document["files"].get("context.json", {})
    result = {"sha256": context.get("sha256")}
    checkpoint = context["value"].get("checkpoint") if isinstance(context.get("value"), dict) else None
    if isinstance(checkpoint, dict) and isinstance(checkpoint.get("objective"), str):
        result["objective"] = checkpoint["objective"]
    attribution = context.get("attribution")
    if isinstance(attribution, dict) and isinstance(attribution.get("repository"), str):
        result["repository"] = attribution["repository"]
    return result


def run(destination: Path, *, source: Path | None = None) -> dict:
    """Export every 0.21 record under `source` to `destination`. Returns the counts and the index path."""
    source = (paths.state_home() if source is None else Path(source)).resolve()
    destination = Path(destination).resolve()
    if destination == source or source in destination.parents:
        raise PodError("export_inside_source", f"{destination} is inside the records being exported.",
                       fix="choose a destination outside " + str(source))
    if not source.is_dir():
        raise PodError("export_source_missing", f"{source} is not a directory.",
                       fix="name the state directory that holds the 0.21 records with --from")
    counts = dict.fromkeys(("found", "exported", "incomplete", "failed", "empty"), 0)
    records, skipped = [], []
    for entry in sorted(os.scandir(source), key=lambda item: item.name):
        if not RECORD_KEY.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False):
            if entry.name != paths.objectives_home().name:
                skipped.append(entry.name)
            continue
        counts["found"] += 1
        row = {"record": entry.name, "source": entry.path, "output": None}
        try:
            document = _stable(Path(entry.path))
            row.update(_named(document))
            if not document["files"]:
                row["status"] = "empty"
            else:
                row["status"] = "exported" if document["complete"] else "incomplete"
                row["problems"] = document["problems"]
                row["output"] = str(destination / (entry.name + ".json"))
                _write(Path(row["output"]), document)
        except FAULTS as exc:
            row.update(status="failed", output=None, problems=[f"{type(exc).__name__}: {exc}"])
        counts[row["status"]] += 1
        records.append(row)
    index = destination / "index.json"
    _write(index, {"schema": SCHEMA, "source": str(source), "counts": counts, "records": records,
                   "skipped": skipped})
    return {**counts, "index": str(index)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pod export",
                                     description="Write every record written by 0.21 to plain JSON. "
                                                 "The records themselves are only read.")
    parser.add_argument("--to", required=True, type=Path, help="directory for the exported files")
    parser.add_argument("--from", dest="source", type=Path, help="state directory to read (default: Pod's own)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(args.to, source=args.source)
    except PodError as exc:
        if args.json:
            print(json.dumps({"error": exc.as_dict()}))
        else:
            print(f"pod export: {exc} {exc.fix or ''}".rstrip(), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result))
    else:
        print("{found} records: {exported} exported, {incomplete} incomplete, {failed} failed, "
              "{empty} empty. Index: {index}".format(**result))
    return 1 if result["incomplete"] or result["failed"] else 0
