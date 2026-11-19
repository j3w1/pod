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
  therefore still covers the others; one that fails without naming any required finding covers
  nothing. A project may also require the review at the exact final
  tree. A context path may be a directory, or `.` for the whole tree.
- A required finding is open until a later review of corrected content at its path, content
  the candidate still holds, passes or fails only over other paths, or a waiver names it. A second opinion on the same
  content and a carried review close nothing, and bringing the content back reopens it.
- Finding locations `P:LINE` and `P:LINE:COL` match the unique file `P` in the original reviewed
  tree; a literal tracked filename takes precedence. Unknown or ambiguous locations stay open
  and prevent that review from covering paths. Recorded locations remain unchanged.
- Review dispatch includes required finding paths changed since their original review, within
  the requested scope, even when their correction leaves no net diff against the target.
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

Pod plans nothing and keeps no list of resources (`skills/pod/cleanup.py`). The command that
ends an objective (`deliver`, a `record` after which no criterion is open and the objective is
closed, or `drop`) reads the objective's workers from Orca and:

- asks Orca to release each worker Orca shows as finished whose terminal it reports as
  reclaimable and still its own to release;
- asks Orca to remove each worktree the objective itself asked Orca to create for an assignment
  (`pod dispatch --worktree new-…`), when its worker is shown as finished, Git shows it is
  clean and every commit it holds was delivered or is on the target, no other worker,
  objective or terminal is using it, and Orca has confirmed the release of its worker's
  terminal; it then deletes that worktree's branch.

Orca keeps the terminal of a worker that got its own worktree open and reports it exactly like
one a person kept open, so that terminal goes with its worktree and is released only when the
worktree may be removed. Any other terminal kept open is never released.

Nothing is forced. A worker still running or not shown as finished, a terminal that is not
Orca's to release, a worktree with uncommitted, untracked or undelivered work or in use by
anyone else, and anything Orca cannot be read about are left exactly as they are and named
with the reason. The objective's own worktree and branch and everything belonging to another
objective are never considered at all. A removal that fails says
what remains and never changes the delivery; `pod deliver` (or `pod drop` for a dropped
objective) run again tries again. The only record is one cleanup effect, an intent and its
outcome, saying what was released and removed. There is no cleanup command, plan, state or
host dependency; a host that manages worktrees itself still reads `pod status --all --json`,
in which a closed or dropped objective holds nothing.

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

`<config home>/config.yaml` is the authority for route preferences, `merge_authorization`
(`person` by default, or `coordinator`) and the verifier command. Only the person changes it,
through `pod models` or an editor. Coordination reads it with `skills/pod/routefile.py`.

- `routing` defaults to `preferred`. Set `routing: balance` with `pod config edit` to choose
  between enabled Codex and Claude routes. `codex_share` is an integer percentage from 1 to
  99 and defaults to 50. Claude receives the rest. An explicit worker route or a Pin takes
  precedence. Within the selected family, Pod uses Preferred if eligible, then the first
  enabled route in file order. When only one family is eligible it receives automatic work.
- The split comes from the last 20 confirmed Pod dispatches in the last seven days across this
  Pod state directory. Effective routes are used when recorded. Pod compares the prospective
  split after the next dispatch with the target and chooses the closer result. Preferred breaks
  a tie. The split reflects dispatches, including explicit choices, and can temporarily differ
  from the target after overrides, simultaneous choices or rounding. The read budget is 8,192
  filesystem entries and 16 MiB of recently modified logs. If history is unreadable or exceeds
  either limit, automatic selection falls back to the eligible Preferred route.
  Provider quota and health checks are outside this rule.
- `coordinator_routes` is an optional YAML list of exact `agent/model/effort` routes. With it,
  a host may restrict coordinators to enabled coding routes and their allowed efforts. An
  empty list excludes all routes. `pod config --coordinator --json` reports
  `coordinator.eligible`, `coordinator.selected` and the selection `basis`. A Pin outside
  the list yields no eligible route. The host owns the launch. Coordinator launches by CE Dev
  are outside the measured split until the CE Dev pin follow-up.

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
  GPT-6.2 Astra and GPT-6.2 Sol use the Codex IDs `gpt-6.2-astra` and `gpt-6.2-sol`, each
  with low, medium, high, xhigh, max and ultra efforts. Adding a model enables nothing.
- **Refresh** is explicit (`pod models refresh` or the key in the workspace): one bounded
  fetch, no retries, no credentials. Model, creator, the intelligence index and cost per task
  are required; other known columns are optional metrics that read as unavailable when absent;
  unknown columns are listed in diagnostics. A missing required column fails the whole refresh
  and the last good snapshot stays. A snapshot records the methodology the page states.
  The current public response includes the collapsed table and structured records for its
  expanded profiles. Pod reads the records as bounded JSON, retaining the release, creator,
  raw profile qualifier, full-precision measurements and units. Task duration is converted
  from seconds to minutes. An explicitly expandable table requires complete grouped data.
  Earlier standalone tables are still supported. Missing or unreadable grouped records and
  snapshots exceeding the cache read limit fail the refresh before replacing the last good copy.
  Public profiles with different fallback qualifiers remain separate. An unpublished registry
  effort has no public measurements. Refresh leaves enabled routes, Preferred, Pin and merge
  authorization as the person configured them.
- **Workspace.** The interactive browser groups public observations and routable efforts by
  provider and model. ↑↓ moves between models. Each model starts with its first profile under
  the active sort. ←→ cycles the selected model’s exact efforts and public profiles. The
  browser shows each profile’s numeric intelligence index with a bar scaled to the largest
  visible index, and the benchmark cost per task. The selection pane shows publication and
  route state, measurements and guidance. On narrow terminals it sits below the list.
  Enter opens scrollable details and sources, `/` searches, and `?` shows all shortcuts.
  Only explicit route edits change preferences. The plain table keeps its OpenAI and Opus
  selection. JSON diagnostics can include every provider with `--all`.
  The header shows the recent Codex and Claude dispatch split and whether balance is enabled.
- **High-value mark.** A cost is marked when no other routable route costs less while scoring
  no more than 2 index points lower, and its index is at or above the median of routable
  routes with both measurements. Rows with an estimated index or a fallback profile are left
  out. It is benchmark-relative decision support and changes no state.

## Tests and CI

Three plain `unittest` jobs, `core`, `models` and `install`, behind one job that resolves the
latest stable CPython and the required `linux` aggregate. `tests/support.py` gives every test
its own Pod homes; `tests/core/support.py` adds a repository and stand-ins for `orca` and `gh`
that reproduce the behaviours Pod relies on.
