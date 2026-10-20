"""Generate disposable source-bound proof with the genuine preceding bundle."""

from contextlib import contextmanager
import json
from pathlib import Path
import sys
from unittest.mock import patch

from pod import __version__
from pod.github import issue_intake
from pod.ledger import read
from tests.pod_tick_support import TickCase

BASELINE = "ca5913f639b23d8af8869174fcb8ba72187977cd"


def main():
    assert __version__ == "0.21.4", __version__
    destination = Path(sys.argv[1])

    @contextmanager
    def persistent_fixture():
        yield destination

    with patch("tests.issue41_support.fixture", persistent_fixture):
        case = TickCase()
        case.setUp()
    try:
        case.issue.body = "\n".join(f"- [ ] **PoD#{n}** requirement {n}" for n in range(1, 6))
        if "nested" in sys.argv[2:]:
            case.issue.body = case.issue.body.replace("requirement 1", "requirement 1\n  Acceptance requires alpha.", 1)
        if "long" in sys.argv[2:]:
            case.issue.body = case.issue.body.replace("requirement 1", "requirement 1\n  " + "nested alpha " * 100, 1)
        original_body = case.issue.body
        proof_bodies = [original_body]
        source = issue_intake(case.project, case.locator, port=case.issue)["source"]
        rows = [case.criterion("blocked_external", id=f"O{n}", source={"ref": f"PoD#{n}"},
                               check=f"Result {n} meets acceptance",
                               external={"party": "user", "need": "answer", "unblocks_when": "answered"})
                for n in range(1, 6)]
        delegated = "delegated" in sys.argv[2:]
        unproved = "unproved" in sys.argv[2:]
        if delegated:
            rows[0].pop("external")
            rows[0].update(state="active", executor="coordinator")
        case.write(rows, objective_source=source, criteria=[f"PoD#{n}" for n in range(1, 6)],
                   governance={"base_ref": "target"})
        if delegated:
            frozen = case.packet([f"O{n}" for n in range(1, 6)], criteria=[f"PoD#{n}" for n in range(1, 6)])
            admission = case.start("existing-task", frozen)["admission"]
            if not unproved:
                case.settle(admission)
        accepting_source = {}
        if "observe-before-proof" in sys.argv[2:] or "observe-with-proof" in sys.argv[2:]:
            assert not delegated and not unproved
            case.issue.body = case.issue.body.replace("alpha", "beta")
            proof_bodies.append(case.issue.body)
            changed_source = issue_intake(case.project, case.locator, port=case.issue)["source"]
            if "observe-before-proof" in sys.argv[2:]:
                case.write(case.stored(), objective_source=changed_source, criteria=[f"PoD#{n}" for n in range(1, 6)])
            else:
                accepting_source = {"objective_source": changed_source}
        if not unproved:
            rows = case.stored()
            for row in rows:
                row.pop("external", None); row.pop("executor", None)
                row.update(state="satisfied", evidence=[case.proof(row["id"])])
            if delegated:
                pending = case.stored()
                for index, row in enumerate(pending):
                    row.pop("executor", None)
                    if index == 0:
                        row.update(state="active", executor="coordinator")
                    else:
                        row.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
                case.report(admission, frozen, map={"obligations": pending})
                case.write(rows, criteria=[f"PoD#{n}" for n in range(1, 6)], dispositions=[{
                    "admission": admission["admission_id"], "integrated_into": case.candidate,
                    "attestation": "Generic no-change result checked"}])
            else:
                case.write(rows, criteria=[f"PoD#{n}" for n in range(1, 6)], **accepting_source)
            if "unticked" not in sys.argv[2:]:
                case.tick()
        if "conflicting" in sys.argv[2:]:
            case.issue.body = case.issue.body.replace("requirement 1", "changed requirement 1", 1)
            proof_bodies.append(case.issue.body)
            changed_source = issue_intake(case.project, case.locator, port=case.issue)["source"]
            rows = case.stored()
            for row in rows:
                row["evidence"].append(case.proof(row["id"], reference="later-" + row["id"]))
            case.write(rows, objective_source=changed_source, criteria=[f"PoD#{n}" for n in range(1, 6)])
            case.tick(2)
        state = read(case.project, "objective")
        assert "criterion_scopes" not in state["pod_ticks"]
        assert "blocks" not in state["pod_ticks"]["facts"]
        (destination / "writer.json").write_text(json.dumps({
            "writer": BASELINE, "version": __version__, "body": case.issue.body,
            "original_body": original_body, "source": source, "candidate": case.candidate,
            "state": state, "workers": case.port.workers, "proof_bodies": proof_bodies}, ensure_ascii=False))
    finally:
        case.doCleanups()


if __name__ == "__main__":
    main()
