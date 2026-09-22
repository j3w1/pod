# Verification and reporting

Evidence records bind criterion, candidate and source identity, policy revision, dependencies, environment, command/result, time and reviewer attempt. If a relevant binding changes, mark affected proof stale. Unaffected proof may be reused only if the project permits it. Worker success alone never completes a criterion.

Project checks and review gates are authoritative. Substantial or high-risk work gets independent review of the exact candidate and reproducible evidence, without a prompt that presupposes approval. Required hosted and live integrations remain separate. Do not promote synthetic fixture results into live proof.

Final report: original objective and criteria, achieved/failed/blocked checks, exact candidate, meaningful route exceptions, unknown quota/usage/cost, uncertain native effects, next owner action, and release state. A partial outcome stays partial.

## Governor

Before a Pod-mediated push, pull-request update, workflow dispatch, rerun, remote diagnostic, merge, release, deployment or cancellation, the `internal governor` helper returns ALLOW, REUSE or DEFER against durable records already kept for the objective: the unit's bound candidate generation and preflight receipts, active and uncertain effects, unresolved deliveries, correction history and prior governed actions. Record the outcome with `internal governor-outcome`, or let `internal governor-execute` perform the bound action and record it. The full contract, including delivery units, failure classification, safe cancellation, scoped exceptions and the enforcement level, is in the waste governor reference. These controls cover operations Pod mediates, not every shell command a session can run.
