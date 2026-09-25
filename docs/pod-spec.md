# Pod specification

Status: the product requirements for the version the root `VERSION` file declares on the
same commit. It is not a claim of implementation or verification; a completed test cannot
silently revise a requirement, and a changed requirement is revised here explicitly with
its scenario and coverage row.

Pod is an adaptive coordination policy for Orca. It turns the coding session already in
use into the coordinator, choosing tools, agents, models and effort for each part of a
task and adapting the plan until the objective is verified. Requirement types: B =
behavior, H = hard authorization, I = implementation.

## Scope and ownership

Orca is the single source of truth for Runs, Tasks, Dispatches, request recovery, worker
lifecycle, messaging, placement and terminal/resource disposition. Pod is a thin
selection, admission and evidence layer: it keeps a serialized admission seam and exact native
references, but it does not copy native lifecycle status or implement Delivery, cleanup,
liveness, terminal, release or retry state machines. The installed, version-matched Orca
orchestration guide and its recovery reference govern native mutation semantics. Orca mints
request UUIDs; Pod only records and reuses them for the same immutable admission. Host
policy, explicit user authorization and project governance retain their own authority.

Pod decides desired fan-out, not physical occupancy: one logical reservation per Pod
assignment, freed by exact native assignment settlement regardless of retained terminals.
Settlement requires the exact Run/Task/Dispatch join, a settled projection outcome,
and the Dispatch's own terminal status; a projected stage status, if present,
must agree. A failed stopped attempt qualifies even when its
stage detail is `process_stopped`; an active, unverifiable or undocumented
abandoned attempt does not. Resource release alone proves nothing.
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
remain first-class. Issue and worktree source bindings create no issue or worktree managers.

## Requirements

