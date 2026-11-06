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
host left them, and Pod never renames, re-links or re-statuses them. When the
host titled your coordinator tab, record `host_tab: true` in the checkpoint's
`display` record. Pod fills one gap: while Orca reports the task worktree
unnamed and it is not a main checkout, Pod names it `<task> (#N)` with its issue
link, or `<task> (<repo>#N)` without a link when the issue lives in another
repository, and never sets its status, comment or parent.

**The checkpoint's `display` record.** At intake record `task`: the task in plain
words, such as `Copilot retirement`, up to 48 characters. Use ASCII letters only,
with inner hyphens or apostrophes; ordinary words such as Status or Model are
fine. Digits, paths, underscores, ids and a word part made only of seven or more
of the letters a to f (it reads as a hash) refuse `invalid_display_name`, which
names the refused tokens. List in `continued` the admission ids of workers
you resumed after a relaunch, and `coordinator` when this tab resumed the objective.
The record stays until a checkpoint supplies a new one, which replaces it whole,
so restate every field you keep.

**Tab titles carry only a role.** The role words are Implementer, Investigator,
Reviewer, Follow-up reviewer and Fixer. A follow-up reviewer reviews only the
changes since the previous round; a fixer is an implementer serving a correction.
Pod counts review rounds per objective, and a fixer takes the round of the latest
review admitted before it. Leave out round 1 while it is the only round: once a
second review is admitted, the first reviewer's tab becomes
`Reviewer · round 1`, and the display readback shows that title. A worker on a
reused terminal, or one listed in `display.continued`, adds ` (continued)`.
Retention alone adds nothing, and no worker title carries a status, route, id or prompt
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

For a Pod-owned coordinator title, a known current wait for the person adds
` · needs you`; clearing the wait removes it. A setup offer, an agent wait or
unknown old question facts add no mark. This is the only status text Pod adds
to a tab title. Preserve host-owned names and expose their limitation through
the display link. A projected mark is not proof of a native title change: apply
only supported metadata actions and read back the actual result. Native metadata
and desktop rendering remain separate evidence.

**At each step:**

- Freezing a packet returns `title` and `task_display_name`. Pass them to
  `orca orchestration task-create` as `--task-title` and `--display-name`.
  The Task display name labels the worker's row. Orca cannot change it later, so
  a later title, such as a new round, is data only; the admission's `title` and
  `title_note` say so.
- After each start, including reuse, run the admission acknowledgement's
  `worktree_command` when it supplies one: the first worker in a worktree sets its
  name, parent, issue and pull request links, status and comment; a later one
  updates status and comment. A failed command is a naming gap to report:
  continue the worker and never retry its start.
- When a report acknowledgement or a worker_tabs row carries `worktree_command`,
  run it with that row's release or close step so the finished worker's worktree
  is marked.
- Never rename a worker tab or type into it, not even `/rename`: Orca marks a
  tab that is renamed or receives terminal input user-owned, which stops its
  release or reuse. Worker names are metadata only (the Task display name and the
  worktree command); `session_rename` is null for every worker tab and
  `session_reason` says why.
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
`next_action` to your host. A host row never has a command, and neither does a
worker tab row: its title is data only and never drift. For an `unreadable`
row that names a read, repeat that read. A worker worktree that was removed is simply absent.

**Say only what the readback proves.** `backend_matches` proves Orca's backend
names, not what the desktop shows; `desktop` stays NOT_VERIFIED until owner-visible
evidence, such as a screenshot, proves a row. Orca shows CLI titles in its desktop
rows from the first Orca release containing stablyai/orca#30037 (after 1.4.229).
On an older Orca the verdict is `orca_limitation`, and a row can still show the
agent's own title: report it as an Orca limitation, never as success. Names prove
no identity, progress or outcome.

## Accept reports and settle tabs

The coordinator's own tab stays until its closure is recorded. Once delivery proof and final-report consumption are complete, record closure in that same turn before ending, being released or having that tab closed. Settle worker tabs through the steps below before the ordinary closing write. If the coordinator is already gone, use the router's recovery guidance for evidence closure. Missing completion proof goes to the project lead; completed bookkeeping needs no owner decision.

After each accepted success or failure report, do exactly one: reuse the terminal
for an immediate supported follow-up, retain it within an active correction cycle
or on the owner's explicit recorded request, or otherwise run `worker-release`.
Release is the coordinator's explicit Orca step. Preserve the report and follow
Orca's Delivery acknowledgment order. Never reuse a terminal Orca reports
user-owned or external.

On Orchestration V3 (see the wake guidance for how a result names the runtime)
Orca releases a finished worker about two minutes after its `worker_done`
unless it is kept open, so decide retention and reuse before that:

- When you already know at admission that you will retain or reuse the worker,
  add `"keep_open": true` to the admission input. Pod passes `--keep-open` to
  `worker-start` and records `keep_open: requested` on the admission; on a
  runtime that does not advertise keep-open it records `not_advertised` and
  starts exactly as before.
- At report time a row whose retention is intended (a correction cycle or an
  owner keep) and whose terminal Orca will release at `autoReleaseAt` carries
  `keep_open_command`, `orca orchestration worker-keep-open --dispatch D --json --full`;
  run it at once. A worker that is not retained is left to Orca's release.
- `reuse_of` naming a worker Orca has already released is refused with
  `reuse_released` before any start: admit the follow-up without `reuse_of`.
- Never send to a settled Dispatch to give it more work: on V3 that starts a
  follow-up Task Pod never admitted. Every follow-up is a new admission.

