# Pod specification

Status: the product requirements for the version `pod.__version__` declares on the same
commit. It is not a claim of implementation or verification; a completed test cannot
silently revise a requirement, and a changed requirement is revised here explicitly with
its scenario and coverage row.

Pod is an adaptive coordination policy for Orca. It turns the coding session already in
use into the coordinator, choosing tools, agents, models and effort for each part of a
task and adapting the plan until the objective is verified. Requirement types: B =
behavior, H = hard authorization, I = implementation.

## Scope and ownership

Orca is the single source of truth for Runs, Tasks, Dispatches, request recovery, worker
lifecycle, messaging, placement and terminal/resource disposition. Pod is a thin policy,
routing and evidence layer: it keeps a serialized admission seam and exact native
references, but it does not copy native lifecycle status or implement Delivery, cleanup,
liveness, terminal, release or retry state machines. The installed, version-matched Orca
orchestration guide and its recovery reference govern native mutation semantics. Orca mints
request UUIDs; Pod only records and reuses them for the same immutable admission. Host
policy, explicit user authorization and project governance retain their own authority.

Pod decides desired fan-out, not physical occupancy: one logical reservation per Pod
assignment, freed by exact native assignment settlement regardless of retained terminals.
Independent objectives do not census each other, and Pod never queries, infers or claims
physical capacity. Orca's documented effect-free preflight refusals (`task_not_found`,
`task_not_startable`, `inject_rejected`) create a durable deferred admission with no binding
and no blind retry. `runtime_error` proves nothing about effects and stays unresolved until
request and worker readback settle it; unknown, malformed, lost or partial-effect responses
remain unresolved.

Pod is developed against Orca 1.4.209. Capabilities are discovered from the installed
runtime per operation through its advertised contracts and help; no minimum version is
enforced, and a missing delegation capability blocks only delegation.

Pod Execution Spec is the recommended persistent objective input while direct objectives
remain first-class. `256k` is a 256,000-token policy upper bound. Issue and worktree source
bindings create no issue or worktree managers.

## Requirements