| ID | Type | Requirement | Scenarios |
| --- | --- | --- | --- |
| R01 | B | Maintain this requirement/scenario inventory as product authority. Explicitly revise changed requirements; implementation results cannot silently redefine them. | A38 |
| R02 | B | Keep the current conversation as coordinator. Orca is the sole runtime authority for Runs, Tasks, Dispatches, requests, placement, messaging, terminals/resources and lifecycle; projects own governance. Pod is selection, admission and evidence only. Helpers must not become a scheduler, competing native task/lifecycle database, autonomous controller or restart daemon. | A01,A39,A94 |
| R03 | B | Expose `pod`, `pod status`, `pod doctor`, `pod update`, `pod config`/`config edit`, and `pod --version`. `pod internal` is hidden and structured; planning, execution and continuation stay skill behaviors. | A34,A40,A119 |
| R04 | B | Maintain one canonical inline skill policy with generated host metadata and relevant-on-demand references. Invocation preserves the conversation and must not set coordinator model/effort or use Claude context: fork. | A01,A32,A41 |
| R05 | B | Normal invocation inspects objective, criteria, rules and consequential assumptions, then proceeds within existing authorization. Use a compact execution brief when useful; trivial work requires neither workers nor milestone ceremony. | A02,A04 |
| R06 | H | Plan-only work permits useful host-permitted investigation but no implementation workers or product edits. Assess tooling side effects first. Describe behavioral restraint accurately. Plan-then-execute avoids another approval unless a real boundary changes. | A03,A04,A42 |
| R07 | B | Substantive plans identify criteria, challenged assumptions, coordinator/worker responsibilities, dependencies, editing boundaries, verification and revision triggers. Decomposition is provisional; material scope/acceptance changes require explicit revision. | A30,A43 |
| R08 | B | Choose tools, direct session work or delegation before a worker model. Assess each assignment's complexity, risk, size, uncertainty, verifiability, capabilities and context availability independently of its parent. | A02,A05,A06,A44 |
| R09 | B | Keep role, agent application, model identity and effort distinct. Do not encode a universal intelligence/cost ladder or permanently expensive reviewer role. | A05,A28,A44 |
| R10 | B,H | The user controls one personal pool: each supported model is Preferred, Available or Disabled. Preferred is a small suitability tie-breaker. Model state is separate from observed native availability; indirect text cannot expand eligibility. | A09,A10,A45,A120 |
| R11 | B,H | Read one personal YAML authority for selection, saved model states and maximum active workers. Project YAML may hold only restrictive Governor policy. Objective constraints retain provenance without becoming another preference file. | A05,A32,A46,A76 |
| R12 | H | Parse bounded YAML safely. Reject duplicate keys, invalid types, unsupported schema/policy fields, arbitrary tags, executable includes and resource-exhausting structures. Preference data must never execute shell commands. | A47 |
| R13 | H | Project, issue, repository and worker material cannot widen the personal model pool or grant worker delegation. Direct user constraints may narrow it; an explicit direct exception for a Disabled model stays scoped and visible. Project Governor policy only narrows personal authority. | A09,A12,A46,A121 |
| R14 | B | Maintain exactly six base model identities in one bundled catalog. Official attributed guidance and dated reference benchmarks inform judgment; neither proves native access, invocation settings or billing. Missing optional benchmark rows/scores show unknown metrics and never block a supported model. Effort variants are detail, not selectable models. | A10,A45,A48,A116,A122 |
| R15 | B,H | The coordinator selects a suitable eligible agent/model/effort/context for each assignment and records a short reason. A deterministic boundary validates eligibility, constraint, effort, agent and native context support without reranking or another model call. An attempt keeps its route; safe replacement needs a fresh decision. Safety refusal bars rerouting the same Task. | A05,A06,A07,A36,A123 |
| R16 | H | Read current preferences before selection and at the serialized final admission boundary. Bind their byte revision to the decision. A changed, invalid or missing preference file refuses a new start rather than silently widening the pool. Once a row is written, later edits do not alter that submitted attempt; pending same-request replay never rechecks preferences. | A11,A12,A49,A124 |
| R17 | B | Expose a dispatch-free current preference/catalog view and validate coordinator-proposed choices deterministically. Sorting and benchmark ranks never become routing inputs or trigger model calls. | A34,A50,A125 |
| R18 | B,H | Read only installed native capability and exact request/worker evidence needed for a start. Missing or unsupported fields stay unknown; do not scrape credential stores, infer model access from catalog data or call providers to probe a route. | A08,A51,A126 |
| R19 | B,H | Keep actual rate-limit, unavailable and authentication failures on the failed attempt with source and native retry-after. The same route is held until retry-after or a meaningful runtime/user change; alternatives require known settlement or proven no-start. There is no rotation or blind retry loop. | A13,A14,A15,A16,A52,A127 |
| R20 | H | A preference edit changes model eligibility only. It does not authorize provider purchases, billing changes, service-limit changes or bypassing host/project restrictions. Existing authenticated native sessions remain native authority. | A17,A53 |
| R21 | B,H | Explicit preference mutations use a short lock, targeted-key comparison and atomic validated replacement. Preserve unrelated valid settings and comments when possible, and never overwrite invalid or concurrently changed target data. Internal operations never write preferences. | A17,A22,A54,A128 |
| R22 | B,H | Keep the coordinator conversation, model, effort and host settings unchanged. Preserve useful integration, verification and reporting headroom; checkpoint before foreseeable interruption. Worker selection changes only workers. | A31,A37,A55 |
| R23 | I,H | Resolve the applicable Orca runtime through installed discovery and guide. Validate worker-start launch-preferences capability, exact read/start/request contracts and optional terminal identity. Do not substitute a runtime, host or direct provider API. | A08,A56,A94 |
| R24 | H | Before a new start validate current authority, placement, logical ceiling, packet/source bindings, version and selected route. Record requested and observed effective model/effort/context separately; absent and null launch fields stay unknown, while known relevant disagreements are mismatches. Objective-bound acceptance holds recorded route mismatches and unknown effective routes; caller claims cannot supply route proof. Pending replay retains its admitted request and does not become a new preference decision. | A08,A20,A57,A124,A129 |
| R25 | B | Treat accepted input, started reasoning, native settlement and accepted output as different native observations. Silence/lost responses do not prove failure, justify resending input or authorize replacement work. Recover the same immutable admission through Orca request-show: record a completed receipt, join a pending request with Orca's UUID, or inspect exact Run/Task/Dispatch identity after an absent result or when no UUID was recorded. Missing, ambiguous or contradictory evidence holds and never starts fresh; a later incomplete observation cannot erase a known request-identity conflict. | A22,A58,A95 |
| R26 | H    | `workers.max_active` bounds concurrently reserved logical assignments per objective. It is personal, 0–8, default two, and a direct constraint may narrow it. Every investigator, reviewer and authorized descendant counts, and zero workers is valid. It is a ceiling, never a target: a free reservation admits nothing by itself, and a capacity wait is valid only at the ceiling. Native settlement frees a slot even when a terminal is retained; never infer physical occupancy. | A18,A19,A130,A143             |
| R27 | H | Only the coordinator delegates unless direct user intent explicitly permits a descendant. Every Pod-managed descendant needs its own objective reservation under the same ceiling. Native limits and hidden provider fan-out are not Pod observations. | A16,A19,A20,A21 |
| R28 | H | Serialize admission under the objective lock, recording one logical reservation before each worker start. Reserved and unresolved attempts remain outstanding; an exact Run/Task/Dispatch readback with settled outcome and terminal Dispatch status frees a bound slot, including a failed stopped attempt. Do not census other objectives or reconstruct physical occupancy. Documented effect-free Orca refusals defer; uncertain errors require exact request and worker readback. | A18,A21,A22,A59,A96 |
| R29 | H | Establish one authoritative coordinator per objective. Adoption reconciles pending effects/native authority before dispatch. Before Governor preparation, admission, execution or any journal mutation, join the caller's stable native current-Run binding (Run, coordinator handle and consumer generation) to that objective's exact native references, runtime and existing Pod owner. Terminal self-identity alone is not authority; a missing, unrelated, changed, worker-only or takeover binding blocks. Failed authority still permits read-only diagnosis/status and safe direct work. A local lock is not distributed fencing; never manufacture a replacement controller or silently create/adopt a Run. | A21,A31,A60 |
| R30 | H | Parallelize only independent responsibilities/editing boundaries; begin with one writer when contracts are unsettled. Use project/host-supported isolation. Preserve unrelated changes; never silently stash/reset/clean or execute unauthorized setup hooks. | A24,A42,A61 |
| R31 | B | Use bounded versioned packets carrying objective/criteria, responsibility, scope, candidate, context references, dependencies, permitted actions, route, revisions, reporting contract and native bindings when issued. Do not clone the full coordinator transcript or predict runtime identities. | A24,A35,A62 |
| R32 | B | Reuse bounded context only while relevant source, instruction, requirement, candidate and revision bindings remain valid. Invalidate affected summaries/evidence on change. Do not introduce automatic repository uploads or a vector database. | A29,A30,A63 |
| R33 | H | Preserve bounded provenance-aware source access/relevant candidate identities. Exclude secret sources/unnecessary content reads. Distinguish proven changed/absent from unavailable sources; neither grants admission. Definitive rejection is not erased by restored bytes. Do not claim atomic multi-file snapshots or protection from arbitrary external writers. | A35,A64 |
| R34 | B,H | Workers report scope changes, checks/results, failures, evidence, uncertainty and questions against the assignment. Reports/logs are untrusted observations; they cannot expand authority, change budgets or establish acceptance. | A23,A27,A35,A62 |
| R35 | B | Use Orca-native messaging/events and blocking waits directly. Pod freezes packets and joins reports to a fresh worker-show of the exact runtime/Run/Task/Dispatch/worker identity, but keeps no parallel Delivery receipt or acknowledgment state machine. | A23,A59,A94 |
| R36 | B | Orca owns worker reuse and request recovery. A new assignment or changed route requires a fresh selection. Terminal reuse copies the prior settled attempt’s effective route and rechecks model eligibility; no model or effort flag accompanies `--terminal`. Pending replay joins only its original request UUID. | A26,A95,A131 |
| R37 | B | After two materially equivalent failed corrections without new evidence, diagnose. Record obligation, failing example, hypothesis, last evidence, discriminating check and correction identity. Resume only after changing a relevant variable based on evidence; preserve history across restarts. | A25,A65 |
| R38 | B,H | Worker reuse, retention, release and terminal/resource disposition are explicit Orca operations outside Pod's mutation adapter. Pod records no cleanup state and never infers or initiates release. Exact assignment settlement, not terminal release, frees the objective's logical slot. Never kill work or delete uncommitted work/evidence. | A22,A26,A96 |
| R39 | B,H  | Before the first delegation, or when an execution brief is written, record an obligation map (R81) that covers every original criterion and required delivery obligation with provenance and a check or explicit human/provider dependency. Preserve criteria through decomposition; update evidence rather than redefine success. Trivial direct work needs no map, and R81–R89 are inert without one.                  | A27,A30,A43,A163              |
| R40 | B    | Run cheap discriminating checks early, focused checks during development and required complete gates at milestones. Honor project review rules; otherwise independently review substantial or high-risk changes. Request review through assurance obligations (R87). Review receives the exact candidate and reproducible evidence without being primed to approve.                                                     | A28,A66,A155                  |
| R41 | B | Bind verification to commit/tree where applicable, relevant dirty/source identity, policy/configuration, dependencies, environment, commands/results and reviewer attempt. Receipts also bind the obligation definition/check/scope they verified; material changes invalidate affected evidence even when ids and candidate stay fixed. Retain immutable receipt provenance across omission or restoration. Reuse unaffected proof only when bindings/project rules permit. | A27,A29,A67 |
| R42 | H    | Keep implemented, locally verified, independently reviewed, hosted proof complete, accepted, merged and deployed/released distinct. `independently reviewed` follows R88. The coordinator assesses the objective under project acceptance authority; worker success and synthetic fixtures cannot promote later labels.                                                                                               | A27,A28,A68,A157              |
| R43 | B    | Reports state satisfied obligations, blockers, candidate and check evidence, route exceptions, uncertainty, unresolved native references, remaining gates, withdrawn obligations with provenance and reason, triage downgrades, `boundary_exceeded` results and open proposals as out of scope. An interim report for a quiescent objective says incomplete and names each external dependency. Unobserved usage and cost stay unknown. | A37,A68,A145,A158             |
| R44 | I,H  | Persist compact `pod-context/v4` policy and evidence with the `pod-admission/v4`, `pod-packet/v3`, `pod-checkpoint/v3` and `pod-cli/v4` contracts. Another schema is reported and blocks only its objective (R90); nothing converts it. Keep native ids as references, never copied lifecycle state, and keep private data out of Git.                                                                                   | A31,A35,A39,A69,A96,A132,A161 |
| R45 | B | Recovery selects the objective and reads native state before action. Native request-show governs completed/pending/absent request recovery; exact Run/Task/Dispatch readback may bind only one matching attempt. Missing, ambiguous or unresolved own evidence keeps its logical reservation outstanding and never justifies relaunch. There is no Pod retry, release or lifecycle loop and no daemon. | A22,A31,A60,A69,A95 |
| R46 | B,H | Steering creates a new revision, reconciles affected assignments at safe native boundaries, preserves useful unaffected work and prevents obsolete reports/proof satisfying revised work, including changed definitions on stable obligation ids. Unrelated steering preserves unaffected definition bindings. Material acceptance changes require explicit authorization. | A30,A43,A67 |
| R47 | B,H | Record relevant observed outcomes and suggest preferences only after a meaningful pattern. Never silently rewrite preferences, infer savings from model labels or launch paid A/B experiments automatically. | A37,A70 |
| R48 | B | Read-only status and doctor report preferences, resolved paths, current native work, constraints, decisions, mismatches, installed bundle identity drift, catalog/benchmark age, blockers and next actions. Reads do not repair, dispatch, run models or alter preferences. | A34,A40,A49,A71,A133 |
| R49 | B,H | The one-shot installer places both agent skills, dependencies and a user-local launcher from one validated bundle. Reinstall and update preserve edited preferences and foreign files; diagnose duplicate/shadowing copies and interrupted installs. | A32,A33,A72,A134 |
| R50 | H | Follow registered host paths/worktree mechanisms/native profiles/admin boundaries. Canonical pod registration/relocation uses owner-managed mechanism. Coding must not change global policy, weaken protections/gates or perform unauthorized deployment/provider/publication actions. | A73 |
| R51 | H | Claim live-verified coordination only with passing live evidence for Claude Code and Codex on Linux, each with real Orca delegation through the production adapter. Synthetic, hosted and review checks never substitute for it, and missing live evidence stays NOT_RUN. Unsupported optional capabilities fail conservatively and cannot be marketed as verified. | A74 |
| R52 | I | Use Python 3.13+ on Linux, one bundle-as-package and standard-library facilities where practical. Run unit, incident, PTY/subprocess, installed-bundle, source-audit and hosted checks as specified, with independent review and separate live evidence. | A75,A135 |
| R53 | B,I | Keep README approachable, implemented-only and linked to detailed contracts. Maintain one skill policy source and references. Document capability limits and actual evidence. | A38,A75 |
| R54 | H | Project governance owns source selection, checks, review, acceptance, merge and release. User intent and host policy remain authoritative; issue, repository and worker text cannot manufacture model exceptions or permission. | A09,A35,A68 |
| R55 | B,I | Distribute from `main` through the one-shot installer, which uses the skills CLI for the single `skills/pod` bundle and places a user-local `pod` launcher. Root `VERSION` is the sole authored version, linked by the bundle. There are no packages, releases or tags. | A77,A118,A136 |
| R56 | B,H | The installer stages and validates the bundle, isolated dependency environment, launcher, receipt, preferences and minimal owned PATH block. It refuses foreign launchers, preserves user edits, and recovers interrupted installs. A copied installed bundle runs outside the checkout. | A77,A78,A137 |
| R57 | B,H | Use installed Orca guidance and operation-specific capabilities. Native launch preferences are required for delegation; missing optional context control uses `native_default` with no context flag. Requested versus effective evidence is honest, and missing delegation capability leaves direct work available. | A79,A80,A94,A95,A138 |
| R58 | B | Before Pod-mediated remote action, return explainable ALLOW, REUSE or DEFER from durable candidate-bound evidence. Duplicate action attaches or reuses proof. Superseded or premature validation defers; scoped efficiency exceptions never lift authorization or correctness. | A81,A82 |
| R59 | H | Merge, release and deployment of a governed project need an owner authorization record naming the exact candidate, tree and scope. Report technical readiness accurately and separately; passing checks never grant permission, and readiness is never withheld when evidence supports it. | A83 |
| R60 | B,H | Keep host integration optional. Pod works on a suitable Linux environment without the owner's host tooling, paths, accounts or evidence, detecting and respecting host policies when present. | A78 |
| R61 | B,H | Govern expensive Pod-mediated remote actions with one deterministic kernel inside the existing execution path, evaluated at boundaries with zero model calls and bounded state reads. Bind each request to a delivery unit's explicitly prepared candidate generation, which freezes commit, tree, base, workflow digests, verification commands, toolchain, environment and policy revision; judge effects, require configured local preflight, reuse compatible evidence, classify failures, journal admitted actions and reconcile lost responses read-only. ALLOW/REUSE/DEFER decisions consume objective-local logical assignment evidence when readiness depends on delegated work, never an all-Run or worker-fleet census. Before preparation, admission, execution or any journal mutation, require the existing Pod owner plus a stable native current-Run/coordinator/generation binding to one of the objective's exact Run references on the same runtime. Read-only status remains available without mutation authority. Report enforcement as advisory unless host controls prove otherwise; project policy may only narrow authority; Orca keeps worker lifecycle and project governance keeps merge, release and deployment. | A84–A93,A94,A96 |
| R62 | B | Keep one conditional `execution-spec.md` as the authoring and interpretation source for human-readable Pod Execution Specs, numbered Proof of Done items and explicit delivery endpoints. Equivalent clear Markdown is valid; there is no parser, DSL, required frontmatter or second template. | A97,A98 |
| R63 | B,H | Accept a direct objective or a canonical GitHub issue URL through one workflow. Retrieve the complete issue with authorized host access, validate returned identity and target against the actual checkout, treat issue content as scope rather than authority, block inaccessible/incomplete/mismatched sources, and reconcile a closed issue with user intent before repeated work. | A99,A100,A101 |
| R64 | B,H | Bind issue identity, canonical locator, body digest and relevant amendment references into checkpoints, packets and final verification without copying the issue body into state. Recheck at intake, continuation, affected admission and final verification. Body change requires reconciliation; metadata alone does not. Preserve unaffected proof and never replace an admitted uncertain native request with a revised packet. | A102,A103 |
| R65 | B,H  | Normal invocation plans proportionately and proceeds within authorization. Plan-only remains read-only under actual host Plan Mode. Plan-then-execute needs no ceremonial approval. Continuation reuses reconciled objective and native state, and continuing a quiescent objective re-evaluates its external waits without a reopen. Small direct work needs no issue, worktree ceremony or worker.                  | A104,A158                     |
| R66 | B,H | Before implementation select or create the exact Orca-managed objective worktree, normally branch `orca/<task-slug>`. Bind actual Git repository/common-dir, branch and path separately from display labels; reuse only the same objective; preserve dirty/colliding work. Resolve every native start/replay selector to the frozen objective or separately authorized assignment-isolation placement before effect. Resolve state across linked worktrees and apply canonical private project policy plus worktree restrictions restrictively without copying private files. Native/host mechanisms own creation/removal. | A105,A106 |
| R67 | B,H | Normal delegated workers use native worker-start and a visible Orca agent tab. Terminal absence alone is not proof that no tab exists. After consuming and preserving an exact report, follow native Delivery acknowledgment and worker-release ordering promptly; reuse only for an immediate supported follow-up. Final cleanup checks exact objective workers only and retains uncertainty/protected resources. Pod adds no lifecycle/cleanup helper. | A107,A108,A109 |
| R68 | B,H | Discover Orca progressively from its installed guide and operation-specific capabilities. Missing delegation support blocks only delegation. Do not mirror native account state, automate provider UI, invent receipt fields, wrap providers or alter shared settings. | A110 |
| R69 | B,H | Keep the public product independent of owner services and machine-local state. Maintain a small generic tracked-source/artifact hygiene audit that reports safe path/category/location without echoing matched credentials and permits product identity, public links and sanitized fixtures. The upstream metadata service remains external governance with only a short contributor notice and no Pod integration or competing writer. | A111,A112 |
| R70 | B | README is the practical install and session guide: one-shot command, open/auth/invoke, direct and issue objectives, Plan Mode, worktrees, visible workers, verification, limits, update/removal and linked installation details. Describe implemented behavior only. | A113 |
| R71 | B,H | Keep SKILL at most 750 words, each conditional reference at most 700 and combined references at most 2200. Load the issue reference only when relevant; workers receive bounded criteria/source references. Human status shows objective/source, worktree, relevant native work, blocker/next action and remaining gates while JSON retains detail. | A114,A115 |
| R72 | B,H | The supported catalog is exactly Claude Opus 5.5, Fable 5.1, Sonnet 5 and GPT-6 Astra, Sol, Luna at their exact native ids. Select a supported effort or `native_default`; context is `native_default` while Orca lacks a per-worker context flag. Documented ceilings are not effective proof. | A116,A122,A129 |
| R73 | B,H | The TUI shows all six models, effective states and a persistent focus-driven Details panel with attributed guidance, Pod examples, native/default details and dated AA reference metrics. State edits save immediately; `r` reversibly toggles All models and My selection without erasing saved choices. | A117,A139,A140 |
| R74 | B,H | The TUI works on ordinary Linux terminals and over SSH: arrow focus, Space state cycle, search, model/intelligence/price/latency sorts, provider display filter, help, expansion, quit, resize, monochrome, ASCII and non-TTY summary. Focus is a model id across sort/filter/reload; no-results and tiny terminals stay usable. Reading and navigation do not write. | A139,A140 |
| R75 | B,H | Each focused model's Details panel is always visible without Enter, with 35–65 words of attributed purpose guidance, distinct Pod examples, exact id, native/default capability and dated reference profile. Rank scope is six supported base models; AA variant metrics stay paired and informational, with source URL and age visible. | A122,A139 |
| R76 | B,H | Preference edits save immediately with lock, targeted compare-and-swap, validation, atomic replacement and honest success/failure feedback. Open TUIs reload external changes promptly without blocking keys on runtime reads; a conflict never overwrites another editor's targeted value. | A128,A140 |
| R77 | B,H | Objective constraints retain provenance and cannot edit the personal file. Only direct user instructions may permit a scoped Disabled-model or descendant exception. A later saved-state or mode change lapses the model exception; indirect text may only narrow. | A121,A141 |
| R78 | B,H | Reactive failure records are attempt-local. Honor native retry-after, require settlement before alternatives, and bar same-Task rerouting after safety refusal. Routine questions use Orca reply; advisories keep the route, informational warnings need no input, and unknown or permission prompts block locally. | A127,A141 |
| R79 | B,I | The one-shot installer and explicit update use a staged checked bundle, isolated dependency environment and owned user-local launcher, preserve user-edited files and report PATH or duplicate-copy limitations. Installation is separate from runtime connection and authentication. | A134,A136,A137,A142 |
| R80 | H | Stamp the running `{version, bundle_digest}` in each checkpoint and refuse a new admission or Governor mutation if either differs, including a checkpoint missing the digest. Reload the skill and write a fresh checkpoint; existing native request recovery remains available. Record preference and Governor policy revisions separately so a model edit does not supersede candidate proof. | A124,A132,A142 |
| R81 | H | An obligation has an id, introducing sequence, kind (`criterion`, `delivery`, `assurance`, `correction`, `steer`, `subgoal`), provenance (`objective`, `user_direct`, `project_policy`, `coordinator`), `check` or `resolves` with `stop_condition`, boundary and source. Subgoals, corrections and coordinator assurance name a parent. Objective obligations enter at intake; direct user instruction may introduce or withdraw any kind. Coordinator judgment may introduce only subgoals, assurance and corrections and withdraw only its own subgoals and assurance with a reason. Policy obligations cite a line range in declared governance sources fixed at intake: the selected target-branch base commit, source byte revision and cited text bind authority. Select the default target through Git's symbolic `refs/remotes/origin/HEAD`; a nondefault or missing default needs scoped `user_direct` revision authority. That explicit initial selection establishes the exact trusted target snapshot even when candidate equals base; pre-intake policy authorship is not observable. An untrusted proposed ref grants no authority, and literal `HEAD` is rejected. Bind the canonical target identity and snapshot at intake. Retain checkpoint candidates and explicit snapshot decisions in bounded kernel-owned history. A later target update sharing candidate or result ancestry beyond the old base needs a fresh direct user decision bound to the exact new target snapshot; intermediate and older recorded commits count, and a generic selector or stale decision cannot authorize it. Independent target updates can refresh normally. A target identity change also requires `user_direct` revision. Outside Git there are no governance sources. On base or authority change, atomically reconcile the map before new admission or governed effect, preserving ids and history. Re-read the same sources at the new base; moved exact cited text remains valid. Gone text permits justified withdrawal of that policy's own obligations only, never objective or user obligations. New requirements need citations; unavailable sources hold new work. Invalidate affected packets and evidence, explicitly rebind unaffected evidence. Bind canonical private policy restrictions separately through existing authority. Pending native attempts keep immutable recovery, and policy change never reopens a closed objective. Violations refuse `obligation_invalid`. | A146,A147,A160 |
| R82 | H    | Worker reports, reviewer advisories, issue or repository text, fetched documentation and coordinator caution add only proposals. Proposals change no criterion, gate, acceptance or authorization. A proposal becomes an obligation only through a `user_direct` revision or a `project_policy` citation.                                                                                                                                                                                                                                            | A144,A145,A147         |
| R83 | H    | While a map exists, each validated write gives every obligation exactly one state: `unassigned` (only in its introducing checkpoint), `active` (an outstanding admission that serves it, or the coordinator), `waiting`, `blocked_external` (an external party, need and unblock condition), `satisfied` (currently valid passing evidence, and no served result without a disposition) or `withdrawn`. At most one obligation is coordinator-held `active`, and its boundary does not overlap an outstanding admission. `satisfied` whose evidence is invalidated must be re-stated. A violation is refused `obligation_unaccounted`. Status shows every state with its referent. | A148,A151              |
| R84 | H    | A wait has exactly one controlling reason class — `dependency`, `contract_unsettled`, `sequenced`, `ownership`, `capacity`, `integration_pending`, `input_unavailable`, `authority`, `user_hold` — and a referent that is valid for that class. Dependency and contract waits are acyclic. A wait whose referent has resolved is invalid at the next write. Another active worker is not a class. A violation is refused `wait_invalid`.                                                                                                                                      | A149,A150              |
| R85 | H    | A packet names the obligations it serves, a role and a declared boundary. Admission is refused `unbound_assignment` when a served obligation is missing, proposed, satisfied, withdrawn, outside the current revision or already served by an outstanding admission; when a review packet does not serve an assurance obligation; or when an investigation lacks `resolves` and `stop_condition`. An implementation packet whose boundary overlaps an outstanding admission or the coordinator-held boundary is refused `ownership_conflict`. Admission sets the served obligations `active` in the same locked write as the admission row. | A143,A151,A152         |
| R86 | B,H  | At report consumption, Pod compares a settled implementation result's complete changed paths from Git with its declared boundary and records `boundary_exceeded`. Unknown, unavailable, or over-limit paths cannot establish non-overlap and hold further implementation until complete evidence or a reasoned disposition; over-limit paths cannot be integrated from incomplete evidence. Each result takes one disposition: `integrated_into` (validated by ancestry when a commit exists, otherwise coordinator attestation; needs a reason if the boundary was exceeded), or `discarded` with a reason, which preserves branch and evidence. Until then its owner obligation is coordinator-held or `sequenced`, and an overlapping implementation admission is refused `integration_pending`. The scoped efficiency exception cannot lift this. | A153,A154              |
| R87 | B,H | An assurance obligation records scope, question, candidate, existing evidence and insufficiency. An overlapping second obligation on the same candidate names an uncovered risk, including when the first is withdrawn but its accepted proof remains current. A report is consumed only after exact native settlement and is immutable per Dispatch; exact replay is idempotent, while changed report or result evidence needs a fresh attempt. Findings record reviewer severity and triage: `required_correction` creates a parented correction obligation, consolidated on the candidate lineage; `advisory` creates a proposal; a downgraded blocker or major finding needs a reason. The obligation is satisfied by a completed, validated review observation bound to the current candidate, or a valid REUSE binding, together with satisfied or user-withdrawn corrections. A still-valid accepted proof relationship cannot be erased or restated to admit repeat review. Recording a required correction from any legitimately admitted completed review invalidates earlier accepted assurance proof, even when the report arrives late or old bindings return. Admission timestamps do not determine that order. Advisory additions cannot evict the correction's safety record; qualification waits for corrections and suitable current review proof. Definition-bound receipt history survives omission and candidate metadata changes. A correction affecting reviewed scope, assumptions, dependencies or governance, or with unknown impact, reopens the obligation for affected-scope delta review. Explicit candidate REUSE binding with the Git delta preserves unaffected evidence; stricter project policy governs. | A155,A156 |
| R88 | H | The `independently reviewed` label requires at least one non-withdrawn assurance obligation, with every non-withdrawn assurance satisfied by a binding valid for the delivery candidate. Zero or all-withdrawn assurance withholds the label. When the binding condition is unmet, the report stands, the label is withheld, and `assurance_unbound` names the gap. Stricter project rules still govern. | A157 |
| R89 | H | An objective is quiescent when nothing is active and every unfinished dependency or contract-wait chain ends in `blocked_external`, `authority`, `user_hold` or `input_unavailable`. It stays open, preserves its dependencies and reports incomplete. It closes when every obligation is satisfied or withdrawn and the closure report is written. After closure, admission and Governor mutation are refused `objective_closed` until a `user_direct` revision reopens it. Closure promotes no label and grants no acceptance. | A158,A159,A160 |
| R90 | B,H  | 0.6.0 is a hard objective-state cutover. `pod update` prints a read-only notice of objectives on superseded schemas. Status and doctor list them with their schema and block them. Nothing converts them, and in-flight workers are settled through Orca. New objectives are unaffected.                                                                                                                                                                                                                                     | A161                   |
| R91 | B,I  | Boundary refusals and properties P1–P6 are deterministic and generative tests. Coordination behaviour is evaluated on recorded, sanitized traces by scripted record checks: the efficiency triangle (slot filling refused; serialization flagged when an independent, dependency-free, non-overlapping obligation stays `sequenced` across two accepted map writes with free capacity and positively available delegation, without a bounded rationale and revisit condition bound to current inputs; amplification refused when a review repeats a valid binding), churn flagged at three settled admissions on one obligation without satisfaction, and scope inflation refused. Wall time and admission counts are recorded observations, never gates. There are no automatic paid runs. | A150,A162              |

