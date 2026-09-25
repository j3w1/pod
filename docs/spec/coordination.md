# Coordination

## Requirements

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

Keep the coordinator conversation, model, effort and host settings unchanged. Preserve useful integration, verification and reporting headroom; checkpoint before foreseeable interruption. Worker selection changes only workers.

### R26 — workers.max_active bounds concurrently reserved logical assignments

Type: H · Scenarios: [A18](#a18), [A19](#a19), [A130](#a130), [A143](#a143), [A177](#a177)

`workers.max_active` bounds concurrently reserved logical assignments per objective. It is personal, 0–8, default two, and a direct constraint may narrow it. Investigators, reviewers and authorized descendants count. Zero workers is valid. The ceiling is never a utilization target: dispatch only ready distinct work that saves time toward a verified result. A free reservation admits nothing by itself, and a capacity wait is valid only at the ceiling. Native settlement frees a slot even when a terminal is retained; never infer physical occupancy.

### R27 — Only the coordinator delegates unless direct

Type: H · Scenarios: [A16](models.md#a16), [A19](#a19), [A20](orca.md#a20), [A21](#a21)

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

Steering creates a new revision, reconciles affected assignments at safe native boundaries, preserves useful unaffected work and prevents obsolete reports/proof satisfying revised work, including changed definitions on stable obligation ids. Unrelated steering preserves unaffected definition bindings. Material acceptance changes require explicit authorization.

### R65 — Normal invocation plans proportionately and proceeds

Type: B,H · Scenarios: [A104](#a104), [A158](assurance.md#a158)

Normal invocation plans proportionately and proceeds within authorization. Plan-only remains read-only under actual host Plan Mode. Plan-then-execute needs no ceremonial approval. Continuation reuses reconciled objective and native state, and continuing a quiescent objective re-evaluates its external waits without a reopen. Small direct work needs no issue, worktree ceremony or worker.

### R81 — An obligation has an id, introducing

Type: H · Scenarios: [A146](#a146), [A147](#a147), [A160](#a160)

An obligation has an id, introducing sequence, kind (`criterion`, `delivery`, `assurance`, `correction`, `steer`, `subgoal`), provenance (`objective`, `user_direct`, `project_policy`, `coordinator`), `check` or `resolves` with `stop_condition`, boundary and source. Subgoals, corrections and coordinator assurance name a parent. Objective obligations enter at intake; direct user instruction may introduce or withdraw any kind. Coordinator judgment may introduce only subgoals, assurance and corrections and withdraw only its own subgoals and assurance with a reason. Policy obligations cite a line range in declared governance sources fixed at intake: the selected target-branch base commit, source byte revision and cited text bind authority. Select the default target through Git's symbolic `refs/remotes/origin/HEAD`; a nondefault or missing default needs scoped `user_direct` revision authority. That explicit initial selection establishes the exact trusted target snapshot even when candidate equals base; pre-intake policy authorship is not observable. An untrusted proposed ref grants no authority, and literal `HEAD` is rejected. Bind the canonical target identity and snapshot at intake. Retain checkpoint candidates and explicit snapshot decisions in bounded kernel-owned history. A later target update sharing candidate or result ancestry beyond the old base needs a fresh direct user decision bound to the exact new target snapshot; intermediate and older recorded commits count, and a generic selector or stale decision cannot authorize it. Independent target updates can refresh normally. A target identity change also requires `user_direct` revision. Outside Git there are no governance sources. On base or authority change, atomically reconcile the map before new admission or governed effect, preserving ids and history. Re-read the same sources at the new base; moved exact cited text remains valid. Gone text permits justified withdrawal of that policy's own obligations only, never objective or user obligations. New requirements need citations; unavailable sources hold new work. Invalidate affected packets and evidence, explicitly rebind unaffected evidence. Bind canonical private policy restrictions separately through existing authority. Pending native attempts keep immutable recovery, and policy change never reopens a closed objective. Violations refuse `obligation_invalid`.

### R82 — Worker reports, reviewer advisories, issue or

Type: H · Scenarios: [A144](#a144), [A145](assurance.md#a145), [A147](#a147)

Worker reports, reviewer advisories, issue or repository text, fetched documentation and coordinator caution add only proposals. Proposals change no criterion, gate, acceptance or authorization. A proposal becomes an obligation only through a `user_direct` revision or a `project_policy` citation.

### R83 — While a map exists, each validated

Type: H · Scenarios: [A148](#a148), [A151](#a151)

While a map exists, each validated write gives every obligation exactly one state: `unassigned` (only in its introducing checkpoint), `active` (an outstanding admission that serves it, or the coordinator), `waiting`, `blocked_external` (an external party, need and unblock condition), `satisfied` (currently valid passing evidence, and no served result without a disposition) or `withdrawn`. At most one obligation is coordinator-held `active`, and its boundary does not overlap an outstanding admission. `satisfied` whose evidence is invalidated must be re-stated. A violation is refused `obligation_unaccounted`. Status shows every state with its referent.

### R84 — A wait has exactly one controlling

Type: H · Scenarios: [A149](#a149), [A150](#a150)

A wait has exactly one controlling reason class — `dependency`, `contract_unsettled`, `sequenced`, `ownership`, `capacity`, `integration_pending`, `input_unavailable`, `authority`, `user_hold` — and a referent that is valid for that class. Dependency and contract waits are acyclic. A wait whose referent has resolved is invalid at the next write. Another active worker is not a class. A violation is refused `wait_invalid`.

### R85 — A packet names the obligations it

Type: H · Scenarios: [A143](#a143), [A151](#a151), [A152](#a152)

A packet names the obligations it serves, a role and a declared boundary. Admission is refused `unbound_assignment` when a served obligation is missing, proposed, satisfied, withdrawn, outside the current revision or already served by an outstanding admission; when a review packet does not serve an assurance obligation; or when an investigation lacks `resolves` and `stop_condition`. An implementation packet whose boundary overlaps an outstanding admission or the coordinator-held boundary is refused `ownership_conflict`. Admission sets the served obligations `active` in the same locked write as the admission row.

### R86 — At report consumption, Pod compares a

Type: B,H · Scenarios: [A153](#a153), [A154](#a154)

At report consumption, Pod compares a settled implementation result's complete changed paths from Git with its declared boundary and records `boundary_exceeded`. Unknown, unavailable, or over-limit paths cannot establish non-overlap and hold further implementation until complete evidence or a reasoned disposition; over-limit paths cannot be integrated from incomplete evidence. Each result takes one disposition: `integrated_into` (validated by ancestry when a commit exists, otherwise coordinator attestation; needs a reason if the boundary was exceeded), or `discarded` with a reason, which preserves branch and evidence. Until then its owner obligation is coordinator-held or `sequenced`, and an overlapping implementation admission is refused `integration_pending`. The scoped efficiency exception cannot lift this.

### R89 — An objective is quiescent when nothing

Type: H · Scenarios: [A158](assurance.md#a158), [A159](#a159), [A160](#a160)

An objective is quiescent when nothing is active and every unfinished dependency or contract-wait chain ends in `blocked_external`, `authority`, `user_hold` or `input_unavailable`. It stays open, preserves its dependencies and reports incomplete. It closes when every obligation is satisfied or withdrawn and the closure report is written. After closure, admission and Governor mutation are refused `objective_closed` until a `user_direct` revision reopens it. Closure promotes no label and grants no acceptance.

### R91 — Boundary refusals and properties P1–P6 are

Type: B,I · Scenarios: [A150](#a150), [A162](#a162)

Boundary refusals and properties P1–P6 are deterministic and generative tests. Coordination behaviour is evaluated on recorded, sanitized traces by scripted record checks: the efficiency triangle (slot filling refused; serialization flagged when an independent, dependency-free, non-overlapping obligation stays `sequenced` across two accepted map writes with free capacity and positively available delegation, when parallel work would shorten the verified path and no bounded rationale and revisit condition is bound to current inputs; amplification refused when a review repeats a valid binding), churn flagged at three settled admissions on one obligation without satisfaction, and scope inflation refused. Wall time and admission counts are recorded observations, never gates. There are no automatic paid runs.

### R95 — Bookkeeping interfaces

Type: B,I · Scenarios: [A173](#a173), [A174](#a174)

`internal map` reads the current restatable obligation map, seq, revision, candidate, governance, delivery, closure, outstanding work, settled attempts, undispositioned results, review eligibility and next actions. Checkpoint and report-map writes accept a validated `update` patch with required `seq`; carried-forward core fields avoid restatement. Short non-assurance evidence expands to the canonical receipt, and a settled served assurance report can use its attempt. Refusals identify current seq, revision and a concrete correction. Ref aliases require proven identity.

### R96 — Critical-path coordination

Type: B · Scenarios: [A176](#a176), [A177](#a177)

Optimize time to a verified result, not utilization or worker count. Re-evaluate at transitions; dispatch only ready, distinct, time-saving work. Once a candidate is frozen, run local gates and same-candidate read-only review concurrently when useful; acceptance requires both. Governor validation may overlap that review, while release waits. Waiting is valid; spare capacity creates no duty to dispatch, and lean is never a one-worker rule.

## Acceptance scenarios

- <a id="a02"></a>**A02** — Trivial mechanical work uses tools/current session with zero unnecessary workers or milestone ceremony.
- <a id="a03"></a>**A03** — Plan-only permits appropriate investigation but no implementation worker or product edit.
- <a id="a04"></a>**A04** — Plan-then-execute proceeds without redundant approval; real scope/authority changes stop dependent actions.
- <a id="a05"></a>**A05** — A suitable eligible model is chosen with an assignment-specific reason; Preferred only breaks close suitability ties.
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
- <a id="a147"></a>**A147** — A review requirement cited from `AGENTS.md` at the independently selected target base revision lands as a `project_policy` obligation. The same text from the issue body, another repository file, fetched documentation or a candidate edit outside an explicitly trusted target snapshot stays a proposal. The canonical default target comes from symbolic `origin/HEAD`, including its genuine checked-out local alias. A new nondefault or missing-default target needs scoped direct-user selection, which establishes the exact initial trusted snapshot even when candidate equals base. Initial default equality also works. After intake, candidate-only policy cannot gain authority through equal bytes, an existing target ref or plain refresh. A target update sharing known objective candidate or result ancestry beyond the old base needs a fresh direct-user decision for that exact new snapshot, including intermediate commits and older checkpoint candidates. Generic, mismatched or stale authorization is refused. Candidate history and exact user decisions persist through subsequent map writes and cannot be erased by caller round-trip; bounds fail closed. Missing old objects hold unless that exact new snapshot is authorized, without deleting the retained records. Independent target updates refresh normally. `HEAD` cannot be a governance source. `project_policy` cannot withdraw an objective criterion. At a changed target base, unchanged or moved exact cited text remains authoritative; genuinely gone text permits justified withdrawal of that policy obligation only. A changed target identity requires direct-user revision. Unavailable declared sources hold new work.
- <a id="a148"></a>**A148** — A write that omits a state, gives two states, leaves `unassigned` past its introducing checkpoint, marks `satisfied` without passing evidence, keeps `satisfied` after its evidence is invalidated, or keeps a worker `active` after its admission settles is refused `obligation_unaccounted`.
- <a id="a149"></a>**A149** — An unclassified wait, a wait on an unknown or resolved referent, a dependency cycle, `sequenced` with no coordinator-held obligation, `capacity` below the ceiling, or `ownership` without overlap is refused `wait_invalid`. A wait whose only stated reason is "another worker is active" has no class and is refused.
- <a id="a150"></a>**A150** — Across accepted map writes, an independent ready obligation left `sequenced` despite free capacity and positively available delegation is flagged unless a bounded, input-bound rationale and revisit condition explain why parallel work would not shorten the verified path. No worker-count target is imposed.
- <a id="a151"></a>**A151** — A second coordinator-held `active` obligation is refused. A coordinator-held boundary that overlaps an outstanding admission is refused. A worker packet that overlaps the coordinator-held boundary is refused `ownership_conflict`, and the coordinator records an `ownership` wait.
- <a id="a152"></a>**A152** — A second admission for an obligation that already has an outstanding admission is refused `unbound_assignment`. One correction packet serving several related correction obligations admits.
- <a id="a153"></a>**A153** — An implementation packet is refused `integration_pending` while an undispositioned settled result has unknown, unavailable or over-limit changed paths; a non-overlapping packet admits only when changed paths are complete. `integrated_into` is validated by ancestry. A result that exceeded its boundary is recorded as `boundary_exceeded` and needs a reason before integration. With 1,024 in-scope changed paths plus one outside, result ingestion records an over-limit hold; integration cannot waive missing complete evidence. The owner obligation cannot be satisfied while the disposition is missing.
- <a id="a154"></a>**A154** — **Rejected-result regression.** Review rejects result A, which becomes `discarded` with a reason; preserve its branch and evidence. Replacement B on the same boundary admits without any supersession reference, and A remains discarded.
- <a id="a159"></a>**A159** — **Closure.** When every obligation is satisfied or withdrawn, the closure is recorded, and admission and Governor mutation are then refused `objective_closed`. A worker report or project-policy change does not reopen the objective; a direct user instruction does, by revision. Closure promotes no label.
- <a id="a160"></a>**A160** — A direct user instruction withdraws a criterion. The coordinator withdraws its own subgoal with a reason. A coordinator attempt to withdraw an objective criterion or a correction is refused `obligation_invalid`. Withdrawn obligations appear in the report.
- <a id="a162"></a>**A162** — Scripted record checks run over sanitized recorded traces, covering the triangle, churn and scope inflation. Wall time and admission counts appear only as observations. No automatic paid run exists.
- <a id="a163"></a>**A163** — Trivial direct work completes without a map and the kernel stays inert. The first delegation without a map is refused `unbound_assignment`.
- <a id="a173"></a>**A173** — `internal map` returns restatable rows and objective evidence; an `update` patch with the next seq validates as a full map, while stale seq refuses with current seq and a re-read action.
- <a id="a174"></a>**A174** — A short non-assurance receipt expands with current bindings and timestamp; a settled served assurance can record its attempt automatically. A changed state drops the prior state record, and specific refusals name valid restatements or missing evidence.
- <a id="a176"></a>**A176** — For a frozen candidate, local gates and read-only review proceed concurrently, Governor validation admits alongside that same-candidate review, and merge/release waits until both results are valid.
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
Governor mutation after closure until direct-user reopening.

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
REUSE is explicit and definition-bound, preserving only unaffected bindings.

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
A failed, incomplete or scope-unreconciled review observation cannot satisfy assurance.
Reservation stamps immutable `admitted_seq`; successful report consumption
stamps immutable `reported_seq`; triage stamps `recorded_seq`. Correction
completion records `resolved_seq` from its accepted proof or authorized withdrawal.
Review proof covering that correction must be admitted and reported after
resolution. Missing required ordering evidence fails closed; timestamps cannot
supply it. Genuinely unaffected assurances retain explicit REUSE. Findings retain
all entries within the 32-entry bound and refuse `finding_limit` on overflow
instead of evicting history. A completed review may still require code corrections.
The private `brief` operation returns an obligation map while remaining a read-only
draft operation; runtime admission still requires a validated checkpoint.

Private boundary errors include `map_stale` for a stale checkpoint sequence,
`governance_changed` for a target-base change requiring refresh, and
`governance_unavailable` when that source observation cannot be made. Superseded
objective schemas are reported as `objective_superseded`, without conversion.
Each refusal identifies the affected record and the next safe action.
