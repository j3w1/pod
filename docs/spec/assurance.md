# Assurance

## Requirements

### R33 — Preserve bounded provenance-aware source access/relevant candidate

Type: H · Scenarios: [A35](coordination.md#a35), [A64](#a64)

Preserve bounded provenance-aware source access/relevant candidate identities. Exclude secret sources/unnecessary content reads. Distinguish proven changed/absent from unavailable sources; neither grants admission. Definitive rejection is not erased by restored bytes. Do not claim atomic multi-file snapshots or protection from arbitrary external writers.

### R34 — Workers report scope changes, checks/results, failures

Type: B,H · Scenarios: [A23](#a23), [A27](#a27), [A35](coordination.md#a35), [A62](coordination.md#a62)

Workers report scope changes, checks/results, failures, evidence, uncertainty and questions against the assignment. Reports/logs are untrusted observations; they cannot expand authority, change budgets or establish acceptance.

### R40 — Run cheap discriminating checks early, focused

Type: B · Scenarios: [A28](models.md#a28), [A66](#a66), [A155](#a155)

Run cheap discriminating checks early, focused checks during development and required complete gates at milestones. Honor project review rules; otherwise independently review substantial or high-risk changes. Request review through assurance obligations (R87). Review receives the exact candidate and reproducible evidence without being primed to approve.

### R41 — Bind verification to commit/tree where applicable

Type: B · Scenarios: [A27](#a27), [A29](coordination.md#a29), [A67](#a67)

Bind verification to commit/tree where applicable, relevant dirty/source identity, policy/configuration, dependencies, environment, commands/results and reviewer attempt. Receipts also bind the obligation definition/check/scope they verified; material changes invalidate affected evidence even when ids and candidate stay fixed. Retain immutable receipt provenance across omission or restoration. Reuse unaffected proof only when bindings/project rules permit.

### R42 — Keep implemented, locally verified, independently reviewed

Type: H · Scenarios: [A27](#a27), [A28](models.md#a28), [A68](#a68), [A157](#a157)

Keep implemented, locally verified, independently reviewed, hosted proof complete, accepted, merged and deployed/released distinct. `independently reviewed` follows R88. The coordinator assesses the objective under project acceptance authority; worker success and synthetic fixtures cannot promote later labels.

### R43 — Reports state satisfied obligations, blockers, candidate

Type: B · Scenarios: [A37](coordination.md#a37), [A68](#a68), [A145](#a145), [A158](#a158)

Reports state satisfied obligations, blockers, candidate and check evidence, route exceptions, uncertainty, unresolved native references, remaining gates, withdrawn obligations with provenance and reason, triage downgrades, `boundary_exceeded` results and open proposals as out of scope. An interim report for a quiescent objective says incomplete and names each external dependency. Unobserved usage and cost stay unknown.

### R51 — Claim live-verified coordination only with passing

Type: H · Scenarios: [A74](#a74)

Claim live-verified coordination only with passing live evidence for Claude Code and Codex on Linux, each with real Orca delegation through the production adapter. Synthetic, hosted and review checks never substitute for it, and missing live evidence stays NOT_RUN. Unsupported optional capabilities fail conservatively and cannot be marketed as verified.

### R87 — An assurance obligation records scope, question

Type: B,H · Scenarios: [A155](#a155), [A156](#a156)

An assurance obligation records scope, question, candidate, existing evidence and insufficiency. An overlapping second obligation on the same candidate names an uncovered risk, including when the first is withdrawn but its accepted proof remains current. A report is consumed only after exact native settlement and is immutable per Dispatch; exact replay is idempotent, while changed report or result evidence needs a fresh attempt. Findings record reviewer severity and triage: `required_correction` creates a parented correction obligation, consolidated on the candidate lineage; `advisory` creates a proposal; a downgraded blocker or major finding needs a reason. The obligation is satisfied by a completed, validated review observation bound to the current candidate, or a valid REUSE binding, together with satisfied or user-withdrawn corrections. A still-valid accepted proof relationship cannot be erased or restated to admit repeat review. Recording a required correction from any legitimately admitted completed review invalidates earlier accepted assurance proof, even when the report arrives late or old bindings return. Admission timestamps do not determine that order. Advisory additions cannot evict the correction's safety record; qualification waits for corrections and suitable current review proof. Definition-bound receipt history survives omission and candidate metadata changes. A correction affecting reviewed scope, assumptions, dependencies or governance, or with unknown impact, reopens the obligation for affected-scope delta review. Explicit candidate REUSE binding with the Git delta preserves unaffected evidence; stricter project policy governs.

### R88 — The independently reviewed label requires at

Type: H · Scenarios: [A157](#a157)

The `independently reviewed` label requires at least one non-withdrawn assurance obligation, with every non-withdrawn assurance satisfied by a binding valid for the delivery candidate. Zero or all-withdrawn assurance withholds the label. When the binding condition is unmet, the report stands, the label is withheld, and `assurance_unbound` names the gap. Stricter project rules still govern.

## Acceptance scenarios

- <a id="a23"></a>**A23** — Native duplicate/delayed messaging remains Orca-owned; Pod report ingestion joins a frozen packet and exact fresh Dispatch identity without a parallel acknowledgment ledger.
- <a id="a27"></a>**A27** — Worker success with failed required checks leaves objective incomplete.
- <a id="a64"></a>**A64** — Changed/absent/redirected sources differ from unavailable reads; definitive rejection survives restoration; unrelated dirty paths do not grant sensitive reads.
- <a id="a66"></a>**A66** — Cheap discriminating checks precede expensive work; independent review receives exact candidate and reproducible unprimed evidence.
- <a id="a67"></a>**A67** — Source/config/dependency/environment/candidate changes invalidate affected proof; no fabricated Git metadata and explicit alternative source binding. Ordinary receipts bind the verified definition; authorized check/scope changes invalidate old proof, and omission/restoration or candidate relabeling cannot refresh it.
- <a id="a68"></a>**A68** — Completion labels/final report follow evidence/project authority; failures, blockers, route exceptions and unresolved native references remain visible.
- <a id="a74"></a>**A74** — Missing live PASS for Claude Code or Codex on Linux, or for either delegation adapter, keeps live verification NOT_RUN despite synthetic CI and a read-only doctor.
- <a id="a145"></a>**A145** — A worker report proposing new owner decisions leaves every obligation unchanged, and the report lists the proposals as out of scope.
- <a id="a155"></a>**A155** — **Amplification.** A review packet must serve an assurance obligation. A second assurance obligation on an overlapping scope and the same candidate without `uncovered_risk` is refused `obligation_invalid`, including when the first was withdrawn but its accepted proof remains current. A review attempt against a satisfied assurance obligation is refused `unbound_assignment`. Removing a still-valid accepted receipt or changing candidate metadata cannot enable a repeat or hide scope overlap; bounded provenance survives.
- <a id="a156"></a>**A156** — A `required_correction` creates a parented correction obligation, and an `advisory` finding becomes a proposal. Downgrading a reviewer's blocker records a reason that the report lists. A legitimately admitted review's required correction remains recordable when its report arrives after a newer attempt's acceptance or an older environment or source binding returns. Earlier proof cannot qualify over that newly recorded correction, and advisory overflow cannot remove that protection. Review order is independent of wall-clock admission timestamps. A correction affecting reviewed scope, assumptions, dependencies or governance, or with unknown impact, re-opens the same assurance obligation for delta review. Unaffected evidence receives an explicit candidate REUSE binding with the Git delta; stricter project policy governs. The coordinator cannot withdraw a correction. Changing a question/check/scope under user authority invalidates old attempt receipts on the same id and candidate; a fresh attempt can qualify, while unrelated steering preserves unaffected proof.
- <a id="a157"></a>**A157** — With an unbound assurance obligation, none at all, or only withdrawn assurances, the report stands, the label is withheld, and `assurance_unbound` names the gap. An unsettled review report cannot be consumed, and a failed report cannot be changed to success on its Dispatch; a fresh settled review attempt may qualify. A valid REUSE binding satisfies the requirement.
- <a id="a158"></a>**A158** — **Quiescence.** When only a non-Owner test identity remains outstanding, as `blocked_external` with the user as party, the interim report says incomplete and names the need and the unblock condition. The objective stays open. After the user supplies the identity, continuation admits the dependent obligation without a reopen.


## Verification

Python 3.13+ on Linux, one bundle-as-package and standard-library facilities
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
