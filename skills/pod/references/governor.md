# Delivery

Governor ALLOW/REUSE/DEFER never overrides project authority. Efficiency exceptions never lift authority or correctness. Mutation needs the existing owner and stable native current Run/coordinator/generation binding; missing proof permits direct work/read-only status. Establish/adopt through Orca; join only exact objective assignments.

Record required unit audits as assurance obligations with `unit`, using the Governor unit identifier; the binding cannot be changed on an existing row. Release gates its own unit's required audits and every unbound audit. The separate release gate still waits for every outstanding objective assignment; the objective label and final acceptance still need every assurance.

Group corrections in one delivery unit. `internal governor-prepare` freezes candidate bindings; its acknowledgement supplies the next request. Changed inputs open a generation; tracked dirt leaves it unfrozen. `governor-preflight` records configured checks; CI-triggering push is validation.

`governor-status` reads Governor state. Use `governor-execute` directly for push, PR update, workflow dispatch, rerun, diagnostic and cancellation. `governor` reserves caller execution: ALLOW permits, REUSE attaches work/proof, DEFER names hold. Gates, validation and read-only review may overlap on one frozen candidate; merge/release/deploy waits for valid results. Execute the bound commit; merge/release/deploy remain decide-only. Lost responses stay UNKNOWN; `governor-reconcile` reads provider state without resubmission.

Publish the candidate through `governor-execute` (push or pr_update), never plain git. Pod counts only the pushes it recorded, so a push it did not perform leaves validation and merge deferred `candidate_unpublished`. Recording a push that already landed moves no ref, and no run starts for it: its validation settles only from a push or pull-request run on that head created at or after the remote's newest update of the branch to it, which Pod reads from GitHub's branch activity, or reuses from its own earlier readback, and names as the time it used. Activity that cannot prove that update, such as a disagreeing or possibly incomplete second, keeps the submission; a successor that carried the validation keeps the same floor.

An unverified item has one of two homes. A checkpoint's `verification_gaps` holds only gaps in verifying the merge unit itself, such as a check not run on the candidate or a required result still missing for it. Each gap holds the merge: merge, release and deploy defer while any remains. An item that can only happen after the merge, such as installing merged `main` and reading it back, or that needs the owner's own acceptance, is not a verification gap. Record it as an obligation instead: `blocked_external` when it waits on the owner or another outside party, `waiting` when it is a sequenced step after the merge. Such an obligation does not hold the merge. A criterion that needs such an item stays open, honestly, after the merge until that item's evidence arrives; never mark it satisfied early, and never list it as a gap to keep it visible. When the Governor defers on `verification_gaps`, move each gap that is really post-merge or owner acceptance into such an obligation and drop it from the list in your next checkpoint. The gate itself does not change: a gap that does concern the merge unit is closed with evidence on the candidate.

Before the first remote Git mutation without applicable consent, offer **merge remotely, keep local, or defer**, showing repository, target, exact candidate commit/tree and publish/merge scope. Record merge-remotely `pod-authorization/v1` for publish/merge bound to commit/tree and `<remote>/<base>`. Consent covers push/PR, required CI, reruns/diagnostics/cancellation, exact-candidate merge with `--match-head-commit`, and stated post-merge install/verification. Routine validation needs no further prompt; candidate/target movement requires fresh consent. Deployment, new provider effects and spending remain outside. Keep-local records user_direct delivery revision, defers remote kinds and marks hosted checks NOT_RUN; defer records `user_hold`. Prior exact-scope instructions count.

The specialized publish/merge consent path may stay `channel: relayed` only when the host configured a verifier in personal `authorization_verifier`. Send a separate authorization for each scope, with `relay.reference`, target `<remote>/<base>` and, for merge, equal integer `pr` on the action and authorization. Pod verifies the exact action, objective, commit/tree, repository/remote/base and PR at the Governor boundary, supplies `relay.owner_source` from the verdict and retains the honest channel in status/report. Managed effects revalidate immediately before mutation. Merge stays decide-only: call the Governor at the merge boundary, then use its verified PR and `--match-head-commit`; Pod cannot intercept a later external command. Caller-performed publish reservations share that pre-effect revalidation limit; managed `governor-execute` is the rechecked publish path. A recorded-question answer or free-text source grants nothing. Repeating the same exact publish/merge authorization is idempotent; a consumed decision cannot authorize a different binding. Follow each verifier refusal's next action; absent verification needs session consent. On that specialized path, exact `pod-authorization/v1` release/deploy consent remains session-only; the verified standing exception below has its own scope. Never label an agent-retyped relay as session: explicit session-plus-relay input refuses, but Pod cannot detect hidden origins after every relay field was stripped.

