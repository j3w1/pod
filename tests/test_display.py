"""Synthetic display facts and production entries with all native ports replaced."""
from copy import deepcopy
import random
import re
import unittest
from unittest.mock import patch

from pod import display, internal
from pod.errors import PodError
from pod.ledger import read
from tests.test_worker_tabs import TabCase


def state_fixture():
    return {"owner": "owner", "checkpoint": {"objective": "objective", "worktree": {
        "path": "/project/task", "repository": "acme/example"},
        "display": {"task": "Parser repair", "continued": [], "host_tab": False},
        "objective_source": {"repository": "acme/example", "number": 17}, "obligations": []}, "admissions": {}}


def admit(state, role="implement", purpose="Source changes", path="/project/worker", **fields):
    key = "assignment-" + str(len(state["admissions"]) + 1)
    body = {"role": role, "purpose": purpose, "serves": [], **fields}
    row = {"admission_id": key, "role": role, "state": "bound", "admitted_seq": len(state["admissions"]) + 1,
           "serves": body["serves"], "reuse_of": fields.get("reuse_of"), "report": None,
           "request": {"agent": "claude"}, "recovery": {"placement_binding": {"path": path}},
           "display": display.admission_facts(state, body, path),
           "native_binding": {"terminalHandle": "tab-" + key}}
    state["admissions"][key] = row
    return row


class DisplayNameTests(unittest.TestCase):
    def test_purpose_only_filter_task_phrase_and_fallback(self):
        body = {"purpose": "Parser repair source retirement /private/x branch/topic deadbeef a1b2c3d4", "responsibility": "Forbidden prose"}
        self.assertEqual(display.job(body, "implementer", "parser repair"), "Source retirement")
        self.assertEqual(display.job({"responsibility": "Forbidden prose"}, "implementer"), "Implementation")
        for kind, noun in display.NOUNS.items():
            self.assertEqual(display.job({"purpose": "Parser repair"}, kind, "parser repair"), noun)
        self.assertEqual(display.job({"purpose": "Parser repairs"}, "implementer", "Parser repair"), "Parser repairs")

    def test_boundaries_and_sibling_ordinals_and_reuse(self):
        long = "Migration architecture " * 8
        value = display.job({"purpose": long}, "implementer")
        self.assertLessEqual(len(value), 40)
        self.assertFalse(value.endswith(" "))
        state = state_fixture()
        first = admit(state, purpose=long)
        second = admit(state, purpose=long.lower(), path="/project/other")
        again = admit(state, purpose="Different purpose", path="/project/worker")
        self.assertEqual(second["display"]["worktree_name"].casefold(), first["display"]["job"].casefold() + " 2")
        self.assertEqual(again["display"]["worktree_name"], first["display"]["worktree_name"])
        third = admit(state, purpose=long, path="/project/third")
        self.assertTrue(third["display"]["worktree_name"].endswith(" 3"))
        self.assertLessEqual(len(third["display"]["worktree_name"]), 44)

    def test_seeded_names_exclude_identifiers_prompts_and_parent_phrase(self):
        rng = random.Random(83)
        for _ in range(128):
            junk = rng.sample(["term_fixture", "ctx_fixture", "a1b2c3d4", "deadbeef", "branch/topic",
                               "/private/file", "model=example", "completed", "authorized", "12345"], 4)
            body = {"purpose": "Parser repair schema migration " + " ".join(junk), "responsibility": "Confidential instructions"}
            value = display.job(body, "implementer", "Parser repair")
            self.assertEqual(value, "Schema migration")
            self.assertIsNone(re.search(r"\d|/|[a-f0-9]{7,}|branch|Confidential|instructions|Parser|repair", value, re.I))


