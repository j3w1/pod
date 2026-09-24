# Planning and packets

For issue-backed work, read [Pod Execution Spec](execution-spec.md), verify the
actual repository and bind source, body digest and worktree. Direct work skips
issue intake. Plan-only is read-only; accepted execution continues in the same
session under its actual authorization.

Use a brief that preserves every original criterion, maps it to a check or
explicit dependency, and names consequential assumptions. The `brief` helper
validates this map. Decomposition may change as evidence arrives; acceptance
criteria change only with authority.

Give each assignment one responsibility and an editing boundary. Serialize
overlapping changes while contracts are unsettled. Preserve unrelated work and
use the project's supported isolation mechanism.

Freeze a `pod-packet/v2` before admission. It binds objective, criteria,
responsibility, scope, permitted actions, candidate, dependencies, proposed
route, preference and plan revisions, issue/worktree/placement identity when
applicable, sources and reporting expectations. Native references join the
admission after Orca issues them; they are not predicted packet fields.

Context entries are bounded source, instruction or summary references with
digests. Include only relevant material, excluding secrets and the full
conversation. Unavailable sources need a fresh binding before admission.
Definitively changed or absent sources reject that assignment; restored bytes
do not erase the rejection. This is no atomic snapshot against external writers.

The `report` helper joins a bounded worker report to the frozen packet,
admission and fresh native attempt evidence. Scope deviations require
reconciliation. A report cannot grant permissions or establish acceptance.

Correction history binds Task and criterion. Rewording a failure or restarting
the session does not reset its count. Diagnosis identifies the obligation,
failing example, hypothesis, last evidence and discriminating check. Its
bounded source observation establishes provenance, not truth of the hypothesis.

Changed sources, instructions, candidate, plan or relevant preference revision
invalidate affected packets and evidence. Preserve unaffected bound context.
Orca's guide governs session reuse and runtime handoffs.
