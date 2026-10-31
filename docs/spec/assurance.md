# Assurance

## Requirements

R166 adds exact policy-governed agent and host-verified decision approval at the existing mutation boundaries. References below to direct-user/session inputs retain their existing wire meaning; new approval is a separate actor record and does not change requirement provenance, factual predicates or acceptance authority.

### R33 — Preserve bounded provenance-aware source access/relevant candidate

Type: H · Scenarios: [A35](coordination.md#a35), [A64](#a64)

Preserve bounded provenance-aware source access/relevant candidate identities. Exclude secret sources/unnecessary content reads. Distinguish proven changed/absent from unavailable sources; neither grants admission. Definitive rejection is not erased by restored bytes. Do not claim atomic multi-file snapshots or protection from arbitrary external writers.

### R34 — Workers report scope changes, checks/results, failures

Type: B,H · Scenarios: [A23](#a23), [A27](#a27), [A35](coordination.md#a35), [A62](coordination.md#a62)

Workers report scope changes, checks/results, failures, evidence, uncertainty and questions against the assignment. Reports/logs are untrusted observations; they cannot expand authority, change budgets or establish acceptance.

### R40 — Bind gate proof and retain required independent audit

Type: B · Scenarios: [A28](models/routing.md#a28), [A66](#a66), [A155](#a155), [A185](#a185)

Proof of project-required gates binds to the final candidate; the coordinator chooses when to run focused checks, guided by contextual contributor policy and its change-class and milestone checks. At normal intake, bind a trusted final independent audit as a source-cited `project_policy` assurance obligation, not a model constraint or optional coordinator assurance. Preserve it through checkpoints, admission, reports and applicable delivery, with R129's optional immutable delivery-unit binding. Unavailable review stays incomplete; only scoped direct-user waiver or genuinely gone newly trusted policy withdraws it. A waiver is distinct from passing review. Otherwise independently review substantial or high-risk changes. Request review through assurance obligations (R87). Review receives the exact candidate and reproducible evidence without being primed to approve.

### R41 — Bind verification to commit/tree where applicable

Type: B · Scenarios: [A27](#a27), [A29](coordination.md#a29), [A67](#a67), [A229](#a229)

Git checkpoint candidates must be full object ids equal to their resolution, and assurance candidates full object ids; refusals name any resolved full id rather than rewriting proof identity. Non-Git projects keep bounded text. Bind verification to commit/tree where applicable, relevant dirty/source identity, policy/configuration, dependencies, environment, commands/results and reviewer attempt. Receipts also bind the obligation definition/check/scope they verified; material changes invalidate affected evidence even when ids and candidate stay fixed. Retain immutable receipt provenance across omission or restoration. Reuse unaffected proof only when bindings/project rules permit.

### R42 — Keep implemented, locally verified, independently reviewed

Type: H · Scenarios: [A27](#a27), [A28](models/routing.md#a28), [A68](#a68), [A157](#a157)

Keep implemented, locally verified, independently reviewed, hosted proof complete, accepted, merged and deployed/released distinct. `independently reviewed` follows R88. The coordinator assesses the objective under project acceptance authority; worker success and synthetic fixtures cannot promote later labels.

### R43 — Reports state satisfied obligations, blockers, candidate

Type: B · Scenarios: [A37](coordination.md#a37), [A68](#a68), [A145](#a145), [A158](#a158)

Reports state satisfied obligations, blockers, candidate and check evidence, route exceptions, uncertainty, unresolved native references, remaining gates, withdrawn obligations with provenance and reason, triage downgrades, `boundary_exceeded` results and open proposals as out of scope. An interim report for a quiescent objective says incomplete and names each external dependency. Unobserved usage and cost stay unknown.

### R51 — Claim live-verified coordination only with passing

Type: H · Scenarios: [A74](#a74)

Claim live-verified coordination only with passing live evidence for Claude Code and Codex on Linux, each with real Orca delegation through the production adapter. Synthetic, hosted and review checks never substitute for it, and missing live evidence stays NOT_RUN. Unsupported optional capabilities fail conservatively and cannot be marketed as verified.

### R87 — An assurance obligation records scope, question

Type: B,H · Scenarios: [A155](#a155), [A156](#a156), [A225](#a225)

An assurance obligation records scope, question, candidate, existing evidence and insufficiency. An overlapping second obligation on the same candidate names an uncovered risk, including when the first is withdrawn but its accepted proof remains current. A report is consumed only after exact native settlement and is immutable per Dispatch; exact replay is idempotent, while changed report or result evidence needs a fresh attempt. Findings record reviewer severity and triage: `required_correction` creates a parented correction obligation, consolidated on the candidate lineage; `advisory` creates a proposal; a downgraded blocker or major finding needs a reason. Several related findings of one attempt may share one new correction, retaining up to eight ids and highest severity. A related group carries up to eight `findings` members with individual finding/severity; advisory groups declare a common `root_cause`, while required groups name their shared correction. The stored entry preserves ids, highest severity and shared summary, counts once, replays idempotently and cannot regroup stored ids. Each assurance retains at most 64 finding records across distinct reports; one review attempt contributes at most 32, including incremental submissions. Exact replay spends no new slots; neither a request nor an accumulated attempt can supply 33 records. Bounds refuse atomically without truncation or eviction. A 65th retained record names the capacity blocker and the supported next action under R150, using accepted state facts rather than unaccepted correction dependencies; R151 permits a fresh identical row once its serving-result requirements are met, while no write frees a slot in the old history by resolving, withdrawing, regrouping or moving earlier findings. Existing finding content, attempt/source provenance, correction links, resolution order and withdrawal reasons retain their meaning. Earlier readers with the 32-record contract refuse larger finding histories during semantic validation without rewriting them. The obligation is satisfied by a completed, validated review observation bound to the current candidate, or a valid REUSE binding, together with satisfied or user-withdrawn corrections. A still-valid accepted proof relationship cannot be erased or restated to admit repeat review. Recording a required correction from any legitimately admitted completed review invalidates earlier accepted assurance proof, even when the report arrives late or old bindings return. Admission timestamps do not determine that order. Advisory additions cannot evict the correction's safety record; qualification waits for corrections and suitable current review proof. Definition-bound receipt history survives omission and candidate metadata changes. A correction affecting reviewed scope, assumptions, dependencies or governance, or with unknown impact, reopens the obligation for affected-scope delta review. Explicit candidate REUSE binding with the Git delta preserves unaffected evidence; stricter project policy governs.

### R88 — The independently reviewed label requires at

Type: H · Scenarios: [A157](#a157)

The `independently reviewed` label requires at least one live assurance obligation, with every live assurance satisfied by a binding valid for the delivery candidate. Zero or all-terminal assurance withholds the label. When the binding condition is unmet, the report stands, the label is withheld, and `assurance_unbound` names the gap. Stricter project rules still govern.

### R150 — Retain 64 receipts separately from 16 selected evidence items

Type: I,H · Scenarios: [A501](#a501), [A502](#a502), [A503](#a503), [A530](#a530)

Each obligation retains at most 64 immutable receipts (`MAX_RECEIPTS`), while a write selects at most 16 evidence items (`MAX_EVIDENCE`). Contract discovery and diagnostics expose these independently. Criterion proof-scope history in `pod_ticks` follows the 64-receipt bound. A replay of an existing receipt spends no slot. A checkpoint or report that would retain a 65th receipt refuses `receipt_limit`, naming retained, proposed and limit counts, plus the pending admission to consume or, when none is pending, an exact next-sequence R151 `renew` request with a fresh-id placeholder. An assurance admission reserves one receipt and 32 finding records per outstanding unconsumed serving attempt, including itself: retained receipts plus those attempts must fit 64, and retained findings plus 32 times those attempts must fit 64. Otherwise admission refuses before launch with `receipt_limit` or `finding_limit`. If a serving report is pending, the refusal names its admission and directs consumption before retrying; a route-held attempt names the same admission and the project lead's applicable R161 settlement step. It offers the exact renewal request only when nothing is pending. Native settlement keeps the reservation until report consumption, except for R160's validated terminal route discard of an unconsumed review/investigation; deferred or ordinarily dispositioned implementation attempts reserve nothing. An unsettled route mismatch, an unknown effective route, a recoverable error or a native failure without a consumed report otherwise keeps the reservation and renewal hold, because Pod still accepts a later matching report. Terminal route discard preserves known required findings as ordinary corrections, releases reservations and the renewal hold atomically, and prohibits every later report as `route_settled_discarded`; neither lead acceptance alone nor a capacity-only discard supplies that exception. When no other serving report is pending, including when the refused write itself consumes the only one, the refusal gives the exact renewal request. Checkpoint receipts and incremental findings from older consumed reports cannot take another pending attempt's reserved slots; replay spends none. The renewal request uses accepted state facts, never dependencies introduced only by the refused write. It publishes neither context nor chunks. Nothing evicts, rewrites or borrows a receipt; the 512 KiB physical context bound and immutable chunk protections remain.

## Acceptance scenarios

- <a id="a501"></a>**A501** — A genuine 0.21.11 kernel refuses the seventeenth receipt on a 16-receipt row; the current kernel accepts it and preserves all earlier receipt content.
- <a id="a502"></a>**A502** — Checkpoint and earlier report-ingested 64-plus-one receipts refuse atomically with retained/proposed/limit counts and settlement and consumption of another named pending admission, or the next renewal request when no other report is pending; replay spends no receipt slot. New assurance admissions reserve one receipt and 32 findings per pending serving attempt, including themselves, and refuse before launch if either bound would overflow. Post-settlement receipt padding and incremental findings from older consumed reports cannot consume another pending attempt's reservation. Failed outcomes reserve capacity before they are known and retain required findings on consumption; a failed consumption releases its unused receipt. An unsettled route mismatch, an unknown route or a native failure without a consumed report otherwise keeps its receipt/finding reservations and renewal hold. A route-held refusal names the same admission and the lead's applicable R161 step. Validated R160 terminal review/investigation discard releases reservations and renewal atomically while retaining required corrections and withholding review proof; an invalid discard releases nothing and every later report of a discarded attempt refuses. Suggested renewal state follows the accepted coordinator slot even when the refused write tried to free it.
- <a id="a503"></a>**A503** — Contract output keeps receipt history at 64 and selected evidence at 16; per-criterion proof-source history accepts 64, never transfers proofs to a renewal, and physical protections remain effective.

- <a id="a23"></a>**A23** — Native duplicate/delayed messaging remains Orca-owned; Pod report ingestion joins a frozen packet and exact fresh Dispatch identity without a parallel acknowledgment ledger.
- <a id="a27"></a>**A27** — Worker success with failed required checks leaves objective incomplete.
- <a id="a64"></a>**A64** — Changed/absent/redirected sources differ from unavailable reads; definitive rejection survives restoration; unrelated dirty paths do not grant sensitive reads.
- <a id="a66"></a>**A66** — Cheap discriminating checks precede expensive work; independent review receives exact candidate and reproducible unprimed evidence.
- <a id="a67"></a>**A67** — Source/config/dependency/environment/candidate changes invalidate affected proof; no fabricated Git metadata and explicit alternative source binding. Ordinary receipts bind the verified definition; authorized check/scope changes invalidate old proof, and omission/restoration or candidate relabeling cannot refresh it.
- <a id="a68"></a>**A68** — Completion labels/final report follow evidence/project authority; failures, blockers, route exceptions and unresolved native references remain visible.
- <a id="a74"></a>**A74** — Missing live PASS for Claude Code or Codex on Linux, or for either delegation adapter, keeps live verification NOT_RUN despite synthetic CI and a read-only doctor.
- <a id="a145"></a>**A145** — A worker report proposing new owner decisions leaves every obligation unchanged, and the report lists the proposals as out of scope.
- <a id="a155"></a>**A155** — **Amplification.** Assurance candidates in Git are full object ids. A review packet must serve an assurance obligation. A second assurance obligation on an overlapping scope and the same candidate without `uncovered_risk` is refused `obligation_invalid`, including when the first was withdrawn but its accepted proof remains current. A review attempt against a satisfied assurance obligation is refused `unbound_assignment`. Removing a still-valid accepted receipt or changing candidate metadata cannot enable a repeat or hide scope overlap; bounded provenance survives.
- <a id="a156"></a>**A156** - Required findings of one review may share one new correction retaining every id and highest severity; related groups preserve at most eight ids without regrouping and history overflow never evicts. Retained history across distinct reports, including earlier already-admitted attempts, accepts 32 plus one and 63 plus one, preserving every prior field and correction's resolution or withdrawal; 64 plus one refuses atomically. A request or accumulated review attempt cannot carry 33 records, including after reload; exact replay of stored findings spends no new slots. Recording or retrying a failed review never supplies passing proof. A `required_correction` creates a parented correction obligation, and an `advisory` finding becomes a proposal. Downgrading a reviewer's blocker records a reason that the report lists. A legitimately admitted review's required correction remains recordable when its report arrives after a newer attempt's acceptance or an older environment or source binding returns. Earlier proof cannot qualify over that newly recorded correction, and advisory overflow cannot remove that protection. Review order is independent of wall-clock admission timestamps. A correction affecting reviewed scope, assumptions, dependencies or governance, or with unknown impact, re-opens the same assurance obligation for delta review. Unaffected evidence receives an explicit candidate REUSE binding with the Git delta; stricter project policy governs. The coordinator cannot withdraw a correction. Changing a question/check/scope under user authority invalidates old attempt receipts on the same id and candidate; a fresh attempt can qualify, while unrelated steering preserves unaffected proof.
- <a id="a157"></a>**A157** — With an unbound assurance obligation, none at all, or only withdrawn assurances, the report stands, the label is withheld, and `assurance_unbound` names the gap. An unsettled review report cannot be consumed, and a failed report cannot be changed to success on its Dispatch; a fresh settled review attempt may qualify. A valid REUSE binding satisfies the requirement.
- <a id="a158"></a>**A158** — **Quiescence.** When only a non-Owner test identity remains outstanding, as `blocked_external` with the user as party, the interim report says incomplete and names the need and the unblock condition. The objective stays open. After the user supplies the identity, continuation admits the dependent obligation without a reopen.


- <a id="a185"></a>**A185** — Normal brief/intake cites a trusted final audit as project_policy; production checkpoint/report refuse omission, relabeling, unauthorized downgrade or withdrawal. Required review blocks acceptance/closure despite caller review_required=false, and production Governor release-purpose admission holds missing/invalid proof for its unit-bound or unbound required audits (R129) without blocking publication/validation or making optional assurance mandatory. A valid fresh review qualifies; a scoped user waiver is recorded separately and never qualifies as passing review. A147 candidate/history, unavailable/moved/genuinely removed source protections remain.

- <a id="a225"></a>**A225** - Several required findings from one review share one new correction without losing ids or highest severity. Related grouped entries retain at most eight ids and highest severity, count once, replay unchanged and cannot regroup stored ids. Correction-id, 33-entry report and 65-entry retained-history overflow refuse without eviction; the latter names the capacity blocker rather than impossible regrouping, with R151 renewal after consumption or an ordinary implementation disposition. Interrupted report publication and identical retry preserve all findings without duplication. A genuine earlier 32-bound reader preserves histories within that bound and refuses 33 retained records during semantic validation without rewriting. Major/blocker downgrades need reasons and ordering remains unchanged.

- <a id="a229"></a>**A229** — Git checkpoint candidates are full object ids equal to their resolution; assurance candidates are full object ids. Mutable names refuse with the resolved id; non-Git candidates remain bounded text. Genuine 0.7.1 full-id criterion, subgoal, correction and assurance proof survives unchanged-map checkpoint/report, absent proof_scope preserves definition identity, and abbreviated proof invalidates honestly on full-id restatement.

## Verification

The latest stable CPython on Linux, one interpreter per qualification run, one bundle-as-package and standard-library facilities
where practical. Focused tests run during development; the complete unit and
incident suite, explicit incident discovery, compile/whitespace, catalog check,
skill validation, source audit, PTY/subprocess and disposable installed-bundle
checks gate milestones. Hosted Linux CI, live native trials on Codex and Claude
Code, independent candidate-bound review and project acceptance are distinct
facts. Missing live/provider evidence remains `NOT_RUN`.

Live trials use disposable objectives and prove both supported worker adapters:
installation/discovery, same-session coordination, an effective native route,
supervision, request recovery, verification, interruption/adoption and native
Delivery/release ordering. Unsupported optional interactions remain unverified.
No production or provider mutation is authorized by this contract alone.

- <a id="a530"></a>**A530** — A sanitized sixteen-receipt assurance consumes a review already admitted before the reservation upgrade, retaining every earlier receipt and its finding, adding the seventeenth receipt without another admission, discard or borrowed evidence.

### R170 — Bind semantic review roles and keep route evidence honest

Type: B,H · Scenarios: [A576](#a576), [A577](#a577)

Assurances may declare typed `audit_role: reviewer|auditor`, bound into their definition and immutable after designation. New review starts under a composite require all served assurances to have a consistent designation and the packet's explicit matching `routing_role`. Implement/investigate normally means worker, while resolved correction ancestry requires explicit fixer. Coordinator is never a worker-admission role. Titles and free responsibility prose establish none of these facts. Earlier admitted/pending work and packets without a composite retain their supported behavior. Rule approval never supplies review truth, independence, native effective-route evidence or project acceptance; unsupported running-coordinator effective evidence stays UNAVAILABLE.

- <a id="a576"></a>**A576** — Composite review starts require reviewer/auditor audit_role on every served assurance and matching packet routing_role; a correction-serving implementation requires explicit fixer, and designation cannot be changed after binding.
- <a id="a577"></a>**A577** — Running-coordinator effective model/effort without supported actual readback remains UNAVAILABLE; worker effective evidence stays mandatory and synthetic route checks never become live or acceptance proof.

### R171 — Admit verifying reviews after required finding resolution

Type: B,H · Scenarios: [A587](#a587), [A588](#a588), [A589](#a589)

Before reserving a review, check the accepted map's required findings on its assurance, including findings retained through assurance renewal and the effective correction tips. An unresolved required finding refuses `review_precedes_resolution`, naming the findings and corrections and directing the coordinator to consume the fixer's report and satisfy those corrections with proof before admitting the review. The refusal changes no record and starts no worker; an accompanying admission map cannot supply the missing resolution. Advisory findings and corrections already satisfied or validly withdrawn do not block admission. Other admission checks and immutable recovery remain in force.

The same chronology predicate validates evidence and explains invalidation: both the attempt's `admitted_seq` and its receipt's `reported_seq` must strictly follow every required finding's `resolved_seq`, which must be at least its `recorded_seq`. Unknown or malformed sequence evidence cannot qualify. Older receipts without `reported_seq` retain their `accepted_seq` fallback; a missing admission sequence is never fabricated. Backward-readable reviews with premature or unproven chronology still refuse `evidence_invalidated`, classified `ordering` with the finding, correction, resolution, admission and report sequences. Diagnostics name at most eight ordering records and count the rest. Genuine definition, governance and binding changes retain their classifications and precedence. The installed proof guidance owns the correction-cycle procedure.

- <a id="a587"></a>**A587** — A production admission serving an assurance with an unresolved required finding refuses `review_precedes_resolution` without any record change or native start. After consuming the fixer's report and satisfying its correction with proof, the same review packet admits; its consumed report satisfies the assurance. An accompanying admission map cannot bypass accepted resolution, and direct map admission shares the guard. Advisory findings and valid user-withdrawn corrections permit review.
- <a id="a588"></a>**A588** — A backward-readable review admitted before correction resolution refuses at report consumption or later checkpoint satisfaction as `evidence_invalidated` / `ordering`, naming the finding and its resolution, admission and report sequences. A genuine environment change still classifies `binding change` and each refusal preserves records. Both sequences must strictly follow resolution, unknown sequence evidence stays invalid, older accepted-sequence fallback remains supported, and bounded diagnostics count omitted findings.
- <a id="a589"></a>**A589** — Grouped required findings and assurance/correction renewals use the same effective lineage for admission, evidence and diagnostics. Reopening or renewing a predecessor correction holds new review until fresh correction proof; old review proof remains invalid. The five-step correction cycle has one authoritative statement in the installed proof reference.

### R172 — Project hosted suite shares from same-run planned worker time

Type: B,I · Scenarios: [A590](#a590), [A591](#a591), [A592](#a592), [A593](#a593), [A604](#a604)

Hosted suite timing lines record each share's wall seconds and its largest worker estimate from the original checkout plan. Refresh retains those measurements with their plan and commit identity; it never rebuilds the denominator from observed test durations or a refreshed manifest. Each lane projects share wall time as its current largest planned worker multiplied by the maximum measured share ratio, floored at one. A earlier manifest without calibration uses one; earlier timing logs refresh durations while retaining any existing calibration. Verified earlier plan reconstruction is identified separately from direct telemetry.

Hosted CI resolves the latest stable CPython once per run and runs every share, once, on that exact version. Timing lines record the interpreter version, and a refresh requires the whole run to have used one; no interpreter is preferred for calibration and earlier logs without a version keep their own evidence. Refresh validates complete same-run lane shares and timing chunks, consistent identities and metadata, finite timings, passing-test accounting and unique listed test IDs before updating anything. Identical chunk replay is idempotent; conflicting duplicates, missing shares or parts, mixed telemetry, skipped or incomplete execution and unsuccessful worker completion refuse. Earlier logs without worker completion telemetry retain their older evidence limits.

Raw worker estimates above the target remain a hard finding. Calibrated projections above target also require another shard below the parallel cap. At that cap, calibrated-only excess is an explicit warning with projected seconds and residual; it cannot suggest an unauthorized seventh shard. The existing hosted wall failure and owner authority over budgets, targets, timing overrides and the cap remain unchanged. Shard changes update the manifest and hosted matrix together. Detailed contributor procedures live in `docs/validation.md`.

- <a id="a590"></a>**A590** — Same-run share wall time substantially above its original planned worker estimate makes the calibrated check require more shards; equal wall and planned time does not. The log records the original denominator, calibration preserves the samples, the ratio uses the lane maximum with floor one, and changing observed durations cannot rewrite that denominator.
- <a id="a591"></a>**A591** — Refresh refuses incomplete, inconsistent, nonfinite or unsuccessful timing input and duplicate listed test IDs before changing the manifest. Identical chunk replay is harmless; conflicting replay refuses. Incomplete or malformed calibration objects refuse manifest validation.
- <a id="a592"></a>**A592** — Earlier manifests project with ratio one, and complete earlier logs retain existing calibration while refreshing durations. A complete older shard topology can refresh after shard counts increase. Earlier calibration identifies a verified original plan rather than inferring one from observed timings.
- <a id="a593"></a>**A593** — The parallel shard remedy stays within the existing cap and matches hosted matrix rows. At the cap, calibrated-only excess warns with numbers and owner authority, raw worker overload still fails, and the hosted wall-time failure stays effective. Candidate hosted evidence reports each parallel share below target or its numerical residual.
- <a id="a604"></a>**A604** — Hosted CI resolves one latest stable CPython per run and passes the exact version to every job and shard, each test runs once, and a failing resolution fails the required `linux` job. Timing identity records that version for every share and a refresh refuses a run that used more than one; earlier logs keep their own version evidence.

### R173 — Keep review proportional and reuse proof through unrelated change

Type: B · Scenarios: [A605](#a605), [A606](#a606), [A607](#a607), [A608](#a608)

One independent reviewer is the normal starting point for a substantive change; another is added for a concrete independent risk or a missing competence, not by default. A review-round count never blocks admission, grants or withholds permission, or requires a lead or owner answer. Repeated settled attempts on one obligation without satisfaction raise the existing `churn` observation, which status words as a diagnostic: narrow the approach, repair the named defect or get specialist input. It blocks nothing and never accepts a defective candidate because effort was spent.

A new `required_correction` finding names what it blocks in `blocks` (an outcome, requirement, PoD or policy; at most 128 characters), and its summary states why. A new required finding without `blocks` refuses atomically as `finding_unlinked` with a next action: supply the link, or record an advisory with the required downgrade reason; a substantive safety or privacy defect is `policy:safety` or `policy:privacy` and is never downgraded for a missing label. An advisory carries no `blocks`. Stored findings without `blocks` stay valid and replay unchanged. A reader earlier than this contract refuses persisted `blocks` as an unsupported field without rewriting it.

Reuse compares the Git delta between the evidence's own candidate and the current one with the obligation's scope. A change to the root `VERSION` file alone does not touch a scope that merely contains it; a scope that names `VERSION`, and every other path including installation or behavior metadata, still overlaps normally. A reuse proposal in a refusal derives from the candidate the selected evidence was recorded against, never from the caller's `reuse.from`; evidence that spans several candidates or records none proposes no reuse. A `reuse.from` that is not that candidate refuses `evidence_invalidated` naming the predicate `reuse_from_is_evidence_candidate`, the given and recorded candidates and a next action that is valid for the recorded one. An untouched-scope failure names `reuse_scope_untouched`. A refusal never recommends the request it refuses: it directs fresh evidence unless the real source is eligible. An unreadable delta or an undeclared scope stays unknown and is never treated as eligible. The validation policy likewise treats a root `VERSION` number as no milestone by itself: risk, not a version number, selects the gates.

- <a id="a605"></a>**A605** — A new required finding without `blocks` refuses `finding_unlinked` atomically with a next action naming the link, the advisory route and the safety/privacy labels; a linked finding retains `blocks`, including `policy:safety` and `policy:privacy`; an advisory carrying `blocks` refuses; stored findings without it replay unchanged; a genuine earlier reader refuses persisted `blocks` without rewriting.
- <a id="a606"></a>**A606** — A version-only delta keeps REUSE for a repository-wide proof scope and review scope; a scope naming `VERSION`, or a delta that also touches catalog, installer, workflow or any other path in scope, still refuses as touched.
- <a id="a607"></a>**A607** — A wrong `reuse.from` refuses naming the predicate and the evidence's candidate, and the aggregated public refusal keeps that failed predicate and the compared candidates in each obligation's `reuse_failure` through checkpoint and report; the proposal and its next action use that candidate and applying it succeeds; a touched scope proposes fresh evidence, never the same reuse; unreadable deltas, undeclared scopes and evidence with no single candidate remain unknown.
- <a id="a608"></a>**A608** — Six settled review attempts without satisfaction raise the churn diagnostic yet a further review still admits, with no allowance, permission stop or question.

### R174 — Refuse a mistyped verification-gap list

Type: B · Scenarios: [A609](#a609)

A checkpoint's `verification_gaps` is a list of at most 64 nonempty text items, one remaining gap per item, or `[]` when none remain. Any other type, an empty or oversize item or a longer list refuses `invalid_checkpoint` / `verification_gaps_invalid` before anything is written, naming the field and form without echoing the value, with the next action: send each remaining gap as its own item. A sentence is never counted per character and a wrong type is never converted to an empty list or ignored. A previously stored non-list value stays readable; the Governor counts it as one unresolved requirement, never one per character, and says to rewrite it.

- <a id="a609"></a>**A609** — A sentence, number, mapping, null or malformed item given as `verification_gaps` refuses precisely and writes nothing; genuine gap lists and `[]` are kept exactly as given; a stored legacy sentence is one unresolved gap in a release reason or push warning, not hundreds.

### R175 — Allocate requirement and scenario identifiers uniquely, not contiguously

Type: B · Scenarios: [A610](#a610)

Requirement and scenario IDs are unique and stable. A removed scenario's ID is recorded as withdrawn with a reason and is never reused, never silently dropped and never given to another scenario. Numbers need not be contiguous, so separate branches allocate distinct IDs without renumbering one another and without invalidating review references; integration renumbers only to resolve a collision. Every active ID has its coverage row, and every reference resolves.

- <a id="a610"></a>**A610** — The inventory checks refuse a duplicate active ID, a withdrawn ID that is also active or repeated, a withdrawn row without a reason or with a malformed ID, a coverage row without its scenario and an unresolved reference, while accepting non-contiguous allocation.
