# Pod — consolidated normative specification

Status: accepted implementation contract; not a claim of implementation or verification.
Baseline: `474a84a6d5a1f7947abc1e38d232c379adf7ff93`.
Schema/version: Pod v1.

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
| Fixed launch/finish waves | Dependency-ready admission when capacity safely frees. |
| All workers need terminals and old receipt shapes are universal | Installed-version backend-specific native identity, optional terminal. |
| Automatic managed installer/PATH repair in setup | Explicit isolated installation; setup only owned enrollment/integration writes. |
| WSL implies a Windows-owned Pod controller | Verified transport, one runtime/state owner. |
| Unconditional session-age stop/restart | Evidence-based checkpoints and retained-settled-only idle expiry. |
| Release core integrations as merely unverified | Missing live core evidence blocks release. |
| Keep old aliases/machinery/tests for compatibility | No compatibility period; preserve applicable invariants with replacement coverage. |
| Passive setup means no writes | Setup may write authorized owned integration; diagnostics/default reads remain read-only. |
| Earlier proposals stay parallel authorities | Historical provenance only; this consolidated specification governs Pod. |

## Normative requirements

| ID | Type | Requirement | Sources | Scenarios |
| --- | --- | --- | --- | --- |
| R01 | B | Maintain this consolidated requirement/scenario inventory as product authority. Explicitly revise changed requirements and preserve history; implementation results cannot silently redefine them. | F1,8,12; PO–P | A40 |
| R02 | B | Keep the current conversation as coordinator. Orca owns native execution objects, placement, messaging and lifecycle; projects own governance. Helpers must not become a scheduler, competing task database, autonomous controller or restart daemon. | F1–2,9,15; PA,N | A01,A41 |
| R03 | B | Expose only pod setup, pod config, pod doctor and pod status as public command families. Planning, implementation, explanation, steering and continuation remain skill behaviors. Internal operations install no additional global executables. | F3,11; PH | A35,A36,A42 |
| R04 | B | Maintain one canonical inline skill policy with generated host metadata and relevant-on-demand references. Invocation preserves the conversation and must not set coordinator model/effort or use Claude context: fork. | F2–3,11; PA,H | A01,A32,A43 |
| R05 | B | Normal invocation inspects objective, criteria, rules and consequential assumptions, then proceeds within existing authorization. Use a compact execution brief when useful; trivial work requires neither workers nor milestone ceremony. | F3; PA | A02,A04 |
| R06 | H | Plan-only work permits useful host-permitted investigation but no implementation workers or product edits. Assess tooling side effects first. Describe behavioral restraint accurately. Plan-then-execute avoids another approval unless a real boundary changes. | F3; PA,E | A03,A04,A44 |
| R07 | B | Substantive plans identify criteria, challenged assumptions, coordinator/worker responsibilities, dependencies, editing boundaries, verification and revision triggers. Decomposition is provisional; material scope/acceptance changes require explicit revision. | F3,7–9; PA,J | A30,A45 |
| R08 | B | Choose tools, direct session work or delegation before a worker model. Assess each assignment's complexity, risk, size, uncertainty, verifiability, capabilities and context availability independently of its parent. | F4; PA–B | A02,A05,A06,A46 |
| R09 | B | Keep role, agent application, model identity and effort distinct. Do not encode a universal intelligence/cost ladder or permanently expensive reviewer role. | F4,8; PA–B,F | A05,A28,A46 |
| R10 | H | Distinguish preferred, approved and usable-now routes. Approval binds resolved agent/model/account route and restrictions. Discovery, aliases, repository files and worker reports cannot create approval. Reuse valid existing grants. | F4; PB | A09,A10,A47 |
| R11 | B | Resolve personal YAML, optional project overrides and current-task preferences into one effective table with provenance. Derive agent from the model entry. Support validation and explicit editing; display must not write configuration. | F3–4; PB,H | A05,A32,A48,A82 |
| R12 | H | Parse bounded YAML safely. Reject duplicate keys, invalid types, unsupported schema/policy fields, arbitrary tags, executable includes and resource-exhausting structures. Preference data must never execute shell commands. | F4; PB | A49 |
| R13 | H | Merge soft preferences by specificity and hard restrictions restrictively. Resolve keyed entries consistently; local lists cannot broaden restrictive lists. Project/task files cannot expand model/provider approval, spending authority, destructive permissions or hard ceilings. | F4; PB | A09,A12,A48 |
| R14 | B | Supply five starter preferences as pending capability-validated recommendations. Offer Opus/Fable as additional approvable alternatives. Exact identifiers/efforts depend on the approved installed route; names are not permanent rankings. | F4; PB,P | A10,A47,A50 |
| R15 | B,H | Start with the matching preference, filter infeasible routes and depart only for a concrete recorded reason. Strict pins prohibit substitution. Exceptions do not edit saved preferences. Never reroute to circumvent a provider safety refusal. | F4–5; PB–C | A05,A06,A07,A38 |
| R16 | H | Bind each assignment to immutable configuration/catalog revisions, assessment and route. Preferences affect future assignments; hard revocations apply at the next enforceable boundary. Invalid configuration blocks new dispatch without blocking diagnosis or authorized reconciliation/cleanup; never silently use an older revision. | F4; PB,J | A11,A12,A51 |
| R17 | B | Provide dispatch-free routing preview and deterministic replay from supplied assessment, policy, capability and quota snapshots. Helpers validate/explain decisions; they do not claim to understand arbitrary tasks through numerical lookup. | F2,10; PJ | A36,A52 |
| R18 | B,H | Read supported metadata for the actual authentication/account route. Preserve buckets/windows, observed consumption, resets, timestamps, source, freshness and uncertainty. Prefer notifications/bounded cached reads. Do not scrape credential stores/undocumented endpoints or use provider inference APIs for workers. | F2,5; PC | A13,A14,A15,A16,A39,A53 |
| R19 | B,H | Apply configurable quota heuristics: initially 20% low, 5% critical, 60-second freshness. Assess bounded work individually. Unknown quota allows at most one newly active managed worker per unobserved account route; existing workers count against that allowance. Exhaustion requires positive renewed-availability evidence. Avoid oscillation and account rotation. | F5; PC | A13,A14,A15,A16,A54 |
| R20 | H | Separate model approval from paid usage, premium modes and reset authority. Spending grants must be explicit, scoped, bounded and visible. Exclude routes whose billing eligibility could cause unauthorized charges; uncertainty disclosure is not spending permission or a zero-charge guarantee. | F5; PC | A17,A55 |
| R21 | H | Reset consumption requires the exact grant, supported idempotency, durable operation identity and subsequent quota readback. An uncertain response cannot authorize another redemption. Passive commands consume nothing. | F5; PC | A17,A22,A56 |
| R22 | B,H | Account for coordinator work and preserve practical integration, verification, recovery and reporting headroom. Checkpoint before foreseeable exhaustion. Changing workers does not move/replenish the coordinator. A supported coordinator model change must be explicit, session-scoped and visible. | F5,7,9; PC | A31,A39,A57 |
| R23 | I,H | Resolve and retain the applicable Orca executable/runtime identity. Validate advertised capabilities and installed-version contracts, including backend-specific identities and optional terminals. Do not silently substitute runtimes, execution hosts, accounts or direct provider worker APIs. | F2,5,11; PA,I | A08,A34,A58 |
| R24 | H | Validate approval, capability, effort, billing, host restrictions and fan-out controls before dispatch. Compare requested/confirmed effective configuration afterward and at observable changes. Missing proof remains unknown; policy violations hold/quarantine the result. Post-launch detection cannot replace necessary pre-launch spending protection. | F4,6–7; PA–B,J | A08,A20,A59 |
| R25 | B | Treat accepted input, started reasoning, native settlement and accepted output as different observations. Silence/lost responses do not prove failure, justify resending input or authorize replacement work. | F7,9; PA,L | A22,A60 |
| R26 | H | Use zero workers when sufficient, default capacity two, ordinary one–three and justified expansion to three. Four–eight needs an explicit reasoned grant bound to objective, Run, plan/decomposition revision and limit. Reject above eight. Free hardware alone does not justify workers or substitute compute restrictions for decomposition. | F6; PD | A18,A19 |
| R27 | H | Count investigators, reviewers and authorized descendants under the same objective ceiling across Runs. Children need explicitly delegated authority. Disable hidden fan-out unless authorized and natively accounted. Ultra/ultracode are not ordinary effort values; unverifiable fan-out controls block affected routes. | F6; PD | A16,A19,A20,A21 |
| R28 | H | Guard admission with native atomic facilities or serialized host-local admission, durable intentions and fresh native reads. Account jointly for objective occupancy/shared buckets. Launching, active, uncertain, retained and unproven-release attempts occupy capacity; reconcile without double-counting. Provider quota reservations are advisory. | F5–6,9; PC–D,L | A18,A21,A22,A61 |
| R29 | H | Establish one authoritative coordinator per objective. Adoption reconciles pending effects/native authority before dispatch. A local lock is not distributed fencing. Ambiguous caller/cross-host ownership permits safe inspection/direct work but blocks delegation; never manufacture a replacement controller. | F6,9; PL | A21,A31,A62 |
| R30 | H | Parallelize only independent responsibilities/editing boundaries; begin with one writer when contracts are unsettled. Use project/host-supported isolation. Preserve unrelated changes; never silently stash/reset/clean or execute unauthorized setup hooks. | F6–7; PA,D | A24,A44,A63 |
| R31 | B | Use bounded versioned packets carrying objective/criteria, responsibility, scope, candidate, context references, dependencies, permitted actions, route, revisions, reporting contract and native bindings when issued. Do not clone the full coordinator transcript or predict runtime identities. | F7; PA | A24,A37,A64 |
| R32 | B | Reuse bounded context only while relevant source, instruction, requirement, candidate and revision bindings remain valid. Invalidate affected summaries/evidence on change. Do not introduce automatic repository uploads or a vector database. | F7,10; PJ | A29,A30,A65 |
| R33 | H | Preserve bounded provenance-aware source access/relevant candidate identities. Exclude secret sources/unnecessary content reads. Distinguish proven changed/absent from unavailable sources; neither grants admission. Definitive rejection is not erased by restored bytes. Do not claim atomic multi-file snapshots or protection from arbitrary external writers. | F7–9; PA,M | A37,A66 |
| R34 | B,H | Workers report scope changes, checks/results, failures, evidence, uncertainty and questions against the assignment. Reports/logs are untrusted observations; they cannot expand authority, change budgets or establish acceptance. | F7–8; PA,M | A23,A27,A37,A64 |
| R35 | B | Prefer native events/blocking waits. Record/reconcile every Delivery item before acknowledgment; duplicate/delayed delivery is idempotent. Bind effects to exact native identities, retain evidence safely and do not acknowledge unresolved required effects. | F7,9; PA,L | A23,A61 |
| R36 | B | Use fresh sessions for new Tasks. A settled worker may handle compatible same-Task correction when supported. Changed model/effort/account route requires a newly bound attempt unless native reuse can apply and prove it. | F7; PA | A26 |
| R37 | B | After two materially equivalent failed corrections without new evidence, diagnose. Record obligation, failing example, hypothesis, last evidence, discriminating check and correction identity. Resume only after changing a relevant variable based on evidence; preserve history across restarts. | F7; PK | A25,A67 |
| R38 | B,H | After settlement explicitly reuse, retain or release. Release unnecessary workers promptly. Retained settled workers default to configurable 30-minute idle expiry, applied only by native executor/active coordinator. Never infer release, kill active work on this timer or delete uncommitted work/evidence. | F7; PD,L | A22,A26,A68 |
| R39 | B | Before substantive work map every original criterion to a planned check or explicit human/provider dependency. Preserve criteria through decomposition; update evidence rather than redefine success to fit output. | F8; PA,J,M | A27,A28,A30,A45 |
| R40 | B,H | Run cheap discriminating checks early, focused checks during development and required complete gates at milestones. Honor project review rules; otherwise independently review substantial/high-risk changes. Review receives exact candidate/reproducible evidence without being primed to approve. | F8; PF,J | A28,A69 |
| R41 | B | Bind verification to commit/tree where applicable, relevant dirty/source identity, policy/configuration, dependencies, environment, commands/results and reviewer attempt. Material changes invalidate affected evidence. Reuse unaffected proof only when bindings/project rules permit. | F8; PM | A27,A29,A70 |
| R42 | H | Keep implemented, locally verified, independently reviewed, hosted proof complete, accepted, merged and deployed/released distinct. The coordinator assesses the objective under project acceptance authority; worker success/synthetic fixtures cannot promote later labels. | F8; PA,M | A27,A28,A71 |
| R43 | B | Final reports state objective, achieved criteria, exact blockers, candidate/check evidence, failures, material route exceptions, uncertainty and release state. Partial results remain partial; unobserved usage/cost remains unknown. | F8,14; PA,M | A39,A71 |
| R44 | I,H | Persist only compact decision context, provenance, checkpoints, effects/reconciliation records and evidence references. Use private OS-appropriate host-local storage, explicit versions, atomic writes and bounded retention. Keep live identifiers/private material out of Git; never expire unresolved effects/required evidence. | F9; PA,L | A31,A37,A41,A72 |
| R45 | B | Recovery selects the objective, reads native state before deciding actions and distinguishes active, completed-awaiting-integration, failed, uncertain, stale and stopped coordination. Missing local records do not justify relaunch. No daemon/automatic conversation migration. | F9; PL | A22,A31,A62,A72 |
| R46 | B,H | Steering creates a new revision, reconciles affected assignments at safe native boundaries, preserves useful unaffected work and prevents obsolete reports/proof satisfying revised work. Material acceptance changes require explicit authorization. | F8–9; PJ,M | A30,A45,A70 |
| R47 | B,H | Record relevant observed outcomes and suggest preferences only after a meaningful pattern. Never silently rewrite preferences, infer savings from model labels or launch paid A/B experiments automatically. | F10,14; PJ | A39,A73 |
| R48 | B | Read-only tools show effective policy/provenance, approval, runtime/capability/version issues, quota visibility and compact native work/verification context. Ambiguous status requires selection. Reads must not create/migrate state, repair integrations, run hooks/models, dispatch or spend. | F3,10; PH,J | A36,A42,A51,A74 |
| R49 | B,H | Local setup enrolls the project/reconciles owned skills. Global setup installs user skills without changing the current project. Reuse compatible global installations; diagnose duplicate/shadowed copies. Remove redundant local copies only during local setup when owned/unchanged; preserve modified copies. | F3,11; PH | A32,A33,A36,A43,A75 |
| R50 | I,H | Support explicit isolated installation from reviewed checkout on native Windows/Linux with Python 3.13+. Bootstrap installs only into the selected environment and reports the command. Normal setup performs no implicit machine installation, automatic PATH repair, Orca installation, billing change or project hook. | F3,11; PI | A34,A76 |
| R51 | B,H | Support native Windows/Linux. WSL/remote forwarding is verified transport with one runtime/state owner, not mandatory Windows controller architecture. Unknown ownership/unsupported transport fails conservatively; no silent local fallback. | F11; PI | A34,A58,A62 |
| R52 | B | Preserve Git history while renaming repository/product Pod/pod, module/executable pod and distribution j3w1-pod. Update package/import/CI/install/docs references. Breaking release exposes no legacy aliases, compatibility shims or dual controller mode. | F11; PG | A35,A77 |
| R53 | H | Bound migration by an ownership manifest. Reconcile/explicitly checkpoint legacy work and preserve necessary recovery evidence before destructive cleanup. Retain reviewed pre-cutover Git reference/required archive. Imports translate validated concepts only, never broaden authorization or permit competing coordinators. | F11; PG,L | A22,A24,A35,A77 |
| R54 | H | Follow registered host paths/worktree mechanisms/native profiles/admin boundaries. Canonical pod registration/relocation uses owner-managed mechanism. Coding must not change global policy, weaken protections/gates or perform unauthorized deployment/provider/publication actions. | F11–12; PI | A34,A77,A78 |
| R55 | H | Block release until required live core integrations have candidate-bound passing evidence: Claude Code and Codex on native Windows and Linux. Required synthetic, packaging, hosted, review and project gates also apply. Unsupported optional capabilities fail conservatively and cannot be marketed as verified. | F12,14; PI | A34,A79 |
| R56 | B,H | Evaluate direct-agent, native-Orca and Pod on matched candidates/criteria and representative tasks. Report acceptance, defects, interventions, delegation, retries, elapsed time, context/dispatch volume, observed usage, visibility, sample size/confidence. Offline CI is normal; live experiments need bounded authorization. No universal savings target. | F14; PC,J | A39,A73,A80 |
| R57 | I | Retain Python 3.13+, standard src packaging and standard-library facilities where practical. Reuse applicable safety invariants/tests; remove obsolete controller/installer machinery with documented replacement coverage. Build/install/test frozen candidate on both native OSes and follow repository implementation/review ownership. | F2,11–12; PA,I | A35,A76,A81 |
| R58 | B,I | Keep README approachable, implemented-only and linked to detailed contracts. Maintain/package one skill policy source and references. Document capability limits, breaking migration and actual evidence; historical proposals are not competing active instructions. | F2,11,14; PN–O | A40,A81 |
| R59 | H | Preserve project authority over source selection, checks, review, acceptance, merge and release. Repository instructions may constrain work but cannot expand personal provider/spending approval. Explicit user scope/host rules remain authoritative; reports/configuration cannot manufacture grants. | F1,8,11–12; PF–G,O | A09,A37,A71 |

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
personal Windows `%APPDATA%\pod\config.yaml`, optional project `.pod/config.yaml`.
YAML is editable authority; Markdown tables are generated. No live identifiers,
account credentials or personal machine paths belong in committed examples.

