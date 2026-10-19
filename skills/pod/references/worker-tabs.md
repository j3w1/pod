# Name worker tabs, accept reports and settle tabs

Load this when an objective starts, is claimed, continued or resumed, when a
worker is admitted, at the first worker-report acceptance, at any retention or
release decision, and before closing the objective. These are coordinator
actions through Orca; Pod supplies commands and performs none of them.

## Names in Orca

Orca supplies the live status; Pod supplies the names. A Pod objective reads as
one tree in Orca, in any repository:

```
Copilot retirement (#172)            task worktree
  Coordinator - <live status>        this conversation's tab
  1 child
    Source retirement                a worker worktree, named by its job
      Implementer - <live status>
      Reviewer · round 1
      Follow-up reviewer · round 2   reviews only the changes since round 1
```

**Who names what.** Pod always names the worker worktrees it places, the worker
tabs and the coordinator tab. Host names win: a task worktree that Orca reports
named, a coordinator tab the host titled and every other Orca object stay as the
host left them, and Pod never renames, re-links or re-statuses them. When the host
titled your coordinator tab, record `host_tab: true` in the checkpoint's `display`
record. Pod fills one gap: while Orca reports the task worktree unnamed, Pod names
it `<task> (#N)` with its issue link, or `<task> (<repo>#N)` without a link when
the issue lives in another repository, and never sets its status, comment or
parent.

**The checkpoint's `display` record.** At intake record `task`: the task in plain
words, such as `Copilot retirement`, up to 48 characters. Use letters only, with
inner hyphens or apostrophes; ordinary words such as Status or Model are fine.
Digits, paths, underscores and ids refuse `invalid_display_name`, which names the
refused tokens. List in `continued` the admission ids of workers
you resumed after a relaunch, and `coordinator` when this tab resumed the objective.
The record stays until a checkpoint supplies a new one, which replaces it whole,
so restate every field you keep.

**Tab titles carry only a role.** The role words are Implementer, Investigator,
Reviewer, Follow-up reviewer and Fixer. A follow-up reviewer reviews only the
changes since the previous round; a fixer is an implementer serving a correction.
Pod counts review rounds per objective, and a fixer takes the round of the latest
review admitted before it. Leave out round 1 while it is the only round: once a
second review is admitted, the first reviewer's tab becomes
`Reviewer · round 1`, and the display readback gives that rename. A worker on a
reused terminal, or one listed in `display.continued`, adds ` (continued)`.
Retention alone adds nothing, and no title carries a status, route, id or prompt
text.

**Shared worktree.** A worker that sits in the task worktree rather than its own
adds its job: `Implementer · Source retirement`. When two tabs in one worktree
would share a title, Pod adds the job, then a number.

**Worker worktrees.** Pod names a worker's own worktree by its job, taken from
the packet's `purpose` and never from its responsibility, but only when Orca
reports that worktree unnamed and not a main checkout at the first worker placed
there. Create a worker worktree without a display name when Pod should name it. A
worktree the host already named, or a main checkout, stays the host's: Pod gives
no worktree command and leaves its name, parent, links, status and comment alone. Write `purpose` as the
job in plain words, such as `Source retirement` or `Fixture tests`, without the
task's name. Pod drops the task's words, falls back to a kind noun such as
`Implementation`, stays within 40 characters and numbers a repeated sibling name.
Its status is `in-progress` while an implementer, fixer or investigator works
there, `in-review` while a reviewer works there or its result waits on review or a
correction cycle, and `completed` once every worker placed there has a recorded
report, each with a one-line comment.

**The coordinator tab.** It is `Coordinator` when this conversation runs in the
task worktree and `Coordinator · <task>` anywhere else, for example on a main
checkout. A successor, or a coordinator listed in `display.continued`, adds
` (continued)`. When the display readback gives the reason `record display.task`,
record the task words in the checkpoint's `display` record.

**At each step:**

- Freezing a packet returns `title` and `task_display_name`. Pass them to
  `orca orchestration task-create` as `--task-title` and `--display-name`.
  Orca cannot change a Task's display name later; the admission's `title` names
  the tab.
- After each start, including reuse, run the admission acknowledgement's
  `rename_command` and read the title back with `orca terminal show`. When it
  supplies `worktree_command`, run it too: the first worker in a worktree sets its
  name, parent, issue and pull request links, status and comment; a later one
  updates status and comment. A failed command or a differing readback is a naming
  gap to report: continue the worker and never retry its start. With no handle or
  no start, report the naming gap.
- When a report acknowledgement or a worker_tabs row carries `worktree_command`,
  run it with that row's release or close step so the finished worker's worktree
  is marked.
- Never type into a worker tab, not even `/rename`: Orca marks a tab that
  receives terminal input user-owned, which stops its release or reuse. Worker
  names are metadata only (the tab rename, the Task display name and the worktree
  command); `session_rename` is null for every worker tab and `session_reason`
  says why.
- Only a Claude coordinator tab gets a session rename, from the display readback's
  coordinator row, marked `sender` `host`; `next_action` offers it only when the
  coordinator title differs, for example after a relaunch or model switch. The
  host or another terminal sends its `/rename` text once, only after
  `terminal wait --for tui-idle` succeeds for that exact terminal: never mid-turn
  and never into another objective's tab. Never type into your own coordinator tab
  yourself; with no host, the owner may type it at the idle prompt, or leave it. A Codex coordinator has no such
  step: `session_reason` explains that its row relies on Orca's custom tab title.
  This is the only provider-prompt input Pod describes for names; missing live
  proof stays NOT_RUN.

