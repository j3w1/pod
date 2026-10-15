"""Write single-unit decision evidence with the genuine 0.15.0 bundle."""

import json
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

from pod import __version__
from pod.config import effective
from pod.governor import prepare_candidate, decide, _read_journal, _record_path
from pod.ledger import logical_projection, read
from pod.operations import OrcaPort
from tests import kernel_support as ks


def main():
    assert __version__ == "0.15.0", __version__
    destination = Path(sys.argv[1])
    case = ks.KernelCase()
    case.setUp()
    try:
        with patch.object(OrcaPort, "read_native", autospec=True,
                side_effect=lambda _port, owner, **kwargs: case.port.read_native(owner, **kwargs)):
            case.intake(ks.governance_policy(case, "PA"))
            binding = prepare_candidate(case.project, "objective", owner="owner", unit="delivery", now=ks.NOW,
                observation=ks.observation(commit=case.candidate,
                    tree=ks.git(case.project, "rev-parse", "HEAD^{tree}"), base=case.base,
                    policy=effective(case.project)["revision"], workflows={}, verification=[]),
                branch={"remote": "origin", "base": "target", "branch": "delivery"})["candidate"]
            action = {"kind": "merge", "candidate": binding["id"], "unit": "delivery", "target": "origin/target",
                      "reason": "single unit", "effects": [], "authorization": ks.authorization(
                          candidate=binding["commit"], tree=binding["tree"], scope=("merge",), target="origin/target")}
            for stage in ("missing", "satisfied", "attached"):
                if stage == "satisfied":
                    packet = case.packet(["PA"], role="review")
                    admission = case.start("review", packet)["admission"]
                    case.settle(admission)
                    rows = case.stored()
                    rows[1].pop("executor")
                    rows[1].update(state="satisfied", evidence=[{"attempt": admission["admission_id"]}])
                    case.report(admission, packet, map={"obligations": rows})
                state = read(case.project, "objective")
                journal_path = _record_path(case.project, "objective")
                before = _read_journal(journal_path)
                native = case.port.read_native("owner", authority_runs=("run",),
                                               assignments=tuple(state["admissions"].values()))
                decision = decide(case.project, "objective", owner="owner", action=action, now=ks.NOW,
                    native_projection=logical_projection(case.project, native, objective="objective"))
                (destination / (stage + ".json")).write_text(json.dumps({
                    "state": state, "workers": case.port.workers, "action": action,
                    "journal_before": before, "journal_after": _read_journal(journal_path), "decision": decision}))
            shutil.copytree(case.project, destination / "project")
            (destination / "provenance.json").write_text(json.dumps({"version": __version__, "writer": "29a57a5"}))
    finally:
        case.doCleanups()


if __name__ == "__main__":
    main()
