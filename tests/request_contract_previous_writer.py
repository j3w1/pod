"""Observe refusal codes through either complete bundle, with recording ports."""

import json
import sys

from pod import __version__
from pod.errors import PodError
from pod.internal import run
from pod.ledger import read, update_admission
from tests.issue41_support import ProductionCase


def closing(scenario):
    case = ProductionCase()
    case.setUp()
    try:
        case.intake(case.sub("S"))
        fields = ({"boundary": {"paths": ["src"]}} if scenario == "discard" else
                  {"role": "investigate", "resolves": "which result?", "stop_condition": "answer"})
        packet = case.packet(["S"], **fields)
        admission = case.start("work", packet)["admission"]
        case.settle(admission)

        def finished():
            rows = case.stored()
            for row in rows:
                row.pop("executor", None); row.pop("wait", None)
                row.update(state="satisfied", evidence=[case.proof(row["id"])])
            return rows

        if scenario == "closed":
            case.report(admission, packet, map={"obligations": finished(), "close": True})
        case.port.effective = {"agent": "codex", "model": "gpt-6-luna", "effort": "medium"}
        runtime_changed = scenario in ("runtime", "runtime_malformed")
        if runtime_changed:
            case.port.runtime = "changed-runtime"
        continuity = None
        try:
            case.report(admission, packet, outcome="bogus" if scenario in ("malformed", "runtime_malformed") else "succeeded",
                        map={"obligations": finished(), "close": True})
        except PodError as exc:
            code = exc.code
            continuity = (exc.detail or {}).get("runtime_continuity")
        else:
            code = "ACCEPTED"
        if scenario == "discard":
            case.write(finished(), close=True, dispositions=[{
                "admission": admission["admission_id"], "discarded": True, "reason": "superseded"}])
        state = read(case.project, "objective")
        result = {"version": __version__, "code": code,
                  "route_mismatch": state["admissions"][admission["admission_id"]]["route_decision"]["route_mismatch"],
                  "closed": bool(state["checkpoint"].get("closure"))}
        if runtime_changed:
            history = state["checkpoint"]["continuity"]["history"]
            keys = ("from_runtime", "to_runtime", "provenance")
            result.update(admission_runtime=state["admissions"][admission["admission_id"]]["runtime"],
                          refs_runtime=[ref["runtime"] for ref in state["checkpoint"]["native_refs"]],
                          persisted_rebinds=[{key: entry[key] for key in keys} for entry in history],
                          runtime_continuity=[{key: entry[key] for key in keys} for entry in continuity or []],
                          reported_rebind_matches_history=continuity == history)
        if scenario == "discard":
            passing = {"schema": "pod-evidence/v1", "criterion": "PoD#1", "candidate": case.candidate,
                       "sources": [], "policy_revision": "p", "dependencies": [], "environment": "fixture",
                       "check": "unit", "command": "unit", "result": "passed", "timestamp": "2026-09-24T00:00:00Z",
                       "status": "PASS", "reference": "log"}
            owner = {"schema": "pod-acceptance-authorization/v1", "candidate": case.candidate,
                     "policy_revision": "p", "utc": "2026-09-24T00:00:00Z", "accepted_by": "owner"}
            acceptance = run("acceptance", {
                "project": str(case.project), "objective": "objective", "criteria": ["PoD#1"],
                "evidence_rows": [passing], "candidate": case.candidate, "policy_revision": "p", "sources": [],
                "dependencies": [], "environment": "fixture", "review_required": False, "hosted_required": False,
                "owner_acceptance": owner})
            result.update(accepted=acceptance["accepted"],
                          checks_pass=acceptance["required_checks_pass"],
                          owner_granted=not acceptance["project_assessment_required"],
                          route_holds=[row["reason"] for row in acceptance["route_holds"]])
        print(json.dumps(result, sort_keys=True))
    finally:
        case.doCleanups()


def main():
    case = ProductionCase()
    case.setUp()
    try:
        case.intake(case.sub("S"), case.sub("T"))
        packet = case.packet(["S"], role="investigate", resolves="which result?", stop_condition="answer")
        observed = {}

        def result(name, call):
            try:
                call()
            except PodError as exc:
                observed[name] = exc.code
            else:
                observed[name] = "ACCEPTED"

        for name, fields in (("text", {"responsibility": "x" * 4097}),
                             ("list", {"scope": ["src"] * 65}),
                             ("serves", {"serves": ["S"] * 17}),
                             ("role", {"role": "invalid"}),
                             ("unsupported", {"unexpected": "PRIVATE-SENTINEL"})):
            result(name, lambda fields=fields: run("packet", {**packet["body"], **fields}))
        missing = dict(packet["body"])
        missing.pop("report_contract")
        result("missing", lambda: run("packet", missing))
        result("obligation_text", lambda: case.write([case.criterion(check="x" * 513), case.sub("S"), case.sub("T")]))
        result("wait", lambda: case.write([case.criterion(), case.sub("S", wait={"class": "dependency", "referent": "missing"}), case.sub("T")]))
        result("stored_only", lambda: case.start("work", packet, accompanying={"revision": 1}))
        for field, value in (("candidate", "PRIVATE-SENTINEL"), ("criteria", ["PRIVATE-SENTINEL"]), ("plan_revision", "PRIVATE-SENTINEL")):
            changed = case.packet(["S"], role="investigate", resolves="which result?", stop_condition="answer", **{field: value})
            result("plan_" + field, lambda changed=changed: case.start("work", changed))
        changed = case.packet(["S"], policy_revision="a" * 64)
        result("packet_policy", lambda: case.start("work", changed))
        admission = case.start("work", packet)["admission"]
        case.settle(admission)
        result("settled_report", lambda: case.report(admission, packet))
        def unknown(row):
            row["route_decision"].update(effective_unknown=True,
                                         effective={key: "unknown" for key in ("agent", "model", "effort", "context")})
        update_admission(case.project, "objective", owner="owner", admission_id=admission["admission_id"], update=unknown)
        result("malformed_nonclosing_effective_refresh", lambda: case.report(admission, packet, outcome="bogus"))
        refreshed = read(case.project, "objective")["admissions"][admission["admission_id"]]["route_decision"]
        case.port.effective = {"agent": "codex", "model": "gpt-6-luna", "effort": "medium"}
        result("malformed_nonclosing_route_mismatch", lambda: case.report(admission, packet, outcome="bogus"))
        stored = read(case.project, "objective")["admissions"][admission["admission_id"]]
        print(json.dumps({"version": __version__, "codes": observed,
                          "effective_unknown": refreshed["effective_unknown"],
                          "route_mismatch": stored["route_decision"]["route_mismatch"]}, sort_keys=True))
    finally:
        case.doCleanups()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        closing(sys.argv[1])
    else:
        main()
