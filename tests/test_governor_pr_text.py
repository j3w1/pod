"""R240: a later publication keeps an existing pull request's title and body current.

`pr_update` opens a pull request with the supplied text when none exists and records that text's digests. When it
reuses one, it reads the text fresh after the push and writes the supplied fields, recording before and after
digests, but only over text Pod itself wrote. Text another writer edited is never overwritten silently: the row
records the conflict for the lead, who may publish again naming exactly the text it saw. The push, the pull
request and the row's outcome are proved as before either way.
"""

import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.github import PR_TEXT_FIELDS, GhPort, gh_allowed
from pod.governor import execute, prepare_candidate
from tests.kernel_support import COMMIT, COMMIT2, COMMIT3, NOW, TREE2, TREE3, GovernorFakePort as FakePort, \
    action, observation
from tests.test_governor import GovernorCase
from tests.test_github import completed

COMMIT4, TREE4 = "3" * 40, "4" * 40
COMMIT5, TREE5 = "5" * 40, "6" * 40
HEADS = {COMMIT2: TREE2, COMMIT3: TREE3, COMMIT4: TREE4, COMMIT5: TREE5}
FIRST = {"title": "Repair the release boundary", "body": "About: unit A\n"}
SECOND = {"title": "Repair the release boundary and its guidance", "body": "About: units A and B\n"}


def digest(text):
    return hashlib.sha256(text.replace("\r\n", "\n").replace("\r", "\n").encode()).hexdigest()


def digests(title, body):
    return {"title_sha256": digest(title), "body_sha256": digest(body)}


class PullRequestTextCase(GovernorCase):
    def setUp(self):
        super().setUp()
        self.binding = self.prepared()["candidate"]
        self.preflight(self.binding["id"])
        self.port = FakePort()

    def publish(self, pull_request=None, *, commit=None, kind="pr_update"):
        """Publish the first candidate, or prepare and publish a later head, through governor-execute."""
        binding = self.binding
        if commit is not None:
            binding = prepare_candidate(self.project, "objective", owner="owner", unit="release",
                                        observation=observation(commit=commit, tree=HEADS[commit]), now=NOW)["candidate"]
            self.preflight(binding["id"])
        extra = {} if pull_request is None else {"pull_request": pull_request}
        result = execute(self.project, "objective", owner="owner", port=self.port, now=NOW,
                         action=self.with_publish_authorization(action(kind=kind, candidate=binding["id"])), **extra)
        self.assertEqual((result["decision"], result["outcome"]), ("ALLOW", "PASS"))
        return result

    def text(self, result):
        return result["receipt"]["provider"].get("pr_text")

    def remote_calls(self, start=0):
        """The publication and pull-request calls, without the triggered-run lookups after each push."""
        return [call[0] for call in self.port.calls[start:] if call[0] != "runs"]


class OpenAndRefreshTests(PullRequestTextCase):
    def test_opening_is_unchanged_and_records_the_written_text(self):
        opened = self.publish(FIRST)
        self.assertEqual(self.remote_calls(), ["push", "pr_list", "pr_create"])
        self.assertFalse(opened["receipt"]["provider"]["pr_reused"])
        self.assertEqual(self.text(opened), {"status": "opened", "after": digests(FIRST["title"], FIRST["body"])})
        self.assertEqual(self.port.texts[7]["title"], FIRST["title"])

    def test_opening_without_text_records_the_reason_title_and_empty_body(self):
        opened = self.publish()
        self.assertEqual(self.text(opened)["after"], digests("converged candidate", ""))

    def test_a_later_head_updates_the_reused_title_and_body_with_digests(self):
        self.publish(FIRST)
        start = len(self.port.calls)
        updated = self.publish(SECOND, commit=COMMIT2)
        self.assertEqual(self.remote_calls(start), ["push", "pr_list", "pr_text", "pr_edit"])
        self.assertTrue(updated["receipt"]["provider"]["pr_reused"])
        self.assertEqual(self.text(updated), {
            "status": "updated", "fields": ["title", "body"],
            "requested": digests(SECOND["title"], SECOND["body"]),
            "before": digests(FIRST["title"], FIRST["body"]), "after": digests(SECOND["title"], SECOND["body"])})
        self.assertEqual((self.port.texts[7]["title"], self.port.texts[7]["body"]), (SECOND["title"], SECOND["body"]))
        # Pod's own update is the next baseline, so a third head updates again without a conflict.
        third = self.publish(FIRST, commit=COMMIT3)
        self.assertEqual(self.text(third)["status"], "updated")

    def test_without_supplied_text_a_reused_pull_request_is_left_as_before(self):
        self.publish(FIRST)
        start = len(self.port.calls)
        reused = self.publish(commit=COMMIT2)
        self.assertEqual(self.remote_calls(start), ["push", "pr_list"])
        self.assertNotIn("pr_text", reused["receipt"]["provider"])
        self.assertIsNone(reused["receipt"]["detail"])

    def test_only_supplied_fields_are_written_and_equal_text_writes_nothing(self):
        self.publish(FIRST)
        self.port.texts[7]["body"] = "A person's own description\n"
        retitled = self.publish({"title": SECOND["title"]}, commit=COMMIT2)
        self.assertEqual(self.text(retitled)["status"], "updated")
        self.assertEqual(self.text(retitled)["fields"], ["title"])
        self.assertEqual(self.port.calls[-1], ("pr_edit", 7, SECOND["title"], None))
        self.assertEqual(self.port.texts[7]["body"], "A person's own description\n")
        start = len(self.port.calls)
        same = self.publish({"title": SECOND["title"], "body": "A person's own description\r\n"}, commit=COMMIT3)
        self.assertEqual(self.text(same)["status"], "unchanged")
        self.assertNotIn("pr_edit", self.remote_calls(start))


