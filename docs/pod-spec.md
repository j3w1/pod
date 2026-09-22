# Pod — consolidated normative specification

Status: accepted implementation contract; not a claim of implementation or verification.
Baseline: `1b0aa6fc68ee2157f6f67ba4a60076d967791cc4`.
Schema/version: Pod 0.3.0 candidate; context `pod-context/v2`.

Pod is an adaptive coordination policy for Orca. It turns the coding session already
in use into the coordinator, choosing tools, agents, models and effort for each part
of a task and adapting the plan until the objective is verified.

## Authority and reconciliation

This is the sole authority for Pod product requirements, consolidated from the final
rewrite plan and the user-supplied P01 reconciliation packet. Earlier proposals are
historical inputs. Host policy, explicit user authorization and project governance
retain their respective authority. A completed test cannot silently revise a requirement.

Priority applied: latest explicit user decisions, final plan, Adaptive Routing proposal,
original skill-first proposal, existing implementation evidence. F denotes the numbered
final-plan section; P denotes the reconciliation-packet letter. Every retained normative
requirement is indexed below. B = behavior, H = hard authorization, I = implementation.

The following obsolete requirements are explicitly superseded; their removal is not a
blanket permission to remove applicable correctness checks.

| Superseded | Replacement |
| --- | --- |
| Replacement top-level controller | Existing Claude/Codex session coordinates; helpers are bounded. |
| Large public execution/preflight/resume CLI | Four command families; skill handles execution and continuation. |
| Editable Markdown plus YAML; mandatory legacy JSON configuration | Canonical YAML with generated table; validated source/check references may migrate without second routing authority. |
| Static role-to-strongest-model and implicit model approval | Assignment assessment, approved resolved pool, independent responsibility/model/effort. |
| Shared exhausted bucket solved by model substitution | Availability follows account/bucket, not model label. |
| Low quota always stops; unknown quota permits normal fan-out | Task-aware low quota; conservative unknown; exhausted unavailable. |
| Model approval grants spending or reset credits | Separate exact scoped bounded standing/action grants. |
| Fixed launch/finish waves | Dependency-ready admission when an objective logical slot safely frees. |
| All workers need terminals and old receipt shapes are universal | Installed-version backend-specific native identity, optional terminal. |
| Automatic managed installer/PATH repair in setup | Explicit isolated installation; setup only owned enrollment/integration writes. |
| Unconditional session-age stop/restart and Pod retention timer | Evidence-based checkpoints; Orca owns worker retention and disposition. |
| Release core integrations as merely unverified | Missing live core evidence blocks release. |
| Keep old aliases/machinery/tests for compatibility | No compatibility period; preserve applicable invariants with replacement coverage. |
| Passive setup means no writes | Setup may write authorized owned integration; diagnostics/default reads remain read-only. |
| Earlier proposals stay parallel authorities | Historical provenance only; this consolidated specification governs Pod. |

### Owner decisions of 2026-09-21

The owner superseded the following requirements for the completion of this rewrite.
Each is retired in place with its reason; a retired requirement is never reported as
passed, and retiring one does not permit removing an applicable correctness check.

| Retired | Replacement | Reason |
| --- | --- | --- |
| Support for the retired second platform and its transport ownership, with their gates (R51, A34) | Supported execution environment: Linux | The product is Linux-only; the implementation, tests, CI and documentation are removed rather than merely ungated. <!-- platform-audit: refusal -->|
| Comparative three-mode benchmark (R56, A73 benchmark clause, A80) | No benchmark prerequisite | The owner withdrew the matched-evaluation requirement; efficiency is governed by a bounded decision check instead. |
| Universal Orca billing and hidden fan-out attestation before any launch | Per-route establishment with explicit control tiers (R62) | No installed Orca command proves a universal attestation, so the demand made every launch fail closed while proving nothing. |
| `release_authorized` projected as a constant | Owner authorization supplied as an input record (R64) | A gate must report technical readiness accurately; authorization is an owner decision, never a test result. |
| Hosted native-OS matrices and a separate ceremonial owner sign-off | One hosted Linux check; disposable-project acceptance | The remaining ceremony proved nothing the recorded evidence did not already carry. |

### Owner decision of 2026-09-22

Orca is the single source of truth for Runs, Tasks, Dispatches, request recovery,
worker lifecycle, messaging, placement and terminal/resource disposition. Pod is a thin
policy, routing and evidence layer. Pod keeps a serialized admission seam and exact native
references, but it does not copy native lifecycle status or implement Delivery, cleanup,
liveness, terminal, release or retry state machines. The installed, version-matched Orca
orchestration guide and its recovery reference govern native mutation semantics. Orca mints
request UUIDs; Pod only records and reuses them for the same immutable admission.

The owner also retired Pod's fleet-wide resource-occupancy model on 2026-09-22.
These retirements are architecture decisions, not passing results:

| Retired | Replacement | Reason |
| --- | --- | --- |
| All-Run pagination, worker-fleet census, terminal liveness and retained/released/resource reconstruction for admission | Serialized objective-local logical reservations plus exact settlement of already-bound assignments | Orca owns workers and lifecycle; CE owns physical limits. Reconstructing their state in Pod created a shadow resource model. |
| Occupancy deduplication, foreign historical worker reconstruction and cross-objective runtime-capacity inference | One logical reservation per Pod assignment; independent objectives do not census each other's history | Pod decides desired fan-out, not physical occupancy. |
| Governor fleet-completeness prerequisite | Stable native current-Run/coordinator/generation authority and exact objective assignment evidence when relevant | Governor needs authority and objective readiness, not a machine-wide capacity audit. |
| Terminal release as the signal that an assignment frees a Pod slot (A68, A103) | Exact native assignment settlement frees the logical slot regardless of retained terminal; unresolved own requests remain held | Terminal disposition is independent of objective work settlement. |

An authoritative native `capacity_full` refusal creates a durable deferred admission with
no binding and no blind retry. A malformed, lost or partial-effect response remains
unresolved. Pod does not query, infer or claim physical capacity.

## Normative requirements