class DisplayTitleTests(unittest.TestCase):
    def test_role_words_correction_and_delta(self):
        state = state_fixture()
        state["checkpoint"]["obligations"] = [{"id": "K", "kind": "correction"}]
        examples = [("implement", {}, "Implementer"), ("implement", {"serves": ["K"]}, "Fixer"),
                    ("investigate", {}, "Investigator"), ("review", {}, "Reviewer")]
        for role, fields, word in examples:
            row = admit(state, role, path="/project/" + word, **fields)
            self.assertEqual(display.worker_title(state, row), word)
        follow = admit(state, "review", delta_from="previous", path="/project/delta")
        self.assertEqual(display.worker_title(state, follow), "Follow-up reviewer · round 2")

    def test_rounds_recompute_and_failed_starts_do_not_consume(self):
        state = state_fixture()
        first = admit(state, "review")
        self.assertEqual(display.worker_title(state, first), "Reviewer")
        failed = admit(state, "review", path="/project/failed")
        failed["state"] = "deferred"
        second = admit(state, "review", path="/project/review")
        self.assertEqual(second["display"]["round"], 2)
        self.assertEqual(display.worker_title(state, first), "Reviewer · round 1")
        state["checkpoint"]["obligations"] = [{"id": "K", "kind": "correction"}]
        fixer = admit(state, serves=["K"], path="/project/fixer")
        self.assertEqual(display.worker_title(state, fixer), "Fixer · round 2")
        first["state"] = "closed"; second["state"] = "unresolved"
        self.assertEqual(display.worker_title(state, first), "Reviewer · round 1")

    def test_shared_worktree_collisions_continued_and_bound(self):
        state = state_fixture()
        first = admit(state, path="/project/task")
        second = admit(state, path="/project/task")
        self.assertEqual(display.worker_title(state, first), "Implementer · Source changes")
        self.assertEqual(display.worker_title(state, second), "Implementer · Source changes 2")
        third = admit(state, path="/project/task", reuse_of=first["admission_id"])
        self.assertEqual(display.worker_title(state, third), "Implementer · Source changes (continued)")
        state["checkpoint"]["display"]["continued"] = [second["admission_id"]]
        for title in display.worker_titles(state).values():
            self.assertLessEqual(len(title), 96)
        self.assertTrue(display.worker_title(state, second).endswith(" (continued)"))

    def test_packet_preview_includes_next_round_and_has_plain_fallback(self):
        state = state_fixture()
        first = admit(state, "review")
        body = {"role": "review", "delta_from": first["admission_id"], "purpose": "Delta assessment",
                "placement": {"path": "/project/review"}}
        self.assertEqual(display.packet_title(state, body), "Follow-up reviewer · round 2")
        self.assertEqual(display.packet_title(None, body), "Follow-up reviewer")
        self.assertEqual(len(state["admissions"]), 1)

    def test_own_worktree_collision_adds_job_then_ordinal(self):
        state = state_fixture()
        rows = [admit(state) for _ in range(3)]
        self.assertEqual([display.worker_title(state, row) for row in rows],
                         ["Implementer", "Implementer · Source changes", "Implementer · Source changes 2"])

    def test_coordinator_inside_outside_and_successor(self):
        state = state_fixture()
        self.assertEqual(display.coordinator_title(state, {"worktreePath": "/project/task"}, None), "Coordinator")
        elsewhere = {"worktreePath": "/project/elsewhere"}
        self.assertEqual(display.coordinator_title(state, elsewhere, None), "Coordinator · Parser repair (#17)")
        host = {"displayNameMode": "fixed", "displayName": "Host task"}
        state["continues"] = {"objective": "predecessor"}
        self.assertEqual(display.coordinator_title(state, elsewhere, host), "Coordinator · Host task (continued)")
        state.pop("continues"); state["checkpoint"]["display"]["continued"] = ["coordinator"]
        self.assertEqual(display.coordinator_title(state, {"worktreePath": "/project/task"}, host), "Coordinator (continued)")
        state["checkpoint"].pop("display")
        self.assertIsNone(display.coordinator_title(state, elsewhere, None))