| ID | Type | Requirement | Scenarios |
| --- | --- | --- | --- |
| R01 | B | Maintain this requirement/scenario inventory as product authority. Explicitly revise changed requirements; implementation results cannot silently redefine them. | A38 |
| R02 | B | Keep the current conversation as coordinator. Orca is the sole runtime authority for Runs, Tasks, Dispatches, requests, placement, messaging, terminals/resources and lifecycle; projects own governance. Pod is policy, routing and evidence only. Helpers must not become a scheduler, competing native task/lifecycle database, autonomous controller or restart daemon. | A01,A39,A95 |
| R03 | B | Expose only pod setup, pod config, pod doctor and pod status as public command families. Planning, implementation, explanation, steering and continuation remain skill behaviors. Internal operations install no additional global executables. | A34,A40 |
| R04 | B | Maintain one canonical inline skill policy with generated host metadata and relevant-on-demand references. Invocation preserves the conversation and must not set coordinator model/effort or use Claude context: fork. | A01,A32,A41 |
| R05 | B | Normal invocation inspects objective, criteria, rules and consequential assumptions, then proceeds within existing authorization. Use a compact execution brief when useful; trivial work requires neither workers nor milestone ceremony. | A02,A04 |
| R06 | H | Plan-only work permits useful host-permitted investigation but no implementation workers or product edits. Assess tooling side effects first. Describe behavioral restraint accurately. Plan-then-execute avoids another approval unless a real boundary changes. | A03,A04,A42 |
| R07 | B | Substantive plans identify criteria, challenged assumptions, coordinator/worker responsibilities, dependencies, editing boundaries, verification and revision triggers. Decomposition is provisional; material scope/acceptance changes require explicit revision. | A30,A43 |
| R08 | B | Choose tools, direct session work or delegation before a worker model. Assess each assignment's complexity, risk, size, uncertainty, verifiability, capabilities and context availability independently of its parent. | A02,A05,A06,A44 |
| R09 | B | Keep role, agent application, model identity and effort distinct. Do not encode a universal intelligence/cost ladder or permanently expensive reviewer role. | A05,A28,A44 |
| R10 | H | Distinguish preferred, approved and usable-now routes. Approval binds the resolved agent/model and redacted native account identity plus restrictions; a free-form label is not account proof. The model alias remains the human-facing label. Discovery, aliases, repository files and worker reports cannot create approval. Reuse valid existing grants. | A09,A10,A45 |
| R11 | B | Resolve personal YAML, optional project overrides and current-task preferences into one effective table with provenance. Derive agent from the model entry. Support validation and explicit editing; display must not write configuration. | A05,A32,A46,A77 |
| R12 | H | Parse bounded YAML safely. Reject duplicate keys, invalid types, unsupported schema/policy fields, arbitrary tags, executable includes and resource-exhausting structures. Preference data must never execute shell commands. | A47 |
| R13 | H | Merge soft preferences by specificity and hard restrictions restrictively. Resolve keyed entries consistently; local lists cannot broaden restrictive lists. Project/task files cannot expand model/provider approval, spending authority, destructive permissions or hard ceilings. | A09,A12,A46 |
| R14 | B | Supply the five R74 complexity defaults as pending capability-validated recommendations and Fable as an alternative. The closed catalog fixes launch identities but never proves installed access, billing, effort or context capability. | A10,A45,A48,A118 |
| R15 | B,H | Start with the matching preference, filter infeasible routes and depart only for a concrete recorded reason. Strict pins prohibit substitution. The approved model, effort and account route is immutable for one assignment: never silently substitute it or ask a worker/user to select another model mid-attempt. Unavailability or startup failure holds/fails that attempt for a fresh policy decision. Exceptions do not edit saved preferences. Never reroute to circumvent a provider safety refusal. | A05,A06,A07,A36 |
| R16 | H | Bind each assignment to immutable configuration/catalog revisions, assessment and route. Preferences affect future assignments; hard revocations apply at the next enforceable mutation boundary. Invalid configuration blocks a pending replay or new dispatch without blocking read-only diagnosis or binding a completed native effect; never silently use an older revision. | A11,A12,A49 |
| R17 | B | Provide dispatch-free routing preview and deterministic replay from supplied assessment, policy, capability and quota snapshots. Helpers validate/explain decisions; they do not claim to understand arbitrary tasks through numerical lookup. | A34,A50 |
| R18 | B,H | Read supported metadata for the actual authentication/account route. Use one redacted native identity as the approval, quota and grant key; identity and authentication/billing proof must come from the same selected account context. A present but incomplete native default cannot fall through to another login. Re-read that joined evidence at the final reservation or pending-replay mutation boundary and never relabel an observed account as the requested one. Preserve buckets/windows, observed consumption, resets, timestamps, source, freshness and uncertainty. Prefer notifications/bounded cached reads. Do not scrape credential stores/undocumented endpoints or use provider inference APIs for workers. | A13,A14,A15,A16,A37,A51 |
| R19 | B,H | Apply configurable quota heuristics: initially 20% low, 5% critical, 60-second freshness. Assess bounded work individually. Unknown quota allows at most one outstanding logical assignment on the objective's overlapping account route; it does not require a census of foreign runtime resources or make earlier work in an independent objective occupy the slot. Exhaustion requires positive renewed-availability evidence. Avoid oscillation and account rotation. | A13,A14,A15,A16,A52 |
| R20 | H | Separate model approval from paid usage, premium modes and reset authority. Spending grants must be explicit, scoped, bounded, visible and bound to the same redacted native account identity established for launch. Exclude routes whose billing eligibility could cause unauthorized charges; another login or account label cannot supply billing proof, and uncertainty disclosure is not spending permission or a zero-charge guarantee. | A17,A53 |
| R21 | H | Reset consumption requires the exact grant, supported idempotency, durable operation identity and subsequent quota readback. An uncertain response cannot authorize another redemption. Passive commands consume nothing. | A17,A22,A54 |
| R22 | B,H | Account for coordinator work and preserve practical integration, verification, recovery and reporting headroom. Checkpoint before foreseeable exhaustion. Changing workers does not move/replenish the coordinator. A supported coordinator model change must be explicit, session-scoped and visible. | A31,A37,A55 |
| R23 | I,H | Resolve the applicable Orca executable/runtime through installed Orca discovery, load the version-matched orchestration guide, and validate advertised read/start/request-recovery contracts, backend identities and optional terminals. Do not silently substitute runtimes, execution hosts, accounts or direct provider worker APIs. | A08,A56,A95 |
| R24 | H | Validate approval, capability, effort, exact selected-account identity/billing, host restrictions and fan-out controls before dispatch and same-request pending replay. Repeat the selected-account join immediately before the effect even when quota windows are absent. Compare requested/confirmed effective configuration afterward and at observable changes. That post-start equality check detects mismatch but does not prove an opt-in strict, noninteractive provider startup before task delivery; without a native per-launch control, that guarantee remains unavailable. Missing proof remains unknown; policy violations hold/quarantine the result. Completed/absent request diagnosis stays observational, and post-launch detection cannot replace necessary pre-launch spending protection. | A08,A20,A57 |
| R25 | B | Treat accepted input, started reasoning, native settlement and accepted output as different native observations. Silence/lost responses do not prove failure, justify resending input or authorize replacement work. Recover the same immutable admission through Orca request-show: record a completed receipt, join a pending request with Orca's UUID, or inspect exact Run/Task/Dispatch identity after an absent result. Missing, ambiguous or contradictory evidence holds and never starts fresh; a later incomplete observation cannot erase a known request-identity conflict. | A22,A58,A96 |
| R26 | H | Use zero workers when sufficient, default logical fan-out two, ordinary one–three and justified expansion to three. Four–eight needs an explicit reasoned grant bound to objective, Run, plan/decomposition revision and limit. Reject above eight. Free hardware alone does not justify workers or substitute compute restrictions for decomposition; Pod does not infer physical capacity. | A18,A19 |
| R27 | H | Count every Pod-managed investigator, reviewer and authorized descendant as a logical assignment under the same objective ceiling across Runs. Children need explicitly delegated authority and their own reservation. Do not claim that Pod observes hidden provider fan-out or physical occupancy. Ultra/ultracode are not ordinary effort values. | A16,A19,A20,A21 |
| R28 | H | Serialize admission under the objective lock and persist one logical reservation before each native start. Reserved and unresolved own requests remain outstanding; an already-bound assignment frees a slot only from exact matching native settlement, regardless of retained/released terminal state. Do not enumerate all Runs or fleet workers, reconstruct foreign/history resources, infer terminal liveness, or deduplicate occupancy. Unknown quota is conservative over the objective's own overlapping logical admissions. Treat Orca's documented effect-free refusals (`task_not_found`, `task_not_startable`, `inject_rejected`) as durable `deferred` with no binding or blind retry, even without a UUID; hold `runtime_error` as `unresolved` until request and worker readback settle it; unknown codes and contradictory or partial-effect evidence remain unresolved. Provider quota reservations are advisory and paid grant units remain spent. | A18,A21,A22,A59,A97 |
| R29 | H | Establish one authoritative coordinator per objective. Adoption reconciles pending effects/native authority before dispatch. Before Governor preparation, admission, execution or any journal mutation, join the caller's stable native current-Run binding (Run, coordinator handle and consumer generation) to that objective's exact native references, runtime and existing Pod owner. Terminal self-identity alone is not authority; a missing, unrelated, changed, worker-only or takeover binding blocks. Failed authority still permits read-only diagnosis/status and safe direct work. A local lock is not distributed fencing; never manufacture a replacement controller or silently create/adopt a Run. | A21,A31,A60 |
| R30 | H | Parallelize only independent responsibilities/editing boundaries; begin with one writer when contracts are unsettled. Use project/host-supported isolation. Preserve unrelated changes; never silently stash/reset/clean or execute unauthorized setup hooks. | A24,A42,A61 |
| R31 | B | Use bounded versioned packets carrying objective/criteria, responsibility, scope, candidate, context references, dependencies, permitted actions, route, revisions, reporting contract and native bindings when issued. Do not clone the full coordinator transcript or predict runtime identities. | A24,A35,A62 |
| R32 | B | Reuse bounded context only while relevant source, instruction, requirement, candidate and revision bindings remain valid. Invalidate affected summaries/evidence on change. Do not introduce automatic repository uploads or a vector database. | A29,A30,A63 |
| R33 | H | Preserve bounded provenance-aware source access/relevant candidate identities. Exclude secret sources/unnecessary content reads. Distinguish proven changed/absent from unavailable sources; neither grants admission. Definitive rejection is not erased by restored bytes. Do not claim atomic multi-file snapshots or protection from arbitrary external writers. | A35,A64 |
| R34 | B,H | Workers report scope changes, checks/results, failures, evidence, uncertainty and questions against the assignment. Reports/logs are untrusted observations; they cannot expand authority, change budgets or establish acceptance. | A23,A27,A35,A62 |
| R35 | B | Use Orca-native messaging/events and blocking waits directly. Pod freezes packets and joins reports to a fresh worker-show of the exact runtime/Run/Task/Dispatch/worker identity, but keeps no parallel Delivery receipt or acknowledgment state machine. | A23,A59,A95 |
| R36 | B | Native retry/reuse is an Orca operation. Pod never runs a retry loop or selects retry UUIDs. A changed model, effort, account, packet, worktree or semantic Task requires a fresh policy decision and native operation; recovery of one admission may only join that admission's Orca-issued request. | A26,A96 |
| R37 | B | After two materially equivalent failed corrections without new evidence, diagnose. Record obligation, failing example, hypothesis, last evidence, discriminating check and correction identity. Resume only after changing a relevant variable based on evidence; preserve history across restarts. | A25,A65 |
| R38 | B,H | Worker reuse, retention, release and terminal/resource disposition are explicit Orca operations outside Pod's mutation adapter. Pod records no cleanup state and never infers or initiates release. Exact assignment settlement, not terminal release, frees the objective's logical slot. Never kill work or delete uncommitted work/evidence. | A22,A26,A97 |
| R39 | B | Before substantive work map every original criterion to a planned check or explicit human/provider dependency. Preserve criteria through decomposition; update evidence rather than redefine success to fit output. | A27,A28,A30,A43 |
| R40 | B,H | Run cheap discriminating checks early, focused checks during development and required complete gates at milestones. Honor project review rules; otherwise independently review substantial/high-risk changes. Review receives exact candidate/reproducible evidence without being primed to approve. | A28,A66 |
| R41 | B | Bind verification to commit/tree where applicable, relevant dirty/source identity, policy/configuration, dependencies, environment, commands/results and reviewer attempt. Material changes invalidate affected evidence. Reuse unaffected proof only when bindings/project rules permit. | A27,A29,A67 |
| R42 | H | Keep implemented, locally verified, independently reviewed, hosted proof complete, accepted, merged and deployed/released distinct. The coordinator assesses the objective under project acceptance authority; worker success/synthetic fixtures cannot promote later labels. | A27,A28,A68 |
| R43 | B | Final reports state objective, achieved criteria, exact blockers, candidate/check evidence, failures, material route exceptions, uncertainty and release state. Partial results remain partial; unobserved usage/cost remains unknown. | A37,A68 |
| R44 | I,H | Persist only compact `pod-context/v3` policy/evidence: admissions (`reserved`, `bound`, `unresolved`, `closed`, `deferred`), owner, checkpoint, interventions and source rejections. A record in another schema is reported, blocks that objective and is never converted. Store native IDs and request UUIDs as references with route/packet/grant/request/effective evidence, never copied native lifecycle status. `deferred` records an authoritative no-start decision, not a successful binding. Use private host-local storage, explicit versions and atomic bounded writes; keep live identifiers/private material out of Git. | A31,A35,A39,A69,A97 |
| R45 | B | Recovery selects the objective and reads native state before action. Native request-show governs completed/pending/absent request recovery; exact Run/Task/Dispatch readback may bind only one matching attempt. Missing, ambiguous or unresolved own evidence keeps its logical reservation outstanding and never justifies relaunch. There is no Pod retry, release or lifecycle loop and no daemon. | A22,A31,A60,A69,A96 |
| R46 | B,H | Steering creates a new revision, reconciles affected assignments at safe native boundaries, preserves useful unaffected work and prevents obsolete reports/proof satisfying revised work. Material acceptance changes require explicit authorization. | A30,A43,A67 |
| R47 | B,H | Record relevant observed outcomes and suggest preferences only after a meaningful pattern. Never silently rewrite preferences, infer savings from model labels or launch paid A/B experiments automatically. | A37,A70 |
| R48 | B | Read-only tools show effective policy/provenance, approval, runtime/capability/version issues, quota visibility, compact fresh native work/verification context and unsupported-state reports. Ambiguous status requires selection. Reads must not create or convert state, repair integrations, run hooks/models, dispatch or spend. | A34,A40,A49,A71 |
| R49 | B,H | Local setup enrolls the project/reconciles owned skills. Global setup installs user skills without changing the current project. Reuse compatible global installations; diagnose duplicate/shadowed copies. Remove redundant local copies only during local setup when owned/unchanged; preserve modified copies. | A32,A33,A34,A41,A72 |
| R50 | I,H | Support explicit isolated installation from a reviewed checkout on Linux with Python 3.13+. Bootstrap installs only into the selected environment and reports the command. Normal setup performs no implicit machine installation, automatic PATH repair, Orca installation, billing change or project hook. | A73 |
| R51 | H | Follow registered host paths/worktree mechanisms/native profiles/admin boundaries. Canonical pod registration/relocation uses owner-managed mechanism. Coding must not change global policy, weaken protections/gates or perform unauthorized deployment/provider/publication actions. | A74 |
| R52 | H | Block release until required live core integrations have candidate-bound passing evidence: Claude Code and Codex on Linux, each with real Orca delegation through the production adapter. Required synthetic, packaging, hosted, review and project gates also apply. Unsupported optional capabilities fail conservatively and cannot be marketed as verified. | A75 |
| R53 | I | Retain Python 3.13+, one bundle-as-package layout and standard-library facilities where practical. Reuse applicable safety invariants/tests. Build, install and test the frozen candidate on Linux and follow repository implementation/review ownership. | A73,A76 |
| R54 | B,I | Keep README approachable, implemented-only and linked to detailed contracts. Maintain/package one skill policy source and references. Document capability limits and actual evidence. | A38,A76 |
| R55 | H | Preserve project authority over source selection, checks, review, acceptance, merge and release. Repository instructions may constrain work but cannot expand personal provider/spending approval. Explicit user scope/host rules remain authoritative; reports/configuration cannot manufacture grants. | A09,A35,A68 |
| R56 | B,I | Install through the existing agent-skills ecosystem. Publish one discoverable `skills/pod` bundle whose `SKILL.md`, references, helpers and resources are the same authoring source the Python distribution packages, with a parity check rather than divergent copies. Document a release-pinned source form and a Node-free install from the reviewed release. Do not create an npm package, require a publication registry, or recommend an unrelated package. | A77,A78 |
| R57 | B,H | The installed skill carries its own first-party helpers. Loading it never installs tools or dependencies, edits shell profiles, installs Orca, changes billing or overwrites project files. Declare the actual prerequisites and supply one explicit user-space bootstrap step when one is needed. Never depend on a source checkout, an editable install, `PYTHONPATH`, a broken link or a path outside the distributed skill. | A78,A79 |
| R58 | B,H | Establish each route against the installed runtime instead of demanding a universal attestation. Load the installed Orca guide before native effects and use its recovery semantics. Distinguish an enforceable control, supported runtime observation, owner route configuration and unavailable metadata, claiming no more than those controls prove. Join the personal approval's redacted identity and authentication/billing evidence within the same runtime-selected account context; a present native default must supply its own usable auth proof, and login fallback is permitted only when that native default context is absent. An unresolved, partial, mismatched or freshly rotated route that could incur an unauthorized charge must not launch or replay; unknown optional quota metadata is disclosed, not treated as installation failure. | A80,A81,A95,A96 |
| R59 | B | Before a Pod-mediated push, pull-request update, workflow dispatch, rerun, remote diagnostic, merge, release, deployment or cancellation, return an explainable ALLOW, REUSE or DEFER from durable objective records. WARN is an annotation, never a fourth state, and a duplicate action attaches to the running one or reuses its evidence rather than deferring. Defer a superseded candidate and validation that is premature while the unit is unsettled; permit a justified bounded remote diagnostic. A scoped efficiency exception replaces the generic override and never lifts an authorization, spending or correctness restriction. These controls cover Pod-mediated operations, not arbitrary shell commands. | A82,A83 |
| R60 | H | Derive release authorization from an owner record naming the exact candidate, tree and scope. Report technical readiness accurately and separately; passing checks never grant permission, and a projection never withholds a readiness fact it has evidence for. | A84 |
| R61 | B,H | Keep host integration optional. Pod works on a suitable Linux environment without the owner's host tooling, paths, accounts or evidence, detecting and respecting host policies when present. | A79 |
| R62 | B,H | Govern expensive Pod-mediated remote actions with one deterministic kernel inside the existing execution path, evaluated at boundaries with zero model calls and bounded state reads. Bind each request to a delivery unit's explicitly prepared candidate generation, which freezes commit, tree, base, workflow digests, verification commands, toolchain, environment and policy revision; judge effects, require configured local preflight, reuse compatible evidence, classify failures, journal admitted actions and reconcile lost responses read-only. ALLOW/REUSE/DEFER decisions consume objective-local logical assignment evidence when readiness depends on delegated work, never an all-Run or worker-fleet census. Before preparation, admission, execution or any journal mutation, require the existing Pod owner plus a stable native current-Run/coordinator/generation binding to one of the objective's exact Run references on the same runtime. Read-only status remains available without mutation authority. Report enforcement as advisory unless host controls prove otherwise; project policy may only narrow authority; Orca keeps worker lifecycle and project governance keeps merge, release and deployment. | A85–A94,A95,A97 |
| R63 | B | Keep one conditional `execution-spec.md` as the authoring and interpretation source for human-readable Pod Execution Specs, numbered Proof of Done items and explicit delivery endpoints. Equivalent clear Markdown is valid; there is no parser, DSL, required frontmatter or second template. | A98,A99 |
| R64 | B,H | Accept a direct objective or a canonical GitHub issue URL through one workflow. Retrieve the complete issue with authorized host access, validate returned identity and target against the actual checkout, treat issue content as scope rather than authority, block inaccessible/incomplete/mismatched sources, and reconcile a closed issue with user intent before repeated work. | A100,A101,A102 |
| R65 | B,H | Bind issue identity, canonical locator, body digest and relevant amendment references into checkpoints, packets and final verification without copying the issue body into state. Recheck at intake, continuation, affected admission and final verification. Body change requires reconciliation; metadata alone does not. Preserve unaffected proof and never replace an admitted uncertain native request with a revised packet. | A103,A104 |
| R66 | B,H | Normal invocation plans proportionately and proceeds within authorization; plan-only remains read-only under actual host Plan Mode; plan-then-execute needs no ceremonial approval; continuation reuses reconciled objective/native state; small direct work needs no issue, worktree ceremony or worker. | A105 |
| R67 | B,H | Before implementation select or create the exact Orca-managed objective worktree, normally branch `orca/<task-slug>`. Bind actual Git repository/common-dir, branch and path separately from display labels; reuse only the same objective; preserve dirty/colliding work. Resolve every native start/replay selector to the frozen objective or separately authorized assignment-isolation placement before effect. Resolve state across linked worktrees and apply canonical private project policy plus worktree restrictions restrictively without copying private files. Native/host mechanisms own creation/removal. | A106,A107 |
| R68 | B,H | Normal delegated workers use native worker-start and a visible Orca agent tab. Terminal absence alone is not proof that no tab exists. After consuming and preserving an exact report, follow native Delivery acknowledgment and worker-release ordering promptly; reuse only for an immediate supported follow-up. Final cleanup checks exact objective workers only and retains uncertainty/protected resources. Pod adds no lifecycle/cleanup helper. | A108,A109,A110 |
| R69 | B,H | Discover Orca progressively from its installed guide and operation-specific help/capabilities. Missing delegation support blocks only delegation. Do not mirror Orca's catalog, automate provider UI, invent receipt fields, wrap providers or alter shared settings. | A111 |
| R70 | B,H | Keep the public product independent of owner services and machine-local state. Maintain a small generic tracked-source/artifact hygiene audit that reports safe path/category/location without echoing matched credentials and permits product identity, public links and sanitized fixtures. The upstream metadata service remains external governance with only a short contributor notice and no Pod integration or competing writer. | A112,A113 |
| R71 | B | README is the practical guide: issue-first and direct workflows, authoring versus authorized publication, installation/open/auth/invoke, planning/continuation, objective worktrees, visible workers/release, verification/outcome, helpers, catalog/context/approval, limits, update/removal and source-versus-published-versus-loaded versions. | A114 |
| R72 | B,H | Keep SKILL at most 750 words, each conditional reference at most 700 and combined references at most 2200. Load the issue reference only when relevant; workers receive bounded criteria/source references. Human status shows objective/source, worktree, relevant native work, blocker/next action and remaining gates while JSON retains detail. | A115,A116 |
| R73 | B,H | A release is published only by the release workflow from a `main` commit whose declared version has no tag, with notes taken from `CHANGELOG.md`. Implementation, validation and review publish nothing, and Pod work never deletes or rewrites a tag or release. | A117 |
| R74 | B,H | New policy accepts exactly Luna/Codex/gpt-6-luna, Sol/Codex/gpt-6-sol, Astra/Codex/gpt-6-astra, Sonnet/Claude/claude-sonnet-5, Opus/Claude/claude-opus-5-5 and Fable/Claude/claude-fable-5-1. Routing includes `256k` (a 256,000-token upper bound) or `max`, preserves requested versus proven effective context and provider ceilings, and refuses before effect where native per-worker context control is unavailable. | A118 |
| R75 | B,H | Guided `config approve/revoke ALIAS` resolves and rechecks joined native account/auth/billing evidence, shows a concise redacted proposal, requires explicit confirmation, atomically edits only personal YAML and creates no spending/reset/capacity grant. Direct work needs no approved worker; human doctor distinguishes direct readiness, approved/usable routes, actionable limitations. | A119 |