The accepted boundary records establish six properties. P1, **structural obligation
accounting**, requires a new `unassigned` obligation to coexist with an active
obligation in its introducing checkpoint. Every state and wait validates
independently; without active work, unfinished dependency chains terminate in
`blocked_external`, `authority`, `user_hold` or `input_unavailable`. Quiescence
follows these chains without deleting dependencies. P2 forbids authority from
discovery alone. P3 binds every admission to a current unsatisfied obligation,
with at most one outstanding admission per obligation. P4 gives every
undispositioned result a coordinator-held or sequenced owner; discarding it
releases the integration hold. P5, **assurance-binding integrity**, qualifies the
review label only through R88 and refuses repeat review of a valid binding; it
does not certify review truth or completeness. P6 bars new admission and
Governor mutation after closure until direct-user reopening.

The map is checked at creation, revision, admission, report consumption,
coordinator completion, interruption checkpoint, quiescence and closure.
The governance paths fixed at intake are canonical project policy,
`.pod/config.yaml`, and tracked root `AGENTS.md` and `CLAUDE.md`, subject
to direct-user exclusions; a candidate copy cannot amend their authority.
Admission persists the served obligations' active state with its row. Result
disposition never by itself satisfies an obligation. `integration_pending`
requires an undispositioned result and boundary overlap. A `capacity` wait
requires reservations at the effective ceiling; below-ceiling capacity is
`wait_invalid`. Boundary overlap is component-wise path-prefix overlap or an
equal named surface.

