"""Seeded, stdlib-only generative tests of P1–P6 over the boundary functions.

A small simulator drives the same kernel functions the ledger calls at the checkpoint,
admission and report boundaries: map writes, admissions under the logical ceiling, native
settlement, report consumption with triage, dispositions, closure and reopening. After
every accepted transition the kernel's own predicates must hold, and so must a separate
oracle written here in plain terms, so the test is not a restatement of the code under
test. On failure the seed, step and operation are printed as the counterexample.
"""

from copy import deepcopy
import os
import random
import unittest

from pod.errors import PodError
from tests.common import VERIFICATION, proof
from pod.obligations import (PROPERTIES, TERMINAL, accept_write, admission_binding, admission_refusal, admit,
                             disposition,
                             label_qualification, overlaps, properties, triage, undispositioned)

SEEDS = int(os.environ.get("POD_PROPERTY_SEEDS", "40"))
STEPS = int(os.environ.get("POD_PROPERTY_STEPS", "70"))
PATHS = ("src", "src/api", "src/cli", "docs", "tests", "tools")
PARTIES = ("owner", "user", "provider", "third_party")


class Simulation:
    def __init__(self, seed: int):
        self.random = random.Random(seed)
        self.seed = seed
        self.map = None
        self.admissions: dict[str, dict] = {}
        self.outstanding: list[str] = []
        self.ceiling = self.random.choice((0, 1, 2, 3))
        self.candidate = "c1"
        self.counter = 0
        self.accepted = 0
        self.refused = 0
        self.closures = 0
        self.quiescent = 0
        self.qualified = 0
        self.log: list[str] = []

    # -------------------------------------------------------------- facts
    def ctx(self) -> dict:
        return {"admissions": self.admissions, "outstanding": list(self.outstanding), "ceiling": self.ceiling,
                "delegation": "available" if self.ceiling else "unavailable", "constraints": [
                    {"id": "hold", "kind": "max_workers", "provenance": "user_direct", "value": 1}],
                "criteria": ["PoD#1", "PoD#2"], "candidate": self.candidate, "policy_revision": "p",
                "verification": dict(VERIFICATION), "source_current": lambda entry: True,
                "governance": {"status": "no_repository"},
                "source_state": lambda path: "unavailable" if path.startswith("missing") else "present",
                "authorization_granted": lambda scope, candidate: False,
                "git_delta": lambda source, target: [self.random.choice(PATHS) + "/f.py"],
                "is_ancestor": lambda head, target: self.random.random() < 0.7,
                "governor_pending": False}

    def boundary(self) -> dict:
        return {"paths": sorted(set(self.random.sample(PATHS, self.random.randint(0, 2)))), "surfaces": []}

    def fresh(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    # -------------------------------------------------------------- plausible states
    def plausible(self, rows: list[dict], row: dict) -> None:
        """Give one obligation a state that is often, but not always, valid now."""
        ids = [item["id"] for item in rows]
        live = [item["id"] for item in rows if item["state"] not in TERMINAL and item["id"] != row["id"]]
        holder = [item["id"] for item in rows if item["state"] == "active" and item.get("executor") == "coordinator"]
        for key in ("executor", "wait", "external"):
            row.pop(key, None)
        serving = [key for key in self.outstanding if row["id"] in self.admissions[key]["serves"]]
        choice = self.random.random()
        if serving and choice < 0.9:
            row["state"], row["executor"] = "active", serving[0]
            return
        pending = [key for key, item in undispositioned(self.ctx()).items() if row["id"] in item["serves"]]
        if pending and choice < 0.8:
            if holder:
                row["state"], row["wait"] = "waiting", {"class": "sequenced", "referent": holder[0]}
            else:
                row["state"], row["executor"] = "active", "coordinator"
            return
        options = ["waiting_dep", "blocked", "authority", "hold", "input", "satisfied", "sequenced", "sequenced",
                   "sequenced", "active", "capacity", "ownership", "garbage"]
        pick = self.random.choice(options)
        if pick == "active" and holder:
            pick = "sequenced"
        if pick == "waiting_dep" and live:
            target = self.random.choice(live)
            kind = "contract_unsettled" if next(i for i in rows if i["id"] == target)["kind"] == "subgoal" \
                and self.random.random() < 0.5 else "dependency"
            row["state"], row["wait"] = "waiting", {"class": kind, "referent": target}
        elif pick == "blocked":
            row["state"], row["external"] = "blocked_external", {
                "party": self.random.choice(PARTIES), "need": "an external input",
                "unblocks_when": "it is supplied"}
        elif pick == "authority":
            row["state"], row["wait"] = "waiting", {"class": "authority", "referent": {
                "kind": "authorization", "scope": "merge", "candidate": self.candidate}}
        elif pick == "hold":
            row["state"], row["wait"] = "waiting", {"class": "user_hold", "referent": "hold"}
        elif pick == "input":
            row["state"], row["wait"] = "waiting", {"class": "input_unavailable", "referent": "missing/in.csv"}
        elif pick == "satisfied":
            row["state"] = "satisfied"
            if row["kind"] == "assurance":
                attempts = [key for key, item in self.admissions.items()
                            if item["role"] == "review" and item["serves"] == [row["id"]]]
                row["evidence"] = [{"attempt": self.random.choice(attempts)}] if attempts else []
            else:
                row["evidence"] = [proof(self.random.choice((row["id"], row["id"], row["id"], "other")),
                                         self.random.choice((self.candidate, self.candidate, "c0")),
                                         status=self.random.choice(("PASS", "PASS", "FAILED")),
                                         environment=self.random.choice(("fixture", "fixture", "other")))]
            if not row["evidence"]:
                row.pop("evidence")
        elif pick == "sequenced":
            row["state"], row["wait"] = "waiting", {"class": "sequenced",
                                                    "referent": holder[0] if holder else self.random.choice(ids)}
        elif pick == "active":
            row["state"], row["executor"] = "active", "coordinator"
        elif pick == "capacity":
            row["state"], row["wait"] = "waiting", {"class": "capacity", "referent": list(self.outstanding)}
        elif pick == "ownership":
            row["state"], row["wait"] = "waiting", {"class": "ownership", "referent": self.random.choice(
                self.outstanding or ids)}
        else:
            row["state"] = self.random.choice(("unassigned", "waiting", "active"))
            if row["state"] == "waiting":
                row["wait"] = {"class": "another_worker_active", "referent": "x"}
            elif row["state"] == "active":
                row["executor"] = self.random.choice(list(self.admissions) or ["ghost"])

    def new_row(self, rows: list[dict]) -> dict:
        parents = [item for item in rows if item["state"] != "withdrawn"]
        kind = self.random.choice(("subgoal", "subgoal", "assurance", "correction", "criterion", "steer"))
        provenance = self.random.choice(("coordinator", "coordinator", "coordinator", "user_direct", "objective"))
        row = {"id": self.fresh("N"), "kind": kind, "provenance": provenance, "check": "it holds",
               "boundary": self.boundary(), "state": "unassigned"}
        if parents and (kind in ("subgoal", "correction") or provenance == "coordinator"):
            candidates = [item for item in parents if item["kind"] == "assurance"] if kind == "correction" else parents
            if candidates:
                row["parent"] = self.random.choice(candidates)["id"]
        if kind == "assurance":
            row.update(scope=self.boundary(), question="is it right?", candidate=self.candidate,
                       existing_evidence="tests", insufficiency="unreviewed")
            if self.random.random() < 0.5:
                row["uncovered_risk"] = "a distinct risk"
        if provenance == "user_direct":
            row["source"] = {"instruction": "the user asked for it"}
        elif provenance == "objective":
            row["source"] = {"ref": "late"}
        return row

    # -------------------------------------------------------------- operations
    def intake(self):
        rows = [{"id": "O1", "kind": "criterion", "provenance": "objective", "source": {"ref": "PoD#1"},
                 "check": "PoD#1 passes", "boundary": self.boundary(), "state": "active",
                 "executor": "coordinator"},
                {"id": "O2", "kind": "delivery", "provenance": "objective", "source": {"ref": "PoD#2"},
                 "check": "merged", "boundary": self.boundary(), "state": "waiting",
                 "wait": {"class": "sequenced", "referent": "O1"}}]
        for _ in range(self.random.randint(1, 4)):
            rows.append({"id": self.fresh("S"), "kind": "subgoal", "provenance": "coordinator", "parent": "O1",
                         "check": "part done", "boundary": self.boundary(), "state": "unassigned"})
        if self.random.random() < 0.7:
            rows.append({"id": "A1", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                         "check": "reviewed", "scope": {"paths": ["src"], "surfaces": []},
                         "question": "is src correct?", "candidate": self.candidate,
                         "existing_evidence": "unit tests", "insufficiency": "no independent review",
                         "boundary": {"paths": [], "surfaces": []}, "state": "unassigned"})
        return "intake", {"governance": {"base_ref": None}, "obligations": rows,
                          "proposals": [{"id": "P1", "source": "worker_report", "origin_ref": "r",
                                         "summary": "an idea", "status": "open"}]}

    def write(self):
        rows = deepcopy(self.map["obligations"])
        for row in rows:
            stale = (row["state"] == "unassigned"
                     or row["state"] == "active" and row.get("executor") not in ("coordinator", *self.outstanding))
            if row["state"] != "withdrawn" and (stale or self.random.random() < 0.2):
                self.plausible(rows, row)
        if self.random.random() < 0.3:
            rows.append(self.new_row(rows))
        value = {"obligations": rows, "proposals": deepcopy(self.map["proposals"])}
        if self.random.random() < 0.15:
            target = self.random.choice(rows)
            if target["state"] != "withdrawn":
                for key in ("executor", "wait", "external"):
                    target.pop(key, None)
                target["state"] = "withdrawn"
                target["withdrawal"] = {"by": self.random.choice(("coordinator", "user_direct", "project_policy")),
                                        "reason": "judged unnecessary"}
        if self.random.random() < 0.15:
            value["revision_authority"] = {"provenance": "user_direct", "instruction": "direct steering"}
        if self.random.random() < 0.1:
            value["close"] = True
        if self.map.get("closure") and self.random.random() < 0.5:
            value["reopen"] = True
            value["revision_authority"] = {"provenance": "user_direct", "instruction": "reopen"}
        if self.random.random() < 0.05:
            self.candidate = "c" + str(self.random.randint(1, 3))
        return "write", value

    def try_admit(self):
        live = [row["id"] for row in self.map["obligations"]
                if row["state"] not in TERMINAL or self.random.random() < 0.1]
        serves = self.random.sample(live, min(len(live), self.random.choice((1, 1, 2))))
        if self.random.random() < 0.1:
            serves = ["P1"]
        role = self.random.choice(("implement", "implement", "investigate", "review"))
        body = {"serves": serves, "role": role, "boundary": self.boundary(),
                "map_revision": self.map["revision"] if self.random.random() < 0.95 else 99}
        if role == "investigate" and self.random.random() < 0.8:
            body.update(resolves="which option", stop_condition="one option named")
        key = self.fresh("adm")
        context = self.ctx()
        if len(self.outstanding) >= self.ceiling:
            with self.assert_refused("capacity"):
                admission_refusal(self.map, body, context, admission_id=key)
                raise PodError("logical_capacity_full", "ceiling")
            return
        accompanying = None
        if self.random.random() < 0.8:
            rows = deepcopy(self.map["obligations"])
            for row in rows:
                if (row["state"] == "unassigned" or row["state"] == "active"
                        and row.get("executor") not in ("coordinator", *self.outstanding)):
                    self.plausible(rows, row)
            accompanying = {"obligations": rows}
        try:
            state = admit(self.map, body, context, admission_id=key, accompanying=accompanying)
        except PodError:
            self.refused += 1
            return
        self.admissions[key] = {"serves": list(body["serves"]), "role": role, "boundary": body["boundary"],
                                "state": "reserved", "disposition": None, "changed_paths": None,
                                "boundary_exceeded": [], "candidate": self.candidate, "report": None,
                                "result": {"base": "b" * 40, "head": None} if role == "implement" else None,
                                "binding": admission_binding(body, context)}
        self.outstanding.append(key)
        self.accept("admit " + key + " " + repr(body), state)

    def settle(self):
        if self.outstanding:
            key = self.random.choice(self.outstanding)
            self.outstanding.remove(key)
            self.admissions[key]["state"] = self.random.choice(("bound", "bound", "deferred"))
            self.log.append("settle " + key)

    def report(self):
        settled = [key for key, row in self.admissions.items()
                   if key not in self.outstanding and row["report"] is None and row["state"] == "bound"]
        if not settled:
            return
        key = self.random.choice(settled)
        row = self.admissions[key]
        row["report"] = {"outcome": self.random.choice(("succeeded", "succeeded", "succeeded", "failed",
                                                        "partial", "blocked", "uncertain")),
                         "status": self.random.choice(("validated_observation", "validated_observation",
                                                       "reconciliation_required"))}
        if row["role"] == "implement":
            changed = [self.random.choice(PATHS) + "/x.py" for _ in range(self.random.randint(0, 3))]
            row["changed_paths"] = sorted(set(changed))
            row["boundary_exceeded"] = [path for path in row["changed_paths"]
                                        if not any(path.startswith(prefix + "/") for prefix in row["boundary"]["paths"])]
            row["result"] = {"base": "b" * 40, "head": self.random.choice(("b" * 40, "d" * 40))}
        rows = deepcopy(self.map["obligations"])
        for item in rows:
            if item["state"] == "active" and item.get("executor") == key:
                self.plausible(rows, item)
        findings = []
        if row["role"] == "review" and self.random.random() < 0.6:
            severity = self.random.choice(("blocker", "major", "minor"))
            triaged = self.random.choice(("required_correction", "advisory"))
            finding = {"finding": self.fresh("f"), "severity": severity, "triage": triaged, "summary": "issue"}
            if triaged == "advisory" and self.random.random() < 0.7:
                finding["reason"] = "not material"
            if triaged == "required_correction":
                correction = {"id": self.fresh("K"), "kind": "correction", "provenance": "coordinator",
                              "parent": row["serves"][0], "check": "fixed", "boundary": self.boundary(),
                              "state": "unassigned"}
                rows.append(correction)
                finding["correction"] = correction["id"]
            findings.append(finding)
        proposals = [{"summary": "a discovered idea"}] if self.random.random() < 0.3 else []
        try:
            value, triaged = triage(self.map, {"obligations": rows}, findings, proposals, self.ctx(),
                                    admission_id=key)
            state = accept_write(self.map, value, self.ctx(), triaged=triaged)
        except PodError:
            self.refused += 1
            return
        self.accept("report " + key, state)

    def dispose(self):
        pending = undispositioned(self.ctx())
        if not pending:
            return
        key = self.random.choice(sorted(pending))
        row = self.admissions[key]
        request = ({"admission": key, "discarded": True, "reason": "rejected"} if self.random.random() < 0.5 else
                   {"admission": key, "integrated_into": self.candidate, "reason": "accepted",
                    "attestation": "applied by hand"})
        try:
            record = disposition(row, request, self.ctx(), seq=self.map["seq"] + 1)
        except PodError:
            self.refused += 1
            return
        previous = row["disposition"]
        row["disposition"] = record
        rows = deepcopy(self.map["obligations"])
        for item in rows:
            if self.random.random() < 0.4:
                self.plausible(rows, item)
        try:
            state = accept_write(self.map, {"obligations": rows, "proposals": self.map["proposals"]}, self.ctx())
        except PodError:
            row["disposition"] = previous
            self.refused += 1
            return
        self.accept("dispose " + key, state)

    def finish(self):
        """Drive toward quiescence or closure: satisfy, withdraw or block every free obligation."""
        rows = deepcopy(self.map["obligations"])
        pending = undispositioned(self.ctx())
        blocked = [item for row in pending.values() for item in row["serves"]]
        quiesce = self.random.random() < 0.4
        for row in rows:
            if row["state"] in TERMINAL or row.get("executor") in self.outstanding or row["id"] in blocked:
                continue
            for key in ("executor", "wait", "external"):
                row.pop(key, None)
            if quiesce:
                row["state"], row["external"] = "blocked_external", {
                    "party": "user", "need": "a test identity", "unblocks_when": "the user supplies it"}
            elif row["kind"] == "assurance":
                attempts = [key for key, item in self.admissions.items()
                            if item["role"] == "review" and item["serves"] == [row["id"]]
                            and key not in self.outstanding and item["report"] is not None]
                if attempts:
                    row["state"], row["evidence"] = "satisfied", [{"attempt": attempts[-1]}]
                else:
                    row["state"], row["withdrawal"] = "withdrawn", {"by": "coordinator", "reason": "low risk"}
            elif row["provenance"] == "coordinator" and row["kind"] == "subgoal" and self.random.random() < 0.3:
                row["state"], row["withdrawal"] = "withdrawn", {"by": "coordinator", "reason": "folded"}
            else:
                row["state"], row["evidence"] = "satisfied", [proof(row["id"], self.candidate)]
        value = {"obligations": rows, "proposals": self.map["proposals"]}
        if not quiesce and self.random.random() < 0.7:
            value["close"] = True
        if self.map.get("closure"):
            value.update(reopen=True, revision_authority={"provenance": "user_direct", "instruction": "reopen"})
        try:
            state = accept_write(self.map, value, self.ctx())
        except PodError:
            self.refused += 1
            return
        self.accept("finish", state)

    def review(self):
        """Admit a review attempt on an open assurance obligation when capacity allows."""
        targets = [row["id"] for row in self.map["obligations"]
                   if row["kind"] == "assurance" and row["state"] not in TERMINAL
                   and row.get("executor") not in self.outstanding]
        if not targets or len(self.outstanding) >= self.ceiling:
            return
        key = self.fresh("rev")
        body = {"serves": [self.random.choice(targets)], "role": "review",
                "boundary": {"paths": [], "surfaces": []}, "map_revision": self.map["revision"]}
        rows = deepcopy(self.map["obligations"])
        for row in rows:
            if (row["state"] == "unassigned" or row["state"] == "active"
                    and row.get("executor") not in ("coordinator", *self.outstanding)):
                self.plausible(rows, row)
        try:
            state = admit(self.map, body, self.ctx(), admission_id=key, accompanying={"obligations": rows})
        except PodError:
            self.refused += 1
            return
        self.admissions[key] = {"serves": body["serves"], "role": "review", "boundary": body["boundary"],
                                "state": "reserved", "disposition": None, "changed_paths": None,
                                "boundary_exceeded": [], "candidate": self.candidate, "report": None,
                                "result": None, "binding": admission_binding(body, self.ctx())}
        self.outstanding.append(key)
        self.accept("admit " + key, state)

    # -------------------------------------------------------------- checks
    class _Refused:
        def __init__(self, sim, label):
            self.sim, self.label = sim, label

        def __enter__(self):
            return self

        def __exit__(self, kind, value, trace):
            if kind is None or not issubclass(kind, PodError):
                raise AssertionError(f"seed {self.sim.seed}: {self.label} was not refused")
            self.sim.refused += 1
            return True

    def assert_refused(self, label):
        return self._Refused(self, label)

    def accept(self, label: str, state: dict) -> None:
        self.log.append(label)
        self.map = state
        self.accepted += 1
        self.closures += bool(state.get("closure"))
        self.quiescent += (state.get("quiescence") or {}).get("state") == "quiescent"
        context = self.ctx()
        self.qualified += label_qualification(state, context, context["candidate"])["label"] == "QUALIFIED"
        failures = {name: found for name, found in properties(state, context).items() if found}
        failures.update({name: found for name, found in oracle(state, context).items() if found})
        if failures:
            raise AssertionError(f"counterexample seed={self.seed} step={len(self.log)} after {label}: "
                                 f"{failures}\nhistory: {self.log[-8:]}")
        if state.get("closure"):
            try:
                admission_refusal(state, {"serves": [state["obligations"][0]["id"]], "role": "implement",
                                          "boundary": {"paths": [], "surfaces": []},
                                          "map_revision": state["revision"]}, context, admission_id="post")
            except PodError as exc:
                if exc.code != "objective_closed":
                    raise AssertionError(f"seed {self.seed}: closed objective refused with {exc.code}")
            else:
                raise AssertionError(f"seed {self.seed}: a closed objective admitted work")

    def step(self) -> None:
        if self.map is None:
            _, value = self.intake()
            try:
                self.accept("intake", accept_write(None, value, self.ctx()))
            except PodError:
                self.refused += 1
            return
        operation = self.random.choice(("write", "write", "admit", "admit", "settle", "settle", "report",
                                        "report", "dispose", "finish", "review"))
        if operation == "write":
            _, value = self.write()
            try:
                state = accept_write(self.map, value, self.ctx())
            except PodError:
                self.refused += 1
                return
            self.accept("write", state)
        elif operation == "admit":
            self.try_admit()
        elif operation == "settle":
            self.settle()
        elif operation == "report":
            self.report()
        elif operation == "finish":
            self.finish()
        elif operation == "review":
            self.review()
        else:
            self.dispose()


def oracle(state: dict, context: dict) -> dict:
    """P1–P6 restated independently in plain terms for accepted records."""
    rows = {row["id"]: row for row in state["obligations"]}
    outstanding = set(context["outstanding"])
    admissions = context["admissions"]
    found = {"oracle_P1": [], "oracle_P2": [], "oracle_P3": [], "oracle_P4": [], "oracle_P5": [], "oracle_P6": []}
    active = [row for row in rows.values() if row["state"] == "active"]
    if not active:
        for row in rows.values():
            if row["state"] in TERMINAL:
                continue
            cursor, hops = row, 0
            while (cursor["state"] == "waiting" and cursor["wait"]["class"] in ("dependency", "contract_unsettled")
                   and hops <= len(rows)):
                cursor, hops = rows[cursor["wait"]["referent"]], hops + 1
            external = (cursor["state"] == "blocked_external" or cursor["state"] == "waiting"
                        and cursor["wait"]["class"] in ("authority", "user_hold", "input_unavailable"))
            if not external:
                found["oracle_P1"].append(row["id"])
    if len([row for row in active if row["executor"] == "coordinator"]) > 1:
        found["oracle_P1"].append("two coordinator-held obligations")
    for row in rows.values():
        cursor = row
        while cursor["provenance"] == "coordinator":
            if "parent" not in cursor:
                found["oracle_P2"].append(row["id"])
                break
            cursor = rows[cursor["parent"]]
    served: dict[str, list[str]] = {}
    for key in outstanding:
        for item in admissions[key]["serves"]:
            served.setdefault(item, []).append(key)
            target = rows.get(item)
            if target is None or target["state"] != "active" or target.get("executor") != key:
                found["oracle_P3"].append(f"{key}->{item}")
    found["oracle_P3"] += [item for item, keys in served.items() if len(keys) > 1]
    for key, row in admissions.items():
        if (row["role"] == "implement" and key not in outstanding and row["state"] != "deferred"
                and row["disposition"] is None):
            for item in row["serves"]:
                owner = rows.get(item)
                held = owner is not None and (owner["state"] == "active" and owner.get("executor") == "coordinator"
                                              or owner["state"] == "waiting"
                                              and owner["wait"]["class"] == "sequenced")
                if not held:
                    found["oracle_P4"].append(f"{key}->{item}")
                if owner is not None and owner["state"] == "satisfied":
                    found["oracle_P4"].append(f"{item} satisfied before disposition")
    label = label_qualification(state, context, context["candidate"])
    live = [row for row in rows.values() if row["kind"] == "assurance" and row["state"] != "withdrawn"]
    if label["label"] == "QUALIFIED":
        if not live:
            found["oracle_P5"].append("label without assurance")
        for row in live:
            attempts = [item["attempt"] for item in row.get("evidence", [])]
            if row["state"] != "satisfied" or not attempts or any(
                    admissions.get(attempt, {}).get("role") != "review" or attempt in outstanding
                    or (admissions[attempt].get("report") or {}).get("outcome") != "succeeded"
                    or (admissions[attempt].get("report") or {}).get("status") != "validated_observation"
                    for attempt in attempts):
                found["oracle_P5"].append(row["id"])
    for row in rows.values():
        # R41: every satisfied non-assurance obligation carries evidence joined to it by id and
        # bound to the current environment and policy.
        if row["state"] == "satisfied" and row["kind"] != "assurance":
            for item in row.get("evidence", []):
                if (item.get("criterion") != row["id"] or item.get("status") != "PASS"
                        or item.get("environment") != context["verification"]["environment"]
                        or item.get("policy_revision") != context["policy_revision"]):
                    found["oracle_P5"].append(f"{row['id']} evidence binding")
    if state.get("closure"):
        found["oracle_P6"] += [row["id"] for row in rows.values() if row["state"] not in TERMINAL]
        if outstanding:
            found["oracle_P6"].append("outstanding admissions")
    for key in outstanding:
        for other in outstanding:
            if (key < other and admissions[key]["role"] == "implement" and admissions[other]["role"] == "implement"
                    and admissions[key]["state"] == "reserved" and admissions[other]["state"] == "reserved"
                    and overlaps(admissions[key]["boundary"], admissions[other]["boundary"])):
                found["oracle_P3"].append(f"overlapping implementations {key} {other}")
    return found


class PropertyTests(unittest.TestCase):
    def test_properties_hold_after_every_accepted_transition(self):
        totals = {"accepted": 0, "refused": 0, "admissions": 0, "closures": 0, "quiescent": 0, "qualified": 0}
        for seed in range(SEEDS):
            simulation = Simulation(seed)
            for _ in range(STEPS):
                simulation.step()
            totals["accepted"] += simulation.accepted
            totals["refused"] += simulation.refused
            totals["admissions"] += len(simulation.admissions)
            totals["closures"] += simulation.closures
            totals["quiescent"] += simulation.quiescent
            totals["qualified"] += simulation.qualified
        # The generator must exercise the boundary, not only be refused by it.
        self.assertGreater(totals["accepted"], SEEDS * 5, totals)
        self.assertGreater(totals["refused"], SEEDS * 5, totals)
        self.assertGreater(totals["admissions"], SEEDS, totals)
        self.assertGreater(totals["closures"], 0, totals)
        self.assertGreater(totals["quiescent"], 0, totals)
        self.assertGreater(totals["qualified"], 0, totals)

    def test_predicates_are_the_boundary_predicates(self):
        self.assertEqual(sorted(PROPERTIES), ["P1", "P2", "P3", "P4", "P5", "P6"])


if __name__ == "__main__":
    unittest.main()