`pod/v1` sections:
- `models`: stable alias, agent, exact model identity, approval, billing restriction;
  optional account/effort/capability/data-location/approval-provenance restrictions.
- `routing`: five complexity rows, model alias and effort; derive agent.
- `policy`: concurrency, quota, billing, delegation, review and lifecycle/retention.
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
unless project rules are stricter. Retained-settled idle expiry defaults to 30 minutes.
No-progress diagnostic threshold is two equivalent failed corrections without new evidence.

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
Reserve capacity and record intent under the applicable admission lock before a native
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
| A16 | Shared buckets include known coordinator/worker consumption and admissions without reallocating the same remainder. |
| A17 | Paid fallback/reset availability causes no consumption without the exact applicable grant. |
| A18 | Five ready Tasks at capacity two admit two; next useful ready Task starts when capacity safely frees. |
| A19 | Three requires justification; four–eight requires bound grant; above eight rejected. |
| A20 | Hidden/native descendants disabled or expressly authorized/counted; unverifiable compliance blocks route. |
| A21 | Concurrent managed admissions/coordinators do not over-admit; ambiguous cross-host authority blocks dispatch. |
| A22 | Lost launch/release responses retain occupancy and reconcile exact identities without blind replacements/repeated effects. |
| A23 | Duplicate/multi-item Deliveries process all required effects idempotently before acknowledgment. |
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
| A34 | Windows/Linux and supported transports establish one correct runtime/state owner without phantom guarantees. |
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
| A61 | Faults around reservation/effect/receipt/Delivery preserve uncertainty and prevent double-counting/duplicate effects. |
| A62 | Missing caller/conflicting ownership/partial handover blocks delegation; valid adoption preserves work without proxy coordinator. |
| A63 | Unsettled contracts start one writer; project isolation/hooks honored without weakening host protection. |
| A64 | Packets/reports enforce bounded fields, criterion/candidate/scope/runtime-issued identity; reject transcript cloning and report permissions. |
| A65 | Relevant files/instructions/requirements/candidate changes invalidate context; unchanged bound context can be reused. |
| A66 | Changed/absent/redirected sources differ from unavailable reads; definitive rejection survives restoration; unrelated dirty paths do not grant sensitive reads. |
| A67 | Restarted correction history recognizes equivalence; rewording is insufficient but discriminating evidence permits changed attempts. |
| A68 | Retained settled workers occupy capacity; expiry requires active/native executor, never kills active work or deletes changes. |
| A69 | Cheap discriminating checks precede expensive work; independent review receives exact candidate and reproducible unprimed evidence. |
| A70 | Source/config/dependency/environment/candidate changes invalidate affected proof; no fabricated Git metadata and explicit alternative source binding. |
| A71 | Completion labels/final report follow evidence/project authority; failures/blockers/route exceptions/unreleased state visible. |
| A72 | Interrupted versioned checkpoints recover privately and atomically; retention/native-first recovery protect unresolved effects/evidence. |
| A73 | Feedback may suggest but not auto-edit preferences or launch unauthorized benchmarks. |
| A74 | Ambiguous status requires selection; compact counts, route reasons, quota confidence, verification gaps and next safe action. |
| A75 | Preserve modified copies; global setup never prunes local; local cleanup removes only redundant owned unchanged copies. |
| A76 | Reviewed isolated install works on native Windows/Linux, rejects old Python, handles command paths, no automatic PATH/Orca repair. |
| A77 | Migration inventories ownership, preserves history/pre-cutover/uncertain evidence, removes legacy public behavior and prevents competing control. |
| A78 | Unregistered canonical relocation/prohibited placement refused; owner-managed registration is a separate cutover gate. |
| A79 | Missing live PASS for any Claude/Codex × Windows/Linux core combination blocks release despite synthetic CI/read-only doctor. |
| A80 | Three matched baselines cover representative tasks/failures with actual metrics/unknowns/confidence and bounded live authorization. |
| A81 | Frozen build/isolated install/Windows-Linux CI/incident discovery/compile-diff/independent audit/implemented-only docs bind release candidate; retired gates have replacements. |
| A82 | Explicit config edit defaults personal; project scope explicit; preserve invalid edits and block dispatch, never silently restore/sanitize. |