## Public interfaces

These contracts elaborate R03, R11 and R48–R50, not a second authority.

- `pod setup [--global] [--json]`: local enrollment/integration by default; global
  skill installation only with the flag, without current-project changes.
- `pod config [--check] [--json]`: render/validate effective policy, no writes or
  inference calls; invalid validation returns an actionable unsuccessful result.
- `pod config --edit [--scope personal|project]`: default personal; project explicitly
  selected. Validate after editing and preserve invalid user edits while blocking dispatch.
- `pod config approve ALIAS` / `revoke ALIAS`: interactive guided personal-YAML edits.
  Machine/host flow reads a redacted JSON proposal and passes its proposal identity only
  after explicit human confirmation; changed account/config/runtime evidence refuses.
- `pod doctor [--json]`: read-only installation, scope, capability, account visibility,
  policy and drift diagnostics. Active probes are not a doctor side effect.
- `pod status [--run RUN] [--json]`: native status plus objective/source, selected
  worktree, blocker/next action and remaining gates; require selection when ambiguous.
- Skill: Codex `$pod ...`, Claude Code `/pod ...`; no portable `@pod` convention.
- Private helper: bounded structured operation through the installed package interpreter,
  including issue intake/recheck and project-context reads;
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

| Complexity | Starter agent/model preference | Effort | Context |
| --- | --- | --- | --- |
| Trivial | Codex / Luna (`gpt-6-luna`) | low | `256k` |
| Simple | Claude Code / Sonnet (`claude-sonnet-5`) | medium | `256k` |
| Standard | Codex / Sol (`gpt-6-sol`) | medium | `256k` |
| Complex | Claude Code / Opus (`claude-opus-5-5`) | high | `max` |
| Very complex | Codex / Astra (`gpt-6-astra`) | xhigh | `max` |