class ConflictTests(PullRequestTextCase):
    def conflicted(self):
        self.publish(FIRST)
        self.port.texts[7]["body"] = "Edited by a person\n"
        start = len(self.port.calls)
        held = self.publish(SECOND, commit=COMMIT2)
        self.assertEqual(self.remote_calls(start), ["push", "pr_list", "pr_text"])
        self.assertEqual(self.port.texts[7], {**self.port.texts[7], "title": FIRST["title"],
                                              "body": "Edited by a person\n"})
        text = self.text(held)
        self.assertEqual((text["status"], text["decision"], text["edited"]), ("conflict", "lead", ["body"]))
        self.assertEqual(text["before"], digests(FIRST["title"], "Edited by a person\n"))
        self.assertIn("the lead decides", held["receipt"]["detail"])
        self.assertTrue(held["receipt"]["provider"]["pr_reused"])
        # Unresolved, the next head conflicts again rather than taking the edit as Pod's own.
        again = self.publish(SECOND, commit=COMMIT3)
        self.assertEqual(self.text(again)["status"], "conflict")
        return text

    def test_a_person_edited_pull_request_is_not_overwritten_and_records_a_lead_conflict(self):
        self.conflicted()

    def test_the_lead_replaces_only_exactly_the_text_it_saw(self):
        text = self.conflicted()
        self.port.texts[7]["body"] = "Edited again after the conflict\n"
        stale = self.publish({**SECOND, "replace_edited": text["before"]}, commit=COMMIT4)
        self.assertEqual(self.text(stale)["status"], "conflict")
        self.assertEqual(self.port.texts[7]["body"], "Edited again after the conflict\n")
        seen = self.text(stale)["before"]
        replaced = self.publish({**SECOND, "replace_edited": seen}, commit=COMMIT5)
        self.assertEqual((self.text(replaced)["status"], self.text(replaced)["replaced"]), ("updated", ["body"]))
        self.assertEqual(self.port.texts[7]["body"], SECOND["body"])

    def test_a_whitespace_only_edit_is_a_persons_edit(self):
        self.publish({"title": "t", "body": "code\n"})
        self.port.texts[7]["body"] = "    code\n"
        held = self.publish({"body": "replacement\n"}, commit=COMMIT2)
        self.assertEqual((self.text(held)["status"], self.text(held)["edited"]), ("conflict", ["body"]))
        self.assertEqual(self.port.texts[7]["body"], "    code\n")

    def test_an_older_pod_text_restored_after_an_uncertain_replacement_conflicts(self):
        self.publish(FIRST)
        self.port.texts[7]["body"] = "A person's body\n"
        held = self.publish(SECOND, commit=COMMIT2)
        self.port.lost.add("pr_edit")
        lost = self.publish({**SECOND, "replace_edited": self.text(held)["before"]}, commit=COMMIT3)
        self.assertEqual(self.text(lost)["status"], "uncertain")
        self.port.lost.clear()
        self.port.texts[7]["body"] = FIRST["body"]
        restored = self.publish({"body": "Next revision\n"}, commit=COMMIT4)
        self.assertEqual((self.text(restored)["status"], self.text(restored)["edited"]), ("conflict", ["body"]))
        self.assertEqual(self.port.texts[7]["body"], FIRST["body"])

    def test_text_pod_never_recorded_counts_as_another_writers(self):
        self.port.prs[("agent/release", "main")] = {"number": 9, "url": "https://example.invalid/pull/9",
                                                     "head_sha": None, "draft": False}
        self.port.texts[9] = {"head": "agent/release", "base": "main", "state": "OPEN",
                              "title": "Opened by hand", "body": ""}
        held = self.publish(SECOND)
        self.assertEqual((self.text(held)["status"], self.text(held)["edited"]), ("conflict", ["title", "body"]))
        self.assertEqual(self.port.texts[9]["title"], "Opened by hand")


