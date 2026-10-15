"""A caller discovers input contracts and repairs refusals through real entries."""

from copy import deepcopy
import json
from unittest.mock import patch

from pod.config import effective, load
from tests.issue41_support import ProductionCase
from tests.test_issue41_corrections import entry
from tests import kernel_support as ks

SENTINEL = "PRIVATE-REQUEST-SENTINEL"


def limits(spec, path=()):
    for key in ("max_length", "max_items"):
        if key in spec:
            yield path, key, spec[key]
    for key, item in spec.items():
        if isinstance(item, dict):
            yield from limits(item, (*path, key))


def replace(value, path, item):
    if not path:
        return item
    if path[0] == "items":
        return [replace(value[0] if value else None, path[1:], item)]
    if path[0] == "values":
        return {"input": item}
    if path[0] == "keys":
        return {item: "value"}
    if path[0] == "capacity":
        return item
    result = deepcopy(value)
    result[path[0]] = replace(result.get(path[0]), path[1:], item)
    return result


class CallerCase(ProductionCase):
    def setUp(self):
        super().setUp()
        # This is the caller's first helper request, before any write.
        self.contracts = self.ok("contract", {"record": "all"})["records"]

    def ok(self, operation, request):
        code, result, output = entry(self, operation, request)
        self.assertEqual(code, 0, output)
        return result["result"]

    def refused(self, operation, request, code=None):
        result_code, result, output = entry(self, operation, request)
        self.assertEqual(result_code, 1, output)
        self.assertNotIn(SENTINEL, output)
        self.assertNotIn("Traceback", output)
        error = result["error"]
        if code:
            self.assertEqual(error["code"], code, error)
        return error

    def owned(self, **fields):
        return {"project": str(self.project), "objective": "objective", "owner": "owner", **fields}

    def map_read(self):
        return self.ok("map", {"project": str(self.project), "objective": "objective"})

    def initial(self):
        value = self.core(obligations=[self.criterion(), self.sub("S"), self.sub("T")],
                          governance={"base_ref": "target"})
        self.ok("checkpoint", self.owned(value=value))

    def raw_packet(self, **fields):
        snapshot = load(self.project)
        p = self.contracts["packet"]
        return {"schema": p["fields"]["schema"]["allowed"][0], "objective": "objective", "criteria": ["PoD#1"],
                "responsibility": "inspect parser", "scope": ["src"], "actions": ["read"], "candidate": self.candidate,
                "context": [], "dependencies": [], "route": {**ks.ROUTE, "preference_revision": snapshot["revision"]},
                "policy_revision": snapshot["policy_revision"], "plan_revision": "plan", "report_contract": "checks",
                "sources": [], "serves": ["S"], "role": "investigate", "boundary": {"paths": [], "surfaces": []},
                "map_revision": 1, "resolves": "which result?", "stop_condition": "answer", **fields}

    def frozen(self):
        return self.ok("packet", self.raw_packet(map_revision=self.map_read()["revision"]))

    def admit(self, packet, task="work", **fields):
        return self.ok("admission", self.owned(run="run", task=task, plan_revision="plan", packet=packet, **fields))["admission"]

    def report_request(self, packet, admission, **fields):
        r = self.contracts["report"]
        body = {key: [] for key, spec in r["fields"].items() if spec["type"] == "list"}
        body.update(schema=r["fields"]["schema"]["allowed"][0], assignment=packet["packet_id"],
                    attempt=admission["native_binding"]["dispatchId"], candidate=packet["body"]["candidate"],
                    outcome="succeeded", scope=packet["body"]["scope"])
        return {"project": str(self.project), "objective": "objective", "packet": packet,
                "admission_id": admission["admission_id"], "report": body, **fields}

    def assert_within(self, value, record):
        contract = self.contracts[record]
        self.assertFalse(set(value) - set(contract["accepted"]))
        for key, item in value.items():
            self.within(item, contract["fields"][key])

    def within(self, value, spec):
        if value is None and spec.get("nullable"):
            return
        if "max_length" in spec:
            self.assertLessEqual(len(value), spec["max_length"])
        if "max_items" in spec:
            self.assertLessEqual(len(value), spec["max_items"])
        if "allowed" in spec:
            self.assertIn(value, spec["allowed"])
        if "record" in spec:
            self.assert_within(value, spec["record"])
        if isinstance(value, list) and "items" in spec:
            for item in value:
                self.within(item, spec["items"])


