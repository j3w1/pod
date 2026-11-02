"""A host-verified local append, recorded as a typed delivery (R209).

Some hosts deliver a reviewed fix by a protected local append to a release train, with no remote merge. The
coordinator names only the host's operation id. Pod reads the operation back through the personal
`authorization_verifier` (action `local_append`), sending nothing but that id, so the host has no expectation to
echo; every fact comes from the host's own record. Pod then checks the facts against its own record and its own Git
reads, and records a delivery only for an operation that succeeded. Pod never appends, retries or replays anything.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import gitio
from .errors import PodError
from .util import bounded_text, exact, normalize_timestamp

KIND = "local_append"
ACTION = "local_append"
STATES = ("succeeded", "requested", "running", "failed", "unknown")
METHODS = ("fast-forward", "merge")
MAX_REFERENCE = 512
MAX_ROW_SEQUENCE = 2 ** 53 - 1
MAX_DEFERRED = 32
VERDICT_FIELDS = frozenset({"schema", "action", "reference", "verified", "state", "operation"})
IDENTITY_FIELDS = frozenset({"repository", "objective", "source", "candidate"})
PROVENANCE_FIELDS = frozenset({"store", "row", "previous", "result", "method", "certificate", "appended_at"})
OPERATION_FIELDS = IDENTITY_FIELDS | PROVENANCE_FIELDS
RECORD_FIELDS = frozenset({"kind", "seq", "operation", "base_ref", "base", "candidate", "tree", "previous",
                           "previous_tree", "result", "result_tree", "method", "store", "row", "certificate",
                           "appended_at", "verification"})
_REPO_KEY = re.compile(r"[0-9a-f]{64}\Z")
_ROW_DIGEST = re.compile(r"[\x21-\x7e]{1,128}\Z")

_WAIT = ("the host has not finished this append; repeat the same checkpoint once the host's own record shows it "
         "succeeded. Pod never appends, retries or replays it")
_HOST = "repair the host verifier so it reports its own protected record exactly, then repeat the same checkpoint"
_EXACT = ("this append is not an exact fast-forward or clean merge of the checkpoint candidate, so it delivers "
          "nothing here; the host appends the exact reviewed candidate under a new operation, then record that one")
NEXT = {
    "local_append_verifier_unconfigured": "configure the host's authorization_verifier in the personal config; Pod "
                                          "records a local append only from the host's own read-back",
    "local_append_verifier_failed": "repair the host verifier, then repeat the same checkpoint",
    "local_append_verifier_timeout": "resolve the host verifier's timeout, then repeat the same checkpoint",
    "local_append_verifier_malformed": "correct the host verifier's local_append verdict contract, then repeat",
    "local_append_verifier_mismatch": _HOST,
    "local_append_absent": "name an operation the host verifier can read back from its own protected record",
    "local_append_foreign": "name this objective's own append operation; this one belongs to another repository, "
                            "objective or source",
    "local_append_requested": _WAIT,
    "local_append_running": _WAIT,
    "local_append_unknown": ("the host does not know whether this append took effect; settle it on the host, then "
                             "repeat the same checkpoint. Pod never appends or retries it"),
    "local_append_failed": ("the host's append failed and delivered nothing; the host appends again under a new "
                            "operation, then record that one"),
    "local_append_candidate_mismatch": ("this operation appended another commit or tree; name the operation that "
                                        "appended this checkpoint's candidate"),
    "local_append_row_mismatch": _HOST,
    "local_append_unproved": ("the exact-candidate proof for this checkpoint candidate is not ready (read it with "
                              "pod admission --selected); settle what it names, then repeat the same checkpoint. A "
                              "write that also changes the candidate is judged against the recorded one: record the "
                              "append in its own checkpoint"),
    "local_append_git_unavailable": ("fetch or restore the named commits in this repository (a partial clone is "
                                     "refused), then repeat the same checkpoint"),
    "local_append_tip_mismatch": _HOST,
    "local_append_not_exact": _EXACT,
    "local_append_remote_merge": ("this objective has a remote merge that landed or may have landed: record a "
                                  "merge PASS with delivery: {record}, or let a pending or UNKNOWN merge row settle "
                                  "first; a FAILED or CANCELED merge does not block a local append"),
}
MESSAGES = {
    "local_append_verifier_unconfigured": "No host verifier is configured to read back a local append",
    "local_append_verifier_failed": "The host verifier failed",
    "local_append_verifier_timeout": "The host verifier timed out",
    "local_append_verifier_malformed": "The host verifier's local_append verdict is malformed",
    "local_append_verifier_mismatch": "The host verifier answered for another request",
    "local_append_absent": "The host holds no authenticated record of this operation",
    "local_append_foreign": "The host's record of this operation belongs to another objective",
    "local_append_requested": "The host's append is still requested",
    "local_append_running": "The host's append is still running",
    "local_append_unknown": "The host does not know the outcome of this append",
    "local_append_failed": "The host's append failed",
    "local_append_candidate_mismatch": "The host's append is not of this checkpoint candidate",
    "local_append_row_mismatch": "The host's ledger row records another operation",
    "local_append_unproved": "The checkpoint candidate's exact proof is not ready",
    "local_append_git_unavailable": "Pod cannot read the append's commits or compute its result",
    "local_append_tip_mismatch": "A stated train tip tree differs from Git",
    "local_append_not_exact": "The append result is not exact",
    "local_append_remote_merge": "This objective has a remote merge",
}
# Every local append refusal is a finalization refusal: it says what remains in recording order.
CODES = frozenset(NEXT)


def refusal(code: str, **referent) -> PodError:
    return PodError(code, f"{MESSAGES[code]}. Next: {NEXT[code]}",
                    {"detail": code, "next_action": NEXT[code], **({"referent": referent} if referent else {})})


def is_local(delivery: object) -> bool:
    return isinstance(delivery, dict) and delivery.get("kind") == KIND


def remote(delivery: object) -> dict | None:
    """The R93 delivery record, or None for none or a local append."""
    return delivery if isinstance(delivery, dict) and "kind" not in delivery else None


# --------------------------------------------------------------------------- input


def request(value: object, prior: object) -> str | None:
    """The operation a checkpoint's `delivery` input names, or None for an R93 input (read as today).

    The R93 branch is untouched: any object without the new keys is an R93 request read by its `record`, including a
    restated complete stored R93 record. `{local_append: {operation}}` is the new request. A restated stored local
    record is accepted only when it equals the stored record exactly."""
    if not isinstance(value, dict) or not ({"local_append", "kind"} & set(value)):
        return None
    if "local_append" in value and set(value) != {"local_append"}:
        raise PodError("delivery_kind_mixed", "A delivery names exactly one kind: {record} or {local_append}",
                       {"detail": "delivery_kind_mixed", "field": "delivery",
                        "next_action": "send delivery: {local_append: {operation}} alone, or the R93 {record}"})
    if "local_append" in value:
        inner = exact(value["local_append"], {"operation"}, {"operation"}, name="local_append")
        return bounded_text(inner["operation"], name="local_append.operation", limit=MAX_REFERENCE)
    if value.get("kind") != KIND:
        raise PodError("delivery_kind_unsupported", "This delivery kind is not supported",
                       {"detail": "delivery_kind_unsupported", "field": "delivery.kind",
                        "next_action": "send delivery: {record} or {local_append: {operation}}"})
    if "record" in value:
        raise PodError("delivery_kind_mixed", "A delivery names exactly one kind: {record} or {local_append}",
                       {"detail": "delivery_kind_mixed", "field": "delivery",
                        "next_action": "send delivery: {local_append: {operation}} alone, or the R93 {record}"})
    if value != prior:
        raise PodError("delivery_restatement_mismatch", "A restated local delivery differs from the recorded one",
                       {"detail": "delivery_restatement_mismatch", "field": "delivery",
                        "next_action": "restate the recorded delivery exactly, or send delivery: {local_append: "
                                       "{operation}} naming its operation"})
    return value["operation"]


# --------------------------------------------------------------------------- verdict


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError("text")
    value.encode("utf-8")
    return value


def _object_pair(value: object) -> dict:
    pair = exact(value, {"commit", "tree"}, {"commit", "tree"}, name="local append object pair",
                 error=lambda name: ValueError(name))
    if any(not isinstance(item, str) or not gitio.OBJECT_ID.fullmatch(item) for item in pair.values()):
        raise ValueError("object id")
    return dict(pair)


def _source(value: object) -> dict | None:
    if value is None:
        return None
    source = exact(value, {"repository", "number", "locator"}, {"repository", "number", "locator"},
                   name="local append source", error=lambda name: ValueError(name))
    if type(source["number"]) is not int or not 1 <= source["number"] <= 2 ** 31 - 1:
        raise ValueError("number")
    _text(source["repository"], 256)
    _text(source["locator"], 2048)
    return dict(source)


def _closed(value: object, fields: set[str], name: str) -> dict:
    return exact(value, fields, fields, name=name, error=lambda label: ValueError(label))


def decode(value: object) -> dict:
    """The verdict's exact shape, also for stored evidence; ValueError for anything else. Times are normalized."""
    verdict = _closed(value, set(VERDICT_FIELDS), "local append verdict")
    for key in ("schema", "action"):
        if not isinstance(verdict[key], str):
            raise ValueError(key)
    _text(verdict["reference"], MAX_REFERENCE)
    if type(verdict["verified"]) is not bool:
        raise ValueError("verified")
    if not verdict["verified"]:
        if verdict["state"] is not None or verdict["operation"] is not None:
            raise ValueError("unverified facts")
        return dict(verdict)
    if verdict["state"] not in STATES:
        raise ValueError("state")
    facts = _closed(verdict["operation"], set(OPERATION_FIELDS), "local append operation")
    if not isinstance(facts["repository"], str) or not _REPO_KEY.fullmatch(facts["repository"]):
        raise ValueError("repository")
    _text(facts["objective"], 256)
    decoded = {"repository": facts["repository"], "objective": facts["objective"],
               "source": _source(facts["source"]), "candidate": _object_pair(facts["candidate"])}
    width = len(decoded["candidate"]["commit"])
    if verdict["state"] != "succeeded":
        if any(facts[key] is not None for key in PROVENANCE_FIELDS):
            raise ValueError("provenance before success")
        decoded.update({key: None for key in PROVENANCE_FIELDS})
    else:
        store = _closed(facts["store"], {"instance", "epoch"}, "local append store")
        row = _closed(facts["row"], {"sequence", "digest", "operation"}, "local append row")
        if (type(row["sequence"]) is not int or not 1 <= row["sequence"] <= MAX_ROW_SEQUENCE
                or not isinstance(row["digest"], str) or not _ROW_DIGEST.fullmatch(row["digest"])):
            raise ValueError("row")
        _text(row["operation"], MAX_REFERENCE)
        certificate = _closed(facts["certificate"], {"tier", "deferred"}, "local append certificate")
        deferred = certificate["deferred"]
        if (not isinstance(deferred, list) or len(deferred) > MAX_DEFERRED
                or len(set(map(str, deferred))) != len(deferred)):
            raise ValueError("deferred")
        if facts["method"] not in METHODS:
            raise ValueError("method")
        decoded.update({"store": {"instance": _text(store["instance"], 256), "epoch": _text(store["epoch"], 256)},
                        "row": {"sequence": row["sequence"], "digest": row["digest"], "operation": row["operation"]},
                        "previous": _object_pair(facts["previous"]), "result": _object_pair(facts["result"]),
                        "method": facts["method"],
                        "certificate": {"tier": _text(certificate["tier"], 64),
                                        "deferred": [_text(item, 256) for item in deferred]}})
        try:
            decoded["appended_at"] = normalize_timestamp(facts["appended_at"], field="append time",
                                                         code="local_append_verifier_malformed")
        except PodError:
            raise ValueError("appended_at") from None
        if any(len(decoded[key][part]) != width for key in ("previous", "result") for part in ("commit", "tree")):
            raise ValueError("object format")
    if len(decoded["candidate"]["tree"]) != width:
        raise ValueError("object format")
    return {**verdict, "operation": decoded}