class DisplayStatusTests(unittest.TestCase):
    def test_implementer_reviewer_fixer_investigator_statuses(self):
        for role, fields, initial in [("implement", {}, "in-progress"), ("review", {}, "in-review"),
                                     ("investigate", {}, "in-progress"), ("implement", {"serves": ["K"]}, "in-progress")]:
            with self.subTest(role=role, fields=fields):
                state = state_fixture()
                state["checkpoint"]["obligations"] = [{"id": "K", "kind": "correction", "state": "satisfied"}]
                row = admit(state, role, **fields)
                status, comment = display.worktree_status(state, "/project/worker")
                self.assertEqual(status, initial)
                self.assertEqual(comment, display.worker_title(state, row) + " working" if initial == "in-progress" else "Waiting on review")
                row["report"] = {"outcome": "succeeded"}
                self.assertEqual(display.worktree_status(state, "/project/worker"), ("completed", "Report recorded"))
                state["checkpoint"]["obligations"].append({"id": "A", "kind": "assurance", "state": "waiting"})
                expected = "in-review" if role == "implement" else "completed"
                self.assertEqual(display.worktree_status(state, "/project/worker")[0], expected)

    def test_corrections_delta_review_and_progress_precedence(self):
        state = state_fixture(); row = admit(state, "review")
        row["report"] = {"outcome": "succeeded"}
        state["checkpoint"]["obligations"] = [{"id": "A", "kind": "assurance", "state": "waiting"},
            {"id": "K", "kind": "correction", "state": "unassigned", "parent": "A", "finding": {"attempt": row["admission_id"]}}]
        self.assertEqual(display.worktree_status(state, "/project/worker"), ("in-review", "Waiting on review"))
        state["checkpoint"]["obligations"][1]["state"] = "satisfied"
        self.assertEqual(display.worktree_status(state, "/project/worker")[0], "in-review")
        working = admit(state, "investigate")
        self.assertEqual(display.worktree_status(state, "/project/worker"),
                         ("in-progress", display.worker_title(state, working) + " working"))
        working["report"] = {"outcome": "succeeded"}
        state["checkpoint"]["obligations"][0]["state"] = "withdrawn"
        self.assertEqual(display.worktree_status(state, "/project/worker"), ("completed", "Report recorded"))


def inventories(state):
    task = {"id": "task-tree", "path": "/project/task", "displayNameMode": "automatic", "displayName": "task",
            "linkedIssue": None, "linkedPR": None, "parentWorktreeId": None, "workspaceStatus": "in-progress", "comment": ""}
    terminal = {"handle": "owner", "worktreePath": "/project/task", "title": "Coordinator", "connected": True}
    return {"result": {"terminals": [terminal]}}, {"result": {"worktrees": [task]}}


class DisplayOwnershipTests(unittest.TestCase):
    def test_automatic_task_name_issue_and_host_names_win(self):
        state = state_fixture(); terms, trees = inventories(state)
        output = display.projection(state, terms, trees, {"version": "1.4.230"})
        task = output["rows"][0]
        self.assertEqual(task["owner"], "pod")
        self.assertEqual(task["command"], ["orca", "worktree", "set", "--worktree", "path:/project/task",
                         "--display-name", "Parser repair (#17)", "--issue", "17", "--json"])
        trees["result"]["worktrees"][0].update(displayNameMode="fixed", displayName="Host title")
        state["checkpoint"]["display"]["host_tab"] = True
        terms["result"]["terminals"][0]["title"] = "Host coordinator"
        output = display.projection(state, terms, trees, {"version": "1.4.230"})
        self.assertEqual([row["owner"] for row in output["rows"]], ["host", "host"])
        self.assertTrue(all(row["command"] is None and row["verdict"] == "host" for row in output["rows"]))
        self.assertEqual(output["next_action"], [])

    def test_cross_repository_issue_has_repository_form_without_link(self):
        state = state_fixture(); terms, trees = inventories(state)
        state["checkpoint"]["objective_source"]["repository"] = "other/repo"
        row = display.projection(state, terms, trees, {"version": "1.4.230"})["rows"][0]
        self.assertEqual(row["expected"]["name"], "Parser repair (other/repo#17)")
        self.assertNotIn("--issue", row["command"])