class DiscoveryTests(CallerCase):
    def test_discovered_contract_builds_successful_first_requests(self):
        self.initial()
        packet_body = self.raw_packet(map_revision=self.map_read()["revision"])
        self.assert_within(packet_body, "packet")
        packet = self.ok("packet", packet_body)
        request = self.owned(run="run", task="first", plan_revision="plan", packet=packet)
        self.assert_within(request, "admission")
        admission = self.ok("admission", request)["admission"]
        self.settle(admission)
        current = self.map_read()
        request = self.report_request(packet, admission, map={"seq": current["next_seq"], "update": {
            "S": {"state": "satisfied", "evidence": [self.proof("S")]}}})
        self.assert_within(request["report"], "report")
        self.ok("report", request)
        current = self.map_read()
        value = self.core(seq=current["next_seq"], obligations=current["obligations"], acceptances=[{
            "obligation": "S", "source": "outside-answer", "verdict": "accepted", "candidate": current["candidate"],
            "evidence": "observed outside result"}])
        self.assert_within(value, "checkpoint")
        self.ok("checkpoint", self.owned(value=value))
        binding = self.ok("governor-prepare", self.owned(unit="default", branch={
            "remote": "origin", "base": "target", "branch": "delivery"}))["candidate"]
        action = {"kind": "merge", "candidate": binding["id"], "target": "origin/target", "reason": "deliver",
                  "effects": [], "authorization": ks.authorization(candidate=binding["commit"], tree=binding["tree"],
                    scope=("merge",), target="origin/target")}
        self.assert_within(action, "action")
        self.assertEqual(self.ok("governor", self.owned(action=action))["decision"], "ALLOW")
        self.assertFalse(self.remote.calls)

    def test_shape_and_text_refusals_give_whole_constant_contract_without_values(self):
        self.initial()
        for record, operation, request in (
            ("packet", "packet", {**self.raw_packet(), "responsibility": SENTINEL * 300}),
            ("packet", "packet", {**self.raw_packet(), "unknown": SENTINEL}),
            ("obligation", "checkpoint", self.owned(value=self.core(obligations=[self.criterion(check=SENTINEL * 50)]))),
            ("external", "checkpoint", self.owned(value=self.core(obligations=[self.criterion(state="blocked_external",
                external={"party": "user", "need": SENTINEL * 50, "unblocks_when": "provided"}), self.sub("S"), self.sub("T")], seq=2))),
        ):
            with self.subTest(record=record):
                error = self.refused(operation, request)
                detail = error["detail"].get("referent", error["detail"])
                contract = self.contracts[record]
                self.assertEqual(detail["accepted"], contract["accepted"])
                self.assertEqual(detail["optional"], contract["optional"])
                self.assertEqual(detail["bounded_fields"], {key: spec for key, spec in contract["fields"].items()
                                                           if list(limits(spec)) or "allowed" in spec or "pattern" in spec})
        self.assertGreater(len(self.contracts["obligation"]["accepted"]), 8)


