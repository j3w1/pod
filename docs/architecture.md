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
| Assignment | Orca Task and Dispatch ids, the digest of the frozen packet, role, declared paths, base commit, requested route, review exception when used |
| Fact | Immutable: a worker result, check, review with findings, CI readback, authorization, waiver, scope change, decision, takeover. Names its commit and tree where it has one |
| Effect | The intent and the read-back outcome of an action Pod performs: a merge, an issue update, a cleanup |

**Storage.** One append-only file per objective at
`<state home>/objectives/<repository>/<objective>.jsonl`, one JSON entry per line, written
under a file lock (`skills/pod/log.py`). State is a fold over the file
(`skills/pod/state.py`). An append names the length it read, so a writer with an older view is
refused. A partial last line was never an entry and is ignored. There is no caller-supplied
sequence, no limit on entries and no snapshot. Records written by 0.21 live beside this
directory, are never read by the core and are never modified.

**Closure is derived.** An objective is closed when no criterion is open and every delivery it
opened is merged (or it was opened with `--no-delivery`, or dropped). No command closes one.

**Folder workspaces.** Outside every Git worktree, Pod runs in the registered Orca folder
workspace of the calling terminal (`ORCA_TERMINAL_HANDLE`): `orca terminal show` names the
workspace, `orca worktree show` gives its address and instance, and the project must be of kind
`folder`. Every workspace of a folder project can share one path, so the binding is the
workspace address (`workspace_id`: `folder:<id>`, or `id:` and the workspace's own Orca id) and
its native instance (`workspace_instance_id`, required); the path is checked as an attribute and
never chooses the objective. A selector Orca refuses as naming several workspaces is refused
(`folder_workspace_ambiguous`). Every command checks the binding on the state it folds under its
lock, an open of an existing id included, and a lost start is recovered only from Orca's own
receipt, never from a title marker another workspace in the Run could share. An earlier failed
plan review never stands in for another (`--after` is refused). Such an objective is opened with
`--no-delivery`, logs under `folder-<project id>`, and has no branch, pull request, target or
base. Workers start in that same workspace (`--worktree` must name it exactly; Pod makes no
worktree for them), a review reviews the plan in its task text, and an implement result is
accepted without a commit. Every delivery command (`deliver`, `record --authorize`, `--check`,
`--waive`, `--carried`, `--commit`, `--pr`, `--branch`) refuses with `folder_no_delivery`.
`pod config --json` reports `capabilities.folder_no_delivery: true`. Git repositories never
consult Orca for this.

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
  starting again. For `new-*` worktrees it derives a human `--display-name` and `--name` slug from
  the assignment title (unless `--name` is given). After a successful start it reads the worker's
  current tab title and, only when it is empty, matches `worker-<taskId>` for the observed task,
  matches the exact `--task-title` Pod passed, or is a daemon's `<Role> · <task title>` rename that begins the task title with this assignment's own `[<objective>:<ident>]` marker, renames it to
  `<Role> │ <model>/<effort> │ <description>` (U+2502 bar with one space each side, effort `medium` shown as `med`, description cut to 34 characters with `…`, no marker, no assignment id, using profile role labels such
  as `Builder`, `Builder (deep)`, `Builder (UI)` for `ui-*`, `Reviewer (fast)`, `Reviewer (deep)`,
  `Scout`, `Scout (deep)`, `Analyst` or `Challenger` and short model names); an unreadable title
  or failed rename is a `note`. Single-owner rule: Pod names only the workers it dispatches, once,
  and only from a default title; it never touches role or coordinator tabs. Downstream daemons can
  identify Pod dispatches by two stable markers: the `worker-start` `--task-title` begins with
  `[<objective>:<ident>]` (objective id and assignment id, e.g. `[pod-…:a3]`, placed first so
  80-character readback truncation usually preserves it; `--retry` recovery also recognizes the
  earlier `· <objective>:<ident> ` form), and request ids are derived from the objective and
  assignment (`orca.request_id`). Orca has no atomic conditional rename, so this read-then-rename
  guard accepts a narrow concurrent-rename window. A start recovered by `--retry` also applies the
  guarded rename when its effective route is known.
  `pod status --json` keeps the formatted `assignments` strings and adds `assignment_records`:
  per assignment `requested_route` and `effective_route` (`{agent, model, effort, fallback_tag}`,
  plus `fallback_reason` when recorded) and `events` (`kind` is `dispatched`, `accepted` or
  `review_verdict`, `at` is epoch seconds or null, `detail`, in log order; `dispatched` needs a
  recorded Orca dispatch and `effective_route` is null where no launch was observed; both list
  keys are always present, possibly empty), plus a
  top-level `events` list of `merged` events (only on `pod status --json`). All come from the objective log; Pod records no separate "reported" event.
  `pod config edit` edits a copy and saves under the same `.config.lock` as `pod models`: it
  refuses a symlinked or non-regular file, a failed or missing editor and an editor output that is
  not a regular file; under the lock it first checks the live file is the same file with the same
  bytes (`preferences_changed`, the edited copy is kept and its path reported), then that the
  result is readable if the original was, and keeps the live file's mode. Known limit: an editor that
  returns but keeps the copy open and writes again after Pod's last check is not detected.
  **Known limits (0.23.4):** author families for reviewer filtering and the recorded
  `author_families` still come from the whole objective, not the reviewed commit; only the
  mixed-author fallback decision and its counts are scoped to integrated work (`contained: true`)
  in the reviewed commit's history (tracked in pod#348).
  Also: retry recovery matches Tasks by a marker in the title read back from Orca
  (truncated to ~80 chars; supported objective ids up to 80 chars can still lose it), may bind a
  Task whose topic quotes another assignment's marker, and does not settle a completed request
  receipt with an unknown start; hardening is tracked in a follow-up issue.
- A merge is at most once because GitHub merges only the exact head Pod names, Pod writes its
  intent first, and every later call reads the pull request before acting.

## Commands

`open`, `dispatch`, `accept`, `record`, `status`, `deliver`, `resume`, `drop`
(`skills/pod/commands.py`, `skills/pod/gate.py`), plus `config`, `models`, `doctor`
and `update`. Each takes a handful of flags. A refusal has a code, a message and a `fix`.

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
- **The routing authority export.** `pod models --json` includes a stable `routing` block
  (`schema: "pod-routing/v1"`) that downstream tools such as CE Dev can render directly into
  documentation without parsing `config.yaml`:

  ```json
  {
    "schema": "pod-routing/v1",
    "status": "ok",
    "preferred": null,
    "pinned": null,
    "default_profiles": {"implement": "build-fast", "review": "review-fast", "investigate": "scout"},
    "review_agents": ["codex", "antigravity", "claude"],
    "profiles": {
      "build-fast": {"role": "implement", "mcp": false, "routes": ["..."], "spec": "implement ..."}
    },
    "pauses": {
      "paused_agents": [{"agent": "agy-gemini", "reason": "...", "until": null, "ttl": null, "active": true}],
      "soft_drained_agents": [{"agent": "agy-claude", "reason": "...", "until": null, "ttl": null, "active": true}]
    }
  }
  ```
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

`<config home>/config.yaml` (`schema: pod/v2`) is the only authority for enabled routes,
Preferred, Pin, `profiles`, `default_profiles`, `review_agents`, `paused_agents`, `soft_drained_agents`,
`merge_authorization` (`person` by default, or `coordinator`) and the verifier command. Only the
person changes it, through `pod models` or an editor. Coordination reads it with
`skills/pod/routefile.py` and loads nothing else about models.

- **Profiles and role defaults.** An optional flat `profiles:` map defines one line per profile:
  `<role>[+mcp] <route1> [<route2> ...]`. The first route is the default and the rest are the
  ordered fallback chain; `+mcp` declares MCP access (default: no MCP). An optional
  `default_profiles:` map sets the default profile per role; when absent, Pod reads the shipped
  role mapping (`implement: build-fast`, `review: review-fast`, `investigate: scout`) directly
  from `skills/pod/models/registry.json` whenever those profiles exist in `config.yaml`, so
  existing installs pick up the same mapping without rewriting `config.yaml`. A dispatch with
  neither `--profile` nor `--route` resolves by role to that default profile and never picks the
  highest-ranked route. An explicit `--route` outside every configured profile (including when
  `profiles` is empty or absent, or when the explicit `--route` equals `Pin`) is refused
  (`route_outside_profiles`) unless `--reason TEXT` is given, and the reason is recorded on the
  assignment row. For existing pre-profile configs or configs with only custom profile names and
  no matching `default_profiles` entry, role-only dispatch still falls back to `preferred` (or
  refuses with `route_required` when `preferred` is `null`); users can opt into role-based
  routing while preserving their file by adding profiles (`pod models --profile NAME=SPEC`) and
  defaults (`pod models --default-profile ROLE=NAME`), or by passing `--profile` on `pod dispatch`.
  `pod dispatch`, `pod dispatch --dry-run` and `pod models` What-If share the same pure
  `resolve_dispatch()` function, respecting active `paused_agents` and `soft_drained_agents`
  holds (by quota pool id such as `agy-gemini`, `agy-claude`, `codex`, `claude-max`, or by agent
  name, with optional reason and TTL/`until` expiry), `--exclude-agent` and fail-closed `Pin`.
- **Review agents & reviewer notes.** An optional `review_agents` list such as
  `[codex, antigravity, claude]` limits new review dispatches to those agents in preference order.
  Review independence compares model families (`provider`: `Anthropic`, `OpenAI`, `Google`) rather
  than raw agent names. For automatic and role-only review selection (`--role review` or
  `--profile review-*` without an explicit `--route`), candidates whose model family authored the
  diff (any accepted `implement` dispatch, including mixed authorship after a handoff) are
  filtered out rather than merely deprioritized, and the remaining candidates are ordered by
  `review_agents`. When every enabled other-family reviewer route in the candidate list is
  unavailable because its agent or quota pool has an active quota hold in `paused_agents` or
  `soft_drained_agents` (unreasoned hold or quota/rate-limit reason, not a non-quota hold
  such as maintenance, exclusion or unsupported effort), Pod falls back to an eligible non-Claude
  author-family route within `review_agents` (never selecting Claude-family routes, bypassing
  `review_agents` or manual `Pin`, or using a paused/soft-drained route), records
  `review_independence: same_provider_fallback` and `review_fallback_reason` (naming the
  unavailable agent or pool) bound to the observed effective launch on the assignment and review
  fact, and still requires a fresh session; once an other-family reviewer returns, new reviews go
  back to cross-family. Explicit review exceptions (`--words` or `--after aN --why "…"`) remain
  accepted in the quota-fallback case. If no independent candidate remains for any other reason
  (disabled routes, non-quota holds, `--exclude-agent`, unsupported effort, no other-family route
  in the profile, or a pinned author-family route) and at least one otherwise-eligible candidate
  was filtered for matching an author family, Pod refuses with `reviewer_same_family` unless a
  review exception (`--words` or `--after aN --why "…"`) is given; if every candidate was
  unavailable because it was disabled, paused, soft-drained or excluded, Pod refuses with
  `no_eligible_route`. Each review records a
  factual `review_independence` tag (`cross_provider`, `cross_provider_fallback`, `same_provider`,
  `same_provider_fallback`, `mixed_author_fallback`, `unknown_author`, or `fallback_unknown_author`).
  When every enabled non-Claude family authored part of the reviewed commit's history (two or more
  author families counted from integrated implement work that is an ancestor of the reviewed commit,
  and no enabled non-Claude family left out), the review does not refuse for same-family: Pod picks
  the author family with the fewest such assignments among routes that are enabled, allowed and not
  held (ties by `review_agents` order, never Claude, a Pin still wins) and records
  `mixed_author_fallback` with a `review_fallback_reason` beginning `mixed authors:`; if every
  such route is held, the review is refused.
  Reviews must run in a separate session from any implementer, and optional reviewer `notes` (or
  partial findings on an interrupted review) are persisted in the objective log and passed into
  successor dispatches. Omit `review_agents` or use `[]` to allow any enabled agent. To use an
  agent outside `review_agents`, or to override a `reviewer_same_family` refusal on automatic or
  role-only review selection, give the person's own `--words` or use `--after aN --why "…"` after
  an allowed review of the same commit failed or was voided without a review result. An explicit
  `--route` on an allowed agent (with `--reason` when outside configured profiles) is not
  filtered by author family and records its factual `review_independence` tag.
