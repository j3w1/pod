# Orca

## Requirements

### R18 — Requirement R18

Type: B,H · Scenarios: [A08](#a08), [A51](#a51), [A126](#a126)

Read only installed native capability and exact request/worker evidence needed for a start. Missing or unsupported fields stay unknown; do not scrape credential stores, infer model access from catalog data or call providers to probe a route.

### R23 — Requirement R23

Type: I,H · Scenarios: [A08](#a08), [A56](#a56), [A94](../pod-spec.md#a94)

Resolve the applicable Orca runtime through installed discovery and guide. Validate worker-start launch-preferences capability, exact read/start/request contracts and optional terminal identity. Do not substitute a runtime, host or direct provider API.

### R24 — Requirement R24

Type: H · Scenarios: [A08](#a08), [A20](#a20), [A57](#a57), [A124](models.md#a124), [A129](#a129)

Before a new start validate current authority, placement, logical ceiling, packet/source bindings, version and selected route. Record requested and observed effective model/effort/context separately; absent and null launch fields stay unknown, except verified same-terminal reuse binds the prior settled attempt's known effective route as inherited evidence with provenance. Known relevant disagreements are mismatches. Objective-bound acceptance holds recorded route mismatches and unknown effective routes; caller claims cannot supply route proof. Pending replay retains its admitted request and does not become a new preference decision.

### R25 — Requirement R25

Type: B · Scenarios: [A22](models.md#a22), [A58](#a58), [A95](#a95)

Treat accepted input, started reasoning, native settlement and accepted output as different native observations. Silence/lost responses do not prove failure, justify resending input or authorize replacement work. Recover the same immutable admission through Orca request-show: record a completed receipt, join a pending request with Orca's UUID, or inspect exact Run/Task/Dispatch identity after an absent result or when no UUID was recorded. Missing, ambiguous or contradictory evidence holds and never starts fresh; a later incomplete observation cannot erase a known request-identity conflict. Preserve native start responses before classification in immutable admission-bound private evidence, including unknown refusal codes, observed request references, malformed output and transport outcomes. Recording returned facts uses the existing owned reservation even if native contact is lost; it cannot change admission state, routes or effect authority. Classification and subsequent effects still require current native authority. Evidence reads verify integrity; unavailable earlier values are never backfilled.

### R28 — Requirement R28

Type: H · Scenarios: [A18](coordination.md#a18), [A21](coordination.md#a21), [A22](models.md#a22), [A59](#a59), [A96](#a96)

Serialize admission under the objective lock, recording one logical reservation before each worker start. Reserved and unresolved attempts remain outstanding; an exact Run/Task/Dispatch readback with settled outcome and terminal Dispatch status frees a bound slot, including a failed stopped attempt. Do not census other objectives or reconstruct physical occupancy. Documented effect-free Orca refusals defer; uncertain errors require exact request and worker readback.

### R29 — Requirement R29

Type: H · Scenarios: [A21](coordination.md#a21), [A31](coordination.md#a31), [A60](#a60)

Establish one authoritative coordinator per objective. Adoption reconciles pending effects/native authority before dispatch. Before Governor preparation, admission, execution or any journal mutation, join the caller's stable native current-Run binding (Run, coordinator handle and consumer generation) to that objective's exact native references, runtime and existing Pod owner. Terminal self-identity alone is not authority; a missing, unrelated, changed, worker-only or takeover binding blocks. Failed authority still permits read-only diagnosis/status and safe direct work. A local lock is not distributed fencing; never manufacture a replacement controller or silently create/adopt a Run.

### R35 — Requirement R35

Type: B · Scenarios: [A23](assurance.md#a23), [A59](#a59), [A94](../pod-spec.md#a94)

Use Orca-native messaging/events and blocking waits directly. Pod freezes packets and joins reports to a fresh worker-show of the exact runtime/Run/Task/Dispatch/worker identity, but keeps no parallel Delivery receipt or acknowledgment state machine.

### R36 — Requirement R36

Type: B · Scenarios: [A26](#a26), [A95](#a95), [A131](#a131)

Orca owns worker reuse and request recovery. A new assignment or changed route requires a fresh selection. Verified same-terminal and worktree reuse binds the prior settled attempt’s known effective route with provenance and rechecks model eligibility; known Orca disagreement is a mismatch. No model or effort flag accompanies `--terminal`. Pending replay joins only its original request UUID.

### R38 — Requirement R38

Type: B,H · Scenarios: [A22](models.md#a22), [A26](#a26), [A96](#a96)

Worker reuse, retention, release and terminal/resource disposition are explicit Orca operations outside Pod's mutation adapter. Pod records no cleanup state and never infers or initiates release. Exact assignment settlement, not terminal release, frees the objective's logical slot. Never kill work or delete uncommitted work/evidence.

### R44 — Requirement R44

Type: I,H · Scenarios: [A31](coordination.md#a31), [A35](coordination.md#a35), [A39](../pod-spec.md#a39), [A69](#a69), [A96](#a96), [A132](#a132), [A161](#a161)

Persist compact `pod-context/v4` policy and evidence with the `pod-admission/v4`, `pod-packet/v3`, `pod-checkpoint/v3` and `pod-cli/v4` contracts. Another schema is reported and blocks only its objective (R90); nothing converts it. Keep native ids as references, never copied lifecycle state, and keep private data out of Git.                                                                                  

### R45 — Requirement R45

Type: B · Scenarios: [A22](models.md#a22), [A31](coordination.md#a31), [A60](#a60), [A69](#a69), [A95](#a95)

Recovery selects the objective and reads native state before action. Native request-show governs completed/pending/absent request recovery; exact Run/Task/Dispatch readback may bind only one matching attempt. Missing, ambiguous or unresolved own evidence keeps its logical reservation outstanding and never justifies relaunch. There is no Pod retry, release or lifecycle loop and no daemon.

### R57 — Requirement R57

Type: B,H · Scenarios: [A79](#a79), [A80](#a80), [A94](../pod-spec.md#a94), [A95](#a95), [A138](#a138)

Use installed Orca guidance and operation-specific capabilities. Native launch preferences are required for delegation; missing optional context control uses `native_default` with no context flag. Requested versus effective evidence is honest, and missing delegation capability leaves direct work available.

### R66 — Requirement R66

Type: B,H · Scenarios: [A105](#a105), [A106](#a106)

Before implementation select or create the exact Orca-managed objective worktree. A new objective worktree explicitly requests branch `orca/<task-slug>` from the host or native creation mechanism instead of accepting its default; a reused objective worktree keeps its branch, and assignment isolation follows Orca placement. Bind actual Git repository/common-dir, branch and path separately from display labels; reuse only the same objective; preserve dirty/colliding work. Resolve every native start/replay selector to the frozen objective or separately authorized assignment-isolation placement before effect. Resolve state across linked worktrees and apply canonical private project policy plus worktree restrictions restrictively without copying private files. Native/host mechanisms own creation/removal.

### R67 — Requirement R67

Type: B,H · Scenarios: [A107](#a107), [A108](#a108), [A109](#a109)

Normal delegated workers use native worker-start and Orca's new-agent-tab setting. Status exposes exact native Dispatch/Task/worktree and available terminal/tab identities, placement surfaces and discoverability warnings. Terminal allocation and native tab discovery do not prove rendered UI visibility or focus; absence alone does not prove no worker/tab. Diagnose missing tabs through the existing worker. Invent no visibility override, change no tab preference and focus only on explicit user request. After consuming and preserving an exact report, follow native Delivery acknowledgment and worker-release ordering promptly; reuse only for an immediate supported follow-up. Final cleanup checks exact objective workers only and retains uncertainty/protected resources. Pod adds no lifecycle/cleanup helper.

### R68 — Requirement R68

Type: B,H · Scenarios: [A110](#a110)

Discover Orca progressively from its installed guide and operation-specific capabilities. Missing delegation support blocks only delegation. Do not mirror native account state, automate provider UI, invent receipt fields, wrap providers or alter shared settings.

### R80 — Requirement R80

Type: H · Scenarios: [A124](models.md#a124), [A132](#a132), [A142](interface.md#a142)

Stamp the running `{version, bundle_digest}` in each checkpoint and refuse a new admission or Governor mutation if either differs, including a checkpoint missing the digest. Reload the skill and write a fresh checkpoint; existing native request recovery remains available. Record preference and Governor policy revisions separately so a model edit does not supersede candidate proof.

### R90 — Requirement R90

Type: B,H · Scenarios: [A161](#a161)

0.6.0 is a hard objective-state cutover. `pod update` prints a read-only notice of objectives on superseded schemas. Status and doctor list them with their schema and block them. Nothing converts them, and in-flight workers are settled through Orca. New objectives are unaffected.                                                                                                                                                                                                                                    

## Acceptance scenarios

- <a id="a08"></a>**A08** — Requested and effective launch values remain distinct; unknown or mismatched proof stays visible and blocks acceptance.
- <a id="a20"></a>**A20** — Descendant delegation requires direct user intent and counts under the same objective ceiling.
- <a id="a26"></a>**A26** — New Tasks get fresh sessions; compatible corrections may reuse; unsupported route changes get fresh bound attempts.
- <a id="a51"></a>**A51** — Missing native metadata remains unknown and causes no credential or undocumented-endpoint scraping.
- <a id="a56"></a>**A56** — Current native workers without terminals use worker identity/lifecycle APIs; missing terminal alone is not failure.
- <a id="a57"></a>**A57** — Missing launch-preferences capability blocks delegation; optional context selection is omitted rather than asserted.
- <a id="a58"></a>**A58** — Accepted but unproven submission triggers observation, not automatic Enter/resend/replacement/acceptance.
- <a id="a59"></a>**A59** — Faults around reservation/start/request receipt preserve uncertainty, immutable native start provenance and exact request recovery without a duplicate start. Unknown refusal codes retain observed identifiers without becoming effect-free refusals; subsequent readback cannot overwrite the original response. Corrupt or missing recorded evidence holds recovery.
- <a id="a60"></a>**A60** — Missing caller, conflicting ownership or a partial handover blocks delegation; valid adoption preserves the work without a proxy coordinator.
- <a id="a69"></a>**A69** — Interrupted versioned checkpoints recover privately and atomically; retention/native-first recovery protect unresolved effects/evidence.
- <a id="a79"></a>**A79** — Native-default context does not block an otherwise valid model start; actual context insufficiency is disclosed and the packet narrowed or decomposed.
- <a id="a80"></a>**A80** — Installer receipt and bundle digest identify ownership; changed copies are preserved and shadowing paths reported.
- <a id="a95"></a>**A95** — Completed, pending and absent Orca request recovery binds the same immutable admission without a second semantic start; invalid UUID, changed authority/worktree, contradictory receipt or ambiguous native attempt holds.
- <a id="a96"></a>**A96** — Orca's documented effect-free refusal records durable deferred evidence with no binding or blind retry; `runtime_error` and any unknown, malformed or partial-effect refusal remain unresolved until native readback settles them, and physical-capacity enforcement stays unavailable.
- <a id="a105"></a>**A105** — A new objective worktree is requested on branch `orca/<task-slug>`, never a host default, and a reused one keeps its branch. Actual Git repository/common-dir, branch and path bind the objective worktree; matching display text is insufficient and dirty/colliding owner work is preserved.
- <a id="a106"></a>**A106** — Canonical private project policy remains effective in a linked objective worktree and worktree/task policy can only narrow it, without copying the private file.
- <a id="a107"></a>**A107** — Normal worker-start uses the native Orca tab path and user setting. Exact native placement references and discoverability warnings, including a background surface, are exposed without asserting UI rendering or focus. Missing terminal/tab evidence remains unverified and does not trigger another worker.
- <a id="a108"></a>**A108** — After exact report consumption/preservation, native Delivery acknowledgment and worker release occur promptly in guide order; reuse requires an immediate supported follow-up.
- <a id="a109"></a>**A109** — Final cleanup checks exact objective workers only, protects uncertainty/foreign resources and removes a worktree only after integration, preservation and cleanliness.
- <a id="a110"></a>**A110** — Installed Orca guidance is loaded progressively and missing delegation controls do not block safe direct work or diagnostics; no wrapper/shared-setting workaround appears.
- <a id="a126"></a>**A126** — Missing runtime metadata stays unknown without account probes or inferred native capability.
- <a id="a129"></a>**A129** — Supported effort is used or omitted as native default; context is omitted without native control and effective metadata remains honest. Null, absent and irrelevant extra launch fields do not create a mismatch, while known contradictions do. Objective-bound acceptance holds a recorded route mismatch or unknown effective route despite passing caller checks and owner authorization; a known matching route can pass.
- <a id="a131"></a>**A131** — A settled same-Task terminal reuse rechecks eligibility and sends no model or effort flag. When native terminal and worktree identity match the prior bound or closed attempt, its known effective route binds as inherited evidence with admission, dispatch, terminal and field provenance; a known Orca disagreement is a mismatch.
- <a id="a132"></a>**A132** — Versioned records refuse other schemas without conversion; version drift blocks new mutation but leaves recovery available.
- <a id="a138"></a>**A138** — No context control blocks no valid start; missing launch-preferences capability blocks only delegation.
- <a id="a161"></a>**A161** — After an update, a 0.5 objective is reported with its schema and blocked, and it is not converted. `pod update` printed the notice. An in-flight 0.5 worker is settleable through Orca. A new objective works normally.                                                                                                                                                       


## Structured contracts

The coordinator proposes `{agent, model, effort|native_default, context:
native_default, reason}` after reading the pool. Judgment considers reasoning
need, ambiguity, risk, breadth, duration, capabilities, verification and useful
context. There is no formula, complexity tier or role table. Deterministic
selection validates the proposed id, eligibility, constraints, agent, supported
effort and native context control; it does not rank choices. Orca currently
exposes model and effort launch preferences but no per-worker context flag, so
`native_default` omits that flag. Catalog context data and AA metrics are not
native capability proof.

| Record | Bound information |
| --- | --- |
| Objective source | Issue identity, locator, body digest and amendments, without copied body. |
| Worktree binding | Exact Git common-dir, worktree path/branch and authorized placement. |
| Constraints | Objective-local kind, provenance (`user_direct`, `issue`, `repository`, `worker`), target and active exception. Indirect sources only narrow. |
| Route decision | Agent/model, requested effort/context, short reason, mode, preference and Governor revisions, constraint refs, Pod version, then observed effective values or `unknown`. |
| Worker packet | Objective/criteria, scope, source references, candidate, route, preference revision, reporting contract and authorized actions. |
| Admission | Intent, exact request and Run/Task/Dispatch/worker references, route decision, requested/effective values and recovery state. |
| Failure | Attempt-local `rate_limited`, `unavailable`, `auth_failed` or `safety_refusal`, source, time, optional native retry-after and later clear provenance. |
| Evidence/checkpoint | Candidate/source/policy/dependency/environment bindings, checks, remaining gates, native references, next safe action and Pod version. |
| Obligation checkpoint | `pod-checkpoint/v3`: sequence, map revision, fixed governance source paths/base commits/byte revisions, obligations, proposals, coordinator slot, quiescence or closure. |
| Obligation packet | `pod-packet/v3`: `serves[]`, role, path/surface boundary, investigation decision and stopping condition. |
| Obligation admission | `pod-admission/v4`: served obligations, role, boundary, Git-derived changed paths, boundary exceedance and result disposition. |

The map boundary refuses `obligation_invalid`, `obligation_unaccounted` and
`wait_invalid`; admission also refuses `unbound_assignment`,
`ownership_conflict` and `integration_pending`. Admission and Governor
mutations refuse `objective_closed`. Each refusal identifies its referent
and next safe action. `assurance_unbound` qualifies the report and withholds
the review label; it does not refuse the report. Existing admission, native
recovery and Governor codes retain their separate meanings.

Governance binds the selected target identity and exact base snapshot. An explicit
initial user selection is trusted input, including in a repository without a
known default or on a maintained nondefault branch with its own policy. Git refs,
ancestry and byte equality cannot prove pre-intake authorship or distinguish an
accepted merge from manually moving a ref. Later updates that contain known
objective candidate or result commits beyond the old base are guarded: they need
a fresh direct user decision for the exact new snapshot. Ordinary independent
target updates remain refreshable. Neither a repeated generic selector, stale
snapshot decision, equal policy bytes nor an already bound ref bypasses that
check. This is a bounded known-identity and authority check, not merge or
policy-authorship proof.

A guarded refresh uses existing private map fields: `governance_refresh: true`,
a fresh `revision_authority` with `provenance: user_direct` and its instruction,
and `governance` with `base_ref` equal to the full canonical target ref and
`base` equal to the full newly observed commit. Both snapshot fields must match
Git observation; the supplied base never overrides it. The checkpoint's
kernel-owned `governance_history` preserves up to 64 distinct candidate identities
and 64 explicit user decisions, including the canonical ref, commit, instruction
and map sequence. A mapped Git checkpoint requires its current candidate to
resolve to a commit before any write; unresolved names cannot enter the history.
History survives admission and report writes; caller omission or replacement
cannot erase it. Capacity refuses rather than evicts. Shared
ancestry outside the trusted base catches intermediate commits as well as tips.
An exact new snapshot decision can proceed despite unavailable old Git objects;
it preserves those records and grants no authority for a different future base.
Without that decision, missing comparison evidence holds and identifies the
missing identity and recovery action. Recorded decisions appear in the report.

Admission validates packet and recovers prior effects first, then issue, placement,
authority, version, source and logical-ceiling checks. Under the objective lock,
the final preference read validates the choice immediately before persisting the
admission row and submitting native `worker-start`; the lock is released before
the subprocess. A changed revision with a now-ineligible choice reports
`preference_changed`; an ineligible choice at the same revision reports its
specific selection reason. A changed revision with an otherwise valid choice
reports `preference_revision_stale`. All refuse before a row write.
An edit after row persistence is after this boundary and does not alter the
attempt. Pending same-UUID replay checks authority, runtime, placement, issue
body and checkpoint core but does not re-check preferences. Completed and absent
request diagnosis remains read-only. A new assignment on a reused worker gets a
new eligibility read; `--terminal` reuse carries no model or effort flag and
copies the prior effective route.

A direct user constraint may narrow agents/models, exclude models, select a
role model or lower maximum workers. Only direct user intent may allow a named
Disabled model or descendant delegation, and the exception lapses when the
model's saved state or selection mode changes. Constraints never edit global
YAML. A failure holds its route until retry-after or a meaningful change; a
replacement waits for settlement or proven no-start. Safety refusal bars a
same-Task alternative. There is no scheduler or blind retry.

The checkpoint binds root `VERSION` and the installer's canonical bundle digest,
computed from running bundle files without caches. A same-version code change or
missing checkpoint digest refuses a new admission or Governor mutation with
`installed_version_changed`; status and doctor show both identities. Reload the
skill, re-read `pod config --json`, and write a fresh checkpoint. Recovery of
an existing native request remains available.
The Governor keeps ALLOW/REUSE/DEFER, preflight, consolidation, CI reuse,
supersedence, classification and scoped efficiency exceptions; it uses the
Governor policy revision, not preference bytes. Orca owns lifecycle, and project
governance owns acceptance.

0.6.0 is a hard objective-state cutover. The old updater's staged installer
prints a read-only notice for older objective schemas before replacement.
Status and doctor identify and block those objectives without converting
them. Existing native workers continue through Orca; new objectives use the
new schema.
