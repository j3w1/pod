# Verification and reporting

Evidence records bind criterion, candidate and source identity, policy revision, dependencies, environment, command/result, time and reviewer attempt. If a relevant binding changes, mark affected proof stale. Unaffected proof may be reused only if the project permits it. Worker success alone never completes a criterion.

Project checks and review gates are authoritative. Substantial or high-risk work gets independent review of the exact candidate and reproducible evidence, without a prompt that presupposes approval. Required hosted and live integrations remain separate. Do not promote synthetic fixture results into live proof.

Final report: original objective and criteria, achieved/failed/blocked checks, exact candidate, meaningful route exceptions, unknown quota/usage/cost, uncertain native effects, next owner action, and release state. A partial outcome stays partial.

## Governor

Before a Pod-mediated push, pull-request update, workflow dispatch, merge, release or deployment, the `internal governor` helper returns ALLOW, WARN or DEFER against durable records already kept for the objective: the bound candidate, active and uncertain effects, unresolved deliveries, correction history and prior governed actions. It defers a duplicate action, a superseded candidate, and validation that is premature while integration is unsettled; it allows a necessary rerun when an input actually changed, and one early remote diagnostic that supplies information unavailable locally. Record the outcome with `internal governor-outcome` once the action finishes. An efficiency override is recorded verbatim and softens only an efficiency deferral; it never lifts an authorization, spending or correctness hold. These controls cover operations Pod mediates, not every shell command a session can run.
