# Waste governor

A small deterministic control layer that stops redundant or premature remote execution, names the next useful action, and leaves every authorization, verification, review and release requirement where it already lives. It evaluates at boundaries only: before a managed remote action, after a validation result, when a candidate is superseded, and during recovery. No background agent, no cost model, no model call.

## Delivery units and candidates

A delivery unit is the smallest coherent part of the objective that is reviewed and delivered together. Related corrections in one unit converge on one branch and one pull request; an unrelated or urgent unit is not held back by them. Prepare a unit with `internal governor-prepare`, naming its remote, branch and base, the Tasks that belong to it, the workflow files a validation depends on, and the verification commands. The helper reads commit and tree from Git itself and freezes one candidate generation bound to commit, tree, base, workflow digests, verification commands, toolchain, environment and policy revision. Identical inputs return the same generation; changed inputs open a new one and name the superseded validation still pending. A working tree with modified tracked paths is observed as unfrozen and cannot be validated remotely until it is committed or discarded.

Record each configured local check against the current candidate with `internal governor-preflight` (PASS, FAILED or UNAVAILABLE). The project names its preflight checks in `waste_governor.preflight` and maps what a push or pull-request update triggers in `waste_governor.triggers`; a request may instead declare its own `effects`. What an action triggers is what the governor judges, so a push that starts CI is validation, not a Git operation.

## Decisions

Run `internal governor` before a push, pull-request update, workflow dispatch, rerun, remote diagnostic, merge, release, deployment or cancellation. The answer is one of three:

- ALLOW: execute exactly the bound action.
- REUSE: an identical action is already running (attach to it) or a passing result already binds this candidate and context (use it).
- DEFER: not now; the result names the reason class, the code, and the next action.

A warning is an annotation. Authorization denial stays its own class, separate from "permitted but premature". The order is fixed: authority; candidate and effect binding; equivalent running or recorded evidence; supersedence and unresolved prior effects; readiness or the diagnostic exception; repeated failure. Readiness is scoped to the unit: its own Tasks, deliveries and corrections, its own preflight receipts. It never requires every worker in the objective to finish and never requires review before CI.

Unknowns differ. An unknown authority, candidate identity or downstream effect defers the affected action until it is resolved. Unknown cost is a warning. Verification gaps in the checkpoint defer a merge, release or deployment and only annotate anything else.

## Managed execution and recovery

`internal governor-execute` admits and performs one action as a single step against the bound candidate commit, never against whatever HEAD points to later. Each journaled row carries the commit it was admitted for, so a row of a superseded generation is read back by its own commit. The journal records the admitted intention before the remote call; the lock is not held across the call. Push, pull-request reuse or creation, workflow dispatch with run readback, a failed-job rerun and a cancellation are the supported kinds. Merge, release and deployment are decided here and performed by project governance. A lost response leaves the row UNKNOWN; `internal governor-reconcile` settles it from provider readback and never resubmits. Any other failure around the remote call also settles the row rather than leaving it pending. A retry keeps the same logical operation and a new attempt number, so its failure history is not erased.

A publication that triggers a workflow is itself the validation: when a push or pull-request update lands, the governor journals one derived validation row per triggered workflow, with the run identity when the executor could read it back. A later dispatch of that workflow on the same candidate attaches to that row instead of starting a second run, and a dispatch requested while the publication is still pending is deferred until it lands. A reuse is a decision, not a row; nothing is journaled that could later be mistaken for a run that happened.

After a remote failure, classify it with `internal governor-classify` before considering another expensive attempt: `code_defect` returns to local convergence and is recorded as a correction through the ledger's own intervention rule, so a third equivalent correction requires a diagnosis naming distinct bounded evidence; `remote_only` asks for a bounded diagnostic naming the question, the local limitation, the smallest discriminating check and its stopping condition; `transient` permits the configured bounded retry; `external` reports a blocker instead of treating application code as broken. An unclassified failure defers the next attempt.

A `pod-governor/v1` journal is carried forward in place, but its rows are marked and never reused: the upgrade cannot supply the commit, tree, workflow digests, base or environment that v1 never froze, so such a row records that something happened rather than what it covered. A matching logical key therefore yields `ALLOW` with a `legacy_evidence_ignored` warning rather than `REUSE`, and a v1 row still `pending` or `UNKNOWN` defers as an unresolved effect, because v1 recorded no provider or run identity to read back.

Supersedence cancels only a pending, cancel-safe validation of a superseded candidate in the same unit, and only when `waste_governor.cancel_superseded_validation` allows it. A deployment, migration, merge, release or worker is never canceled this way; worker lifecycle stays with Orca. A superseded run's result can never approve the newer candidate, because evidence is keyed by candidate identity.

## Exceptions, modes and enforcement

An efficiency exception is not a force flag. It names a current personal `waste_governor.exceptions` grant bound to the objective, the action kinds and optionally the unit and candidate, plus a reason and who applied it. It softens only an efficiency deferral; an authorization, correctness or spending hold is never lifted, and a project file cannot create such a grant. `mode: observe` records what enforcement would have deferred and lets efficiency deferrals through as warnings for evaluation; a project may only tighten the mode, the retry budget and cancellation authority.

Enforcement is reported honestly. A skill cannot restrict a shell holding the same credentials, so the level is `advisory` unless the owner's personal policy declares a `host_control` reference, which is recorded at the owner-configuration tier and never claimed as a proven control. `internal governor-status` and `status` show units, candidate generations, preflight, the last decision and blocker, active and unresolved validation, counters, enforcement, and a read-only trigger proposal discovered from the repository's workflow files. A proposal is never policy until it is written into project configuration.

## Repository-side contract

Required-check reporting stays correct: a path- or branch-filtered required workflow that never runs leaves its check pending, so a selective example must handle that explicitly. Scoped concurrency groups may cancel a superseded pull-request run; a merge queue validates against the target branch and needs `merge_group`, and that context is not interchangeable with an earlier pull-request check. Local preflight receipts are preparation evidence, not a substitute for independently required remote proof. The target is no unnecessary duplicate validation of one candidate and context, not exactly one workflow run regardless of what the repository requires.