def read_verdict(command: list[str] | None, operation: str) -> dict:
    """One bounded read of the host's own record of `operation`; refusals before any write."""
    from . import relay
    if not command:
        raise refusal("local_append_verifier_unconfigured")
    sent = {"schema": relay.SCHEMA, "action": ACTION, "reference": operation}
    try:
        raw = relay._output(command, sent)
    except PodError as exc:
        code = {"relay_verifier_timeout": "local_append_verifier_timeout",
                "relay_verifier_malformed": "local_append_verifier_malformed"}.get(exc.code, "local_append_verifier_failed")
        raise refusal(code) from None
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=relay._unique)
        verdict = decode(value)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise refusal("local_append_verifier_malformed") from None
    differs = sorted(key for key in sent if verdict[key] != sent[key])
    if differs:
        raise refusal("local_append_verifier_mismatch", fields=differs)
    return verdict


# --------------------------------------------------------------------------- Pod's own Git reads


def _commit(project: Path, value: str) -> bool:
    return gitio.resolve_commit(project, value) == value


def _tree(project: Path, commit: str) -> str | None:
    found = gitio.read(project, ["rev-parse", "--verify", "--quiet", "--end-of-options", commit + "^{tree}"])
    value = found.stdout.strip() if found is not None and found.returncode == 0 else ""
    return value if gitio.OBJECT_ID.fullmatch(value) and len(value) == len(commit) else None