| ID | Type | Requirement | Sources | Scenarios |
| --- | --- | --- | --- | --- |
| R01 | B | Maintain this consolidated requirement/scenario inventory as product authority. Explicitly revise changed requirements and preserve history; implementation results cannot silently redefine them. | F1,8,12; PO–P | A40 |
| R02 | B | Keep the current conversation as coordinator. Orca is the sole runtime authority for Runs, Tasks, Dispatches, requests, placement, messaging, terminals/resources and lifecycle; projects own governance. Pod is policy, routing and evidence only. Helpers must not become a scheduler, competing native task/lifecycle database, autonomous controller or restart daemon. | F1–2,9,15; PA,N; owner 2026-09-22 | A01,A41,A101 |
| R03 | B | Expose only pod setup, pod config, pod doctor and pod status as public command families. Planning, implementation, explanation, steering and continuation remain skill behaviors. Internal operations install no additional global executables. | F3,11; PH | A35,A36,A42 |
| R04 | B | Maintain one canonical inline skill policy with generated host metadata and relevant-on-demand references. Invocation preserves the conversation and must not set coordinator model/effort or use Claude context: fork. | F2–3,11; PA,H | A01,A32,A43 |
| R05 | B | Normal invocation inspects objective, criteria, rules and consequential assumptions, then proceeds within existing authorization. Use a compact execution brief when useful; trivial work requires neither workers nor milestone ceremony. | F3; PA | A02,A04 |
| R06 | H | Plan-only work permits useful host-permitted investigation but no implementation workers or product edits. Assess tooling side effects first. Describe behavioral restraint accurately. Plan-then-execute avoids another approval unless a real boundary changes. | F3; PA,E | A03,A04,A44 |
| R07 | B | Substantive plans identify criteria, challenged assumptions, coordinator/worker responsibilities, dependencies, editing boundaries, verification and revision triggers. Decomposition is provisional; material scope/acceptance changes require explicit revision. | F3,7–9; PA,J | A30,A45 |
| R08 | B | Choose tools, direct session work or delegation before a worker model. Assess each assignment's complexity, risk, size, uncertainty, verifiability, capabilities and context availability independently of its parent. | F4; PA–B | A02,A05,A06,A46 |
| R09 | B | Keep role, agent application, model identity and effort distinct. Do not encode a universal intelligence/cost ladder or permanently expensive reviewer role. | F4,8; PA–B,F | A05,A28,A46 |
| R10 | H | Distinguish preferred, approved and usable-now routes. Approval binds the resolved agent/model and redacted native account identity plus restrictions; a free-form label is not account proof. The model alias remains the human-facing label. Discovery, aliases, repository files and worker reports cannot create approval. Reuse valid existing grants. | F4; PB | A09,A10,A47 |
| R11 | B | Resolve personal YAML, optional project overrides and current-task preferences into one effective table with provenance. Derive agent from the model entry. Support validation and explicit editing; display must not write configuration. | F3–4; PB,H | A05,A32,A48,A82 |
| R12 | H | Parse bounded YAML safely. Reject duplicate keys, invalid types, unsupported schema/policy fields, arbitrary tags, executable includes and resource-exhausting structures. Preference data must never execute shell commands. | F4; PB | A49 |
| R13 | H | Merge soft preferences by specificity and hard restrictions restrictively. Resolve keyed entries consistently; local lists cannot broaden restrictive lists. Project/task files cannot expand model/provider approval, spending authority, destructive permissions or hard ceilings. | F4; PB | A09,A12,A48 |
| R14 | B | Supply five starter preferences as pending capability-validated recommendations. Offer Opus/Fable as additional approvable alternatives. Exact identifiers/efforts depend on the approved installed route; names are not permanent rankings. | F4; PB,P | A10,A47,A50 |
| R15 | B,H | Start with the matching preference, filter infeasible routes and depart only for a concrete recorded reason. Strict pins prohibit substitution. Exceptions do not edit saved preferences. Never reroute to circumvent a provider safety refusal. | F4–5; PB–C | A05,A06,A07,A38 |
| R16 | H | Bind each assignment to immutable configuration/catalog revisions, assessment and route. Preferences affect future assignments; hard revocations apply at the next enforceable mutation boundary. Invalid configuration blocks a pending replay or new dispatch without blocking read-only diagnosis or binding a completed native effect; never silently use an older revision. | F4; PB,J | A11,A12,A51 |
| R17 | B | Provide dispatch-free routing preview and deterministic replay from supplied assessment, policy, capability and quota snapshots. Helpers validate/explain decisions; they do not claim to understand arbitrary tasks through numerical lookup. | F2,10; PJ | A36,A52 |
| R18 | B,H | Read supported metadata for the actual authentication/account route. Use one redacted native identity as the approval, quota and grant key; identity and authentication/billing proof must come from the same selected account context. A present but incomplete native default cannot fall through to another login. Re-read that joined evidence at the final reservation or pending-replay mutation boundary and never relabel an observed account as the requested one. Preserve buckets/windows, observed consumption, resets, timestamps, source, freshness and uncertainty. Prefer notifications/bounded cached reads. Do not scrape credential stores/undocumented endpoints or use provider inference APIs for workers. | F2,5; PC | A13,A14,A15,A16,A39,A53 |
| R19 | B,H | Apply configurable quota heuristics: initially 20% low, 5% critical, 60-second freshness. Assess bounded work individually. Unknown quota allows at most one outstanding logical assignment on the objective's overlapping account route; it does not require a census of foreign runtime resources or make historical work in an independent objective occupy the slot. Exhaustion requires positive renewed-availability evidence. Avoid oscillation and account rotation. | F5; PC; owner 2026-09-22 | A13,A14,A15,A16,A54 |
| R20 | H | Separate model approval from paid usage, premium modes and reset authority. Spending grants must be explicit, scoped, bounded, visible and bound to the same redacted native account identity established for launch. Exclude routes whose billing eligibility could cause unauthorized charges; another login or account label cannot supply billing proof, and uncertainty disclosure is not spending permission or a zero-charge guarantee. | F5; PC | A17,A55 |
| R21 | H | Reset consumption requires the exact grant, supported idempotency, durable operation identity and subsequent quota readback. An uncertain response cannot authorize another redemption. Passive commands consume nothing. | F5; PC | A17,A22,A56 |
| R22 | B,H | Account for coordinator work and preserve practical integration, verification, recovery and reporting headroom. Checkpoint before foreseeable exhaustion. Changing workers does not move/replenish the coordinator. A supported coordinator model change must be explicit, session-scoped and visible. | F5,7,9; PC | A31,A39,A57 |
| R23 | I,H | Resolve the applicable Orca executable/runtime through installed Orca discovery, load the version-matched orchestration guide, and validate advertised read/start/request-recovery contracts, backend identities and optional terminals. Do not silently substitute runtimes, execution hosts, accounts or direct provider worker APIs. | F2,5,11; PA,I; owner 2026-09-22 | A08,A34,A58,A101 |
| R24 | H | Validate approval, capability, effort, exact selected-account identity/billing, host restrictions and fan-out controls before dispatch and same-request pending replay. Repeat the selected-account join immediately before the effect even when quota windows are absent. Compare requested/confirmed effective configuration afterward and at observable changes. Missing proof remains unknown; policy violations hold/quarantine the result. Completed/absent request diagnosis stays observational, and post-launch detection cannot replace necessary pre-launch spending protection. | F4,6–7; PA–B,J | A08,A20,A59 |
| R25 | B | Treat accepted input, started reasoning, native settlement and accepted output as different native observations. Silence/lost responses do not prove failure, justify resending input or authorize replacement work. Recover the same immutable admission through Orca request-show: record a completed receipt, join a pending request with Orca's UUID, or inspect exact Run/Task/Dispatch identity after an absent result; missing, ambiguous or contradictory evidence holds and never starts fresh. | F7,9; PA,L; owner 2026-09-22 | A22,A60,A102 |
| R26 | H | Use zero workers when sufficient, default logical fan-out two, ordinary one–three and justified expansion to three. Four–eight needs an explicit reasoned grant bound to objective, Run, plan/decomposition revision and limit. Reject above eight. Free hardware alone does not justify workers or substitute compute restrictions for decomposition; Pod does not infer physical capacity. | F6; PD; owner 2026-09-22 | A18,A19 |
| R27 | H | Count every Pod-managed investigator, reviewer and authorized descendant as a logical assignment under the same objective ceiling across Runs. Children need explicitly delegated authority and their own reservation. Do not claim that Pod observes hidden provider fan-out or physical occupancy. Ultra/ultracode are not ordinary effort values. | F6; PD; owner 2026-09-22 | A16,A19,A20,A21 |
| R28 | H | Serialize admission under the objective lock and persist one logical reservation before each native start. Reserved and unresolved own requests remain outstanding; an already-bound assignment frees a slot only from exact matching native settlement, regardless of retained/released terminal state. Do not enumerate all Runs or fleet workers, reconstruct foreign/history resources, infer terminal liveness, or deduplicate occupancy. Unknown quota is conservative over the objective's own overlapping logical admissions. Treat authoritative native `capacity_full` as durable `deferred` with no binding or blind retry; contradictory or partial-effect evidence remains unresolved. Provider quota reservations are advisory and paid grant units remain spent. | F5–6,9; PC–D,L; owner 2026-09-22 | A18,A21,A22,A61,A105 |
| R29 | H | Establish one authoritative coordinator per objective. Adoption reconciles pending effects/native authority before dispatch. Before Governor preparation, admission, execution or any journal mutation, join the caller's stable native current-Run binding (Run, coordinator handle and consumer generation) to that objective's exact native references, runtime and existing Pod owner. Terminal self-identity alone is not authority; a missing, unrelated, changed, worker-only or takeover binding blocks. Failed authority still permits read-only diagnosis/status and safe direct work. A local lock is not distributed fencing; never manufacture a replacement controller or silently create/adopt a Run. | F6,9; PL | A21,A31,A62 |
| R30 | H | Parallelize only independent responsibilities/editing boundaries; begin with one writer when contracts are unsettled. Use project/host-supported isolation. Preserve unrelated changes; never silently stash/reset/clean or execute unauthorized setup hooks. | F6–7; PA,D | A24,A44,A63 |
| R31 | B | Use bounded versioned packets carrying objective/criteria, responsibility, scope, candidate, context references, dependencies, permitted actions, route, revisions, reporting contract and native bindings when issued. Do not clone the full coordinator transcript or predict runtime identities. | F7; PA | A24,A37,A64 |
| R32 | B | Reuse bounded context only while relevant source, instruction, requirement, candidate and revision bindings remain valid. Invalidate affected summaries/evidence on change. Do not introduce automatic repository uploads or a vector database. | F7,10; PJ | A29,A30,A65 |
| R33 | H | Preserve bounded provenance-aware source access/relevant candidate identities. Exclude secret sources/unnecessary content reads. Distinguish proven changed/absent from unavailable sources; neither grants admission. Definitive rejection is not erased by restored bytes. Do not claim atomic multi-file snapshots or protection from arbitrary external writers. | F7–9; PA,M | A37,A66 |
| R34 | B,H | Workers report scope changes, checks/results, failures, evidence, uncertainty and questions against the assignment. Reports/logs are untrusted observations; they cannot expand authority, change budgets or establish acceptance. | F7–8; PA,M | A23,A27,A37,A64 |
| R35 | B | Use Orca-native messaging/events and blocking waits directly. Pod freezes packets and joins reports to a fresh worker-show of the exact runtime/Run/Task/Dispatch/worker identity, but keeps no parallel Delivery receipt or acknowledgment state machine. | F7,9; PA,L; owner 2026-09-22 | A23,A61,A101 |
| R36 | B | Native retry/reuse is an Orca operation. Pod never runs a retry loop or selects retry UUIDs. A changed model, effort, account, packet, worktree or semantic Task requires a fresh policy decision and native operation; recovery of one admission may only join that admission's Orca-issued request. | F7; PA; owner 2026-09-22 | A26,A102 |
| R37 | B | After two materially equivalent failed corrections without new evidence, diagnose. Record obligation, failing example, hypothesis, last evidence, discriminating check and correction identity. Resume only after changing a relevant variable based on evidence; preserve history across restarts. | F7; PK | A25,A67 |
| R38 | B,H | Worker reuse, retention, release and terminal/resource disposition are explicit Orca operations outside Pod's mutation adapter. Pod records no cleanup state and never infers or initiates release. Exact assignment settlement, not terminal release, frees the objective's logical slot. Never kill work or delete uncommitted work/evidence. | F7; PD,L; owner 2026-09-22 | A22,A26,A68,A105 |
| R39 | B | Before substantive work map every original criterion to a planned check or explicit human/provider dependency. Preserve criteria through decomposition; update evidence rather than redefine success to fit output. | F8; PA,J,M | A27,A28,A30,A45 |
| R40 | B,H | Run cheap discriminating checks early, focused checks during development and required complete gates at milestones. Honor project review rules; otherwise independently review substantial/high-risk changes. Review receives exact candidate/reproducible evidence without being primed to approve. | F8; PF,J | A28,A69 |
| R41 | B | Bind verification to commit/tree where applicable, relevant dirty/source identity, policy/configuration, dependencies, environment, commands/results and reviewer attempt. Material changes invalidate affected evidence. Reuse unaffected proof only when bindings/project rules permit. | F8; PM | A27,A29,A70 |
| R42 | H | Keep implemented, locally verified, independently reviewed, hosted proof complete, accepted, merged and deployed/released distinct. The coordinator assesses the objective under project acceptance authority; worker success/synthetic fixtures cannot promote later labels. | F8; PA,M | A27,A28,A71 |
| R43 | B | Final reports state objective, achieved criteria, exact blockers, candidate/check evidence, failures, material route exceptions, uncertainty and release state. Partial results remain partial; unobserved usage/cost remains unknown. | F8,14; PA,M | A39,A71 |
| R44 | I,H | Persist only compact `pod-context/v2` policy/evidence: admissions (`reserved`, `bound`, `unresolved`, `closed`, `deferred`, `legacy_hold`), owner, checkpoint, interventions, source rejections and immutable legacy archive references. Store native IDs and request UUIDs as references with route/packet/grant/request/effective evidence, never copied native lifecycle status. `deferred` records an authoritative no-start decision, not a successful binding. Use private host-local storage, explicit versions and atomic bounded writes; keep live identifiers/private material out of Git. | F9; PA,L; owner 2026-09-22 | A31,A37,A41,A72,A104,A105 |
| R45 | B | Recovery selects the objective and reads native state before action. Native request-show governs completed/pending/absent request recovery; exact Run/Task/Dispatch readback may bind only one matching attempt. Missing, ambiguous or unresolved own evidence keeps its logical reservation outstanding and never justifies relaunch. There is no Pod retry, release or lifecycle loop and no daemon/automatic conversation migration. | F9; PL; owner 2026-09-22 | A22,A31,A62,A72,A102 |
| R46 | B,H | Steering creates a new revision, reconciles affected assignments at safe native boundaries, preserves useful unaffected work and prevents obsolete reports/proof satisfying revised work. Material acceptance changes require explicit authorization. | F8–9; PJ,M | A30,A45,A70 |
| R47 | B,H | Record relevant observed outcomes and suggest preferences only after a meaningful pattern. Never silently rewrite preferences, infer savings from model labels or launch paid A/B experiments automatically. | F10,14; PJ | A39,A73 |
| R48 | B | Read-only tools show effective policy/provenance, approval, runtime/capability/version issues, quota visibility, compact fresh native work/verification context and `migration_required`. Ambiguous status requires selection. Reads must not create/migrate state, repair integrations, run hooks/models, dispatch or spend. | F3,10; PH,J; owner 2026-09-22 | A36,A42,A51,A74,A104 |
| R49 | B,H | Local setup enrolls the project/reconciles owned skills. Global setup installs user skills without changing the current project. Reuse compatible global installations; diagnose duplicate/shadowed copies. Remove redundant local copies only during local setup when owned/unchanged; preserve modified copies. | F3,11; PH | A32,A33,A36,A43,A75 |
| R50 | I,H | Support explicit isolated installation from a reviewed checkout on Linux with Python 3.13+. Bootstrap installs only into the selected environment and reports the command. Normal setup performs no implicit machine installation, automatic PATH repair, Orca installation, billing change or project hook. | F3,11; PI | A34,A76 |
| R51 | B,H | RETIRED 2026-09-21. Supported execution environment: Linux. The skill's instruction format is portable; its executable dependencies are not, and no support is promised elsewhere. Unknown ownership or an unsupported transport still fails conservatively, with no silent local fallback. | F11; PI | A34,A58,A62 |
| R52 | B | Preserve Git history while renaming repository/product Pod/pod, module/executable pod and distribution j3w1-pod. Update package/import/CI/install/docs references. Breaking release exposes no legacy aliases, compatibility shims or dual controller mode. | F11; PG | A35,A77 |
| R53 | H | Bound migration by an ownership manifest. Reconcile/explicitly checkpoint legacy work and preserve necessary recovery evidence before destructive cleanup. Retain reviewed pre-cutover Git reference/required archive. Imports translate validated concepts only, never broaden authorization or permit competing coordinators. | F11; PG,L | A22,A24,A35,A77 |
| R54 | H | Follow registered host paths/worktree mechanisms/native profiles/admin boundaries. Canonical pod registration/relocation uses owner-managed mechanism. Coding must not change global policy, weaken protections/gates or perform unauthorized deployment/provider/publication actions. | F11–12; PI | A34,A77,A78 |
| R55 | H | Block release until required live core integrations have candidate-bound passing evidence: Claude Code and Codex on Linux, each with real Orca delegation through the production adapter. Required synthetic, packaging, hosted, review and project gates also apply. Unsupported optional capabilities fail conservatively and cannot be marketed as verified. | F12,14; PI | A34,A79 |
| R56 | B,H | RETIRED 2026-09-21. No comparative benchmark is a prerequisite. Historical wording: evaluate direct-agent, native-Orca and Pod on matched candidates/criteria and representative tasks. Report acceptance, defects, interventions, delegation, retries, elapsed time, context/dispatch volume, observed usage, visibility, sample size/confidence. Offline CI is normal; live experiments need bounded authorization. No universal savings target. | F14; PC,J | A39,A73,A80 |
| R57 | I | Retain Python 3.13+, one bundle-as-package layout and standard-library facilities where practical. Reuse applicable safety invariants/tests; remove obsolete controller/installer machinery with documented replacement coverage. Build, install and test the frozen candidate on Linux and follow repository implementation/review ownership. | F2,11–12; PA,I | A35,A76,A81 |
| R58 | B,I | Keep README approachable, implemented-only and linked to detailed contracts. Maintain/package one skill policy source and references. Document capability limits, breaking migration and actual evidence; historical proposals are not competing active instructions. | F2,11,14; PN–O | A40,A81 |
| R59 | H | Preserve project authority over source selection, checks, review, acceptance, merge and release. Repository instructions may constrain work but cannot expand personal provider/spending approval. Explicit user scope/host rules remain authoritative; reports/configuration cannot manufacture grants. | F1,8,11–12; PF–G,O | A09,A37,A71 |
| R60 | B,I | Install through the existing agent-skills ecosystem. Publish one discoverable `skills/pod` bundle whose `SKILL.md`, references, helpers and resources are the same authoring source the Python distribution packages, with a parity check rather than divergent copies. Document a release-pinned source form and a Node-free install from the reviewed release. Do not create an npm package, require a publication registry, or recommend an unrelated package. | O1 | A82,A83 |
| R61 | B,H | The installed skill carries its own first-party helpers. Loading it never installs tools or dependencies, edits shell profiles, installs Orca, changes billing or overwrites project files. Declare the actual prerequisites and supply one explicit user-space bootstrap step when one is needed. Never depend on a source checkout, an editable install, `PYTHONPATH`, a broken link or a path outside the distributed skill. | O2 | A83,A84 |
| R62 | B,H | Establish each route against the installed runtime instead of demanding a universal attestation. Load the installed Orca guide before native effects and use its recovery semantics. Distinguish an enforceable control, supported runtime observation, owner route configuration and unavailable metadata, claiming no more than those controls prove. Join the personal approval's redacted identity and authentication/billing evidence within the same runtime-selected account context; a present native default must supply its own usable auth proof, and login fallback is permitted only when that native default context is absent. An unresolved, partial, mismatched or freshly rotated route that could incur an unauthorized charge must not launch or replay; unknown optional quota metadata is disclosed, not treated as installation failure. | O3; owner 2026-09-22 | A85,A86,A101,A102 |
| R63 | B | Before a Pod-mediated push, pull-request update, workflow dispatch, rerun, remote diagnostic, merge, release, deployment or cancellation, return an explainable ALLOW, REUSE or DEFER from durable objective records. Revised 2026-09-21: WARN is an annotation, never a fourth state, and a duplicate action attaches to the running one or reuses its evidence rather than deferring. Defer a superseded candidate and validation that is premature while the unit is unsettled; permit a justified bounded remote diagnostic. A scoped efficiency exception replaces the generic override and never lifts an authorization, spending or correctness restriction. These controls cover Pod-mediated operations, not arbitrary shell commands. | O7,O9 | A87,A88 |
| R64 | H | Derive release authorization from an owner record naming the exact candidate, tree and scope. Report technical readiness accurately and separately; passing checks never grant permission, and a projection never withholds a readiness fact it has evidence for. | O8 | A89 |
| R65 | B,H | Keep host integration optional. Pod works on a suitable Linux environment without the owner's host tooling, paths, accounts or evidence, detecting and respecting host policies when present. | O2 | A84 |
| R66 | B,H | Govern expensive Pod-mediated remote actions with one deterministic kernel inside the existing execution path, evaluated at boundaries with zero model calls and bounded state reads. Bind each request to a delivery unit's explicitly prepared candidate generation, which freezes commit, tree, base, workflow digests, verification commands, toolchain, environment and policy revision; judge effects, require configured local preflight, reuse compatible evidence, classify failures, journal admitted actions and reconcile lost responses read-only. ALLOW/REUSE/DEFER decisions consume objective-local logical assignment evidence when readiness depends on delegated work, never an all-Run or worker-fleet census. Before preparation, admission, execution or any journal mutation, require the existing Pod owner plus a stable native current-Run/coordinator/generation binding to one of the objective's exact Run references on the same runtime. Read-only status remains available without mutation authority. Report enforcement as advisory unless host controls prove otherwise; project policy may only narrow authority; Orca keeps worker lifecycle and project governance keeps merge, release and deployment. | O9; owner 2026-09-22 | A90–A99,A101,A105 |

