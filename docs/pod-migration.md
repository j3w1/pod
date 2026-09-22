# Pod 0.3.0 state migration

Baseline `1b0aa6fc68ee2157f6f67ba4a60076d967791cc4` and Git history preserve the
pre-0.3 implementation and evidence. The migration changes Pod's private policy/evidence
context only. It performs no Orca mutation and does not release, retry, stop or delete native
work.

## Boundary change

Pod 0.3 removes its parallel native lifecycle machinery. Orca is authoritative for Runs,
Tasks, Dispatches, requests, messaging, worker state, terminals/resources and disposition.
Pod retains routing approval, quota/capacity/spending admission, frozen packets, source
bindings, reports, checkpoints, interventions, acceptance evidence and the waste governor.

`pod-context/v2` contains exactly owner, admissions, checkpoint, interventions,
source-rejections and legacy-archive references. An admission is `reserved`, `bound`,
`unresolved`, `closed` or `legacy_hold`. Native identifiers and Orca-issued request UUIDs are
references, never copied lifecycle authority. v1 Delivery and cleanup collections survive only
inside the immutable archive.

## Explicit migration

Run the private `state-migrate` operation separately for each selected v1 objective. The
operation:

1. takes the serialized admission/objective locks and reads the v1 file through a bounded,
   no-follow descriptor;
2. validates the complete v1 shape before native reads;
3. writes an immutable digest-named archive and verifies its SHA-256;
4. uses worker-show only to validate exact runtime, Run, Task, Dispatch, worker, worktree,
   terminal/resource and effective-launch evidence;
5. constructs and validates v2, then atomically replaces the active context.

Confirmed exact active effects become `bound`. Only exact native released proof becomes
`closed`. Reserved, uncertain, malformed, absent, ambiguous and pending/unknown-release rows
become `legacy_hold`. Spent-grant evidence is preserved whether the admission is active or
closed. A failure before the atomic replacement leaves v1 active and unchanged. Doctor and
status are read-only and report `migration_required`; new admissions for an affected objective
remain blocked.

There is no automatic downgrade. A v1 archive may be restored only before any v2 admission
has been made for that objective; otherwise restoration would discard newer policy evidence.
Native remediation, release and retry remain explicit Orca operations under the installed
orchestration guide.

## Installation and release

The package directory remains the single authoring source for Python, skill text and wheel
payload. Setup upgrades only copies it owns and prunes retired owned files such as
`references/native-effects.md`; skills-CLI-managed or modified copies are reported, not
overwritten. Installation examples remain pinned to the actually published `v0.1.2` until a
later release exists.

Hosted CI, live provider/Orca evidence, independent audit, project acceptance, merge, tag,
release and publication are separate gates. No such gate is implied by successful state
migration.