class UncertainTextTests(PullRequestTextCase):
    def test_an_unread_or_changed_pull_request_is_not_written(self):
        self.publish(FIRST)
        self.port.unreadable.add("pr_text")
        unread = self.publish(SECOND, commit=COMMIT2)
        self.assertEqual((self.text(unread)["status"], self.text(unread)["detail"]), ("unread", "remote_read_failed"))
        self.port.unreadable.clear()
        self.port.texts[7]["base"] = "release"
        moved = self.publish(SECOND, commit=COMMIT3)
        self.assertEqual((self.text(moved)["status"], self.text(moved)["detail"]), ("unread", "pull_request_changed"))
        self.assertNotIn("pr_edit", self.remote_calls())
        self.assertEqual(self.port.texts[7]["title"], FIRST["title"])

    def test_a_lost_edit_leaves_both_texts_as_pods_own(self):
        self.publish(FIRST)
        self.port.lost.add("pr_edit")
        lost = self.publish(SECOND, commit=COMMIT2)
        self.assertEqual(lost["outcome"], "PASS")
        self.assertEqual(self.text(lost)["status"], "uncertain")
        self.port.lost.clear()
        settled = self.publish(FIRST, commit=COMMIT3)
        self.assertEqual(self.text(settled)["status"], "updated")

    def test_a_candidate_prepared_after_the_push_holds_the_edit(self):
        self.publish(FIRST)
        self.port.on_text_read = lambda: prepare_candidate(
            self.project, "objective", owner="owner", unit="release",
            observation=observation(commit=COMMIT3, tree=TREE3), now=NOW)
        held = self.publish(SECOND, commit=COMMIT2)
        self.assertEqual((held["outcome"], self.text(held)["status"], self.text(held)["detail"]),
                         ("PASS", "held", "candidate_moved"))
        self.assertEqual(held["receipt"]["provider"]["remote_head"], COMMIT2)
        self.assertNotIn("pr_edit", self.remote_calls())
        self.assertEqual(self.port.texts[7]["title"], FIRST["title"])

    def test_authority_lost_after_the_push_holds_the_edit(self):
        self.publish(FIRST)
        lost = PodError("standing_authorization_inactive", "Standing authorization is no longer active")
        with patch("pod.governor_effects._revalidate_standing", side_effect=[None, lost]) as standing:
            held = self.publish(SECOND, commit=COMMIT2)
        self.assertEqual(standing.call_count, 2)
        self.assertEqual((held["outcome"], self.text(held)["status"], self.text(held)["detail"]),
                         ("PASS", "held", "standing_authorization_inactive"))
        self.assertNotIn("pr_edit", self.remote_calls())

    def test_unsupported_provider_text_or_a_text_step_fault_keeps_the_publication(self):
        self.publish(FIRST)
        self.port.texts[7]["body"] = "\ud800"
        unsupported = self.publish(SECOND, commit=COMMIT2)
        self.assertEqual((self.text(unsupported)["status"], self.text(unsupported)["detail"]),
                         ("unread", "unsupported_text"))
        self.port.texts[7]["body"] = FIRST["body"]
        def fault():
            raise TypeError("unexpected shape")
        self.port.on_text_read = fault
        faulted = self.publish(SECOND, commit=COMMIT3)
        self.assertEqual((faulted["outcome"], self.text(faulted)["status"], self.text(faulted)["detail"]),
                         ("PASS", "unread", "TypeError"))
        self.assertEqual(faulted["receipt"]["provider"]["remote_head"], COMMIT3)
        self.assertNotIn("pr_edit", self.remote_calls())

    def test_an_edit_refused_before_submission_is_failed_not_uncertain(self):
        self.publish(FIRST)
        self.port.rejected.add("pr_edit")
        refused = self.publish(SECOND, commit=COMMIT2)
        self.assertEqual((refused["outcome"], self.text(refused)["status"]), ("PASS", "failed"))