All entries remain pending until explicitly approved against the real route. Fable
(`claude-fable-5-1`) is a pending alternative, not a mandatory default. `256k` is a
conservative 256,000-token upper bound; a lower proven clamp remains visible. `max`
resolves only from native capability evidence and is bounded by 1.05M for catalog Codex
models and 1M for catalog Claude models; those ceilings are not live capability proof.
Ultra/ultracode are not ordinary effort values. Default worker ceiling 2; ordinary maximum 3; explicitly granted
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
| Objective source | GitHub repository/issue identity, canonical locator, body digest and relevant amendment bindings; never the copied issue body. |
| Worktree binding | Stable Git repository/common-dir identity, exact objective worktree path/branch and optional separately authorized assignment placement; display labels and selector text are not identity. |
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

These are expected outcomes, not evidence. Parameterized tests may cover multiple
scenarios. Behavioral/live claims cannot be certified by checking document text.

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
| A34 | Ordinary config/status/doctor and simulated policy operations do not call models/hooks/dispatch/spending/hidden repair. |
| A35 | Secret-bearing context/malicious reports do not expose protected material or promote observations into authority. |
| A36 | No fallback intended to bypass a safety refusal. |
| A37 | Unobserved tokens/cost remain unknown; worker counts/model labels do not become savings/provider-compute claims. |
| A38 | Requirement/scenario references have no duplicate/orphan IDs, unclassified requirement or contradiction. |
| A39 | No competing native task database, scheduler, autonomous reasoning/restart loop, dashboard or marketplace. |
| A40 | Installed CLI has exactly four public families with text/JSON contracts; private operations add no global commands. |
| A41 | Host integrations derive from one policy, use verified discovery roots, stay inline and never override coordinator model/effort. |
| A42 | Side-effectful read-only-labelled tooling is withheld in plan-only mode; planning output obeys host rules. |
| A43 | Execution brief/revised acceptance map covers each original criterion and human/provider dependency without user-authored milestone file. |
| A44 | Small high-risk, large repetitive and mixed-complexity assignments demonstrate method-first independent assessment. |
| A45 | Onboarding is pending and model-call-free; reuse valid approval; discovery grants neither approval nor spending. |
| A46 | Layered configuration preserves keyed identities, restrictive lists, derived agent and personal/project/task provenance. |
| A47 | Duplicate/type/unknown-field/executable-tag/include and oversized/recursive YAML fails safely without execution. |
| A48 | Starter recommendations resolve actual capability; unsupported names/efforts pending/blocked, including Opus/Fable billing. |
| A49 | Invalid edited config blocks dispatch but permits diagnosis/authorized owned-work reconciliation without old-config fallback. |
| A50 | Captured assessment/policy/capability/quota fixtures replay route/reasons without inference/native mutation. |
| A51 | Missing/unsupported metadata preserves unknowns/source limits without credential/undocumented-endpoint scraping. |
| A52 | Refresh stale metadata at material boundaries without daemon; marginal change causes no oscillation or productive-worker killing. |
| A53 | Wrong-scope/account/action/bounds/validity spending grants fail; applicable grants authorize only bounded action. |
| A54 | Uncertain reset keeps logical idempotency identity and reconciles/readbacks without a second credit. |
| A55 | Worker-provider changes leave coordinator unchanged; headroom/checkpoints explicit and no permanent setting edits. |
| A56 | Current native workers without terminals use worker identity/lifecycle APIs; missing terminal alone is not failure. |
| A57 | Missing required pre-dispatch billing/fan-out assurance rejects before launch; requested settings alone are not effective proof. |
| A58 | Accepted but unproven submission triggers observation, not automatic Enter/resend/replacement/acceptance. |
| A59 | Faults around reservation/start/request receipt preserve uncertainty, exact identity and request recovery without double-counting or a duplicate start. |
| A60 | Missing caller, conflicting ownership or a partial handover blocks delegation; valid adoption preserves the work without a proxy coordinator. |
| A61 | Unsettled contracts start one writer; project isolation/hooks honored without weakening host protection. |
| A62 | Packets/reports enforce bounded fields, criterion/candidate/scope/runtime-issued identity; reject transcript cloning and report permissions. |
| A63 | Relevant files/instructions/requirements/candidate changes invalidate context; unchanged bound context can be reused. |
| A64 | Changed/absent/redirected sources differ from unavailable reads; definitive rejection survives restoration; unrelated dirty paths do not grant sensitive reads. |
| A65 | Restarted correction history recognizes equivalence; rewording is insufficient but discriminating evidence permits changed attempts. |
| A66 | Cheap discriminating checks precede expensive work; independent review receives exact candidate and reproducible unprimed evidence. |
| A67 | Source/config/dependency/environment/candidate changes invalidate affected proof; no fabricated Git metadata and explicit alternative source binding. |
| A68 | Completion labels/final report follow evidence/project authority; failures, blockers, route exceptions and unresolved native references remain visible. |
| A69 | Interrupted versioned checkpoints recover privately and atomically; retention/native-first recovery protect unresolved effects/evidence. |
| A70 | Feedback may suggest but never auto-edits preferences or launches unauthorized live work. |
| A71 | Ambiguous status requires selection; compact counts, route reasons, quota confidence, verification gaps and next safe action. |
| A72 | Preserve modified copies; global setup never prunes local; local cleanup removes only redundant owned unchanged copies. |
| A73 | Reviewed isolated install works on Linux, rejects old Python, handles command paths, no automatic PATH/Orca repair. |
| A74 | Unregistered canonical relocation/prohibited placement refused; owner-managed registration is a separate cutover gate. |
| A75 | Missing live PASS for Claude Code or Codex on Linux, or for either delegation adapter, blocks release despite synthetic CI and a read-only doctor. |
| A76 | Frozen build, isolated install, hosted Linux CI, incident discovery, compile/diff, independent audit and implemented-only docs bind the candidate under release review. |
| A77 | Explicit config edit defaults personal; project scope explicit; preserve invalid edits and block dispatch, never silently restore/sanitize. |
| A78 | `npx skills add j3w1/pod --skill pod`, its per-agent and global forms, and a release-pinned source all install a bundle whose helpers run without a checkout. |
| A79 | A copied bundle runs from an unrelated directory with no `PYTHONPATH` and no source tree; a missing prerequisite prints one actionable step, never an import traceback or an invented payment requirement. |
| A80 | An approved subscription route whose optional quota bucket is unavailable still launches, with the gap disclosed; unknown billing or an unbacked paid route still fails closed before any native effect. |
| A81 | A skills-CLI-managed copy is detected and never overwritten, removed or claimed; repeating setup is a cheap no-op when correct, and global setup writes nothing into the current repository. |
| A82 | A superseded candidate and premature validation each defer with an explainable reason and next action; an identical running action is attached to and a passing result for the same candidate and context is reused; a necessary rerun after changed input proceeds. |
| A83 | A scoped efficiency exception bound to a personal grant softens only an efficiency deferral and never lifts an authorization, spending or correctness hold. |
| A84 | A complete gate without owner authorization reports readiness and withholds permission; authorization naming the exact candidate, tree and scope authorizes it, and an incomplete gate stays blocked regardless. |
| A85 | Several locally discoverable corrections in one delivery unit converge on the same branch and pull request; no intermediate correction crosses the remote boundary until its candidate passes the configured local preflight. |
| A86 | Two callers requesting identical validation concurrently produce one admitted execution; the other attaches to it. |
| A87 | A submission whose response is lost stays UNKNOWN, blocks a resubmission, and is settled only by provider readback; after a coordinator restart the unit's candidate bindings, evidence, decisions and pending effects remain recoverable. |
| A88 | A changed source, base, workflow, environment or policy opens a new candidate generation, and evidence bound to the previous one is not reused as current proof even on the same commit. |
| A89 | A remote-only question admits a bounded diagnostic naming its question, local limitation, check and stopping condition while the candidate is still converging, without pretending the candidate is release-ready; an unchanged repeat is answered from the record. |
| A90 | An unclassified remote failure is not retried; a code defect is recorded as a correction, and the third equivalent correction requires a diagnosis with distinct bounded evidence before validation resumes. |
| A91 | Supersedence cancels a pending, cancel-safe validation of the old candidate only when policy allows it, its result can never approve the newer candidate, and a pending deployment is never canceled by supersedence. |
| A92 | An independent or urgent delivery unit is admitted while another unit's workers, deliveries or corrections are unsettled. |
| A93 | A project file that relaxes the governor mode, widens the retry budget, enables cancellation, declares host control or adds an exception grant is refused as authority expansion. |
| A94 | The enforcement level is reported as advisory unless the owner's personal policy declares a host control, and it is never reported as a proven control. |
| A95 | Pod exposes no private Delivery, cleanup, release, terminal or lifecycle mutation operation; Orca owns those states, while reports and Governor decisions use exact objective assignment evidence when needed. |
| A96 | Completed, pending and absent Orca request recovery binds the same immutable admission without a second semantic start; invalid UUID, changed authority/worktree, contradictory receipt or ambiguous native attempt holds. |
| A97 | Orca's documented effect-free refusal records durable deferred evidence with no binding or blind retry; `runtime_error` and any unknown, malformed or partial-effect refusal remain unresolved until native readback settles them, and physical-capacity enforcement stays unavailable. |
| A98 | One installed conditional reference defines the readable Pod Execution Spec skeleton, terms, numbered PoD items and explicit delivery endpoint; no parallel template exists. |
| A99 | A representative filled spec remains ordinary readable Markdown without parser-only boilerplate or empty ceremony. |
| A100 | Complete authorized issue intake returns the full body and exact identity, while inaccessible or incomplete reads fail clearly without requesting credentials or claiming success. |
| A101 | An issue for another repository is rejected against actual Git identity before implementation; issue text/comments cannot grant authority. |
| A102 | A closed issue requires outcome and user-intent reconciliation rather than silently repeating work. |
| A103 | Body changes require reconciliation while metadata-only updates preserve valid derived work; relevant amendment bindings remain bounded. |
| A104 | Continuation reuses objective/native state across linked worktrees, preserves unaffected evidence and prevents a revised packet from replacing an uncertain same-Task attempt. |
| A105 | Issue and direct objectives share proportionate plan-only, plan-then-execute, continuation and direct-work behavior under actual host permissions. |
| A106 | Actual Git repository/common-dir, branch and path bind the objective worktree; matching display text is insufficient and dirty/colliding owner work is preserved. |
| A107 | Canonical private project policy remains effective in a linked objective worktree and worktree/task policy can only narrow it, without copying the private file. |
| A108 | A normal delegated worker is started through native Orca into its own visible agent tab; terminal absence alone is not treated as proof of no tab. |
| A109 | After exact report consumption/preservation, native Delivery acknowledgment and worker release occur promptly in guide order; reuse requires an immediate supported follow-up. |
| A110 | Final cleanup checks exact objective workers only, protects uncertainty/foreign resources and removes a worktree only after integration, preservation and cleanliness. |
| A111 | Installed Orca guidance is loaded progressively and missing delegation controls do not block safe direct work or diagnostics; no wrapper/shared-setting workaround appears. |
| A112 | Generic tracked-source/artifact hygiene detects representative private residue while permitting Pod identity, public links and sanitized fixtures. |
| A113 | Public code and guidance require no external metadata service; AGENTS keeps only the short upstream ownership notice and introduces no competing writer. |
| A114 | README practically covers install/open/auth/invoke, author/publish/execute, direct/planning/continuation, worktrees, visible workers/release, verification/outcome, catalog/context/approval, helpers, limits and update/removal. |
| A115 | SKILL remains at most 750 words, each conditional reference at most 700 and their combined total at most 2200; Execution Spec loads only for issue/spec work. |
| A116 | Human status shows objective/source, selected worktree, relevant native work, blocker/next action and remaining gates; JSON retains detailed evidence without side effects. |
| A117 | Implemented, reviewed, hosted, live and accepted facts remain distinct from published; only the release workflow publishes, from a declared version with no tag, and Pod work removes no tag or release. |
| A118 | The exact six-model catalog and five context-aware defaults govern new work; `256k` means 256,000 tokens, lower clamps remain visible, provider ceilings are not proof and current missing native context control refuses before effect. |
| A119 | Guided approval/revocation needs explicit confirmation, rechecks native/config evidence, edits only personal YAML atomically, shows paid/unknown honestly and creates no spending/reset/capacity grant; direct work and objective-scoped doctor remain available. |


