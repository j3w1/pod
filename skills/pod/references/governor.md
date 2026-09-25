# Governor

The Governor evaluates Pod-mediated remote actions at decision boundaries without model calls. Orca owns workers; project governance owns acceptance.

## Candidate and readiness

Governor mutation requires this terminal's stable native current Run, referenced by the objective checkpoint or admissions on the same runtime, and the existing Pod owner. Establish/adopt it through Orca. Missing proof permits direct work and read-only status. Join only this objective's exact assignments.

Group corrections in one delivery unit. `internal governor-prepare` names its Tasks, branch, base, workflows and checks; it freezes commit, tree, base, workflow digests, toolchain, environment and policy. Changed inputs open a generation. `internal governor-preflight` records configured local checks. Project preflight names checks and triggers map effects. A CI-triggering push is validation. Tracked dirt leaves the candidate unfrozen.

Before each push, PR update, workflow dispatch, rerun, diagnostic, merge, release, deployment or cancellation, ask Governor: `ALLOW` permits the exact action, `REUSE` attaches equivalent work/proof, `DEFER` names a hold. It checks authority, candidate/effects, equivalent work, supersedence, readiness and failures. Review need not precede CI. Validation may overlap a read-only review of the same candidate; merge/release/deploy waits for both valid results.

`internal governor-execute` performs supported push, PR, workflow, rerun and cancellation actions against the bound commit. A prior reservation must not be duplicated. Merge, release and deployment remain project-governed. A lost response stays `UNKNOWN`; `internal governor-reconcile` reads provider state without resubmission. Publication records triggered validation.

## One delivery decision

Before the first remote Git mutation without applicable consent, ask once: **merge remotely, keep local, or defer**. Show repository, target, exact candidate commit/tree and `publish`/`merge` scope. For merge remotely, record `pod-authorization/v1` for both `publish` and `merge` bound to that commit/tree. Consent includes push/PR, required CI, merge of that exact candidate using `--match-head-commit`, and the stated post-merge install/verification. Routine validation and post-merge checks need no further prompt. Candidate or target movement requires fresh consent. Deployments, new provider effects and spending remain outside it. Keep local records a `user_direct` delivery revision, makes no remote mutation and reports hosted checks `NOT_RUN`; defer records `user_hold`. A previous explicit exact-scope instruction counts.

After merge, record `governor-outcome` with provider `merge_commit`; checkpoint `delivery: {record}`. Pod verifies authorization, exact result/tree, target identity and readback, retaining trusted governance and valid proof. Do not refresh governance unless Pod says the target moved. See [verification](verification.md).

## Objective cleanup

`internal cleanup-plan` is read-only and objective-scoped. It classifies worktrees and branches as integrated, merged remote head, unique or protected, showing dirty data, stashes, other worktrees, refs and observed terminals. One batched confirmation covers deletion classes; an exact-scope instruction counts. Retain unique work, verify an archive bundle before deleting its refs, or obtain separate discard consent. Rerun with `expect` immediately before deletion; changed facts stop cleanup. Execute only listed guarded Orca/Git commands, with exact ref tips and remote leases. Never add `--force` or delete a directory directly. Protected and uncertain resources stay. Native settlement, Delivery acknowledgment and release remain automatic.

## Failures and reporting

Classify failure with `internal governor-classify`: `code_defect` returns to correction; `remote_only` names a bounded discriminating check and stop condition; `transient` uses policy's bounded retry; `external` names a dependency. Unclassified failures defer. Supersedence cancels only pending cancel-safe older validation when policy permits; its result cannot approve a new candidate.

A scoped efficiency exception cannot lift authority or correctness. Observe mode turns only efficiency deferrals into warnings. Enforcement is advisory unless personal policy names host controls. `internal governor-status` reports candidates, preflight, decisions, validation and counters. Workflow proposals are suggestions until configured. Unknown authority, candidate or effects defer. Unobserved cost stays unknown; local checks cannot replace required hosted proof. Follow the project's required checks and merge queue.
