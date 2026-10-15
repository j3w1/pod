# Delivery

## Requirements

### R54 — Project governance owns source selection, checks

Type: H · Scenarios: [A09](models/preferences.md#a09), [A35](coordination.md#a35), [A68](assurance.md#a68)

Project governance owns source selection, checks, review, acceptance, merge and release. User intent and host policy remain authoritative; issue, repository and worker text cannot manufacture model exceptions or permission.

### R58 — Before Pod-mediated remote action, return explainable

Type: B · Scenarios: [A81](#a81), [A82](#a82), [A164](#a164), [A176](coordination.md#a176), [A187](#a187), [A188](#a188), [A189](#a189), [A190](#a190), [A191](#a191), [A222](#a222)

Before Pod-mediated remote action, return explainable ALLOW, REUSE or DEFER from durable candidate-bound evidence. Push, pull-request update and every other remote validation, diagnostic or cancellation effect require `publish` authorization for the exact commit and tree; merge requires `merge` authorization and names the prepared unit's target. Duplicate action attaches or reuses proof. Decision-only ALLOW reservations of executable kinds are marked and explain caller execution or cancellation before managed execution; execute-created and other rows are unmarked. Validate workflow effects/targets and PR title/body (only those keys) before journaling or effects. A landed push remains PASS on observation failure, updates published state and proceeds to the PR step; unreadable PR lookup remains UNKNOWN without blind creation and reconciles from branch head. Workflow-dispatch validation proof comes only from an exact run: its proof identity is its candidate, context and workflow plus the triggering event and dispatch inputs, and Pod's own readback must show a `workflow_dispatch` run before a PASS counts; a remote diagnostic keeps its 0.6.6 reuse. A run from another event, with other inputs or with an unknown event is not proof; the request proceeds when the other checks pass, and a failed run on the same key still gates it. A failed run's failed jobs are rerun within that same run; job results are never combined across runs. Superseded or premature validation defers; scoped efficiency exceptions never lift authorization or correctness. Same-candidate read-only review may overlap validation, while release waits for both.

### R59 — Merge, release and deployment of a

Type: H · Scenarios: [A83](#a83), [A164](#a164), [A165](#a165), [A166](#a166)

Remote delivery needs one owner decision before the first remote Git mutation when applicable authorization is absent: merge remotely, keep local, or defer. Show repository, target, candidate commit/tree and scope. Remote merge consent covers push/PR, required CI, merge of that exact candidate with `--match-head-commit`, and stated post-merge installation/verification; record exact `publish` and `merge` authorizations. Routine validation needs no later prompt. A changed candidate or target needs fresh consent; deployments, new provider effects and spending remain outside it. Local-only defers every remote kind and reports hosted checks `NOT_RUN`; defer uses `user_hold`. Project governance still owns acceptance, merge and release.

### R61 — Govern expensive Pod-mediated remote actions with

Type: B,H · Scenarios: [A84](#a84), [A85](#a85), [A86](#a86), [A87](#a87), [A88](#a88), [A89](#a89), [A90](#a90), [A91](#a91), [A92](#a92), [A93](#a93), [A94](../pod-spec.md#a94), [A96](orca.md#a96), [A164](#a164), [A176](coordination.md#a176), [A223](#a223)

Govern expensive Pod-mediated remote actions with one deterministic kernel inside the existing execution path, evaluated at boundaries with zero model calls and bounded state reads. Bind each request to a prepared candidate generation freezing commit, tree, base, workflows, verification, toolchain, environment and policy. Require configured local preflight, exact-scope authorization for publish/merge, reuse only exact-run evidence or same-run failed-job reruns (R58), classify failures, journal admitted actions and reconcile lost responses read-only. Validation may overlap a same-candidate read-only review; release waits. Objective-local native evidence and stable current-Run authority are required for mutation. Enforcement remains advisory unless host controls prove otherwise; project policy only narrows it. Orca owns lifecycle.

### R92 — Delivery decision

Type: H · Scenarios: [A164](#a164), [A165](#a165), [A166](#a166), [A186](#a186)

Before the first remote Git mutation, obtain the one three-way delivery decision in R59 unless exact applicable authorization already exists. A `publish` authorization binds push/PR, workflow dispatch, rerun, remote diagnostic and cancellation to the prepared candidate commit and tree, and `merge` binds merge to that same pair and the unit's prepared target. Both retain the stated continuation; candidate or target movement invalidates consent. Release-purpose admission preserves non-withdrawn project_policy audits bound to its delivery unit and unbound audits as R129 specifies, using existing candidate proof validity; missing/invalid proof defers with correctness `assurance_unbound`. Scoped waivers/trusted source removal remain distinct from passing review, while publication/validation can overlap required review and optional coordinator assurance gains no new gate. Status and authority-wait readers use the same applicable-target truth: an unbound/missing unit has no applicable merge consent even with an older target-less row. Actual merge refusal remains unchanged. Local-only and defer authorize no remote effect, and routine post-merge checks do not create a separate approval step.

### R93 — Verified delivery record

Type: H · Scenarios: [A167](#a167), [A168](#a168), [A169](#a169)

After an allowed merge lands, fetch its target and record the merge PASS with its provider `merge_commit` through `governor-outcome`, which applies these checks then (R116), then checkpoint `delivery: {record}`. Verify this objective's record, `merge` authorization, candidate/tree, canonical target identity and exact merge, squash or fast-forward result with matching tree and readback. The target is the one admitted with the merge decision; the action and its `merge` authorization name it as `<remote>/<base>`. A mismatch refuses `delivery_unverified`; a moved target requires a new direct user snapshot decision. Recording or restating a delivery never skips currency, and a delivery result stays current only for the base and target it was verified against. Store one idempotent record; retain the trusted governance baseline and valid existing proof without refresh or rebind. Candidate-authored policy gains no authority.

### R94 — Objective cleanup plan and consent

Type: H · Scenarios: [A170](#a170), [A171](#a171), [A172](#a172)

`internal cleanup-plan` reads only this objective's worktrees, branches, native terminals, Git status, stashes, ancestry and remote tips; it classifies integrated, merged remote head, unique and protected resources. One scoped batched confirmation covers deletable classes, and an existing explicit instruction for the exact scope counts. Unique work is retained or archived and verified before ref deletion, unless separately confirmed for discard. Protected resources include the default and target branches, branches checked out in another worktree, remote branches this objective's Governor did not publish, and terminals not currently owned by a settled objective Dispatch. Rerun with `expect` immediately before deletion; execute only listed native/host guarded commands with ref leases, no automatic force or raw directory deletion. Native settlement and Delivery acknowledgment follow Orca; release is the coordinator’s explicit Orca step under R124, never automatic.

### R115 — Exact provider run binding

Type: B,H · Scenarios: [A285](#a285), [A286](#a286), [A287](#a287), [A288](#a288), [A289](#a289), [A290](#a290), [A291](#a291)

A Governor workflow row binds only a run its own effect could have started: one of its events, its exact head commit, created at or after its submission, and its own attempt. A publication-derived validation row, internally kind `workflow_dispatch` with `derived_from`, binds only a `push`, `pull_request` or `pull_request_target` run: a branch publication starts push runs and, on a branch with an open pull request, pull-request runs, and Pod cannot tell whether one was open. A run of another event, such as `workflow_dispatch`, `schedule` or `merge_group`, is never proof for that row. Requested dispatches and remote diagnostics bind only `workflow_dispatch` runs. These rows bind the run's first attempt. A validation rerun binds only the attempt provider facts prove its own. Before submitting, Pod durably records with the row the run, the latest attempt read just before and the submission start, and keeps them through every interruption and error; a rerun whose run cannot be read reruns nothing. Right after GitHub accepts the call, the row records that acceptance. One rule attributes a rerun's attempt on every path that reads one: execution's immediate read, readback, a reservation's outcome readback, a merge PASS's readback of its unit's effects and a carried row's readback. Attribution is proved by the count: Pod's accepted call creates an attempt, so when the row records that provenance (the submission start at full precision) and the acceptance, and the run shows exactly one attempt after the pre-read, that attempt is Pod's and no other actor's can be it. The attempt's start (GitHub's per-attempt `startedAt`) is only a contradiction guard, at the provider's precision: GitHub sets a rerun's start at the request in whole seconds, so the attempt binds when its start is not in a whole second earlier than the second containing the recorded submission start. Pod floors its own timestamp to that second only to match the provider's precision, never further. Every other state holds the row pending or UNKNOWN with a reason naming it: no recorded provenance, including an attempt an earlier Pod bound to a pending row without it, a caller-performed rerun reservation and any row recorded before this rule; no recorded acceptance (an interruption inside the call, or a lost reply), since a single new attempt may then be another actor's while Pod's own never landed; two or more new attempts; a start in a whole second earlier than the submission's, which contradicts the record whether another actor started the attempt or this host's clock and GitHub's disagree, or no reported start; and a recorded acceptance while the run still shows no new attempt, a contradiction that records no outcome. Only an unconfirmed rerun, with the exact run view showing no attempt after the pre-read once the call's time limit (120 s) and the 600 s absence margin have passed, reads FAILED with that reason, never no-effect. Pod's own earlier rows are never taken as the run's complete history. A held rerun is never resubmitted. Limitation: GitHub reports no requester per attempt and only whole-second starts, so a held rerun, including Pod's own attempt that appears in an earlier second through host clock skew, stays unsettled and blocks closure until the owner acts, for example by historical retirement of delivered work. Pod records the proved attempt with its event and reads that attempt by number afterwards, under the same rule; a later attempt, whoever started it, never settles the row. A run already bound to an unsettled row, whoever bound it, is verified exactly as a newly selected one: its event, head commit, creation at or after the submission and first attempt, and its exact workflow by appearing in the list GitHub filters by workflow file and commit, since a run view names only a display name. One that fails is kept as `rejected_run` (naming `head_sha` for a commit mismatch, or a `reason`) and never binds; one that cannot be verified holds; settled rows read as recorded. `gh run list --workflow --commit` filters on GitHub; each lookup states its scope, its limit of 20 runs, whether the list was truncated (the port asks for one run more) and that GitHub lists only the runs it retains. Missing or partial history leaves the row pending, or UNKNOWN, with that reason kept as its `readback` note: never FAILED and never proof of no effect. A truncated list settles and binds nothing, on every path, right after the list is read: readback, a bound run's verification even when that run is visible in the list, a landed publication's binding of its runs, execution's run lookup, a reservation's outcome readback and a merge PASS's readback of its unit's effects. An earlier Pod (0.12.0) loads these rows unchanged and ignores `readback`, `attempt` and `head_sha`; if it reconciles such a row it applies its own looser matching.

### R116 — Each outcome needs its own evidence

Type: H · Scenarios: [A292](#a292), [A293](#a293), [A294](#a294), [A295](#a295), [A296](#a296), [A297](#a297)

`governor-outcome` records only an outcome its evidence supports; otherwise it refuses `outcome_evidence_required` with a subcode and writes nothing. A row Pod performed (an executable kind without the decision-reservation marker, a publication-derived validation, or an unmarked older row) takes PASS, FAILED or CANCELED only from execution or `governor-reconcile`; by hand it records only UNKNOWN, without a provider receipt. A rerun Pod performs records its provenance before its effect and its acceptance right after (R115), so an interrupted rerun resumes by readback and is never resubmitted. Its own attempt is proved by the count, the single attempt after the pre-read once the acceptance is recorded, and its whole-second start is checked only for a contradiction at GitHub's precision; when R115's one attribution rule cannot prove its own attempt, including for a pending row an earlier Pod bound without that provenance, the row stays pending or UNKNOWN and blocks closure until the owner acts. A merge PASS keeps the caller's provider `merge_commit` and passes R93's Git checks at outcome time; the merge decision and each refusal say to fetch the target first. A merge FAILED or CANCELED needs the fetched target to lack the candidate's result: neither the candidate commit nor, within a bounded walk from the bound base, a commit with its tree. A release or deploy, performed by the project, takes PASS or FAILED only with a provider receipt or an evidence reference. A decision reservation's PASS or FAILED is what Pod's own provider readback shows; the caller adds evidence references only. CANCELED needs a `detail` attesting that the action never ran. For a reservation it also needs the row pending with no recorded sign of execution (a provider receipt, a bound or rejected run, a validation it started) and a readback from complete history that shows no effect; one predicate decides this and whether a helper offers "cancel, then execute". UNKNOWN never becomes CANCELED. An identical restatement answers from the record without a write, and another receipt for a recorded outcome refuses. Existing rows read exactly as recorded.

### R129 — Scope the required merge audit to its delivery unit

Type: B,H · Scenarios: [A371](#a371), [A372](#a372), [A373](#a373), [A374](#a374), [A375](#a375)

An assurance obligation may name an optional `unit` using the Governor's existing delivery-unit identifier form. Only assurances accept it; no unit need exist when the obligation is recorded. Its binding is immutable, including omission on an existing row, like kind, provenance and parent. Candidate remains the existing retargetable proof target and never supplies a unit binding. These are optional additions in the current record format, with no conversion.

For Governor purpose release, the required-audit gate selects non-withdrawn, not-gone project_policy assurances whose unit is absent or equals the action's unit. Only their gaps defer assurance_unbound, using the existing candidate/proof validator. A later unit's open, unadmitted audit therefore cannot hold an earlier unit's merge; its own merge still needs its audit. Every unbound assurance, including all pre-rule records, gates every unit. Single-unit decisions, deferral codes and journal rows stay identical to 0.15.0. Publication/validation and optional assurance retain their earlier rules.

Only the assurance gate narrows. The separate integration_unsettled release gate still waits for every outstanding objective assignment, including another unit's running audit. The objective-wide independently reviewed label (R88) and final acceptance still require every non-withdrawn assurance.

## Acceptance scenarios

- <a id="a81"></a>**A81** — A superseded candidate and premature validation each defer with an explainable reason and next action; an identical running action is attached to and a passing result with the same exact proof identity (candidate and context, workflow, triggering event and dispatch inputs) is reused; a necessary rerun after changed input proceeds.
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
- <a id="a170"></a>**A170** — Worker release remains an explicit coordinator Orca step, separate from read-only cleanup planning. A read-only cleanup plan classifies this objective's clean integrated resources, merged remote head, unique data and protected/shared/active/unknown-terminal resources without changing refs, files, journals or Orca state.
- <a id="a171"></a>**A171** — After exact-scope consent, unique work is retained, or a bundle is verified by `git bundle verify`, `list-heads` and restoration before guarded removal; a separately confirmed discard is the only unarchived unique-work deletion path.
- <a id="a172"></a>**A172** — Changed ref or plan digest refuses `cleanup_changed`; deletion uses the plan's no-force Orca worktree command, exact-tip `git update-ref -d` or remote `--force-with-lease`, and protects the default and target branches, main checkout, shared branches and terminals that are not currently owned by a settled objective Dispatch.

- <a id="a186"></a>**A186** — Target-less journals written by exact released 0.6.4 source in isolated fixtures cannot show applicable merge consent or resolve a genuine checkpoint authority wait for an unbound or missing unit. Bound matching consent still works; missing/stale/wrong-target/target-less consent does not. Synthetic controls are distinguished from released-writer fixtures, neither proves live owner-state exposure, and actual unbound merges remain refused without migration/backfill.
- <a id="a187"></a>**A187** — An identical repeated workflow dispatch with the same candidate, workflow, event and inputs attaches while pending, and after PASS is reused as evidence once Pod's own readback showed a `workflow_dispatch` run. A PASS recorded without that readback is not proof, and a dispatch with other inputs is allowed.
- <a id="a188"></a>**A188** — A failed run's failed jobs are rerun within that same run with `failed_only`, under unchanged rerun selection, keying and gating. Where another run's passing jobs would complete the proof, the Governor still never answers REUSE, and the GitHub port reads no job-level results.
- <a id="a189"></a>**A189** — A run from another event, with other inputs or with an unknown event is never REUSE for a dispatch; the request is allowed when the other checks pass, and a failed run of another event on the same key still gates it. A selective `pull_request` publication run, pending or passed, does not satisfy an explicit full `workflow_dispatch` on the same candidate. An unsettled publication still defers the request without claiming that its run is the requested validation.
- <a id="a190"></a>**A190** — Every workflow row records the event Pod read back for its run. A requested dispatch binds only a `workflow_dispatch` run: after a push, the dispatch and a later pull-request opening on the same commit, it binds the dispatch run and never the later `pull_request` run. A publication-derived row takes its run's event and never matches a dispatch.
- <a id="a191"></a>**A191** — A 0.6.6 Governor journal loads without conversion and keeps its stored keys. Its publication-derived rows never satisfy a dispatch, and its settled dispatch rows never count and get no provider read. A pending dispatch row still attaches an identical request and counts only once readback shows a `workflow_dispatch` run; a bound run of another event is neither counted nor allowed to hold later requests. Remote diagnostic reuse is unchanged.

- <a id="a222"></a>**A222** — Governor decisions reserve executable actions with a positive marker and caller instructions; identical execution attaches, cancellation permits one execution, and unmarked older pending rows keep their attach behavior and, like rows Pod performed, settle only by readback (R116). Genuine 0.7.1 reads the reservation marker in a synthetic pre-channel control unchanged, while actual channel-bearing journals refuse under R120; neither read writes.

- <a id="a223"></a>**A223** — Declared and mapped workflow paths, dispatch/rerun/diagnostic targets, PR bounds and unknown PR keys refuse before a journal row or effect. A landed push remains PASS on run-read failure; an unreadable PR lookup is UNKNOWN without blind creation and reconciles from branch head. Unknown effects remain a correctness DEFER with syntax and trigger-proposal guidance.

- <a id="a285"></a>**A285** — An actual dispatch refusal leaves the requested dispatch UNKNOWN. Readback that lists no `workflow_dispatch` run keeps it UNKNOWN and states the read's scope, limit and retention; FAILED and CANCELED refuse for it; the publication-derived row on the same workflow and commit settles from its own push run.
- <a id="a286"></a>**A286** — A pull-request update's derived row, internally kind `workflow_dispatch`, binds the `pull_request` run on its exact commit created after the publication, never a newer `workflow_dispatch`, `schedule` or `merge_group` run nor one created before it, and records the run's event and first attempt.
- <a id="a287"></a>**A287** — An unexecuted decision reservation is held pending by readback, is never FAILED or proof, is canceled with an attestation while readback shows no effect, and execution then performs it once.
- <a id="a288"></a>**A288** — With no listed run, or a list truncated at its limit, whether or not a visible run matches and whether it failed or passed, the derived row stays pending; the result states scope, limit, truncation and retention, and the row keeps the reason. A row already bound to a run that stays visible in the truncated list holds too, failing or passing. A publication landing on a truncated list binds no run; a reservation's PASS refuses while its readback, of a visible bound run included, sees only a truncated list; and a merge PASS's readback sweep leaves such a row pending.
- <a id="a289"></a>**A289** — A completed matching run settles its row with that run's event and first attempt.
- <a id="a290"></a>**A290** — A later attempt never settles the first. A rerun records its run, pre-read attempt and submission start before submitting and binds only the single new attempt that started after it, so another actor's later passing attempt never settles it; a rerun whose run cannot be read reruns nothing. Interrupted after its accepted submission and before its result write, it resumes from that record and binds its own attempt, even when another actor's attempt preceded the pre-read, and holds when another actor's attempt followed it. Interrupted inside its call, or with its reply lost, it never binds the new attempt it cannot prove its own; with no attempt after the pre-read past the call's time limit and margin it reads FAILED as not taken effect. An actor between the pre-read and the submission, or a single new attempt that started before the submission, holds the row. One table runs every attribution state through readback and execution: the accepted single attempt binds whether it starts the next second or within the submission's own second, including a whole-second start within a fractional submission's second (01 against 01.8), and a bound attempt is read by number past a later actor's and within that second; a pending row an earlier 0.13.0 candidate bound without provenance, a rerun recorded with only its run, a caller-performed reservation, an unaccepted attempt, several new attempts, a start in a whole second earlier than the submission's (including 00 against 01.8), no reported start, an acceptance contradicted by no new attempt, and a bound attempt without acceptance, not the next one or started in an earlier second all hold with their reason; execution binds a start within the submission's second and never one in an earlier second; only an unaccepted rerun with no new attempt past the floor reads FAILED. A bound run of another event (a synthetic control of the binding 0.12.0 made), on another head commit, created before the publication or of another workflow (both bound by the genuine 0.12.0 writer) never binds, and the row's own run binds once listed.
- <a id="a291"></a>**A291** — The released 0.12.0 source, extracted from Git objects, reads channel-free candidate-written validation rows holding R115's `readback`, `attempt` and `rejected_run` fields and their outcomes exactly as recorded. It refuses the actual channel-bearing journal under R120; both reads preserve bytes.
- <a id="a292"></a>**A292** — For a row Pod performed (an executed dispatch and a derived validation), PASS, FAILED and CANCELED refuse `performed_by_pod` and UNKNOWN with a provider receipt refuses; UNKNOWN records, its identical restatement writes nothing, another receipt refuses, and the row never becomes CANCELED.
- <a id="a293"></a>**A293** — For a decision reservation, PASS with a caller receipt, PASS or FAILED that readback does not show, CANCELED without an attestation, CANCELED when readback sees the effect, and an unreadable provider each refuse without a write; PASS that readback shows records Pod's receipt and the caller's evidence reference.
- <a id="a294"></a>**A294** — A fresh reservation offers "cancel, then execute"; once readback bound a run to it, an attaching execute is told to reconcile instead, and CANCELED refuses.
- <a id="a295"></a>**A295** — A merge decision's next action says to fetch first. Merge PASS without `merge_commit`, or with one the unfetched target does not contain, refuses; after the fetch PASS keeps the merge commit. FAILED and CANCELED refuse while the target holds the result, including as a squash, or cannot be observed, and FAILED records once it lacks the result.
- <a id="a296"></a>**A296** — A release or deploy PASS or FAILED without a receipt or evidence reference refuses and with one records; CANCELED needs an attestation.
- <a id="a297"></a>**A297** — A lost execution reply leaves UNKNOWN, which holds the repeated request until readback settles it and then answers REUSE; a repeated outcome after a lost reply writes nothing; each provider effect happens once.

### R119 — Adopt unchanged governance at a checkpoint

Type: B,H · Scenarios: [A331](#a331), [A332](#a332), [A333](#a333), [A334](#a334), [A335](#a335), [A336](#a336), [A337](#a337), [A338](#a338)

Governance binds the canonical selected target identity and exact base snapshot. An
explicit initial user selection is trusted input, including on a nondefault branch
or without a known default. Git cannot prove pre-intake authorship. The default
selection continues to require the same symbolic `origin/HEAD` target.

A checkpoint, including closure, first retains today's currency rules: the bound
base passes, and the exact verified delivery result passes only for its recorded
base and target, retaining the trusted baseline without adopting candidate policy
(R93). Otherwise a target advanced beyond the bound base is adopted without `governance_refresh` or an owner
question when every bound source has the identical byte revision, the recorded
delivery result (if any) is an ancestor of that target, and the default branch is
unchanged. The bound base must be an ancestor of the new target; backward and sideways ref moves keep today's refusal. Bound sources are `GOVERNANCE_PATHS` minus the intake exclusions;
recorded absence participates, so appearing or disappearing is a change.

Pod records a `derived` decision of kind `unchanged_sources`, with the previous and
new bases, canonical target, sequence and unchanged path/revision table. It moves
the bound base and citations, retaining the original `proof_base` and governance
proof identity across the unchanged-source adoption chain. Existing receipts are
unchanged; merge rows retain their admitted governance base (earlier rows use the
pre-adoption proof base), so later outcome and delivery recording still verify that
exact merge; candidate, definition, dependency, environment and other proof checks
still apply. Adoption never rebinds proof to a changed candidate. Objective commits
alone do not fire `candidate_policy_target`: only changed bound governance does.
Admission and Governor effects may refuse until that checkpoint; their next action
then names the plain checkpoint and says it needs no owner question.

A changed or unreadable bound source, missing delivery ancestry or changed default
branch retains today's `governance_changed` refusal and owner snapshot recovery.
An unresolved target retains `governance_unavailable`. Only derived adoption limits
observation to bound sources; owner refresh and bound-base observation still read
every governance path, including excluded paths, and hold on unreadable sources.
Changed snapshots sharing known objective ancestry beyond the old base require a
fresh owner decision through `governance_refresh: true`, `revision_authority`, and
`governance` naming the exact newly observed canonical `base_ref` and full `base`.
Generic or stale selectors cannot authorize that snapshot. Independent changed
policy refreshes retain their existing rules. An exact new owner snapshot decision
can proceed despite unavailable old objects, preserving their retained identities.

Kernel-owned `governance_history` keeps up to 64 distinct candidate identities and
64 owner decisions. Derived adoptions have no count cap and retain every row unchanged;
R121 relocates earlier rows, and the record byte capacity contract governs fit.
A mapped Git checkpoint needs a resolvable candidate;
caller omission/replacement cannot erase history. Capacity refuses rather than
evicts: a well-formed history beyond either remaining count bound refuses
`governance_unavailable` / `governance_history_full` on checkpoint, closure and
validation alike, naming the exhausted candidate or owner-decision history and
preservation/reporting as the next action. Full derived rows appear only in
`governance_history`. Status's top-level `derived_governance_decisions` and the
closure report's same-named field share a bounded projection: count, the first
`previous_base`, latest `base` and `base_ref`, latest unchanged `sources` table and
`history` reference to `governance_history/decisions`, with zero-based `first`/`last`
positions and `provenance: derived` selecting only those rows within the interval.
The projection never lists individual derived rows. The closure report's
`governance_decisions` lists only owner decisions in full, with R120 channels;
automatic adoptions cannot grow the report or status by repeating their rows.

- <a id="a331"></a>**A331** — After sibling merges with identical bound sources, a plain checkpoint passes, moves the governance base and records a derived adoption naming both bases and every unchanged digest, including absent sources. Eighty such moves pass and validate with every adoption unchanged, including after R121 relocation; derived rows spend no owner-decision slots. Six hundred retained adoptions still close, with only the bounded derived projection in status and the closure report, full owner decisions unchanged and every derived row kept in governance history. Status JSON growth from one to hundreds of adoptions is limited to the projection's counters and byte measurements, and text prints one summary. The sixty-fifth owner decision still refuses `governance_unavailable` / `governance_history_full` without a write; well-formed owner or candidate overflow gives that capacity refusal and its preservation/reporting next action through checkpoint, closure and validation, never a malformed-state refusal.
- <a id="a332"></a>**A332** — When only the objective's own merge advanced the target, a plain checkpoint passes without `candidate_policy_target` and records derived adoption when R93's exact-delivery exemption does not already apply.
- <a id="a333"></a>**A333** — A changed bound source, an absent source appearing, or a present source disappearing refuses `governance_changed` without a write, retaining owner snapshot recovery. A backward or sideways target move also retains its refusal even with identical source bytes.
- <a id="a334"></a>**A334** — A new target missing the recorded delivery result refuses `governance_changed` without a write even when all source revisions match.
- <a id="a335"></a>**A335** — A changed default branch for `selection: default` refuses `governance_changed` without a write.
- <a id="a336"></a>**A336** — An unreadable bound source at a moved target refuses `governance_changed`, "the target branch moved since governance was bound", with next action `write a checkpoint with governance_refresh` and no write; unavailable evidence is never inferred unchanged.
- <a id="a337"></a>**A337** — A closing write adopts unchanged governance, relocates citations and retains receipts valid for its unchanged candidate without `rebind`; changing the candidate never rebinds that proof and closure refuses invalidated evidence.
- <a id="a338"></a>**A338** — Excluded sources do not participate in derived adoption; owner refresh and bound-base observation still read every governance path, so an unreadable excluded source holds a refresh as before. Admission/effect refusal names a plain adopting checkpoint without an owner question. An exact source-changing delivery retains R93's existing exemption and old base, while a later sibling merge still refuses because its policy differs from that bound base. A 0.13.3-shaped merge row without `governance_binding` keeps its original base through adoption, outcome and delivery.

### R120 — Record every owner decision's channel

Type: B,H · Scenarios: [A339](#a339), [A340](#a340), [A341](#a341), [A342](#a342), [A343](#a343), [A356](#a356)

One shared validator checks owner-authority inputs: `provenance: user_direct`,
`instruction`, optional `channel` (`session` by default, or `relayed`), and `relay`
exactly when relayed. A relay contains bounded nonempty `reference` and
`owner_source` text identifying the relay and the verified owner source. Existing
kind-specific inputs that lacked provenance/instruction retain their fields and
accept these common fields additively. Every new accepted decision stores its
channel and, when relayed, the complete relay. Existing records without a channel
read as `channel: session` with `channel_recorded: false` and text "session (earlier record)"; they are never rewritten on read.

The exhaustive relayed list is: owner answers to an already recorded question,
governance snapshot adoption/retarget, withdrawal, R110 continuation instructions
recorded as `claim`, and retirement (R112). A general `revision_authority` answer supplies
`question`, resolving to a string or the `id`/`reference` of a question previously
recorded in the checkpoint; Pod preserves that reference. A question added in the
same write is not a previously recorded question. A relayed general revision
without that binding refuses `relayed_decision_refused` with a next action.

Every `pod-authorization/v1` scope (`publish`, `merge`, `release`, `deploy`) requires
`session`; a relayed authorization refuses `relayed_authorization_refused` and names
typed consent in the coordinator's session as its next action. Every other kind
requires session too, including owner handoff R105, runtime continuity R98,
direct-user route/delegation constraints, general revisions/reopen without a recorded-question answer,
user-cleared route failures and Governor efficiency exceptions. Such a relay refuses
`relayed_decision_refused`. Missing/partial relay or relay alongside session refuses.
A recorded question answer permits only the answer it conveys; a mailbox message
alone is never a decision or authority. The coordinator classifies the channel
honestly; Pod cannot prove where a keystroke came from or independently verify a
relay's claimed owner source.

Channels remain with revision history, governance decisions, obligation sources,
withdrawals, reopens, constraints, continuity decisions, claims, retirement entries,
route-failure clear decisions and Governor authorization/exception rows. Status and closure reports
show the channel of every owner decision they list, and distinguish R119's bounded
derived-adoption projection from full owner decisions. A carried Governor row's
authorization and efficiency exception remain predecessor decisions, omitted from
the successor's owner-decision inventory while retained in the journal (R118).
Channel-bearing records that earlier readers cannot
read faithfully carry the `decision_channels` discriminator whenever a stored channel
or relay key is present, or a stored closure report carries `decision_channels`,
in contexts and Governor journals alike, so those readers
refuse rather than misread carried consent, relay provenance or the closure format; earlier
records, claims, closures and retirements retain their original meaning.

Owner revision decisions have no count cap. R121 moves earlier rows unchanged and
the record's byte capacity contract governs fit; `governance_history` retains its
separate 64-candidate and 64-owner-decision bounds; derived adoptions spend no owner-decision slots.

- <a id="a339"></a>**A339** — Every owner-decision surface uses the shared validator, stores default `session` or the supplied honest channel and includes it in status/report projections; derived adoptions use R119's bounded projection, with full rows only in governance history and full owner decisions unchanged, including owners interleaved among adoptions.
- <a id="a340"></a>**A340** — A complete verified-source relay is accepted for snapshot adoption, withdrawal, a recorded question answer, claim and retirement, preserving the reference/source and showing `relayed` in status and closure reports.
- <a id="a341"></a>**A341** — All authorization scopes refuse relayed consent with `relayed_authorization_refused`; handoff, runtime continuity, constraints, Governor exceptions and other unlisted kinds refuse `relayed_decision_refused`, naming session consent as the next action.
- <a id="a342"></a>**A342** — Missing or partial relay, empty/oversized reference or owner source, a relay with session, unsupported channel or an unresolved question reference refuses without a write; newly supplied questions cannot authorize the same write. Through public `internal governor` merge/publish and `governor-execute` publish, a declared relay missing its complete relay or session consent carrying a relay refuses `invalid_decision_relay`, and an unsupported authorization channel refuses `invalid_decision_channel`; each names consent typed in the coordinator's own session and writes no journal row.
- <a id="a343"></a>**A343** — Genuine earlier-writer records, claims, closures and retirements read unchanged with session channels marked as earlier records; earlier readers refuse contexts and journals with stored channel or relay keys, and contexts whose stored closure report carries `decision_channels`, preserving bytes. Genuine 0.13.3 refuses a candidate closure without any owner decision at the context marker, and a candidate-written carried journal rather than losing carried-consent recognition; the current reader still validates that closure and detects carried consent, and R118 preflight counts the marker before any claim write. A pre-channel retirement replays the same decision with omitted or explicit default `session` and no relay; other decisions still refuse `objective_retired`.
- <a id="a356"></a>**A356** — Eighty successive owner-authorized writes are accepted and read back with every revision decision, without a count refusal; the record byte capacity contract and R121 relocation still govern fit.

- <a id="a371"></a>**A371** — With A's required audit satisfied and B's audit open but unadmitted, A's merge allows without withdrawing B; B defers assurance_unbound until its own audit is satisfied.
- <a id="a372"></a>**A372** — An assurance without unit binding gates both units, including unchanged records written before the rule.
- <a id="a373"></a>**A373** — Only an assurance accepts a valid Governor unit identifier; it can name an unprepared unit, and no later write can add, remove or change an existing row's binding, even with revision authority.
- <a id="a374"></a>**A374** — Genuine 0.15.0 single-unit missing-proof, satisfied-proof and attached-action decisions, codes and complete journal rows match on the same inputs and clock; existing records validate without conversion.
- <a id="a375"></a>**A375** — A's merge can pass its assurance gate while the objective's label and final acceptance remain withheld for B's open audit; admitting B's running audit still defers release with integration_unsettled, and satisfying every audit qualifies the objective.