Checkpoint `verification` records the current environment and dependency identities.
Non-assurance proof uses the canonical detailed `pod-evidence/v1` contract,
associated with the obligation and bound to its candidate, sources, effective
Governor policy, dependencies and environment. Map receipts additionally name
the obligation's `definition` fingerprint. Pod computes that fingerprint from
identity/parent, check or decision/stopping condition, and verified scope (the
assurance's scope/question/risk context, or the ordinary obligation's boundary).
State, candidate metadata, map revision and executable test names are separate;
human outcome prose need not equal a test command. Review admission `binding`
freezes served `definitions` and governance alongside the same verification context.
REUSE is explicit and definition-bound, preserving only unaffected bindings.

Each obligation retains kernel-owned `receipts`, including omitted or invalidated
proof, within the existing 16-receipt bound. A detailed receipt's `reference` names
one immutable observation within its obligation; a fresh observation needs a fresh
reference. Review receipts use the immutable attempt identity. Changing an observed
receipt's definition, candidate or content refuses `receipt_conflict`; full history
refuses `receipt_limit` instead of evicting provenance. Only a successfully satisfied
write stamps `accepted_seq` and its explicit REUSE binding. A still-current accepted
assurance refuses reopening as `assurance_still_bound`; an incomplete review does
not acquire that protection. Restored bindings do not erase an already admitted
native review or qualify its incomplete work. Authorized withdrawal retains history, and actual
binding invalidation or an authorized changed definition permits fresh review.
A failed, incomplete or scope-unreconciled review observation cannot satisfy assurance.
Reservation stamps immutable `admitted_seq`; successful report consumption
stamps immutable `reported_seq`; triage stamps `recorded_seq`. Correction
completion records `resolved_seq` from its accepted proof or authorized withdrawal.
Review proof covering that correction must be admitted and reported after
resolution. Missing required ordering evidence fails closed; timestamps cannot
supply it. Genuinely unaffected assurances retain explicit REUSE. Findings retain
all entries within the 32-entry bound and refuse `finding_limit` on overflow
instead of evicting history. A completed review may still require code corrections.
The private `brief` operation returns an obligation map while remaining a read-only
draft operation; runtime admission still requires a validated checkpoint.

