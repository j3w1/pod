"""Generate genuine 0.7.1 records with the Git-object bundle, never record editing."""
import argparse
import json
from pathlib import Path
import shutil

from pod import __version__
from pod.ledger import objective_root
from tests.issue41_support import ProductionCase


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--abbreviated", action="store_true")
    args = parser.parse_args()
    assert __version__ == "0.7.1", __version__
    case = ProductionCase(); case.setUp()
    try:
        if args.abbreviated:
            case.candidate = case.base[:7]
            case.write([case.criterion(state="satisfied", evidence=[{
                "check":"check", "command":"check", "result":"pass", "reference":"old-observation"}])],
                governance={"base_ref":"target"})
        else:
            assurance = {"id":"A", "kind":"assurance", "provenance":"coordinator", "parent":"O1", "check":"review",
                "scope":{"paths":["src"]}, "question":"correct?", "candidate":case.base, "existing_evidence":"tests",
                "insufficiency":"no review", "state":"waiting", "wait":{"class":"sequenced", "referent":"O1"}}
            case.intake(assurance, case.sub("S"))
            frozen=case.packet(["A"], role="review"); admission=case.start("review",frozen)["admission"]
            case.settle(admission)
            rows=case.stored(); rows[1].pop("executor"); rows[1].update(state="waiting",wait={"class":"sequenced","referent":"O1"})
            rows.append({"id":"C", "kind":"correction", "provenance":"coordinator", "parent":"A", "check":"fix", "state":"waiting", "wait":{"class":"sequenced","referent":"O1"}})
            case.report(admission,frozen,map={"obligations":rows},triage=[{
                "finding":"F1", "severity":"major", "triage":"required_correction", "summary":"fix root", "correction":"C"}])
            rows=case.stored(); c=next(row for row in rows if row["id"]=="C"); c.pop("wait"); c.update(state="satisfied",evidence=[case.proof("C")]);case.write(rows)
            frozen=case.packet(["A"], role="review"); admission=case.start("delta-review",frozen)["admission"]
            case.settle(admission)
            rows=case.stored(); a=next(row for row in rows if row["id"]=="A");a.pop("executor");a.update(state="satisfied",evidence=[{"attempt":admission["admission_id"]}])
            case.report(admission,frozen,map={"obligations":rows})
            rows=case.stored()
            for row in rows:
                if row["id"] in ("O1","S"):
                    row.pop("wait",None);row.pop("executor",None);row.update(state="satisfied",evidence=[case.proof(row["id"])])
            case.write(rows)
            (args.destination / "port.json").write_text(json.dumps(case.port.workers))
            (args.destination / "packet.json").write_text(json.dumps(frozen))
            (args.destination / "admission.json").write_text(json.dumps(admission))
        shutil.copytree(case.project,args.destination / "project")
        shutil.copyfile(objective_root(case.project,"objective") / "context.json",args.destination / "context.json")
        (args.destination / "provenance.json").write_text(json.dumps({"version":__version__,"candidate":case.base,"writer":"2f02be99f27e6a860c3115a9c62539b0812bc038"}))
    finally:
        case.doCleanups()


if __name__ == "__main__":
    main()