- **Antigravity wire format.** `routefile.wire_route()` translates model-suffix routes such as
  `antigravity/gemini-4-neon/<effort>` and `antigravity/claude-opus-5-5/<effort>` to
  `--agent antigravity --model <id>-<effort>` (omitting `--effort`), refusing efforts not listed
  for that `(agent, model)` in `registry.json`. `canonical_effective()` normalizes Orca's
  `launch.effective` back to the 3-part canonical route.
- **Implement concurrency.** At most one live `implement` assignment may run in the same worktree
  (`worktree_busy`); a second concurrent implementer requires `--worktree new-child`, which Orca
  creates from the objective's branch in the repository's main checkout (the one Orca registers),
  even when Pod runs in a linked worktree. Scouts and reviewers may run concurrently.

## Models

`pod models` lives in `skills/pod/models/` (interactive TUI in `skills/pod/models/tui.py`), which
only that command imports. The view, adapters and TUI are scoped strictly to Anthropic, Google and
OpenAI. Every route, profile, `default_profiles`, `paused_agents` and `soft_drained_agents` edit
has a CLI flag twin (`--enable`, `--disable`, `--preferred`, `--pin`, `--pause`, `--drain`,
`--unpause`, `--reason`, `--ttl`, `--profile`, `--delete-profile`, `--default-profile`,
`--clear-default`, `--review-agents`).

| State | Source |
| --- | --- |
| Observed publicly | The last good leaderboard snapshot in the cache |
| Known to the installed adapter | The adapter's own local catalog (Codex cache, or cached `agy models` after explicit refresh), read defensively |
| Routable | `skills/pod/models/registry.json` (also the single shipped source of initial defaults) |
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
  route state and measurements. On narrow terminals it sits below the list.
  Enter opens scrollable details and sources, `/` searches, and `?` shows all shortcuts.
  Only explicit route edits change preferences. The plain table keeps its OpenAI and Opus
  selection. JSON diagnostics can include every provider with `--all`.
- **High-value mark.** A cost is marked when no other routable route costs less while scoring
  no more than 2 index points lower, and its index is at or above the median of routable
  routes with both measurements. Rows with an estimated index or a fallback profile are left
  out. It is benchmark-relative decision support and changes no state.

## Tests and CI

Three plain `unittest` jobs, `core`, `models` and `install`, behind one job that resolves the
latest stable CPython and the required `linux` aggregate. `tests/support.py` gives every test
its own Pod homes; `tests/core/support.py` adds a repository and stand-ins for `orca` and `gh`
that reproduce the behaviours Pod relies on.