## Implementation sequence and verification

P00: freeze baseline/runtime identities, keep/adapt/remove and owned-state inventories;
verify capabilities or specify conservative failures. P01: preserve this specification,
supersession/traceability and structured contracts; add executable failing scenarios
with meaningful failure causes. P02: configuration/approval/routing/preview/replay.
P03: native adapters, metadata/quota/billing, effective checks, admission/uncertainty.
P04: inline skill/direct/planning/adaptive packets, supervision, diagnosis/reuse/cleanup.
P05: acceptance maps/context/evidence/checkpoints/steering/adoption/feedback/status.
P06: isolated installation, scope reconciliation, package/CLI rename and owned cleanup.
P07: frozen-candidate offline/fault/packaging/hosted/independent/live gates, matched
evaluation and owner-managed cutover. Phases are not fixed worker waves.

Python 3.13+, standard src packaging, minimal safe YAML dependency and standard-library
facilities where practical. Keep one canonical skill, ideally about 250 lines or less,
with relevant-on-demand references and generated host metadata. No empty abstractions.
Preserve source access, candidate binding, uncertain effects, requested/effective checks
and Delivery replay; replace controller/managed-installer assumptions.

One writer owns overlapping source. Repository-specific ownership: Sol/high implementation,
fresh Sol/xhigh final audit. Reviews bind a frozen candidate, report findings, and do not
repair. This is project governance, not a universal Pod model default.

