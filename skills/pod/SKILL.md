---
name: pod
description: Use when the user invokes Pod ($pod or /pod) to deliver an objective or GitHub issue through supervised Orca workers, or to continue, check or close a Pod objective.
license: MIT
compatibility: Linux, the latest stable CPython and Orca.
metadata:
  source: https://github.com/j3w1/pod
---

# Pod

You are the coordinator, in this conversation, with your current model. Pod is a small command
that keeps what no other system keeps: the objective, its criteria, the facts that prove them
and one delivery gate. Orca owns workers, Git owns content, GitHub owns checks and merges. Pod
reads them when it needs them; never copy their state into Pod or into your messages.

## Decide how much process the task needs

- **Direct work.** A change you can make and verify yourself, with no pull request gate to
  pass, needs no objective. Do it and report what you observed.
- **An objective.** Open one when the work is delivered through a pull request, uses workers,
  or must survive this conversation. `pod open --issue N` or `pod open --text "…"`.
- **Several objectives.** A programme is several objectives under one parent issue. Keep each
  small enough to deliver on its own.

Workers are for work that is independent, bounded and worth a second session: a separable
implementation, a review by someone who did not write the change, an investigation that would
flood your context. Do the rest yourself.

## The eight commands

| Command | What it does | What you state |
| --- | --- | --- |
| `pod open` | Reads the issue or sentence, returns criteria | The source; an existing `--branch`, `--pr`, `--run` when continuing earlier work |
| `pod dispatch` | Starts one worker through Orca | `--role`, `--title`, the task text, `--paths`, optionally `--route` |
| `pod accept A` | Reads the worker's result and the diff, records the facts | Only the decision: `--integrated COMMIT` or `--discarded` |
| `pod record` | Adds what only you know | A criterion met with its facts, the person's words, a scope change, the note |
| `pod status` | The derived picture and what delivery still needs | Nothing |
| `pod deliver` | Runs the gate and merges the exact candidate once | Nothing; `--check` only prints the verdict |
| `pod resume` | Continues an objective from a new conversation | Nothing |
| `pod drop` | Stops an objective | `--why` |

Every command accepts `--help` and `--json`. A refusal prints a `fix:` line; do what it says
instead of trying variations. There is nothing to look up before a call and no sequence
number, state or wait to maintain.

## The normal path

1. `pod open`. Read the criteria it returns. If one can only be proved after the merge, say
   so: `pod record --after-merge c3`.
2. Work directly, or `pod dispatch` workers. Give each worker a task text that stands on its
   own: the goal, what is already known, the files to read, what done looks like. Declare
   `--paths` for an implementer; two implementers never run on overlapping paths.
3. Orca wakes you when a worker reports. `pod accept a1 --integrated COMMIT` after you have
   brought its commit into the branch (or `--discarded`). Read what it flags: paths outside
   the declared set, a missing result commit, findings.
4. `pod record --met c1 --facts 6,7` when a criterion is proved. Use the fact numbers the
   commands printed, or `--why` with what you observed yourself.
5. Push, open the pull request, `pod record --pr N` if Pod does not find it, then
   `pod deliver`. It lists what is missing, or merges.

`pod record --check "COMMAND"` runs a check and records the exit it observed, bound to the
commit when the tree is clean. Prefer it to describing a run in words.

## Review

Whether review is required is the project's rule (`.pod/config.yaml` on the target branch),
not yours and not Pod's. When it is required, every path the candidate changes needs a passing
review by a worker that changed nothing.

- Dispatch a reviewer with `--role review` on a committed candidate. Ask for every finding in
  one pass; reviewers report, the writer corrects.
- A review keeps covering a file while that file's content stays the same. After a correction,
  review only the changed paths (`--paths`); after merging the target branch, only files whose
  content changed in the candidate. Name `--context` paths when a review depends on a file it
  does not cover.
- A required finding stays open until a later review passes on the corrected file, or the
  person waives it; a second reviewer's pass on the same content does not close it: `pod record --waive a2.1 --why "…" --words "what they said"`.

## Evidence

- Record only what was observed. If a check was not run, or a result could not be read, say
  so; it stays unknown. Never restate a worker's claim as your own observation.
- Worker success, local checks, hosted checks, independent review, project acceptance and the
  merge are separate facts. Report them separately.
- The person's decisions are theirs. For a merge authorization, a waiver or a scope change,
  ask, then record their words with `--words`. Only when their preference file says
  `merge_authorization: coordinator` may you authorize a merge yourself.

## Continuing and stopping

- Before your context runs short, `pod record --note "…"` with what a fresh conversation needs:
  the plan, what is waiting, what to do next. Keep it current; it replaces the previous note.
- A new conversation runs `pod resume` in the objective's worktree. It takes the Orca Run over
  and prints the status. No proof that the earlier conversation ended is needed.
- After a lost reply from `pod dispatch`, run `pod dispatch --retry aN`. It reads Orca and
  never starts a second worker. After a lost reply from `pod deliver`, run it again; it reads
  GitHub first and never merges twice.
- An objective closes by itself when its delivery is merged and no criterion is open. Nothing
  closes it by hand. `pod drop --why "…"` stops one that will not be delivered.

## Routes and models

Workers run only on routes the person enabled; the Pin, when set, is the only route used.
`pod dispatch` uses the Pin, else the Preferred route, else asks you to name one of the enabled
routes it lists. Choose by the task: its difficulty, risk and size. `pod models` is the
person's workspace for comparing and enabling routes; do not change their preferences.

## Document types

Pod delivers an ordinary issue or a PES; a Proof of Done checklist, when present, becomes the
criteria. It refuses an issue titled `EEL:` or `SEAL:`. The authoring templates are in
`references/pes-template.md`, `references/seal-template.md` and `references/eel-template.md`;
read one only when asked to write such a document.