## Public interfaces

These contracts elaborate R03, R11 and R48–R50, not a second authority.

- `pod setup [--global] [--json]`: local enrollment/integration by default; global
  skill installation only with the flag, without current-project changes.
- `pod config [--check] [--json]`: render/validate effective policy, no writes or
  inference calls; invalid validation returns an actionable unsuccessful result.
- `pod config --edit [--scope personal|project]`: default personal; project explicitly
  selected. Validate after editing and preserve invalid user edits while blocking dispatch.
- `pod doctor [--json]`: read-only installation, scope, capability, account visibility,
  policy and drift diagnostics. Active probes are not a doctor side effect.
- `pod status [--run RUN] [--json]`: native status plus decisions/verification; require
  selection when ambiguous.
- Skill: Codex `$pod ...`, Claude Code `/pod ...`; no portable `@pod` convention.
- Private helper: bounded structured operation through the installed package interpreter;
  validated operation-specific inputs/outputs, no arbitrary shell-command field, no
  additional global executable, scheduler or provider worker launcher.

## Configuration and defaults

R10–R16 use personal Linux `${XDG_CONFIG_HOME:-~/.config}/pod/config.yaml`,
optional project `.pod/config.yaml`.
For disposable validation, an absolute `POD_CONFIG_HOME` may process-locally replace only
the personal Pod directory containing `config.yaml`, and an absolute `POD_STATE_HOME` may
process-locally replace only Pod's state directory. Defaults remain unchanged; these
overrides do not replace agent or Orca profile environment variables and do not expand
project YAML authority.
YAML is editable authority; Markdown tables are generated. No live identifiers,
account credentials or personal machine paths belong in committed examples.

