# The Orca boundary

Use Orca's installed guide for runtime operations and cleanup; Pod adds no
lifecycle journal.

## Pod admission

Admission serializes logical reservations. A mapped packet serves current
unsatisfied obligations with role and boundary; its row atomically activates
them and refuses overlapping ownership. It validates coordinator authority,
packet, placement, sources, version, eligibility, constraints, route and
ceiling. Read preferences under the objective lock before recording the row;
release the lock before `worker-start`. A changed choice refuses
`preference_changed` without a write. Later edits affect later starts.

A bound assignment frees a logical slot after exact native settlement, even
when its terminal is retained. Reserved and unresolved requests remain
outstanding. Do not enumerate all Runs or workers, infer terminal liveness or
physical occupancy, or count another objective's workers.

Normal workers use native worker-start and Orca's new-agent-tab setting.
Report Task/Dispatch, worktree, terminal/tab references and native placement
warnings through status/readback. `surface: background` and discoverability
warnings describe native placement; terminal presence/absence proves neither
rendering nor focus. Diagnose missing tabs with the existing worker, never a
duplicate. Invent no visibility flag; change no preferences. Focus requires
supported behavior and user request. Preserve reports; follow Delivery/release
ordering. Reuse only for immediate follow-up; retain protected/uncertain resources.

Orca's documented effect-free refusals `task_not_found`, `task_not_startable`
and `inject_rejected` record `deferred` with no binding or blind retry.
`runtime_error`, unknown codes and malformed, lost or partial-effect receipts
stay unresolved until exact native request and worker readback settle them.
Orca-issued UUIDs are recovery references, never Pod-generated identities.
Completed and absent request diagnosis is read-only. Pending replay preserves
its original request and checks authority, runtime, placement, issue body and
checkpoint core. It does not re-check preferences or make a new model choice.

Terminal reuse requires a settled prior attempt with a known
terminal, copies its effective route and rechecks eligibility. Native
`worker-start --terminal` carries no model or effort flag. New model+effort and
model-with-omitted-effort starts use only Orca's documented launch preferences.
Only delegation requires this capability.

Requested and effective model/effort evidence is distinct. Missing effective
fields stay `unknown`; an exact bound attempt with a mismatch is marked
`route_mismatch` and blocks acceptance. Orca currently has no per-worker
context selector; omit the context flag and record `native_default`. Do not
write provider settings, automate provider prompts or wrap direct APIs.

## Private state

`pod-context/v4` stores compact `pod-admission/v4` reservations (`reserved`,
`bound`, `unresolved`, `closed`, `deferred`), checkpoints, source rejections and
interventions. `pod-packet/v3`, `pod-checkpoint/v3` and `pod-cli/v4` identify
changed shapes. A record in another schema blocks only its objective and is
reported, never converted. Status and doctor list older objectives; native
workers remain Orca's authority.

New admissions and Governor mutations refuse `installed_version_changed`
until skill reload and a fresh Pod-stamped checkpoint. Recovery is exempt.
On continuation, read native state first and retain the next safe action.
