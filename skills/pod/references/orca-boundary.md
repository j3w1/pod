# The Orca boundary

Load Orca's installed, version-matched orchestration guide before runtime work.
It owns Runs, Tasks, Dispatches, placement, messaging, Delivery, lifecycle,
request recovery and cleanup. Pod supplies no alternate lifecycle journal.

## Pod admission

The `admission` helper serializes an objective's logical reservations before a
native start. It validates coordinator authority, immutable packet, placement,
sources, installed version, current model eligibility, explicit constraints,
agent, effort, context support and the personal worker ceiling. The final
preference read occurs under the objective lock, immediately before recording
the row; the lock is released before `worker-start`. A changed choice refuses
`preference_changed` without a write. An edit after the row is written affects
later starts, not the submitted attempt. The skill reselects at most twice.

A bound assignment frees a logical slot after exact native settlement, even
when its terminal is retained. Reserved and unresolved requests remain
outstanding. Do not enumerate all Runs or workers, infer terminal liveness or
physical occupancy, or count another objective's workers.

Normal workers use native worker-start and visible agent tabs. After validating
and preserving an exact completed report, follow Orca's Delivery acknowledgment
and worker-release order promptly. Reuse requires an immediate supported
follow-up; uncertainty and protected resources remain retained.

Orca's documented effect-free refusals `task_not_found`, `task_not_startable`
and `inject_rejected` record `deferred` with no binding or blind retry.
`runtime_error`, unknown codes and malformed, lost or partial-effect receipts
stay unresolved until exact native request and worker readback settle them.
Orca-issued UUIDs are recovery references, never Pod-generated identities.
Completed and absent request diagnosis is read-only. Pending replay preserves
its original request and checks authority, runtime, placement, issue body and
checkpoint core. It does not re-check preferences or make a new model choice.

New terminal reuse requires the prior attempt to be settled with a known
terminal, copies its effective route and rechecks eligibility. Native
`worker-start --terminal` carries no model or effort flag. New model+effort and
model-with-omitted-effort starts use only Orca's documented launch preferences.
The capability is required for delegation, not safe direct work.

Requested and effective model/effort evidence is distinct. Missing effective
fields stay `unknown`; an exact bound attempt with a mismatch is marked
`route_mismatch` and blocks acceptance. Orca currently has no per-worker
context selector; omit the context flag and record `native_default`. Do not
write provider settings, automate provider prompts or wrap direct APIs.

## Private state

`pod-context/v4` stores compact `pod-admission/v3` reservations (`reserved`,
`bound`, `unresolved`, `closed`, `deferred`), checkpoints, source rejections and
interventions. `pod-packet/v2`, `pod-checkpoint/v2` and `pod-cli/v4` identify
changed shapes. A record in another schema blocks only its objective and is
reported, never converted. Native state remains Orca's authority.

New admissions and Governor mutations refuse `installed_version_changed`
until skill reload and a fresh Pod-stamped checkpoint. Recovery is exempt.
On continuation, read native state first and retain the next safe action.