`pod/v1` sections:
- `models`: stable human-facing alias, agent, exact model identity, redacted native account
  identity, approval and billing restriction; optional effort/capability/data-location and
  approval-provenance restrictions. Account values are lowercase SHA-256 digests observed by
  `pod doctor`, never free-form labels or raw provider identifiers.
- `routing`: five complexity rows, model alias and effort; derive agent.
- `policy`: admission concurrency, quota, billing, delegation and review controls.
- Optional project context references for enrollment/verification, without execution
  or personal authorization grants.

Personal approval is separate from repository overrides. Changed model identity cannot
inherit approval just because its alias is unchanged. A missing personal file displays
a pending template without creating it.

| Complexity | Starter agent/model preference | Effort |
| --- | --- | --- |
| Trivial | Codex / Luna | low |
| Simple | Claude Code / Sonnet | medium |
| Standard | Codex / Terra | medium |
| Complex | Codex / Sol | high |
| Very complex | Codex / Astra | high |

All entries remain pending until explicitly approved against the real route. Opus/Fable
are pending alternatives, not mandatory defaults. IDs such as gpt-5.6-luna,
gpt-5.6-terra, gpt-5.6-sol and gpt-6-astra are onboarding suggestions, not access proof.
xhigh requires evidence/support; max is exceptional/bounded; Ultra/ultracode are not
ordinary effort values. Default worker ceiling 2; ordinary maximum 3; explicitly granted
exceptional maximum 8. Quota low 20%, critical 5%, freshness 60 seconds. Unknown quota
conservative, extra usage/reset credits require explicit authorization. Child delegation
requires coordinator authorization; independent review for substantial/high-risk work
unless project rules are stricter. Worker retention and idle disposition have no Pod timer;
they follow explicit Orca operations. No-progress diagnostic threshold is two equivalent
failed corrections without new evidence.

