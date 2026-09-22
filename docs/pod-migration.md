# Pod 0.3.0 state migration

Baseline `1b0aa6fc68ee2157f6f67ba4a60076d967791cc4` and Git history preserve the
pre-0.3 implementation and evidence. The migration changes Pod's private policy/evidence
context only. It performs no Orca mutation and does not release, retry, stop or delete native
work.

## Boundary change

Pod 0.3 removes its parallel native lifecycle machinery. Orca is authoritative for Runs,
Tasks, Dispatches, requests, messaging, worker state, terminals/resources and disposition.
Pod retains routing approval, quota/logical-fan-out/spending admission, frozen packets, source
bindings, reports, checkpoints, interventions, acceptance evidence and the waste governor.

`pod-context/v2` contains exactly owner, admissions, checkpoint, interventions,
source-rejections and legacy-archive references. An admission is `reserved`, `bound`,
`unresolved`, `closed`, `deferred` or `legacy_hold`. Native identifiers and Orca-issued request UUIDs are
references, never copied lifecycle authority. v1 Delivery and cleanup collections survive only
inside the immutable archive.

## Explicit migration

Run the private `state-migrate` operation separately for each selected v1 objective. The
operation:

1. takes the serialized admission/objective locks and reads the v1 regular file through a
   bounded, nonblocking, no-follow descriptor;
2. validates the complete v1 shape before native reads;
3. uses worker-show only to validate exact runtime, Run, Task, Dispatch, worker, worktree,
   assignment settlement and effective-launch evidence; terminal/resource disposition is ignored;
4. constructs and validates v2, writes and boundedly verifies an immutable digest-named
   archive, then atomically replaces the active context.

Use the installed bundle helper with a private JSON request; this is a Pod helper call, not an
Orca lifecycle command:

```json
{"project":"/absolute/path/to/project","objective":"the exact objective","owner":"the current Orca terminal handle"}
```

```sh
python3.13 /absolute/path/to/skills/pod/scripts/pod.py internal state-migrate --input /absolute/path/to/request.json
```

Choose the objective reported by `pod status`/`pod doctor` and the current coordinator handle.
The helper discovers and reads the installed Orca runtime; the request must not contain a Run,
Task, Dispatch, release instruction or user-selected request UUID.

Confirmed exact unsettled assignments become `bound`. Exact native assignment settlement becomes
`closed` regardless of retained terminal state. Reserved, uncertain, malformed, absent and
ambiguous rows become `legacy_hold`. Spent-grant evidence is preserved whether the admission is active or
closed. A failure before the atomic replacement leaves v1 active and unchanged. Doctor and
status are read-only and report `migration_required`; new admissions for an affected objective
remain blocked.

There is no automatic downgrade. A v1 archive may be restored only before any v2 admission
has been made for that objective; otherwise restoration would discard newer policy evidence.
Native remediation, release and retry remain explicit Orca operations under the installed
orchestration guide.

The retired v1 configuration key `policy.retain_idle_minutes` is not imported or accepted by
0.3. Remove it from personal, project and task policy before validating configuration. Orca's
explicit retention/disposition operations replace that old Pod timer; removing the key grants
no release authority and performs no native action.

Approved model routes also use a single redacted native account identity in `models.*.account`.
Run `pod doctor --json` read-only and copy the selected provider's 64-character
`account_identities.<provider>.identity_digest`. First mark the personal model route
`approved: false`; then place that digest in its `account` field and in any applicable personal
`allowed_accounts` or grant `account` field. Run `pod config --check --json`, copy that model's
new `approval_routes` value into `approval_route`, record the fresh personal approval in
`approval_ref`, set `approved: true`, and check again. Do not copy a display label, email,
workspace name or raw provider identifier. A missing identity leaves the route unavailable; a
changed identity needs a new personal approval and cannot inherit the old account's grants.

## Installation and release

The package directory remains the single authoring source for Python, skill text and wheel
payload. Setup upgrades only copies it owns and prunes retired owned files such as
`references/native-effects.md`; skills-CLI-managed or modified copies are reported, not
overwritten. Installation examples remain pinned to the actually published `v0.1.2` until a
later release exists.

Hosted CI, live provider/Orca evidence, independent audit, project acceptance, merge, tag,
release and publication are separate gates. No such gate is implied by successful state
migration.
