"""R215: Pod reads Orca's full orchestration records (`--json --full`), never the concise agent view.

The answers under tests/fixtures/orca-1.4.251 were recorded read-only from Orca 1.4.251, in both forms, and
sanitized: identifiers, handles, paths and free text are replaced, the structure is Orca's own. Run lists keep
their first three Runs."""

import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from pod import orca, wake, worker_tabs
from pod.errors import PodError
from pod.operations import OrcaPort

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "orca-1.4.251"
RUN, DISPATCH, REQUEST = "run-example-1", "ctx-example-1", "00000000-0000-4000-8000-000000000001"


def answer(name: str) -> str:
    return (FIXTURES / (name + ".json")).read_text()


class Native:
    """Orca's process boundary: answers each exact argv from one recorded file and keeps every argv it saw."""

    def __init__(self, answers: dict, *, binary: bool = False):
        self.answers, self.binary, self.calls = answers, binary, []

    def __call__(self, command, **_):
        argv = list(command[1:])
        self.calls.append(argv)
        stdout = self.answers[tuple(argv)]
        return subprocess.CompletedProcess(command, 0, stdout.encode() if self.binary else stdout,
                                           b"" if self.binary else "")


def reads(form: str) -> dict:
    """Each orchestration read Pod makes, as the exact argv Pod sends, with its recorded answer in `form`."""
    return {
        ("orchestration", "run-current", "--json", "--full"): answer("run-current." + form),
        ("orchestration", "run-show", "--id", RUN, "--json", "--full"): answer("run-show." + form),
        ("orchestration", "run-list", "--limit", "100", "--json", "--full"): answer("run-list." + form),
        ("orchestration", "worker-list", "--include-remote", "--json", "--full", "--limit", "100", "--run", RUN):
            answer("worker-list." + form),
        ("orchestration", "worker-show", "--dispatch", DISPATCH, "--json", "--full"): answer("worker-show." + form),
        ("orchestration", "request-show", "--request", REQUEST, "--json", "--full"): answer("request-show." + form),
        ("orchestration", "check", "--run", RUN, "--json", "--full"): answer("check." + form),
    }


CALLS = {
    "run-current": lambda: orca.current_run(),
    "run-show": lambda: orca.run_show(RUN),
    "run-list": lambda: orca.read_command(["orchestration", "run-list", "--limit", "100", *orca.FULL_JSON]),
    "worker-list": lambda: orca.worker_rows(RUN),
    "worker-show": lambda: orca.worker_show(DISPATCH),
    "request-show": lambda: OrcaPort().request_show(REQUEST),
    "check": lambda: orca.mailbox_check(RUN),
}


class ConciseViewTests(unittest.TestCase):
    def test_every_recorded_concise_answer_is_refused_and_names_the_full_record(self):
        native = Native({argv: text for argv, text in reads("concise").items()})
        with patch("pod.orca.executable", return_value=Path("orca")), patch("pod.orca.subprocess.run", native):
            for name, call in CALLS.items():
                with self.subTest(read=name), self.assertRaises(PodError) as caught:
                    call()
                self.assertEqual(caught.exception.code, "orca_contract")
                self.assertEqual(str(caught.exception), orca.CONCISE_VIEW)
                self.assertIn("--json --full", str(caught.exception))

    def test_the_concise_view_carries_no_runtime_identity_and_no_record(self):
        for name in CALLS:
            with self.subTest(read=name):
                concise, full = json.loads(answer(name + ".concise")), json.loads(answer(name + ".full"))
                self.assertTrue(orca.concise_view(concise))
                self.assertFalse(orca.concise_view(full))
                self.assertEqual(set(concise), {"ok", "result"})
        # The facts Pod reads exist only in the full record.
        self.assertNotIn("run", json.loads(answer("run-current.concise"))["result"])
        self.assertNotIn("run", json.loads(answer("run-show.concise"))["result"])
        self.assertNotIn("page", json.loads(answer("worker-list.concise"))["result"])

    def test_a_concise_mutation_answer_is_no_effect_proof(self):
        # Synthetic: Orca's concise view for run-use and worker-start, which Pod cannot record without an effect.
        answers = {("orchestration", "run-use", "--id", RUN, "--json", "--full"):
                       json.dumps({"ok": True, "result": {"runId": RUN}}),
                   ("orchestration", "worker-start", "--task", "t", "--run", RUN, "--worktree", "current",
                    "--agent", "codex", "--model", "m", "--json", "--full"):
                       json.dumps({"ok": True, "result": {"dispatchId": DISPATCH, "state": "ready"}})}
        native = Native(answers, binary=True)
        with patch("pod.orca.executable", return_value=Path("orca")), patch("pod.orca.subprocess.run", native), \
                patch("pod.github.forget_repository_reads"):
            for argv in answers:
                with self.subTest(argv=argv[1]), self.assertRaises(PodError) as caught:
                    orca.mutate_command(list(argv))
                self.assertEqual(caught.exception.code, "native_effect_uncertain")
                self.assertEqual(str(caught.exception), orca.CONCISE_VIEW)
                self.assertEqual(caught.exception.native_observation["argv"][-2:], ["--json", "--full"])


