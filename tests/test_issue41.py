"""0.8.0 A1-A12 production boundary regressions (synthetic ports, no live claims)."""
import ast
from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import unittest
from unittest.mock import patch

from pod import internal
from pod.errors import PodError
from pod.ledger import read, objective_root
from pod.util import digest
from pod.orca import read_command as real_read_command
from tests.issue41_support import ProductionCase
from tests.kernel_support import git, assurance_review, assurance_satisfied


class GovernorBoundaryTests(ProductionCase):
    def test_decide_marks_only_executable_reservations_and_execute_attaches(self):
        self.prepare()
        for kind in ("push", "pr_update", "remote_diagnostic"):
            with self.subTest(kind=kind):
                action = self.action(kind)
                decided = self.op("governor", action=action)
                self.assertEqual(decided["decision"], "ALLOW", decided)
                self.assertFalse(decided["executes"])
                self.assertIn("reserved", decided["next_action"])
                attached = self.op("governor-execute", action=action)
                self.assertEqual(attached["reuse"]["kind"], "attach")
                self.assertEqual(attached["record_id"], decided["record_id"])
                self.assertIn("CANCELED", attached["next_action"])
                self.assertFalse(self.remote.calls)
                self.op("governor-outcome", record_id=decided["record_id"], outcome="CANCELED")
        executed = self.op("governor-execute", action=self.action())
        self.assertEqual(executed["outcome"], "PASS")
        self.assertEqual(len([call for call in self.remote.calls if call[0] == "push"]), 1)
        journal = json.loads((objective_root(self.project, "objective") / "governor.json").read_text())
        self.assertTrue(all(row.get("decision_reserved") is True for row in journal["actions"][:-1]))
        self.assertNotIn("decision_reserved", journal["actions"][-1])

    def test_effect_and_pull_request_validation_precedes_journal_and_effect(self):
        self.prepare()
        journal = objective_root(self.project, "objective") / "governor.json"
        before = journal.read_bytes()
        cases = [(self.action(effects=["workflow:.github/workflows/ci.yml"]), {}),
                 (self.action("workflow_dispatch", target=".github/workflows/ci.yml"), {}),
                 (self.action("validation_rerun", target=".github/workflows/ci.yml"), {}),
                 (self.action("remote_diagnostic", target=".github/workflows/ci.yml"), {}),
                 (self.action("pr_update", reason="t" * 257), {}),
                 (self.action("pr_update"), {"pull_request": {"body": "b" * 16385}}),
                 (self.action("pr_update"), {"pull_request": {"draft": True}})]
        for action, fields in cases:
            with self.subTest(action=action["kind"], fields=list(fields)), self.assertRaises(PodError):
                self.op("governor-execute", action=action, **fields)
            self.assertEqual(journal.read_bytes(), before)
            self.assertFalse(self.remote.calls)
        for action, _ in cases[:5]:
            with self.assertRaises(PodError): self.op("governor", action=action)
            self.assertEqual(journal.read_bytes(), before)

    def test_landed_push_read_failure_is_pass_and_pr_lookup_failure_unknown(self):
        self.prepare()
        original = self.remote.runs
        def unread(**kwargs): raise PodError("invalid_workflow", "private remote message")
        self.remote.runs = unread
        executed = self.op("governor-execute", action=self.action("pr_update", effects=["workflow:ci.yml"]))
        self.assertEqual(executed["outcome"], "PASS")
        self.assertIn("ci.yml", executed["receipt"]["detail"])
        self.assertEqual(len([call for call in self.remote.calls if call[0] == "pr_create"]), 1)
        self.assertNotIn("private remote message", str(executed))
        self.remote.runs = original
        self.move(); self.write()
        candidate = self.op("governor-prepare", unit="default")["candidate"]
        self.authorization = {
            "schema": "pod-authorization/v1", "candidate": candidate["commit"], "tree": candidate["tree"],
            "scope": ["publish"], "authorized_by": "owner", "utc": "2026-01-01T00:00:00Z", "reference": "next"}
        def lookup(**kwargs): raise PodError("invalid_remote", "private remote message")
        self.remote.pull_request = lookup
        second = self.op("governor-execute", action=self.action("pr_update"))
        self.assertEqual(second["outcome"], "UNKNOWN")
        push = [call for call in self.remote.calls if call[0] == "push"][-1]
        self.assertEqual(push[-1], self.base)
        self.assertEqual(len([call for call in self.remote.calls if call[0] == "pr_create"]), 1)
        settled = self.op("governor-reconcile", record_id=second["record_id"])
        self.assertEqual(settled["status"], "PASS")


