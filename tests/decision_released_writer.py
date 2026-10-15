"""Generate genuine pre-channel records using the released bundle from Git objects."""

import argparse
import json
from pathlib import Path
import shutil

from pod import __version__
from pod.ledger import objective_root
from tests.kernel_support import KernelCase, USER, governance_policy
from tests.test_succession import SuccessionCase
from tests.test_historical_retirement import RetirementCase


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("kind", choices=("admission", "closed", "claim", "retirement"))
    args = parser.parse_args()
    assert __version__ == "0.13.3", __version__
    case = {"admission": RetirementCase, "closed": KernelCase, "claim": SuccessionCase, "retirement": RetirementCase}[args.kind]()
    case.setUp()
    try:
        if args.kind == "admission":
            case.establish()
        elif args.kind == "closed":
            case.intake(governance_policy(case, "A"), revision_authority=USER)
            review = case.stored()[1]
            review.pop("wait")
            review.update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "scoped owner waiver"})
            case.write([case.criterion("satisfied", evidence=[case.proof("O1")]), review], close=True,
                       revision_authority={**USER, "instruction": "Waive this review for the disposable objective"},
                       acceptances=[{"obligation": "O1", "source": "owner-answer", "verdict": "accepted",
                                     "candidate": case.candidate, "evidence": "external result"}])
        elif args.kind == "claim":
            case.predecessor()
            case.successor()
            case.claim(case.decide())
        else:
            case.delivered()
            decision = case.decide()
            case.retire(decision)
            (args.destination / "decision.json").write_text(json.dumps(decision))
        shutil.copytree(case.project, args.destination / "project")
        for objective in ("objective", "successor"):
            source = objective_root(case.project, objective) / "context.json"
            if source.exists():
                shutil.copyfile(source, args.destination / f"{objective}.json")
        (args.destination / "provenance.json").write_text(json.dumps({"version": __version__, "kind": args.kind,
            "writer": "60247346a4ee7306fbbbe47510adc5f11c4b4aa2"}))
    finally:
        case.doCleanups()


if __name__ == "__main__":
    main()
