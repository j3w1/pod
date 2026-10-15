"""Observe refusal codes through either complete bundle, with recording ports."""

import json

from pod import __version__
from pod.errors import PodError
from pod.internal import run
from pod.ledger import read, update_admission
from tests.issue41_support import ProductionCase


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
    main()