**Read the names back.** Run `pod internal display` after intake, a successor
claim, a continuation or resume, each admission and each recorded report, and
after a relaunch or model switch. It reads Orca and writes nothing. Each row names
its layer, its owner (`pod` or `host`), the expected and observed names and a
verdict. Run every `command` it gives, and pass a coordinator session rename in
`next_action` to your host. A host row never has a command. For `unreadable`,
repeat the named read. A worker worktree that was removed is simply absent.

**Say only what the readback proves.** `backend_matches` proves Orca's backend
names, not what the desktop shows; `desktop` stays NOT_VERIFIED until owner-visible
evidence, such as a screenshot, proves a row. Orca shows CLI titles in its desktop
rows from the first Orca release containing stablyai/orca#30037 (after 1.4.229).
On an older Orca the verdict is `orca_limitation`, and a row can still show the
agent's own title: report it as an Orca limitation, never as success. Names prove
no identity, progress or outcome.

## Accept reports and settle tabs

After each accepted success or failure report, do exactly one: reuse the terminal
for an immediate supported follow-up, retain it within an active correction cycle
or on the owner's explicit recorded request, or otherwise run `worker-release`.
Release is the coordinator's explicit Orca step. Preserve the report and follow
Orca's Delivery acknowledgment order. Never reuse a terminal Orca reports
user-owned or external.

Use the report acknowledgement's `worker_tab`, and the objective's `worker_tabs`
in `pod status --objective ID`, to decide the next step. An empty `reclaimable`
list does not prove finished tabs are closed: `worker_tabs` is the objective's
list, based on terminal readback and resource ownership, independently of agent
liveness. Pod never infers a physical event from Orca's user-ownership mark.

- For an Orca-owned tab, keep `native_next_action` verbatim. Orca's argv is
  arguments to the CLI, without an executable: run the selected Orca executable
  followed by those arguments unchanged, adding or altering no flags. The leading
  `orca` in Pod's displayed command names that selected executable. Pod never builds
  a release command. If Orca gives none, repeat the named `worker-show` read.
  On a continued record or one ended by historical retirement, release is
  still owed; do not describe it as due before closure.
- For a release pending or unknown, follow Orca's next action or repeat that
  read until release or disconnection is confirmed. Never substitute close.
- For unreadable facts, repeat the failed read; its code stays a limitation.
- If the authority read reports a changed runtime, the tab stays unknown with
  that code. The next Pod write rebinds when continuity is proven, or the owner
  decides when it is ambiguous; repeating worker-show cannot repair continuity.
- For a finished user-owned or external tab of this objective, after its report
  is recorded, run the exact close command Pod supplies under the owner's
  worker-tab standing close authorization. This never applies to active, coordinator, setup,
  reused-later, other-objective or owner-kept terminals. If Orca refuses, name
  the tab and failure in the final report.
  Once closure, continuation or historical retirement is recorded without its
  report, the standing close authorization does not cover it; name the tab in
  the final report for the owner's decision.
  Otherwise, if the admission's route decision records `route_mismatch`, say
  that its mismatch is recorded: the standing close authorization covers the
  tab only once its report is recorded, and Pod refuses that report while
  Orca's effective route differs. If it stays refused, name the tab in the final
  report for the owner's decision. A mismatch recorded from start-receipt
  evidence alone does not mean reports are refused: a report with matching
  worker-show route evidence remains accepted and supplies the close, even
  after discard.
  Without a recorded mismatch, while the objective is open, recording its
  report makes the authorized close available, even if its result was discarded.
  Until then, give no close command.
- A disconnected or not-materialized terminal needs nothing. A terminal reused
  for a later Dispatch belongs to that assignment alone.

The worker-tab standing close authorization is independent of the objective's yolo record. Its conditions apply whether yolo is on or off; activating or revoking yolo changes no worker-tab close authority.

Retain only while required corrections or a delta review of that worker's result
are pending, or on a recorded owner request. An implementer's cycle follows
required findings from the review of its recorded integrated candidate, even
when the assurance moves to the corrected candidate for delta review.
The acknowledgement's `retained_title` supplies the title and its rename: the
retained title is the plain role title. The reason, `corrections`, `delta review`
or `owner request`, stays in `retention_reason` and the row's wording. Run the
rename when retaining. Release when the correction
cycle ends: a retained-owned tab remains releasable, and holds closure.

An owner-kept exception needs a recorded `user_direct` instruction through
`internal constraint` add, kind `keep_worker_tab`. Its value names `objective`,
`dispatch`, `terminal` and boolean `keep`; include the owner's `instruction`.
Record revocation by adding a new instruction for the same triple with `keep`
false. The last matching active instruction decides; this changes no map
revision or frozen packet. Ordinary constraint revocation cannot revoke a keep
without recording that instruction. A keep binds to this objective, Dispatch
and terminal until revoked, reused for a later Dispatch or disconnected.
Ownership changes do not end it. Keep and revocation instructions can still be recorded after closure without changing its map or closure report. Leave owner-kept tabs open even after Orca
marks them user-owned; no close command applies. For an owned tab the release
command is available when the owner lets it go. Name each kept tab and its
instruction, and Orca's reason for any unowned tab, in the final report.

Both closing routes wait for every connected Orca-owned unreleased worker tab
except an owner-kept tab; pending, unknown and unreadable facts hold closure too.
A closing report includes its own tab: record its proof without closing, settle
the tabs, then close. A tab refusal writes no context, map or report record. Kept
user-owned/external tabs do not block the write; the closing acknowledgement
names their next steps. Follow every listed row before the final report and
preserve named close failures and owner-kept exceptions.