def _parents(project: Path, commit: str) -> list[str] | None:
    found = gitio.read(project, ["rev-list", "--parents", "-n", "1", "--end-of-options", commit])
    words = found.stdout.split() if found is not None and found.returncode == 0 else []
    if not words or words[0] != commit or any(not gitio.OBJECT_ID.fullmatch(item) for item in words):
        return None
    return words[1:]


def _scratch_root(project: Path) -> Path:
    from .observations import cache_root
    root = cache_root(project).parent / "local-append"
    if root.is_symlink():
        raise OSError("redirected scratch")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def _isolated_environment(home: Path) -> dict[str, str]:
    """Only Pod-owned structural settings: no system, global or user config or attributes, no redirection."""
    kept = {key: value for key, value in os.environ.items()
            if key in ("PATH", "LANG", "TZ") or key.startswith("LC_")}
    return {**kept, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}


def merge_tree(project: Path, previous: str, candidate: str) -> tuple[str, str | None]:
    """Pod's own clean merge of `previous` and `candidate`: ("clean", tree), ("conflict", None) or
    ("unavailable", None).

    Git runs on a disposable bare repository of the source's object format whose only objects are the source's own
    (a read-only alternate), with no config, an empty HOME, no system attributes, no repository attributes and an
    empty attribute source. So no attribute, merge driver, filter or local merge setting applies, and the source
    gains no object; the scratch repository is deleted afterwards. A partial clone or promisor source is refused."""
    if gitio.partial_clone(project) is not False:
        return "unavailable", None
    form = gitio.read(project, ["rev-parse", "--show-object-format"])
    objects = gitio.read(project, ["rev-parse", "--path-format=absolute", "--git-path", "objects"])
    fmt = form.stdout.strip() if form is not None and form.returncode == 0 else ""
    source = Path(objects.stdout.strip()) if objects is not None and objects.returncode == 0 else None
    if fmt not in ("sha1", "sha256") or source is None or not source.is_absolute() or not source.is_dir():
        return "unavailable", None
    try:
        scratch = Path(tempfile.mkdtemp(prefix="merge-", dir=_scratch_root(project)))
    except (OSError, PodError):
        return "unavailable", None
    try:
        home, repository = scratch / "home", scratch / "repository"
        home.mkdir(mode=0o700)
        env = _isolated_environment(home)

        def git(argv: list[str], *, data: bytes | None = None) -> subprocess.CompletedProcess:
            return subprocess.run(["git", *argv], cwd=str(scratch), env=env, input=data if data is not None else b"",
                                  capture_output=True, timeout=30, check=False)

        if git(["init", "--bare", "--quiet", "--template=", f"--object-format={fmt}", str(repository)]).returncode:
            return "unavailable", None
        alternates = repository / "objects" / "info"
        alternates.mkdir(parents=True, exist_ok=True)
        (alternates / "alternates").write_text(str(source) + "\n", encoding="utf-8")
        empty = git(["-C", str(repository), "hash-object", "-t", "tree", "--stdin"])
        empty_tree = empty.stdout.decode("ascii", "replace").strip() if empty.returncode == 0 else ""
        if not gitio.OBJECT_ID.fullmatch(empty_tree):
            return "unavailable", None
        merged = git(["-C", str(repository), f"--attr-source={empty_tree}", "merge-tree", "--write-tree",
                      "--no-messages", "--end-of-options", previous, candidate])
        if merged.returncode == 1:
            return "conflict", None
        tree = merged.stdout.decode("ascii", "replace").split("\n", 1)[0].strip() if merged.returncode == 0 else ""
        if not gitio.OBJECT_ID.fullmatch(tree) or len(tree) != len(candidate):
            return "unavailable", None
        return "clean", tree
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return "unavailable", None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _exact_method(project: Path, candidate: str, previous: str, result: str, result_tree: str) -> str:
    """The lead's rule: a fast-forward to the candidate, or a merge [previous, candidate] with Pod's own tree."""
    if result == candidate:
        ancestor = gitio.is_ancestor(project, previous, candidate)
        if ancestor is None:
            raise refusal("local_append_git_unavailable", commit=previous)
        if not ancestor:
            raise refusal("local_append_not_exact", reason="previous_not_ancestor")
        return "fast-forward"
    parents = _parents(project, result)
    if parents is None:
        raise refusal("local_append_git_unavailable", commit=result)
    if parents != [previous, candidate]:
        raise refusal("local_append_not_exact", reason="parents")
    outcome, tree = merge_tree(project, previous, candidate)
    if outcome == "unavailable":
        raise refusal("local_append_git_unavailable", commit=result)
    if outcome == "conflict":
        raise refusal("local_append_not_exact", reason="merge_conflicts")
    if tree != result_tree:
        raise refusal("local_append_not_exact", reason="merge_tree")
    return "merge"