class ProofBoundaryTests(ProductionCase):
    def satisfied_rows(self):
        rows = [self.criterion(boundary={"paths": ["src"]}, proof_scope=["README.md"]),
                self.sub("S", proof_scope=[]), self.sub("T", boundary={"paths": ["src"]}), self.sub("N")]
        self.write(rows, governance={"base_ref": "target"})
        rows = self.stored()
        for row in rows:
            row.pop("executor", None); row.pop("wait", None)
            row.update(state="satisfied", evidence=[self.proof(row["id"])])
        self.write(rows)
        return rows

    def test_complete_invalidation_and_explicit_reuse_list_without_new_receipts(self):
        self.satisfied_rows(); self.move("src/old.py")
        before = read(self.project, "objective")
        with self.assertRaises(PodError) as caught: self.write()
        self.assertEqual(caught.exception.detail["detail"], "evidence_invalidated")
        listed = caught.exception.detail["referent"]["invalidations"]
        self.assertEqual({item["obligation"]: item["eligibility"] for item in listed},
                         {"O1": "eligible", "S": "eligible", "T": "touched", "N": "no declared scope"})
        self.assertEqual(read(self.project, "objective"), before)
        rows = self.stored()
        for row in rows:
            if row["id"] in ("T", "N"):
                row.update(state="blocked_external", external={"party": "user", "need": "fresh proof", "unblocks_when": "supplied"})
        written = self.write(rows, reuse={"from": self.base, "ids": ["O1", "S"]})
        current = written["checkpoint"]["obligations"]
        self.assertEqual(len(next(row for row in current if row["id"] == "O1")["receipts"]), 1)
        self.move("src/old.py"); self.write()
        self.move("README.md")
        with self.assertRaises(PodError) as caught: self.write()
        self.assertIn("touched", str(caught.exception))

    def test_scope_shape_and_definition_binding(self):
        for row in (self.criterion(proof_scope=[]), self.sub("S", proof_scope=["../outside"]),
                    self.sub("S", proof_scope="README.md")):
            with self.assertRaises(PodError): self.write([row], governance={"base_ref": "target"})
        self.satisfied_rows()
        with self.assertRaises(PodError): self.write(reuse={"from": self.base, "ids": []})
        rows = self.stored(); rows[0]["proof_scope"] = ["src"]
        with self.assertRaises(PodError) as caught:
            self.write(rows)
        self.assertIn("definition change", str(caught.exception))

    def test_brief_uses_same_candidate_and_verification_without_writes(self):
        row = self.criterion(state="satisfied", evidence=[{"check": "check", "command": "check", "result": "pass", "reference": "initial"}])
        value = self.core(obligations=[row], governance={"base_ref": "target"})
        args = {"criteria": ["PoD#1"], "coverage": [{"criterion": "PoD#1", "check": "check"}],
                "project": str(self.project), "map": {"obligations": [row], "governance": value["governance"]},
                "candidate": self.base, "verification": value["verification"]}
        brief = internal.run("brief", args)
        self.assertIsNone(read(self.project, "objective"))
        written = self.op("checkpoint", value=value)
        for field in ("definition", "state"):
            self.assertEqual(brief["map"]["obligations"][0][field], written["checkpoint"]["obligations"][0][field])
        with self.assertRaises(PodError) as caught: internal.run("brief", {k:v for k,v in args.items() if k != "candidate"})
        self.assertIn("candidate", str(caught.exception)); self.assertIn("O1", str(caught.exception))
        for candidate in ("HEAD", "main", self.base[:7]):
            with self.assertRaises(PodError) as caught: self.write(candidate=candidate)
            self.assertIn(self.base, str(caught.exception))
            with self.assertRaises(PodError): internal.run("brief", {**args, "candidate": candidate})