class FullRecordTests(unittest.TestCase):
    def test_every_recorded_full_answer_is_accepted(self):
        native = Native(reads("full"))
        with patch("pod.orca.executable", return_value=Path("orca")), patch("pod.orca.subprocess.run", native):
            results = {name: call() for name, call in CALLS.items()}
        runtime = "00000000-0000-4000-8000-000000000102"
        self.assertEqual(results["run-current"], {"runtime": runtime, "run": None})
        shown = results["run-show"]
        self.assertEqual((shown["run"], shown["consumer_generation"], shown["coordinator_known"]), (RUN, 1, True))
        self.assertTrue(shown["coordinator_handle"].startswith("term_"))
        self.assertEqual([row["id"] for row in results["run-list"]["result"]["runs"]],
                         ["run-example-1", "run-example-2", "run-example-3"])
        workers = results["worker-list"]
        self.assertTrue(workers["complete"])
        self.assertEqual([row["dispatchId"] for row in workers["workers"]],
                         ["ctx-example-1", "ctx-example-2", "ctx-example-3"])
        self.assertEqual(results["worker-show"]["result"]["dispatch"]["id"], DISPATCH)
        self.assertEqual(results["request-show"]["result"]["state"], "absent")
        self.assertEqual(results["check"]["messages"], [])
        self.assertIsNone(results["check"]["delivery"])
        self.assertEqual(native.calls, [list(argv) for argv in reads("full")])


class ArgvTests(unittest.TestCase):
    def test_the_read_allowlist_needs_full_in_its_one_position(self):
        for argv in reads("full"):
            argv = list(argv)
            if argv[1] == "check":
                continue  # The mailbox call is its own exact shape, never a plain read.
            with self.subTest(argv=argv[1]):
                self.assertTrue(orca._read_allowed(argv))
                index = argv.index("--full")
                without = argv[:index] + argv[index + 1:]
                moved = without + ["--full"] if index != len(argv) - 1 else argv[:index - 1] + ["--full", "--json"]
                for shape in (without, moved, argv + ["--full"]):
                    self.assertFalse(orca._read_allowed(shape))
        cursor = ["orchestration", "worker-list", "--include-remote", "--json", "--full", "--limit", "100",
                  "--terminal-state", "active", "--cursor", "c1"]
        self.assertTrue(orca._read_allowed(cursor))
        self.assertTrue(orca._read_allowed(["orchestration", "run-list", "--limit", "100", "--json", "--full",
                                            "--cursor", "c1"]))
        self.assertFalse(orca._read_allowed(["orchestration", "run-list", "--limit", "100", "--json",
                                             "--cursor", "c1"]))

    def test_orcas_other_commands_are_unchanged_and_never_take_full(self):
        for argv in (["status", "--json"], ["terminal", "list", "--json"], ["worktree", "list", "--json"],
                     ["terminal", "show", "--terminal", "term", "--json"],
                     ["worktree", "show", "--worktree", "path:/x", "--json"]):
            with self.subTest(argv=argv):
                self.assertTrue(orca._read_allowed(argv))
                self.assertFalse(orca._read_allowed(argv + ["--full"]))
        with patch("pod.orca.subprocess.run") as runner, self.assertRaises(PodError) as caught:
            orca.read_command(["orchestration", "run-current", "--json"])
        self.assertEqual(caught.exception.code, "unsupported_orca_read")
        runner.assert_not_called()

    def test_the_mutation_allowlist_needs_full_last(self):
        start = ["orchestration", "worker-start", "--task", "t", "--run", "r", "--worktree", "current",
                 "--agent", "codex", "--model", "m"]
        for argv in (["orchestration", "run-use", "--id", "r", "--json", "--full"], start + ["--json", "--full"],
                     start + ["--keep-open", "--retry-request", REQUEST, "--json", "--full"]):
            with self.subTest(argv=argv):
                self.assertTrue(orca._mutate_allowed(argv))
                self.assertFalse(orca._mutate_allowed(argv[:-1]))
                self.assertFalse(orca._mutate_allowed(argv[:-2] + ["--full", "--json"]))
        with patch("pod.orca.subprocess.run") as runner, self.assertRaises(PodError) as caught:
            orca.mutate_command(["orchestration", "run-use", "--id", "r", "--json"])
        self.assertEqual(caught.exception.code, "unsupported_orca_mutation")
        runner.assert_not_called()

    def test_commands_relayed_to_a_coordinator_read_the_full_record(self):
        self.assertEqual(worker_tabs.read_command("d1")[-2:], ["--json", "--full"])
        self.assertEqual(worker_tabs.keep_open_command("d1")[-2:], ["--json", "--full"])
        features = {"capabilities": {"worker_turn_stop_reports": True, "worker_auto_release": True}}
        stopped = wake.classify({"id": "m1", "type": "escalation",
                                 "payload": json.dumps({"stopReason": "finished", "dispatchId": "d1"})}, features)
        self.assertIn("worker-show --dispatch d1 --json --full`", stopped["action"])
        done = wake.classify({"id": "m2", "type": "worker_done",
                              "payload": json.dumps({"outcome": "succeeded", "dispatchId": "d1"})}, features)
        self.assertIn("worker-keep-open --dispatch d1 --json --full`", done["action"])

    def test_the_guidance_tells_a_coordinator_to_read_the_full_record(self):
        references = Path(__file__).resolve().parent.parent / "skills" / "pod" / "references"
        boundary = (references / "orca-boundary.md").read_text()
        self.assertIn("only `--full` prints the whole record", boundary)
        self.assertIn("run it with `--json --full`", boundary)
        guide = (references / "wake.md").read_text()
        for command in ("check --run RUN --all --json --full", "worker-list --run RUN --json --full",
                        "worker-show --dispatch D --json --full", "worker-keep-open --dispatch D --json --full"):
            self.assertIn("`orca orchestration " + command + "`", guide)
        self.assertNotIn("--json`", guide.replace("--json --full`", ""))