class DisplayOpTests(TabCase):
    def display_read(self, argv, **kwargs):
        self.display_calls.append(argv)
        if argv == ["--version"]:
            return {"version": self.version}
        if self.unreadable == argv:
            raise PodError("orca_read_failed", "synthetic failure")
        if argv == ["terminal", "list", "--json"]:
            return deepcopy(self.terminals)
        if argv == ["worktree", "list", "--json"]:
            return deepcopy(self.worktrees)
        raise AssertionError(argv)

    def setup_display(self):
        self.intake(self.sub("S"), worktree=self.binding(),
                    display={"task": "Parser repair", "continued": [], "host_tab": False})
        state = read(self.project, "objective")
        self.terminals = {"result": {"terminals": [{"handle": "owner", "worktreePath": str(self.project),
                                                   "title": "Coordinator", "connected": True}]}}
        self.worktrees = {"result": {"worktrees": [{"path": str(self.project), "id": "task-tree",
            "displayNameMode": "automatic", "displayName": "Parser repair", "linkedIssue": None}]}}
        self.version = "1.4.230"; self.unreadable = None; self.display_calls = []
        return state

    def binding(self):
        return {key: value for key, value in self.port.placement.items() if key != "runtime"}

    def run_display(self):
        before = self.saved()
        with patch("pod.orca.read_command", side_effect=self.display_read), patch("pod.orca.mutate_command") as mutation:
            result = self.op("display")
        self.assertEqual(self.display_calls[-3:], [["terminal", "list", "--json"], ["worktree", "list", "--json"], ["--version"]])
        self.assertEqual(self.saved(), before); mutation.assert_not_called()
        self.assertEqual(result["desktop"], "NOT_VERIFIED")
        return result

    def test_read_only_matches_new_backend_and_old_limitation(self):
        self.setup_display()
        self.assertEqual(self.run_display()["verdict"], "backend_matches")
        for version in ("1.4.229", "Orca 1.4.228", "1.3.999"):
            self.version = version
            result = self.run_display()
            self.assertEqual(result["verdict"], "orca_limitation")
            self.assertEqual(result["orca"]["limitation"], display.LIMITATION)
        self.version = "unreadable"
        self.assertEqual(self.run_display()["verdict"], "orca_limitation")

    def test_differs_returns_exact_commands_and_host_has_none(self):
        self.setup_display(); self.terminals["result"]["terminals"][0]["title"] = "Auto prompt"
        output = self.run_display()
        self.assertEqual(output["verdict"], "drift")
        coordinator = output["rows"][1]
        self.assertEqual(coordinator["command"], ["orca", "terminal", "rename", "--terminal", "owner", "--title", "Coordinator", "--json"])
        self.assertIsNone(coordinator["session_rename"])
        self.worktrees["result"]["worktrees"][0].update(displayNameMode="fixed", displayName="Host name")
        self.assertIsNone(self.run_display()["rows"][0]["command"])

    def test_unreadable_and_truncated_repeat_read(self):
        self.setup_display(); self.unreadable = ["terminal", "list", "--json"]
        output = self.run_display(); row = output["rows"][1]
        self.assertEqual(row["verdict"], "unreadable")
        self.assertEqual(row["read_command"], ["orca", "terminal", "list", "--json"])
        self.assertIsNone(row["command"])
        self.assertEqual(output["verdict"], "drift")
        self.unreadable = None; self.worktrees["result"]["truncated"] = True
        self.assertEqual(self.run_display()["rows"][0]["verdict"], "unreadable")
    def test_worker_layers_readback_status_and_session_steps(self):
        state = state_fixture(); row = admit(state)
        terms, trees = inventories(state)
        task = trees["result"]["worktrees"][0]
        task.update(displayNameMode="fixed", displayName="Host task")
        trees["result"]["worktrees"].append({"id": "worker-tree", "path": "/project/worker",
            "displayName": "Source changes", "displayNameMode": "fixed", "parentWorktreeId": "task-tree",
            "linkedIssue": {"number": 17}, "linkedPR": None, "workspaceStatus": "in-progress",
            "comment": "Implementer working"})
        terms["result"]["terminals"].append({"handle": row["native_binding"]["terminalHandle"],
            "worktreePath": "/project/worker", "title": "Implementer", "connected": True})
        output = display.projection(state, terms, trees, {"version": "1.4.230"})
        self.assertEqual(output["verdict"], "backend_matches")
        self.assertEqual([item["layer"] for item in output["rows"]],
                         ["task_worktree", "coordinator_tab", "worker_worktree", "worker_tab"])
        self.assertEqual(output["rows"][2]["verdict"], "matches")
        terms["result"]["terminals"][1]["title"] = "Auto prompt"
        output = display.projection(state, terms, trees, {"version": "1.4.230"})
        self.assertEqual(output["rows"][3]["verdict"], "differs")
        self.assertEqual(output["next_action"][1]["session_rename"]["text"], "/rename Implementer")
        terms["result"]["terminals"][1]["connected"] = False
        self.assertEqual(len(display.projection(state, terms, trees, {"version": "1.4.230"})["rows"]), 3)



