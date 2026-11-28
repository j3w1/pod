import os
from unittest import mock

from pod import cli, commands, gate, log, orca
from pod.errors import PodError
from tests.core.support import CoreCase, ROUTES

PROJECT = "11111111-2222-3333-4444-555555555555"
PROFILES = ("profiles:\n  ui-design: implement claude/opus/high\n  scout: investigate codex/sol/high\n"
            "  review-deep: review codex/sol/high\n")


class FolderWorkspaceTests(CoreCase):
    """An Orca folder workspace: objectives without Git that never deliver."""

    def setUp(self):
        super().setUp()
        self.write_routes(ROUTES + PROFILES)
        self.folder = (self.tmp / "analysts").resolve()          # every workspace of the project has this one path
        self.folder.mkdir(parents=True)
        self.address = "folder:f-1"
        self.set_orca(folders=[self.row("f-1", "i-1"), self.row("f-2", "i-2")], repos={PROJECT: "folder"},
                      terminals={"term_a": "folder:f-1", "term_b": "folder:f-2"})
        self.terminal("term_a")

    def row(self, ident, instance):
        return {"id": f"folder:{ident}", "repoId": PROJECT, "path": str(self.folder), "address": f"folder:{ident}",
                "instanceId": instance}

    def terminal(self, handle):
        patched = mock.patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": handle})
        patched.start()
        self.addCleanup(patched.stop)

    def opened(self, **options):
        return commands.open_(self.folder, text="Lay out the agent map", objective="layout", criteria=["a layout"],
                              delivery=False, **options)

    def folder_entries(self):
        return log.read(log.path(f"folder-{PROJECT}", "layout"))

    def test_open_binds_the_folder_and_its_workspace_with_no_branch_pr_or_delivery(self):
        view = self.opened()
        self.assertEqual({key: view[key] for key in ("objective", "state", "worktree", "workspace_id", "workspace_instance_id",
                                                     "workspace_kind", "delivery", "branch", "pr", "target")},
                         {"objective": "layout", "state": "open", "worktree": str(self.folder), "workspace_id": self.address,
                          "workspace_instance_id": "i-1", "workspace_kind": "folder", "delivery": "none", "branch": None,
                          "pr": None, "target": None})
        self.assertEqual(self.folder_entries()[0]["repository"], f"folder-{PROJECT}")
        self.assertEqual(commands.open_(self.folder, text="Lay out the agent map", objective="layout", delivery=False,
                                        criteria=["a layout"])["workspace_id"], self.address)      # the same open again
        root = f"{PROJECT}::{self.folder}"                          # the project root: terminal show names it bare
        self.set_orca(folders=self.orca()["folders"] + [{**self.row("r", "i-r"), "id": root, "address": f"id:{root}"}],
                      terminals={"term_root": root})
        self.terminal("term_root")
        self.assertEqual(commands.open_(self.folder, text="Root work", objective="root", criteria=["x"],
                                        delivery=False)["workspace_id"], f"id:{root}")

    def test_a_folder_refuses_every_delivery_and_an_unregistered_folder_is_no_repository(self):
        for check in (False, True):                                 # placement decides, before any objective lookup
            self.refused("folder_no_delivery", gate.deliver, self.folder, None, check=check)
        self.refused("folder_no_delivery", commands.open_, self.folder, text="Deliver it", objective="d")
        self.refused("folder_no_delivery", commands.open_, self.folder, text="Deliver it", objective="d",
                     delivery=False, branch="work")
        stray = self.tmp / "stray"
        stray.mkdir()
        self.refused("not_a_repository", commands.open_, stray, text="Nowhere", delivery=False)   # not this terminal's path
        with mock.patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": ""}):
            self.refused("not_a_repository", commands.open_, self.folder, text="No terminal", delivery=False)
        self.set_orca(repos={PROJECT: "git"})                       # a Git project's row is never a folder workspace
        self.refused("not_a_repository", commands.open_, self.folder, text="Not a folder", delivery=False)
        self.set_orca(repos={PROJECT: "folder"})
        self.opened()
        for flag in ({"authorize": True, "why": "x"}, {"check": "true"}, {"pr": 7}, {"branch": "b"}, {"pr": 0},
                     {"branch": ""}, {"after_merge": ["c1"]},
                     {"carried": "x", "commit": "HEAD", "paths": ["a"], "verdict": "pass"}, {"waive": "a1.1", "why": "x"}):
            with self.subTest(flag=flag):
                self.refused("folder_no_delivery", commands.record, self.folder, None, **flag)
        self.refused("folder_no_delivery", gate.deliver, self.folder, None)
        self.refused("folder_no_delivery", gate.deliver, self.folder, None, check=True)
        self.refused("folder_no_delivery", gate.deliver, self.folder, "opened-later", check=True)   # whatever the log holds
        self.assertIsNone(self.folder_entries()[0]["pr"])

    def test_profiles_start_in_the_objectives_folder_and_accept_without_git(self):
        self.opened()
        scout = commands.dispatch(self.folder, "layout", profile="scout", title="read the map", spec="Read it.")
        designer = commands.dispatch(self.folder, "layout", profile="ui-design", title="draw the map", spec="Draw it.",
                                     worktree=self.address)
        tasks = self.orca()["tasks"]
        self.assertEqual([tasks[row["task"]]["worktree"] for row in (scout, designer)], [self.address, self.address])
        self.assertEqual([tasks[row["task"]]["repo"] for row in (scout, designer)], [None, None])    # Pod makes no worktree
        packet = tasks[designer["task"]]["spec"]
        self.assertIn(f"Folder: {self.folder}", packet)
        self.assertIn("do not create a repository or a commit", packet)
        self.assertIn("worker_done with what you changed", packet)        # no commit or checks are recorded here
        self.assertNotIn("Base commit", packet)
        self.refused("folder_no_worktree", commands.dispatch, self.folder, "layout", profile="ui-design", title="t",
                     spec="s", worktree="new-child")
        self.refused("folder_binding_mismatch", commands.dispatch, self.folder, "layout", profile="scout", title="t",
                     spec="s", worktree="path:/elsewhere")
        self.refused("folder_no_commit", commands.dispatch, self.folder, "layout", profile="scout", title="t", spec="s",
                     commit="HEAD")
        self.finish(designer["task"], {"outcome": "succeeded", "commit": None, "summary": "drew it"})
        self.refused("folder_no_commit", commands.accept, self.folder, "layout", designer["assignment"], integrated="HEAD")
        accepted = commands.accept(self.folder, "layout", designer["assignment"])
        self.assertEqual(accepted["decision"], "integrated")       # its work stays in the folder
        self.finish(scout["task"], {"outcome": "succeeded", "commit": None, "summary": "read"})
        self.assertEqual(commands.accept(self.folder, None, scout["assignment"])["decision"], "read")

    def test_a_plan_review_needs_no_commit_and_the_last_criterion_closes_the_objective(self):
        self.opened()
        review = commands.dispatch(self.folder, "layout", profile="review-deep", title="challenge the plan",
                                   spec="Challenge this plan.")
        packet = self.orca()["tasks"][review["task"]]["spec"]
        self.assertIn("Review the plan in the task text above. There is no commit.", packet)
        self.finish(review["task"], {"outcome": "succeeded", "verdict": "pass", "findings": [], "summary": "sound"})
        accepted = commands.accept(self.folder, "layout", review["assignment"])
        self.assertEqual(accepted["verdict"], "pass")
        fact = self.folder_entries()[accepted["facts"][-1]]
        self.assertEqual((fact["commit"], fact["tree"]), (None, None))
        status = commands.status(self.folder, "layout", typed=True)
        self.assertEqual((status["workspace_id"], status["target"], status["assignment_records"][0]["assignment"]),
                         (self.address, None, review["assignment"]))
        self.assertIn("nothing to deliver", status["gate"])
        row = commands.listing()["objectives"][0]
        self.assertEqual({key: row[key] for key in ("repository", "worktree", "branch", "pr", "target")},
                         {"repository": f"folder-{PROJECT}", "worktree": str(self.folder), "branch": None, "pr": None,
                          "target": "none"})
        commands.record(self.folder, None, met=["c1"], facts=[str(accepted["facts"][-1])])
        self.assertEqual(commands.status(self.folder, "layout")["state"], "closed")
        self.assertEqual(commands.listing()["objectives"], [])

    def test_sibling_workspaces_in_one_path_keep_their_own_objectives(self):
        self.opened()
        self.terminal("term_b")                                     # a sibling workspace in the same path
        self.refused("folder_binding_mismatch", commands.status, self.folder, "layout")
        self.refused("folder_binding_mismatch", commands.dispatch, self.folder, "layout", profile="scout", title="t", spec="s")
        self.refused("objective_unclear", commands.resume, self.folder, None)      # the path alone chooses nothing
        commands.open_(self.folder, text="Its own work", objective="sibling", criteria=["x"], delivery=False)
        self.assertEqual(commands.status(self.folder, None)["objective"], "sibling")
        rows = commands.listing()["objectives"]
        self.assertEqual(sorted(row["objective"] for row in rows), ["layout", "sibling"])
        self.assertEqual({row["worktree"] for row in rows}, {str(self.folder)})    # repeated paths are listed
        self.terminal("term_a")
        self.assertEqual(commands.status(self.folder, None)["objective"], "layout")
        self.refused("folder_binding_mismatch", commands.dispatch, self.folder, "layout", profile="scout", title="t",
                     spec="s", worktree="folder:f-2")                # a selector never crosses into a sibling
        self.terminal("term_b")
        self.refused("folder_binding_mismatch", commands.open_, self.folder, text="Lay out the agent map",
                     objective="layout", criteria=["a layout"], delivery=False)    # the same id and source from a sibling
        self.terminal("term_a")
        state = self.orca()
        state["folders"].append(self.row("f-1", "i-3"))              # Orca names two workspaces by this terminal's id
        self.set_orca(folders=state["folders"])
        self.refused("folder_workspace_ambiguous", commands.status, self.folder, "layout")

    def test_an_implicit_preview_and_a_missing_selector_report_what_orca_said(self):
        self.opened()
        state = self.orca()
        state["folders"].append(self.row("f-1", "i-3"))
        self.set_orca(folders=state["folders"])
        self.refused("folder_workspace_ambiguous", commands.dispatch, self.folder, None, profile="scout", dry_run=True)
        self.set_orca(terminals={"term_m": "folder:several workspaces"})     # a missing selector is not ambiguous
        self.terminal("term_m")
        self.refused("not_a_repository", commands.status, self.folder, None)

    def test_a_workspace_needs_a_native_instance_and_a_non_root_id_is_its_own_address(self):
        ident = f"{PROJECT}::{self.folder}::workspace:u-9"
        self.set_orca(folders=[{"id": ident, "repoId": PROJECT, "path": str(self.folder), "address": f"id:{ident}",
                                "instanceId": "u-9"}, {**self.row("f-3", None)}],
                      terminals={"term_n": ident, "term_x": "folder:f-3"})
        self.terminal("term_n")
        view = commands.open_(self.folder, text="Non-root", objective="nonroot", criteria=["x"], delivery=False)
        self.assertEqual((view["workspace_id"], view["workspace_instance_id"]), (f"id:{ident}", "u-9"))
        self.terminal("term_x")                                     # Orca gave no instance: nothing can be bound
        self.refused("not_a_repository", commands.open_, self.folder, text="No instance", objective="n2", delivery=False)

    def test_a_lost_start_is_recovered_only_from_orcas_own_receipt(self):
        self.opened()
        first = commands.dispatch(self.folder, "layout", profile="scout", title="read", spec="Read.")
        from pod import orca
        self.assertTrue(orca._says_several("Multiple worktrees found for 'worktrees'.", "worktrees"))
        self.assertFalse(orca._says_several("no workspace folder:several workspaces", "folder:several workspaces"))
        state = self.orca()
        run = state["tasks"][first["task"]]["run_id"]
        state["tasks"]["task_sibling"] = {"id": "task_sibling", "run_id": run, "task_title": "Follow up [layout:a2]",
                                          "status": "dispatched", "result": None, "spec": "", "worktree": "folder:f-2"}
        state["lose_reply"] = True
        self.set_orca(**state)
        self.refused("orca_unknown", commands.dispatch, self.folder, "layout", profile="ui-design", title="again",
                     spec="Again.", paths_=["map"])
        state = self.orca()
        state["requests"] = {key: value for key, value in state["requests"].items()
                             if value["receipt"]["taskId"] == first["task"]}            # Orca kept no receipt for a2
        self.set_orca(**state)
        self.refused("start_unknown", commands.dispatch, self.folder, "layout", retry="a2")   # unknown stays unknown
        rows = [row for row in self.folder_entries() if row.get("id") == "a2"]
        self.assertFalse(any(row.get("void") for row in rows))      # its paths stay held; nothing is started again
        self.assertNotIn("task_sibling", [row.get("task") for row in rows])   # the sibling's marked Task is never a2
        self.refused("paths_overlap", commands.dispatch, self.folder, "layout", profile="ui-design", title="more",
                     spec="More.", paths_=["map"])

    def test_delivery_uses_one_placement_for_its_refusal_and_lookup(self):
        self.opened()
        placements = [commands.place(self.repo), commands.place(self.folder)]   # Git first, then a folder, if read twice
        with mock.patch.object(commands, "place", side_effect=lambda cwd: placements.pop(0)):
            self.refused("objective_unknown", gate.deliver, self.folder, "layout", check=True)
        self.assertEqual(len(placements), 1)                        # read once; the folder objective is never reached

    def test_a_failed_plan_review_never_stands_in_for_another_plan(self):
        self.write_routes(ROUTES + PROFILES + "review_agents: [codex]\n")
        self.opened()
        failed = commands.dispatch(self.folder, "layout", role="review", title="plan A", spec="Review plan A.",
                                   route="codex/sol/high", reason="r")
        self.finish(failed["task"], None, outcome="failed")         # an allowed reviewer failed with no verdict
        for call in (dict(), dict(dry_run=True)):
            with self.subTest(**call):
                self.refused("review_exception_invalid", commands.dispatch, self.folder, "layout", role="review",
                             title="r", spec="Review plan B.", route="claude/opus/high", reason="r", after="a1",
                             why="a1 failed", **call)

    def test_git_repositories_never_ask_orca_for_a_folder_and_config_states_the_capability(self):
        commands.open_(self.repo, text="Make the widget", objective="widget", criteria=["it works"])
        commands.dispatch(self.repo, None, role="implement", title="write", spec="Do it.", paths_=["src"])
        self.assertEqual(self.orca().get("shows", 0), 0)
        self.assertEqual(cli._config(False)["capabilities"], {"folder_no_delivery": True})

    def test_an_objective_whose_workspace_orca_no_longer_lists_is_dropped_by_id_from_anywhere(self):
        self.opened()
        self.terminal("term_b")                                     # a sibling workspace of the same project
        self.refused("folder_binding_mismatch", commands.drop, self.folder, "layout", why="x")
        stray = self.tmp / "stray"
        stray.mkdir()
        self.refused("folder_binding_mismatch", commands.drop, stray, "layout", why="x")       # still listed: not gone
        self.refused("folder_binding_mismatch", commands.drop, self.repo, "layout", why="x")
        self.refused("objective_unknown", commands.drop, stray, "nothing", why="x")
        self.set_orca(folders=[self.row("f-2", "i-2")])             # the bound workspace was removed
        for cwd in (self.repo, stray):
            with self.subTest(cwd=cwd):
                done = commands.drop(cwd, "layout", why="its workspace was removed")
                self.assertEqual(done["state"], "dropped")
        gone = [entry for entry in self.folder_entries() if entry.get("dropped")]
        self.assertEqual([entry["workspace_gone"] for entry in gone],
                         [{"workspace_id": self.address, "instance_id": "i-1", "orca": "selector_not_found"}])     # a second drop adds nothing
        self.assertNotIn("layout", [row["objective"] for row in commands.listing()["objectives"]])

    def test_a_reused_workspace_address_with_another_instance_is_gone_and_unknown_orca_proves_nothing(self):
        self.opened()
        self.terminal("term_b")
        self.set_orca(folders=[self.row("f-1", "i-9"), self.row("f-2", "i-2")])
        real = orca.call

        def silent(*args, **options):                               # Orca does not answer for the bound workspace
            if args[:4] == ("worktree", "show", "--worktree", self.address):
                raise PodError("orca_unknown", "no answer", fix="repeat")
            return real(*args, **options)
        with mock.patch("pod.orca.call", side_effect=silent):
            self.refused("orca_unknown", commands.drop, self.folder, "layout", why="x")
        self.assertFalse(any(entry.get("dropped") for entry in self.folder_entries()))
        commands.drop(self.folder, "layout", why="re-created")
        self.assertEqual(self.folder_entries()[-1]["workspace_gone"]["orca"], "instance i-9")

    def git_log(self, repository, objective, worktree):
        with log.writer(log.path(repository, objective), create=True) as writer:
            writer.append({"kind": "objective", "id": objective, "repository": repository, "worktree": worktree,
                           "target": "main", "branch": "work", "pr": None})

    def test_a_drop_by_id_never_picks_a_git_log_an_empty_file_or_one_of_two_same_id_objectives(self):
        self.opened()
        self.set_orca(folders=[self.row("f-2", "i-2")])            # its workspace is gone
        self.terminal("term_b")
        stray = self.tmp / "stray"
        stray.mkdir()
        self.git_log("folder-acct--repo", "layout", "/elsewhere")  # a Git account whose name starts folder-
        log.path("folder-other", "layout").parent.mkdir(parents=True, exist_ok=True)
        log.path("folder-other", "layout").write_bytes(b"")        # left by a refused open: no objective
        self.assertEqual(commands.drop(stray, "layout", why="gone")["state"], "dropped")
        self.assertFalse(any(entry.get("dropped") for entry in log.read(log.path("folder-acct--repo", "layout"))))
        commands.open_(self.folder, text="Again", objective="again", criteria=["x"], delivery=False)
        self.set_orca(folders=[self.row("f-9", "i-9")], terminals={"term_c": "folder:f-9"})
        self.terminal("term_c")
        repository, _ = commands.gitio.repository(self.repo)
        self.git_log(repository, "again", "/another/worktree")      # the same id in the caller's Git repository
        self.refused("objective_unclear", commands.drop, self.repo, "again", why="x")
        self.git_log(repository, "again-here", str(self.repo))      # at its own worktree a Git drop is as before
        self.assertEqual(commands.drop(self.repo, "again-here", why="x")["state"], "dropped")
        with log.writer(log.path("folder-second", "again"), create=True) as writer:   # a second folder project
            writer.append({"kind": "objective", "id": "again", "repository": "folder-second", "worktree": "/w",
                           "workspace_kind": "folder", "workspace_id": "folder:z", "workspace_instance_id": "i-z",
                           "delivery": "none"})
        self.refused("objective_unclear", commands.drop, stray, "again", why="x")
        self.assertFalse(any(entry.get("dropped") for entry in log.read(log.path(f"folder-{PROJECT}", "again"))))
