# The Orca boundary

Load Orca's installed, version-matched orchestration guide before runtime work.
It owns Runs, Tasks, Dispatches, placement, messaging, Delivery, lifecycle,
retries and cleanup. Pod supplies no alternate procedure or lifecycle journal.

## Pod admission

The `admission` helper serializes policy reservations before a native start:
owner, approved route, spending, quota, logical fan-out, frozen packet and source
bindings. It retains native references and effective launch evidence so Pod
decisions can be checked. Runtime state is read fresh.

The objective lock reserves one slot per assignment. Exact native settlement of
an already-bound assignment frees that slot even if its terminal is retained.
An unresolved own request stays outstanding. Do not enumerate all Runs or
workers, reconstruct foreign resources, infer terminal liveness, or deduplicate
occupancy. Orca and the host own actual workers and physical limits.

Normal workers use native worker-start and visible agent tabs. After validating
and preserving an exact completed report, follow the guide's Delivery
acknowledgment and worker-release order promptly. Reuse needs an immediate
supported follow-up; uncertainty and protected resources remain retained.

Treat an authoritative native `capacity_full` no-start receipt as durable
`deferred`: record it without a successful binding and do not retry or audit the
fleet. A malformed, lost or partial-effect receipt remains unresolved.

A repeated admission must preserve its original request. Orca-issued mutation
UUIDs are recovery references, never Pod-generated operation IDs. Use the same
admission to consult the native request record; missing or ambiguous evidence
keeps the reservation unresolved. Creating a new admission does not resolve the
old one. Orca's guide governs actual retry decisions.
Completed or absent request recovery is read-only even after policy revocation;
replaying a pending mutation requires current authority.

A missing required capability blocks the affected operation. Diagnose or update
Orca; do not substitute another runtime, execution host, account or direct
provider worker API. Host-local serialization is not distributed fencing.

The requested/effective readback detects a model/effort mismatch only after worker start;
it is not a strict provider-startup guarantee before task delivery. The installed
`worker-start` exposes model and effort selection but no per-worker context,
per-launch strict-route or noninteractive flag, so those guarantees are unavailable. Do not automate
provider UI, write shared/personal provider settings or add a Pod provider wrapper.
Any future strict automation control must be opt-in per launch and leave ordinary
non-Pod sessions unchanged.

## Private state and migration

`pod-context/v2` stores policy admissions, checkpoints, source rejections,
interventions and legacy archive references. Admission reservations and
unresolved migration holds count conservatively against objective policy limits.
Orca remains authoritative for current workers and lifecycle.

Use `internal state-migrate` explicitly for v1 records. It archives old state
and inspects Orca before producing v2; it does not perform lifecycle actions.
Old Delivery and cleanup records remain historical evidence. An unresolved
legacy record becomes a hold, not permission to repeat an action.
Exact native assignment settlement can clear a bound hold; unbound ambiguity stays held.
Read-only diagnostics never migrate automatically.

On continuation, recover the objective's decisions, relevant evidence gaps and
native references, then consult Orca. The checkpoint should identify the next
safe action without reproducing native status.