class FindingBoundaryTests(ProductionCase):
    review = assurance_review
    def intake_review(self):
        self.intake({"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1", "check": "review",
          "scope": {"paths": ["src"]}, "question": "correct?", "candidate": self.base, "existing_evidence": "tests",
          "insufficiency": "no review", "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}})
        frozen = self.packet(["A"], role="review"); admission = self.start("review", frozen)["admission"]
        self.settle(admission)
        return frozen, admission

    def test_shared_correction_preserves_ids_severity_and_resolution(self):
        frozen, admission = self.intake_review()
        rows = self.stored(); rows[1].pop("executor"); rows[1].update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        rows.append({"id":"C", "kind":"correction", "provenance":"coordinator", "parent":"A", "check":"fix", "state":"waiting", "wait":{"class":"sequenced", "referent":"O1"}})
        self.report(admission, frozen, map={"obligations": rows}, triage=[
          {"finding":"F1", "severity":"major", "triage":"required_correction", "summary":"root", "correction":"C"},
          {"finding":"F2", "severity":"minor", "triage":"required_correction", "summary":"sibling", "correction":"C"}])
        rows = self.stored(); c = next(row for row in rows if row["id"] == "C")
        self.assertEqual(c["finding"]["findings"], ["F1", "F2"]); self.assertEqual(c["finding"]["severity"], "major")
        c.pop("wait"); c.update(state="satisfied", evidence=[self.proof("C")]); self.write(rows)
        a = next(row for row in self.stored() if row["id"] == "A")
        self.assertTrue(all("resolved_seq" in finding for finding in a["findings"]))
        a.pop("wait"); a.update(state="satisfied", evidence=[{"attempt":admission["admission_id"]}])
        with self.assertRaises(PodError): self.write([row if row["id"] != "A" else a for row in self.stored()])

    def test_grouped_findings_preserve_all_ids_and_replay_without_regrouping(self):
        frozen, admission = self.intake_review()
        rows = self.stored(); rows[1].pop("executor"); rows[1].update(state="waiting", wait={"class":"sequenced", "referent":"O1"})
        group = {"findings":[{"finding":f"F{i}", "severity":"major" if i == 0 else "minor"} for i in range(8)],
                 "triage":"advisory", "summary":"one root cause", "root_cause":"shared predicate", "reason":"bounded residual concern"}
        self.report(admission, frozen, map={"obligations":rows}, triage=[group])
        a = self.stored()[1]; self.assertEqual(len(a["findings"]), 1)
        self.assertEqual(a["findings"][0]["findings"], [f"F{i}" for i in range(8)])
        before = read(self.project, "objective")
        self.report(admission, frozen, triage=[group])
        self.assertEqual(read(self.project, "objective"), before)
        for altered in ({**group,"findings":group["findings"] + [{"finding":"F8", "severity":"minor"}]},
                        {**group,"findings":group["findings"][:2]}, {k:v for k,v in group.items() if k != "reason"}):
            with self.assertRaises(PodError): self.report(admission, frozen, triage=[altered])


class ReleasedWriterTests(ProductionCase):
    def released(self, abbreviated=False):
        source = self.root / "released"; source.mkdir()
        archive = self.root / "released.tar"
        repository = Path(__file__).resolve().parents[1]
        subprocess.run(["git", "-C", str(repository), "archive", "-o", str(archive),
                        "2f02be99f27e6a860c3115a9c62539b0812bc038", "skills", "VERSION"], check=True)
        with tarfile.open(archive) as bundle:
            bundle.extractall(source, filter="data")
        destination = self.root / "fixture"; destination.mkdir()
        command = [sys.executable, str(repository / "tests/issue41_released_writer.py"), str(destination)]
        if abbreviated: command.append("--abbreviated")
        result = subprocess.run(command, cwd=repository, env={**os.environ,
            "PYTHONPATH": os.pathsep.join((str(source / "skills"), str(repository)))}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((destination / "provenance.json").read_text())["version"], "0.7.1")
        return source, destination

    def restore(self, destination):
        self.project = destination / "project"
        self.base = self.candidate = git(self.project, "rev-parse", "HEAD")
        target = objective_root(self.project, "objective") / "context.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((destination / "context.json").read_bytes())
        if (destination / "port.json").exists():
            self.port.workers = json.loads((destination / "port.json").read_text())

    def test_genuine_full_id_071_proof_survives_checkpoint_and_report(self):
        _, fixture = self.released(); self.restore(fixture)
        before = read(self.project,"objective")
        definitions = {row["id"]:row["definition"] for row in before["checkpoint"]["obligations"]}
        checkpoint = self.write()["checkpoint"]
        self.assertEqual({row["id"]:row["definition"] for row in checkpoint["obligations"]}, definitions)
        self.assertTrue(all(row["state"] == "satisfied" for row in checkpoint["obligations"]))
        frozen=json.loads((fixture / "packet.json").read_text());admission=json.loads((fixture / "admission.json").read_text())
        result=self.report(admission,frozen,map={"seq":checkpoint["seq"]+1})
        self.assertEqual(result["report"]["label"]["label"],"QUALIFIED")
        self.assertEqual({row["id"]:row["definition"] for row in self.stored()},definitions)
        self.assertNotIn("findings",next(row for row in self.stored() if row["id"]=="C")["finding"])

    def test_genuine_abbreviated_071_proof_is_listed_on_full_id_restatement(self):
        _, fixture = self.released(abbreviated=True); self.restore(fixture)
        with self.assertRaises(PodError) as caught: self.write()
        self.assertEqual(caught.exception.detail["detail"],"evidence_invalidated")
        self.assertEqual(caught.exception.detail["referent"]["invalidations"][0]["obligation"],"O1")
        rows=self.stored(); rows[0].update(state="blocked_external",external={"party":"user","need":"fresh proof","unblocks_when":"provided"})
        self.write(rows)
        self.assertEqual(read(self.project,"objective")["checkpoint"]["candidate"],self.base)

    def test_071_validator_loads_new_marked_journal(self):
        source,_ = self.released(abbreviated=True)
        self.prepare(); self.op("governor",action=self.action())
        journal=objective_root(self.project,"objective") / "governor.json"
        result=subprocess.run([sys.executable,"-c",
          "from pathlib import Path; import sys; from pod.governor import _read_journal; print(_read_journal(Path(sys.argv[1]))['actions'][0]['decision_reserved'])",
          str(journal)],cwd=source,env={**os.environ,"PYTHONPATH":str(source / "skills")},capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout.strip(),"True")


class DiagnosticsBoundaryTests(ProductionCase):
    def cli(self, *argv):
        from pod.cli import main
        output,errors=StringIO(),StringIO(); previous=Path.cwd()
        try:
            os.chdir(self.project)
            with redirect_stdout(output),redirect_stderr(errors): code=main(list(argv))
        finally: os.chdir(previous)
        return code,output.getvalue(),errors.getvalue()

    def test_every_bare_kernel_refusal_has_an_explicit_referent(self):
        from pod import obligations,assurance
        for module in (obligations,assurance):
            tree=ast.parse(Path(module.__file__).read_text())
            for node in ast.walk(tree):
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id == "refuse":
                    self.assertTrue(any(key.arg != "next_action" for key in node.keywords),
                                    f"{module.__name__}:{node.lineno} has no referent")

    def test_kernel_fields_and_vocabulary_never_echo_values(self):
        sentinel="VALUE-SHOULD-NOT-ESCAPE"
        cases=[([{"unknown":sentinel}],{}),([self.criterion(check=sentinel*60)],{}),
               ([self.criterion(kind=sentinel)],{}),([self.criterion(provenance=sentinel)],{}),
               ([self.criterion(state=sentinel)],{}),
               ([self.criterion(boundary={"unexpected":sentinel})],{}),
               ([self.criterion(source={"unexpected":sentinel})],{}),
               ([self.criterion()],{"governance":{"unexpected":sentinel}}),
               ([self.criterion()],{"revision_authority":{"unexpected":sentinel}}),
               ([self.criterion()],{"proposals":[{"unexpected":sentinel}]}),
               ([self.criterion(state="blocked_external",external={"unexpected":sentinel})],{}),
               ([self.criterion(state="waiting",wait={"class":"dependency","unexpected":sentinel})],{}),
               ([self.criterion(state="waiting",wait={"class":sentinel,"referent":"O1"})],{}),
               ([self.criterion(state="waiting",wait={"class":"authority","referent":{"unexpected":sentinel}})],{})]
        for rows,fields in cases:
            request={"project":str(self.project),"objective":"objective","owner":"owner",
                     "value":self.core(obligations=rows,governance={"base_ref":"target"},**fields) if "governance" not in fields else self.core(obligations=rows,**fields)}
            path=self.root / "input.json";path.write_text(json.dumps(request))
            output,errors=StringIO(),StringIO()
            with self.subTest(fields=fields,rows=rows),redirect_stdout(output),redirect_stderr(errors):
                self.assertEqual(internal.main(["checkpoint","--input",str(path)]),1)
            envelope=json.loads(output.getvalue());self.assertTrue(envelope["error"]["detail"].get("referent"))
            self.assertNotIn(sentinel,output.getvalue()+errors.getvalue())

    def test_wait_refusal_lists_all_dependents_and_retry_restates_them_once(self):
        subs=[self.sub(f"S{i}",state="waiting",wait={"class":"dependency","referent":"O1"}) for i in range(10)]
        self.intake(*subs)
        rows=self.stored();rows[0].pop("executor");rows[0].update(state="satisfied",evidence=[self.proof("O1")])
        with self.assertRaises(PodError) as caught:self.write(rows)
        self.assertEqual(caught.exception.detail["dependents"],[f"S{i}" for i in range(8)])
        self.assertEqual(caught.exception.detail["remaining"],2)
        self.assertEqual(caught.exception.detail["referent"]["obligation"],"S0")
        self.assertEqual(caught.exception.detail["referent"]["referent"],"O1")
        for row in rows[1:]:
            row.pop("wait");row.update(state="blocked_external",external={"party":"user","need":"input","unblocks_when":"provided"})
        self.write(rows)

    def test_admission_refusal_names_update_and_same_task_recovery(self):
        self.intake(self.sub("S"))
        frozen=self.packet(["S"])
        with self.assertRaises(PodError) as caught:self.start("work",frozen,accompanying={"update":{}})
        self.assertIn("update",str(caught.exception));self.assertIn("same Task",str(caught.exception))
        admitted=self.start("work",frozen)["admission"]
        with self.assertRaises(PodError) as caught:self.start("work",self.packet(["S"],responsibility="other"))
        self.assertNotIn("same Task",str(caught.exception))

    def test_placement_corrections_name_placement_and_isolated_assignment_admits(self):
        from pod.github import repository_context
        context=repository_context(self.project)
        binding={"repository":context["repository"],"repo_key":context["repo_key"],"path":context["worktree"],"branch":context["branch"]}
        self.intake(self.sub("S"),worktree=binding)
        git(self.project,"worktree","add","-qb","child",str(self.root / "child"),self.base)
        child_context=repository_context(self.root / "child")
        child={"repository":child_context["repository"],"repo_key":child_context["repo_key"],"path":child_context["worktree"],"branch":child_context["branch"]}
        self.port.placement={**child,"runtime":"runtime"}
        with self.assertRaises(PodError) as caught:self.start("work",self.packet(["S"],worktree=binding),worktree="child")
        self.assertEqual(caught.exception.code,"worktree_binding_changed");self.assertIn("placement",str(caught.exception))
        with self.assertRaises(PodError) as caught:self.start("work",self.packet(["S"],worktree=child),worktree="child")
        self.assertEqual(caught.exception.code,"worktree_binding_changed");self.assertIn("placement",str(caught.exception))
        admitted=self.start("work",self.packet(["S"],worktree=binding,placement=child),worktree="child")
        self.assertEqual(admitted["status"],"bound")

    def test_status_blocked_branches_render_without_tracebacks(self):
        self.intake()
        for args,blocker in [(("status","--objective","absent"),"objective_unknown"),
                             (("status","--objective","objective","--run","foreign"),"objective_run_mismatch")]:
            code,out,err=self.cli(*args);self.assertEqual(code,1);self.assertIn(blocker,out);self.assertFalse(err)
        code,out,err=self.cli("status","--objective","absent","--json")
        result=json.loads(out);self.assertEqual(result["objective"],"absent");self.assertIn(str(self.project),result["repository_context"])
        self.assertIn("state_root_searched",result)
        self.native_error=PodError("orca_read_failed","native failure",{"native_code":"no_active_sender_terminal"})
        code,out,err=self.cli("status");self.assertEqual(code,1);self.assertIn("live Orca terminal",out);self.assertNotIn("--from",out)
        self.native_error=None
        code,out,err=self.cli("status","--objective","objective");self.assertEqual(code,0)
        code,out,err=self.cli("doctor","--json");self.assertNotIn("Traceback",err)

    def test_status_projects_released_worker_and_start_time_warnings_from_exact_read(self):
        self.intake(self.sub("S"));admission=self.start("work",self.packet(["S"]))["admission"]
        self.settle(admission);dispatch=admission["native_binding"]["dispatchId"]
        original=self.port.show_worker
        def released(key):
            shown=original(key);worker=shown["result"]["worker"]
            worker.update(state="stopped",terminalState="released",effects=[{"kind":"terminal","role":"agent","id":worker["agentTerminalHandle"],"warning":"background_start"}])
            shown["result"]["projection"].update(resource={"state":"released","releaseState":"released"})
            return shown
        self.port.show_worker=released
        # Existing port mock captured the bound method; replace it at the port again.
        with patch.object(__import__('pod.operations',fromlist=['OrcaPort']).OrcaPort,"show_worker",side_effect=released):
            before=(objective_root(self.project,"objective") / "context.json").read_bytes()
            code,out,err=self.cli("status","--objective","objective","--json")
            result=json.loads(out);ref=result["native_references"][0]
            self.assertEqual(ref["native_release"]["state"],"released");self.assertEqual(ref["native_terminal_state"],"released")
            code,text,err=self.cli("status","--objective","objective")
            self.assertIn("start-time",text);self.assertIn("released",text);self.assertNotIn("| running |",text)
            self.assertEqual((objective_root(self.project,"objective") / "context.json").read_bytes(),before)

    def test_displaced_run_and_nonowner_diagnostics_preserve_authority(self):
        self.prepare();frozen=self.packet(["O1"],boundary={"paths":["src"]})
        # Yield the coordinator slot so a real implementation admission can be started.
        rows=self.stored();rows[0].pop("executor");rows[0].update(state="blocked_external",external={"party":"user","need":"input","unblocks_when":"provided"});self.write(rows)
        admission=self.start("work",frozen)["admission"];self.settle(admission)
        self.current["id"]="displaced"
        calls=[lambda:self.write(),lambda:self.op("governor",action=self.action()),
               lambda:self.report(admission,frozen),lambda:self.start("second",self.packet(["O1"]))]
        for call in calls:
            with self.assertRaises(PodError) as caught:call()
            self.assertEqual(caught.exception.code,"native_authority_unverified")
            self.assertIn("run-use --id run",str(caught.exception))
        self.current["id"]="run"
        with patch.dict(os.environ,{"ORCA_TERMINAL_HANDLE":"other"}):
            for call in calls:
                with self.assertRaises(PodError) as caught:call()
                self.assertIn("not the recorded owner",str(caught.exception));self.assertNotIn("run-use",str(caught.exception))
        self.port.runtime="changed"
        with self.assertRaises(PodError) as caught:self.write()
        self.assertNotIn("run-use",str(caught.exception))

    def test_retained_and_live_owned_terminals_use_current_exact_resource_facts(self):
        from pod.operations import OrcaPort
        self.intake(self.sub("S"));admission=self.start("work",self.packet(["S"]))["admission"]
        self.settle(admission);original=self.port.show_worker
        def retained(key):
            shown=original(key);worker=shown["result"]["worker"]
            shown["result"]["projection"].update(resource={"state":"retained","releaseState":"retained","terminalState":"retained"},
                                                  liveness={"verdict":"live"})
            shown["result"]["terminalResource"]={"ownerDispatchId":key,"terminalHandle":worker["agentTerminalHandle"],"retainedReason":"Owner requested retention"}
            return shown
        with patch.object(OrcaPort,"show_worker",side_effect=retained):
            code,out,err=self.cli("status","--objective","objective","--json")
            result=json.loads(out);self.assertEqual(result["retained_terminals"][0]["reason"],"Owner requested retention")
            self.assertEqual(result["native_references"][0]["native_terminal_state"],"retained")
            code,out,err=self.cli("status","--objective","objective");self.assertIn("Owner requested retention",out)
        self.port.workers[admission["native_binding"]["dispatchId"]]["outcome"]="in_progress"
        code,out,err=self.cli("status","--objective","objective","--json")
        result=json.loads(out);self.assertEqual(len(result["assignments"]["active"]),1)
        self.assertEqual(result["native_references"][0]["terminal"],admission["native_binding"]["terminalHandle"])
        self.assertEqual(result["native_references"][0]["ui_visibility"],"unverified")


    def test_native_code_is_bounded_and_native_message_is_never_copied(self):
        real=real_read_command
        for code in ("no_active_sender_terminal","a"*64,"BAD","bad\ncode","a"*65,None):
            payload=json.dumps({"ok":False,"error":{"code":code,"message":"PRIVATE-NATIVE-MESSAGE"}})
            proc=subprocess.CompletedProcess([],1,payload,"")
            with patch("pod.orca.executable",return_value=Path("/test/orca")),patch("subprocess.run",return_value=proc),self.assertRaises(PodError) as caught:
                real(["orchestration","run-current","--json"])
            self.assertEqual(caught.exception.code,"orca_read_failed")
            expected=code if isinstance(code,str) and code in ("no_active_sender_terminal","a"*64) else None
            self.assertEqual((caught.exception.detail or {}).get("native_code"),expected)
            self.assertNotIn("PRIVATE-NATIVE-MESSAGE",str(caught.exception))

        from pod.orca import MAX_OUTPUT
        for payload in ("not-json", "[]", "{", "["*2000+"]"*2000,
                        json.dumps({"error":{"code":"no_active_sender_terminal"}})+" "*MAX_OUTPUT):
            proc=subprocess.CompletedProcess([],1,payload,"")
            with patch("pod.orca.executable",return_value=Path("/test/orca")),patch("subprocess.run",return_value=proc),self.assertRaises(PodError) as caught:
                real(["orchestration","run-current","--json"])
            self.assertEqual(caught.exception.code,"orca_read_failed")
            self.assertNotIn("native_code",caught.exception.detail or {})


class StorageBoundaryTests(ProductionCase):
    def test_objective_governor_and_native_start_records_are_compact_and_old_indent_reads(self):
        self.prepare()
        root=objective_root(self.project,"objective"); context=root / "context.json"
        value=json.loads(context.read_text());old_digest=digest(value)
        context.write_text(json.dumps(value,indent=2)+"\n")
        self.assertEqual(digest(read(self.project,"objective")),old_digest)
        self.write()
        self.assertNotIn(b'\n  ',context.read_bytes())
        self.op("governor",action=self.action());self.assertNotIn(b'\n  ',(root / "governor.json").read_bytes())
        self.start("work",self.packet(["O1"]))
        observations=list(root.rglob("*.json"))
        self.assertTrue(any(path.name not in ("context.json","governor.json") for path in observations))
        for path in observations:self.assertNotIn(b'\n  ',path.read_bytes())
        self.assertEqual(digest(value),old_digest)

    def test_oversized_checkpoint_is_atomic_and_names_sizes_and_sections(self):
        subs=[self.sub(f"S{i}",state="blocked_external",external={"party":"user","need":"input","unblocks_when":"provided"}) for i in range(64)]
        self.intake(*subs)
        root=objective_root(self.project,"objective");path=root / "context.json"
        for turn in range(6):
            before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            value={"schema":"pod-checkpoint/v3", "seq":read(self.project,"objective")["checkpoint"]["seq"]+1,
                   "update":{row["id"]:{"state":"satisfied","evidence":[{
                       "check":"c"*512,"command":"d"*512,"result":"r"*512,"reference":f"{row['id']}-observation-{turn}"}]}
                       for row in subs}}
            request={"project":str(self.project),"objective":"objective","owner":"owner","value":value}
            source=self.root / "bounded-input.json";source.write_text(json.dumps(request))
            self.assertLess(source.stat().st_size,128*1024)
            output=StringIO()
            with redirect_stdout(output):code=internal.main(["checkpoint","--input",str(source)])
            if code == 0:continue
            error=json.loads(output.getvalue())["error"];self.assertEqual(error["code"],"record_too_large")
            detail=error["detail"]
            self.assertEqual(detail["current_bytes"],len(before[str(path)]));self.assertEqual(detail["limit_bytes"],512*1024)
            self.assertGreater(detail["proposed_bytes"],detail["limit_bytes"])
            self.assertEqual(detail["largest_sections"][0]["section"],"checkpoint")
            self.assertEqual({str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()},before)
            break
        else:self.fail("bounded genuine writes did not reach the context limit")


class AdditionalFindingTests(FindingBoundaryTests):
    def test_findings_and_correction_id_bounds_refuse_without_eviction(self):
        frozen,admission=self.intake_review();rows=self.stored();rows[1].pop("executor");rows[1].update(state="waiting",wait={"class":"sequenced","referent":"O1"})
        rows.append({"id":"C","kind":"correction","provenance":"coordinator","parent":"A","check":"fix","state":"waiting","wait":{"class":"sequenced","referent":"O1"}})
        findings=[{"finding":f"F{i}","severity":"minor","triage":"required_correction","summary":"shared predicate","correction":"C"} for i in range(9)]
        before=read(self.project,"objective")
        with self.assertRaises(PodError) as caught:self.report(admission,frozen,map={"obligations":rows},triage=findings)
        self.assertEqual(caught.exception.detail["detail"],"finding_limit");self.assertEqual(read(self.project,"objective"),before)
        rows.pop()
        advisories=[{"finding":f"F{i}","severity":"minor","triage":"advisory","summary":"independent issue"} for i in range(32)]
        self.report(admission,frozen,map={"obligations":rows},triage=advisories)
        before=read(self.project,"objective")
        with self.assertRaises(PodError) as caught:self.report(admission,frozen,triage=[{"finding":"F32","severity":"minor","triage":"advisory","summary":"another issue"}])
        self.assertEqual(caught.exception.detail["detail"],"finding_limit");self.assertEqual(read(self.project,"objective"),before)


class RemainingGovernorTests(ProductionCase):
    def test_dispatch_and_rerun_decision_reservations_attach_and_cancel_then_execute(self):
        from datetime import datetime,timezone
        self.prepare();self.op("governor-execute",action=self.action())
        original=self.remote._start
        def start(*args,**kwargs):
            run=original(*args,**kwargs);run["created_at"]=datetime.now(timezone.utc).isoformat();return run
        self.remote._start=start
        action=self.action("workflow_dispatch")
        decided=self.op("governor",action=action);self.assertEqual(decided["decision"],"ALLOW",decided)
        calls=len(self.remote.calls);attached=self.op("governor-execute",action=action)
        self.assertEqual(attached["reuse"]["kind"],"attach");self.assertEqual(len(self.remote.calls),calls)
        self.op("governor-outcome",record_id=decided["record_id"],outcome="CANCELED")
        executed=self.op("governor-execute",action=action)
        run_id=executed["receipt"]["provider"]["run_id"];self.remote.complete(run_id,"failure")
        self.op("governor-reconcile",record_id=executed["record_id"])
        self.op("governor-classify",record_id=executed["record_id"],classification={"class":"transient","reason":"fixture interruption"})
        action=self.action("validation_rerun")
        decided=self.op("governor",action=action);self.assertEqual(decided["decision"],"ALLOW",decided)
        calls=len(self.remote.calls);attached=self.op("governor-execute",action=action)
        self.assertEqual(attached["reuse"]["kind"],"attach");self.assertEqual(len(self.remote.calls),calls)
        self.op("governor-outcome",record_id=decided["record_id"],outcome="CANCELED")
        self.op("governor-execute",action=action)
        self.assertEqual(len([call for call in self.remote.calls if call[0]=="rerun"]),1)

    def test_cancel_validation_decision_reservation_is_marked_and_attached(self):
        from datetime import datetime,timezone
        self.prepare();self.op("governor-execute",action=self.action())
        original=self.remote._start
        def start(*args,**kwargs):
            run=original(*args,**kwargs);run["created_at"]=datetime.now(timezone.utc).isoformat();return run
        self.remote._start=start
        dispatched=self.op("governor-execute",action=self.action("workflow_dispatch"))
        self.move();self.write();candidate=self.op("governor-prepare",unit="default")["candidate"]
        self.authorization={**self.authorization,"candidate":candidate["commit"],"tree":candidate["tree"]}
        action=self.action("cancel_validation",target=dispatched["record_id"])
        decided=self.op("governor",action=action);self.assertEqual(decided["decision"],"ALLOW",decided)
        calls=len(self.remote.calls);attached=self.op("governor-execute",action=action)
        self.assertEqual(attached["reuse"]["kind"],"attach");self.assertEqual(len(self.remote.calls),calls)
        self.op("governor-outcome",record_id=decided["record_id"],outcome="CANCELED")
        executed=self.op("governor-execute",action=action);self.assertEqual(executed["outcome"],"PASS")
        self.assertEqual(len([call for call in self.remote.calls if call[0]=="cancel"]),1)

    def test_mapped_path_effect_refuses_before_journal_and_unknown_effects_explain_syntax(self):
        self.prepare();root=objective_root(self.project,"objective");journal=root / "governor.json"
        action={key:value for key,value in self.action().items() if key!="effects"}
        deferred=self.op("governor",action=action)
        self.assertEqual(deferred["decision"],"DEFER")
        self.assertEqual(deferred["reasons"][0]["class"],"correctness")
        for phrase in ("effects: []","workflow:ci.yml","trigger_proposal"):self.assertIn(phrase,deferred["next_action"])
        (self.project / ".pod").mkdir();policy=self.project / ".pod/config.yaml"
        policy.write_text('schema: pod/v1\nwaste_governor:\n  triggers:\n    push: ["workflow:.github/workflows/ci.yml"]\n')
        before=journal.read_bytes()
        for operation in ("governor","governor-execute"):
            with self.assertRaises(PodError) as caught:self.op(operation,action=action)
            self.assertIn("workflow:ci.yml",str(caught.exception));self.assertEqual(journal.read_bytes(),before)
        self.assertFalse(self.remote.calls)


class RemainingProofTests(ProofBoundaryTests):
    def test_one_refusal_classifies_candidate_definition_binding_and_governance_failures(self):
        from pod.records import source_identity
        source=source_identity(self.project,"src/old.py")
        ids=["O1","T","N","U","R","D","B","G","S"]
        rows=[self.criterion(proof_scope=["README.md"]),self.sub("T",proof_scope=["src"]),self.sub("N"),
              self.sub("U",proof_scope=["README.md"]),self.sub("R",proof_scope=["src"]),
              self.sub("D",proof_scope=["README.md"]),self.sub("B",proof_scope=["README.md"]),
              self.sub("G",proof_scope=["README.md"]),self.sub("S",proof_scope=[])]
        self.write(rows,governance={"base_ref":"refs/remotes/origin/target"})
        rows=self.stored()
        for row in rows:
            row.pop("executor",None);row.pop("wait",None)
            row.update(state="satisfied",evidence=[self.proof(row["id"],**({"sources":[source]} if row["id"]=="B" else {}))])
        self.write(rows);self.move("src/old.py")
        git(self.project,"switch","-qc","independent",self.base)
        (self.project / "AGENTS.md").write_text("An independently updated policy.\n")
        git(self.project,"add","AGENTS.md");git(self.project,"commit","-qm","independent policy")
        updated=git(self.project,"rev-parse","HEAD");git(self.project,"update-ref","refs/remotes/origin/target",updated)
        git(self.project,"switch","-q","main")
        rows=self.stored();next(row for row in rows if row["id"]=="U")["reuse"]={"from":"f"*40}
        next(row for row in rows if row["id"]=="R")["reuse"]={"from":self.base}
        next(row for row in rows if row["id"]=="D")["proof_scope"]=["src"]
        before=read(self.project,"objective")
        with self.assertRaises(PodError) as caught:self.write(rows,governance_refresh=True,rebind=[key for key in ids if key!="G"])
        detail=caught.exception.detail;self.assertEqual(detail["detail"],"evidence_invalidated")
        entries={item["obligation"]:item for item in detail["referent"]["invalidations"]}
        self.assertEqual(detail["referent"]["remaining"],1)
        self.assertEqual(entries["O1"]["eligibility"],"eligible");self.assertEqual(entries["T"]["eligibility"],"touched")
        self.assertEqual(entries["N"]["eligibility"],"no declared scope");self.assertEqual(entries["U"]["eligibility"],"delta unreadable")
        self.assertEqual(entries["R"]["eligibility"],"touched");self.assertEqual(entries["D"]["classification"],"definition change")
        self.assertEqual(entries["B"]["classification"],"binding change");self.assertEqual(entries["G"]["classification"],"governance change")
        self.assertTrue(all(item["correction"] for item in entries.values()));self.assertEqual(read(self.project,"objective"),before)

    def test_report_expands_explicit_reuse_list_without_a_new_receipt(self):
        self.write([self.criterion(boundary={"paths":["src"]},proof_scope=["README.md"]),self.sub("S")],governance={"base_ref":"target"})
        rows=self.stored();rows[0].pop("executor");rows[0].update(state="satisfied",evidence=[self.proof("O1")])
        rows[1].pop("wait");rows[1].update(state="active",executor="coordinator");self.write(rows)
        frozen=self.packet(["S"],role="investigate",resolves="which branch applies?",stop_condition="answer")
        admission=self.start("investigate",frozen)["admission"]
        self.move("src/old.py");rows=self.stored();rows[0].update(state="blocked_external",external={"party":"user","need":"input","unblocks_when":"supplied"});self.write(rows)
        self.settle(admission)
        result=self.report(admission,frozen,map={"update":{
            "O1":{"state":"satisfied"},"S":{"state":"blocked_external","external":{"party":"user","need":"input","unblocks_when":"supplied"}}},
            "seq":read(self.project,"objective")["checkpoint"]["seq"]+1,"reuse":{"from":self.base,"ids":["O1"]}})
        row=self.stored()[0];self.assertEqual(row["state"],"satisfied");self.assertEqual(len(row["receipts"]),1)

    def test_ordinary_proof_scope_revision_invalidates_proof_and_restated_map_succeeds(self):
        self.satisfied_rows();rows=self.stored();rows[0]["proof_scope"]=["src"]
        with self.assertRaises(PodError) as caught:self.write(rows)
        self.assertEqual(caught.exception.detail["detail"],"evidence_invalidated")
        rows[0].update(state="blocked_external",external={"party":"user","need":"current proof","unblocks_when":"supplied"})
        self.write(rows)
        self.assertEqual(self.stored()[0]["proof_scope"],["src"])

    def test_empty_process_scope_still_refuses_an_unreadable_delta(self):
        self.satisfied_rows();self.move("src/old.py")
        rows=self.stored();rows[1]["reuse"]={"from":"f"*40}
        with self.assertRaises(PodError) as caught:self.write(rows)
        s=next(item for item in caught.exception.detail["referent"]["invalidations"] if item["obligation"]=="S")
        self.assertEqual(s["eligibility"],"delta unreadable")


class RemainingDiagnosticsTests(ProductionCase):
    cli=DiagnosticsBoundaryTests.cli
    def test_known_obligation_field_diagnostics_and_remaining_exact_record_shapes(self):
        sentinel="FIELD-VALUE-IS-PRIVATE"
        cases=[([self.criterion(check=sentinel*100)],{}),([self.criterion(parent=sentinel*10)],{}),
               ([self.criterion(adopts=sentinel*10)],{}),
               ([self.criterion(source={"unexpected":sentinel})],{}),
               ([self.criterion(provenance="user_direct",source={"unexpected":sentinel})],{}),
               ([self.criterion(),self.sub("P",provenance="project_policy",source={"unexpected":sentinel})],{}),
               ([self.criterion(state="withdrawn",withdrawal={"unexpected":sentinel})],{}),
               ([self.criterion(state="withdrawn",withdrawal={"by":sentinel,"reason":"reason"})],{}),
               ([self.criterion(state="satisfied",evidence=[{"check":"check","command":"check","result":"pass","reference":"initial","sources":[{"unexpected":sentinel}]}])],{}),
               ([self.criterion(state="satisfied",evidence=[{"check":"check","command":"check","result":"pass","reference":"initial"}],reuse={"unexpected":sentinel})],{}),
               ([self.criterion()],{"reuse":{"unexpected":sentinel}})]
        for rows,fields in cases:
            with self.subTest(row=rows),self.assertRaises(PodError) as caught:self.write(rows,governance={"base_ref":"target"},**fields)
            self.assertNotIn(sentinel,str(caught.exception));self.assertTrue(caught.exception.detail["referent"])
            if rows[0].get("check","").startswith(sentinel) or "parent" in rows[0] or "adopts" in rows[0]:
                self.assertEqual(caught.exception.detail["referent"]["obligation"],"O1")
        self.intake({"id":"A","kind":"assurance","provenance":"coordinator","parent":"O1","check":"review",
            "scope":{"paths":["src"]},"question":"correct?","candidate":self.base,"existing_evidence":"tests","insufficiency":"no review",
            "state":"waiting","wait":{"class":"sequenced","referent":"O1"}})
        frozen=self.packet(["A"],role="review");admission=self.start("review",frozen)["admission"];self.settle(admission)
        rows=self.stored();rows[1].pop("executor");rows[1].update(state="waiting",wait={"class":"sequenced","referent":"O1"})
        for fields in ({"triage":[{"unexpected":sentinel}]},
                       {"triage":[{"findings":[{"unexpected":sentinel}],"triage":"advisory","summary":"root","root_cause":"same"}]},
                       {"proposals":[{"unexpected":sentinel}]}):
            with self.assertRaises(PodError) as caught:self.report(admission,frozen,map={"obligations":rows},**fields)
            self.assertNotIn(sentinel,str(caught.exception));self.assertIn("missing",str(caught.exception));self.assertIn("unsupported",str(caught.exception))
        bad=deepcopy(rows);bad[1].pop("wait");bad[1].update(state="satisfied",evidence=[{"unexpected":sentinel}])
        with self.assertRaises(PodError) as caught:self.report(admission,frozen,map={"obligations":bad})
        self.assertNotIn(sentinel,str(caught.exception));self.assertIn("assurance evidence",str(caught.exception))

    def test_failed_outside_terminal_intake_has_native_cause_and_observation_remains_available(self):
        self.native_error=PodError("orca_read_failed","This process is not in a live Orca terminal; Pod records need one. Observe read-only with pod status --objective ID",{"native_code":"no_active_sender_terminal"})
        with self.assertRaises(PodError) as caught:self.intake()
        self.assertEqual(caught.exception.detail["native_code"],"no_active_sender_terminal")
        self.assertIn("live Orca terminal",str(caught.exception));self.assertNotIn("--from",str(caught.exception))
        self.native_error=None;self.intake()
        self.native_error=PodError("orca_read_failed","unavailable",{"native_code":"no_active_sender_terminal"})
        code,out,err=self.cli("status","--objective","objective","--json")
        self.assertNotIn("Traceback",err);self.assertIn("objective",out)

    def test_ambiguous_continuity_nonowner_has_no_owner_route_and_no_write(self):
        self.intake();self.current=None;self.port.runtime="changed"
        before=read(self.project,"objective")
        for supplied in ("owner","other"):
            with patch.dict(os.environ,{"ORCA_TERMINAL_HANDLE":"other"}),self.assertRaises(PodError) as caught:
                self.op("checkpoint",owner=supplied,value=self.core())
            self.assertIn("not the recorded owner",str(caught.exception));self.assertNotIn("Ask the Owner",str(caught.exception))
            self.assertEqual(read(self.project,"objective"),before)

    def test_non_git_candidates_keep_bounded_text(self):
        import shutil
        shutil.rmtree(self.project / ".git")
        self.candidate="folder-candidate"
        self.write([self.criterion()],governance={"base_ref":None})
        self.assertEqual(read(self.project,"objective")["checkpoint"]["candidate"],"folder-candidate")


class GuidanceBudgetTests(unittest.TestCase):
    def test_actual_guidance_has_effective_headroom_and_all_four_rules(self):
        from pod.skill_validation import MAX_SKILL_WORDS,MAX_REFERENCE_WORDS,MAX_REFERENCE_WORDS_COMBINED
        root=Path(__file__).resolve().parents[1] / "skills/pod"
        self.assertLessEqual(len((root / "SKILL.md").read_text().split()),MAX_SKILL_WORDS-2)
        references=[path.read_text() for path in (root / "references").glob('*.md')]
        self.assertTrue(all(len(text.split()) <= MAX_REFERENCE_WORDS for text in references))
        self.assertLessEqual(sum(len(text.split()) for text in references),MAX_REFERENCE_WORDS_COMBINED)
        joined=" ".join(references)
        for rule in ("governor-execute` directly","proof_scope","reuse: {from, ids}","per-assignment `placement`", "one predicate", "one table test"):
            self.assertIn(rule,joined)


class FinalDiagnosticShapesTests(ProductionCase):
    def test_cycle_refusal_lists_every_affected_dependent(self):
        rows=[self.criterion(),*[self.sub(f"S{i}",state="waiting",wait={"class":"dependency","referent":f"S{(i+1)%4}"}) for i in range(4)]]
        with self.assertRaises(PodError) as caught:self.write(rows,governance={"base_ref":"target"})
        self.assertEqual(caught.exception.code,"wait_invalid");self.assertEqual(caught.exception.detail["detail"],"cycle")
        self.assertEqual(caught.exception.detail["dependents"],[f"S{i}" for i in range(4)])
        self.assertEqual(caught.exception.detail["referent"]["obligations"],"S0,S1,S2,S3")

    def test_disposition_exact_fields_are_named_without_values(self):
        self.intake(self.sub("S"));frozen=self.packet(["S"]);admission=self.start("work",frozen)["admission"];self.settle(admission)
        with self.assertRaises(PodError) as caught:self.write(dispositions=[{"admission":admission["admission_id"],"unexpected":"SENTINEL-PRIVATE-FIELD"}])
        self.assertIn("disposition",str(caught.exception));self.assertIn("unsupported",str(caught.exception))
        self.assertNotIn("SENTINEL-PRIVATE-FIELD",str(caught.exception))
        self.assertTrue(caught.exception.detail["referent"])

    def test_assurance_proof_scope_and_nonfull_candidate_refuse(self):
        row={"id":"A","kind":"assurance","provenance":"coordinator","parent":"O1","check":"review",
             "scope":{"paths":["src"]},"question":"correct?","candidate":self.base,"existing_evidence":"tests","insufficiency":"no review",
             "state":"waiting","wait":{"class":"sequenced","referent":"O1"}}
        for changed in ({"proof_scope":["src"]},{"proof_scope":[]},{"candidate":"HEAD"}):
            with self.assertRaises(PodError) as caught:self.intake({**row,**changed})
            self.assertEqual(caught.exception.code,"obligation_invalid")
            self.assertIsNone(read(self.project,"objective"))