Required checks: focused tests in development; complete unit/incident coverage and explicit
incident discovery; compileall and diff checks; frozen git-archive build; fresh isolated
wheel install/CLI smokes on Windows/Linux; hosted candidate CI; independent final audit;
required live runtime cases; project acceptance. Retired bootstrap/controller gates must
map explicitly to superseded requirements and replacement tests; do not weaken checks.

Live core matrix: Codex/Linux, Claude Code/Linux, Codex/Windows, Claude Code/Windows.
Each proves installation/discovery, in-session coordination, authorized native execution
and effective route, supervision/lifecycle, verification and interruption/adoption.
Missing live PASS for any required combination blocks release despite offline PASS.
Optional unsupported transport/provider capabilities fail conservatively and are not
advertised operational. WSL/remote ownership is separate from native OS support.

Compare direct-agent, native-Orca and Pod from matching candidates/criteria for trivial
edit, routine feature, ambiguous diagnosis, independent parallel work, overlapping edits,
high-risk change, low quota, blocked infrastructure and interrupted coordination.
Record acceptance/defects/interventions/unnecessary delegation/retries without evidence,
time/context/dispatch/observed usage, visibility/confidence/sample size. Unknown stays
unknown; wall time is not provider compute; no universal savings percentage.

Live exercises are disposable and bounded/authorized. Existing CE/ordinary-project trials
are read-only. No purchase/reset, production mutation, deployment or PyPI publication is
authorized merely by the rewrite task. Actual release/rename must honor host registration
and repository authority. No compatibility period or public dual interface.

## Evidence boundary at reconciliation

Baseline is clean main at the commit above. Read-only observations found reachable Orca
1.4.205 advertising orchestration.contract.v1 and worker-launch-preferences, Codex CLI
0.155.1 and Claude Code 2.1.278. Native help describes optional worker terminals.
These facts are not Pod lifecycle, quota, billing, permission/fan-out or recovery proof.
Host workspace doctor passed, but canonical pod registration is an owner-admin cutover
dependency. Implementation/tests, live core matrix, hosted CI, independent audit,
acceptance, merge and release were NOT_RUN for Pod at reconciliation.
