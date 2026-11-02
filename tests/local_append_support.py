"""R209 fixtures: real production entries, a real verifier process answering from a control file, and plain Git."""

import json
from pathlib import Path
import sys

import yaml

from pod.config import personal_path
from pod.github import repository_context
from pod.governor import _record_path
from pod.ledger import read
from tests.kernel_support import assurance_review, git
from tests.pod_tick_support import TickCase

VERIFIER = '''import json, os, signal, sys
from pathlib import Path
control = json.loads(Path(sys.argv[1]).read_text())
request = json.load(sys.stdin)
with Path(sys.argv[2]).open("a") as log:
    log.write(json.dumps(request) + "\\n")
mode = control.get("mode")
if mode == "fail":
    print("verifier-private-sentinel", file=sys.stderr)
    sys.exit(3)
if mode == "timeout":
    signal.pause()
if mode == "oversized":
    os.write(1, b"x" * 32768)
    signal.pause()
if mode == "invalid_json":
    print("verifier-private-sentinel")
    sys.exit()
text = json.dumps(control["verdict"])
if mode == "duplicate":
    text = text[:-1] + ', "verified": true}'
print(text)
'''
OPERATION = "host-op-0001"


class LocalAppendMixin:
    """A fix commit on main (the checkpoint candidate) and a host train built beside it with plain Git."""
    review = assurance_review

    def setup_verifier(self):
        self.control = self.root / "verifier-control.json"
        self.log = self.root / "verifier-requests.jsonl"
        script = self.root / "verifier.py"
        script.write_text(VERIFIER)
        self.command = [sys.executable, str(script), str(self.control), str(self.log)]
        self.configure(self.command)
        self.operation = OPERATION

    # ----------------------------------------------------------------- configuration and verifier

    def configure(self, command):
        path = personal_path(self.project)
        value = yaml.safe_load(path.read_text())
        if command is None:
            value.pop("authorization_verifier", None)
        else:
            value["authorization_verifier"] = command
        path.write_text(yaml.safe_dump(value))

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def answer(self, verdict=None, *, mode=None):
        self.control.write_text(json.dumps({**({"verdict": verdict} if verdict is not None else {}),
                                            **({"mode": mode} if mode else {})}))

    def verdict(self, changes=None, *, state="succeeded", verified=True, previous=None, result=None, method=None):
        """The host's own record of the operation; `changes` maps dotted paths to replacement values."""
        if not verified:
            value = {"schema": "pod-relay-verification/v1", "action": "local_append", "reference": self.operation,
                     "verified": False, "state": None, "operation": None}
        else:
            facts = {"repository": repository_context(self.project)["repo_key"], "objective": "objective",
                     "source": self.source_identity(),
                     "candidate": {"commit": self.candidate, "tree": self.tree(self.candidate)},
                     "store": None, "row": None, "previous": None, "result": None, "method": None,
                     "certificate": None, "appended_at": None}
            if state == "succeeded":
                previous = previous or self.previous
                result = result or self.result
                facts.update(store={"instance": "host-store", "epoch": "3"},
                             row={"sequence": 41, "digest": "sha256:" + "ab" * 32, "operation": self.operation},
                             previous={"commit": previous, "tree": self.tree(previous)},
                             result={"commit": result, "tree": self.tree(result)},
                             method=method or self.method,
                             certificate={"tier": "V2", "deferred": ["combined docs review at ship"]},
                             appended_at="2026-11-01T22:00:00-05:00")
            value = {"schema": "pod-relay-verification/v1", "action": "local_append", "reference": self.operation,
                     "verified": True, "state": state, "operation": facts}
        for path, item in (changes or {}).items():
            target, parts = value, path.split(".")
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = item
        return value

    def source_identity(self):
        source = self.source
        return None if source is None else {key: source[key] for key in ("repository", "number", "locator")}

    # ----------------------------------------------------------------- Git

    def tree(self, commit):
        return git(self.project, "rev-parse", commit + "^{tree}")

    def fix(self, path="src/fix.py", text="fix\n"):
        target = self.project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        git(self.project, "add", path)
        git(self.project, "commit", "-qm", "fix")
        self.candidate = git(self.project, "rev-parse", "HEAD")
        self.fast_forward()
        return self.candidate

    def fast_forward(self):
        """The host appended the candidate by fast-forward from the governance base."""
        self.previous, self.result, self.method = self.base, self.candidate, "fast-forward"

    def train_merge(self, path="docs/train.md", text="train\n"):
        """The host's train gained another fix, then a clean merge [previous, candidate]."""
        git(self.project, "checkout", "-q", "-b", "train", self.base)
        target = self.project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        git(self.project, "add", path)
        git(self.project, "commit", "-qm", "train fix")
        self.previous = git(self.project, "rev-parse", "HEAD")
        git(self.project, "merge", "--no-ff", "-q", "-m", "append", self.candidate)
        self.result, self.method = git(self.project, "rev-parse", "HEAD"), "merge"
        git(self.project, "checkout", "-q", "main")
        return self.result

    # ----------------------------------------------------------------- the objective

    def ready(self, *rows, sources=(), **fields):
        """Fix committed, assurance A reviewed at the fix and criterion O1 proved: the proof is ready."""
        if self.candidate == self.base:
            self.fix()
        self.intake({"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                     "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src right?",
                     "candidate": self.candidate, "existing_evidence": "tests", "insufficiency": "no review",
                     "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}, *rows, **fields)
        self.review()
        stored = self.stored()
        criterion = next(row for row in stored if row["id"] == "O1")
        criterion.pop("executor", None)
        from pod.records import source_identity
        criterion.update(state="satisfied", evidence=[self.proof("O1", sources=[
            source_identity(self.project, path) for path in sources])])
        self.write(stored)

    def bound(self, body="- [ ] **PoD#1"):
        """The same objective bound to an issue source."""
        from pod.github import issue_intake
        self.issue.body = body
        self.source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.issue.calls.clear()
        return self.source

    def request(self, operation=None):
        return {"local_append": {"operation": operation or self.operation}}

    def record(self, operation=None, **fields):
        self.answer(self.verdict())
        return self.write(delivery=self.request(operation), **fields)

    def delivery(self):
        return read(self.project, "objective")["checkpoint"].get("delivery")

    def journal_bytes(self):
        path = _record_path(self.project, "objective")
        return Path(path).read_bytes() if Path(path).exists() else None

    def move_target(self, path, text="later target change\n"):
        """The governance target moves forward from the base without the candidate (the host's train is elsewhere)."""
        git(self.project, "checkout", "-q", "-b", "target-move", "refs/remotes/origin/target")
        target = self.project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        git(self.project, "add", path)
        git(self.project, "commit", "-qm", "target change")
        moved = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/target", moved)
        git(self.project, "checkout", "-q", "main")
        git(self.project, "branch", "-q", "-D", "target-move")
        return moved



def recorded_source_packet(case, serves, **fields):
    """A packet freezing the recorded source when there is one; a direct objective freezes none."""
    from tests.kernel_support import KernelCase
    source = read(case.project, "objective")["checkpoint"].get("objective_source")
    fields.pop("objective_source", None)
    if source is not None:
        fields["objective_source"] = source
    return KernelCase.packet(case, serves, **fields)


class LocalAppendCase(LocalAppendMixin, TickCase):
    def setUp(self):
        super().setUp()
        self.setup_verifier()
        self.source = None

    packet = recorded_source_packet