After merge record `governor-outcome` provider `merge_commit` and checkpoint `delivery: {record}`; at closure, reconcile pending rows first, except that the closing write keeps a validation derived from an earlier head the merged pull request superseded as UNKNOWN; verify exact result/tree, target and readback, retaining trusted governance and valid proof. Follow finalization through every validation readback and report consumption to closure. Once delivery is proved, record closure in that turn before the coordinator ends, is released or its tab closes. A closing checkpoint or report, and a checkpoint only recording an R93 verified delivery, need no governance refresh or owner snapshot decision when the unchanged target contains that delivery: retain the trusted baseline, adopt no changed policy and record that no adoption occurred. Work replaced before delivery closes by withdrawal: in one closing checkpoint, withdraw every remaining live obligation with the authority it needs (the coordinator's own subgoals and assurance by `coordinator`; other rows under a top-level `revision_authority`, an agent decision where the decision policy lets agents change scope) and change nothing else, with no new proof, row, delivery, disposition, rebind or governance refresh. That write keeps the bound base even after the target moved, needs no fresh review of satisfied rows, which stay as recorded history, and its answer says withdrawn, not delivered; do not refresh governance merely to stop. Other target movement keeps the governance rules. Follow project checks/merge queue. Observe mode warns only on efficiency deferrals; enforcement stays advisory unless personal policy names host controls. Workflow proposals remain suggestions. Unknown authority/effects defer; unobserved cost stays unknown; local checks cannot replace required hosted proof. Supersedence cancels only permitted pending cancel-safe older validation, never approving the new candidate.

When project governance delivers by a host's protected local append to a release train instead of a remote merge (the delivery decision stays local), record it after the host's operation succeeded with a delivery-only checkpoint `delivery: {local_append: {operation: ID}}`, naming only the host's operation id. Pod reads it back through the personal `authorization_verifier` and checks the candidate, its ready exact proof (`pod admission --selected`) and an exact fast-forward or clean merge itself; no publish, merge or standing authorization is used. Each refusal names its next step and writes nothing: on `local_append_requested`, `local_append_running` or `local_append_unknown` repeat the same checkpoint once the host settles; never append, retry or replay on the host. After a lost acknowledgement, read the record back with `pod status` and continue at the next sequence. Then tick PoDs and close in a later checkpoint as for any delivery; the closing write may restate the delivery but never records it first. One objective holds one delivery, so a remote merge is never admitted after a local one. A subgoal that cites an append receipt is not a delivery.

For multiple delivery units, distinguish the trusted policy base from the current
target and the merge's admitted parent. At admission the Governor requires the
prepared target ref to match governance and binds its exact current commit in
`target_binding.commit`. Currency accepts the policy base or this objective's one
verified delivery result for that same base and target, without adopting the
delivered policy. A second merge can therefore use that result as its parent while
retaining the earlier policy base. Merge parents must be exactly the admitted
commit and candidate; a squash has only the admitted commit as parent, and a
fast-forward equals the candidate with the admitted commit as ancestor. Fetch
first and call the Governor at the merge boundary. If the result does not use
the admitted commit (the target moved after admission or the merge rewrote
commits), a later snapshot decision cannot settle that row: keep it UNKNOWN with
the merge evidence. Never call a landed merge FAILED or CANCELED.

The second merge creates no second map delivery record or new currency exception.
Before later work, target movement beyond the recorded delivery needs a plain
checkpoint when every bound governance source is unchanged; Pod adopts that
snapshot without an owner question. Changed governance is adopted with
`governance_refresh: true`, which needs no owner decision unless the target holds
this objective's own work beyond its bound base, such as a delivery the base does not
yet contain. That needs an exact snapshot decision for the observed canonical
`base_ref` and full `base`, recorded as `revision_authority` from the decider the
decision policy names for scope changes; the refusal says which applies. An honestly
relayed snapshot decision remains relayed. Satisfied proof crosses changed governance only by an attested rebind in that refresh (see the proof reference). The completion-bookkeeping exception above retains the
trusted baseline and adopts no later policy, and so does a checkpoint that only records post-delivery
bookkeeping (acceptance, external evidence, questions, next action) with the same candidate, rows and
settled proof. Both, and the same writes while governance is still current, judge the settled proof's
bound files against the delivered candidate's Git tree, so
later work in a shared worktree does not invalidate it; keep the checkpoint candidate on the delivered one
and leave the worktree where it is.

For an earlier merge row lacking `target_binding.commit`, retry the supported
`governor-outcome PASS` with its exact provider `merge_commit` after fetching.
Pod first checks its original policy base. It can recover the earlier parent
only from this objective's map delivery result when that delivery names a different
ALLOW merge PASS row in the same journal, its receipt finished at or before this
row's admission, its base and target match this row's admitted policy base and
target, the row is not carried, and all candidate, tree, authorization and readback
checks pass. The acknowledgement returns a kernel-owned `parent_basis` marked
`basis: verified_delivery` and `observed_at_admission: false`, with the commit,
delivery record, admitted base and `prior` (an earlier UNKNOWN outcome and its
time, or null). This preserves that the parent was not observed at admission;
admitted bindings, provider evidence and detail stay recorded, and a PASS that would
replace them refuses `receipt_recorded`. Do not submit that marker or edit a journal
to obtain recovery. Other earlier cases keep their precise refusal: a row with an
admitted governance binding stays UNKNOWN with its evidence, because a new snapshot
decision cannot change its admitted base; only a row without that binding is still
settled by an exact snapshot decision. Carried rows settle through their existing provider-readback path using
their copied target binding and gain no earlier-delivery inference.

## Yolo mode

Yolo mode is the owner's standing authorization for one objective, recorded with `pod internal standing-authorization` (read `internal contract` for its record first). It removes only the wait for consent. Every gate still runs: local checks, hosted CI, independent review, exact-head merge, governance currency and the revalidation at each effect.

Record it when the owner asks. `/pod <link> yolo` is recorded right after intake and before any admission. "Use yolo from now on" is recorded as an `activate` with the exact scope, any explicit widening such as "yolo including deploy" and any expiry the owner stated; "stop yolo" is a `revoke`, recorded at once. Activation of an issue-bound objective refuses `issue_reconciliation_required` while a live edit is unreconciled: reconcile first. Use `show`, or status's `standing_authorization`, to read the mode.

Record `channel: session` only for text the owner typed in your own tab. Text that an executor, a host agent or a mailbox relayed is `relayed`: Pod accepts it only after the host verifier confirms it, and refuses it without one. Pod cannot tell where a keystroke came from, so never relabel a relayed message as session.

For a new policy-governed standing decision, read `standing-authorization` show and supply its current `scope_digest` on the activation decision. A saved positive answer cannot confirm changed material scope. On this path, a `paid_resources` widening accepts only explicit `spending: included_subscription`; future spending requires its own exact person approval. The router's decision guidance owns the new actor input.

A verified relayed standing activation may cover release and an explicit owner widening to deploy, only when the configured verifier confirms the exact objective, operation, scope, widening and expiry. Deploy stays excluded by default, and this record authorizes no deploy target by itself. Each activation, revocation or responder-answer key records one row; a replay refuses, including after a stop.

While the mode is `yolo`, do not offer the three-way delivery decision or wait for publish, merge or release consent. Call the Governor or `governor-execute` with no `authorization`: it derives an exact-scope authorization for each candidate, and a merge request must name its `pr`. A consent wait that already deferred is satisfied at the next Governor check, and explicit exact consent still takes precedence. With `install` in scope, perform the stated post-merge installation and verification without asking. With `cleanup` in scope, read `standing_authorization` in the cleanup plan: it lists what yolo covers and what still asks.

Immediately before a caller-executed merge or reservation, when time has passed or a stop may have arrived, run the Governor again (or `show` to check standing state). Continue only with current applicable authority for that exact candidate and target; an earlier ALLOW cannot establish that yolo is still active.

Yolo alone supplies no authority for production deployment unless explicitly widened, new paid or cloud resources, secrets, deleting unique or unmerged material or work beyond the objective's scope. Resolve those decisions under the machine policy using the router's decision guidance; new spending always needs the person. Host maintenance confirmations, protected credential changes and other host-policy items are host rules that this record never widens.

A material scope change suspends yolo: changed requirement text, an amendment or the criteria. Checkbox-only edits and Pod's own ticks do not. Expiry, a revocation and closure end it too, and it never carries to a successor or another objective. While it is not active, ask the three-way delivery decision as usual; the owner turns it on again with a new activation.

With yolo on, follow the host's designated responder's answer to a recorded question only when it arrives through the verified answer path (`answer`). Pod records it as the responder's decision, never the owner's, and it grants no authority. A responder answer never resolves an R4-excluded question or any owner-decision kind: production deployment, new paid or cloud resources, secrets, deleting unique or unmerged material, or work beyond the objective's scope. Those questions require their own supported decision path. A separate host-verified decision may authorize the exact effect when current machine policy permits; a trusted agent's attested scope must cover it. Money/new commitments, policy changes and human-only login stay person-only. Use the router's decision guidance for that separate path. An existing explicit owner widening supplies only its own bounded scope; the responder cannot extend it. Pod stores answers without classifying their subject, so enforce this boundary yourself. Without that observation path, use the router's decision guidance for a fresh applicable decision; a responder's recommendation may go in the question's choices.

Final reports and closure name the mode with its scope and source, each effect authorized under it and each clarification the responder decided; status's `standing_authorization` carries them.

After Pod records closure, its shared source effect immediately rechecks and closes only the objective's own bound source as completed. Read `source_issues` and `parents_open` in the acknowledgement: Pod closure and provider completion are distinct facts. An already completed source needs no write; another closing reason stays unchanged. Refused, unavailable or unknown effects leave Pod closure intact. Run `pod sweep --objective ID --json` to recheck and retry the remaining source effect, including after an ordinary closure or a process interruption. Never replace missing proof with an owner completion question. Referenced SEAL/EEL parents remain tasks for the project lead with the facts Pod read and their rule: an EEL closes when its consuming PES work is delivered; a SEAL closes when every requirement's delivering PES is closed. The child alone cannot evaluate other consumers or requirements. GitHub has no conditional issue update, so an outside edit between the immediate source read and PATCH remains a race.
