# Accept reports and settle worker tabs

Load this at the first worker-report acceptance, any retention or release decision,
and before closing the objective. These are coordinator actions through Orca;
Pod supplies commands and performs none of them.

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
  standing authorization. This never applies to active, coordinator, setup,
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

Retain only while required corrections or a delta review of that worker's result
are pending, or on a recorded owner request. An implementer's cycle follows
required findings from the review of its recorded integrated candidate, even
when the assurance moves to the corrected candidate for delta review.
The acknowledgement supplies
`<title> (retained: <reason>)` and its rename, with `corrections`, `delta review`
or `owner request`. Run the rename when retaining. Release when the correction
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
