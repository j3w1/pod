# Planning and packets

Use a brief when it improves coordination: preserve the original criteria, map
each to a check or explicit dependency, and name assumptions that would change
the approach. The private `brief` helper validates this map. Decomposition may
change as evidence arrives; acceptance criteria change only with authority.

Give each assignment one responsibility and an editing boundary. Serialize
overlapping changes while contracts are unsettled. Preserve unrelated work and
use the project's supported isolation mechanism.

Freeze a `pod-packet/v1` before admission. It binds objective, criteria,
responsibility, scope, permitted actions, candidate, dependencies, route,
policy/plan revisions, sources and reporting expectations. Native references
join the admission after Orca issues them; they are not predicted packet fields.

Context entries are bounded source, instruction or summary references with
digests. Include only relevant material, excluding secrets and the full
conversation. Unavailable sources need a fresh actual binding before admission.
Definitively changed or absent sources reject that assignment; restoring bytes
does not erase the rejection. This check does not promise an atomic snapshot
against external writers.

The `report` helper joins a `pod-report/v1` to the frozen packet, admission and
fresh native attempt evidence. Scope deviations require reconciliation, and a
report cannot grant new permissions or establish acceptance.

Correction history binds the Task and checkpoint criterion. Rewording a failure
or restarting the session does not reset its count. A diagnosis must identify
the obligation, failing example, hypothesis, last evidence and discriminating
check. Its bounded source observation establishes provenance, not the truth of
the hypothesis.

When sources, instructions, candidate or plan change, invalidate affected packets
and evidence. Preserve useful unaffected context only while its bindings remain
valid. Orca's guide governs session reuse and runtime handoffs.