## Structured contracts

R16–R18, R28, R31, R34, R41 and R44–R46 define bounded records with explicit
`pod-.../v1` schemas and stable content identity when referenced:

| Record | Required information |
| --- | --- |
| Assessment | Method, responsibility, complexity, risk, size, uncertainty, verifiability, capabilities, relevant context, reason. |
| Route decision | Assessment; preferred/requested/effective route; policy digest/catalog revision; quota/capabilities; authorization references, provenance, exception reason. |
| Quota snapshot | Provider/account-route/bucket, applicable windows, observed values, resets, timestamp, source, freshness/confidence, explicit unknowns. |
| Worker packet | Objective/criteria, responsibility, scope/actions, candidate, bounded context, dependencies, route, revisions, report contract. |
| Admission/effect receipt | Stable operation identity/intent, exact native request/Run/Task/Dispatch/worker identities when issued, requested/effective configuration, observed effects, reconciliation result. |
| Worker report | Assignment/attempt/candidate, claimed outcome, scope/files changed, checks/results, failures, evidence, uncertainty, questions. |
| Verification evidence | Criterion, candidate/source/config/dependency/environment, check/reviewer, command/result, timestamp, status, sanitized reference. |
| Checkpoint | Objective/criteria, plan/candidate/policy revisions, native references, assignments, pending questions/effects, verification gaps, next safe action. |