class InputTests(PullRequestTextCase):
    def test_replace_edited_is_validated_before_any_record_or_effect(self):
        bad = ({"title": "t", "replace_edited": {"title_sha256": "0" * 64}},
               {"title": "t", "replace_edited": {"title_sha256": "0" * 64, "body_sha256": "X" * 64}},
               {"title": "t", "replace_edited": "0" * 64})
        for pull_request in bad:
            with self.subTest(pull_request=pull_request), self.assertRaises(PodError) as refused:
                self.publish(pull_request)
            self.assertEqual(refused.exception.code, "invalid_pull_request")
        with self.assertRaises(PodError) as push:
            self.publish({"replace_edited": digests("t", "")}, kind="push")
        self.assertEqual(push.exception.code, "invalid_pull_request")
        for text in ({"title": "t", "body": "\ud800"}, {"title": "\udfff"}):
            with self.subTest(text=text), self.assertRaises(PodError) as surrogate:
                self.publish(text)
            self.assertEqual(surrogate.exception.code, "invalid_pull_request")
        with self.assertRaises(PodError) as unknown:
            self.publish({"title": "t", "draft": True})
        self.assertIn("replace_edited", str(unknown.exception))
        self.assertEqual(self.port.calls, [])

    def test_guidance_names_the_conflict_and_the_lead_decision(self):
        text = (Path(__file__).resolve().parents[1] / "skills" / "pod" / "references" / "governor.md").read_text()
        for phrase in ("pr_text", "replace_edited", "never overwrites", "`edited`", "`held`"):
            self.assertIn(phrase, text)


class PortTests(unittest.TestCase):
    def setUp(self):
        self.port = GhPort(Path("."))
        self.commands = []

    def run_with(self, responses):
        def runner(command, *, cwd, timeout, mutation):
            self.commands.append((command[1:], mutation))
            return responses.pop(0)
        return patch.multiple("pod.github", _run=runner, gh_executable=lambda: Path("/usr/bin/gh"))

    def test_text_read_and_edit_use_exact_allowlisted_argv(self):
        row = {"number": 7, "url": "u", "state": "OPEN", "headRefName": "agent/x", "baseRefName": "main",
               "title": "T", "body": "B"}
        with self.run_with([completed(json.dumps(row)), completed("https://example.invalid/pull/7\n"),
                            completed("", 1)]):
            self.assertEqual(self.port.pull_request_text(number=7),
                             {"number": 7, "url": "u", "state": "OPEN", "head": "agent/x", "base": "main",
                              "title": "T", "body": "B"})
            self.assertEqual(self.port.edit_pull_request(number=7, title="New", body=""), {"status": "edited"})
            with self.assertRaises(PodError) as unconfirmed:
                self.port.edit_pull_request(number=7, body="only")
        self.assertEqual(unconfirmed.exception.code, "remote_effect_uncertain")
        self.assertEqual(self.commands, [(["pr", "view", "7", "--json", PR_TEXT_FIELDS], False),
                                         (["pr", "edit", "7", "--title", "New", "--body", ""], True),
                                         (["pr", "edit", "7", "--body", "only"], True)])

    def test_an_edit_answer_over_the_output_bound_is_uncertain(self):
        with self.run_with([completed("x" * 10)]), patch("pod.github.MAX_OUTPUT", 5):
            with self.assertRaises(PodError) as unbounded:
                self.port.edit_pull_request(number=7, title="New")
        self.assertEqual(unbounded.exception.code, "remote_effect_uncertain")
        with self.run_with([]), self.assertRaises(PodError) as refused:
            self.port.edit_pull_request(number=7, title=" ")
        self.assertEqual((refused.exception.code, self.commands), ("unsupported_gh_operation", [(["pr", "edit", "7", "--title", "New"], True)]))

    def test_the_allowlist_refuses_other_edit_shapes(self):
        for argv in (["pr", "edit", "7"], ["pr", "edit", "7", "--title", " "], ["pr", "edit", "7", "--title", "x" * 257],
                     ["pr", "edit", "7", "--body", "b" * (16 * 1024 + 1)], ["pr", "edit", "7", "--base", "main"],
                     ["pr", "edit", "7", "--body", "b", "--title", "t"], ["pr", "edit", "7", "--title", "a", "--title", "b"],
                     ["pr", "edit", "x7", "--title", "t"], ["pr", "view", "7", "--json", "title,body"]):
            with self.subTest(argv=argv):
                self.assertFalse(gh_allowed(argv))
        self.assertTrue(gh_allowed(["pr", "edit", "7", "--title", "t", "--body", ""]))


if __name__ == "__main__":
    unittest.main()