# --------------------------------------------------------------------------- recording


def remote_merge_rows(journal: dict) -> list[str]:
    """ALLOW merge rows that landed or may have landed; FAILED and CANCELED ones never did."""
    return [row["record_id"] for row in journal.get("actions", [])
            if row.get("decision") == "ALLOW" and (row.get("action") or {}).get("kind") == "merge"
            and row.get("outcome") in ("PASS", "pending", "UNKNOWN")]


def recorded(prior: dict, reason: str) -> PodError:
    name = prior.get("operation") if is_local(prior) else prior.get("record")
    return PodError("delivery_recorded", "This objective already has a different verified delivery record",
                    {"detail": "delivery_recorded", "record": name, "kind": prior.get("kind", "merge"),
                     "reason": reason,
                     "next_action": "keep the existing delivery; an objective holds one delivery, and a local append "
                                    "and a remote merge never replace one another; deliver further work under a new "
                                    "objective"})


def source_identity(source: object) -> dict | None:
    if not isinstance(source, dict):
        return None
    return {key: source.get(key) for key in ("repository", "number", "locator")}


def verify(project: Path, objective: str, map_state: dict, operation: str, *, seq: int, candidate: object,
           source: object, command: list[str] | None, proof) -> dict:
    """R209: the record for `operation`, or a refusal before any write.

    `proof` is a callable returning the shared exact-candidate proof judged on the caller's authority-joined
    snapshot. A recorded local delivery with the same operation is a restatement: it answers from the record with
    no read. The checks after it run in a fixed order, and only a succeeded operation of this objective's exact
    candidate, with exact tips, records anything."""
    prior = map_state.get("delivery")
    if prior is not None:
        if not is_local(prior):
            raise recorded(prior, "kind")
        validate_record(prior)
        if prior["operation"] == operation:
            return dict(prior)
        raise recorded(prior, "operation")
    from .governor import _read_journal, _record_path
    merges = remote_merge_rows(_read_journal(_record_path(project, objective)))
    if merges:
        raise refusal("local_append_remote_merge", records=",".join(sorted(merges)[:8]))
    governance = map_state.get("governance") or {}
    if not isinstance(governance.get("base_ref"), str) or not isinstance(governance.get("base"), str):
        raise PodError("delivery_unverified", "A local append needs the objective's bound governance target",
                       {"detail": "governance_unbound",
                        "next_action": "bind governance to its target with a checkpoint, then record the append"})
    verdict = read_verdict(command, operation)
    if not verdict["verified"]:
        raise refusal("local_append_absent")
    facts = verdict["operation"]
    from .github import repository_context
    expected = {"repository": repository_context(project)["repo_key"], "objective": objective,
                "source": source_identity(source)}
    foreign = sorted(key for key, value in expected.items() if facts[key] != value)
    if foreign:
        raise refusal("local_append_foreign", fields=foreign)
    if verdict["state"] != "succeeded":
        raise refusal("local_append_" + verdict["state"])
    if not isinstance(candidate, str) or not gitio.OBJECT_ID.fullmatch(candidate):
        raise refusal("local_append_candidate_mismatch", field="candidate")
    tree = _tree(project, candidate)
    if tree is None:
        raise refusal("local_append_git_unavailable", commit=candidate)
    differs = [key for key, value in (("commit", candidate), ("tree", tree)) if facts["candidate"][key] != value]
    if differs:
        raise refusal("local_append_candidate_mismatch", fields=differs)
    if facts["row"]["operation"] != operation:
        raise refusal("local_append_row_mismatch")
    judged = proof()
    if judged.get("status") != "ready":
        error = refusal("local_append_unproved")
        error.detail["proof"] = {"status": judged.get("status"), "reason": judged.get("reason"),
                                 "gaps": list(judged.get("gaps") or [])[:16]}
        raise error
    trees = {}
    for name in ("previous", "result"):
        commit = facts[name]["commit"]
        if not _commit(project, commit) or (found := _tree(project, commit)) is None:
            raise refusal("local_append_git_unavailable", commit=commit)
        if found != facts[name]["tree"]:
            raise refusal("local_append_tip_mismatch", tip=name)
        trees[name] = found
    previous, result = facts["previous"]["commit"], facts["result"]["commit"]
    method = _exact_method(project, candidate, previous, result, trees["result"])
    if facts["method"] != method:
        raise refusal("local_append_not_exact", reason="method", method=method)
    return {"kind": KIND, "seq": seq, "operation": operation, "base_ref": governance["base_ref"],
            "base": governance["base"], "candidate": candidate, "tree": tree, "previous": previous,
            "previous_tree": trees["previous"], "result": result, "result_tree": trees["result"], "method": method,
            "store": dict(facts["store"]), "row": {"sequence": facts["row"]["sequence"],
                                                    "digest": facts["row"]["digest"]},
            "certificate": {"tier": facts["certificate"]["tier"], "deferred": list(facts["certificate"]["deferred"])},
            "appended_at": facts["appended_at"], "verification": verdict}