Use the report acknowledgement's `worker_tab`, and the objective's `worker_tabs`
in `pod status --objective ID`, to decide the next step. An empty `reclaimable`
list does not prove finished tabs are closed: `worker_tabs` is the objective's
list, based on terminal readback and resource ownership, independently of agent
liveness. Pod never infers a physical event from Orca's user-ownership mark.

- For an Orca-owned tab, keep `native_next_action` verbatim. Orca's argv is
  arguments to the CLI, without an executable: run the selected Orca executable
  followed by those arguments unchanged, adding or altering no flags. The leading
  `orca` in Pod's displayed command names that selected executable. Pod never builds
  Orca's native action. A separately labelled `next_action.source: pod_guidance`
  with kind `worker_release` and `command_source: pod_guidance` gives the exact
  release command only for a consumed, settled worker that Pod asked to keep open,
  now owned and retained with no recorded reuse or retention remaining. Native
  retention reasons other than absent, `coordinator_request` or `user_requested`
  exclude it. Choose release yourself; Pod runs nothing. `user_requested` cannot
  distinguish Pod's request from a later person's request: if a person asked to
  keep it, do not release it. Every other empty-argv case repeats the named
  `worker-show` read.
  On a continued record or one ended by historical retirement, release is
  still owed; do not describe it as due before closure.
- For a release pending or unknown, follow Orca's next action or repeat that
  read until release or disconnection is confirmed. Never substitute close.
- For unreadable facts, repeat the failed read; its code stays a limitation.
- If the authority read reports a changed runtime, the tab stays unknown with
  that code. The next Pod write rebinds when continuity is proven, or the owner
  decides when it is ambiguous; repeating worker-show cannot repair continuity.
  On a closed objective nothing rebinds and nothing needs reopening: the row's
  `tab_intent` names the keep or let-go write for that tab.
- For a finished user-owned or external tab of this objective, after its report
  is recorded, run the exact close command Pod supplies under the owner's
  worker-tab standing close authorization. This never applies to active, coordinator, setup,
  reused-later, other-objective or owner-kept terminals. If Orca refuses, name
  the tab and failure in the final report.
- For an unreported user-owned or external tab with an active route hold,
  name the held admission and the project lead's settlement step from the
  route-settlement guidance reached through the router. A start-receipt
  mismatch alone does not mean a report will refuse: matching worker-show
  evidence can still permit consumption. Settlement alone supplies no standing
  close command for an unreported tab. On a closed, continued, evidence-closed
  record or one ended by historical retirement, give the project lead the remaining tab
  follow-up in the successor or repository task; this ended record accepts no
  settlement write and supplies no close command.
- For a terminally discarded, unreported review or investigation tab, the
  corrections remain in this objective's map while it is open. After the record
  ends, the project lead preserves its correction follow-up in the successor or
  repository task. This tab has no standing close command.
- For other unreported user-owned or external tabs, while the objective is open,
  recording the report makes the authorized close available, even if an
  implementation result was discarded. Until then, give no close command.
  After closure, continuation or historical retirement, the standing close
  authorization does not cover an unreported tab; name it in the final report
  for the owner's decision.
- A disconnected or not-materialized terminal needs nothing. A terminal reused
  for a later Dispatch belongs to that assignment alone.

The worker-tab standing close authorization is independent of the objective's yolo record. Its conditions apply whether yolo is on or off; activating or revoking yolo changes no worker-tab close authority.

Retain only while required corrections or a delta review of that worker's result
are pending, or on a recorded owner request; after ordinary closure an agent's
keep also retains, with reason `agent request`. An implementer's cycle follows
required findings from the review of its recorded integrated candidate, even
when the assurance moves to the corrected candidate for delta review.
The acknowledgement's `retained_title` supplies the title as data, with no
rename: the retained title is the plain role title. The reason, `corrections`,
`delta review`, `owner request` or (closed objectives only) `agent request`, stays
in `retention_reason` and the row's wording. Release when the correction
cycle ends: a retained-owned tab remains releasable, and holds closure.

An owner-kept exception needs a recorded `user_direct` instruction through
`internal constraint` add, kind `keep_worker_tab`. Its value names `objective`,
`dispatch`, `terminal` and boolean `keep`; include the owner's `instruction`.
Record revocation by adding a new instruction for the same triple with `keep`
false. The last matching active instruction decides; this changes no map
revision or frozen packet. Ordinary constraint revocation cannot revoke a keep
without recording that instruction. A keep binds to this objective, Dispatch
and terminal until revoked, reused for a later Dispatch or disconnected.
Ownership changes do not end it. Keep and revocation instructions can still be recorded after ordinary coordinator closure without changing its map or closure report, even after Orca's runtime changed: that write never rebinds. On a closed objective it is accepted from the recorded coordinator, the person's direct decision, or an honest agent decision from any caller; an agent keep shows `kept_by: agent` and an agent may revoke it, but the person's keep stands until the person revokes it. An agent's tab intent on an open objective is refused. An evidence-closed objective accepts no further Pod write. Leave owner-kept tabs open even after Orca
marks them user-owned; no close command applies. For an owned tab the release
command is available when the owner lets it go. Name each kept tab and its
instruction, and Orca's reason for any unowned tab, in the final report.

Both coordinator closing routes wait for every connected Orca-owned unreleased worker tab
except an owner-kept tab; pending, unknown and unreadable facts hold closure too.
A closing report includes its own tab: record its proof without closing, settle
the tabs, then close. A tab refusal writes no context, map or report record. Kept
user-owned/external tabs do not block the write; the closing acknowledgement
names their next steps. Follow every listed row before the final report and
preserve named close failures and owner-kept exceptions.