class BoundTests(CallerCase):
    """One past each listed length/count, grouped by the record's production path."""

    def check_bounds(self, record, original, operation, wrap):
        for field, spec in self.contracts[record]["fields"].items():
            for path, kind, maximum in limits(spec):
                base = original
                if record == "context_reference" and field in ("path", "sha256"):
                    base = {"kind": "source", "path": "src/old.py", "sha256": "a" * 64}
                if record == "obligation" and field == "proof_scope":
                    base = self.sub("A", proof_scope=["src"])
                value = base.get(field)
                if value is None:
                    value = "value" if kind == "max_length" else []
                if kind == "max_length":
                    bad = "x" * (maximum + 1)
                    if record == "worktree_binding" and field == "path":
                        bad = "/" + "x" * maximum
                elif path and path[-1] in ("capacity",):
                    bad = ["reservation"] * (maximum + 1)
                elif spec["type"] == "object":
                    bad = {"input" + str(index): "value" for index in range(maximum + 1)}
                else:
                    exemplar = value[0] if isinstance(value, list) and value else "item"
                    bad = [deepcopy(exemplar) for _ in range(maximum + 1)]
                changed = {**deepcopy(base), field: replace(value, path, bad)}
                with self.subTest(record=record, field=field, path=path, bound=maximum):
                    self.refused(operation, wrap(changed))

    def test_packet_text_and_list_bounds(self):
        body = self.raw_packet(context=[{"kind": "summary", "id": "summary", "binding_digest": "a" * 64}],
                               sources=[{"path": "src/old.py", "state": "present", "sha256": "a" * 64}])
        self.ok("packet", body)
        self.check_bounds("packet", body, "packet", lambda value: value)

    def test_packet_nested_record_bounds(self):
        body = self.raw_packet(context=[{"kind": "summary", "id": "summary", "binding_digest": "a" * 64}],
                               sources=[{"path": "src/old.py", "state": "present", "sha256": "a" * 64}],
                               worktree={"repository": None, "repo_key": "a" * 64, "path": "/fixture", "branch": "main"})
        self.ok("packet", body)
        for record, field, listed in (("packet_route", "route", False), ("context_reference", "context", True),
                                      ("source", "sources", True), ("worktree_binding", "worktree", False)):
            self.check_bounds(record, body[field][0] if listed else body[field], "packet",
                              lambda value, field=field, listed=listed: {**body, field: [value] if listed else value})

    def checkpoint_bounds(self, record, row, field=None):
        self.initial()
        if field is None:
            original = self.core(seq=self.map_read()["next_seq"], obligations=self.stored())
            self.check_bounds(record, original, "checkpoint", lambda value: self.owned(value=value))
        else:
            self.check_bounds(record, row, "checkpoint", lambda value: self.owned(value=self.core(
                seq=self.map_read()["next_seq"], obligations=[self.criterion(), {**self.sub("S"), field: value}, self.sub("T")])))

    def test_checkpoint_and_map_collection_bounds(self):
        self.checkpoint_bounds("checkpoint", None)
        original = self.core(seq=self.map_read()["next_seq"], obligations=self.stored())
        self.check_bounds("map", original, "checkpoint", lambda value: self.owned(value=value))

    def test_obligation_text_and_collection_bounds(self):
        self.initial()
        row = {"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1", "check": "review",
               "scope": {"paths": []}, "question": "correct?", "candidate": self.base, "existing_evidence": "tests",
               "insufficiency": "review", "unit": "A", "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        self.check_bounds("obligation", row, "checkpoint", lambda value: self.owned(value=self.core(
            seq=self.map_read()["next_seq"], obligations=[self.criterion(), value, self.sub("S"), self.sub("T")])) )

    def test_boundary_and_wait_bounds(self):
        self.initial()
        for record, field, value in (("boundary", "boundary", {"paths": ["src"], "surfaces": ["api"]}),
                                      ("wait", "wait", {"class": "sequenced", "referent": "O1"})):
            self.check_bounds(record, value, "checkpoint", lambda value, field=field: self.owned(value=self.core(
                seq=self.map_read()["next_seq"], obligations=[self.criterion(), self.sub("S", **{field: value}), self.sub("T")])) )

    def test_external_and_withdrawal_bounds(self):
        self.initial()
        for record, state, field, value in (
            ("external", "blocked_external", "external", {"party": "user", "need": "input", "unblocks_when": "provided"}),
            ("withdrawal", "withdrawn", "withdrawal", {"by": "coordinator", "reason": "no longer needed"}),
        ):
            self.check_bounds(record, value, "checkpoint", lambda value, field=field, state=state: self.owned(value=self.core(
                seq=self.map_read()["next_seq"], obligations=[self.criterion(), self.sub("S", state=state, **{field: value}), self.sub("T")])) )

    def test_evidence_and_acceptance_bounds(self):
        self.initial()
        proof = self.proof("S")
        self.check_bounds("map evidence", proof, "checkpoint", lambda value: self.owned(value=self.core(
            seq=self.map_read()["next_seq"], obligations=[self.criterion(), self.sub("S", state="satisfied", evidence=[value]), self.sub("T")])) )
        acceptance = {"source": "answer", "candidate": self.candidate, "verdict": "accepted", "evidence": "observed"}
        self.check_bounds("acceptance", acceptance, "checkpoint", lambda value: self.owned(value=self.core(
            seq=self.map_read()["next_seq"], obligations=[self.criterion(), self.sub("S", state="satisfied", evidence=[proof]), self.sub("T")],
            acceptances=[{"obligation": "S", **value}])) )

    def test_short_evidence_and_acceptance_request_bounds(self):
        self.initial()
        proof = {"check": "unit", "command": "unit", "result": "observed", "reference": "log",
                 "sources": [], "dependencies": [], "environment": "fixture"}
        self.check_bounds("short evidence", proof, "checkpoint", lambda value: self.owned(value=self.core(
            seq=self.map_read()["next_seq"], obligations=[self.criterion(), self.sub("S", state="satisfied", evidence=[value]), self.sub("T")])) )
        acceptance = {"obligation": "S", "source": "answer", "candidate": self.candidate,
                      "verdict": "accepted", "evidence": "observed"}
        self.check_bounds("acceptance request", acceptance, "checkpoint", lambda value: self.owned(value=self.core(
            seq=self.map_read()["next_seq"], obligations=[self.criterion(), self.sub("S", state="satisfied", evidence=[proof]), self.sub("T")],
            acceptances=[value])) )

    def test_admission_and_its_full_map_bounds(self):
        self.initial()
        request = self.owned(run="run", task="work", plan_revision="plan", packet=self.frozen())
        self.check_bounds("admission", request, "admission", lambda value: value)
        current = self.map_read()
        self.check_bounds("admission map", {"seq": current["next_seq"], "obligations": current["obligations"]},
                          "admission", lambda value: {**request, "map": value})

    def test_disposition_bounds(self):
        self.initial()
        packet = self.ok("packet", self.raw_packet(role="implement", map_revision=self.map_read()["revision"]))
        admission = self.admit(packet)
        self.settle(admission)
        disposition = {"admission": admission["admission_id"], "discarded": True, "reason": "unused", "attestation": "no result"}
        self.check_bounds("disposition", disposition, "checkpoint", lambda value: self.owned(value=self.core(
            seq=self.map_read()["next_seq"], obligations=[self.criterion(), self.sub("S"), self.sub("T")], dispositions=[value])))

    def test_report_bounds(self):
        self.initial()
        packet = self.frozen()
        admission = self.admit(packet)
        self.settle(admission)
        request = self.report_request(packet, admission)
        self.check_bounds("report", request["report"], "report", lambda value: {**request, "report": value})

    def test_governor_action_and_diagnostic_bounds(self):
        self.initial()
        binding = self.ok("governor-prepare", self.owned(unit="default", branch={
            "remote": "origin", "base": "target", "branch": "delivery"}))["candidate"]
        action = {"kind": "remote_diagnostic", "candidate": binding["id"], "target": "probe.yml", "reason": "probe",
                  "effects": [], "evidence": ["log"], "inputs": {"input": "value"},
                  "authorization": ks.authorization(candidate=binding["commit"], tree=binding["tree"], scope=("publish",)),
                  "diagnostic": {"question": "which result?", "local_limitation": "remote actor", "check": "probe", "stopping_condition": "answer"}}
        self.check_bounds("action", action, "governor", lambda value: self.owned(action=value))
        self.check_bounds("diagnostic", action["diagnostic"], "governor", lambda value: self.owned(action={**action, "diagnostic": value}))

    def test_verification_proposal_and_authority_bounds(self):
        self.initial()
        for record, field, original in (
            ("verification", "verification", {"dependencies": ["dependency"], "environment": "fixture"}),
            ("proposal", "proposals", {"id": "P", "source": "coordinator_caution", "origin_ref": "conversation", "summary": "risk", "status": "open"}),
            ("revision authority", "revision_authority", {"provenance": "user_direct", "instruction": "Continue"}),
            ("governance", "governance", {"base_ref": "target"}),
        ):
            self.check_bounds(record, original, "checkpoint", lambda value, field=field: self.owned(value=self.core(
                seq=self.map_read()["next_seq"], obligations=self.stored(),
                **{field: [value] if field == "proposals" else value})))

    def test_provenance_source_text_bounds(self):
        self.initial()
        self.check_bounds("objective source", {"ref": "PoD#1"}, "checkpoint",
            lambda value: {**self.owned(value=self.core(obligations=[self.criterion(source=value)],
                governance={"base_ref": "target"})), "objective": "fresh-intake"})
        source = {"instruction": "record the user requirement", "ref": "conversation"}
        self.check_bounds("user source", source, "checkpoint", lambda value: self.owned(value=self.core(
            seq=self.map_read()["next_seq"], obligations=[*self.stored(), {
                "id": "U", "kind": "steer", "provenance": "user_direct", "source": value, "check": "fulfilled",
                "state": "blocked_external", "external": {"party": "user", "need": "input", "unblocks_when": "provided"}}],
            revision_authority=ks.USER)))

    def test_policy_citation_line_bound(self):
        (self.project / "AGENTS.md").write_text("Required candidate review.\n" * 65)
        ks.git(self.project, "add", "AGENTS.md")
        ks.git(self.project, "commit", "-qm", "trusted review rule")
        self.base = self.candidate = ks.git(self.project, "rev-parse", "HEAD")
        ks.git(self.project, "branch", "-f", "target", self.base)
        ks.git(self.project, "update-ref", "refs/remotes/origin/target", self.base)
        maximum = self.contracts["policy citation"]["fields"]["lines"]["max_lines"]
        audit = ks.governance_policy(self, lines="1-" + str(maximum))
        self.ok("checkpoint", self.owned(value=self.core(obligations=[self.criterion(), audit],
            governance={"base_ref": "target"})))
        audit = ks.governance_policy(self, "next-audit", lines="1-" + str(maximum + 1), scope={"paths": ["README.md"]})
        self.refused("checkpoint", self.owned(value=self.core(seq=self.map_read()["next_seq"],
            obligations=[*self.stored(), audit])), "obligation_invalid")

    def test_packet_total_bound(self):
        body = self.raw_packet(criteria=["a" * 512] * 64, dependencies=["b" * 450] * 64)
        maximum = self.contracts["packet"]["max_repr_length"]
        body["responsibility"] += "r" * (maximum + 1 - len(str(body)))
        self.assertEqual(len(str(body)), maximum + 1)
        self.assert_within(body, "packet")
        self.refused("packet", body, "invalid_packet")


