# Interface

## Requirements

### R03 — Expose pod, pod models, pod status, pod doctor

Type: B · Scenarios: [A34](#a34), [A40](#a40), [A119](#a119), [A216](#a216), [A252](#a252), [A256](#a256)

Expose `pod`, `pod models` with `models refresh [--check]` and `models status`, `pod status` with `--list [--repository PATH | --worktree PATH]`, `pod doctor`, `pod update`, `pod config`/`config edit`, and `pod --version`. Model, config, doctor and status commands offer clean `--json` machine output; the model read optionally restricts its rows with `--eligible-only`. `pod update` updates software only; model data refreshes only through `pod models refresh` or the workspace. `pod internal` is hidden and structured; planning, execution and continuation stay skill behaviors.

### R04 — Maintain one canonical trigger-led skill router

Type: B · Scenarios: [A01](../pod-spec.md#a01), [A32](#a32), [A41](#a41)

Maintain one canonical inline skill router with relevant-on-demand references and one interface description. Frontmatter retains its restrictions and repository source, with a ≤250-character description containing `Use when` and a plain compatibility line. Invocation preserves the conversation, model, effort and settings; no coordinator override or Claude context fork. Product version is authored only in root VERSION.

### R48 — Read-only status and doctor report preferences

Type: B · Scenarios: [A34](#a34), [A40](#a40), [A49](models/preferences.md#a49), [A71](#a71), [A133](#a133), [A175](#a175), [A230](#a230), [A406](#a406), [A426](#a426), [A427](#a427), [A428](#a428), [A440](#a440)

Read-only status and doctor report preferences including their status and any route-setup action, the Preferred and Pinned routes, the actual config path, a route-state summary, current native work, constraints, decisions, mismatches, installed bundle identity drift, blockers and next actions; doctor also reports the offline observation cache state. These reads never fetch. Run-selected status includes Governor authorization rows in `owner_decisions`, using the same inventory as objective-selected status. `pod status --objective ID` selects one objective, optionally constrained by `--run`; ambiguous shared Runs return `selection_required` with choices rather than selecting one. Status adds current exact release, terminal, ownership, retained reason and Dispatch stage facts, labels placement warnings start-time and renders every blocked result without a traceback. An unknown objective names its id, repository context and state root searched. R133 adds the last observed PoD ticks, authorship, criteria and due/blocked states from the ledger; R135 adds the recorded-question projection; R139 adds the standing-authorization projection. Status also reports local route failure observations/deadlines and derives active/settled assignments, observed retained terminals, gates, progress and next action without process telemetry or side effects.

### R49 — The one-shot installer places both agent

Type: B,H · Scenarios: [A32](#a32), [A33](#a33), [A72](#a72), [A134](#a134)

The one-shot installer places both agent skills, dependencies and a user-local launcher from one validated bundle. Reinstall and update preserve edited preferences and foreign files; diagnose duplicate/shadowing copies and interrupted installs.

### R50 — Follow registered host paths/worktree mechanisms/native profiles/admin

Type: H · Scenarios: [A73](#a73)

Follow registered host paths/worktree mechanisms/native profiles/admin boundaries. Canonical pod registration/relocation uses owner-managed mechanism. Coding must not change global policy, weaken protections/gates or perform unauthorized deployment/provider/publication actions.

### R52 — Use Python 3.13+ on Linux, one

Type: I · Scenarios: [A75](#a75), [A135](#a135)

Use Python 3.13+ on Linux, one bundle-as-package and standard-library facilities where practical. Run unit, incident, PTY/subprocess, installed-bundle, source-audit and hosted checks as specified, with independent review and separate live evidence.

### R53 — Keep contributor documents contextual and README approachable

Type: B,I · Scenarios: [A38](../pod-spec.md#a38), [A75](#a75)

Keep README approachable, implemented-only and linked to AGENTS for contextual contributor reading and completion criteria. AGENTS preserves host authority, worker/model judgment, mandatory fresh independent audit, native orchestration and metadata ownership. The verification contract defines change-class checks and milestones; installed runtime guidance contains no run-tests/checks reminders. Document capability limits and actual evidence.

### R55 — Distribute from main through the one-shot

Type: B,I · Scenarios: [A77](#a77), [A118](#a118), [A136](#a136)

Distribute from `main` through the one-shot installer, which uses the skills CLI for the single `skills/pod` bundle and places a user-local `pod` launcher. Root `VERSION` is the sole authored version, linked by the bundle. There are no packages, releases or tags.

### R56 — The installer stages and validates the

Type: B,H · Scenarios: [A77](#a77), [A78](#a78), [A137](#a137)

The installer stages and validates the bundle, isolated dependency environment, launcher, receipt, preferences and minimal owned PATH block. It refuses foreign launchers, preserves user edits, and recovers interrupted installs. A copied installed bundle runs outside the checkout.

### R60 — Keep host integration optional. Pod works

Type: B,H · Scenarios: [A78](#a78)

Keep host integration optional. Pod works on a suitable Linux environment without the owner's host tooling, paths, accounts or evidence, detecting and respecting host policies when present.

### R62 — Separate issue intake from canonical document authoring

Type: B · Scenarios: [A97](#a97), [A98](#a98), [A204](#a204), [A255](#a255), [A381](#a381)

Keep `issue-intake.md` as the conditional intake/interpretation source, `seal-delivery.md` as the SEAL delivery planning reference, and one installed authoring reference each for SEAL, PES and EEL. SEAL is Scope, Expectations, Acceptance & Limits: requirements for one or more systems, with zero or more PES children and an optional advisory roadmap, never a parent execution coordinator. PES is a bounded executable contract, standalone or implementing selected parent requirements; detailed implementation procedures are optional and non-fixed choices belong to the implementer. EEL evaluates observations, evidence, uncertainty and disposition, including no change; its findings require a separate PES for implementation. A bare SEAL link or a request to plan or decompose its delivery follows R122; another explicitly scoped analysis request and proportionate direct objectives remain usable without authorizing implementation of SEAL/EEL content.

The canonical references are `seal-template.md`, `pes-template.md` and `eel-template.md`. Each supplies a role definition, external GitHub title example, first-line format marker and fenced body with useful section explanations, removable placeholders/comments and optional sections. Use stable document-local R/AC/PoD/F references qualified across issues. Requirements, proof, validation, overall acceptance and delivery have separate responsibilities; unperformed or withdrawn checks never pass. Ordinary Markdown and equivalent clear headings are valid; no document parser beyond R130's bounded PoD checkbox recognition, mandatory pipeline, universal gates/approvals or parallel templates. Authoring/revision reads its relevant template through a direct router trigger; ordinary execution reads applicable intake/runtime guidance.

### R63 — Accept a direct objective or a

Type: B,H · Scenarios: [A99](#a99), [A100](#a100), [A101](#a101), [A202](#a202), [A253](#a253)

Accept a direct objective or a canonical GitHub issue URL through one workflow. Retrieve the complete issue with authorized host access, validate returned identity and target against the actual checkout, treat issue content as scope rather than authority, block inaccessible/incomplete/mismatched sources, and reconcile a closed issue with user intent before repeated work. Executable intake recognizes exact title prefixes `EEL:` and `SEAL:` or the exact first nonempty unindented body declaration `**Format:** Evidence Evaluation Ledger v1` or `**Format:** SEAL v1`, optionally ending `<br>`, with LF or CRLF. It refuses EEL (`issue_is_evidence_ledger`) and SEAL (`issue_is_requirements_spec`) before binding. The EEL diagnostic and separate-PES boundary remain unchanged; the shared intake/recheck SEAL refusal's next action is `use read-only SEAL delivery planning, a bounded Pod Execution Spec citing selected SEAL requirements, or an explicitly scoped analysis request`. Either restrictive identity blocks despite conflicting title/body declarations; EEL keeps its diagnostic when both roles occur. Quoted, indented, code-block or later body markers are not identity. Reading linked requirements/evidence remains allowed.

From another repository, or a directory with no GitHub repository identity (not a Git checkout or no GitHub `origin`), intake makes one authorized issue read. Only a validated, complete response for that exact issue which same-repository intake would refuse as SEAL or EEL receives its role refusal. Every other case keeps `repository_mismatch`, including a failed read, incomplete response, wrong identity or ordinary PES. Recheck still refuses a repository mismatch without reading GitHub; same-repository intake and other recheck behavior remain unchanged.

### R64 — Bind issue identity, canonical locator, body

Type: B,H · Scenarios: [A102](#a102), [A103](#a103), [A203](#a203), [A253](#a253), [A395](#a395), [A396](#a396)

Bind issue identity, canonical locator, body digest and relevant amendment references into checkpoints, packets and final verification without copying the issue body into state. Recheck at intake, continuation, affected admission and final verification. Body change requires reconciliation; metadata alone does not, except that a title gaining an EEL/SEAL marker stops execution like the body marker, and every caller surfaces the role refusal itself. Forward Governor decisions/execution recheck the checkpoint source before native continuity or remote effects; role changes, body/amendment drift and inaccessible reads hold forward work. Cancellation, outcome recording and provider readback retain their supported paths. Closure alone does not block already authorized Governor work: reconcile closed-issue intent at intake, and rebind reconciled body/amendment changes through the existing checkpoint. Amendments are not checked for markers. Preserve unaffected proof and never replace an admitted uncertain native request with a revised packet. R131 adds the sole automatic body rebind: a tick's matching intended readback and receipt atomically update the checkpoint digest without a map transition or evidence invalidation. Other body edits still reconcile through a checkpoint, whose line-fact refresh is best effort. Ticks never use the Governor's closed-source exemption: CLOSED stays blocked, even after reconciliation/rebind, until an outside reopen. Every issue_reconciliation_required includes the recheck reason. Reserved or unresolved admissions keep their original packet and defer a tick with pod_tick_admission_pending until settlement; an in-flight tick intent defers new admission with pod_tick_pending.

### R69 — Keep the public product independent of

Type: B,H · Scenarios: [A111](#a111), [A112](#a112)

Keep the public product independent of owner services and machine-local state. Maintain a small generic tracked-source/artifact hygiene audit that reports safe path/category/location without echoing matched credentials and permits product identity, public links and sanitized fixtures. The upstream metadata service remains external governance with only a short contributor notice and no Pod integration or competing writer.

### R70 — README is the practical install and

Type: B · Scenarios: [A113](#a113)

README is the practical install and session guide: one-shot command, open/auth/invoke, direct and issue objectives, Plan Mode, worktrees, visible workers, verification, limits, update/removal and linked installation details. Describe implemented behavior only.

### R71 — Maintain the documentation contract

Type: B,H · Scenarios: [A114](#a114), [A115](#a115), [A180](#a180), [A233](#a233), [A255](#a255)

This is the authoritative documentation contract:

- Write clear, complete instructions. Do not delete useful explanation or compress rules into ambiguous shorthand to make files smaller.
- Each reference serves one coherent task or moment.
- Each rule has one authoritative detailed statement in its owning file. Other text may name the rule without restating it.
- `SKILL.md` holds the essential boundaries and navigation, with one explicit read-when-needed trigger per reference.
- Optimize the total work needed to reach a verified result, not the number of words stored in the repository.

The router instructs the coordinator to read a reference when the current action, or a necessary prerequisite of it, needs it; never preload the reference directory or read a reference merely because it is linked; read an authoring template only to author or revise that kind of document; and reload guidance when it is lost, changed or insufficient. Trivial work reads none. These are coordinator instructions, not a loading mechanism; static checks establish wording and routing, while observed runs establish actual reference reads.

`SKILL.md` has exactly Roles, Done when, Boundaries, Helpers and Read when needed. Each bundled reference has one unique triggered router link and no reference-to-reference links. Validation retains frontmatter/metadata restrictions, VERSION, inventory and symlink rules, required and forbidden helper forms, launcher, interface, catalog and observations checks, and the 64 KiB per-file bundle byte bound. Metadata and VERSION restrictions are reported before body checks. Word and line counts cause neither refusals nor warnings; printed per-file stored word counts and their single total are informational and do not measure the context an agent loads. Runtime-required delivery, cleanup, coordination, model and bookkeeping guidance lives in the installed bundle's applicable task references, preserving authority, completion, judgment and recovery; templates serve authoring and revision, while repository docs provide contributor checks and detailed contracts.

### R73 — The model workspace is one table of exact routes

Type: B,H · Scenarios: [A117](#a117), [A139](#a139), [A140](#a140), [A178](models/catalog.md#a178), [A183](#a183), [A217](#a217)

`pod` and `pod models` open the model workspace on a TTY; otherwise they print the cached route table, with each route's AA profile qualifiers beside its metrics, every source's own retrieval time and the full benchmark disclaimer, and exit nonzero unless preferences are valid. The workspace is one table of exact model+effort routes from the joined projection, with stable identity, a sticky heading, scrolling and a focus distinct from state, Preferred, Pin, compare and frontier marks, each with an ASCII equivalent. Focus always rests on a shown row; with no shown row, row edits are refused. Narrow labels shorten the model name, never the effort, so every row stays a unique model+effort identity. Every AA value carries its profile: † (ASCII `#`) marks a value AA measured "with fallback", its harness and never Pod fallback, and `~` marks an estimated index; Help lists every mark. It sorts by AA index descending by default, with unknown values last in either direction and registry and effort order breaking ties, and also sorts by benchmark cost per task, output tokens per second, first and total response, model, effort and state in either direction. A collapsible model-grouped view lists each model's routes by effort for managing them. New and unsupported observations appear beside supported routes by default with new/unsup discovery labels and remain non-routable. Narrow observation labels retain the source profile effort; none denotes an informational non-reasoning profile. Source profile efforts also drive observation effort sorting and filtering, without changing native model or effort identity. The discovery filter can restrict the table to supported routes; clearing filters restores the inclusive view. Fixed wide (from 140 columns), normal (from 80) and narrow presets move secondary fields to the inspector, keep the sort metric visible, and work down to 40×12; Help documents units and that ranks are per source, never global.

### R74 — The workspace works on ordinary Linux terminals

Type: B,H · Scenarios: [A139](#a139), [A140](#a140), [A178](models/catalog.md#a178), [A219](#a219)

The workspace works on ordinary Linux terminals and over SSH and is complete from the keyboard. A searchable command palette (`:` or Ctrl+P) exposes refresh, state, Preferred and Pin, bulk and reset, the refresh setting, route setup, sorting, grouping, filters, compare, frontier, inspector tabs and help, while common shortcuts remain. Provider, model, effort, state and discovery filters compose with search over display and native identity; focus and hidden Preferred/Pin summaries survive filtering, sorting, refresh and reload. It supports resize, monochrome and `NO_COLOR`, ASCII under C/POSIX locales or limited `TERM` values, a too-small notice below 40×12, and Ctrl+C, SIGTERM and SIGHUP. Mouse input is not required. Reading, navigation, sorting, filtering and comparison never write, start workers or call models.

### R75 — A full-width bottom inspector explains the focused route

Type: B,H · Scenarios: [A122](models/catalog.md#a122), [A139](#a139), [A178](models/catalog.md#a178), [A218](#a218)

A full-width bottom inspector dock, never a right-side panel, has Details, Benchmarks, Routing and Sources tabs, plus Compare while routes are marked, with focus and scrolling independent of the table; short terminals keep a compact dock that pages to every line. Details shows the exact route, native identity, state and selection, documented context and that native access is unknown; the workspace starts no subprocess or Orca probe, and `pod doctor` reports Orca launch capability. Benchmarks shows the AA profile with its fallback and estimated-index caveats, methodology, retrieval time, every metric with units, the scoped frontier status and changes since the previous snapshot. Routing renders authored guidance, not a prediction for an unspecified task. Sources shows each source's URL, attribution, latest attempt, data age, methodology and diagnostics, the route's alias mapping and native evidence limits. The status line computes data age from the clock, so an open window ages and turns stale, and adds "mixed ages (oldest …)" when a kept source is older than the required one; the age and STALE come first at every width from 40 columns, the mixed note shortens or drops before them, and a notice is cut rather than hiding the age; title items that do not fit move to their own selections line from 20 rows, as width allows; below 20 rows they share the status line, where only an exact Pin or Preferred key may precede the age, and a "none" placeholder or the enabled count (or its READ-ONLY / SETUP REQUIRED substitute) follows the age, the refresh outcome, the native status and the save time, showing when there is room. In the table view without a notice or error such a key keeps its place as width allows, so at the narrowest widths the age may then appear only in Sources while STALE stays on the table's controls line, and at 40–48 columns a second key may not fit on either line and then appears only in Details; with a notice or error the age is kept and the key may drop; in the help, palette, bulk and setup overlays the age and STALE come first. The full disclaimer opens the Benchmarks and Compare tabs and Help at every size, and a width-fitted short form sits under any table that shows a benchmark cost or response-time column, at every size: “AA benchmark cost is not the user's subscription charge, quota consumption or Pod invoice; first/total benchmark response is not worker task duration.”

### R76 — Preference edits save immediately through the safe writer

Type: B,H · Scenarios: [A128](models/preferences.md#a128), [A140](#a140), [A183](#a183), [A219](#a219), [A221](#a221)

Explicit edits save immediately through the one safe preference writer with honest feedback. Space enables a disabled or not-set route and disables an enabled one; returning a route to not set is a palette action. `p` and `P` move or clear the Pin and the Preferred route. Bulk actions (all efforts of a model, shown routes in an effort range, shown routes that are not set, or reset to shipped defaults) preview the exact routes changed and save once; reset enables exactly the listed shipped routes and keeps Pin, Preferred, worker limit and refresh setting, and no bulk action covers routes added later. A bulk save writes exactly the preview the user reviewed against the values it was built from; if the preferences changed, nothing is saved and a fresh preview says "changed elsewhere; review again". A bulk change that would disable the Pin or Preferred route needs an explicit clear of that exact shown key in the same save. Refusals, conflicts and failures report “Not saved” and leave the file unchanged. Open workspaces notice external edits and refreshed data promptly without blocking keys, including an external edit that lands with the window's own save or refresh or is merged while the save waits for the preference lock. A palette command that writes preferences is refused with "changed elsewhere; review again" when the preferences changed while the palette was open, and a command on the focused row is refused when the focus moved; nothing is written. Invalid preferences make the workspace read-only; a kept `pod/v1` file opens the route setup screen, which saves nothing until the user confirms. An automatic or manual data refresh writes the observation cache only, never preferences.

### R104 — Comparisons and changed data stay non-authorizing

Type: B · Scenarios: [A220](#a220)

Compare holds up to four exact routes using existing data only, with no calls or benchmarks. Index differences are absolute points; other metrics show percentage deltas only between values from the same source and methodology with a nonzero baseline, and there is no “twice as intelligent” claim. Routes whose AA source, methodology or profile qualifiers differ are not like-for-like: Compare shows a caveat and "not comparable" deltas, and changed data names a profile or methodology change without a difference. An optional frontier is computed within each like-for-like set of shown routes and marks those that no other route in the set dominates, being at least as good on index (higher), benchmark cost per task and first response (lower) and not identical; missing or non-comparable dimensions are unknown, and the label states its scope and dimensions and that it is not a recommendation or eligibility rule. Changed data compares supported routes with the one previous snapshot in the Benchmarks tab; across a methodology or AA profile change no metric, context included, shows a difference, and the change is named as not comparable, never as model improvement. There is no long-term history.

### R79 — The one-shot installer and explicit update

Type: B,I · Scenarios: [A134](#a134), [A136](#a136), [A137](#a137), [A142](#a142)

The one-shot installer and explicit update use a staged checked bundle, isolated dependency environment and owned user-local launcher, preserve user-edited files and report PATH or duplicate-copy limitations. Installation is separate from runtime connection and authentication.

### R97 — Status separates unverified settlement from active work

Type: B · Scenarios: [A192](#a192), [A193](#a193), [A194](#a194), [A265](#a265)

When status's exact native settlement read fails, every open admission is listed under `assignments.unverified`, not active, and the human summary counts it separately. Status names the failed read's code and the objective's bound and current runtime, or unknown, labels Orca's attention for those rows as Orca's and claims no wait for their delivery; the next action names the unverified settlement. The shared envelope is now `pod-cli/v5`: the genuine 0.6.6 comparison asserts its old v4 marker and the new v5 marker separately, with exact equality of every other comparable projection field apart from R114's new fields, and of the rendering after R114's readiness line. This is presentation only: the kernel's outstanding set, settlement rules, logical slots and fail-closed mutation are unchanged, and existing verified fields retain their meanings. Current resource/stage facts are additive under R48; older missing facts stay empty or absent and preserve rendering. A closed objective's open admissions are an exception: its closure was accepted only with no outstanding assignment, judged from an exact read, so when today's read fails status lists them under `assignments.settled` with `settlement: closure` and the closure sequence, and none as unverified. This reads no Orca and changes no kernel rule.

### R99 — Input refusals name their fields

Type: B · Scenarios: [A200](#a200), [A201](#a201), [A227](#a227), [A376](#a376), [A377](#a377), [A378](#a378), [A379](#a379), [A397](#a397)

An `exact()` shape refusal lists its record's complete accepted and optional field names, including for a non-object. Missing/unsupported caller names remain bounded in count and length and control-stripped. A non-object also names its expected type; a `pod internal` refusal names its operation. No refused value is echoed, except the policy digests below. There are no prose examples or generated schemas.

`skills/pod/contracts.py` owns the input-field table for packet, report, checkpoint, map and its obligations, waits, externals, withdrawals, evidence and acceptances, admission and Governor action, plus their bounded subrecords. These include action.authorization (its decision fields, authorized_by/reference/target up to 512 and scope of 1–8 vocabulary items), the report request's triage finding (summary up to 512, downgrade reason up to 1024, root_cause up to 512, groups of 1–8 finding/severity members), and the keep_worker_tab constraint value (objective, dispatch and terminal up to 256). Parent field descriptions name the authorization, finding and group-member records. The validators read that table's names, text/count bounds, identifier forms and vocabulary. Existing opaque fields stay opaque and acquire no new validation. Before any write, `pod internal contract` reads a named record or `all`, selected by its required `record` field, returning bounded constant field data with accepted/optional names. Shape and text-limit refusals include every bounded field of the affected record and its complete accepted/optional names; those constant lists bypass the kernel's eight-item caller-referent cap.

The same inputs retain their acceptance/refusal codes and ordered validation. Every report, including one whose accompanying map closes, detects and records route_mismatch through the existing admission update, then raises that code before body validation and the map, open-objective and tab checks. This route refusal preserves the acceptance hold and is not a tab refusal. A nonclosing effective_unknown refresh follows its existing update_admission path before body validation too. Only that refresh is deferred into a closing report's consume transaction, so a tab refusal writes nothing. Kernel refusals keep their detail and known obligation context. `packet_plan_mismatch` names every differing candidate, criteria, plan_revision, objective_source, worktree or packet_id, never its value; its reads name the matching fields in `internal map`, or the fresh `internal packet` freeze for packet_id. Map reads expose the checkpoint criteria, plan_revision, objective_source, worktree and policy_revision as read-only additions. After a tick rebind, compare a new packet's objective_source to the checkpoint before the packet's own issue recheck, so an earlier frozen source refuses packet_plan_mismatch naming objective_source. Other existing refusal ordering stays unchanged; an existing admission is always recovered with its admitted packet.

`wait_invalid` names every affected obligation, and `obligation_unaccounted` with active_admission_settled names every settled executor's obligation to restate, complete within the map's 96-row bound. Both name the map read and next sequence. The report next action puts the restating map with its update in that same report request. Admission still rejects update and accepts only a full obligations restatement in the same admission map. A checkpoint restates the rows in its map. Pre-row admission failures retain same-Task retry and unused Task settlement guidance.

Each `policy_revision_mismatch` site names the expected effective-policy digest and the received checkpoint or packet digest, together with every checkpoint, packet and current-effective digest it knows. Only actual policy digests may be echoed. The next action explains that effective policy depends on the helper's project: `pod config --json` runs inside the objective's project, and every `pod internal` request names that project; read map, refresh the checkpoint there and freeze the packet again. Pending replay instead preserves the same request while correcting its project; a genuine policy/core change retains its existing recovery hold. Receipt conflicts and history bounds keep their detail-specific recovery and authority.

### R106 — Compact successful helper writes at the output boundary

Type: B,I · Scenarios: [A251](#a251), [A407](#a407)

`pod-cli/v5` versions public envelopes and private JSON output. Successful checkpoint, report, constraint add/revoke, admission and route-failure acknowledgements omit objective/full-row echoes. Checkpoints select explicit next-consumer bindings and notices, with compact continuity/closure identities rather than stored histories. Governor prepare, outcome and classify and continuity/handoff writes compact only stored-row echoes; preflight/correct already return bounded projections. Preserve status, objective, written identities, map seq/revision and next_seq where a map is involved, next safe action, warnings, invalidations, report ingestion/disposition facts and exact request/native/route/recovery references. Admission retains its verification binding (including unknown/null context), effective-route uncertainty/mismatch and inherited evidence, placement bindings and any warnings. Governor prepare retains candidate identity, generation, commit/tree, dirty state, base/branch/workflow bindings, verification/toolchain/environment/policy and superseded validation ids; outcome retains provider facts including merge_commit. Packet, source, map and exactly governor/governor-execute/governor-reconcile result payloads remain equal; status and other bounded read-only projections keep their behavior. In-process handlers retain kernel values before JSON compaction; report identifiers are projected from the authoritative accepted map, including grouped findings and generated proposal ids. Projection changes no persistence, authorization, acceptance/refusal, record bounds, effect order or recovery. Optional title/rename and worker-tab additions (R123–R128) come from the handlers’ existing reads, never a fresh acknowledgement read; checkpoint/report acknowledgements preserve worker_tabs and the reporting worker’s worker_tab. They also preserve the ledger-produced pod_ticks and introducing/rebinding pod_tick_intake results (R133), never recomputing them with a fresh acknowledgement read. Local metadata readback adds no native read/effect; an unavailable readback leaves the write successful with an explicit warning to read map before the next map write. Routine follow-up needs no full-record read; map remains the resume/map_stale path.

### R107 — Trace selected parent requirements through existing sources

Type: B · Scenarios: [A254](#a254)

A child PES identifies its parent and selected requirements/Limits without copying it or retargeting execution. Retain locator, observed body/amendment digests and selected requirement versions in existing references, checkpoint verification dependencies and evidence. Read parents through authorized issue access (for example, `gh issue view`), rather than primary executable intake; this adds no helper parent-fetch interface. Read required parents at intake, continuation and affected verification. A relevant source change requires scoped reconciliation before affected work; inaccessible required sources name an external dependency. Bind proof to its relevant dependencies so unrelated coverage/status changes preserve applicable evidence. No parent registry, recursive crawler, automatic issue publisher or parent runtime is added; source observations confer no execution authority.

### R108 — List the objectives that touch a repository or worktree

Type: B · Scenarios: [A256](#a256), [A257](#a257), [A284](#a284)

`pod status --list [--repository PATH | --worktree PATH] --json` is a read-only public list for a non-owner such as an administrator outside Orca. It reads only Pod's state directory and Git identity: no Orca, preference or provider read, and no write, lock, conversion or repair. A record belongs to the repository when its state directory is the key of that repository and the objective it names; paths never decide membership, so a repository whose Git common directory moved no longer lists its earlier records. Each row names the objective and record, its phase (`open`, `closed`, `continued` under R110, or `retired` with its `retirement` block under R112 and R113, meaningful only with schema `current`), its schema (`current`, `superseded` for an earlier version of a current schema family such as the 0.6.0 cutover, or `unsupported`), the recorded worktree locations of its coordinator and assignments, whether every location is recorded, its Run identities, its succession links and its record-only `readiness` label (R114). `--worktree` lists exact location matches and, marked `repository`, members whose recorded work lacks a location. The answer is `listed`, `absent` when no state root exists, or `unreadable` when the root cannot be enumerated. Unreadable and unattributed records are listed apart and make the list incomplete; more rows or directories than its bounds set `truncated` with the total.

### R114 — Status says in plain words whether an objective is finished, as new fields only

Type: B · Scenarios: [A275](#a275), [A276](#a276), [A277](#a277), [A278](#a278), [A279](#a279), [A280](#a280), [A281](#a281), [A282](#a282), [A283](#a283), [A284](#a284), [A408](#a408)

`pod status --objective ID`, and a status whose Run selects exactly one objective, adds the versioned public `readiness` block (`pod-readiness/v1`, detailed under Public interfaces) for every reader, owner or not; human output prints its `summary` first. It is a projection, fresh at each read, of Pod's own records (objective state and map, Governor journal, admissions) and the native reads status makes: it stores nothing, takes no lock, repairs nothing, calls no provider and changes no value Pod stores or later re-derives, `quiescence` and the closure report with its `hosted_checks` included. R126 adds worker_tabs and R127 layers worker-tab wording without changing readiness state or coordinator_needed. R133 adds pod_ticks from recorded checkbox observations and current proof, with no provider fetch and no change to readiness state or coordinator_needed. Every other field status printed before keeps its exact value, `blocker`, `next_safe_action`, `next_action` and `owner_handoff` included.

Its `state` distinguishes `working`, `waiting` (on the named party or decision of a quiescent map), `finalizing` (delivered but finalizing), `closed`, `closed_external`, `coordinator_lost`, `retired`, `continued` and `unavailable`. What remains to record comes from one implementation, R117's finalization projection (`pod-finalization/v1`), embedded as `finalization` (R127 layers its next-action wording when tab facts are known): in recording order `merge_outcome`, `delivery_record`, `validation_readback`, `report_consumption` and `closure`, each with its record ids, kinds, states and derivations, whether finalization is finished and its next action; readiness only decides the state and names the records in plain words by kind. Records that cannot be read give no projection and read `unavailable`. Delivery is under way once a merge is decided or recorded, or every obligation is satisfied or withdrawn; until closure the objective is then `finalizing`, naming what remains, so a merged candidate never reads closed or retirable because a prompt is idle or a pull request merged. Only a recorded closure, which ends finalization, reads `closed`, or `closed_external` while it lists remaining gates; both set `coordinator_needed: false`, meaning its coordinator may close, and still read so after the target advances or the bound worktree is removed. Under R117 a closing write refuses while any record before closure remains, so such a closure leaves nothing unrecorded; a closure recorded before that rule reads `closed` as recorded, even where it left a merge's delivery record or a review worker's report unrecorded. An objective without an obligation map has no closure and never reads closed.

The block names the recorded coordinator's handle, Run, Runs, consumer generation and runtime. While Pod still needs the coordinator, a reader that is not that coordinator reads its terminal through R105's terminal show; the coordinator itself reads its own terminal as live. Orca reporting it stale, gone or exited reads `coordinator_lost` with `recovery`: `same_run_resume`, `owner_handoff` (what every caller's handoff meets first: closure, an exited handle, the recorded Run and runtime, and the Run's current coordinator and generation by Run show) and `successor_claim`, each `available`, `blocked` with its condition and message, or `unverified`, and the first available route. Such a reader also gets `successor_claim`: R110's checks of the predecessor's own record and work, in the claim's order and with its refusals, computed read-only with no successor, claim write, run-use or terminal. `retired`, `continued` and `unavailable` give their `reason`. The `--list` label (R108) is projected from Pod's records alone, with no Orca read: it is never `coordinator_lost`, treats open admissions as unsettled, and reads `unavailable` for a record that is not current or a Governor journal that cannot be read.

### R122 — Plan SEAL delivery through bounded PESs

Type: B,H · Scenarios: [A357](#a357), [A358](#a358)

A bare SEAL link, or a request to plan or decompose a SEAL's delivery, routes to the bundled `seal-delivery.md`. Identify it as a SEAL and explain the read-only planning boundary. Planning creates no Pod objective, Orca Run, worker or Pod record. Host-permitted read-only investigation is allowed; creating or editing issues, comments or the SEAL needs the owner's authorization in that conversation. The GitHub SEAL remains the only record of requirements, linked deliveries and acceptance; add no SEAL command, status helper, database, parent coordinator or phase scheduler.

Reconcile the current requirements, amendments, Delivery outline and linked or citing PESs, pull requests and delivery records across repositories. Read related SEALs only for shared requirements, with no transitive link crawl. Classify applicable criteria as verified, partial, uncovered, blocked or owner-withdrawn with evidence. Reuse established proof, reopen only proof tied to changed requirements, and stop after reporting reconciliation if nothing applicable remains open, without questions or decomposition.

For open work, ask one batched round with recommended options first, always confirming the outcome and included criteria. Other questions address only what the sources cannot settle about duplication, overlap, proportionality or ambiguity in outcomes, criteria or boundaries, never implementation choices. One follow-up round is allowed only when an answer raises a new decision. Propose owner amendments rather than withdrawing, merging or weakening requirements.

Decompose into the smallest coherent, independently verifiable set. Test whether each delivery produces a meaningful result on its own, owns distinct contracts/state/files, can be delivered or safely staged without breaking dependent projects, and reduces expected total work, risk or delay through the proposed split. Split independent work, including across projects with stable interfaces; combine coupled work when splitting adds artificial objectives, approvals or integration risk. Preserve each repository's pull request, gates and permissions, order by real dependencies and parallelize only with demonstrated benefit. No PES exists merely for a lifecycle stage, in-scope bug fix or already-proven criterion. Each open applicable criterion has exactly one owning delivery, which may reuse evidence; combined criteria name their combined proof and remain open until it exists. Fully reused proof needs no delivery.

After plan approval, use the PES template to draft only ready PESs with target, endpoint, parent requirement/criterion ids, applicable Limits, dependencies and their own Proof of Done. Publish only owner-approved drafts; authorized Delivery outline revisions use the SEAL template. Report each published PES's `/pod` invocation and dependencies, then stop. Start one ready PES in the same conversation only on explicit request, as an ordinary objective; reinvoking the SEAL reconciles first. A child's passing proof supports parent criteria but never closes them; the SEAL closes only when applicable criteria are verified or the owner explicitly disposes of them.

### R130 — Map only recognized bound PES PoD lines to current criteria

Type: B,H · Scenarios: [A381](#a381), [A382](#a382), [A383](#a383), [A384](#a384), [A385](#a385)

Only the objective's own bound PES issue is in scope. The one bounded document-recognition exception to R62 is an unindented body line beginning exactly `- [ ] **PoD#N`, `- [x] **PoD#N` or `- [X] **PoD#N`, with N of one to nine decimal digits followed by a nondigit or end of text. LF and CRLF are recognized; quoted, indented and fenced-code lines are excluded. CommonMark backtick/tilde fences use at most three leading spaces and a same-character closer at least as long; an unclosed fence excludes the rest. No other PES prose is parsed.

A criterion maps to N exactly when pod_ref_number(source.ref) is N; exact `PoD#1` maps to 1, never 10. Use every criterion in the current map, including withdrawn criteria. Proven means a nonempty mapping in which every criterion is satisfied and evidence_valid against the current facts and bound governance is true, exactly as closure requires. Withdrawal is not satisfaction. Unmapped lines are never proven or due to tick; missing or duplicated lines are never written.

The checkpoint introducing criteria, and a checkpoint rebinding objective_source, reads the live source once through the issue port and reports unmapped lines, multiply mapped lines, duplicate numbers and criteria naming missing lines, without refusing. Failed reads and body/binding digest mismatches report not_read, never fabricate facts. Facts from this checkpoint read are recorded only for the matching bound digest; no issue body is stored. Missing or stale line facts conservatively make each proven mapped line possibly due and each own tick with lost proof possibly due to untick, until a tick request reads the current line. A direct objective is not_applicable.

An own tick with an empty mapping makes an untick due; an unticked empty mapping never does. Under today's kernel a criterion cannot leave the map or change its source: kind, provenance and source are immutable, obligations are never dropped, and withdrawn criteria still map. R10 table cases D2 and D7 therefore define forward compatibility and are unreachable through today's entry points. Their synthetic controls replace only the mapping lookup, preserving real tick/status/closing entry points; they do not claim a supported map-removal operation.

### R131 — Write and recover one PoD checkbox with honest authorship

Type: B,H · Scenarios: [A386](#a386), [A387](#a387), [A388](#a388), [A389](#a389), [A390](#a390), [A391](#a391), [A392](#a392), [A393](#a393), [A394](#a394), [A395](#a395), [A396](#a396), [A397](#a397), [A414](#a414), [A415](#a415), [A416](#a416), [A417](#a417), [A418](#a418)

The dedicated `pod internal pod-tick` request takes project, objective, owner and integer pod (0-999999999). The recorded coordinator's Run authority and an open objective are required; closed objectives, objectives under historical retirement and continued objectives refuse with objective_closed, objective_retired or objective_continued before any issue read/write. A direct objective is not applicable. There is no Governor tick kind, Orca mutation, publish authorization or per-objective consent. OD-5 is standing authority for this bounded body edit alone, separate from both the yolo record and the worker-tab standing close authorization.

R10's ordered table governs each request: A checks objective and repository; B immediately runs issue_recheck_live with the binding (the intended digest while recovering), refusing body/amendment drift, role changes, unreadable sources and CLOSED; C checks the line is unique; D decides from current records/proof and that body; E classifies the result and readback. CLOSED is tested explicitly and refuses issue_reconciliation_required with reason closed_issue_requires_intent_reconciliation even when the ordinary recheck reports body drift first. No allow_closed or reopen is used. A later outside reopen permits ticking only once the ordinary recheck passes.

An unticked proven line is due to tick. Pod's own tick with lapsed proof is due to untick, including candidate movement, source/evidence invalidation, direct user reopen and withdrawal. An unticked unproven line does nothing; an unmapped unticked line refuses unmapped. Missing lines refuse line_missing and have nothing due; a number with no line, mapping or authorship persists no request. Duplicated lines refuse line_duplicated and preserve a due request. A request that needs no write returns its projected R10 status (or not_due). A current own tick needs no write. Every tick Pod did not make is left alone: proven reads current, otherwise ticked_without_pod_proof, including an unmapped line. Ambiguous ticks are left alone regardless of proof or mapping and read ambiguous.

Record a durable intent before the remote call, release the objective lock, send exactly one `gh api --method PATCH --include repos/{owner}/{repo}/issues/{N} --input -` with JSON containing exactly the body string, record the held result, then read back before anything else. The allowlist permits this new shape only for the validated bound repository/issue and a body at most 512 KiB encoded. Change exactly one recognized checkbox character, preserving all other bytes and line endings. Neither the issue body nor intended body is stored. The optional context-level pod_ticks ledger holds compact line facts, per-number latest request (including its map seq), timed intent, result/readback and current authorship. Only its latest request/receipt is kept per mapped, observed or authored number. A held result may update only that same request id, even after completion. An intent younger than the send timeout (MUTATION_TIMEOUT_S) plus five seconds refuses pod_tick_pending as in flight, without a read or write; its message and detail name intent_at and retry_after (Unix seconds). At retry_after recovery proceeds by readback alone. A clock set back keeps the request held until that time. Earlier records lacking the optional intent time retain readback recovery without conversion. Context record limits still apply. The receipt and checkpoint.objective_source.body_sha256 update share one atomic context write; map seq, obligation definitions/mapping and evidence stay unchanged. Repeated ticks cannot create a tick/untick loop.

E1: only a held success response and the intended body read back establish an own tick. Matching intended readback rebinds from the held result alone, even when its recheck holds for CLOSED (B3), an amendment or a role change (B2), or an unreadable amendment (B5); the hold remains on the request and in status; the line keeps its R10 status. Every later recheck still enforces those holds. A source already rebound by the same request is accepted, and a later held success establishes own authorship over that request’s earlier ambiguous recovery. E2: a held final 4xx refusal is read back; unchanged bound body leaves the tick due and blocked on GitHub's status/message, with no rebind, and any changed body, even the intended one, is an outside edit requiring reconciliation. E3: a lost/UNKNOWN response reads back without resending; intended body rebinds with ambiguous authorship, earlier body stays due without a refusal, and any other body holds reconciliation. E4: an interrupted intent resumes by readback alone; held success follows E1, otherwise E3, never treating the intent as authorship. E5: successful response with a different readback holds issue_reconciliation_required with reason tick_readback_mismatch and never rebinds to the intended digest. Unreadable readbacks retain a recoverable result. An ambiguous untick leaves no tick to attribute. Every later read or rebound body showing a line unticked ends its earlier authorship; matching readback alone never grants it.

A reserved/unresolved admission refuses pod_tick_admission_pending, names its original-packet settle/replay step and changes nothing; this wait never exempts a tick from closure. A pending tick intent blocks new admission with pod_tick_pending until recovery; recovery never replaces an admitted request. The request's seq records its place relative to the latest checkpoint/report. After a tick's rebind, stale frozen packets name objective_source in packet_plan_mismatch before their issue recheck; freeze a new packet only for a new admission. Admission, Governor effects and final acceptance use the new binding and retain every other R63/R64 hold.

GitHub has no conditional issue update. An outside edit between the immediate read and write can be overwritten, and a matching readback cannot reveal it. The immediate read narrows this residual race; it cannot eliminate it.

### R132 — Settle writable due ticks before either new closing write

Type: B,H · Scenarios: [A398](#a398), [A399](#a399), [A400](#a400), [A401](#a401), [A402](#a402), [A403](#a403), [A404](#a404), [A405](#a405)

For an issue-bound objective, both a closing checkpoint and a report whose accompanying map closes compute due ticks and unticks from the post-write map and current facts, including proof or withdrawal in that write. They refuse pod_ticks_due while any due tick remains writable, including conservatively due missing/stale facts and unfinished intents. The refusal writes nothing: context, map, report consumption and closure remain byte-identical. It names each PoD/direction, exact pod internal pod-tick request and steps: record the same proof without closing, run and confirm due requests, then close. The closing helper never writes the issue.

A due tick is blocked for closure only when its latest request was made after the latest checkpoint/report (the request's seq equals that map's seq) and stopped on source reconciliation/role change (B2), CLOSED (B3), an unreadable issue (B5), duplicate line (C3), or a held GitHub refusal whose readback equals the bound body (E2). E2/E3/E5 reconciliation holds count as B2. Repository mismatch and waits on pending admissions are not exemptions. Lost, UNKNOWN or interrupted responses with the earlier body remain writable; they are never refusals. A later checkpoint/report makes an earlier block insufficient; a later tick request runs the full protocol and replaces that request's block.

A closing acknowledgement names each due line left blocked, its tick/untick direction, block code/reason (including GitHub status/message) and last checkbox/authorship readback. Store those limitations in the optional tick ledger, not the closure record or closure report. After closure status retains them and nothing further is due or writable. After a direct user reopen, each later closure replaces those limitations with only the blocks it leaves itself; a resolved block never reappears from an earlier closure. When both the worker-tab and tick gate refuse, retain the existing tab refusal as primary and include both codes, worker_tabs and pod_ticks with the next steps. With no tick due existing refusal ordering stays unchanged. A direct objective, and an issue-bound objective with nothing due, close in one write as before. Existing recorded closures re-validate without this new precondition. Finalization remaining, records and finished stay unchanged.

### R133 — Show recorded PoD states and confirm each readback

Type: B · Scenarios: [A406](#a406), [A407](#a407), [A408](#a408), [A409](#a409), [A410](#a410), [A413](#a413), [A414](#a414), [A415](#a415), [A418](#a418)

Status adds pod_ticks without fetching: for every recorded or currently mapped number, the last observed checkbox (and every occurrence's checkbox when duplicated), occurrence count, authorship (own, ambiguous or not_pod), each mapped criterion's state/current proof, and R10 status (due, blocked with why, current, not_proven, ticked_without_pod_proof or ambiguous). Reasons cover unmapped, missing/duplicated lines, reconciliation/role changes, CLOSED before and after rebind, unreadable issue, GitHub refusal with unchanged readback and repository mismatch. Missing/stale facts are explicit. A map-less issue-bound objective remains readable through the CLI. When the kernel view fails, pod_ticks reports unavailable with the failure code and no due/direction or invented proof. A completed matching readback under a B2/B3/B5 hold exposes that code and reason in an additive hold field and latest_request.block; the hold remains on the request and in status; the line keeps its R10 status (a tick stays current and ticked by Pod for E1, ambiguous for E3; an untick keeps its resulting R10 status). Pre-send B2/B3 refusals never relabel a line that is not due. A pending timed intent names intent_at and retry_after in its row request, says in_flight until retry_after and recovery_ready thereafter; human status renders the same times. Pending admissions are named as waits without a closure-exempt block. After closure, limitations retain their original direction and block with the observed state. Human output renders those same facts. Readiness state/coordinator_needed and existing status fields retain their values.

Checkpoint/report acknowledgements take pod_ticks from the accepted ledger result, including every due tick/untick with its exact request and every blocked due line with its reason; intake/rebinding also carries pod_tick_intake. A closing refusal gives those requests and the nonclosing-proof steps; a closing acknowledgement names blocked limitations. R106 forbids a fresh acknowledgement read. Each tick's acknowledgement gives the receipt, held result, readback, authorship and rebind for the coordinator to confirm. A pre-write source or line refusal also names the PoD, computed direction, block and ledger-derived exact request, with no fresh acknowledgement read.

The final report lists every PoD's tick state against its criteria, every tick without Pod proof, every ambiguous tick, and every due tick or untick closure left blocked, including a held GitHub refusal. None is counted as ticked by Pod. The closure record and report_projection remain byte-compatible and contain no tick fields.

### R134 — Ship tick guidance and compatibility in the one bundle

Type: B,I · Scenarios: [A411](#a411), [A412](#a412)

The root VERSION and its bundle link remain the sole version source. Bundle pod_ticks.py and references/pod-ticks.md. Under R71 the reference is triggered for ticking, a due tick/untick, readback confirmation or closing an issue-bound objective, and supplies the protocol, closing-refusal steps, honest authorship, final-report limitations and residual race. SKILL.md names the final-report requirement; README has a short implemented-behavior mention. No word caps apply. Existing contexts lacking pod_ticks, tick requests lacking the optional intent time, and all earlier closures read without conversion; no existing schema changes, parallel body store, Governor row or Orca write are introduced. Offline tests replace only the remote/native ports, except the explicitly labelled D2/D7 mapping controls. Milestone gates, hosted CI, preceding-mechanism independent audit, owner acceptance, install and controlled disposable native trials remain separate evidence; missing live/provider evidence stays NOT_RUN.

### R135 — Expose only recorded checkpoint question facts

Type: B,H · Scenarios: [A426](#a426), [A427](#a427), [A428](#a428)

Public status for a selected objective adds `recorded_questions` with the versioned
contract below. Project only its existing checkpoint questions, sequence and
revision, including recorded identity, text, choices and resolution facts. Preserve
older string/object forms without converting stored records, inventing an id or
inferring resolution. Explicit empty lists are complete; missing/unreadable records
or an unsupported questions container are unavailable, never evidence of no
questions. Missing row facts and bounded truncation are partial and name their
gaps. The projection is read-only and adds no question store, native scrape,
private-service dependency, decision permission or lifecycle authority. Every prior
status field and human rendering keeps its behavior.

### R139 — Show the objective's yolo mode and extend the relay verifier to it

Type: B · Scenarios: [A432](delivery.md#a432), [A440](#a440)

Public status for a selected objective adds `standing_authorization`, the versioned
projection defined under the standing-authorization status contract: the mode (`yolo`
with its scope, exclusions, widening and source, or `normal`), each effect authorized
under it and each clarification the host's responder decided (R136-R138). The
acknowledgement of the checkpoint that records `delivery` carries the same projection,
and so does the stored closure report of an objective with standing decisions, so a final
report can name the mode and its effects. The closure projection also records `at_closure`:
its UTC observation time, mode, `ran_in_yolo`, the last activation id (or null) and
`active_until_closure`, making explicit that the objective ran in yolo until closure
when that was true. Later expiry preserves those closure fields. A closure of an
objective without a standing record keeps its earlier report shape and channel discriminator
through close, reopen and close, so genuine 0.18.0 still reads it; Governor DEFER text
also stays unchanged. A stop is a yolo operation and creates the record even while off.
A no-decision closure keeps the earlier report shape; its readers show `normal`,
with a stable `at_closure` or none, never a new read time. The projection reads
stored facts only. It never runs the verifier and attests nothing about the host's
present decision. An unreadable Governor journal cannot turn a committed checkpoint
into a refusal: its acknowledgement uses a journal-less standing projection, as the
other ledger readers do. Each standing decision is also listed among the owner decisions of
status and the closure report, as kind `standing_authorization` with its channel (R120).
`delivery_authorization` is current applicable consent; effects completed under yolo
are listed under `standing_authorization.effects`, even after consent becomes MISSING.
The closure owner-decision rows are compact references (id, operation, channel and
decision time), without full verdicts. Its responder text uses at most 256 encoded
content bytes plus truncation and full-record references; status outside closure
keeps full text. `internal cleanup-plan` adds the coverage R137 defines.

`pod internal standing-authorization` is the only writer of decision and answer rows;
checkpoints store the bounded lifecycle and material scope facts of R136. The host
verifier of the relayed authorization contract gains two actions, `standing` and
`answer`, for R136 and R138; `publish` and `merge` requests and verdicts stay
byte-identical. No new personal or project config key exists.

## Acceptance scenarios

- <a id="a32"></a>**A32** — Global/local coexistence preserves project policy and diagnoses duplicate/shadowed/mismatched skills.
- <a id="a33"></a>**A33** — Repeated installer runs and update preserve user files and do not create duplicate active bundles.
- <a id="a34"></a>**A34** — Config/status/doctor and internal validation reads call no models, hooks or dispatch and perform no hidden repair.
- <a id="a40"></a>**A40** — The public launcher has only the documented small command surface; `internal` is hidden and structured.
- <a id="a41"></a>**A41** — Host integrations derive from one policy, use verified discovery roots, stay inline and never override coordinator model/effort.
- <a id="a71"></a>**A71** — Ambiguous status requests selection and reports pool, constraints, decisions, drift, verification gaps and next action.
- <a id="a72"></a>**A72** — Foreign or edited launcher/skill copies are preserved and reported with an actionable path.
- <a id="a73"></a>**A73** — Unregistered canonical relocation/prohibited placement refused; owner-managed registration is a separate cutover gate.
- <a id="a75"></a>**A75** — Hosted Linux CI, incident discovery, compile/whitespace, skill validation, source hygiene, skills-CLI installation, independent audit and implemented-only docs cover each change to `main`; CI publishes nothing.
- <a id="a77"></a>**A77** — The one-shot installer from `main` installs both global skills and the user-local `pod` command from one bundle.
- <a id="a78"></a>**A78** — A copied bundle runs from an unrelated directory with no `PYTHONPATH` and no source tree; a missing prerequisite prints one actionable step, never an import traceback or an invented payment requirement.
- <a id="a97"></a>**A97** — The installed intake and three canonical authoring templates have distinct direct read-when triggers; each template holds a role definition, external issue title and explained fenced body, with numbered PoD outcomes and explicit delivery in PES and no parallel skeleton.
- <a id="a98"></a>**A98** — The PES authoring reference and a representative filled spec remain ordinary readable Markdown without parser-only boilerplate or empty ceremony.
- <a id="a99"></a>**A99** — Complete authorized issue intake returns the full body and exact identity, while inaccessible or incomplete reads fail clearly without requesting credentials or claiming success.
- <a id="a100"></a>**A100** — From another repository or a directory with no GitHub repository identity, intake makes one authorized read: a validated, complete exact SEAL/EEL receives its role refusal, with EEL precedence; every other result keeps `repository_mismatch`. Recheck refuses a mismatch without reading. SEAL next action names read-only SEAL delivery planning, a bounded PES citing selected SEAL requirements and an explicitly scoped analysis request. Issue text/comments cannot grant authority or implementation.
- <a id="a101"></a>**A101** — A closed issue requires outcome and user-intent reconciliation rather than silently repeating work.
- <a id="a102"></a>**A102** — Body changes require reconciliation while metadata-only updates preserve valid derived work, except a title that gains an EEL/SEAL marker, which is refused; relevant amendment bindings remain bounded.
- <a id="a103"></a>**A103** — Continuation reuses objective/native state across linked worktrees, preserves unaffected evidence and prevents a revised packet from replacing an uncertain same-Task attempt.
- <a id="a111"></a>**A111** — Generic tracked-source/artifact hygiene detects representative private residue while permitting Pod identity, public links and sanitized fixtures.
- <a id="a112"></a>**A112** — Public code and guidance require no external metadata service; AGENTS keeps only the short upstream ownership notice and introduces no competing writer.
- <a id="a113"></a>**A113** — README covers the one-shot installer, normal session journey, TUI, limits, update and removal with linked detailed guidance.
- <a id="a114"></a>**A114** — Long guidance validates without a word/line-count refusal or warning. Validation reports per-file stored word counts and one total as informational, not loaded context; it preserves metadata, VERSION, byte, inventory, symlink and helper checks, the five sections and unique triggered router links, and rejects missing/duplicate/untriggered links and cross-reference links. Metadata and VERSION restrictions are reported before body checks, including duplicate VERSION with an invalid body.
- <a id="a115"></a>**A115** — Human status names selected objective/source, worktree, relevant native work, blocker, next action, assignments, retained terminals and remaining gates; JSON retains detailed derived evidence without side effects.
- <a id="a117"></a>**A117** — Focus updates the inspector without a model call; Space, `p` and `P` save exact-route edits immediately; a kept `pod/v1` file opens route setup instead of allowing edits, and saves only after confirmation; a save conflict or an outside change re-reads the file and rebuilds the preview, keeping only choices that still apply and naming any it cleared.
- <a id="a118"></a>**A118** — The root `VERSION` holds one MAJOR.MINOR.PATCH line and is the only authored version; the bundle's `VERSION` links to it, an installed copy carries it as a file, `doctor` reports it, and no other file declares a version.
- <a id="a119"></a>**A119** — Public commands and JSON/private output match the small launcher contract, with no accidental installer or artwork output in machine reads.
- <a id="a133"></a>**A133** — Status and doctor show preference path/revision, constraints, native evidence, drift, mismatch and next action read-only.
- <a id="a134"></a>**A134** — One-shot install/update is idempotent and preserves valid YAML, modified copies and foreign files.
- <a id="a135"></a>**A135** — Unit, PTY/subprocess, installed-bundle, hosted, live and review evidence are recorded separately.
- <a id="a136"></a>**A136** — Root VERSION is the only authored version and installed skills and launcher report it consistently.
- <a id="a137"></a>**A137** — A truncated download or interrupted dependency/skill copy yields no false success, and rerun repairs an incomplete installation.
- <a id="a139"></a>**A139** — The route table and a full-width bottom dock with its four tabs remain readable at 160×45, 100×30, 80×24, 60×20 and 40×12, never as a right-hand panel; short heights page to every inspector line, essential roles stay legible in 16-colour light and dark palettes, strict frames sweep widths 40–200 without overflow, narrow labels keep the effort, a disclaimer accompanies any cost or time column, and `LANG=C` uses ASCII.
- <a id="a140"></a>**A140** — Edits save immediately and survive restart and external edits, while browsing, sorting, filtering, grouping, paging and comparing never write; an invalid or missing preference file is announced as read-only after one held poll.
- <a id="a142"></a>**A142** — A new install and explicit update leave ordinary agent sessions and running workers unchanged; coordinators reload before new starts.
- <a id="a175"></a>**A175** — Current exact release/terminal/ownership/stage facts and retained reason are separate from start-time warnings, and every blocked text branch renders without a traceback. Two objectives on one Run produce `selection_required` and choices until `--objective` selects one; status then reports derived assignments with Orca's own attention for each active one (`unknown` when Orca gives none), gates, progress and next action without writes or invented telemetry.
- <a id="a180"></a>**A180** — The installed five-section router and uniquely triggered task references validate under R71, preserve completion, delivery, cleanup, critical-path, model and bookkeeping anchors, and need no repository-only runtime instructions. Stored word counts are informational and do not measure loaded context.


- <a id="a183"></a>**A183** — In a real terminal, Pin and Preferred moves and clears use marks distinct from focus and route state, survive restart, sorting, filtering, resize, grouped and narrow views and ASCII/monochrome output, and stay visible as hidden summaries; invalidating edits, stale concurrent edits and persistence failures change no settings, and no action starts workers.
- <a id="a192"></a>**A192** — With a changed runtime and N open admissions, status reports 0 active and N under `assignments.unverified`, its render counts the N as unverified, and no progress line or next action waits for their delivery.
- <a id="a193"></a>**A193** — The same status names the failed read's code, the objective's bound and current runtime (unknown when unread) and Orca's attention for each unverified row, labelled as Orca's.
- <a id="a194"></a>**A194** — A verified read explicitly asserts the genuine 0.6.6 v4 and current v5 envelope markers and preserves exact equality of every other comparable projection field apart from R114's new fields, and of the rendering after its readiness line, for the same missing-fact fixture, with additive current resource/stage facts under R48 when observed, including an objective whose checkpoint recorded its consumer generation and has no rebind history; with a changed runtime the kernel's outstanding set is unchanged and status writes nothing. A sanitized R15-shaped incident (5 finished Dispatches, 4 consumed reports) shows 0 active and 5 unverified.
- <a id="a200"></a>**A200** — For every `pod internal` operation, a request missing one required field and carrying one unknown field is refused naming the operation, the record and both fields; a sentinel value in the request appears on neither stdout nor stderr. Every shape or text-limit refusal also lists complete accepted/optional constant names and every bounded field of its record from the discovery table; a non-object names its expected type without values.
- <a id="a201"></a>**A201** — The same inputs keep their acceptance/refusal codes and prior diagnostic detail, with additive contract and repair data; obligation-kernel referents and corrections are self-describing without values except known policy digests, and numerous, long or control-character caller field names are bounded and cleaned. Accepted/optional names, bounded-field data and validated obligation-restatement lists remain complete beyond eight entries through util and kernel refusal paths; caller field names remain bounded and control-stripped.
- <a id="a202"></a>**A202** — Issue intake, including `internal issue-intake`, returns no binding and refuses `issue_is_evidence_ledger` for an `EEL:` title, or for the exact format line as the first non-empty unindented body line with or without `<br>` and with LF or CRLF endings; the refusal says the issue is evidence only and implementation needs a separate Pod Execution Spec citing it.
- <a id="a203"></a>**A203** — A bound Pod Execution Spec issue that later gains either marker, including only a title change, is refused at the next recheck; continuation, affected admission, final verification and `internal issue-recheck` surface the EEL refusal, not `issue_reconciliation_required`.
- <a id="a204"></a>**A204** — A PES citing an EEL is accepted unchanged; an indented or later marker line, inside a code block or not, a lowercase or non-prefix title and an amendment do not mark an issue; direct objectives and EEL source references are unaffected; the runtime interpretation rule belongs to intake; the EEL authoring reference contains the copyable marker and role explanation.
- <a id="a216"></a>**A216** — `pod models [--json]`, `pod models refresh [--check] [--json]` and `pod models status [--json]` print the projection, delegate to the refresh and offline status, exit nonzero for a refresh that promotes or checks nothing, report a busy cache or missing observation module as a blocked envelope, and keep `pod update` software-only.
- <a id="a217"></a>**A217** — The table lists exact routes from the shared projection sorted by AA index descending with unknowns last and stable ties; every metric, model, effort and state sort works in both directions with the sort metric visible; the grouped view collapses and keeps routes reachable; filters compose with search; new and unsupported observations are visible by default and non-routable, supported-only filtering remains available, and clearing filters restores the inclusive view; focus and hidden selections survive filtering, sorting and refresh.
- <a id="a218"></a>**A218** — Inspector tabs show the disclaimer, profiles and qualifiers, units, each source's latest attempt with its earlier rows, authored routing guidance and native limits; the status line reports enabled count, Pin, Preferred, data age, refresh state, native status, last save and the config path as width allows, with exact Pin and Preferred keys never shortened, the age always available in Sources and first on the status line of every overlay; table and inspector scroll independently.
- <a id="a219"></a>**A219** — The palette runs the same commands as the shortcuts; bulk scopes preview exact routes and save once; reset enables only the listed shipped routes and keeps Pin and Preferred; a bulk change that disables the pinned route requires an explicit clear; an outside change while the bulk dialog is open saves nothing and re-previews; read-only and setup-required files refuse edits.
- <a id="a220"></a>**A220** — Compare holds at most four routes with bounded point and percentage deltas, unknown or not-comparable values and no zero-baseline percentage; the frontier marks only shown rows within one like-for-like set of source, methodology and profile qualifiers and says it is not a recommendation; differing profiles carry a caveat; changed data and methodology or profile changes are reported against the one previous snapshot.
- <a id="a221"></a>**A221** — Opening the workspace renders cached data and runs one automatic refresh only when due, writing the cache only; manual-only makes no network call until an explicit refresh; data at least seven days old is stale; a reopen after a failure is restrained; quitting cancels without promotion while keys stay responsive; refresh keeps focus, view and compare marks; each outcome is named as it is (updated, no changes, checked, refused with its diagnostic, superseded, cancelled, failed with its code), and a promoted refresh reports AA row additions, changes and removals; refreshed measurements and newly discovered rows appear without restarting or changing discovery filters; stale data stays marked at every supported size.

- <a id="a227"></a>**A227** — Every kernel refusal has a record/field referent and known obligation context, complete accepted/optional shape names, bounded missing/unsupported names, text bounds or allowed vocabulary without values. Detail-specific next actions, complete dependent-wait and settled-executor restatements within the map bound, operation-specific same-request repair, named admission-map keys and unused Task recovery guidance retain existing codes and authority.

- <a id="a230"></a>**A230** — Status reads exact current release, terminal, ownership, retention and Dispatch stage facts separately from start-time placement warnings, preserves live/older missing-field rendering, never presents a released worker as running, and renders all blocked branches with exit 1. Unknown objectives name the objective, repository context and searched state root; reads write nothing.

- <a id="a233"></a>**A233** — Governor, proof_scope/REUSE, placement and recurring-delta correction guidance retains its task-based placement under R71; the single location-aware anchor table preserves required topics exactly once in their owning files and all changed offline coverage pointers resolve. Counts stay informational, not a measure of loaded context.

- <a id="a251"></a>**A251** — Production JSON acknowledgements support the same actual subsequent checkpoint/report/disposition, version reload, delivery/closure, constraint revoke, Governor preflight/decision/outcome and same-admission recovery requests as the unprojected handler results, across successful, refused, pending, UNKNOWN and recovered sibling states. Stored bytes and native/provider effects before/after projection remain equal. Exact result equality covers each explicit exception; bound candidate metadata, merge_commit, warnings, invalidations, effective uncertainty/mismatch and recovery references survive. No routine full-record read is needed by the caller.
- <a id="a252"></a>**A252** — `pod models --json --eligible-only` returns exactly the eligible subset of the existing guidance-bearing route rows, preserving metrics, qualifiers, provenance, preferences, diagnostics and full-registry summary. Disabled/not-set routes and unmapped observations are absent; invalid/setup/empty eligibility yields no routes with existing exit behavior. The default JSON and terminal behavior are unchanged, and the option refuses text and refresh/status subcommands without fetching or writing.

- <a id="a253"></a>**A253** — Exact SEAL title/first-line forms refuse primary executable intake; conflicting identity stays restrictive with EEL precedence. Intake from another repository or a directory without GitHub repository identity makes one authorized read: only a validated, complete exact SEAL/EEL receives its role refusal, every other result keeps `repository_mismatch`, and recheck refuses a mismatch without reading. The shared SEAL next action offers read-only SEAL delivery planning, a bounded PES citing selected SEAL requirements and an explicitly scoped analysis request. Role changes, changed bodies/amendments and inaccessible reads block affected forward Governor decisions/effects without new worker/provider effects. Cancellation remains available through the same authority/candidate gates after such changes or closure. Closure alone permits already authorized Governor finalization, while new admission retains its intake reconciliation boundary. Provider readback and outcome recording remain usable. Quoted/code/later markers, linked requirements and scoped analysis remain supported.
- <a id="a254"></a>**A254** — Same-repository and cross-repository parents supply selected requirements/Limits while the child retains its target. Existing checkpoint/evidence dependencies reopen affected proof for relevant changes, preserve proof for unchanged requirements and record a scoped external wait for inaccessible required sources. Unrelated parent coverage/status edits do not invalidate proof.
- <a id="a255"></a>**A255** — Representative single-system and cross-system SEALs, standalone and child PESs and a no-change EEL preserve their section responsibilities, first-line declarations, external titles and optionality. Observed transcripts name references read for current actions and necessary prerequisites, with no directory preload or reading merely because of links; templates are read only for authoring/revision, guidance reloads when lost, changed or insufficient, and trivial work reads none. Static links/counts establish wording and routing only, never selective reading behavior.
- <a id="a256"></a>**A256** — `pod status --list --json`, with or without `--repository PATH`, lists every recorded objective of the repository by its state key, with record, phase, schema, recorded worktrees, Runs and succession links, and excludes other repositories' records; it reads no Orca, preference or provider state and writes nothing.
- <a id="a257"></a>**A257** — A missing state root answers `absent` and an unenumerable or redirected one `unreadable`; unreadable and unattributed records are listed apart and make the list incomplete, an older-schema record reads `superseded`, and more rows than the bound set `truncated` with the total. `--worktree` lists exact location matches and, as `repository`, members whose work lacks a location; `--list` with `--objective` or `--run`, or a scope flag without `--list`, is a usage error.
- <a id="a265"></a>**A265** — For a closed objective whose exact native read fails, every open admission is listed under `assignments.settled` with `settlement: closure` and the closure sequence, none is unverified, the render says it was settled by closure and the next action is `objective closed`; an open objective keeps the unverified projection of A192.
- <a id="a275"></a>**A275** — For open work the recorded coordinator, a live peer terminal and a reader outside Orca get the same `readiness` (`working`, `coordinator_needed: true`, the recorded handle, Run, generation and runtime, the coordinator live), as does a status whose Run selects the objective; the human output's first line is its summary; only the non-owner readers get `successor_claim`, here refused `predecessor_live`; nothing is written or mutated.
- <a id="a276"></a>**A276** — A quiescent map blocked on the owner reads `waiting`, names the obligation, party and need, and acts next with the owner, for owner and non-owner alike.
- <a id="a277"></a>**A277** — A candidate with a merge PASS, a pending publication-derived validation and a settled worker whose report is unconsumed reads `finalizing`, never closed: its `finalization`, equal to the Governor's projection of the same records, names the delivery record, the validation readback with the publication it derives from, the report consumption and closure, and the summary names them and says it is not safe to close; a merge decided with an UNKNOWN outcome names `merge_outcome`; a coordinator-held active obligation without a decided merge stays `working`.
- <a id="a278"></a>**A278** — A recorded closure reads `closed` with finished finalization, `coordinator_needed: false` and no coordinator read, still after the target advances and after its bound worktree is removed.
- <a id="a279"></a>**A279** — A closure that lists remaining gates reads `closed_external`, naming each gate with its party (the provider for hosted CI, otherwise unnamed) and the owner acting next, with `coordinator_needed: false`.
- <a id="a280"></a>**A280** — With Orca reporting the recorded coordinator stale, every non-owner reader gets the same `coordinator_lost`: same-Run resume blocked, owner handoff available (fresh) and the successor claim available with its predecessor facts and unreported admission, the first available route named. With the coordinator's terminal exited the handoff is blocked `owner_not_lost` and the route is the successor claim. Nothing is written.
- <a id="a281"></a>**A281** — A lost, exited coordinator whose publication left a pending derived validation reads `coordinator_lost` with the successor claim available, carrying that validation (R118), and the validation readback still to record; once a pending release is also recorded, no route is available and the summary names the claim's exact refusal `predecessor_effect_pending`.
- <a id="a282"></a>**A282** — An objective under the owner's historical retirement reads `retired` with its time and reason and a claim refused `predecessor_retired`; a claimed predecessor reads `continued` naming its successor and chain state, while the successor reads its own work; an unknown objective and an unreadable Governor journal read `unavailable` with their reason.
- <a id="a283"></a>**A283** — For open work, a lost coordinator, a closure, a claimed predecessor with its successor and an objective under historical retirement, read by the recorded coordinator and by other readers, every field that the genuine 0.12.0 status printed keeps its exact value and only `readiness` and `successor_claim` are added; the rendering is 0.12.0's after the readiness line, apart from the successor-claim line.
- <a id="a284"></a>**A284** — `pod status --list` rows carry a record-only `readiness` label without any Orca read (`finalizing` for an unrecorded merge delivery, `closed` for a closure), shown in text, and `unavailable` when the Governor journal cannot be read.

- <a id="a357"></a>**A357** — `seal-delivery.md` has its own unique read-when trigger and canonical bundle entry with installed parity. The location-aware anchor table keeps loading rules in SKILL and SEAL planning rules in the new reference, once per owning file; intake alone owns the role refusal. Independent review checks the complete R122 workflow, including the boundary at published PESs.
- <a id="a358"></a>**A358** — Observed read-only planning recognizes completed and open cross-repository SEALs from an `owner/project` checkout, reconciles current amended criteria and prior delivery with bounded related-SEAL reads, and creates no objective, Run, worker or record. Completed work reports and stops without questions or decomposition. Open work confirms outcome and criteria in one batched question round before proposing a minimal set with unique criterion ownership and combined proof, then drafts ready PESs after trial plan approval. Declined publication writes nothing; live handover remains NOT_RUN until real publication, and guidance review checks that planning stops at published PESs with invocations and dependencies rather than executing them. Reference reads are observed under A255.

- <a id="a381"></a>**A381** — Introducing checkpoints report exact PoD mappings, unmapped/multiply mapped/duplicated/missing lines and exclude LF/CRLF quote, indent and fence decoys.
- <a id="a382"></a>**A382** — A nonempty mapping needs every criterion satisfied with current evidence; both mapped criteria must pass and withdrawn criteria remain unproven.
- <a id="a383"></a>**A383** — A failed or digest-mismatched introducing/rebinding read reports not_read without refusing or recording unbound facts; absent/stale facts keep proven lines possibly due.
- <a id="a384"></a>**A384** — A direct objective is not applicable; an unticked unmapped line is never due, and missing/duplicated lines are never written (A1, C2/C3, D1).
- <a id="a385"></a>**A385** — Loss of mapping D2/D7 is unreachable through current immutable, nondroppable criterion entries; mapping-only synthetic controls exercise real tick/status/closure decisions: unticked unmapped lines are never due, while own ticks require an untick.
- <a id="a386"></a>**A386** — Objectives in the closed, `retired` or continued state refuse existing codes without writes; repository mismatch reads no issue; only the exact bound-issue PATCH shape is allowed, under standing tick authority (A2/A3).
- <a id="a387"></a>**A387** — Immediate recheck keeps outside body/amendment and EEL/SEAL title/body holds, with their reasons, plus unreadable-source holds (B2/B5).
- <a id="a388"></a>**A388** — CLOSED wins over body drift, stays blocked after reconciliation/rebind, and resumes only after an outside OPEN read passes the bound-source check (B3/B4).
- <a id="a389"></a>**A389** — One tick changes one checkbox byte; only held success plus intended readback gives own authorship and receipt/rebind; current own ticks are never rewritten (D4/D5/E1).
- <a id="a390"></a>**A390** — Own ticks untick after candidate/evidence movement, direct user reopen and withdrawal; other-actor ticks stay untouched whether proven, unproven or unmapped (D6/D8-D10).
- <a id="a391"></a>**A391** — Held GitHub refusal reads back before anything else: unchanged bound body stays due/blocked with no rebind, any changed body reconciles, and later requests recheck and replace the block (E2).
- <a id="a392"></a>**A392** — Lost/UNKNOWN responses never count as refusals: intended body rebinds ambiguously whether Pod or another actor wrote it, earlier body stays due, other drift reconciles; ambiguous ticks never untick (E3/D11).
- <a id="a393"></a>**A393** — The durable intent precedes an unlocked send; interruptions recover by readback alone, own only with held success; unreadable readbacks retain a recoverable result (E4).
- <a id="a394"></a>**A394** — Success with differing readback stops for reconciliation without rebinding the intended digest; an observed untick ends earlier authorship (E5).
- <a id="a395"></a>**A395** — Confirmed and ambiguous rebinds preserve current proof and seq, pass admission/Governor-effect/final-acceptance rechecks and cause no tick/untick loop; any other byte edit still holds all three.
- <a id="a396"></a>**A396** — Reserved/uncertain admission defers ticking without read/mutation and retains its original-packet replay; a pending tick intent defers new admission until readback.
- <a id="a397"></a>**A397** — A pre-tick packet fails packet_plan_mismatch naming objective_source before its stale issue recheck; other established refusal codes/order retain their behavior.
- <a id="a398"></a>**A398** — Both closing routes refuse proof that makes a last-criterion tick due with exact request/steps, leaving context, map, report consumption and closure byte-identical.
- <a id="a399"></a>**A399** — Both routes close after nonclosing proof, tick/readback, closure; exactly one issue write belongs solely to the tick request.
- <a id="a400"></a>**A400** — Withdrawal in the closing map makes an own untick due and refuses atomically; the generic forward-compatible mapping-loss case also gates unticks until readback.
- <a id="a401"></a>**A401** — Fresh B2/B3/B5/C3 requests permit closure with limitations; duplicates without a request and blocks older than a checkpoint/report do not exempt closure.
- <a id="a402"></a>**A402** — Both closing routes accept fresh held GitHub refusals of due ticks and unticks with unchanged body; ack/status name direction, checkbox and refusal without claiming the requested effect.
- <a id="a403"></a>**A403** — Refusals older than a checkpoint/report do not exempt closure; E3/E4 with earlier body still hold closure and never blindly resend.
- <a id="a404"></a>**A404** — E2/E3/E5 reconciliation holds count as B2; simultaneous tab/tick refusal keeps the tab code and names both reasons.
- <a id="a405"></a>**A405** — Direct/no-due objectives still close in one write; earlier recorded closures and ClosureOrderTests revalidate without this new precondition.
- <a id="a406"></a>**A406** — Fetch-free status renders last checkbox, authorship, mapped criteria/current proof and R10 due/blocked/current/unproven/ambiguous states with source/line/GitHub reasons.
- <a id="a407"></a>**A407** — Checkpoint/report acknowledgements name due tick/untick requests and blocked reasons from ledger results without a fresh read; tick output gives receipt/readback/rebind.
- <a id="a408"></a>**A408** — The additive tick field leaves readiness state/coordinator_needed and finalization remaining/records/finished unchanged.
- <a id="a409"></a>**A409** — Closed status keeps blocked direction/code/reason/readback, including GitHub refusals, with nothing further due; closure/report_projection add no tick fields.
- <a id="a410"></a>**A410** — Triggered guidance and final-report requirements name proof, other-actor/ambiguous ticks and blocked limitations; requirement and guidance state the no-conditional-update residual race.
- <a id="a411"></a>**A411** — One bundle ships the module/reference, sole root version and optional records without conversion or body storage; earlier requests without an intent time still recover by readback.
- <a id="a412"></a>**A412** — A frozen candidate requires full gates/lanes, separate hosted CI, preceding-mechanism independent audit, owner approval/install and disposable native tick/untick/outside-edit proof; fixture success cannot replace unavailable evidence.
- <a id="a413"></a>**A413** — A map-less issue-bound objective exits 0 through status’s CLI without a traceback; a failed kernel view reports unavailable tick proof with its error and no fabricated due untick.
- <a id="a414"></a>**A414** — Concurrent same-PoD callers before or after landing refuse an in-flight intent until the send timeout plus margin; expired recovery reads only, a held success upgrades the same request’s ambiguous rebind to E1, a stale result never overwrites a newer completed request, and a pre-send refusal never replaces another request’s intent. Pending refusals and status name intent_at and retry_after; a clock set back still holds until retry_after.
- <a id="a415"></a>**A415** — Matching intended readback during a CLOSED, amendment or role-change window, or an unreadable amendment (B5), atomically rebinds with own or ambiguous authorship from the held result. The B3/B2/B5 hold remains on the request and in status; the line keeps its R10 status. Map/proof stay unchanged and every later source hold remains enforced.
- <a id="a416"></a>**A416** — Sixty requests for absent, unmapped and unauthored numbers refuse line_missing while leaving context.json byte-identical and adding no request; unknown numbers on a CLOSED issue likewise persist nothing after their pre-send refusal.
- <a id="a417"></a>**A417** — Accepted writes with differing readback report issue_reconciliation_required with tick_readback_mismatch, a B2 block and the original receipt kind, without rebinding.
- <a id="a418"></a>**A418** — No-write tick acknowledgements return the row’s R10 status for unproven and withdrawn criteria and other-actor or ambiguous ticks (D3, D8–D11), with no issue write. Pre-send B2/B3 refusals preserve those labels for lines that are not due (D3, D5, D8, D9 and D11).

## Public interfaces

These elaborate the requirements above; there is one command implementation in the installed bundle.

| Command | Contract |
| --- | --- |
| `pod` | Opens the model workspace on a TTY; otherwise prints the cached route table, exiting nonzero unless preferences are valid. |
| `pod models [--json [--eligible-only]]` | The same workspace on a TTY; otherwise the cached route table, or the joined `pod-routes/v1` projection with `--json`. The JSON-only eligible filter keeps exact eligible route rows, guidance, metric qualifiers/provenance, preferences and diagnostics; it omits noneligible routes and unmapped observations. Summary counts still describe the whole registry. |
| `pod models refresh [--check] [--json]` | Fetches, validates and atomically promotes public model observations; `--check` saves no observations. Exit 0 only for `promoted` or `checked`. Never writes preferences or starts workers. |
| `pod models status [--json]` | Offline source, cache, mapping and automatic-refresh diagnostics. |
| `pod config [--json]` | Read the personal path, byte revision, preference status and any setup action, eligible routes, every supported route's state, Preferred and Pin, worker ceiling, refresh setting, bounded diagnostics and the registry's guide profiles. Reads no observations. |
| `pod config edit` | Opens the personal YAML in `$VISUAL` or `$EDITOR`, validates afterward, retains invalid edits and reports them. |
| `pod status [--objective ID] [--run RUN] [--json]` | Select an objective; report source, worktree, assignments (active, settled, or unverified when the exact native read fails), runtime rebinds, succession, gates, progress, blockers and next action, and the `readiness` answer (R114) first. Shared Runs require explicit selection. |
| `pod status --list [--repository PATH \| --worktree PATH] [--json]` | List every objective recorded for a repository or worktree (R108) from Pod's own state, without Orca, each with its record-only readiness label. Exit 0 for `listed` or `absent`. |
| `pod doctor [--json]` | Read-only installation, registry, preference, route summary, observation cache, runtime capability, message route (R111) and version diagnostics. |
| `pod update` | Runs the installer update path; active coordinators reload, active workers continue. |
| `pod --version` | Reports the installed root `VERSION` before dependency checks. |
| `pod internal <op> --input FILE` or `--input -` | Hidden structured operation for the skill; bounded JSON from a regular file or stdin, with no public command tree. A refusal is a blocked envelope with its code, message and detail; an input-shape refusal names the operation, record and fields, never values. An unexpected failure is the blocked code `internal_error`, with the traceback only on stderr. |

### Readiness contract `pod-readiness/v1`

`pod status` adds the top-level `readiness` object whenever it reports one named or selected objective (R114). A later version of this contract changes `contract`; consumers ignore fields they do not know.

| Field | Meaning |
| --- | --- |
| `contract` | `pod-readiness/v1`. |
| `summary` | Plain readiness and, while worker_tabs has rows, each row’s next step; the first line of human output. |
| `state` | `working`, `waiting`, `finalizing`, `closed`, `closed_external`, `coordinator_lost`, `retired`, `continued` or `unavailable`. |
| `coordinator_needed` | `false` only for `closed`, `closed_external`, `retired` and `continued`: the record needs no further coordinator work; worker_tabs separately names outstanding tab steps and exceptions. `null` when `unavailable`; otherwise `true`. |
| `acts_next` | `coordinator`, `worker`, `owner`, `user`, `provider`, `third_party`, `successor`, `none` or `unknown`. |
| `finalization` | R117's finalization projection of the same records (`pod-finalization/v1`), with R127’s tab wording layered onto next_action: `remaining`, the ordered names of `merge_outcome`, `delivery_record`, `validation_readback`, `report_consumption` and `closure` still to record; `records`, each name's rows (`record` id, `kind`, `state`, `derived_from`); `finished`; and its `next_action`. `null` for `retired`, `continued` and `unavailable`, including records that cannot be read. |
| `coordinator` | The recorded `handle`, `run`, `runs`, `generation` and `runtime`, and `liveness`: `live`, `lost` (stale or gone), `exited` (closed with a confirmed cause), `unknown` (with its `code`) or `not_read` when Pod no longer needs it. `null` when `unavailable`. |
| `waiting_on` | For a quiescent map, each blocked obligation with its `party` and `need`, or its wait `class` and `referent` (`authority` names the owner and `user_hold` the user). |
| `external` | For `closed_external`, each remaining `gate` and its `party`, or `null` when the gate names none. |
| `recovery` | For `coordinator_lost`, `routes` (`same_run_resume`, `owner_handoff`, `successor_claim`, each with `status` `available`, `blocked` or `unverified`, a `condition` and a `message`) and `route`, the first available one or `null`. |
| `unverified` | For `working` when the exact native settlement read fails: its `code` and the open admissions it leaves unverified. |
| `reason` | `retired`: `retired_at` and `reason`. `continued`: `successor` and `succession_state`. `unavailable`: `code` and `message`. |

A reader other than the recorded coordinator also gets `successor_claim`: `status` (`claim_available` or `claim_unavailable`), `condition`, `message`, `next_action`, `facts` (`predecessor`, `predecessor_owner`, `runs`, `ambiguity`) and, when available, the `unfinished` obligations and `unreported` admissions a claim would record. It is never a claim: the successor's own checks run only in `internal succession`. The `--list` rows' `readiness` is one of the `state` values except `coordinator_lost`.

Codex invokes `$pod ...` and Claude Code invokes `/pod ...` inside an existing
conversation. The global launcher is user-local; the one-shot installer from `main`
places both skills through the skills CLI and supplies isolated dependencies. It
may add a minimal owned PATH block when needed. There is no package, tag or release.

### Worker-tab contract

`pod status --objective ID` and a Run selecting exactly one objective add `worker_tabs` (R126), a fresh list from existing worker-show reads. Report and checkpoint acknowledgements carry the same list; report also carries `worker_tab`, including excluded/not-materialized rows that status omits. Each row has `admission`, `dispatch`, `terminal`, `state` (releasable, kept or unknown), `row` (the table case), `ownership`, `release_state`, `reason`, `owner_kept`, `next_action` (native), `command` (argv or null), `wording`, `listed` and `blocks_closure`. `owner_instruction` names an applicable keep; `failed_read` gives its code and repeat-read argv; `retention_reason` and `retained_title` describe a pending retention. The reporting worker's own owned-tab acknowledgement leads with the exactly-one rule; its optional `closure_wording` retains the status wording for readiness and finalization, which still name release owed. Internal-only row booleans express the shared table, never lifecycle state. Empty lists prove no currently listed tab, and a failed read stays unknown. Unknown optional fields remain ignorable. No new closure field or stored terminal cache is added.

- <a id="a376"></a>**A376** — A caller reads internal contract before any write, builds successful first packet, checkpoint, admission, report, map and Governor requests inside the listed bounds, and one past each listed bound refuses through the real entries; discovery and refusal contracts agree without echoed sentinels, including authorization and its decision fields, triage finding and group members, and worker_tab_instruction. Every shape or text-limit refusal carries the affected record's complete bounded_fields.
- <a id="a377"></a>**A377** — Each packet_plan_mismatch field is named without its value and repaired using only its named map fields and packet freeze; malformed frozen identities retain their earlier refusal before admission, while the serialized reservation boundary names packet_id. Missing/unsupported fields, oversized text and stored-only round trips are corrected using the refusal's complete contract. On genuine 0.15.0 and the candidate, closing and nonclosing reports with a changed native route refuse route_mismatch and record it before body validation, including a malformed body and an already-closed objective; discard and a closing checkpoint preserve its objective-bound acceptance hold. A complete native route refresh persists before a malformed nonclosing body is refused.
- <a id="a378"></a>**A378** — A wait refusal lists every affected obligation up to 96 map rows; a settled executor lists every row to restate. Report repair uses update in the same request, admission repair uses full obligations in the same admission map with update still refused, and checkpoint repair restates its rows. Entry operation tagging leaves handler detail unchanged.
- <a id="a379"></a>**A379** — All three policy mismatch sites report their known checkpoint/packet/effective digests and project-dependent recovery. A caller using a different project's effective policy repairs the refusal through the named config/map reads and packet freeze, without exposing any other refused value.

### PoD tick status contract

`pod_ticks` is additive to objective status and checkpoint/report acknowledgements. A direct objective returns `{status: not_applicable, lines: []}`. An issue-bound objective gives status observed or closed, facts_current, its last read status/reason, and lines ordered by PoD number. Each line has pod, ticked (bool or null for a nonunique line), checkboxes (last observed states for every occurrence in body order), occurrences (null for stale facts), authorship, criteria (`id`, `state`, `current`), proven, status, direction (tick/untick or null), due, blocked (code/reason plus table row and GitHub status/message when applicable), and facts_current. A due line includes the exact structured request and command. Latest request detail includes its seq, intent digests, held result and readback. waiting_on_admissions names an admission that must settle first, without changing readiness. blocked_at_closure preserves the direction, block and last checkbox/authorship even after due becomes false. Unknown optional fields remain ignorable.

### Relayed authorization verifier

R120 permits verified relayed `publish` or `merge` consent through the existing
Governor operations. The same verifier also confirms a relayed standing-authorization
activation or revocation and a responder answer, through the two further actions
described after the refusal table (R136, R138). No verifier is installed or configured
by default. The host
may set `authorization_verifier` in its one personal `pod/v2` config to an argv
list of 1-16 nonempty strings, each at most 4096 characters without NUL; the first
is an absolute executable path. Project config cannot set it. Pod uses that argv
directly, without a shell, from `/`, omitting Python path/home overrides. It sends
one JSON object and a newline on stdin and closes stdin. References and expected
bindings are never interpolated into a shell command or appended to argv. Keep
credentials out of command arguments and verdict fields.

Genuine 0.17.0 rejects a config containing `authorization_verifier`. For rollback,
the host must remove that key to restore config readability; this does not make
new verified records readable by older versions.

The command is a trusted, read-only host decision lookup. It must authenticate the
owner source and confirm the decision independently; echoing Pod's expectation
is insufficient. It must return a stable `single_use` key for the decision, unique
across the host's decisions, and the same verdict on repeated verification. Pod
owns consumption; the verifier must support these repeated reads, including at a
later Governor boundary. The contract adds no relay transport or owner service.

Pod bounds the whole invocation to five seconds and stdout to 16 KiB, kills a
timed-out/overflowing process group and discards stderr. Stdout must be one UTF-8
JSON object, without duplicate or unsupported keys. Raw output, arguments and
exception text never appear in refusal diagnostics. Nonzero exit and unavailable
executables both count as command failure.

The request has exactly these fields. A verdict repeats them and adds the four
fields in the second table; no wrapper is used.

| Request field | Contract |
| --- | --- |
| `schema` | `pod-relay-verification/v1`. |
| `reference` | Authorization `relay.reference`, nonempty UTF-8 text up to 512 characters. |
| `action` | Exactly `publish` or `merge`, the authorization's sole scope. |
| `objective` | Current objective id, nonempty text up to 256 characters. |
| `candidate` | Object with exactly `commit` and `tree`, full lowercase Git ids of one format (40 or 64 hex characters), from the prepared unit. |
| `target` | Object with exactly `repository`, `remote`, `base`: canonical GitHub `owner/repo`, prepared remote name and base branch, each nonempty text up to 256 characters. Repository comes from the remote's current single URL; publication uses its push URL. Missing, ambiguous or non-GitHub identity refuses. |
| `pr` | `null` for publish; the governed merge request's `action.pr` (integer 1-2147483647), which must equal `authorization.pr`. |

| Verdict addition | Contract |
| --- | --- |
| `verified` | Boolean. Only `true` with every expected field equal confirms consent; `false` denies it. |
| `owner_source` | Nonempty UTF-8 text up to 512 characters, supplied by the verifier and stored in `relay.owner_source`. |
| `decided_at` | Timezone-aware ISO timestamp, normalized to UTC. |
| `single_use` | Nonempty UTF-8 text up to 512 characters; stable, unique decision-use key. |

A relayed authorization uses existing `pod-authorization/v1` fields, with exactly
one scope, `channel: relayed`, `relay.reference` and target `<remote>/<base>`;
merge additionally requires equal `pr` values on the action and authorization. An input `relay.owner_source` is optional and
never trusted. Pod stores the complete confirmed verdict in `relay_verification`;
callers cannot submit that stored-only field as authorization. Project governance
performs the decided merge on that verified PR with `--match-head-commit` naming
the authorized commit. Release/deploy and combined relayed scopes remain refused.
Merge stays decide-only: project governance must invoke the Governor at its merge
boundary and execute that exact request; Pod cannot intercept a later external
merge command. Managed execution rechecks the verifier and current prepared
candidate immediately before its effect. Caller-performed publish reservations
have the same external-caller pre-effect revalidation limit as merge; use managed
`governor-execute` for the rechecked publish path. Failure after admission marks that row
FAILED with the refusal code and performs no effect; it never silently resubmits.

| Refusal code | Next action |
| --- | --- |
| `relay_verifier_unconfigured` | Configure the personal host command or obtain session consent. |
| `relay_verifier_failed` | Repair the verifier, then retry the same decision, or obtain session consent. |
| `relay_verifier_timeout` | Resolve its timeout, then retry the same decision, or obtain session consent. |
| `relay_verifier_malformed` | Correct the JSON contract or obtain session consent. |
| `relay_verifier_mismatch` | Obtain a decision for the exact expected binding; diagnostics name only differing fields. |
| `relay_verifier_denied` | Obtain a positive verified decision or session consent. |
| `relay_authorization_reused` | Obtain a fresh decision with a new key for this authorization. |

Verifier refusals occur before journal writes or remote effects. Local use receipts
under the Pod state root's `.relay-authorizations/` directory hash the key and
confirmed authorization, serialize first use across objectives and never evict.
They remain separate from objective records and journal compaction. A receipt is
written before the action row; interruption then permits only the same confirmed
authorization to retry. Deleting or replacing this trusted state loses local replay
protection; a host must preserve it. Status/closure report recorded confirmation
without running the verifier and do not attest its current availability or verdict.
New verified rows and contexts containing them carry `decision_channels: 2` so
genuine 0.17.0 readers refuse them; old records and other relays retain their formats.
A context holding a standing-authorization record, and a journal holding a row
authorized by it, carry `decision_channels: 3` so genuine 0.18.0 readers refuse them.

#### Standing and answer requests

The `standing` and `answer` actions use the same command, five-second bound, output
limits, refusal codes and next actions, and `.relay-authorizations/` single-use
receipts as above; only the standing-authorization operation sends them. A request has
`schema`, `reference` (the `relay.reference`), `action`, `objective` and `target` as in
the first table, `candidate: null`, `pr: null` and the one extra field below. `target`
comes from the objective's governance base remote, with the repository from that
remote's single URL; a missing, ambiguous or non-GitHub identity refuses
`relay_verifier_mismatch` naming `target.repository`. A verdict repeats the request and
adds `verified`, `decided_at` and `single_use`; only `verified: true` with every
expected field equal confirms it.

| Action | Extra request field | Verdict source field |
| --- | --- | --- |
| `standing` | `standing`: `operation` (`activate` or `revoke`), `scope` (list or `null`), `widening` (list or `null`) and `expires_at` (timestamp or `null`), exactly as the decision submits them. | `owner_source`, stored in `relay.owner_source`. |
| `answer` | `answer`: `question` (the recorded question id or reference) and `text` (the answer). | `responder_source`, stored in the answer row; there is no `owner_source`. |

A caller-supplied `relay_verification`, and a caller-supplied `standing` object on a
Governor authorization (R137), are refused. A mismatch in the objective, operation,
scope, widening, expiry, question or answer text refuses `relay_verifier_mismatch`
before any write.

### Recorded-question status contract

`pod status --objective ID --json`, and a status selecting one objective by Run,
add `recorded_questions` with exactly this supported schema. Consumers must inspect
its status and gaps: absent or unavailable projection never means an empty list.
Native status failure does not erase question facts already read from the checkpoint.

| Field | Meaning |
| --- | --- |
| `schema` | `pod-recorded-questions/v1`. |
| `status` | `complete`, `partial` or `unavailable`. |
| `checkpoint` | Object with `seq` and `revision`, each the recorded nonnegative integer or `null` when missing/unsupported. |
| `items` | Ordered projected rows, each with `index` (zero-based recorded position), `id`, `reference`, `text`, `choices`, `resolution` and `missing`. Position is not a stable question identity. |
| `total` | Recorded list length, including unsupported or unprojected rows; `null` when the list is unavailable. |
| `truncated` | Whether count/byte bounds omitted any recorded rows. |
| `missing` | Top-level missing facts: `checkpoint`, `checkpoint.seq`, `checkpoint.revision`, `questions`, or `items` when truncated. |
| `reason` | `null` for complete; `question_facts_missing` or `projection_limit` for partial; `checkpoint_unavailable`, `questions_missing`, `questions_unsupported`, or the record-read failure code for unavailable. |

A row copies nonempty UTF-8 `id` and `reference` strings up to 512 characters and
`text` up to 4096 characters; object `question` supplies text only when `text` is
absent. An earlier string form supplies its recorded reference and text, with no invented
id. `choices` preserves any recorded JSON value of at most 4096 encoded UTF-8 bytes.
`resolution` is an object containing only the recorded `resolution`, `resolved`,
`answer` and `status` keys, with their JSON values unchanged and the whole object
bounded to 4096 bytes. Their meaning is not inferred; an absent resolution is
unknown. Missing/unsupported/oversized fields become `null` and are named in the
row's `missing` list as `identity` (neither id nor reference), `text`, `choices` or
`resolution`. Explicit recorded `choices: null` stays distinct from an absent
choices field through that list. Other object keys remain unprojected.

At most 64 rows and 32 KiB of encoded projection are returned. A complete explicit
empty list has `items: []`, `total: 0`, `truncated: false` and no gaps. Any missing
row fact, missing checkpoint sequence/revision or truncation makes a readable list
partial; a missing/unsupported list or unreadable checkpoint is unavailable with
`total: null`. These are observations of recorded questions, not authority to answer
them or proof that unrecorded questions do not exist.

- <a id="a426"></a>**A426** — Public status projects populated questions and explicit empty lists with recorded ids/references, text, choices and resolution facts, checkpoint seq/revision and complete status. A changed question keeps its recorded id and exposes the new text/resolution and checkpoint sequence without writing on read.
- <a id="a427"></a>**A427** — Existing string questions and partial/unsupported row forms remain readable with explicit missing facts and partial status; no id or resolution is invented. Missing questions and unsupported containers report unavailable with unknown total, rather than complete emptiness.
- <a id="a428"></a>**A428** — A failed record read reports unavailable with its code. Missing sequence/revision, oversized facts, count and byte limits report explicit partial gaps/truncation. The projection preserves every prior status field and rendering, introduces no effect and does not change stored records or question authority.

### Standing-authorization status contract

`pod internal standing-authorization --input FILE` (record name "standing
authorization" in `internal contract`) takes `project`, `objective`, `owner` (the
recorded coordinator's terminal), `action` and the record the action needs:

| `action` | Input | Result |
| --- | --- | --- |
| `activate` | `decision`: `provenance`, `instruction`, optional `channel` and `relay`, and optional `scope`, `widening` and `expires_at` (R136). | Writes the activation. |
| `revoke` | `decision`: `provenance`, `instruction`, optional `channel` and `relay`. | Writes a revocation when on; while off, persists the stop barrier without a decision row or key consumption. |
| `answer` | `answer`: `question`, `text` and `relay.reference` (R138). | Writes the responder answer. |
| `show` | None. | Reads the projection below; writes nothing. |

A write returns `standing_authorization` (the projection), the stored `decision` or
`answer` row and the `context_revision`. A relayed `activate`, `revoke` or `answer`
is written only after the verifier confirms it. A full record refuses
`standing_authorization_full` (an activation at 31 or more decisions) or
`responder_answers_full` (16 answers), without a write. Stale verified standing
decisions refuse `standing_authorization_stale` before receipt consumption (R136),
with reason `reopen_time_unavailable` when a reopen predates the first record and
its time is missing. Future-dated activations refuse `standing_authorization_future`;
future-dated verified stops use receipt time as their authority-reducing barrier,
retaining the original verified time in a recorded row. A no-op relayed stop verifies
its time, falling back to receipt time on failure, and returns `state` and `changed: false`.
An inactive answer refuses `responder_answer_requires_yolo` before verification,
regardless of prior answers. An unrecorded question refuses
`decision_question_unrecorded` before answer preflight or target checks. R136/R138's complete encoded
row limits also apply and are exposed by `internal contract` as `stored_max_bytes`.

`pod status --objective ID --json`, and a status selecting one objective by Run, add
`standing_authorization` for every such objective, with this schema. An objective
that never recorded one reads `state: none` and `mode: normal`; the projection
stores nothing and runs no verifier.

| Field | Meaning |
| --- | --- |
| `schema` | `pod-standing-authorization-status/v1`. |
| `mode` | `yolo` only when `state` is `active`; otherwise `normal`. |
| `state` | `none`, `active`, `suspended`, `revoked`, `expired` or `ended` (R136). |
| `reason` | For `suspended`, `scope_changed` or `scope_unobserved`; otherwise `null`. |
| `decision` | `null`, or the latest activation: `id`, `scope`, `exclusions`, `widening`, `expires_at`, `since`, `channel` and, when relayed, `relay_reference` and `owner_source`. |
| `effects` | `total`, `truncated` and `items`: the newest 64 journal rows authorized under yolo, each with `record_id`, `kind`, `scope`, `candidate`, `tree`, `target`, `pr`, `decision`, `outcome`, `at` and `standing`. |
| `responder_decisions` | `total`, `truncated` and `items`: the newest 64 answers, each with `id`, `question`, `text`, `responder_source`, `at` and `under`. |
| `history` | `total` and `items`: every decision's `id`, `operation`, receipt/recording time `at`, ordering time `decided_at` and `channel`. For a future verified stop, `decided_at` is capped at receipt; the full record retains both original times. |
| `at_closure` | Present when captured at closure: `utc`, `mode`, boolean `ran_in_yolo`, last activation `decision` id (or null), and boolean `active_until_closure`. For a no-decision objective it is stable or absent. |

Text status prints one line, such as "Mode: yolo (publish, merge, release, install,
cleanup; session since T)" or "Mode: normal", naming the state and reason when yolo is
not active (for example "yolo suspended: scope changed"). Unknown optional fields remain
ignorable. `internal cleanup-plan` adds `standing_authorization` with `mode`,
`decision`, `covers` and `asks`, as R137 defines them.

- <a id="a440"></a>**A440** - Status JSON and text, a Run-selected status, the closure report and the delivery acknowledgement show the mode, scope, exclusions, widening and source, every effect authorized under yolo and every responder decision. Effect lists keep the newest 64 and say when they are truncated; answer storage is bounded at 16. The closure adds its observed `at_closure` mode, active decision and `active_until_closure` flag, preserving that history after expiry. Run-selected owner decisions include Governor authorization rows. None of these reads runs the verifier. `delivery_authorization` names current applicable consent, while `standing_authorization.effects` retains completed yolo effects after a stop or replacement. The closure references standing decision rows without copying verdicts and bounds responder text to a 256-byte encoded prefix.
