import os
from unittest import mock

from pod import cli, commands, gate, log
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
        for flag in ({"authorize": True, "why": "x"}, {"check": "true"}, {"pr": 7}, {"branch": "b"},
                     {"carried": "x", "commit": "HEAD", "paths": ["a"], "verdict": "pass"}, {"waive": "a1.1", "why": "x"}):
            with self.subTest(flag=flag):
                self.refused("folder_no_delivery", commands.record, self.folder, None, **flag)
        self.refused("folder_no_delivery", gate.deliver, self.folder, None)
        self.refused("folder_no_delivery", gate.deliver, self.folder, None, check=True)

    def test_profiles_start_in_the_objectives_folder_and_accept_without_git(self):
        self.opened()
        scout = commands.dispatch(self.folder, "layout", profile="scout", title="read the map", spec="Read it.")
        designer = commands.dispatch(self.folder, "layout", profile="ui-design", title="draw the map", spec="Draw it.",
                                     worktree=self.address, name="Designer")
        tasks = self.orca()["tasks"]
        self.assertEqual([tasks[row["task"]]["worktree"] for row in (scout, designer)], [self.address, self.address])
        self.assertEqual([tasks[row["task"]]["repo"] for row in (scout, designer)], [None, None])    # Pod makes no worktree
        packet = tasks[designer["task"]]["spec"]
        self.assertIn(f"Folder: {self.folder}", packet)
        self.assertIn("do not create a repository or a commit", packet)
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
        state = self.orca()
        state["folders"].append(self.row("f-1", "i-3"))              # Orca names two workspaces by this terminal's id
        self.set_orca(folders=state["folders"])
        self.refused("folder_workspace_ambiguous", commands.status, self.folder, "layout")

    def test_git_repositories_never_ask_orca_for_a_folder_and_config_states_the_capability(self):
        commands.open_(self.repo, text="Make the widget", objective="widget", criteria=["it works"])
        commands.dispatch(self.repo, None, role="implement", title="write", spec="Do it.", paths_=["src"])
        self.assertEqual(self.orca().get("shows", 0), 0)
        self.assertEqual(cli._config(False)["capabilities"], {"folder_no_delivery": True})
