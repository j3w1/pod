# The Orca boundary

Load the installed Orca orchestration guide before using runtime features.
Its version-matched instructions own Run/Task/Dispatch creation, worker identity,
placement, messaging, Delivery, lifecycle, retries and cleanup. Follow its named
references when a conditional action requires them. Pod supplies no alternate
procedure or lifecycle journal.

## Pod admission

The `admission` helper serializes policy reservations before a native start:
owner, approved route, spending, quota, capacity, frozen packet and source
bindings. It retains native references and effective launch evidence so Pod
decisions can be checked. Runtime state is read fresh.

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

## Private state and migration

`pod-context/v2` stores policy admissions, checkpoints, source rejections,
interventions and legacy archive references. Admission reservations and
unresolved migration holds count conservatively against policy limits.
Orca remains authoritative for current workers and resource occupancy.

Use `internal state-migrate` explicitly for v1 records. It archives old state
and inspects Orca before producing v2; it does not perform lifecycle actions.
Old Delivery and cleanup records remain historical evidence. An unresolved
legacy record becomes a hold, not permission to repeat an action.
Exact native release evidence can clear a bound hold; unbound ambiguity stays held.
Read-only diagnostics never migrate automatically.

On continuation, recover the objective's decisions, relevant evidence gaps and
native references, then consult Orca. The checkpoint should identify the next
safe action without reproducing native status.