Private boundary errors include `map_stale` for a stale checkpoint sequence,
`governance_changed` for a target-base change requiring refresh, and
`governance_unavailable` when that source observation cannot be made. Superseded
objective schemas are reported as `objective_superseded`, without conversion.
Each refusal identifies the affected record and the next safe action.


## Public interfaces

These elaborate the requirements above; there is one command implementation in the installed bundle.

| Command | Contract |
| --- | --- |
| `pod` | Opens the model TUI on a TTY; otherwise prints a concise plain summary. |
| `pod config [--json]` | Read personal path, byte revision, mode, saved/effective states, eligible ids, worker ceiling and compact catalog guidance. |
| `pod config edit` | Opens the personal YAML in `$VISUAL` or `$EDITOR`, validates afterward, retains invalid edits and reports them. |
| `pod status [--run] [--json]` | Objective/source, worktree, constraints, workers, route decisions, drift, blocker and next action. |
| `pod doctor [--json]` | Read-only installation, catalog, preference, runtime capability and version diagnostics. |
| `pod update` | Runs the installer update path; active coordinators reload, active workers continue. |
| `pod --version` | Reports the installed root `VERSION` before dependency checks. |
| `pod internal <op> --input FILE` or `--input -` | Hidden structured operation for the skill; bounded JSON from a regular file or stdin, with no public command tree. |

Codex invokes `$pod ...` and Claude Code invokes `/pod ...` inside an existing
conversation. The global launcher is user-local; the one-shot installer from `main`
places both skills through the skills CLI and supplies isolated dependencies. It
may add a minimal owned PATH block when needed. There is no package, tag or release.

## Configuration and defaults

Personal `${XDG_CONFIG_HOME:-~/.config}/pod/config.yaml` is the only model-preference
authority. The resolved actual path appears in UI and diagnosis. Process-scoped
absolute `POD_CONFIG_HOME` and `POD_STATE_HOME` are for disposable validation only;
they do not redirect agent or Orca profiles. Project `.pod/config.yaml` may contain
only `schema` and restrictive `waste_governor` settings. No per-project model pool
or task preference file exists.

```yaml
schema: pod/v1
selection: custom
models:
  claude-opus-5-5: available
  claude-fable-5-1: available
  claude-sonnet-5: available
  gpt-6-astra: available
  gpt-6-sol: available
  gpt-6-luna: available
workers:
  max_active: 2
```

`preferred`, `available`, `disabled` are mutually exclusive saved states. Preferred
is eligible and a modest suitability tie-breaker; Available is eligible; Disabled
is ineligible for new starts. `selection: all` makes all six available while
preserving the saved map. Returning to `custom` restores it. A state edit in All
models applies to the saved map and changes the mode to My selection in one write.
An empty custom pool is valid and disables delegation only. A syntactically valid
sparse custom map keeps its explicit choices through All models and back; omitted ids are shown as
“Not set (not eligible)”. Missing files and invalid or incomplete YAML structure
have no eligible pool and never default to All models; no internal operation writes
preferences. The installer creates the
initial file with all six Available and `max_active: 2`; `pod config edit` may
create that default if the file is missing. The accepted range is 0–8.

A write locks briefly, re-reads the targeted key, preserves unrelated changes,
validates the result and atomically replaces the file. A conflicting target asks
for a fresh action. A 0.4.0-shaped file with the same `pod/v1` schema is rejected
by shape, not converted. The personal file's SHA-256 byte revision is
`preference_revision`; `policy_revision` is the digest of effective Governor
policy alone. Model edits do not open a Governor candidate generation.

The sole manually maintained bundled catalog holds exact model identity, agent,
documented efforts and native context information, attributed official guidance,
clearly labeled Pod examples, checked source dates and the dated Artificial
Analysis reference snapshot. The six ids are `claude-opus-5-5`,
`claude-fable-5-1`, `claude-sonnet-5`, `gpt-6-astra`, `gpt-6-sol` and
`gpt-6-luna`. `python -m pod.catalog --check` validates it. AA's selected
reference profile is dated 2026-09-24; profile, intelligence, USD per benchmark
task and first-chunk seconds stay together in one variant row. Missing or incomplete
optional benchmark rows show unknown metrics as `—` without blocking selection,
admission or config reads. If any score is missing, the six-model ranking is
unknown. Ranking otherwise uses competition rank among the six supported base models,
never AA's global rank. The TUI attributes
`https://artificialanalysis.ai/leaderboards/models`, shows benchmark age and
states: “AA metrics show each model's selected reference benchmark profile and
are informational only. Pod chooses effort/context independently for real work.”
Native account usage is separate from these observations.

The initial AA reference rows are dated observations, not billing quotes or
selection rules. Each value below belongs to the named profile; other effort
variants remain read-only detail in the same bundled snapshot.

| Base model | Reference profile | Intelligence | USD/task | First chunk, s |
| --- | --- | ---: | ---: | ---: |
| Claude Opus 5.5 | max with fallback | 58 | 5.98 | — |
| Claude Fable 5.1 | max with fallback | 53 | 7.63 | 274.43 |
| GPT-6 Astra | max | 53 | 3.26 | 352.15 |
| GPT-6 Sol | max | 48 | 1.06 | 136.12 |
| Claude Sonnet 5 | max | 38 | 5.09 | 150.02 |
| GPT-6 Luna | max | 37 | 0.07 | 106.86 |

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
and map sequence. History survives admission and report writes; caller omission
or replacement cannot erase it. Capacity refuses rather than evicts. Shared
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

Worker interaction follows this bounded decision table:

| Situation | Coordinator action |
| --- | --- |
| Routine question | Answer through `orca orchestration reply`. |
| Owner-only question | Escalate. |
| Faster-model advisory | Keep the route; use native dismissal only if available. |
| Informational warning | No response. |
| Unknown or permission prompt | Localized blocker; never auto-accept. |
| Safety refusal | No reroute. |

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

## Acceptance scenarios

