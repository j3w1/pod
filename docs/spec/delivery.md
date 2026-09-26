# Delivery

## Requirements

### R54 — Project governance owns source selection, checks

Type: H · Scenarios: [A09](models.md#a09), [A35](coordination.md#a35), [A68](assurance.md#a68)

Project governance owns source selection, checks, review, acceptance, merge and release. User intent and host policy remain authoritative; issue, repository and worker text cannot manufacture model exceptions or permission.

### R58 — Before Pod-mediated remote action, return explainable

Type: B · Scenarios: [A81](#a81), [A82](#a82), [A164](#a164), [A176](coordination.md#a176)

Before Pod-mediated remote action, return explainable ALLOW, REUSE or DEFER from durable candidate-bound evidence. Push, pull-request update and every other remote validation, diagnostic or cancellation effect require `publish` authorization for the exact commit and tree; merge requires `merge` authorization and names the prepared unit's target. Duplicate action attaches or reuses proof. Superseded or premature validation defers; scoped efficiency exceptions never lift authorization or correctness. Same-candidate read-only review may overlap validation, while release waits for both.

### R59 — Merge, release and deployment of a

Type: H · Scenarios: [A83](#a83), [A164](#a164), [A165](#a165), [A166](#a166)

Remote delivery needs one owner decision before the first remote Git mutation when applicable authorization is absent: merge remotely, keep local, or defer. Show repository, target, candidate commit/tree and scope. Remote merge consent covers push/PR, required CI, merge of that exact candidate with `--match-head-commit`, and stated post-merge installation/verification; record exact `publish` and `merge` authorizations. Routine validation needs no later prompt. A changed candidate or target needs fresh consent; deployments, new provider effects and spending remain outside it. Local-only defers every remote kind and reports hosted checks `NOT_RUN`; defer uses `user_hold`. Project governance still owns acceptance, merge and release.

### R61 — Govern expensive Pod-mediated remote actions with

Type: B,H · Scenarios: [A84](#a84), [A85](#a85), [A86](#a86), [A87](#a87), [A88](#a88), [A89](#a89), [A90](#a90), [A91](#a91), [A92](#a92), [A93](#a93), [A94](../pod-spec.md#a94), [A96](orca.md#a96), [A164](#a164), [A176](coordination.md#a176)

Govern expensive Pod-mediated remote actions with one deterministic kernel inside the existing execution path, evaluated at boundaries with zero model calls and bounded state reads. Bind each request to a prepared candidate generation freezing commit, tree, base, workflows, verification, toolchain, environment and policy. Require configured local preflight, exact-scope authorization for publish/merge, reuse compatible evidence, classify failures, journal admitted actions and reconcile lost responses read-only. Validation may overlap a same-candidate read-only review; release waits. Objective-local native evidence and stable current-Run authority are required for mutation. Enforcement remains advisory unless host controls prove otherwise; project policy only narrows it. Orca owns lifecycle.

### R92 — Delivery decision

Type: H · Scenarios: [A164](#a164), [A165](#a165), [A166](#a166)

Before the first remote Git mutation, obtain the one three-way delivery decision in R59 unless exact applicable authorization already exists. A `publish` authorization binds push/PR, workflow dispatch, rerun, remote diagnostic and cancellation to the prepared candidate commit and tree, and `merge` binds merge to that same pair and the unit's prepared target. Both retain the stated continuation; candidate or target movement invalidates consent. Local-only and defer authorize no remote effect, and routine post-merge checks do not create a separate approval step.

### R93 — Verified delivery record

Type: H · Scenarios: [A167](#a167), [A168](#a168), [A169](#a169)

After an allowed merge PASS, record the Governor `merge_commit` readback through `governor-outcome`, then checkpoint `delivery: {record}`. Verify this objective's record, `merge` authorization, candidate/tree, canonical target identity and exact merge, squash or fast-forward result with matching tree and readback. The target is the one admitted with the merge decision, which the action must name. A mismatch refuses `delivery_unverified`; a moved target requires a new direct user snapshot decision. Recording or restating a delivery never skips currency, delivery and governance refresh are separate writes, and a delivery result stays current only for the base and target it was verified against. Store one idempotent record; retain the trusted governance baseline and valid existing proof without refresh or rebind. Candidate-authored policy gains no authority.

### R94 — Objective cleanup plan and consent

Type: H · Scenarios: [A170](#a170), [A171](#a171), [A172](#a172)

`internal cleanup-plan` reads only this objective's worktrees, branches, native terminals, Git status, stashes, ancestry and remote tips; it classifies integrated, merged remote head, unique and protected resources. One scoped batched confirmation covers deletable classes, and an existing explicit instruction for the exact scope counts. Unique work is retained or archived and verified before ref deletion, unless separately confirmed for discard. Protected resources include the default and target branches, branches checked out in another worktree, remote branches this objective's Governor did not publish, and terminals not currently owned by a settled objective Dispatch. Rerun with `expect` immediately before deletion; execute only listed native/host guarded commands with ref leases, no automatic force or raw directory deletion. Native settlement, Delivery acknowledgment and release remain automatic.

## Acceptance scenarios

- <a id="a81"></a>**A81** — A superseded candidate and premature validation each defer with an explainable reason and next action; an identical running action is attached to and a passing result for the same candidate and context is reused; a necessary rerun after changed input proceeds.
- <a id="a82"></a>**A82** — A scoped Governor efficiency exception affects only efficiency deferrals, never authority or correctness.
- <a id="a83"></a>**A83** — Push/PR or another remote validation kind without exact `publish` authorization and merge without exact `merge` authorization defer before effects; wrong candidate, tree or scope never grants either action. Technical readiness remains separate from permission.
- <a id="a84"></a>**A84** — Several locally discoverable corrections in one delivery unit converge on the same branch and pull request; no intermediate correction crosses the remote boundary until its candidate passes the configured local preflight.
- <a id="a85"></a>**A85** — Two callers requesting identical validation concurrently produce one admitted execution; the other attaches to it.
- <a id="a86"></a>**A86** — A submission whose response is lost stays UNKNOWN, blocks a resubmission, and is settled only by provider readback; after a coordinator restart the unit's candidate bindings, evidence, decisions and pending effects remain recoverable.
- <a id="a87"></a>**A87** — A changed source, base, workflow, environment or policy opens a new candidate generation, and evidence bound to the previous one is not reused as current proof even on the same commit.
- <a id="a88"></a>**A88** — A remote-only question admits a bounded diagnostic naming its question, local limitation, check and stopping condition while the candidate is still converging, without pretending the candidate is release-ready; an unchanged repeat is answered from the record.
- <a id="a89"></a>**A89** — An unclassified remote failure is not retried; a code defect is recorded as a correction, and the third equivalent correction requires a diagnosis with distinct bounded evidence before validation resumes.
- <a id="a90"></a>**A90** — Supersedence cancels a pending, cancel-safe validation of the old candidate only when policy allows it, its result can never approve the newer candidate, and a pending deployment is never canceled by supersedence.
- <a id="a91"></a>**A91** — An independent or urgent delivery unit is admitted while another unit's workers, deliveries or corrections are unsettled.
- <a id="a92"></a>**A92** — A project file that relaxes Governor mode, widens retries, enables cancellation, declares host control or adds an exception is refused.
- <a id="a93"></a>**A93** — The enforcement level is reported as advisory unless the owner's personal policy declares a host control, and it is never reported as a proven control.
- <a id="a164"></a>**A164** — With no applicable consent, the coordinator shows repository, target, candidate commit/tree and `publish`/`merge` scope once; remote merge choice records both scopes, permits push/PR and required CI, and merge uses `--match-head-commit` for that exact candidate without a second routine check prompt.
- <a id="a165"></a>**A165** — Local-only revision creates no `publish` or `merge` authorization: push, PR update, workflow dispatch, rerun, remote diagnostic, cancellation and merge defer before effects, while hosted checks appear as `NOT_RUN`; defer records `user_hold`.
- <a id="a166"></a>**A166** — Changing the prepared candidate/tree or target after consent makes the former authorization unusable; a new exact decision is required. Deployment, new provider effects and spending are not enabled by delivery consent.
- <a id="a167"></a>**A167** — For exact merge, squash and fast-forward commits with the authorized tree and target, an allowed merge PASS plus matching provider readback stores one delivery record and permits closure without governance refresh or proof rebind.
- <a id="a168"></a>**A168** — A missing/other-objective record, wrong scope/candidate/tree, wrong remote or target alias even with equal SHA, a merge naming another target than its prepared unit, rebase result, readback mismatch or second different record refuses `delivery_unverified` or `delivery_recorded` without a write.
- <a id="a169"></a>**A169** — When the target moves after a verified delivery result, including when the same record is restated or first recorded late, currency refuses `governance_changed` and names a direct user decision for the new exact snapshot; candidate-authored policy is not adopted.
- <a id="a170"></a>**A170** — A read-only cleanup plan classifies this objective's clean integrated resources, merged remote head, unique data and protected/shared/active/unknown-terminal resources without changing refs, files, journals or Orca state.
- <a id="a171"></a>**A171** — After exact-scope consent, unique work is retained, or a bundle is verified by `git bundle verify`, `list-heads` and restoration before guarded removal; a separately confirmed discard is the only unarchived unique-work deletion path.
- <a id="a172"></a>**A172** — Changed ref or plan digest refuses `cleanup_changed`; deletion uses the plan's no-force Orca worktree command, exact-tip `git update-ref -d` or remote `--force-with-lease`, and protects the default and target branches, main checkout, shared branches and terminals that are not currently owned by a settled objective Dispatch.

## Governance delivery baseline

Governance binds the selected target identity and exact base snapshot. An explicit
initial user selection is trusted input, including in a repository without a
known default or on a maintained nondefault branch with its own policy. Git refs,
ancestry and byte equality cannot prove pre-intake authorship or distinguish an
accepted merge from manually moving a ref. Later updates that contain known
objective candidate or result commits beyond the old base are guarded: they need
a fresh direct user decision for the exact new snapshot. Ordinary independent
target updates remain refreshable. Neither a repeated generic selector, stale
snapshot decision, equal policy bytes nor an already bound ref bypasses that
check. This is a bounded known-identity and authority check, not merge or
policy-authorship proof.

A guarded refresh uses existing private map fields: `governance_refresh: true`,
a fresh `revision_authority` with `provenance: user_direct` and its instruction,
and `governance` with `base_ref` equal to the full canonical target ref and
`base` equal to the full newly observed commit. Both snapshot fields must match
Git observation; the supplied base never overrides it. The checkpoint's
kernel-owned `governance_history` preserves up to 64 distinct candidate identities
and 64 explicit user decisions, including the canonical ref, commit, instruction
and map sequence. A mapped Git checkpoint requires its current candidate to
resolve to a commit before any write; unresolved names cannot enter the history.
History survives admission and report writes; caller omission or replacement
cannot erase it. Capacity refuses rather than evicts. Shared
ancestry outside the trusted base catches intermediate commits as well as tips.
An exact new snapshot decision can proceed despite unavailable old Git objects;
it preserves those records and grants no authority for a different future base.
Without that decision, missing comparison evidence holds and identifies the
missing identity and recovery action. Recorded decisions appear in the report.