def validate_record(value: object) -> dict:
    """A stored local delivery, validated on read without a host call; every verdict-sourced field equals the
    stored verdict, which must be the verified success of this operation."""
    from . import relay
    try:
        record = _closed(value, set(RECORD_FIELDS), "local delivery")
        if record["kind"] != KIND or type(record["seq"]) is not int or record["seq"] < 1:
            raise ValueError("kind")
        _text(record["operation"], MAX_REFERENCE)
        _text(record["base_ref"], 256)
        ids = [record[key] for key in ("base", "candidate", "tree", "previous", "previous_tree", "result",
                                       "result_tree")]
        if any(not isinstance(item, str) or not gitio.OBJECT_ID.fullmatch(item) for item in ids) \
                or len({len(item) for item in ids}) != 1 or record["method"] not in METHODS:
            raise ValueError("ids")
        verdict = decode(record["verification"])
        facts = verdict["operation"]
        if (verdict["schema"] != relay.SCHEMA or verdict["action"] != ACTION or verdict["verified"] is not True
                or verdict["state"] != "succeeded" or verdict["reference"] != record["operation"]
                or verdict != record["verification"]
                or facts["candidate"] != {"commit": record["candidate"], "tree": record["tree"]}
                or facts["previous"] != {"commit": record["previous"], "tree": record["previous_tree"]}
                or facts["result"] != {"commit": record["result"], "tree": record["result_tree"]}
                or facts["method"] != record["method"] or facts["store"] != record["store"]
                or {"sequence": facts["row"]["sequence"], "digest": facts["row"]["digest"]} != record["row"]
                or facts["row"]["operation"] != record["operation"]
                or facts["certificate"] != record["certificate"] or facts["appended_at"] != record["appended_at"]
                or record["method"] == "fast-forward" and (record["result"] != record["candidate"]
                                                           or record["result_tree"] != record["tree"])):
            raise ValueError("verification")
    except (ValueError, TypeError, AttributeError, KeyError):
        raise PodError("state_unsupported", "Verified local delivery record is malformed") from None
    return value


def projection_line(delivery: dict) -> str:
    return (f"Delivery   local append {delivery['method']} {delivery['result'][:12]} "
            f"(operation {delivery['operation'][:64]})")
