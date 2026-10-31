# Coordination

## Requirements

R166 adds exact policy-governed agent and host-verified decision approval at the existing mutation boundaries. References below to direct-user/session inputs retain their existing wire meaning; new approval is a separate actor record and does not change requirement provenance, factual predicates or acceptance authority.

### R05 — Normal invocation inspects objective, criteria, rules

Type: B · Scenarios: [A02](#a02), [A04](#a04)

Normal invocation inspects objective, criteria, rules and consequential assumptions, then proceeds within existing authorization. Use a compact execution brief when useful; trivial work requires neither workers nor milestone ceremony.

### R06 — Plan-only work permits useful host-permitted investigation

Type: H · Scenarios: [A03](#a03), [A04](#a04), [A42](#a42)

Plan-only work permits useful host-permitted investigation but no implementation workers or product edits. Assess tooling side effects first. Describe behavioral restraint accurately. Plan-then-execute avoids another approval unless a real boundary changes.

### R07 — Substantive plans identify criteria, challenged assumptions

Type: B · Scenarios: [A30](#a30), [A43](#a43)

Substantive plans identify criteria, challenged assumptions, coordinator/worker responsibilities, dependencies, editing boundaries, verification and revision triggers. Decomposition is provisional; material scope/acceptance changes require explicit revision.

### R08 — Choose tools, direct session work or

Type: B · Scenarios: [A02](#a02), [A05](#a05), [A06](#a06), [A44](#a44)

Choose tools, direct session work or delegation before a worker model. Assess each assignment's complexity, risk, size, uncertainty, verifiability, capabilities and context availability independently of its parent.

### R22 — Keep the coordinator conversation, model, effort

Type: B,H · Scenarios: [A31](#a31), [A37](#a37), [A55](#a55)

Keep the coordinator conversation, model, effort and host settings unchanged, except the objective coordinator replacement explicitly confirmed under R105. Preserve useful integration, verification and reporting headroom; checkpoint before foreseeable interruption. Worker selection changes only workers.

### R26 — workers.max_active bounds concurrently reserved logical assignments

Type: H · Scenarios: [A18](#a18), [A19](#a19), [A130](#a130), [A143](#a143), [A177](#a177)

`workers.max_active` bounds concurrently reserved logical assignments per objective. It is personal, 0–8, default two, and a direct constraint may narrow it. Investigators, reviewers and authorized descendants count. Zero workers is valid. The ceiling is never a utilization target: dispatch only ready distinct work that saves time toward a verified result. A free reservation admits nothing by itself, and a capacity wait is valid only at the ceiling. Native settlement frees a slot even when a terminal is retained; never infer physical occupancy.

### R27 — Only the coordinator delegates unless direct

Type: H · Scenarios: [A16](models/routing.md#a16), [A19](#a19), [A20](orca.md#a20), [A21](#a21)

Only the coordinator delegates unless direct user intent explicitly permits a descendant. Every Pod-managed descendant needs its own objective reservation under the same ceiling. Native limits and hidden provider fan-out are not Pod observations.

### R30 — Parallelize only independent responsibilities/editing boundaries; begin

Type: H · Scenarios: [A24](#a24), [A42](#a42), [A61](#a61), [A176](#a176), [A177](#a177)

Parallelize ready independent responsibilities with distinct editing boundaries when doing so saves time to a verified result. Begin with one writer while a shared contract is unsettled; this is not a one-worker policy. Re-evaluate at transitions. Use project/host-supported isolation, preserve unrelated changes, and never silently stash/reset/clean or execute unauthorized setup hooks.

### R31 — Use bounded versioned packets carrying objective/criteria

Type: B · Scenarios: [A24](#a24), [A35](#a35), [A62](#a62)

Use bounded versioned packets carrying objective/criteria, responsibility, scope, candidate, context references, dependencies, permitted actions, route, revisions, reporting contract and native bindings when issued. Do not clone the full coordinator transcript or predict runtime identities.

### R32 — Reuse bounded context only while relevant

Type: B · Scenarios: [A29](#a29), [A30](#a30), [A63](#a63)

Reuse bounded context only while relevant source, instruction, requirement, candidate and revision bindings remain valid. Invalidate affected summaries/evidence on change. Do not introduce automatic repository uploads or a vector database.

### R37 — After two materially equivalent failed corrections

Type: B · Scenarios: [A25](#a25), [A65](#a65)

After two materially equivalent failed corrections without new evidence, diagnose. Record obligation, failing example, hypothesis, last evidence, discriminating check and correction identity. Resume only after changing a relevant variable based on evidence; preserve history across restarts.

### R39 — Before the first delegation, or when

Type: B,H · Scenarios: [A27](assurance.md#a27), [A30](#a30), [A43](#a43), [A163](#a163)

Before the first delegation, or when an execution brief is written, record an obligation map (R81) that covers every original criterion and required delivery obligation with provenance and a check or explicit human/provider dependency. Preserve criteria through decomposition; update evidence rather than redefine success. Trivial direct work needs no map, and R81–R89 are inert without one.

### R46 — Steering creates a new revision, reconciles

Type: B,H · Scenarios: [A30](#a30), [A43](#a43), [A67](assurance.md#a67)

Steering creates a new revision, reconciles affected assignments at safe native boundaries, preserves useful unaffected work and prevents obsolete reports/proof satisfying revised work, including changed definitions on stable obligation ids. Unrelated steering preserves unaffected definition bindings. Material acceptance changes require explicit authorization; R149 defines the narrow verified issue-source path.

### R65 — Normal invocation plans proportionately and proceeds

Type: B,H · Scenarios: [A104](#a104), [A158](assurance.md#a158)

Normal invocation plans proportionately and proceeds within authorization. Plan-only remains read-only under actual host Plan Mode. Plan-then-execute needs no ceremonial approval. Continuation reuses reconciled objective and native state, and continuing a quiescent objective re-evaluates its external waits without a reopen. Small direct work needs no issue, worktree ceremony or worker.

### R81 — An obligation has an id, introducing

Type: H · Scenarios: [A146](#a146), [A147](#a147), [A160](#a160)

An obligation has an id, introducing sequence, kind (`criterion`, `delivery`, `assurance`, `correction`, `steer`, `subgoal`), provenance (`objective`, `user_direct`, `project_policy`, `coordinator`), `check` or `resolves` with `stop_condition`, boundary and source. Non-assurance rows may declare optional `proof_scope`, a bounded repo-relative path list separate from editing ownership; only coordinator subgoals may declare it empty. Its presence participates in definition identity; absence preserves older definitions. Changing it follows ordinary boundary authority and invalidates carried proof. Subgoals, corrections and coordinator assurance name a parent. Objective obligations enter at intake; direct user instruction may introduce or withdraw any kind. Coordinator judgment may introduce only subgoals, assurance and corrections and withdraw only its own subgoals and assurance with a reason. Policy obligations cite a line range in declared governance sources fixed at intake: the selected target-branch base commit, source byte revision and cited text bind authority. Select the default target through Git's symbolic `refs/remotes/origin/HEAD`; a nondefault or missing default needs scoped `user_direct` revision authority. That explicit initial selection establishes the exact trusted target snapshot even when candidate equals base; pre-intake policy authorship is not observable. An untrusted proposed ref grants no authority, and literal `HEAD` is rejected. Bind the canonical target identity and snapshot at intake. Retain checkpoint candidates and explicit snapshot decisions in bounded kernel-owned history. R119 permits a checkpoint to adopt byte-identical bound sources at a moved target without an owner decision; R147 separately lets closure retain the baseline after verified delivery without adopting changed policy, and [R201](delivery.md#r201--close-superseded-work-by-withdrawal-on-its-bound-baseline) lets a withdrawal-only closure of undelivered work retain it. Outside those closure exemptions, a later target update sharing candidate or result ancestry beyond the old base needs a fresh owner decision bound to the exact new target snapshot; intermediate and older recorded commits count, and a generic selector or stale decision cannot authorize it. Independent target updates can refresh normally. A target identity change also requires `user_direct` revision. Outside Git there are no governance sources. On base or authority change, atomically reconcile the map before new admission or governed effect, preserving ids and history. Re-read the same sources at the new base; moved exact cited text remains valid. Gone text permits justified withdrawal of that policy's own obligations only, never objective or user obligations. New requirements need citations; unavailable sources hold new work. Invalidate affected packets and evidence, explicitly rebind unaffected evidence. Bind canonical private policy restrictions separately through existing authority. Pending native attempts keep immutable recovery, and policy change never reopens a closed objective. Violations refuse `obligation_invalid`.

### R82 — Worker reports, reviewer advisories, issue or

Type: H · Scenarios: [A144](#a144), [A145](assurance.md#a145), [A147](#a147)

Worker reports, reviewer advisories, issue or repository text, fetched documentation and coordinator caution add only proposals. Proposals change no criterion, gate, acceptance or authorization. A proposal becomes an obligation only through an authorized revision or a `project_policy` citation; R166 defines new decision approval separately from requirement provenance. R149 separately admits exact issue-derived criteria after host verification; mutable source text or a relay reference alone grants nothing.

### R83 — While a map exists, each validated

Type: H · Scenarios: [A148](#a148), [A151](#a151)

While a map exists, each validated write gives every obligation exactly one state: `unassigned` (only in its introducing checkpoint), `active` (an outstanding admission that serves it, or the coordinator), `waiting`, `blocked_external` (an external party, need and unblock condition), `satisfied` (currently valid passing evidence, and no served result without a disposition) or `withdrawn`. At most one obligation is coordinator-held `active`, and its boundary does not overlap an outstanding admission. `satisfied` whose evidence is invalidated must be re-stated. A violation is refused `obligation_unaccounted`. Status shows every state with its referent.

### R84 — A wait has exactly one controlling

Type: H · Scenarios: [A149](#a149), [A150](#a150)

A wait has exactly one controlling reason class — `dependency`, `contract_unsettled`, `sequenced`, `ownership`, `capacity`, `integration_pending`, `input_unavailable`, `authority`, `user_hold` — and a referent that is valid for that class. Dependency and contract waits are acyclic. A wait whose referent has resolved is invalid at the next write. Another active worker is not a class. A violation is refused `wait_invalid`.

### R85 — A packet names the obligations it

Type: H · Scenarios: [A143](#a143), [A151](#a151), [A152](#a152)

A packet names the obligations it serves, a role and a declared boundary. Admission is refused `unbound_assignment` when a served obligation is missing, proposed, satisfied, withdrawn, outside the current revision or already served by an outstanding admission; when a review packet does not serve an assurance obligation; or when an investigation lacks `resolves` and `stop_condition`. An implementation packet whose boundary overlaps an outstanding admission or the coordinator-held boundary is refused `ownership_conflict`. R171 adds the required-finding resolution check before review reservation. Admission sets the served obligations `active` in the same locked write as the admission row.

### R86 — At report consumption, Pod compares a

Type: B,H · Scenarios: [A153](#a153), [A154](#a154)

At report consumption, Pod compares a settled implementation result's complete changed paths from Git with its declared boundary and records `boundary_exceeded`. Unknown, unavailable, or over-limit paths cannot establish non-overlap and hold further implementation until complete evidence or a reasoned disposition; over-limit paths cannot be integrated from incomplete evidence. Each result takes one disposition: `integrated_into` (validated by ancestry when a commit exists, otherwise coordinator attestation; needs a reason if the boundary was exceeded), or `discarded` with a reason, which preserves branch and evidence. Until then its owner obligation is coordinator-held or `sequenced`, and an overlapping implementation admission is refused `integration_pending`. The scoped efficiency exception cannot lift this.

### R89 — An objective is quiescent when nothing

Type: H · Scenarios: [A158](assurance.md#a158), [A159](#a159), [A160](#a160)

An objective is quiescent when nothing is active and every unfinished dependency or contract-wait chain ends in `blocked_external`, `authority`, `user_hold` or `input_unavailable`. It stays open, preserves its dependencies and reports incomplete. It closes when every obligation is satisfied or withdrawn, nothing R117 records before closure remains, and the closure report is written. After closure, admission and Governor mutation are refused `objective_closed` until an authorized revision reopens it (existing `user_direct`, or R166's verified decision path). R145 evidence closure separately ends proved delivered work after coordinator loss and refuses every later Pod write, including revision, with basis evidence. Closure promotes no label and grants no acceptance.

### R91 — Boundary refusals and properties P1–P6 are

Type: B,I · Scenarios: [A150](#a150), [A162](#a162)

Boundary refusals and properties P1–P6 are deterministic and generative tests. Coordination behaviour is evaluated on recorded, sanitized traces by scripted record checks: the efficiency triangle (slot filling refused; serialization flagged when an independent, dependency-free, non-overlapping obligation stays `sequenced` across two accepted map writes with free capacity and positively available delegation, when parallel work would shorten the verified path and no bounded rationale and revisit condition is bound to current inputs; amplification refused when a review repeats a valid binding), churn flagged at three settled admissions on one obligation without satisfaction, and scope inflation refused. Wall time and admission counts are recorded observations, never gates. There are no automatic paid runs.

### R95 — Bookkeeping interfaces

Type: B,I · Scenarios: [A173](#a173), [A174](#a174), [A224](#a224), [A228](#a228)

`internal map` reads the current restatable obligation map, seq, revision, candidate, governance, delivery, closure, outstanding work, settled attempts, undispositioned results, review eligibility and next actions. Checkpoint and report-map writes accept a validated `update` patch with required `seq` and explicit `reuse: {from, ids}` expanded to named per-row bindings; carried-forward core fields avoid restatement. Short non-assurance evidence expands to the canonical receipt, and a settled served assurance report can use its attempt. Refusals identify current seq, revision and a concrete correction. Evidence invalidation lists all failing rows, bounded to eight with remaining count, classifies candidate-only, definition, binding or governance changes and R171 review ordering failures and names REUSE eligibility and corrections. Wait refusals likewise list affected dependents while retaining the first refusal identity. Ref aliases require proven identity.

### R96 — Critical-path coordination

Type: B · Scenarios: [A176](#a176), [A177](#a177)

Optimize time to a verified result, not utilization or worker count. Re-evaluate at transitions; dispatch only ready, distinct, time-saving work. Once a candidate is frozen, run local gates and same-candidate read-only review concurrently when useful; acceptance requires both. Governor validation may overlap that review, while release waits. Waiting is valid; spare capacity creates no duty to dispatch, and lean is never a one-worker rule.

Corrections use ordinary direct-work/delegation judgment and current eligible pool or pin. Their substantive briefs name the failed rule, alternate entrypoints, supported older records and status/wait consumers, with focused related-case regressions. Independent and delta review remain required.

### R109 — Record the coordinator's judgement of outside results

Type: B,H · Scenarios: [A258](#a258), [A259](#a259)

An obligation row carries the coordinator's acceptance records, so a non-owner can tell which outside result an obligation took in. A checkpoint or report map write names them in a map-level `acceptances` list of `{obligation, source, verdict, candidate, evidence}`; `source` is the caller's opaque result id (1–128 printable characters without spaces), `verdict` is `accepted` or `rejected`, `candidate` and `evidence` are bounded text. Pod stamps each record with the writing map sequence and keeps at most 16 per obligation. Only map writes record them, so only the recorded coordinator with Run authority writes them. A recorded source is immutable: an identical restatement adds nothing and a different one refuses `acceptance_conflict`; a row cannot carry them (`acceptance_kernel_field`), and restatable rows omit them. Records survive later writes until the obligation reopens, meaning it leaves `satisfied` or its definition changes; then they clear. Status obligation rows and the map view show them for open and closed objectives; a closed objective records them in or before its closing write. They never enter an obligation's definition or the closure report.

### R117 — Record before announcing; resume without repeating

Type: B,H · Scenarios: [A298](#a298), [A299](#a299), [A300](#a300), [A301](#a301), [A302](#a302), [A303](#a303), [A398](interface.md#a398), [A405](interface.md#a405)

R144 requires same-turn closure once delivery proof and final-report consumption are complete, before the coordinator ends, is released or its tab closes. R145-R147 provide the separate evidence-closure route after coordinator loss without rewriting these records. Before a coordinator says an objective is finished or its terminal safe to retire, it records, in this order: the merge outcome, the delivery record, every validation readback, report consumption and closure. The Governor's pure `finalization(state, journal)` is the one projection of what remains: for each of `merge_outcome`, `delivery_record`, `validation_readback`, `report_consumption` and `closure` still unrecorded, in that order, the records' ids, kinds and states, `finished`, and a next action that names them in order. A pending or UNKNOWN merge row is `merge_outcome`; a merge PASS without the map's delivery record is `delivery_record`; every other unsettled allowed Governor row is `validation_readback`; an admission neither closed nor deferred without a consumed report, or an implementation result without a disposition, is `report_consumption`; an open map is `closure`. A map-less objective has no ordinary delivery record or coordinator closure to write; R145-R146 evidence closure is separate. An implementation result settles by its disposition, with or without a report; a worker that produced no report is consumed through the report route as failed or uncertain. Closure is the last record: a write that closes refuses `closure_unfinished`, naming the remaining records in order, while finalization names any record before closure, and a delivery record counts only from an earlier write. A recorded closure is re-validated without this check, so it stays valid. Closure ends finalization, and only then does it say finished. R127 adds the finished-tab precondition only to new closing writes and layers native tab wording onto reader results; the pure finalization function and its remaining, records and finished values stay unchanged. It reads only the stored context and journal, so a closed objective stays finished after its target advances or its worktree is removed. The `governor-outcome`, `governor-execute`, `governor-reconcile`, checkpoint and report results, the `internal map` view, and refusals on that path (`objective_closed`, closure waiting on settlement, and outcome, conflict and delivery refusals) carry it. A merge PASS reads back every unsettled effect of its unit and keeps a failed read as that attempt, so a merged candidate never leaves a validation whose readback was never attempted. A coordinator resumed on the same Run follows it: a decided merge attaches with "never merge again", an interrupted execution attaches and settles by readback, and recorded outcomes, deliveries, reports and closure answer from the record, so no merge, publication, dispatch, review or other external effect repeats. It adds no command or approval. R132 adds due ticks before either new closing route, including ticks made due by that write; it preserves existing closures, the closure report and finalization remaining, records and finished.

### R149 — Verify a distinct issue-source criteria revision

Type: B,H · Scenarios: [A486](#a486), [A487](#a487), [A488](#a488), [A489](#a489), [A490](#a490)

An existing open issue-bound objective accepts `revision_authority.operation: issue_revision` only through a checkpoint and the configured host's distinct `issue_revision` action. Provenance stays `user_direct` and channel stays `relayed`; input supplies only bounded descriptive instruction and `relay.reference`. Kernel-owned evidence binds the same exact source identity, full previous/proposed source digests, existing pre-write map seq/revision, current effective policy and fresh native coordinator handle/incarnation/Run/runtime/generation. Missing incarnation, native ambiguity, unsupported verifier, partial/mismatched bindings and denial fail closed. General relayed revision refusals remain unchanged. The host independently authenticates approval and causal Stop/epoch/cancellation controls; an echo fixture proves only Pod boundary behavior.

Authority covers only material source-derived criteria reconciliation and new accountable coordinator decomposition under live parent chains. Each live PoD occurs once and maps uniquely to an objective criterion bound to its complete physical source block, including continuation and nested acceptance text. New and replacement checks use that exact block, or its deterministic complete-block fingerprint when the existing check bound cannot hold the text. An existing paraphrased check keeps its definition and proof only when genuine prior definition/source facts establish unchanged complete scope and amendments; header equality or a newly observed body cannot establish those old facts. The accepted requirement scope and each selected PASS's actual proof-time scope must agree; a matching definition cache alone never establishes proof currency. An unreadable acceptance retains its exact known source binding and sequence, so hash-matching DATA can recover its block without inventing an acceptance event. Ordinary unchanged-definition proof re-selection within that accepted lineage keeps its checkpoint/report/admission behavior; missing or divergent source facts hold issue-revision currency, not unrelated bookkeeping. Coordinator-only metadata changes carry earlier physical scope or leave it unknown, even when a former definition hash returns. Owner semantic check revisions allowed by [R120](delivery.md#r120--record-every-owner-decisions-channel) keep their existing authority after genuine R149 supersession as well as ordinary intake. Historical retirement accounting follows the actual accepted replacement definition and its later validated owner revision, while every new semantic change still needs current independent authority; later checkpoint/report/admission and readable closure preserve that retirement. These revisions cannot resurrect an old receipt under changed physical scope, and unknown physical scope grants no proof. Earlier body DATA must hash-match a genuine kernel-retained association, resolving every selected passing proof at its actual accepted sequence. The interface defines the approved first-acceptance/source-sequence convention for earlier records and its residual; it establishes neither original-introduction scope nor earlier approval. Missing or conflicting old facts never carry current PASS: an explicit fresh-approved unknown remap retains the old definition/receipts and introduces exact-current-source criteria awaiting proof, without requiring nonexistent prior passing proof. Checkpoint criteria are exactly the recognized source references in source order: PoD#N, and ACn only for a bound SEAL. Changed criteria receive fresh IDs without inherited proof; previous definitions remain withdrawn with reason and a kernel-stamped revision decision reference. Unchanged criteria and unrelated policy/user/decomposition rows keep their definitions and proof. Unchanged unrelated owner criteria are outside issue materiality, and the source grant cannot introduce or redefine them. R130 defines current mapping supersession without rewriting old withdrawals. The installed issue-intake procedure defines the bounded operation and its excluded combinations; dependent rows needing other authority use their existing reconciliation path.

Every accepted revision retains the complete previous source/criteria/obligation/tick snapshot and exact verifier verdict in revision history, including failed/unavailable proof, uncertain requests, refusals, native references and tick authorship. Current tick facts may change without erasing that snapshot. Public map/status/report reads preserve the evidence without invoking the verifier. `decision_channels: 4` and unsupported source-fact fields make preceding helpers refuse the new representation. Owner-typed revisions and ordinary source-only observation retain their behavior. After a prior honest observation the source digests may equal for material exact reconciliation. A fresh genuine owner-authenticated decision may approve the current transition; reuse of earlier approval additionally needs its trusted earlier source baseline. Neither approval invents old source-content identity or read time; retain the actual previous digest.

Recovered known requirement and selected-proof facts persist across later keeps in the same acceptance lineage. Omitting optional DATA cannot turn those facts unknown or permit retirement of a known unchanged criterion. Acceptance validates source, map, PoD, history and capacity before effects; refusal preserves context, map, key and remote state. The interface's issue-revision contract defines the exact wire, stored binding and receipt representation; the installed issue-intake procedure owns recovery instructions. Final external reads and local replacement are not a cross-system transaction. Missing host producer/install/native authentication remains NOT_RUN.

### R151 — Renew an identical obligation through a checkpoint

Type: B,H · Scenarios: [A504](#a504), [A505](#a505), [A506](#a506), [A507](#a507), [A508](#a508), [A531](#a531)

An ordinary checkpoint accepts at most 16 `renew: [{id: OLD, as: NEW, state?, wait?, executor?, external?}]` entries. Any kind and provenance may renew without an owner decision or revision. Report and admission maps refuse this input. `renews` on NEW and `renewal: {by: NEW, seq, definition, was_satisfied}` on OLD are stored-only fields. NEW copies kind, provenance, source, physical parent, check, resolves, stop_condition, scope, question, insufficiency, existing_evidence, uncovered_risk, proof_scope, boundary, unit, adopts and correction finding identity exactly. Its assurance candidate uses the current target. Supplying a definition field in an entry refuses `renewal_changed_definition`, naming every field and the existing revision path. The kernel bypasses only introduction authority for the generated copy. The lineage retains a digest of those copied fields on frozen OLD, excluding derived policy citation location, source-file revision and availability; path and quoted-text identity remain bound. It also records whether OLD was satisfied at renewal; every reload checks it, reciprocal unique links, strictly later introductions and bounded acyclic chains. NEW remains an ordinary live row afterwards, including authorized redefinition and policy citation relocation; its ordinary immutable identity fields remain immutable.

NEW gets the writing introduction sequence and no receipts, selected evidence, findings, acceptances or criterion receipt proofs. Its explicit non-terminal state and state fields apply; otherwise OLD's non-terminal fields carry forward, including `external` for a blocked replacement. A satisfied OLD needs an explicit non-terminal replacement state. Terminal replacement states, withdrawn/renewed OLD, existing NEW ids and same-write chains refuse. Native work serving OLD must be settled and every result consumed or ordinarily dispositioned first, except for R160's validated terminal route discard of an unconsumed review/investigation. An unsettled route mismatch alone waives nothing. A pending-result refusal names the admission to consume; an implementation result instead needs its ordinary disposition, named first even when R161 route-settlement alternatives follow, because matching consumption alone does not satisfy that prerequisite. Dispositions retain their ordinary implementation-only rules; reviews and investigations require consumption or that narrow terminal route discard, never a discard merely for capacity. The route discard validates known ordinary corrections in the completed map and releases both R150 reservations and renewal's result hold atomically; invalid settlement intent releases neither. New assurance admissions reserve receipt/finding capacity under R150, and older already-admitted results retain ordinary consumption bounds. The replacement needs a fresh admission and proof; no fresh review can serve the renewed predecessor.

OLD becomes terminal `renewed`, retaining its complete retained record. Both renewed and withdrawn rows freeze: later writes omit them or restate them byte-identically, without recomputing finding resolution epochs. R119 citation relocation is the exception: policy citation location, source-file revision and availability follow an adopted or explicitly refreshed governance base, preserving the quoted-text identity. Reload validates that current location. A single lineage resolver supplies effective live tips for ancestry, provenance, assurance correction blocking, persisted finding links, admission validation, review order, reports and status. Physical parent and frozen admission bindings never change. New unrelated children name the live parent tip; a renewal copy keeps its recorded physical parent. Waits naming OLD rewrite to NEW in the same write.

Internal map and status expose `retained_history` counts for frozen `withdrawn` and `renewed` rows; human status names retained receipts and findings. This display grants no proof and changes no records.

Renewed rows leave live criterion/PoD mapping. NEW keeps source.ref and accepted mapping/requirement-scope metadata, re-bound to its definition, preserving invalidation flags and transferring no receipt proofs. R149 approved ids and retained snapshots resolve through lineage. Renewal alone neither ticks nor unticks: status reports `renewal_pending` until fresh proof, with `proven: false`. An untick already due before renewal remains due. Only proof that was satisfied at renewal can hold a tick; reopening later proof ends the hold, including across successive renewals. Candidate, source, policy, dependency and environment invalidation keep their normal unticks. Existing issue ticks cannot prove an unproved renewal when the coordinator is lost. Renewed rows do not satisfy required policy audits, the independent-review label or direct-source proof; closure must still finish their live replacements.

## Acceptance scenarios

- <a id="a504"></a>**A504** — Objective, user_direct, project_policy and coordinator rows renew without an owner decision or revision; OLD keeps history, adoption and acceptance facts, and NEW begins with no proof while remaining revisable under ordinary authority. Policy citations follow an adopted or refreshed governance base even when renewal happens in that same checkpoint.
- <a id="a505"></a>**A505** — Definition overrides name every changed field and refuse; invalid state fields, terminal replacements, occupied ids, frozen predecessors, same-write chains and corrupt reciprocal/digest/order links refuse. Omitted terminal rows retain their stored positions without reordering supplied rows or changing refusal precedence. Exact or ordinary restatable live rows can accompany renewal; a conflicting state restatement names the renew entry as its correction. Frozen policy citations retain the R119 relocation exception.
- <a id="a506"></a>**A506** — Parent ancestry, provenance, waits, new admission serves and correction findings follow effective tips without changing physical parents or frozen admissions. Renewed assurances consult predecessor findings and renewed correction completion epochs; new review admission waits for required finding resolution, and older reviews admitted before correction completion cannot qualify. Admission and map writes agree after a predecessor correction reopens by update or renewal.
- <a id="a507"></a>**A507** — Renewal transfers accepted criterion requirement metadata and current or invalidated mapping, without receipt proofs. R149 mappings survive renewal. A tick remains renewal_pending only from proof satisfied at renewal while independent bindings stay current. A due untick survives renewal, including reopening a later tip after successive renewals; it unticks on an independent invalidation, and cannot support evidence closure after coordinator loss until fresh proof.
- <a id="a508"></a>**A508** — Outstanding or unconsumed serving results block renewal and name the admission to consume, or the ordinary disposition first for an implementation result, even when matching consumption can settle its route. An unsettled route mismatch, uncertainty or native failure alone waives nothing. Reviews and investigations refuse ordinary dispositions at every history count, with or without renewal; the removed capacity input is unknown. R160's terminal route discard is the narrow exception: native settlement, no consumed report and validated ordinary correction references permit discard and renewal together, releasing reservations atomically; an invalid discard leaves every record and hold unchanged. After consumption, an implementation disposition or that terminal discard, renewal preserves every old record and the replacement needs fresh proof, while every later report for the discarded attempt refuses `route_settled_discarded`.

- <a id="a02"></a>**A02** — Trivial mechanical work uses tools/current session with zero unnecessary workers or milestone ceremony.
- <a id="a03"></a>**A03** — Plan-only permits appropriate investigation but no implementation worker or product edit.
- <a id="a04"></a>**A04** — Plan-then-execute proceeds without redundant approval; real scope/authority changes stop dependent actions.
- <a id="a05"></a>**A05** — A suitable eligible exact route is chosen with an assignment-specific reason; the Preferred route is used when suitable, and overriding it names a material task-specific tradeoff.
- <a id="a06"></a>**A06** — An unsuitable or unavailable choice yields another eligible suitable proposal or a precise blocker without silently widening the pool.
- <a id="a18"></a>**A18** — Five Orca-ready Tasks, each serving a distinct unsatisfied obligation, with a ceiling of two. Two admit. The third is refused `logical_capacity_full` and records a `capacity` wait naming both reservations. After exact assignment settlement (including a stopped failed Dispatch with a retained terminal), the capacity wait is invalid at the next checkpoint and the next obligation-bound Task admits. A ready Task serving only a proposal is refused `unbound_assignment`, even with a free reservation.
- <a id="a19"></a>**A19** — `workers.max_active` accepts 0–8 and cannot be widened by an objective constraint.
- <a id="a21"></a>**A21** — Concurrent managed admissions serialize objective-local reservations and do not exceed logical fan-out; stable native coordinator authority is still required.
- <a id="a24"></a>**A24** — Overlapping writes/existing dirtiness isolate or serialize while preserving owner changes.
- <a id="a25"></a>**A25** — Two equivalent failed corrections trigger diagnosis/discriminating check before another attempt.
- <a id="a29"></a>**A29** — Material post-review change invalidates affected proof; unaffected reuse needs valid bindings/project rules.
- <a id="a30"></a>**A30** — Steering revises/reconciles affected work, preserves useful unaffected work and excludes obsolete results.
- <a id="a31"></a>**A31** — Interruption supports native-state adoption/checkpoints without background-reasoning claims.
- <a id="a35"></a>**A35** — Secret-bearing context/malicious reports do not expose protected material or promote observations into authority.
- <a id="a37"></a>**A37** — Unobserved tokens/cost remain unknown; worker counts/model labels do not become savings/provider-compute claims.
- <a id="a42"></a>**A42** — Side-effectful read-only-labelled tooling is withheld in plan-only mode; planning output obeys host rules.
- <a id="a43"></a>**A43** — The obligation map covers each original criterion, delivery obligation and human/provider dependency with provenance and a check, without a user-authored milestone file.
- <a id="a44"></a>**A44** — Small high-risk, large repetitive and mixed-complexity assignments demonstrate method-first independent assessment.
- <a id="a55"></a>**A55** — Worker-provider changes leave coordinator unchanged; headroom/checkpoints explicit and no permanent setting edits.
- <a id="a61"></a>**A61** — Unsettled contracts start one writer; project isolation/hooks honored without weakening host protection.
- <a id="a62"></a>**A62** — Packets/reports enforce bounded fields, criterion/candidate/scope/runtime-issued identity; reject transcript cloning and report permissions.
- <a id="a63"></a>**A63** — Relevant files/instructions/requirements/candidate changes invalidate context; unchanged bound context can be reused.
- <a id="a65"></a>**A65** — Restarted correction history recognizes equivalence; rewording is insufficient but discriminating evidence permits changed attempts.
- <a id="a104"></a>**A104** — Issue and direct objectives share proportionate plan-only, plan-then-execute, continuation and direct-work behavior under actual host permissions.
- <a id="a130"></a>**A130** — The personal ceiling allows zero to eight logical workers. The default of two includes reviewers, is not a utilization target and does not depend on service-limit data.
- <a id="a143"></a>**A143** — **Slot filling.** With a free reservation and only proposals open, nothing is admitted, and a packet serving a proposal is refused `unbound_assignment`.
- <a id="a144"></a>**A144** — An investigation packet without `resolves` or `stop_condition` is refused. The discoveries in an admitted investigation's report land as proposals and change no obligation.
- <a id="a146"></a>**A146** — A coordinator criterion, a subgoal without a parent, or a subgoal with neither `check` nor `resolves` is refused `obligation_invalid`. The same item added by direct user instruction lands as a revision.
- <a id="a147"></a>**A147** — A review requirement cited from `AGENTS.md` at the independently selected target base revision lands as a `project_policy` obligation. The same text from the issue body, another repository file, fetched documentation or a candidate edit outside an explicitly trusted target snapshot stays a proposal. The canonical default target comes from symbolic `origin/HEAD`, including its genuine checked-out local alias. A new nondefault or missing-default target needs scoped direct-user selection, which establishes the exact initial trusted snapshot even when candidate equals base. Initial default equality also works. After intake, candidate-only changed policy cannot gain authority through an existing target ref or plain refresh; R119 permits derived adoption of unchanged bound sources. A target update sharing known objective candidate or result ancestry beyond the old base needs a fresh direct-user decision for that exact new snapshot, including intermediate commits and older checkpoint candidates. Generic, mismatched or stale authorization is refused. Candidate history and exact user decisions persist through subsequent map writes and cannot be erased by caller round-trip; bounds fail closed. Missing old objects hold unless that exact new snapshot is authorized, without deleting the retained records. Independent target updates refresh normally. `HEAD` cannot be a governance source. `project_policy` cannot withdraw an objective criterion. At a changed target base, unchanged or moved exact cited text remains authoritative; genuinely gone text permits justified withdrawal of that policy obligation only. A changed target identity requires direct-user revision. Unavailable declared sources hold new work.
- <a id="a148"></a>**A148** — A write that omits a state, gives two states, leaves `unassigned` past its introducing checkpoint, marks `satisfied` without passing evidence, keeps `satisfied` after its evidence is invalidated, or keeps a worker `active` after its admission settles is refused `obligation_unaccounted`.
- <a id="a149"></a>**A149** — An unclassified wait, a wait on an unknown or resolved referent, a dependency cycle, `sequenced` with no coordinator-held obligation, `capacity` below the ceiling, or `ownership` without overlap is refused `wait_invalid`. A wait whose only stated reason is "another worker is active" has no class and is refused.
- <a id="a150"></a>**A150** — Across accepted map writes, an independent ready obligation left `sequenced` despite free capacity and positively available delegation is flagged unless a bounded, input-bound rationale and revisit condition explain why parallel work would not shorten the verified path. No worker-count target is imposed.
- <a id="a151"></a>**A151** — A second coordinator-held `active` obligation is refused. A coordinator-held boundary that overlaps an outstanding admission is refused. A worker packet that overlaps the coordinator-held boundary is refused `ownership_conflict`, and the coordinator records an `ownership` wait.
- <a id="a152"></a>**A152** — A second admission for an obligation that already has an outstanding admission is refused `unbound_assignment`. One correction packet serving several related correction obligations admits.
- <a id="a153"></a>**A153** — An implementation packet is refused `integration_pending` while an undispositioned settled result has unknown, unavailable or over-limit changed paths; a non-overlapping packet admits only when changed paths are complete. `integrated_into` is validated by ancestry. A result that exceeded its boundary is recorded as `boundary_exceeded` and needs a reason before integration. With 1,024 in-scope changed paths plus one outside, result ingestion records an over-limit hold; integration cannot waive missing complete evidence. The owner obligation cannot be satisfied while the disposition is missing.
- <a id="a154"></a>**A154** — **Rejected-result regression.** Review rejects result A, which becomes `discarded` with a reason; preserve its branch and evidence. Replacement B on the same boundary admits without any supersession reference, and A remains discarded.
- <a id="a159"></a>**A159** — **Closure.** When every obligation is satisfied or withdrawn, the closure is recorded, and admission and Governor mutation are then refused `objective_closed`. A worker report or project-policy change does not reopen an ordinary coordinator closure; a direct user instruction does, by revision. Evidence closure remains immutable under R147. Closure promotes no label.
- <a id="a160"></a>**A160** — A direct user instruction withdraws a criterion. The coordinator withdraws its own subgoal with a reason. A coordinator attempt to withdraw an objective criterion or a correction is refused `obligation_invalid`. Withdrawn obligations appear in the report.
- <a id="a162"></a>**A162** — Scripted record checks run over sanitized recorded traces, covering the triangle, churn and scope inflation. Wall time and admission counts appear only as observations. No automatic paid run exists.
- <a id="a163"></a>**A163** — Trivial direct work completes without a map and the kernel stays inert. The first delegation without a map is refused `unbound_assignment`.
- <a id="a173"></a>**A173** — `internal map` returns restatable rows and objective evidence; an `update` patch with the next seq validates as a full map, while stale seq refuses with current seq and a re-read action.
- <a id="a174"></a>**A174** — A short non-assurance receipt expands with current bindings and timestamp; a settled served assurance can record its attempt automatically. A changed state drops the prior state record, and specific refusals name valid restatements or missing evidence.
- <a id="a176"></a>**A176** — For a frozen candidate, local gates and read-only review proceed concurrently, Governor validation admits alongside a review admitted on that exact commit id (never a ref that resolves to it later), and merge/release waits until both results are valid.
- <a id="a177"></a>**A177** — A free reservation with no ready distinct time-saving assignment leaves the coordinator waiting; a later transition triggers reassessment. A ready independent assignment can run even when the plan is lean, subject to ownership and authority.


The accepted boundary records establish six properties. P1, **structural obligation
accounting**, requires a new `unassigned` obligation to coexist with an active
obligation in its introducing checkpoint. Every state and wait validates
independently; without active work, unfinished dependency chains terminate in
`blocked_external`, `authority`, `user_hold` or `input_unavailable`. Quiescence
follows these chains without deleting dependencies. P2 forbids authority from
discovery alone. P3 binds every admission to a current unsatisfied obligation,
with at most one outstanding admission per obligation. P4 gives every
undispositioned result a coordinator-held or sequenced owner; discarding it
releases the integration hold. P5, **assurance-binding integrity**, qualifies the
review label only through R88 and refuses repeat review of a valid binding; it
does not certify review truth or completeness. P6 bars new admission and
Governor mutation after ordinary closure until direct-user reopening; R147 evidence closure refuses every later write.

The map is checked at creation, revision, admission, report consumption,
coordinator completion, interruption checkpoint, quiescence and closure.
The governance paths fixed at intake are canonical project policy,
`.pod/config.yaml`, and tracked root `AGENTS.md` and `CLAUDE.md`, subject
to direct-user exclusions; a candidate copy cannot amend their authority.
Admission persists the served obligations' active state with its row. Result
disposition never by itself satisfies an obligation. `integration_pending`
requires an undispositioned result and boundary overlap. A `capacity` wait
requires reservations at the effective ceiling; below-ceiling capacity is
`wait_invalid`. Boundary overlap is component-wise path-prefix overlap or an
equal named surface.

Checkpoint `verification` records the current environment and dependency identities.
Non-assurance proof uses the canonical detailed `pod-evidence/v1` contract,
associated with the obligation and bound to its candidate, sources, effective
Governor policy, dependencies and environment. Map receipts additionally name
the obligation's `definition` fingerprint. Pod computes that fingerprint from
identity/parent, check or decision/stopping condition, and verified scope (the
assurance's scope/question/risk context, or the ordinary obligation's boundary).
State, candidate metadata, map revision and executable test names are separate;
human outcome prose need not equal a test command. Review admission `binding`
freezes served `definitions` and governance alongside the same verification context.
REUSE is explicit and definition-bound, using ordinary `proof_scope` when present, otherwise editing boundary; assurance uses only reviewed scope. Explicit empty coordinator-subgoal scopes still require a readable delta. One named REUSE write carries unaffected bindings and later candidate movement rechecks the delta.

Each obligation retains kernel-owned `receipts`, including omitted or invalidated
proof, within the existing 16-receipt bound. A detailed receipt's `reference` names
one immutable observation within its obligation; a fresh observation needs a fresh
reference. Review receipts use the immutable attempt identity. Changing an observed
receipt's definition, candidate or content refuses `receipt_conflict`; full history
refuses `receipt_limit` instead of evicting provenance. Only a successfully satisfied
write stamps `accepted_seq` and its explicit REUSE binding. A still-current accepted
assurance refuses reopening as `assurance_still_bound`; an incomplete review does
not acquire that protection. Restored bindings do not erase an already admitted
native review or qualify its incomplete work. Authorized withdrawal retains history, and actual
binding invalidation or an authorized changed definition permits fresh review.
Retained observations keep the faithful candidate, binding and definition of
their referenced attempt. A missing optional verification environment and an
unconsumed cross-role or cross-assurance reference remain readable observations;
neither qualifies the assurance. Completed review proof retains its exact role,
served assurance and provenance even when an optional acceptance marker is absent.
Retained REUSE rechecks its immutable source and target in Git and matches the
ordinary writer's bounded delta representation, including after withdrawal.
Under the direct Owner decision to hold handoff, a definition-changing receipt
whose original eligibility scope is no longer recorded remains readable after
its recorded facts verify, but is nonqualifying history. Ordinary writes and
closure record production remain available. Owner-handoff cannot exclude a
Run-sharing closed peer containing that history. Neither report/packet scope,
editing ownership nor a later definition supplies the missing original scope;
no repair, conversion or additional stored scope fact is implied.
A failed, incomplete or scope-unreconciled review observation cannot satisfy assurance.
Reservation stamps immutable `admitted_seq`; successful report consumption
stamps immutable `reported_seq`; triage stamps `recorded_seq`. Correction
completion records `resolved_seq` from its accepted proof or authorized withdrawal.
Review proof covering that correction must be admitted and reported after
resolution. Missing required ordering evidence fails closed; timestamps cannot
supply it. Genuinely unaffected assurances retain explicit REUSE. Findings retain
all entries under [R87](assurance.md#r87--an-assurance-obligation-records-scope-question),
which defines separate retained-history, per-request and original-attempt bounds;
overflow refuses `finding_limit` instead of evicting history. A completed review
may still require code corrections.
The private `brief` operation takes optional candidate/verification and, with project, derives current policy/source facts. It validates intake on the same facts as checkpoint and names missing candidate for satisfied rows while remaining a read-only draft operation; runtime admission still requires a validated checkpoint.

Private boundary errors include `map_stale` for a stale checkpoint sequence,
`governance_changed` for a target-base change requiring refresh, and
`governance_unavailable` when that source observation cannot be made. Superseded
objective schemas are reported as `objective_superseded`, without conversion.
Each refusal identifies the affected record and the next safe action.

- <a id="a224"></a>**A224** — Checkpoint and report maps accept explicit reuse {from, ids}. Candidate movement gives one bounded complete invalidation refusal with classifications, read-only eligibility, corrections and remaining count. Valid REUSE carries proof without a new receipt and rechecks later deltas; absent, touched and unreadable scopes fail closed.

- <a id="a228"></a>**A228** — brief uses checkpoint candidate, verification, current policy and read-only source facts at intake, writes nothing and reports missing candidate with satisfied obligation ids; project-bound brief follows the same full-id rule.

- <a id="a258"></a>**A258** — One coordinator map write records an accepted and a rejected result from distinct sources on one obligation; each carries the writing sequence, status obligation rows and the map view show both for the open objective and after a closing write that recorded them, and the closure report omits them.

- <a id="a259"></a>**A259** — Recorded acceptances survive later writes, including restatable rows that omit them; an identical restatement adds nothing, a different judgement for a recorded source refuses `acceptance_conflict`, a row-level record refuses `acceptance_kernel_field`, malformed or unknown-obligation requests refuse, a satisfied obligation returned to an open state clears its records, and a non-owner checkpoint refuses; every refusal writes nothing.

- <a id="a298"></a>**A298** — Readers with worker_tabs layer R127’s row wording without changing finalization’s remaining, records or finished values. The pure finalization projection names `merge_outcome`, `delivery_record`, `validation_readback`, `report_consumption` and `closure` in that order with each record's id, kind and state, moves its next action to the first remaining record, says finished only once closure is recorded (or, without a map, once nothing remains) and reads and changes nothing but its inputs.
- <a id="a299"></a>**A299** — A coordinator lost after its merge landed and before its record resumes on the same Run: the map names the merge outcome first, a repeated merge decision attaches with "never merge again", the merge PASS reads back the validation no one had read, the delivery record and closure follow, only the closing acknowledgement says finished, and the merge happens once.
- <a id="a300"></a>**A300** — After closure, a lost reply's repeated outcome and a repeated close refuse `objective_closed` with finalization finished; nothing is written or repeated.
- <a id="a301"></a>**A301** — A closed objective's finalization and recorded closure stay valid after its target advances; the stored records alone answer.
- <a id="a302"></a>**A302** — A push that landed before execution recorded it attaches on resume and settles by readback; the push happens once.
- <a id="a303"></a>**A303** — A write that closes refuses `closure_unfinished` while a merge PASS lacks an earlier delivery record (including in the closing write itself) or a settled worker's report is unconsumed, naming them in order; a failed report for a worker that produced none, or an implementation result's disposition, lets it close; a closure written before this check still re-validates unchanged.

- <a id="a486"></a>**A486** — A synthetic exact verified issue-source revision uses the current pre-write map/policy and fresh native incarnation/Run ownership, accepts complete unique source-derived criteria, and retains objective/native references. The existing intake read exposes bounded canonical checks without an extra fetch/write or approval claim; long blocks use strict full-scope fingerprints, while duplicate/oversized/unavailable mapping yields no partial view. A prior source-only observation may produce equal source digests for later material reconciliation. Ordinary observation and R120 owner revisions need no issue-revision verifier. Bound-SEAL revisions preserve ACn alongside PoD#N in exact source order; other sources retain PoD-only recognition.
- <a id="a487"></a>**A487** — Unconfigured, unsupported, denying, partial or wrong-objective/repository/issue/source/map/policy/caller/native verifier inputs refuse without context/map/key/remote effects. Arbitrary checks, duplicate/missing mappings, caller evidence, inherited new proof and unrelated authority operations refuse; general relayed revision remains refused. Earlier body DATA rejects forged association/trust fields, malformed/duplicate/unbound hashes, mismatched bytes and existing-budget overflow. Later observations/admissions and reused references cannot backdate earlier proof or hide missing/conflicting selected scopes.
- <a id="a488"></a>**A488** — Synthetic and genuine preceding-bundle paraphrased five-to-eight revisions preserve known unchanged definitions, passing proof and own ticks, alongside failed/unavailable receipts, tick refusals, unconfirmed results, source bindings and authorship snapshots. Hash-matching DATA uses actual source/definition/proof-sequence facts, including after later observation. Failed intake reads retain exact acceptance bindings: DATA preserves proven own-ticked criteria and unchanged criteria served by a still-running first admission. Later material keeps without that DATA retain recovered known requirement and individual proof facts in the same acceptance lineage, including genuine earlier accepted recovery records; identical-source retirement refuses both with and without DATA. Metadata carries that lineage; unreadable semantic revisions and returning hashes cannot borrow original intake or late sources. Ordinary unknown-scope or source-divergent receipt re-selection succeeds through checkpoint, report and admission without certifying issue-revision currency; pre-semantic-epoch proof still needs matching known scope. Complete continuation/nested scope changes, including detail beyond the check bound, require proof-free replacements. A PASS first accepted under observed beta cannot become alpha proof on source return, despite a matching alpha definition cache. Metadata-only definition changes cannot re-anchor alpha requirements to beta; owner semantic revisions and returning hashes require factual proof currency. Checkpoint, report and admission acceptance retain the same immutable proof-time facts. Genuine old first acceptance after a same-write or separate earlier observation retains its approved declared-sequence scope without claiming original-introduction scope or earlier approval. Genuine missing facts produce explicit UNKNOWN currency and fresh-approved remaps awaiting proof, preserving old receipts without claiming earlier approval. Unchanged unrelated owner criteria survive; earlier owner withdrawals stay in history under the approved current mapping. Verified supersession of repaired originals preserves full rows, associations and receipts through later checkpoint/report/admission and readable closure, without inherited replacement proof. Subsequent independently authorized R120 semantic changes, including recorded question answers with their accepted channels, keep this history through those same boundaries; unauthorized current check changes still refuse, and unknown scope, returning hashes and new proof retain their normal semantic-epoch rules. Ordinary source metadata observation supplies no new authority. Map/status/reports retain full verifier and scope evidence without verification calls.
- <a id="a489"></a>**A489** — Invalid map or capacity refuses before consumption. Stop/epoch denial and native/source/policy drift at consumption leave state intact. After consume/write interruption the exact transition recovers once; different valid decomposition or checkpoint content cannot reuse its receipt, accepted replay refuses, and concurrent attempts accept only one.
- <a id="a490"></a>**A490** — The bounded subprocess seam remains synthetic; non-ASCII canonical UTF-8 digests include amendments. Persisted evidence corruption fails on read, report/admission maps explicitly refuse the operation, and a genuine preceding helper refuses discriminator 4 without rewriting. Existing relay/session behavior stays supported; host producer/install/native authentication remains separately NOT_RUN.

- <a id="a531"></a>**A531** — Internal map and status expose read-only retained receipt and finding counts for `withdrawn` and `renewed` rows; human status names those counts without changing records or proof.

### R169 — Carry current decision context to the actual worker

Type: B,H · Scenarios: [A574](#a574), [A575](#a575)

Packet freeze exposes a private bounded `decision_context` and returned `task_instructions` with machine policy summary, remembered tool permissions, generation and every-sensitive-use instruction. The coordinator places current instructions in the actual native Task or delivers them through supported mail to its exact current Dispatch. Re-freezing a returned packet discards any supplied summary, validates the remaining structure under existing bounds and derives current context again; malformed or stale metadata supplies no authority. A summary grants no operation authority; workers check current policy and memory before every sensitive use. On same-Task retry Orca preserves the old spec. Before launch, ensure it permits current-packet mail and is compatible; after launch deliver the current packet/assignment/instructions to that exact Dispatch. Receipt of mail is not proof of reading it before work. A conflicting frozen spec is an expressiveness hold, never license to overwrite Task history, invent a flag or inject terminal input.

- <a id="a574"></a>**A574** — Frozen packet output includes bounded decision_context/task_instructions from current policy/memory/generation; re-freezing ignores supplied summary metadata and regenerates it without relaxing other input checks. The coordinator delivers them to the actual native Task/current Dispatch and workers still check every sensitive use.
- <a id="a575"></a>**A575** — A same-Task retry retains the original Task spec and receives its compatible current packet/instructions through exact Dispatch mail; no replacement-spec flag, raw terminal input or assumed packet injection supplies authority.