These are expected outcomes, not evidence. Parameterized tests may cover multiple
scenarios. Behavioral/live claims cannot be certified by checking document text.

| ID | Required result |
| --- | --- |
| A01 | Invoke Pod in either host: the same conversation remains coordinator with context and coordinator configuration preserved. |
| A02 | Trivial mechanical work uses tools/current session with zero unnecessary workers or milestone ceremony. |
| A03 | Plan-only permits appropriate investigation but no implementation worker or product edit. |
| A04 | Plan-then-execute proceeds without redundant approval; real scope/authority changes stop dependent actions. |
| A05 | A suitable eligible model is chosen with an assignment-specific reason; Preferred only breaks close suitability ties. |
| A06 | An unsuitable or unavailable choice yields another eligible suitable proposal or a precise blocker without silently widening the pool. |
| A07 | An explicit model restriction produces no silent substitution. |
| A08 | Requested and effective launch values remain distinct; unknown or mismatched proof stays visible and blocks acceptance. |
| A09 | Project, issue and worker input cannot expand the personal model pool or change host/project permissions. |
| A10 | Catalog discovery alone cannot enable a model or prove installed capability. |
| A11 | Preference edits govern later starts; active attempts retain their original route and decision revision. |
| A12 | A Disabled state blocks the next start unless direct, scoped user intent permits an exception; prior effects are preserved. |
| A13 | A recorded native rate limit holds the same route until retry-after or a meaningful change. |
| A14 | Missing service-limit metadata does not lower ordinary worker concurrency. |
| A15 | A recorded unavailable or authentication failure is not retried blindly. |
| A16 | An alternative route waits for exact settlement or proven no-start of the failed attempt. |
| A17 | Model-state actions change no provider billing, service or account setting. |
| A18 | Five Orca-ready Tasks, each serving a distinct unsatisfied obligation, with a ceiling of two. Two admit. The third is refused `logical_capacity_full` and records a `capacity` wait naming both reservations. After exact assignment settlement (including a stopped failed Dispatch with a retained terminal), the capacity wait is invalid at the next checkpoint and the next obligation-bound Task admits. A ready Task serving only a proposal is refused `unbound_assignment`, even with a free reservation. |
| A19 | `workers.max_active` accepts 0–8 and cannot be widened by an objective constraint. |
| A20 | Descendant delegation requires direct user intent and counts under the same objective ceiling. |
| A21 | Concurrent managed admissions serialize objective-local reservations and do not exceed logical fan-out; stable native coordinator authority is still required. |
| A22 | Lost start/request responses retain their logical reservation and recover the same immutable admission through exact Orca request/native identity without a blind replacement or repeated semantic start. |
| A23 | Native duplicate/delayed messaging remains Orca-owned; Pod report ingestion joins a frozen packet and exact fresh Dispatch identity without a parallel acknowledgment ledger. |
| A24 | Overlapping writes/existing dirtiness isolate or serialize while preserving owner changes. |
| A25 | Two equivalent failed corrections trigger diagnosis/discriminating check before another attempt. |
| A26 | New Tasks get fresh sessions; compatible corrections may reuse; unsupported route changes get fresh bound attempts. |
| A27 | Worker success with failed required checks leaves objective incomplete. |
| A28 | Substantial or high-risk changes receive candidate-bound independent review through recorded assurance obligations, including under stricter project rules.                                                                                                                                                                                                                         |
| A29 | Material post-review change invalidates affected proof; unaffected reuse needs valid bindings/project rules. |
| A30 | Steering revises/reconciles affected work, preserves useful unaffected work and excludes obsolete results. |
| A31 | Interruption supports native-state adoption/checkpoints without background-reasoning claims. |
| A32 | Global/local coexistence preserves project policy and diagnoses duplicate/shadowed/mismatched skills. |
| A33 | Repeated installer runs and update preserve user files and do not create duplicate active bundles. |
| A34 | Config/status/doctor and internal validation reads call no models, hooks or dispatch and perform no hidden repair. |
| A35 | Secret-bearing context/malicious reports do not expose protected material or promote observations into authority. |
| A36 | No fallback intended to bypass a safety refusal. |
| A37 | Unobserved tokens/cost remain unknown; worker counts/model labels do not become savings/provider-compute claims. |
| A38 | Requirement/scenario references have no duplicate/orphan IDs, unclassified requirement or contradiction. |
| A39 | No competing native task database, scheduler, autonomous reasoning/restart loop, dashboard or marketplace. |
| A40 | The public launcher has only the documented small command surface; `internal` is hidden and structured. |
| A41 | Host integrations derive from one policy, use verified discovery roots, stay inline and never override coordinator model/effort. |
| A42 | Side-effectful read-only-labelled tooling is withheld in plan-only mode; planning output obeys host rules. |
| A43 | The obligation map covers each original criterion, delivery obligation and human/provider dependency with provenance and a check, without a user-authored milestone file.                                                                                                                                                                                                            |
| A44 | Small high-risk, large repetitive and mixed-complexity assignments demonstrate method-first independent assessment. |
| A45 | Fresh install writes six Available states without a model-approval ceremony or model call. |
| A46 | One personal YAML stores model states and worker ceiling; project YAML can only narrow Governor policy, while objective constraints stay local. |
| A47 | Duplicate/type/unknown-field/executable-tag/include and oversized/recursive YAML fails safely without execution. |
| A48 | Catalog identities, documented efforts and runtime capability remain separate; unsupported selection is refused. |
| A49 | A valid sparse custom map makes only explicit eligible states available and labels omissions “Not set (not eligible)”; invalid structure or a missing file disables new delegation while preserving read-only diagnosis and native recovery. |
| A50 | Captured choices validate against a supplied preference snapshot without a ranking formula or model call. |
| A51 | Missing native metadata remains unknown and causes no credential or undocumented-endpoint scraping. |
| A52 | Current preferences are read at selection and final admission without a daemon or effect on productive workers. |
| A53 | A state change cannot enable paid provider settings or make an unavailable native model launchable. |
| A54 | Concurrent edits preserve unrelated keys, refuse a stale targeted key and never overwrite invalid YAML. |
| A55 | Worker-provider changes leave coordinator unchanged; headroom/checkpoints explicit and no permanent setting edits. |
| A56 | Current native workers without terminals use worker identity/lifecycle APIs; missing terminal alone is not failure. |
| A57 | Missing launch-preferences capability blocks delegation; optional context selection is omitted rather than asserted. |
| A58 | Accepted but unproven submission triggers observation, not automatic Enter/resend/replacement/acceptance. |
| A59 | Faults around reservation/start/request receipt preserve uncertainty, exact identity and request recovery without double-counting or a duplicate start. |
| A60 | Missing caller, conflicting ownership or a partial handover blocks delegation; valid adoption preserves the work without a proxy coordinator. |
| A61 | Unsettled contracts start one writer; project isolation/hooks honored without weakening host protection. |
| A62 | Packets/reports enforce bounded fields, criterion/candidate/scope/runtime-issued identity; reject transcript cloning and report permissions. |
| A63 | Relevant files/instructions/requirements/candidate changes invalidate context; unchanged bound context can be reused. |
| A64 | Changed/absent/redirected sources differ from unavailable reads; definitive rejection survives restoration; unrelated dirty paths do not grant sensitive reads. |
| A65 | Restarted correction history recognizes equivalence; rewording is insufficient but discriminating evidence permits changed attempts. |
| A66 | Cheap discriminating checks precede expensive work; independent review receives exact candidate and reproducible unprimed evidence. |
| A67 | Source/config/dependency/environment/candidate changes invalidate affected proof; no fabricated Git metadata and explicit alternative source binding. Ordinary receipts bind the verified definition; authorized check/scope changes invalidate old proof, and omission/restoration or candidate relabeling cannot refresh it. |
| A68 | Completion labels/final report follow evidence/project authority; failures, blockers, route exceptions and unresolved native references remain visible. |
| A69 | Interrupted versioned checkpoints recover privately and atomically; retention/native-first recovery protect unresolved effects/evidence. |
| A70 | Feedback may suggest but never auto-edits preferences or launches unauthorized live work. |
| A71 | Ambiguous status requests selection and reports pool, constraints, decisions, drift, verification gaps and next action. |
| A72 | Foreign or edited launcher/skill copies are preserved and reported with an actionable path. |
| A73 | Unregistered canonical relocation/prohibited placement refused; owner-managed registration is a separate cutover gate. |
| A74 | Missing live PASS for Claude Code or Codex on Linux, or for either delegation adapter, keeps live verification NOT_RUN despite synthetic CI and a read-only doctor. |
| A75 | Hosted Linux CI, incident discovery, compile/whitespace, skill validation, source hygiene, skills-CLI installation, independent audit and implemented-only docs cover each change to `main`; CI publishes nothing. |
| A76 | `pod config edit` targets the personal file and preserves invalid user edits while blocking delegation. |
| A77 | The one-shot installer from `main` installs both global skills and the user-local `pod` command from one bundle. |
| A78 | A copied bundle runs from an unrelated directory with no `PYTHONPATH` and no source tree; a missing prerequisite prints one actionable step, never an import traceback or an invented payment requirement. |
| A79 | Native-default context does not block an otherwise valid model start; actual context insufficiency is disclosed and the packet narrowed or decomposed. |
| A80 | Installer receipt and bundle digest identify ownership; changed copies are preserved and shadowing paths reported. |
| A81 | A superseded candidate and premature validation each defer with an explainable reason and next action; an identical running action is attached to and a passing result for the same candidate and context is reused; a necessary rerun after changed input proceeds. |
| A82 | A scoped Governor efficiency exception affects only efficiency deferrals, never authority or correctness. |
| A83 | A governed merge, release or deployment without owner authorization defers with the missing authorization named; a record naming the exact candidate, tree and scope permits it, and a malformed record or one for another candidate, tree or scope never does. |
| A84 | Several locally discoverable corrections in one delivery unit converge on the same branch and pull request; no intermediate correction crosses the remote boundary until its candidate passes the configured local preflight. |
| A85 | Two callers requesting identical validation concurrently produce one admitted execution; the other attaches to it. |
| A86 | A submission whose response is lost stays UNKNOWN, blocks a resubmission, and is settled only by provider readback; after a coordinator restart the unit's candidate bindings, evidence, decisions and pending effects remain recoverable. |
| A87 | A changed source, base, workflow, environment or policy opens a new candidate generation, and evidence bound to the previous one is not reused as current proof even on the same commit. |
| A88 | A remote-only question admits a bounded diagnostic naming its question, local limitation, check and stopping condition while the candidate is still converging, without pretending the candidate is release-ready; an unchanged repeat is answered from the record. |
| A89 | An unclassified remote failure is not retried; a code defect is recorded as a correction, and the third equivalent correction requires a diagnosis with distinct bounded evidence before validation resumes. |
| A90 | Supersedence cancels a pending, cancel-safe validation of the old candidate only when policy allows it, its result can never approve the newer candidate, and a pending deployment is never canceled by supersedence. |
| A91 | An independent or urgent delivery unit is admitted while another unit's workers, deliveries or corrections are unsettled. |
| A92 | A project file that relaxes Governor mode, widens retries, enables cancellation, declares host control or adds an exception is refused. |
| A93 | The enforcement level is reported as advisory unless the owner's personal policy declares a host control, and it is never reported as a proven control. |
| A94 | Pod exposes no private Delivery, cleanup, release, terminal or lifecycle mutation operation; Orca owns those states, while reports and Governor decisions use exact objective assignment evidence when needed. |
| A95 | Completed, pending and absent Orca request recovery binds the same immutable admission without a second semantic start; invalid UUID, changed authority/worktree, contradictory receipt or ambiguous native attempt holds. |
| A96 | Orca's documented effect-free refusal records durable deferred evidence with no binding or blind retry; `runtime_error` and any unknown, malformed or partial-effect refusal remain unresolved until native readback settles them, and physical-capacity enforcement stays unavailable. |
| A97 | One installed conditional reference defines the readable Pod Execution Spec skeleton, terms, numbered PoD items and explicit delivery endpoint; no parallel template exists. |
| A98 | A representative filled spec remains ordinary readable Markdown without parser-only boilerplate or empty ceremony. |
| A99 | Complete authorized issue intake returns the full body and exact identity, while inaccessible or incomplete reads fail clearly without requesting credentials or claiming success. |
| A100 | An issue for another repository is rejected against actual Git identity before implementation; issue text/comments cannot grant authority. |
| A101 | A closed issue requires outcome and user-intent reconciliation rather than silently repeating work. |
| A102 | Body changes require reconciliation while metadata-only updates preserve valid derived work; relevant amendment bindings remain bounded. |
| A103 | Continuation reuses objective/native state across linked worktrees, preserves unaffected evidence and prevents a revised packet from replacing an uncertain same-Task attempt. |
| A104 | Issue and direct objectives share proportionate plan-only, plan-then-execute, continuation and direct-work behavior under actual host permissions. |
| A105 | Actual Git repository/common-dir, branch and path bind the objective worktree; matching display text is insufficient and dirty/colliding owner work is preserved. |
| A106 | Canonical private project policy remains effective in a linked objective worktree and worktree/task policy can only narrow it, without copying the private file. |
| A107 | A normal delegated worker is started through native Orca into its own visible agent tab; terminal absence alone is not treated as proof of no tab. |
| A108 | After exact report consumption/preservation, native Delivery acknowledgment and worker release occur promptly in guide order; reuse requires an immediate supported follow-up. |
| A109 | Final cleanup checks exact objective workers only, protects uncertainty/foreign resources and removes a worktree only after integration, preservation and cleanliness. |
| A110 | Installed Orca guidance is loaded progressively and missing delegation controls do not block safe direct work or diagnostics; no wrapper/shared-setting workaround appears. |
| A111 | Generic tracked-source/artifact hygiene detects representative private residue while permitting Pod identity, public links and sanitized fixtures. |
| A112 | Public code and guidance require no external metadata service; AGENTS keeps only the short upstream ownership notice and introduces no competing writer. |
| A113 | README covers the one-shot installer, normal session journey, TUI, limits, update and removal with linked detailed guidance. |
| A114 | SKILL remains at most 750 words, each conditional reference at most 700 and their combined total at most 2200; Execution Spec loads only for issue/spec work. |
| A115 | Human status shows objective/source, selected worktree, relevant native work, blocker/next action and remaining gates; JSON retains detailed evidence without side effects. |
| A116 | Exactly six base ids are selectable; effort variants remain detail, context uses native default without invented flags, and effective values are separately observed. |
| A117 | Focus updates the always-visible Details panel; model edits save immediately and the All models toggle restores saved states. |
| A118 | The root `VERSION` holds one MAJOR.MINOR.PATCH line and is the only authored version; the bundle's `VERSION` links to it, an installed copy carries it as a file, `doctor` reports it, and no other file declares a version. |
| A119 | Public commands and JSON/private output match the small launcher contract, with no accidental installer or artwork output in machine reads. |
| A120 | One personal file controls the three exclusive states; a fresh install enables all six without a ceremony. |
| A121 | Direct constraints narrow the pool; a scoped Disabled exception needs explicit user intent and lapses after a conflicting preference change. |
| A122 | The six-model catalog validates exact identities, official source URLs/dates, native effort labels and separate AA variant metadata. |
| A123 | Coordinator choice is proportionate and independently reasoned; a deterministic guard validates but never ranks suitability. |
| A124 | Preference changes before final admission refuse stale choices; post-row edits preserve an in-flight request and pending replay ignores preferences. |
| A125 | TUI sort, filter and AA metrics cannot influence the selected route or start a worker. |
| A126 | Missing runtime metadata stays unknown without account probes or inferred native capability. |
| A127 | Actual route failures honor retry-after and require settlement before alternate dispatch; a safety refusal bars same-Task reroute. |
| A128 | Two concurrent TUIs preserve unrelated edits and reject a stale targeted edit; failed persistence never reports Saved. |
| A129 | Supported effort is used or omitted as native default; context is omitted without native control and effective metadata remains honest. Null, absent and irrelevant extra launch fields do not create a mismatch, while known contradictions do. Objective-bound acceptance holds a recorded route mismatch or unknown effective route despite passing caller checks and owner authorization; a known matching route can pass. |
| A130 | The personal ceiling allows zero to eight logical workers. The default of two includes reviewers, is not a utilization target and does not depend on service-limit data.                                                                                                                                                                                                             |
| A131 | A settled same-Task terminal reuse copies the prior effective route, rechecks eligibility and sends no model or effort flag. |
| A132 | Versioned records refuse other schemas without conversion; version drift blocks new mutation but leaves recovery available. |
| A133 | Status and doctor show preference path/revision, constraints, native evidence, drift, mismatch and next action read-only. |
| A134 | One-shot install/update is idempotent and preserves valid YAML, modified copies and foreign files. |
| A135 | Unit, PTY/subprocess, installed-bundle, hosted, live and review evidence are recorded separately. |
| A136 | Root VERSION is the only authored version and installed skills and launcher report it consistently. |
| A137 | A truncated download or interrupted dependency/skill copy yields no false success, and rerun repairs an incomplete installation. |
| A138 | No context control blocks no valid start; missing launch-preferences capability blocks only delegation. |
| A139 | The six focus-driven Details summaries, labels, AA metrics and age stay visible at ordinary terminal sizes and adapt accessibly. |
| A140 | State changes save immediately, and All models toggles back to exact saved choices across restarts and external edits. |
| A141 | Routine worker questions get coordinator replies; advisories retain route, while permission or unknown prompts block and safety refusal does not reroute. |
| A142 | A new install and explicit update leave ordinary agent sessions and running workers unchanged; coordinators reload before new starts. |
| A143 | **Slot filling.** With a free reservation and only proposals open, nothing is admitted, and a packet serving a proposal is refused `unbound_assignment`.                                                                                                                                                                                                                         |
| A144 | An investigation packet without `resolves` or `stop_condition` is refused. The discoveries in an admitted investigation's report land as proposals and change no obligation.                                                                                                                                                                                                      |
| A145 | A worker report proposing new owner decisions leaves every obligation unchanged, and the report lists the proposals as out of scope.                                                                                                                                                                                                                                              |
| A146 | A coordinator criterion, a subgoal without a parent, or a subgoal with neither `check` nor `resolves` is refused `obligation_invalid`. The same item added by direct user instruction lands as a revision.                                                                                                                                                                        |
| A147 | A review requirement cited from `AGENTS.md` at the independently selected target base revision lands as a `project_policy` obligation. The same text from the issue body, another repository file, fetched documentation or a candidate edit outside an explicitly trusted target snapshot stays a proposal. The canonical default target comes from symbolic `origin/HEAD`, including its genuine checked-out local alias. A new nondefault or missing-default target needs scoped direct-user selection, which establishes the exact initial trusted snapshot even when candidate equals base. Initial default equality also works. After intake, candidate-only policy cannot gain authority through equal bytes, an existing target ref or plain refresh. A target update sharing known objective candidate or result ancestry beyond the old base needs a fresh direct-user decision for that exact new snapshot, including intermediate commits and older checkpoint candidates. Generic, mismatched or stale authorization is refused. Candidate history and exact user decisions persist through subsequent map writes and cannot be erased by caller round-trip; bounds fail closed. Missing old objects hold unless that exact new snapshot is authorized, without deleting the historical records. Independent target updates refresh normally. `HEAD` cannot be a governance source. `project_policy` cannot withdraw an objective criterion. At a changed target base, unchanged or moved exact cited text remains authoritative; genuinely gone text permits justified withdrawal of that policy obligation only. A changed target identity requires direct-user revision. Unavailable declared sources hold new work. |
| A148 | A write that omits a state, gives two states, leaves `unassigned` past its introducing checkpoint, marks `satisfied` without passing evidence, keeps `satisfied` after its evidence is invalidated, or keeps a worker `active` after its admission settles is refused `obligation_unaccounted`.                                                                                              |
| A149 | An unclassified wait, a wait on an unknown or resolved referent, a dependency cycle, `sequenced` with no coordinator-held obligation, `capacity` below the ceiling, or `ownership` without overlap is refused `wait_invalid`. A wait whose only stated reason is "another worker is active" has no class and is refused.                                                                              |
| A150 | **Serialization.** Two accepted map writes show an independent, dependency-free, non-overlapping obligation remaining `waiting: sequenced`, with free capacity and positively available delegation. Flag the wait unless a bounded rationale and revisit condition are bound to current inputs; validate their presence and binding, not their truth. No worker configuration or parallelism is required. `capacity` below the ceiling is `wait_invalid`. Suppress the flag when the ceiling is zero or delegation is unavailable or unknown. Counts and durations remain observations. |
| A151 | A second coordinator-held `active` obligation is refused. A coordinator-held boundary that overlaps an outstanding admission is refused. A worker packet that overlaps the coordinator-held boundary is refused `ownership_conflict`, and the coordinator records an `ownership` wait.                                                                                             |
| A152 | A second admission for an obligation that already has an outstanding admission is refused `unbound_assignment`. One correction packet serving several related correction obligations admits.                                                                                                                                                                                   |
| A153 | An implementation packet is refused `integration_pending` while an undispositioned settled result has unknown, unavailable or over-limit changed paths; a non-overlapping packet admits only when changed paths are complete. `integrated_into` is validated by ancestry. A result that exceeded its boundary is recorded as `boundary_exceeded` and needs a reason before integration. With 1,024 in-scope changed paths plus one outside, result ingestion records an over-limit hold; integration cannot waive missing complete evidence. The owner obligation cannot be satisfied while the disposition is missing.        |
| A154 | **Rejected-result regression.** Review rejects result A, which becomes `discarded` with a reason; preserve its branch and evidence. Replacement B on the same boundary admits without any supersession reference, and A remains discarded. |
| A155 | **Amplification.** A review packet must serve an assurance obligation. A second assurance obligation on an overlapping scope and the same candidate without `uncovered_risk` is refused `obligation_invalid`, including when the first was withdrawn but its accepted proof remains current. A review attempt against a satisfied assurance obligation is refused `unbound_assignment`. Removing a still-valid accepted receipt or changing candidate metadata cannot enable a repeat or hide scope overlap; bounded provenance survives. |
| A156 | A `required_correction` creates a parented correction obligation, and an `advisory` finding becomes a proposal. Downgrading a reviewer's blocker records a reason that the report lists. A legitimately admitted review's required correction remains recordable when its report arrives after a newer attempt's acceptance or an older environment or source binding returns. Earlier proof cannot qualify over that newly recorded correction, and advisory overflow cannot remove that protection. Review order is independent of wall-clock admission timestamps. A correction affecting reviewed scope, assumptions, dependencies or governance, or with unknown impact, re-opens the same assurance obligation for delta review. Unaffected evidence receives an explicit candidate REUSE binding with the Git delta; stricter project policy governs. The coordinator cannot withdraw a correction. Changing a question/check/scope under user authority invalidates old attempt receipts on the same id and candidate; a fresh attempt can qualify, while unrelated steering preserves unaffected proof. |
| A157 | With an unbound assurance obligation, none at all, or only withdrawn assurances, the report stands, the label is withheld, and `assurance_unbound` names the gap. An unsettled review report cannot be consumed, and a failed report cannot be changed to success on its Dispatch; a fresh settled review attempt may qualify. A valid REUSE binding satisfies the requirement.                                                                                                                                                                                         |
| A158 | **Quiescence.** When only a non-Owner test identity remains outstanding, as `blocked_external` with the user as party, the interim report says incomplete and names the need and the unblock condition. The objective stays open. After the user supplies the identity, continuation admits the dependent obligation without a reopen.                                                  |
| A159 | **Closure.** When every obligation is satisfied or withdrawn, the closure is recorded, and admission and Governor mutation are then refused `objective_closed`. A worker report or project-policy change does not reopen the objective; a direct user instruction does, by revision. Closure promotes no label.                                                               |
| A160 | A direct user instruction withdraws a criterion. The coordinator withdraws its own subgoal with a reason. A coordinator attempt to withdraw an objective criterion or a correction is refused `obligation_invalid`. Withdrawn obligations appear in the report.                                                                                                                  |
| A161 | After an update, a 0.5 objective is reported with its schema and blocked, and it is not converted. `pod update` printed the notice. An in-flight 0.5 worker is settleable through Orca. A new objective works normally.                                                                                                                                                        |
| A162 | Scripted record checks run over sanitized recorded traces, covering the triangle, churn and scope inflation. Wall time and admission counts appear only as observations. No automatic paid run exists.                                                                                                                                                                            |
| A163 | Trivial direct work completes without a map and the kernel stays inert. The first delegation without a map is refused `unbound_assignment`.                                                                                                                                                                                                                                      |


## Verification

Python 3.13+ on Linux, one bundle-as-package and standard-library facilities
where practical. Focused tests run during development; the complete unit and
incident suite, explicit incident discovery, compile/whitespace, catalog check,
skill validation, source audit, PTY/subprocess and disposable installed-bundle
checks gate milestones. Hosted Linux CI, live native trials on Codex and Claude
Code, independent candidate-bound review and project acceptance are distinct
facts. Missing live/provider evidence remains `NOT_RUN`.

Live trials use disposable objectives and prove both supported worker adapters:
installation/discovery, same-session coordination, an effective native route,
supervision, request recovery, verification, interruption/adoption and native
Delivery/release ordering. Unsupported optional interactions remain unverified.
No production or provider mutation is authorized by this contract alone.