class RepairTests(CallerCase):
    def test_text_shape_and_stored_only_refusals_are_repaired_from_their_contract(self):
        self.initial()
        body = self.raw_packet(responsibility=SENTINEL * 300)
        error = self.refused("packet", body, "invalid_responsibility")
        maximum = error["detail"]["bounded_fields"]["responsibility"]["max_length"]
        body["responsibility"] = "repair parser"[:maximum]
        self.ok("packet", body)
        body.pop("report_contract")
        body["stored_only"] = SENTINEL
        error = self.refused("packet", body, "invalid_packet")
        for key in error["detail"]["unsupported"]:
            body.pop(key)
        for key in error["detail"]["missing"]:
            body[key] = "checks"
        self.ok("packet", body)
        packet = self.frozen()
        current = self.map_read()
        request = self.owned(run="run", task="work", plan_revision="plan", packet=packet,
                             map={"seq": current["next_seq"], "revision": current["revision"]})
        error = self.refused("admission", request, "obligation_invalid")
        accepted = error["detail"]["referent"]["accepted"]
        request["map"] = {key: value for key, value in request["map"].items() if key in accepted}
        self.ok("admission", request)

    def mismatch(self, field):
        self.initial()
        current = self.map_read()
        body = self.raw_packet(map_revision=current["revision"], **{field: [SENTINEL] if field == "criteria" else SENTINEL})
        packet = self.ok("packet", body)
        request = self.owned(run="run", task="repair", plan_revision="plan", packet=packet)
        error = self.refused("admission", request, "packet_plan_mismatch")
        self.assertEqual(error["detail"]["fields"], [field])
        self.assertEqual(error["detail"]["reads"][field], "pod internal map: " + field)
        current = self.map_read()
        body[field] = current[field]
        request["plan_revision"] = current["plan_revision"]
        request["packet"] = self.ok("packet", body)
        self.ok("admission", request)

    def test_candidate_mismatch_repair(self):
        self.mismatch("candidate")

    def test_criteria_mismatch_repair(self):
        self.mismatch("criteria")

    def test_plan_revision_mismatch_repair(self):
        self.mismatch("plan_revision")

    def test_worktree_mismatch_repair(self):
        from pod.github import repository_context
        context = self.ok("project-context", {"project": str(self.project)})
        worktree = {"repository": context["repository"], "repo_key": context["repo_key"],
                    "path": context["worktree"], "branch": context["branch"]}
        self.ok("checkpoint", self.owned(value=self.core(obligations=[self.criterion(), self.sub("S")],
            governance={"base_ref": "target"}, worktree=worktree)))
        body = self.raw_packet(map_revision=self.map_read()["revision"])
        packet = self.ok("packet", body)
        request = self.owned(run="run", task="repair", plan_revision="plan", packet=packet)
        error = self.refused("admission", request, "packet_plan_mismatch")
        self.assertEqual(error["detail"]["fields"], ["worktree"])
        body["worktree"] = self.map_read()["worktree"]
        request["packet"] = self.ok("packet", body)
        self.ok("admission", request)

    def test_objective_source_mismatch_repair_uses_named_reads(self):
        from tests.test_github import IssuePort
        with patch("pod.github.GhPort", return_value=IssuePort()):
            source = self.ok("issue-intake", {"project": str(self.project),
                "locator": "https://github.com/acme/example/issues/7"})["source"]
            self.ok("checkpoint", self.owned(value=self.core(obligations=[self.criterion(), self.sub("S")],
                governance={"base_ref": "target"}, objective_source=source)))
            body = self.raw_packet(map_revision=self.map_read()["revision"])
            request = self.owned(run="run", task="repair", plan_revision="plan", packet=self.ok("packet", body))
            error = self.refused("admission", request, "packet_plan_mismatch")
            self.assertEqual(error["detail"]["fields"], ["objective_source"])
            body["objective_source"] = self.map_read()["objective_source"]
            request["packet"] = self.ok("packet", body)
            self.ok("admission", request)

    def test_reservation_packet_id_mismatch_names_the_freeze_without_echoing_it(self):
        # guarded_start verifies a supplied identity before reserve; exercise that
        # serialized production boundary directly without bypassing its checks.
        from pod.errors import PodError
        from pod.ledger import admission_identity, reserve
        self.initial()
        packet = self.frozen()
        current = self.map_read()
        with self.assertRaises(PodError) as caught:
            reserve(self.project, "objective", owner="owner", admission_id=admission_identity(
                        objective="objective", run_id="run", task_id="repair"),
                    requested=packet["body"]["route"],
                    native_reader=lambda state: self.port.read_native("owner", authority_runs=("run",),
                        assignments=tuple(state["admissions"].values())), run_id="run", task_id="repair",
                    plan_revision=current["plan_revision"], packet_id=SENTINEL, worktree="current",
                    frozen_packet=packet, expected_runtime="runtime", placement_binding=self.port.placement)
        error = caught.exception
        self.assertEqual(error.code, "packet_plan_mismatch")
        self.assertEqual(error.detail["fields"], ["packet_id"])
        self.assertIn("pod internal packet", error.detail["reads"]["packet_id"])
        self.assertNotIn(SENTINEL, str(error) + json.dumps(error.detail))
        repaired = self.ok("packet", packet["body"])
        self.admit(repaired, task="repair")

    def test_invalid_frozen_packet_id_keeps_the_prior_entry_code(self):
        self.initial()
        packet = self.frozen()
        request = self.owned(run="run", task="repair", plan_revision="plan", packet={**packet, "packet_id": SENTINEL})
        self.refused("admission", request, "invalid_packet")
        request["packet"] = self.ok("packet", packet["body"])
        self.ok("admission", request)

    def test_plan_mismatch_names_all_differences(self):
        self.initial()
        body = self.raw_packet(candidate=SENTINEL, criteria=[SENTINEL], plan_revision=SENTINEL)
        request = self.owned(run="run", task="repair", plan_revision="plan", packet=self.ok("packet", body))
        error = self.refused("admission", request, "packet_plan_mismatch")
        self.assertEqual(set(error["detail"]["fields"]), {"candidate", "criteria", "plan_revision"})
        current = self.map_read()
        for field in error["detail"]["fields"]:
            body[field] = current[field]
        request["packet"] = self.ok("packet", body)
        self.ok("admission", request)

    def settled_request(self, operation):
        self.initial()
        packet = self.ok("packet", self.raw_packet(serves=["S", "T"], map_revision=self.map_read()["revision"]))
        admission = self.admit(packet)
        self.settle(admission)
        if operation == "report":
            request = self.report_request(packet, admission)
        elif operation == "admission":
            body = self.raw_packet(serves=["O1"], map_revision=self.map_read()["revision"])
            request = self.owned(run="run", task="next-work", plan_revision="plan", packet=self.ok("packet", body))
        else:
            request = self.owned(value=self.core(seq=self.map_read()["next_seq"], obligations=self.map_read()["obligations"]))
        error = self.refused(operation, request, "obligation_unaccounted")
        detail = error["detail"]
        self.assertEqual(detail["detail"], "active_admission_settled")
        self.assertEqual(detail["referent"]["restate"], ["S", "T"])
        current = self.map_read()
        updates = {key: {"state": "blocked_external", "external": {"party": "user", "need": "input", "unblocks_when": "provided"}}
                   for key in detail["referent"]["restate"]}
        if operation == "report":
            self.assertIn("same report request", detail["next_action"])
            request["map"] = {"seq": current["next_seq"], "update": updates}
        else:
            rows = current["obligations"]
            for row in rows:
                if row["id"] in updates:
                    row.pop("executor", None)
                    row.update(updates[row["id"]])
            if operation == "admission":
                self.assertIn("full obligations restatement", detail["next_action"])
                request["map"] = {"seq": current["next_seq"], "obligations": rows}
            else:
                self.assertIn("checkpoint", detail["next_action"])
                request["value"].update(seq=current["next_seq"], obligations=rows)
        self.ok(operation, request)

    def test_report_restates_settled_executors_with_update_in_same_request(self):
        self.settled_request("report")

    def test_admission_restates_full_obligations_in_same_request(self):
        self.settled_request("admission")

    def test_checkpoint_restates_settled_executors_in_its_map(self):
        self.settled_request("checkpoint")

    def test_wait_refusal_names_every_restatement_up_to_the_map_bound(self):
        count = self.contracts["map"]["fields"]["obligations"]["max_items"]
        rows = [self.criterion(), *[self.sub("D" + str(index), wait={"class": "dependency", "referent": "O1"})
                                   for index in range(count - 1)]]
        self.ok("checkpoint", self.owned(value=self.core(obligations=rows, governance={"base_ref": "target"})))
        current = self.map_read()
        rows = current["obligations"]
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        request = self.owned(value=self.core(seq=current["next_seq"], obligations=rows))
        error = self.refused("checkpoint", request, "wait_invalid")
        restate = error["detail"]["referent"]["restate"]
        self.assertEqual(restate, ["D" + str(index) for index in range(count - 1)])
        self.assertEqual(error["detail"]["dependents"], restate)
        self.assertEqual(error["detail"]["remaining"], 0)
        request["value"]["seq"] = self.map_read()["next_seq"]
        for row in rows:
            if row["id"] in restate:
                row.pop("wait")
                row.update(state="blocked_external", external={"party": "user", "need": "input", "unblocks_when": "provided"})
        self.ok("checkpoint", request)

    def test_policy_mismatch_from_different_project_is_repaired_by_named_config_and_map_reads(self):
        from contextlib import chdir, redirect_stdout
        from io import StringIO
        from pod.cli import main as cli_main
        self.initial()
        sibling = self.root / "other-project"
        sibling.mkdir()
        (sibling / ".pod").mkdir()
        (sibling / ".pod/config.yaml").write_text("schema: pod/v1\nwaste_governor:\n  preflight: [other-check]\n")
        output = StringIO()
        with chdir(sibling), redirect_stdout(output):
            self.assertEqual(cli_main(["config", "--json"]), 0)
        other = json.loads(output.getvalue())
        body = self.raw_packet(policy_revision=other["policy_revision"], map_revision=self.map_read()["revision"])
        request = self.owned(run="run", task="repair", plan_revision="plan", packet=self.ok("packet", body))
        error = self.refused("admission", request, "policy_revision_mismatch")
        self.assertEqual(error["detail"]["policy_digests"]["packet"], other["policy_revision"])
        self.assertIn("inside the objective's project", error["detail"]["next_action"])
        output = StringIO()
        with chdir(self.project), redirect_stdout(output):
            self.assertEqual(cli_main(["config", "--json"]), 0)
        config = json.loads(output.getvalue())
        current = self.map_read()
        self.assertEqual(current["policy_revision"], config["policy_revision"])
        body["policy_revision"] = current["policy_revision"]
        request["packet"] = self.ok("packet", body)
        self.ok("admission", request)

    def test_checkpoint_policy_mismatch_names_all_known_digests_and_refreshes(self):
        from contextlib import chdir, redirect_stdout
        from io import StringIO
        from pod.cli import main as cli_main
        self.initial()
        original = self.map_read()
        (self.project / ".pod").mkdir()
        (self.project / ".pod/config.yaml").write_text("schema: pod/v1\nwaste_governor: {preflight: [unit]}\n")
        body = self.raw_packet(map_revision=original["revision"])
        request = self.owned(run="run", task="repair", plan_revision="plan", packet=self.ok("packet", body))
        error = self.refused("admission", request, "policy_revision_mismatch")
        digests = error["detail"]["policy_digests"]
        self.assertEqual(digests, {"checkpoint": original["policy_revision"], "packet": body["policy_revision"],
                                   "effective": body["policy_revision"]})
        self.assertEqual((error["detail"]["expected"], error["detail"]["received"]),
                         (digests["effective"], digests["checkpoint"]))
        output = StringIO()
        with chdir(self.project), redirect_stdout(output):
            self.assertEqual(cli_main(["config", "--json"]), 0)
        policy_revision = json.loads(output.getvalue())["policy_revision"]
        current = self.map_read()
        self.ok("checkpoint", self.owned(value=self.core(seq=current["next_seq"], obligations=current["obligations"],
                                                        policy_revision=policy_revision)))
        current = self.map_read()
        body.update(policy_revision=current["policy_revision"], map_revision=current["revision"])
        request["packet"] = self.ok("packet", body)
        self.ok("admission", request)

    def test_pending_replay_from_other_project_names_digests_and_recovers_in_objective_project(self):
        from contextlib import chdir, redirect_stdout
        from io import StringIO
        from pod.cli import main as cli_main
        self.initial()
        packet = self.frozen()
        self.port.receipt = {"runtime": "runtime", "exit": 1, "request_uuid": ks.REQUEST,
                             "error": {"code": "transport_lost", "message": "lost"}}
        self.port.state = "pending"
        admission = self.admit(packet)
        self.assertEqual(admission["state"], "unresolved")
        sibling = self.root / "other-worktree"
        ks.git(self.project, "worktree", "add", "-qb", "other", str(sibling))
        (sibling / ".pod").mkdir()
        (sibling / ".pod/config.yaml").write_text("schema: pod/v1\nwaste_governor: {preflight: [other-check]}\n")
        request = self.owned(run="run", task="work", plan_revision="plan", packet=packet)
        request["project"] = str(sibling)
        error = self.refused("admission", request, "policy_revision_mismatch")
        self.assertEqual(error["detail"]["policy_digests"]["packet"], packet["body"]["policy_revision"])
        self.assertNotEqual(error["detail"]["expected"], error["detail"]["received"])
        output = StringIO()
        with chdir(self.project), redirect_stdout(output):
            self.assertEqual(cli_main(["config", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["policy_revision"], self.map_read()["policy_revision"])
        request["project"] = str(self.project)
        self.assertEqual(self.ok("admission", request)["status"], "unresolved")
        self.assertEqual(self.port.starts[-1]["retry_request"], admission["request_uuid"])
        self.assertTrue(all(row["task"] == "work" for row in self.port.starts))
