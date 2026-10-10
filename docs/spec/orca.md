# Orca

## Requirements

### R18 — Read only installed native capability and

Type: B,H · Scenarios: [A08](#a08), [A51](#a51), [A126](#a126), [A250](#a250)

Read only installed native capability and exact request/worker evidence needed for a start, plus exact terminal show liveness for R105. Missing or unsupported fields stay unknown; do not scrape credential stores, infer model access from catalog data or call providers to probe a route.

### R23 — Resolve the applicable Orca runtime through

Type: I,H · Scenarios: [A08](#a08), [A56](#a56), [A94](../pod-spec.md#a94), [A250](#a250)

Resolve the applicable Orca runtime through installed discovery and guide. Validate worker-start launch-preferences capability, exact read/start/request contracts and optional terminal identity. R105 adds only terminal show --terminal HANDLE --json as a read and orchestration run-use --id RUN --json as a mutation; only owner-handoff calls the latter, never run-create. Do not substitute a runtime, host or direct provider API.

### R24 — Before a new start validate current

Type: H · Scenarios: [A08](#a08), [A20](#a20), [A57](#a57), [A124](models/preferences.md#a124), [A129](#a129)

Before a new start validate current authority, placement, logical ceiling, packet/source bindings, version and selected route. Record requested and observed effective model/effort/context separately; absent and null launch fields stay unknown, except verified same-terminal reuse binds the prior settled attempt's known effective route as inherited evidence with provenance. Known relevant disagreements are mismatches. Objective-bound acceptance holds recorded route mismatches and unknown effective routes; caller claims cannot supply route proof. Pending replay retains its admitted request and does not become a new preference decision.

### R25 — Treat accepted input, started reasoning, native

Type: B · Scenarios: [A22](models/preferences.md#a22), [A58](#a58), [A95](#a95)

Treat accepted input, started reasoning, native settlement and accepted output as different native observations. Silence/lost responses do not prove failure, justify resending input or authorize replacement work. Recover the same immutable admission through Orca request-show: record a completed receipt, join a pending request with Orca's UUID, or inspect exact Run/Task/Dispatch identity after an absent result or when no UUID was recorded. Missing, ambiguous or contradictory evidence holds and never starts fresh; a later incomplete observation cannot erase a known request-identity conflict. Preserve native start responses before classification in immutable admission-bound private evidence, including unknown refusal codes, observed request references, malformed output and transport outcomes. Recording returned facts uses the existing owned reservation even if native contact is lost; it cannot change admission state, routes or effect authority. Classification and subsequent effects still require current native authority. Evidence reads verify integrity; unavailable earlier values are never backfilled.

### R28 — Serialize admission under the objective lock

Type: H · Scenarios: [A18](coordination.md#a18), [A21](coordination.md#a21), [A22](models/preferences.md#a22), [A59](#a59), [A96](#a96)

Serialize admission under the objective lock, recording one logical reservation before each worker start. Reserved and unresolved attempts remain outstanding; an exact Run/Task/Dispatch readback with settled outcome and terminal Dispatch status frees a bound slot, including a failed stopped attempt. Do not census other objectives or reconstruct physical occupancy. Documented effect-free Orca refusals defer; uncertain errors require exact request and worker readback.

### R29 — Establish one authoritative coordinator per objective

Type: H · Scenarios: [A21](coordination.md#a21), [A31](coordination.md#a31), [A60](#a60), [A195](#a195), [A196](#a196), [A197](#a197), [A232](#a232)

Establish one authoritative coordinator per objective. Adoption reconciles pending effects/native authority before dispatch. Before Governor preparation, admission, execution or any journal mutation, join the caller's stable native current-Run binding (Run, coordinator handle and consumer generation) to that objective's exact native references, runtime and existing Pod owner; each authority-joining checkpoint records the generation it observed. Terminal self-identity alone is not authority; a missing, unrelated, changed, worker-only or takeover binding blocks. A changed runtime alone joins only through the runtime-continuity rebind of R98. The only coordinator-change path is the separate Owner-authorized handoff of R105. Failed authority still permits read-only diagnosis/status and safe direct work. Diagnose a displaced Run with its exact run-use command only for the recorded owner on the same runtime; Every non-owner caller, including one supplying the recorded owner string, receives read-only R105 detection under the path's authority code before R98, with no internal runtime-continuity route. Failed native reads carry only a syntactically bounded native code; outside a live Orca terminal they name the cause and read-only objective status alternative. A local lock is not distributed fencing; never manufacture a replacement controller or silently create/adopt a Run.

### R35 — Use Orca-native messaging/events and blocking waits

Type: B · Scenarios: [A23](assurance.md#a23), [A59](#a59), [A94](../pod-spec.md#a94)

Use Orca-native messaging/events and blocking waits directly. Pod freezes packets and joins reports to a fresh worker-show of the exact runtime/Run/Task/Dispatch/worker identity, but keeps no parallel Delivery receipt or acknowledgment state machine.

### R36 — Orca owns worker reuse and request

Type: B · Scenarios: [A26](#a26), [A95](#a95), [A131](#a131)

Orca owns worker reuse and request recovery. A new assignment or changed route requires a fresh selection. Verified same-terminal and worktree reuse binds the prior settled attempt’s known effective route with provenance and rechecks route eligibility and the exact pin; known Orca disagreement is a mismatch. No model or effort flag accompanies `--terminal`. Pending replay joins only its original request UUID.

### R38 — Worker reuse, retention, release and terminal/resource

Type: B,H · Scenarios: [A22](models/preferences.md#a22), [A26](#a26), [A96](#a96)

Worker reuse, retention, release and terminal/resource disposition are explicit Orca operations outside Pod's mutation adapter. Pod records no cleanup state and never infers or initiates release. Exact assignment settlement, not terminal release, frees the objective's logical slot. Never kill work or delete uncommitted work/evidence.

### R44 — Persist compact pod-context/v4 policy and evidence

Type: I,H · Scenarios: [A31](coordination.md#a31), [A35](coordination.md#a35), [A39](../pod-spec.md#a39), [A69](#a69), [A96](#a96), [A132](#a132), [A161](#a161), [A226](#a226)

Persist compact `pod-context/v4` policy and evidence with the `pod-admission/v4`, `pod-packet/v3`, `pod-checkpoint/v3` and `pod-cli/v4` contracts. Another schema is reported and blocks only its objective (R90); nothing converts it. Objective context, Governor and native-start observation files use compact canonical JSON at unchanged byte limits; cache formatting is unchanged. Indented older records read unchanged. Oversize refusals report current/proposed sizes, limit and largest sections before any write. Optional fields stay within existing schemas without conversion; genuine 0.7.1 proof at full-id candidates remains valid. Keep native ids as references, never copied lifecycle state, and keep private data out of Git.

### R45 — Recovery selects the objective and reads

Type: B · Scenarios: [A22](models/preferences.md#a22), [A31](coordination.md#a31), [A60](#a60), [A69](#a69), [A95](#a95)

Recovery selects the objective and reads native state before action. Native request-show governs completed/pending/absent request recovery; exact Run/Task/Dispatch readback may bind only one matching attempt. Missing, ambiguous or unresolved own evidence keeps its logical reservation outstanding and never justifies relaunch. There is no Pod retry, release or lifecycle loop and no daemon.

### R57 — Use installed Orca guidance and operation-specific

Type: B,H · Scenarios: [A79](#a79), [A80](#a80), [A94](../pod-spec.md#a94), [A95](#a95), [A138](#a138)

Use installed Orca guidance and operation-specific capabilities. Native launch preferences are required for delegation; missing optional context control uses `native_default` with no context flag. Requested versus effective evidence is honest, and missing delegation capability leaves direct work available.

### R66 — Before implementation select or create the

Type: B,H · Scenarios: [A105](#a105), [A106](#a106), [A231](#a231)

Before implementation select or create the exact Orca-managed objective worktree. A new objective worktree explicitly requests branch `orca/<task-slug>` from the host or native creation mechanism instead of accepting its default; a reused objective worktree keeps its branch, and assignment isolation follows Orca placement. Concurrent writers use per-assignment `placement` worktrees and integrate by merge to preserve ancestry; placement refusals name this correction. Bind actual Git repository/common-dir, branch and path separately from display labels; reuse only the same objective; preserve dirty/colliding work. Resolve every native start/replay selector to the frozen objective or separately authorized assignment-isolation placement before effect. Resolve state across linked worktrees and apply canonical private project policy plus worktree restrictions restrictively without copying private files. Native/host mechanisms own creation/removal.

### R67 — Normal delegated workers use native worker-start

Type: B,H · Scenarios: [A107](#a107), [A108](#a108), [A109](#a109), [A170](delivery.md#a170), [A171](delivery.md#a171)

Normal delegated workers use native worker-start and Orca's new-agent-tab setting. Status exposes exact native Dispatch/Task/worktree and observed terminal/tab identities, placement surfaces and discoverability warnings without claiming rendered visibility or focus. Diagnose missing tabs through the existing worker; focus only on explicit user request. After an exact preserved report, follow native Delivery acknowledgment and worker-release order promptly; reuse only for an immediate supported follow-up. Objective cleanup is planned read-only and executed through native/host operations with scoped consent; uncertain or protected resources remain.

### R68 — Discover Orca progressively and bound recognized update opt-out

Type: B,H · Scenarios: [A110](#a110)

Discover Orca progressively from its installed guide and operation-specific capabilities. Missing delegation support blocks only delegation. Do not mirror native account state, invent receipt fields, wrap providers, alter shared settings or automate provider UI, except this user-authorized Codex software-update opt-out: confirm exact live native `terminal read --screen` readback of the recognized menu and displayed Skip for now option/control, select it once through supported `terminal send`, verify readiness, then resume normal admission/recovery at the same model/effort. An unproven option/control remains blocked. No software installation, automation of other prompts, provider settings, permission/safety changes, duplicate pending requests or retry/controller loop. Executable helpers and accept/refuse semantics remain unchanged; missing live automatic opt-out proof is NOT_RUN and separate runtime evidence.

### R80 — Stamp the running {version, bundle_digest} in

Type: H · Scenarios: [A124](models/preferences.md#a124), [A132](#a132), [A142](interface.md#a142)

Stamp the running `{version, bundle_digest}` in each checkpoint and refuse a new admission or Governor mutation if either differs, including a checkpoint missing the digest. Reload the skill and write a fresh checkpoint; existing native request recovery remains available. Record preference and Governor policy revisions separately so a route edit does not supersede candidate proof.

### R90 — 0.6.0 is a hard objective-state cutover

Type: B,H · Scenarios: [A161](#a161)

0.6.0 is a hard objective-state cutover. `pod update` prints a read-only notice of objectives on superseded schemas. Status and doctor list them with their schema and block them. Nothing converts them, and in-flight workers are settled through Orca. New objectives are unaffected.

### R98 — Rebind an Orca runtime change only on proven continuity

Type: H · Scenarios: [A195](#a195), [A196](#a196), [A197](#a197), [A198](#a198), [A199](#a199)

For the recorded owner only (non-owner detection in R105 takes precedence regardless of the supplied owner string), when an objective's recorded runtime differs from the current one, each mutating runtime check (`require_authority`, the Governor projection, report reads, admission recovery and new worker admission) first classifies continuity from run-current and the exact worker and Run/Task reads Pod already makes, each at the current runtime. Equal Run, coordinator handle, recorded consumer generation and bound admission Run/Task/Dispatch/worker/worktree ids prove it: Pod rebinds under the objective lock, records old and new runtime, time, verified identities and provenance `automatic`, reports the rebind and continues. An unreadable identity, an unresolved admission, a reserved admission whose exact Run/Task lookup is unavailable, inconclusive or finds a Dispatch, a referenced Run run-current cannot show, or no recorded generation leaves it ambiguous: Pod writes nothing and refuses `runtime_continuity_ambiguous`, naming what it could not read. Only a direct `user_direct` Owner decision through `internal runtime-continuity`, naming the objective, both runtimes and that ambiguity and reclassified at write time, rebinds it with provenance `owner`; it changes only the recorded runtime and a missing generation. Any differing identity disproves continuity: the path keeps its existing refusal and nothing rebinds it. A bind, a refusal deferral, the report path's route updates and report consumption apply native evidence only at the runtime they read it at: none rebinds inside its own authority join, which refuses with its existing code as without continuity, and each refuses `native_authority_unverified` without a write when another call rebound first. Recording a start receipt's request fields may rebind; the bind that follows still refuses a receipt from another runtime. Status shows the bounded continuity history, including handoffs, when there is one, and never writes; a rebind past eight entries refuses without a write. A rebind never changes the coordinator or owner, creates or adopts a Run, or starts, replays or recovers work; R25/R45 recovery stays a separate step.

### R105 — Explicit Owner-authorized coordinator loss handoff

Type: H · Scenarios: [A234](#a234), [A235](#a235), [A236](#a236), [A237](#a237), [A238](#a238), [A239](#a239), [A240](#a240), [A241](#a241), [A242](#a242), [A243](#a243), [A244](#a244), [A245](#a245), [A246](#a246), [A247](#a247), [A248](#a248), [A249](#a249)

Only `internal owner-handoff` may replace an objective coordinator. Read-only detection in status and authority refusals precedes R98 for a non-owner caller regardless of its supplied owner string. The previous coordinator is always the recorded Pod owner. A live non-worker caller must resolve a stable identical recorded Run, whose coordinator is either that owner (fresh), or this caller with a matching durable pending handoff (resume). Orca must report the recorded owner's handle stale or gone; structured-session unsupported and unreadable states fail closed. Every bound admission's Run/Task/Dispatch/worker/worktree identity is compared at the current runtime by R98. The confirmation alone may cover named R98 ambiguities, never differing identities or a different Run. Another open objective on the Run blocks handoff and is named. Scope enumeration must be complete and readable, including objective directories; a referenced peer is excluded only by a fully validated, noncontradictory obligation closure and its matching report. Unavailable scope or unsupported closure cannot prove nonmembership. The direct Owner decision to hold handoff also bars exclusion of a closed peer when retained REUSE changed definition and its original eligibility scope is unrecorded. Recorded facts remain readable for ordinary writes; only handoff holds with a bounded named unverifiable-peer diagnosis. No confirmation overrides it, and a pending handoff remains pending. Report/packet scope, editing boundary and the later definition never replace the missing original scope.

The plain question names the objective and owner change, says Orca reports the previous coordinator's handle stale or gone, and contains no handle or Run id. The coordinator builds a `user_direct` decision with instruction and exactly objective, from_owner, to_owner, run, recorded_generation, recorded_runtime, current_runtime and ambiguity from fresh status facts. A missing recorded generation is a named ambiguity; the decision binds the recorded generation, allowing a resume after generation advances. The operation rebuilds facts under the objective lock and refuses absent, broad, stale or mismatched confirmation, closed objectives and new entries past the shared eight-entry bound before phase 1.

Phase 1 writes only an owner_handoff pending history entry with its decision, both owners, runtimes, recorded Run/generation and pre-rebind liveness/lineage evidence. Branch 1 alone calls `orchestration run-use --id RUN --json` from this caller; Branch 2 never calls it again. Phase 2 rechecks exact work identity and a stable Run naming this caller with generation above the recorded one (only coordinator identity when no generation was recorded). It writes done, objective/admission owners, the new continuity binding and current runtimes on references/admissions. The revision counter may advance; the Governor journal and other objectives stay byte-identical. No work start, replay, retry, recovery, settlement, adoption, redispatch, closure or release occurs.

A refusal/error aborts only after authoritative readback proves the Run still names the recorded owner at the recorded generation; unknown/landed results or failed rereads remain pending. A pending caller alone may resume while its handle is not reported stale or gone; a different live caller cannot abort it. The operation aborts definite eligibility loss (lost pending caller, different Run/identity or another open objective), preserving the reason; unreadable facts leave pending. A new attempt replaces an aborted entry without adding history, and retries retain pending. At most one entry is unfinished. Manual rebinds, aborted or crashed handoffs without live pending lineage require a successor or recovery objective, never a manual run-use. Status/refusals never abort or write.

Only handle, liveness/runtime and bounded native code leave terminal show; preview, title, paths, branch, pty and pane identity never enter handoff records or outputs and never grant authority. After done, ordinary native readback may settle failed reviewers; reserved and unresolved attempts retain R25/R45. Old handles refuse. Genuine 0.8.0 objective status refuses owner_handoff provenance as state_unsupported without conversion; its run-scoped reader skips the record.

## Acceptance scenarios

- <a id="a08"></a>**A08** — Requested and effective launch values remain distinct; unknown or mismatched proof stays visible and blocks acceptance.
- <a id="a20"></a>**A20** — Descendant delegation requires direct user intent and counts under the same objective ceiling.
- <a id="a26"></a>**A26** — New Tasks get fresh sessions; compatible corrections may reuse; unsupported route changes get fresh bound attempts.
- <a id="a51"></a>**A51** — Missing native metadata remains unknown and causes no credential or undocumented-endpoint scraping.
- <a id="a56"></a>**A56** — Current native workers without terminals use worker identity/lifecycle APIs; missing terminal alone is not failure.
- <a id="a57"></a>**A57** — Missing launch-preferences capability blocks delegation; optional context selection is omitted rather than asserted.
- <a id="a58"></a>**A58** — Accepted but unproven submission triggers observation, not automatic Enter/resend/replacement/acceptance.
- <a id="a59"></a>**A59** — Faults around reservation/start/request receipt preserve uncertainty, immutable native start provenance and exact request recovery without a duplicate start. Unknown refusal codes retain observed identifiers without becoming effect-free refusals; subsequent readback cannot overwrite the original response. Corrupt or missing recorded evidence holds recovery.
- <a id="a60"></a>**A60** — Missing caller, conflicting ownership or a partial handover blocks delegation; only R105's exact Owner-confirmed pending/done handoff can replace the coordinator, preserving work without a proxy coordinator.
- <a id="a69"></a>**A69** — Interrupted versioned checkpoints recover privately and atomically; retention/native-first recovery protect unresolved effects/evidence.
- <a id="a79"></a>**A79** — Native-default context does not block an otherwise valid model start; actual context insufficiency is disclosed and the packet narrowed or decomposed.
- <a id="a80"></a>**A80** — Installer receipt and bundle digest identify ownership; changed copies are preserved and shadowing paths reported.
- <a id="a95"></a>**A95** — Completed, pending and absent Orca request recovery binds the same immutable admission without a second semantic start; invalid UUID, changed authority/worktree, contradictory receipt or ambiguous native attempt holds.
- <a id="a96"></a>**A96** — Orca's documented effect-free refusal records durable deferred evidence with no binding or blind retry; `runtime_error` and any unknown, malformed or partial-effect refusal remain unresolved until native readback settles them, and physical-capacity enforcement stays unavailable.
- <a id="a105"></a>**A105** — A new objective worktree is requested on branch `orca/<task-slug>`, never a host default, and a reused one keeps its branch. Actual Git repository/common-dir, branch and path bind the objective worktree; matching display text is insufficient and dirty/colliding owner work is preserved.
- <a id="a106"></a>**A106** — Canonical private project policy remains effective in a linked objective worktree and worktree/task policy can only narrow it, without copying the private file.
- <a id="a107"></a>**A107** — Normal worker-start uses the native Orca tab path and user setting. Exact native placement references and discoverability warnings, including a background surface, are exposed without asserting UI rendering or focus. Missing terminal/tab evidence remains unverified and does not trigger another worker.
- <a id="a108"></a>**A108** — After exact report consumption/preservation, native Delivery acknowledgment and worker release occur promptly in guide order; reuse requires an immediate supported follow-up.
- <a id="a109"></a>**A109** — Final cleanup reads this objective only, protects uncertain and foreign resources, and removes a worktree only after scoped consent, integration or verified archive, and a fresh guarded plan.
- <a id="a110"></a>**A110** — Installed Orca guidance is loaded progressively; missing delegation controls leave safe direct diagnosis available, with no wrapper/shared-setting workaround. Recognized Codex update recovery requires exact live menu readback, displayed Skip for now option/control, one supported selection, verified readiness and the same model/effort; absent proof remains blocked/NOT_RUN, with no other prompt automation, install, permission/safety change, duplicate pending request or controller loop.
- <a id="a126"></a>**A126** — Missing runtime metadata stays unknown without account probes or inferred native capability.
- <a id="a129"></a>**A129** — A new start sends its exact supported effort, while recovery of an older `native_default` admission omits the effort flag; context is omitted without native control and effective metadata remains honest. Null, absent and irrelevant extra launch fields do not create a mismatch, while known contradictions do. Objective-bound acceptance holds a recorded route mismatch or unknown effective route despite passing caller checks and owner authorization; a known matching route can pass.
- <a id="a131"></a>**A131** — A settled same-Task terminal reuse rechecks eligibility and sends no model or effort flag. When native terminal and worktree identity match the prior bound or closed attempt, its known effective route binds as inherited evidence with admission, dispatch, terminal and field provenance; a known Orca disagreement is a mismatch.
- <a id="a132"></a>**A132** — Versioned records refuse other schemas without conversion; version drift blocks new mutation but leaves recovery available.
- <a id="a138"></a>**A138** — No context control blocks no valid start; missing launch-preferences capability blocks only delegation.
- <a id="a161"></a>**A161** — After an update, a 0.5 objective is reported with its schema and blocked, and it is not converted. `pod update` printed the notice. An in-flight 0.5 worker is settleable through Orca. A new objective works normally.
- <a id="a195"></a>**A195** — After a checkpoint recorded the generation and only the runtime changed, a checkpoint write, a Governor mutation, a report read and a new worker admission each rebind automatically, report the rebind and continue, and status shows the history. A reserved admission whose exact Run/Task lookup returns no row does not block; its recovery rebinds, then continues as before.
- <a id="a196"></a>**A196** — For the recorded owner under R98 (R105 detection takes precedence for every non-owner caller), no run-current binding, an unreadable bound worker, an unresolved admission, a reserved admission whose lookup is unavailable, inconclusive or finds a Dispatch, a referenced Run run-current cannot show and a missing recorded generation each refuse `runtime_continuity_ambiguous`, naming what was unreadable, with the context and Governor journal byte-identical; recovery classifies before any hold. A matching exact-scope Owner decision rebinds with provenance `owner`; a stale, broad or mismatched one refuses, and a still-unreadable identity keeps blocking without skipping or closing an admission.
- <a id="a197"></a>**A197** — A differing Run, coordinator, generation or bound Run/Task/Dispatch/worker/worktree id fails closed with the path's existing refusal code, no rebind and no write, with or without an R98 Owner decision. An R105 handoff is a separate transition and never overrides differing Run or admission identity.
- <a id="a198"></a>**A198** — An unchanged runtime runs no continuity path and status never rebinds. A rebind issues no worker start, request replay, Run creation, adoption or owner change, and a rebind past the history bound refuses without a write. A bind whose worker read preceded a runtime change that destroyed its Dispatch refuses `native_authority_unverified`, leaving the row reserved and unbound, whether its own join or another call would rebind; a report whose identity read preceded another call's rebind refuses the same way without consuming; and a Governor reconcile writes its rebind under the objective lock. For a reserved admission whose lookup finds a Dispatch, the Owner's rebind leaves it reserved and unbound, and a separate recovery call binds the single matching Dispatch.
- <a id="a199"></a>**A199** — Sanitized incidents: an R17-shaped objective whose post-update checkpoint recorded its generation rebinds as proven, and R15's changed coordinator still fails closed for the recorded owner. Same-pane replacement handles may use only R105's fresh handoff; a manual rebind without pending lineage remains refused.


- <a id="a226"></a>**A226** — Checkpoint, Governor and native-start objective records write compact canonical JSON, while old indented records read and evidence digests remain unchanged. An oversized write reports current/proposed bytes, limit and largest sections and preserves the file and all other records; observation-cache formatting is unchanged.

- <a id="a231"></a>**A231** — An off-objective packet worktree or a child selector without assignment placement refuses worktree_binding_changed with the placement correction; an explicitly isolated placement admits under unchanged ownership and attribution rules.

- <a id="a232"></a>**A232** — Failed Orca reads retain only a bounded native code, never native message text. Missing terminal identity names live-terminal requirements and read-only alternatives. Only the recorded owner on the same runtime gets a displaced-Run run-use hint; other callers receive the real ownership cause and no Owner continuity route. Codes and authority outcomes are unchanged.

## Structured contracts

| Record | Bound information |
| --- | --- |
| Objective source | Issue identity, locator, body digest and amendments, without copied body. |
| Worktree binding | Exact Git common-dir, worktree path/branch and authorized placement. |
| Constraints | Objective-local kind, provenance (`user_direct`, `issue`, `repository`, `worker`), target and active exception. Indirect sources only narrow. |
| Route decision | Agent/model, exact route key, requested effort/context, short reason, Preferred and Pinned routes, preference schema, preference and Governor revisions, constraint refs, Pod version, then observed effective values or `unknown`. Older rows also carry `mode`. |
| Worker packet | Objective/criteria, scope, source references, candidate, route, preference revision, reporting contract and authorized actions. |
| Admission | Intent, exact request and Run/Task/Dispatch/worker references, route decision, requested/effective values and recovery state. |
| Failure | Attempt-local `rate_limited`, `unavailable`, `auth_failed` or `safety_refusal`, source, time, optional native retry-after, local reconsideration point and later clear provenance. |
| Evidence/checkpoint | Candidate/source/policy/dependency/environment bindings, checks, remaining gates, native references, next safe action and Pod version. |
| Obligation checkpoint | `pod-checkpoint/v3`: sequence, map revision, fixed governance source paths/base commits/byte revisions, obligations, proposals, coordinator slot, quiescence or closure, and optional runtime continuity: the recorded Run, coordinator and consumer generation and at most eight continuity entries, shared by rebinds and owner_handoff attempts. |
| Obligation packet | `pod-packet/v3`: `serves[]`, role, path/surface boundary, investigation decision and stopping condition. |
| Obligation admission | `pod-admission/v4`: served obligations, role, boundary, Git-derived changed paths, boundary exceedance and result disposition. |

The map boundary refuses `obligation_invalid`, `obligation_unaccounted` and
`wait_invalid`; admission also refuses `unbound_assignment`,
`ownership_conflict` and `integration_pending`. Admission and Governor
mutations refuse `objective_closed`. Each refusal identifies its referent
and next safe action. `assurance_unbound` qualifies the report and withholds
the review label; it does not refuse the report. Existing admission, native
recovery and Governor codes retain their separate meanings.

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
new eligibility read; `--terminal` reuse carries no model or effort flag, copies
the prior effective route and must equal the requested and any pinned route exactly.

A direct user constraint may narrow agents/models, exclude models, select a
role model or lower maximum workers. Only direct user intent may allow one exact
Disabled route or descendant delegation, and the route exception lapses when the
personal file changes. Constraints never edit global YAML. A temporary unavailable failure holds its route until meaningful native retry-after or the 60-second local reconsideration point in R19; expiry launches nothing. Actual auth/capacity/safety restrictions remain; a
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

- <a id="a234"></a>**A234** — Fresh detection requires every caller, lineage, recorded-owner, work and scope condition; status and checkpoint/Governor/report/admission refusals name the same result and never write. Phase 1 pending precedes the sole run-use, then phase 2 marks done and only the permitted owner/binding/runtime fields change.
- <a id="a235"></a>**A235** — A matching pending Branch 2 at an advanced generation completes with the original exact confirmation, no second run-use and no added history entry, even with eight entries.
- <a id="a236"></a>**A236** — A Run rebound manually, to the caller without pending, or outside live pending lineage refuses without write or original-pane advice; it names a successor/recovery objective and warns against manual run-use.
- <a id="a237"></a>**A237** — Another caller holding pending blocks attempts while not reported stale or gone. Once definitely lost, the operation aborts/replaces its entry with the latest reason, without adding history; detection never aborts.
- <a id="a238"></a>**A238** — Live, unreadable and terminal_unsupported_for_agent_session recorded-owner states block handoff; the recorded owner, never the Run's later coordinator, is inspected.
- <a id="a239"></a>**A239** — A caller with no stable identical binding, no live own terminal or an objective/Run worker terminal refuses before writes/effects; same-pane replacement identity grants no authority by itself.
- <a id="a240"></a>**A240** — A different Run is never overridden; a pending attempt definitively losing that identity is aborted only by the handoff operation.
- <a id="a241"></a>**A241** — In both fresh and pending handoffs, every other open objective on the recorded Run is named and blocks; unavailable directories or records hold. Closure exclusion uses the ordinary persisted checkpoint, map, selected/retained evidence, immutable receipt/sequence and withdrawal contracts before qualification and matching report. Governance headers and citations match the declared source snapshot, and retained REUSE binds its original proof. A complete consumed report retains its observation, assignment join and native observation digest; this proves past settlement solely when replaying a recorded closure, without settling work or changing ordinary native readback. Report digest validation preserves original runtimes through the bounded continuity history. Completed retained review history keeps its role, exact served assurance and full provenance despite withdrawal or optional-marker omission; retained REUSE targets and deltas match readable immutable Git facts. Continuity runtime strings validate before any fingerprint collection and never supply current native authority. Unreported bound, reserved and unresolved admissions stay outstanding; malformed state diagnoses conservatively. Genuine supported closed, steering, withdrawal/history and foreign-Run peers remain valid controls subject to the Owner-approved hold for changed-definition REUSE with unprovable original scope; unsupported genuine older peers block without conversion. Journals and other objectives remain byte-identical.
- <a id="a242"></a>**A242** — Each differing bound Run/Task/Dispatch/worker/worktree identity blocks and cannot be overridden; the operation aborts a pending attempt on definite loss while unreadable facts preserve pending.
- <a id="a243"></a>**A243** — Unreadable bound workers, unresolved attempts, reserved lookups unavailable/inconclusive/finding a Dispatch, unseen referenced Runs and missing recorded generation are named ambiguities, covered only by exact confirmation; states remain outstanding. The unseen referenced-Run subcase alone uses an explicitly labeled synthetic defensive restored-record control: all supported checkpoint/admission writers preserve a singleton, and genuine pre-guard multi-Run records use a superseded checkpoint schema. It supplies neither genuine-writer nor live proof; all other subcases retain production/released-writer evidence.
- <a id="a244"></a>**A244** — Absent, broad, stale, non-user_direct or mismatched confirmation refuses before pending or native call; the plain question names the objective and handle state without handles/Run ids; facts are rebuilt at write time.
- <a id="a245"></a>**A245** — A closed objective or full eight-entry history without a resumable/replacement entry refuses without write/call; pending resumes and aborted replacements preserve that bound.
- <a id="a246"></a>**A246** — A run-use refusal/error aborts only when authoritative reread proves the old owner/generation unchanged; other fields, Governor and other objectives stay byte-identical.
- <a id="a247"></a>**A247** — Lost/landed run-use responses and unavailable rereads stay pending; a later exact Branch 2 completes without another mutation. Unreadable pending eligibility is never treated as definite loss.
- <a id="a248"></a>**A248** — The genuine 0.8.0 reader archived from merged Git source reports handed-off objective state_unsupported without conversion; its Run-scoped status skips that objective.
- <a id="a249"></a>**A249** — Handoff performs no work effects, keeps unresolved/reserved attempts outstanding, permits ordinary failed-reviewer settlement afterward and refuses old handles; R98 recorded-owner paths including original R15DisprovenIncident remain unchanged.
- <a id="a250"></a>**A250** — The Orca allowlists accept exactly terminal show --terminal HANDLE --json and orchestration run-use --id RUN --json in their respective read/mutation surfaces; only handoff calls run-use, and run-create and all alternate shapes refuse.
