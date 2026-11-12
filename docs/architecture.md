# Pod architecture

Pod keeps five things and derives the rest. This document is the whole design; command help
and the tests are the contract for behaviour, and
[Pod's Enduring Design Principles](design-principles.md) constrain every change.

## Who owns what

| Owner | Owns | Pod's relation |
| --- | --- | --- |
| Pod | Objective, criteria, facts, the gate, route preferences | Authoritative |
| Orca | Runs and who coordinates them, Tasks, Dispatches, workers, messages, worktrees, request deduplication | Stores ids, reads at use, starts workers only through Orca |
| Git | Commits, trees, blobs, changed paths, ancestry | Reads, never copies |
| GitHub | Pull requests, required checks, merge state, issue text | Reads; performs one merge and one issue update |
| Host | Worktree admission and cleanup, process liveness, names, questions and consent delivery | Offers one list read; calls the host's verifier when one is configured |
| Project | Whether review is required, the merge method, required checks | Reads at the gate, from the target branch |

Dependencies point one way. Nothing Pod needs in order to finish waits on a system that waits
on Pod.

## The five concepts

| Concept | Content |
| --- | --- |
| Objective | Source (issue reference and body digest, or a sentence), target branch, worktree, branch, Orca Run, pull request, one continuation note |
| Criterion | Text and `open`, `met` (with fact numbers or what was observed) or `dropped` (with why). One that can only be proved after a merge is marked so |
| Assignment | Orca Task and Dispatch ids, the digest of the frozen packet, role, declared paths, base commit, requested route |
| Fact | Immutable: a worker result, check, review with findings, CI readback, authorization, waiver, scope change, decision, takeover. Names its commit and tree where it has one |
| Effect | The intent and the read-back outcome of an action Pod performs: a merge, an issue update, a cleanup |

**Storage.** One append-only file per objective at
`<state home>/objectives/<repository>/<objective>.jsonl`, one JSON entry per line, written
under a file lock (`skills/pod/log.py`). State is a fold over the file
(`skills/pod/state.py`). An append names the length it read, so a writer with an older view is
refused. A partial last line was never an entry and is ignored. There is no caller-supplied
sequence, no limit on entries and no snapshot. Records written by 0.21 live beside this
directory, are never read by the core and are never modified; `pod export --to DIR` writes
each of them to plain JSON.

**Closure is derived.** An objective is closed when no criterion is open and every delivery it
opened is merged (or it was opened with `--no-delivery`, or dropped). No command closes one.

## Derived proof

Nothing about applicability is declared (`skills/pod/proof.py`).

- A check fact applies to a candidate with the same tree.
- A review fact applies to a path while the candidate's blob at that path equals the blob at
  the reviewed commit. It lapses when a context path it named changes.
- A candidate is reviewed when every path it changes against its merge base with the target is
  covered by an applicable completed review, by a reviewer who changed nothing, that raised no
  required finding on that path (and none without a path). A review that fails over two files
  therefore still covers the others. A project may also require the review at the exact final
  tree. A context path may be a directory, or `.` for the whole tree.
- A required finding is open until a later review raises nothing required on corrected content
  at its path that the candidate still holds, or a waiver names it. A second opinion on the same
  content and a carried review close nothing, and bringing the content back reopens it.
- A review Orca did not report as completed keeps its findings, covers nothing and closes
  nothing.

So a review of only the corrected paths composes with an earlier full review, and merging the
target branch reopens only files whose content changed in the candidate.

## The writer

Identity is never a terminal handle. Orca binds one coordinator to a Run and refuses
`worker-start` from any other (`consumer_fenced`), and it replays a start that carries a
request id it has seen. Both were verified on Orca 1.4.251. Pod therefore keeps no lease:

- `pod resume` rebinds the Run to the current conversation and logs the takeover.
- `pod dispatch` derives its request id from the objective and the assignment, records the
  assignment before calling Orca, and after a lost reply `--retry` reads Orca instead of
  starting again.
- A merge is at most once because GitHub merges only the exact head Pod names, Pod writes its
  intent first, and every later call reads the pull request before acting.

## Commands

`open`, `dispatch`, `accept`, `record`, `status`, `deliver`, `resume`, `drop`
(`skills/pod/commands.py`, `skills/pod/gate.py`), plus `config`, `models`, `doctor`, `update`
and `export`. Each takes a handful of flags. A refusal has a code, a message and a `fix`.

**Worker result.** A worker finishes through Orca. Pod reads the Task's result from Orca and
takes the last JSON object in the message body that states an `outcome`:

```json
{"outcome": "succeeded", "commit": "<result commit or null>",
 "checks": [{"command": "…", "exit": 0}], "summary": "…"}
```

A review states `"verdict": "pass" | "fail"` and `"findings": [{"path", "required", "text"}]`.
Without a verdict no review is recorded; without a commit nothing about content is recorded.

## The gate

`pod deliver` (`skills/pod/gate.py`) is deterministic and makes no model call.

1. The candidate is the local branch head, with a clean tree, and the pull request's head.
2. The required checks are green on that head, read from GitHub (or the project's own list).
   When no check has reported, GitHub's own merge state must allow the merge.
3. If the project requires review, coverage holds; no required finding is open either way.
4. Every criterion that can be proved before the merge is met or dropped.
5. An authorization covers this repository, target and candidate: the person's words recorded
   for this commit, a standing one for the objective, the coordinator's own when the person's
   preference file says `merge_authorization: coordinator`, or the host verifier's.

Then it merges with the exact head, reads the outcome back, ticks the proved criteria on the
source issue in one edit (or only comments when the issue text changed since `open`) and
comments the evidence. Boxes are never unticked.

`pod deliver --check [--commit SHA]` prints the verdict and changes nothing: `pass` covers
conditions 1 to 4 and `authorized` reports the fifth separately, because a host that performs
the delivery supplies its own authorization. It is for a host
that performs delivery itself. A delivery someone else performed is recorded once the pull
request shows merged into the target at the candidate, or, for a commit the objective recorded
work or proof for, Git shows it on the target. A withdrawal of authorization holds until the
person authorizes again.

## Cleanup when an objective ends

Pod plans nothing and keeps no list of resources (`skills/pod/cleanup.py`). When an objective
closes or is dropped, the command that ended it (`deliver`, a `record` that meets the last
criterion of an objective without a delivery, or `drop`) reads the objective's workers from
Orca and:

- asks Orca to release each settled worker whose terminal Orca reports as reclaimable;
- asks Orca to remove each worktree the objective itself asked Orca to create for an assignment
  (`pod dispatch --worktree new-…`), when Git shows it is clean and every commit it holds was
  delivered or is on the target, and then deletes that worktree's branch.

Nothing is forced. A worker still running, a terminal kept open, a worktree with uncommitted,
untracked or undelivered work, one another worker or a person's terminal is using, anything
Orca cannot be read about, the objective's own worktree and branch, and everything belonging
to another objective are left exactly as they are and named with the reason. A removal that
fails says what remains and never changes the delivery; running the same command again tries
again. The only record is one effect entry saying what was released and removed. There is no
cleanup command, plan, state or host dependency; a host that manages worktrees itself still
reads `pod status --all --json`, in which a closed or dropped objective holds nothing.

## Interfaces other tools may use

- **The list read.** `pod status --all --json` returns `pod-objectives/v1`: each open
  objective with its repository, worktree, branch, Run, pull request and the assignments whose
  results are not yet accepted. It reads only Pod's own files.
- **The check-only verdict**, above.
- **The verifier hook.** When the preference file names an `authorization_verifier` command,
  Pod sends it one JSON request on standard input,
  `{"schema": "pod-authorization/v1", "action": "merge", "objective", "repository", "target",
  "pr", "candidate", "tree"}`, and accepts only a verdict that repeats `schema`, `action`,
  `repository`, `target`, `pr` and `candidate` with `"verified": true`. Anything else is not an
  authorization.

## Project rules

`.pod/config.yaml` on the target branch, so a change cannot relax the gate it passes through:

```yaml
schema: pod-project/v1
review: required          # or none
final_tree_review: false  # true: the passing review must be of the exact final tree
merge_method: merge       # or squash, rebase
required_checks:          # optional; default is GitHub's required checks
- linux
```

No file means no rule. A file this version cannot read requires review and says why.

## Preferences

`<config home>/config.yaml` is the only authority for enabled routes, Preferred, Pin,
`merge_authorization` (`person` by default, or `coordinator`) and the verifier command. Only
the person changes it, through `pod models` or an editor. Coordination reads it with
`skills/pod/routefile.py` and loads nothing else about models.

## Models

`pod models` lives in `skills/pod/models/`, which only that command imports.

| State | Source |
| --- | --- |
| Observed publicly | The last good leaderboard snapshot in the cache |
| Known to the installed adapter | The adapter's own local catalog, read defensively |
| Routable | `skills/pod/models/registry.json` |
| Enabled | The preference file |
| No route | Everything else, with the reason |

- **Registry.** An entry is an id, the public name, the agent and the efforts. One rule maps
  leaderboard row names (`Name (effort)`, optionally "with fallback" or "non-reasoning").
  Adding a model is a data change and enables nothing.
- **Refresh** is explicit (`pod models refresh` or the key in the workspace): one bounded
  fetch, no retries, no credentials. Model, creator, the intelligence index and cost per task
  are required; other known columns are optional metrics that read as unavailable when absent;
  unknown columns are listed in diagnostics. A missing required column fails the whole refresh
  and the last good snapshot stays. A snapshot records the methodology the page states.
- **Workspace.** A keyboard-driven terminal table: sort, search, provider filter, enable and
  disable, Preferred, Pin, a bulk change per model, one detail panel. It prints a plain table
  when there is no terminal. A public observation never makes a route, and no refresh or
  update changes a preference.
- **High-value mark.** A cost is marked when no other routable route costs less within 2 index
  points, and its index is at or above the median of routable routes with both measurements.
  Rows with an estimated index or a fallback profile are left out.
  It is benchmark-relative decision support and changes no state.

## Tests and CI

Three plain `unittest` jobs, `core`, `models` and `install`, behind one job that resolves the
latest stable CPython and the required `linux` aggregate. `tests/support.py` gives every test
its own Pod homes; `tests/core/support.py` adds a repository and stand-ins for `orca` and `gh`
that reproduce the behaviours Pod relies on.