Freeze packet body before launch. Join native-issued Dispatch/worker identities through
native preamble/admission afterward; never predict IDs or rewrite the frozen packet.
Reserve logical fan-out and record intent under the applicable admission lock before a native
effect. Reconcile to proven binding without counting reservation and Dispatch twice.
Never hold a lock across waiting for worker admission that needs the same lock.

Metadata is capability-gated for actual authentication routes. Codex documented
`model/list`, `account/rateLimits/read` and reset consumption are potential supported
interfaces, not installed-account proof. Provider adapters are metadata/authorized-credit
operations only; all worker inference/lifecycle remains Orca-native.

## Acceptance scenarios

These are expected outcomes, not evidence. A01–A39 retain identities and clarify
expectations; A40–A82 close reconciliation coverage gaps. Parameterized tests may cover
multiple scenarios. Behavioral/live claims cannot be certified by checking document text.

| ID | Required result |
| --- | --- |
| A01 | Invoke Pod in either host: the same conversation remains coordinator with context and coordinator configuration preserved. |
| A02 | Trivial mechanical work uses tools/current session with zero unnecessary workers or milestone ceremony. |
| A03 | Plan-only permits appropriate investigation but no implementation worker or product edit. |
| A04 | Plan-then-execute proceeds without redundant approval; real scope/authority changes stop dependent actions. |
| A05 | A suitable approved preference is selected without unexplained deviation. |
| A06 | Unsuitable/unavailable preference yields an explained feasible approved alternative or precise blocker. |
| A07 | An unavailable strict pin produces no silent substitution. |
| A08 | Effort clamping, mismatch or missing effective proof stays visible; prohibited configurations cannot receive valid acceptance. |
| A09 | Project expansion of models, providers, spending, destructive permissions or hard capacity is rejected. |
| A10 | New discovery or changed alias identity never inherits approval. |
| A11 | Preference edits affect later assignments; active attempts retain immutable route/revision. |
| A12 | Revocation blocks subsequent enforceable unauthorized actions and safely reconciles without erasing effects. |
| A13 | Low quota is task-aware for bounded versus uncertain work; 5% is not automatic stop or cost prediction. |
| A14 | Unknown/stale quota is disclosed and constrained to conservative account-route admission. |
| A15 | Exhausted applicable bucket serves no new request until supported renewed-availability evidence. |
| A16 | Shared quota decisions include known provider consumption and the objective's own overlapping logical admissions without reallocating the same remainder or scanning foreign workers. |
| A17 | Paid fallback/reset availability causes no consumption without the exact applicable grant. |
| A18 | Five ready Tasks at logical fan-out two admit two, block the third by policy, and admit the next useful Task after exact assignment settlement even if its terminal is retained. |
| A19 | Three requires justification; four–eight requires bound grant; above eight rejected. |
| A20 | Worker-initiated delegation is refused unless authorized; each Pod-managed descendant needs its own objective reservation, without a claim that Pod observes hidden provider fan-out. |
| A21 | Concurrent managed admissions serialize objective-local reservations and do not exceed logical fan-out; stable native coordinator authority is still required. |
| A22 | Lost start/request responses retain their logical reservation and recover the same immutable admission through exact Orca request/native identity without a blind replacement or repeated semantic start. |
| A23 | Native duplicate/delayed messaging remains Orca-owned; Pod report ingestion joins a frozen packet and exact fresh Dispatch identity without a parallel acknowledgment ledger. |
| A24 | Overlapping writes/existing dirtiness isolate or serialize while preserving owner changes. |
| A25 | Two equivalent failed corrections trigger diagnosis/discriminating check before another attempt. |
| A26 | New Tasks get fresh sessions; compatible corrections may reuse; unsupported route changes get fresh bound attempts. |
| A27 | Worker success with failed required checks leaves objective incomplete. |
| A28 | Substantial/high-risk changes receive candidate-bound independent review, including stricter project rules. |
| A29 | Material post-review change invalidates affected proof; unaffected reuse needs valid bindings/project rules. |
| A30 | Steering revises/reconciles affected work, preserves useful unaffected work and excludes obsolete results. |
| A31 | Interruption supports native-state adoption/checkpoints without background-reasoning claims. |
| A32 | Global/local coexistence preserves project policy and diagnoses duplicate/shadowed/mismatched skills. |
| A33 | Repeated setup is idempotent; global setup inside a project leaves the project unchanged. |
| A34 | RETIRED 2026-09-21 with R51. Linux establishes one correct runtime/state owner without phantom guarantees. |
| A35 | Breaking cutover leaves only Pod public surface while preserving history/required evidence. |
| A36 | Ordinary config/status/doctor and simulated policy operations do not call models/hooks/dispatch/spending/hidden repair. |
| A37 | Secret-bearing context/malicious reports do not expose protected material or promote observations into authority. |
| A38 | No fallback intended to bypass a safety refusal. |
| A39 | Unobserved tokens/cost remain unknown; worker counts/model labels do not become savings/provider-compute claims. |
| A40 | Source dispositions and requirement/scenario references have no duplicate/orphan IDs, unclassified requirement, contradiction or parallel historical authority. |
| A41 | No competing native task database, scheduler, autonomous reasoning/restart loop, dashboard or marketplace. |
| A42 | Installed CLI has exactly four public families with text/JSON contracts; private operations add no global commands. |
| A43 | Host integrations derive from one policy, use verified discovery roots, stay inline and never override coordinator model/effort. |
| A44 | Side-effectful read-only-labelled tooling is withheld in plan-only mode; planning output obeys host rules. |
| A45 | Execution brief/revised acceptance map covers each original criterion and human/provider dependency without user-authored milestone file. |
| A46 | Small high-risk, large repetitive and mixed-complexity assignments demonstrate method-first independent assessment. |
| A47 | Onboarding is pending and model-call-free; reuse valid approval; discovery grants neither approval nor spending. |
| A48 | Layered configuration preserves keyed identities, restrictive lists, derived agent and personal/project/task provenance. |
| A49 | Duplicate/type/unknown-field/executable-tag/include and oversized/recursive YAML fails safely without execution. |
| A50 | Starter recommendations resolve actual capability; unsupported names/efforts pending/blocked, including Opus/Fable billing. |
| A51 | Invalid edited config blocks dispatch but permits diagnosis/authorized owned-work reconciliation without old-config fallback. |
| A52 | Captured assessment/policy/capability/quota fixtures replay route/reasons without inference/native mutation. |
| A53 | Missing/unsupported metadata preserves unknowns/source limits without credential/undocumented-endpoint scraping. |
| A54 | Refresh stale metadata at material boundaries without daemon; marginal change causes no oscillation or productive-worker killing. |
| A55 | Wrong-scope/account/action/bounds/validity spending grants fail; applicable grants authorize only bounded action. |
| A56 | Uncertain reset keeps logical idempotency identity and reconciles/readbacks without a second credit. |
| A57 | Worker-provider changes leave coordinator unchanged; headroom/checkpoints explicit and no permanent setting edits. |
| A58 | Current native workers without terminals use worker identity/lifecycle APIs; missing terminal alone is not failure. |
| A59 | Missing required pre-dispatch billing/fan-out assurance rejects before launch; requested settings alone are not effective proof. |
| A60 | Accepted but unproven submission triggers observation, not automatic Enter/resend/replacement/acceptance. |
| A61 | Faults around reservation/start/request receipt preserve uncertainty, exact identity and request recovery without double-counting or a duplicate start. |
| A62 | Missing caller, conflicting ownership or a partial handover blocks delegation; valid adoption preserves the work without a proxy coordinator. |
| A63 | Unsettled contracts start one writer; project isolation/hooks honored without weakening host protection. |
| A64 | Packets/reports enforce bounded fields, criterion/candidate/scope/runtime-issued identity; reject transcript cloning and report permissions. |
| A65 | Relevant files/instructions/requirements/candidate changes invalidate context; unchanged bound context can be reused. |
| A66 | Changed/absent/redirected sources differ from unavailable reads; definitive rejection survives restoration; unrelated dirty paths do not grant sensitive reads. |
| A67 | Restarted correction history recognizes equivalence; rewording is insufficient but discriminating evidence permits changed attempts. |
| A68 | RETIRED 2026-09-22. Terminal retention/release is Orca lifecycle state and no longer determines Pod logical fan-out; exact assignment settlement is the replacement. |
| A69 | Cheap discriminating checks precede expensive work; independent review receives exact candidate and reproducible unprimed evidence. |
| A70 | Source/config/dependency/environment/candidate changes invalidate affected proof; no fabricated Git metadata and explicit alternative source binding. |
| A71 | Completion labels/final report follow evidence/project authority; failures, blockers, route exceptions and unresolved native references remain visible. |
| A72 | Interrupted versioned checkpoints recover privately and atomically; retention/native-first recovery protect unresolved effects/evidence. |
| A73 | Feedback may suggest but never auto-edits preferences or launches unauthorized live work. |
| A74 | Ambiguous status requires selection; compact counts, route reasons, quota confidence, verification gaps and next safe action. |
| A75 | Preserve modified copies; global setup never prunes local; local cleanup removes only redundant owned unchanged copies. |
| A76 | Reviewed isolated install works on Linux, rejects old Python, handles command paths, no automatic PATH/Orca repair. |
| A77 | Migration inventories ownership, preserves history/pre-cutover/uncertain evidence, removes legacy public behavior and prevents competing control. |
| A78 | Unregistered canonical relocation/prohibited placement refused; owner-managed registration is a separate cutover gate. |
| A79 | Missing live PASS for Claude Code or Codex on Linux, or for either delegation adapter, blocks release despite synthetic CI and a read-only doctor. |
| A80 | RETIRED 2026-09-21 with R56. No comparative benchmark is a release prerequisite. |
| A81 | Frozen build, isolated install, hosted Linux CI, incident discovery, compile/diff, independent audit and implemented-only docs bind the release candidate; retired gates have replacements. |
| A82 | Explicit config edit defaults personal; project scope explicit; preserve invalid edits and block dispatch, never silently restore/sanitize. |
| A83 | `npx skills add j3w1/pod --skill pod`, its per-agent and global forms, and a release-pinned source all install a bundle whose helpers run without a checkout. |
| A84 | A copied bundle runs from an unrelated directory with no `PYTHONPATH` and no source tree; a missing prerequisite prints one actionable step, never an import traceback or an invented payment requirement. |
| A85 | An approved subscription route whose optional quota bucket is unavailable still launches, with the gap disclosed; unknown billing or an unbacked paid route still fails closed before any native effect. |
| A86 | A skills-CLI-managed copy is detected and never overwritten, removed or claimed; repeating setup is a cheap no-op when correct, and global setup writes nothing into the current repository. |
| A87 | A superseded candidate and premature validation each defer with an explainable reason and next action; an identical running action is attached to and a passing result for the same candidate and context is reused; a necessary rerun after changed input proceeds. |
| A88 | A scoped efficiency exception bound to a personal grant softens only an efficiency deferral and never lifts an authorization, spending or correctness hold. |
| A89 | A complete gate without owner authorization reports readiness and withholds permission; authorization naming the exact candidate, tree and scope authorizes it, and an incomplete gate stays blocked regardless. |
| A90 | Several locally discoverable corrections in one delivery unit converge on the same branch and pull request; no intermediate correction crosses the remote boundary until its candidate passes the configured local preflight. |
| A91 | Two callers requesting identical validation concurrently produce one admitted execution; the other attaches to it. |
| A92 | A submission whose response is lost stays UNKNOWN, blocks a resubmission, and is settled only by provider readback; after a coordinator restart the unit's candidate bindings, evidence, decisions and pending effects remain recoverable. |
| A93 | A changed source, base, workflow, environment or policy opens a new candidate generation, and evidence bound to the previous one is not reused as current proof even on the same commit. |
| A94 | A remote-only question admits a bounded diagnostic naming its question, local limitation, check and stopping condition while the candidate is still converging, without pretending the candidate is release-ready; an unchanged repeat is answered from the record. |
| A95 | An unclassified remote failure is not retried; a code defect is recorded as a correction, and the third equivalent correction requires a diagnosis with distinct bounded evidence before validation resumes. |
| A96 | Supersedence cancels a pending, cancel-safe validation of the old candidate only when policy allows it, its result can never approve the newer candidate, and a pending deployment is never canceled by supersedence. |
| A97 | An independent or urgent delivery unit is admitted while another unit's workers, deliveries or corrections are unsettled. |
| A98 | A project file that relaxes the governor mode, widens the retry budget, enables cancellation, declares host control or adds an exception grant is refused as authority expansion. |
| A99 | The enforcement level is reported as advisory unless the owner's personal policy declares a host control, and it is never reported as a proven control. |
| A100 | A `pod-governor/v1` row carried forward by the upgrade is marked and never reused as evidence, because the migration cannot supply the candidate and context binding v1 never froze; one left pending or UNKNOWN defers as an unresolved effect rather than attaching to a run with no provider identity. |
| A101 | Pod exposes no private Delivery, cleanup, release, terminal or lifecycle mutation operation; Orca owns those states, while reports and Governor decisions use exact objective assignment evidence when needed. |
| A102 | Completed, pending and absent Orca request recovery binds the same immutable admission without a second semantic start; invalid UUID, changed authority/worktree, contradictory receipt or ambiguous native attempt holds. |
| A103 | RETIRED 2026-09-22. Fleet occupancy, resource-release reconstruction, foreign-history census and occupancy deduplication were removed because Orca and CE own runtime capacity. |
| A104 | Explicit v1 migration archives and hashes the untouched source, reads Orca without mutation, binds only exact proven effects, converts uncertainty to `legacy_hold`, atomically installs v2, and leaves v1 untouched on failure; doctor/status never migrate. |
| A105 | An authoritative native `capacity_full` refusal records durable deferred evidence with no successful binding, fleet audit or blind retry; a malformed or partial-effect refusal remains unresolved, and physical-capacity enforcement stays unavailable unless genuinely observed. |