## Verification

Python 3.13+, one bundle-as-package layout, minimal safe YAML dependency and standard-library
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
required live runtime cases; project acceptance. Do not weaken checks.

Worker success, local verification, hosted CI, independent review and external project
acceptance are distinct facts and are reported separately. Missing live Orca, model or
provider evidence remains unavailable or NOT_RUN; it is never reported as a pass.

Live core matrix: Codex on Linux and Claude Code on Linux. Each proves installation and
discovery, in-session coordination, authorized native execution and effective route,
supervision and lifecycle, verification and interruption/adoption. Real Orca delegation
through the production adapter is proved separately for each advertised worker adapter,
covering request construction, account and authentication selection, launch identity,
effective launch and request recovery. Native messaging, settlement and disposition remain
Orca evidence rather than Pod state. Missing live PASS for any required
combination blocks release despite offline PASS. Optional unsupported capabilities fail
conservatively and are not advertised operational.

Efficiency is governed by the bounded decision check of R59 and the waste governor kernel
of R62, not by a comparative benchmark. It reuses the records an objective already keeps
and returns ALLOW, REUSE or DEFER with an explainable reason and next action. Unknown stays
unknown; observed elapsed time is not provider compute or billed cost; counters report
reuse, deferrals, interruptions and cancellations without an estimate of minutes saved; Pod
claims no universal savings percentage.

Live exercises are disposable and bounded/authorized. Existing ordinary-project trials are
read-only. No purchase, reset-credit redemption, production mutation, deployment or
package-registry publication is authorized merely by an implementation task. Actual release
must honour host registration and repository authority. Supported execution environment: Linux.