class DisplayAckTests(TabCase):
    def binding(self):
        return {key: value for key, value in self.port.placement.items() if key != "runtime"}

    def test_checkpoint_display_validation_persistence_and_whole_replacement(self):
        value = {"task": "Parser repair", "continued": [], "host_tab": True}
        self.intake(self.sub("S"), worktree=self.binding(), display=value)
        self.assertEqual(read(self.project, "objective")["checkpoint"]["display"], value)
        self.write(self.stored())
        self.assertEqual(read(self.project, "objective")["checkpoint"]["display"], value)
        before = self.saved()
        for bad in ({**value, "task": "term_fixture"}, {**value, "task": "deadbeef"},
                    {**value, "task": "branch/name"}, {**value, "task": "word " * 10},
                    {**value, "continued": ["unknown"]}, {**value, "continued": ["coordinator"] * 33},
                    {**value, "host_tab": 1}, {"task": "Parser repair"}):
            with self.subTest(bad=bad), self.assertRaises(PodError) as error:
                self.write(self.stored(), display=bad)
            self.assertEqual(error.exception.code, "invalid_display_name")
            self.assertEqual(self.saved(), before)
        replacement = {"task": "Schema migration", "continued": ["coordinator"], "host_tab": False}
        raw = self.write(self.stored(), display=replacement)
        self.assertEqual(self.ack("checkpoint", {}, raw)["checkpoint"]["display"], replacement)
        self.assertEqual(internal.run("contract", {"record": "display"})["records"]["display"]["fields"]["task"]["max_length"], 48)

    def test_admission_title_rename_codex_and_own_worktree_first_then_later(self):
        objective = self.binding()
        self.intake(self.sub("S"), worktree=objective,
                    display={"task": "Parser repair", "continued": [], "host_tab": False})
        child = {**objective, "path": str(self.root / "child")}
        self.port.placement = {**child, "runtime": "runtime"}
        frozen = self.packet(["S"], role="investigate", resolves="one question", stop_condition="one answer",
                             purpose="Parser repair source analysis", worktree=objective, placement=child)
        self.assertEqual(frozen["task_display_name"], "Investigator")
        result = self.start("first", frozen, worktree="path:" + child["path"])
        row = result["admission"]; handle = row["native_binding"]["terminalHandle"]
        self.assertEqual(result["title"], "Investigator")
        self.assertEqual(result["rename_command"], display.rename("Investigator", handle))
        self.assertIsNone(result["session_rename"])
        self.assertEqual(result["session_reason"], display.CODEX_REASON)
        self.assertEqual(result["worktree_command"], ["orca", "worktree", "set", "--worktree", "path:" + child["path"],
            "--display-name", "Source analysis", "--parent-worktree", "path:" + str(self.project),
            "--workspace-status", "in-progress", "--comment", "Investigator working", "--json"])
        self.settle(row)
        reported = self.report(row, frozen, map={"obligations": self.waiting_rows()})
        finished = self.ack("report", {"admission_id": row["admission_id"]}, reported)["worker_tab"]
        self.assertEqual(finished["retained_title"]["title"], "Investigator")
        self.assertEqual(finished["worktree_status"], "completed")
        self.assertEqual(finished["worktree_command"][-4:], ["completed", "--comment", "Report recorded", "--json"])
        later = self.packet(["S"], role="investigate", resolves="next question", stop_condition="answer",
                            purpose="Other analysis", worktree=objective, placement=child)
        result = self.start("second", later, worktree="path:" + child["path"])
        self.assertNotIn("--display-name", result["worktree_command"])
        self.assertEqual(result["admission"]["display"]["worktree_name"], "Source analysis")
        self.assertEqual(result["title"], "Investigator · Other analysis")
        status_tab = self.status()["worker_tabs"][0]
        self.assertEqual(status_tab["worktree_status"], "in-progress")

    def test_shared_worktree_claude_guard_and_recorded_continuation(self):
        self.intake(self.sub("S"), worktree=self.binding())
        frozen = self.packet(["S"], role="investigate", purpose="Source analysis", resolves="question", stop_condition="answer", worktree=self.binding())
        result = self.start("first", frozen)
        admission = result["admission"]; handle = admission["native_binding"]["terminalHandle"]
        self.assertIsNone(result["worktree_command"])
        rename = display.session_rename(result["title"], handle, "claude")["session_rename"]
        self.assertEqual(rename["text"], "/rename Investigator · Source analysis")
        self.assertEqual(rename["wait"], ["orca", "terminal", "wait", "--terminal", handle,
                                           "--for", "tui-idle", "--timeout-ms", "60000", "--json"])
        self.assertEqual(rename["command"], ["orca", "terminal", "send", "--terminal", handle,
                                              "--text", rename["text"], "--enter", "--json"])
        self.write(self.stored(), display={"task": "Parser repair", "continued": [admission["admission_id"]], "host_tab": False})
        state = read(self.project, "objective")
        self.assertTrue(display.worker_title(state, state["admissions"][admission["admission_id"]]).endswith(" (continued)"))
        self.assertIsNone(display.session_rename("Coordinator", "owner", "claude", coordinator=True)["session_rename"])
        self.assertIsNone(display.session_rename("Investigator", None, "claude")["session_rename"])

    def test_claude_admission_returns_guarded_session_rename(self):
        self.intake(self.sub("S"))
        frozen = self.packet(["S"], role="investigate", purpose="Source analysis", resolves="question", stop_condition="answer")
        from pod.records import packet
        body = deepcopy(frozen["body"])
        body["route"].update(agent="claude", model="claude-sonnet-5-5", effort="high")
        result = self.start("claude", packet(body))
        handle = result["admission"]["native_binding"]["terminalHandle"]
        self.assertEqual(result["session_rename"], display.session_rename(result["title"], handle, "claude")["session_rename"])
        self.assertIsNone(result["session_reason"])

    def test_admission_display_facts_are_validated_and_immutable(self):
        self.intake(self.sub("S"))
        frozen = self.packet(["S"])
        row = self.start("first", frozen)["admission"]
        from pod.ledger import update_admission
        before = self.saved()
        with self.assertRaises(PodError) as error:
            update_admission(self.project, "objective", owner="owner", admission_id=row["admission_id"],
                             update=lambda value: value["display"].update(job="Changed"))
        self.assertEqual(error.exception.code, "admission_conflict")
        self.assertEqual(self.saved(), before)
        for field, value in (("kind", "unknown"), ("round", True), ("own_worktree", 1), ("job", "term_fixture"),
                             ("worktree_name", "a" * 45)):
            with self.subTest(field=field), self.assertRaises(PodError):
                display.validate_facts({**row["display"], field: value})

    def test_first_worktree_command_links_issue_and_known_pr(self):
        state = state_fixture()
        state["checkpoint"]["delivery"] = {"provider": {"pr": 23}}
        row = admit(state)
        command = display.admission_output(state, row)["worktree_command"]
        self.assertEqual(command[command.index("--issue"):command.index("--workspace-status")],
                         ["--issue", "17", "--pr", "23"])

    def test_packet_fallback_is_plain_role_and_earlier_rows_keep_bytes(self):
        self.intake(self.sub("S"))
        frozen = self.packet(["S"], purpose="Source analysis")
        self.assertEqual(frozen["title"], "Implementer: Source analysis")
        self.assertEqual(frozen["task_display_name"], "Implementer")
        with patch("pod.ledger.read", side_effect=PodError("state_unsupported", "synthetic")):
            from pod.records import packet
            value = packet({**frozen["body"], "worktree": self.binding()})
        self.assertEqual(value["task_display_name"], "Implementer")
        row = self.start("first", frozen)["admission"]
        from pod.ledger import _path, _read, _write
        path = _path(self.project, "objective")
        state = _read(path); state["admissions"][row["admission_id"]].pop("display")
        _write(path, state)
        before = self.saved(); state = read(self.project, "objective")
        earlier = state["admissions"][row["admission_id"]]
        self.assertNotIn("display", earlier)
        self.assertEqual(display.worker_title(state, earlier), "Implementer")
        self.assertIsNone(display.worktree_command(state, earlier)["worktree_command"])
        self.assertEqual(self.saved(), before)