## Implementation sequence and verification

P00: freeze baseline/runtime identities, keep/adapt/remove and owned-state inventories;
verify capabilities or specify conservative failures. P01: preserve this specification,
supersession/traceability and structured contracts; add executable failing scenarios
with meaningful failure causes. P02: configuration/approval/routing/preview/replay.
P03: native adapters, metadata/quota/billing, effective checks, admission/uncertainty.
P04: inline skill/direct/planning/adaptive packets, report joining and diagnosis/reuse.
P05: acceptance maps/context/evidence/checkpoints/steering/adoption/feedback/status.
P06: isolated installation, scope reconciliation, package/CLI rename and owned cleanup.
P07: frozen-candidate offline/fault/packaging/hosted/independent/live gates, matched
evaluation and owner-managed cutover. Phases are not fixed worker waves.

Python 3.13+, standard src packaging, minimal safe YAML dependency and standard-library
facilities where practical. Keep one canonical skill, ideally about 250 lines or less,
with relevant-on-demand references and generated host metadata. No empty abstractions.
Preserve source access, candidate binding, uncertain admission evidence and
requested/effective checks; Orca owns Delivery and lifecycle recovery.

One writer owns overlapping source. Repository-specific ownership: Sol/high implementation,
fresh Sol/xhigh final audit. Reviews bind a frozen candidate, report findings, and do not
repair. This is project governance, not a universal Pod model default.

Required checks: focused tests in development; complete unit/incident coverage and explicit
incident discovery; compileall and diff checks; frozen git-archive build; fresh isolated
wheel install/CLI smokes and a copied-bundle smoke on Linux; a skills-CLI install;
hosted candidate CI; independent final audit;
required live runtime cases; project acceptance. Retired bootstrap/controller gates must
map explicitly to superseded requirements and replacement tests; do not weaken checks.

Live core matrix: Codex on Linux and Claude Code on Linux. Each proves installation and
discovery, in-session coordination, authorized native execution and effective route,
supervision and lifecycle, verification and interruption/adoption. Real Orca delegation
through the production adapter is proved separately for each advertised worker adapter,
covering request construction, account and authentication selection, launch identity,
effective launch and request recovery. Native messaging, settlement and disposition remain
Orca evidence rather than Pod state. Missing live PASS for any required
combination blocks release despite offline PASS. Optional unsupported capabilities fail
conservatively and are not advertised operational.

Efficiency is governed by the bounded decision check of R63 and the waste governor kernel
of R66, not by a comparative benchmark. It reuses the records an objective already keeps
and returns ALLOW, REUSE or DEFER with an explainable reason and next action. Unknown stays
unknown; observed elapsed time is not provider compute or billed cost; counters report
reuse, deferrals, interruptions and cancellations without an estimate of minutes saved; Pod
claims no universal savings percentage.

Live exercises are disposable and bounded/authorized. Existing ordinary-project trials are
read-only. No purchase, reset-credit redemption, production mutation, deployment or
package-registry publication is authorized merely by the rewrite task. Actual release and
rename must honour host registration and repository authority. No compatibility period or
public dual interface. Supported execution environment: Linux.

## Evidence boundary at reconciliation

Baseline is clean main at the commit above. The historical reconciliation observation at that
baseline found Orca 1.4.205 advertising orchestration.contract.v1 and
worker-launch-preferences, Codex CLI
0.155.1 and Claude Code 2.1.278. Native help describes optional worker terminals.
These facts are not Pod lifecycle, quota, billing, permission/fan-out or recovery proof.
Implementation/tests, live core matrix, hosted CI, independent audit, acceptance, merge and
release were NOT_RUN for Pod at reconciliation. Current observations and candidate state are
reported in [pod-progress.md](pod-progress.md), not retroactively written into this record.
