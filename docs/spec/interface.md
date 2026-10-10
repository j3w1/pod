# Interface

## Requirements

### R03 — Expose pod, pod models, pod status, pod doctor

Type: B · Scenarios: [A34](#a34), [A40](#a40), [A119](#a119), [A216](#a216), [A252](#a252)

Expose `pod`, `pod models` with `models refresh [--check]` and `models status`, `pod status`, `pod doctor`, `pod update`, `pod config`/`config edit`, and `pod --version`. Model, config, doctor and status commands offer clean `--json` machine output; the model read optionally restricts its rows with `--eligible-only`. `pod update` updates software only; model data refreshes only through `pod models refresh` or the workspace. `pod internal` is hidden and structured; planning, execution and continuation stay skill behaviors.

### R04 — Maintain one canonical trigger-led skill router

Type: B · Scenarios: [A01](../pod-spec.md#a01), [A32](#a32), [A41](#a41)

Maintain one canonical inline skill router with relevant-on-demand references and one interface description. Frontmatter retains its restrictions and repository source, with a ≤250-character description containing `Use when` and a plain compatibility line. Invocation preserves the conversation, model, effort and settings; no coordinator override or Claude context fork. Product version is authored only in root VERSION.

### R48 — Read-only status and doctor report preferences

Type: B · Scenarios: [A34](#a34), [A40](#a40), [A49](models/preferences.md#a49), [A71](#a71), [A133](#a133), [A175](#a175), [A230](#a230)

Read-only status and doctor report preferences including their status and any route-setup action, the Preferred and Pinned routes, the actual config path, a route-state summary, current native work, constraints, decisions, mismatches, installed bundle identity drift, blockers and next actions; doctor also reports the offline observation cache state. These reads never fetch. `pod status --objective ID` selects one objective, optionally constrained by `--run`; ambiguous shared Runs return `selection_required` with choices rather than selecting one. Status adds current exact release, terminal, ownership, retained reason and Dispatch stage facts, labels placement warnings start-time and renders every blocked result without a traceback. An unknown objective names its id, repository context and state root searched. Status also reports local route failure observations/deadlines and derives active/settled assignments, observed retained terminals, gates, progress and next action without process telemetry or side effects.

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

Type: B · Scenarios: [A97](#a97), [A98](#a98), [A204](#a204), [A255](#a255)

Keep `issue-intake.md` as the conditional intake/interpretation source and one installed authoring reference each for SEAL, PES and EEL. SEAL is Scope, Expectations, Acceptance & Limits: requirements for one or more systems, with zero or more PES children and an optional advisory roadmap, never a parent execution coordinator. PES is a bounded executable contract, standalone or implementing selected parent requirements; detailed implementation procedures are optional and non-fixed choices belong to the implementer. EEL evaluates observations, evidence, uncertainty and disposition, including no change; its findings require a separate PES for implementation. Explicitly scoped analysis/planning/decomposition and proportionate direct objectives remain usable without authorizing implementation of SEAL/EEL content.

The canonical references are `seal-template.md`, `pes-template.md` and `eel-template.md`. Each supplies a role definition, external GitHub title example, first-line format marker and fenced body with useful section explanations, removable placeholders/comments and optional sections. Use stable document-local R/AC/PoD/F references qualified across issues. Requirements, proof, validation, overall acceptance and delivery have separate responsibilities; unperformed or withdrawn checks never pass. Ordinary Markdown and equivalent clear headings are valid; no document parser, mandatory pipeline, universal gates/approvals or parallel templates. Authoring/revision reads its relevant template through a direct router trigger; ordinary execution reads applicable intake/runtime guidance.

### R63 — Accept a direct objective or a

Type: B,H · Scenarios: [A99](#a99), [A100](#a100), [A101](#a101), [A202](#a202), [A253](#a253)

Accept a direct objective or a canonical GitHub issue URL through one workflow. Retrieve the complete issue with authorized host access, validate returned identity and target against the actual checkout, treat issue content as scope rather than authority, block inaccessible/incomplete/mismatched sources, and reconcile a closed issue with user intent before repeated work. Executable intake recognizes exact title prefixes `EEL:` and `SEAL:` or the exact first nonempty unindented body declaration `**Format:** Evidence Evaluation Ledger v1` or `**Format:** SEAL v1`, optionally ending `<br>`, with LF or CRLF. It refuses EEL (`issue_is_evidence_ledger`) and SEAL (`issue_is_requirements_spec`) before binding. The EEL diagnostic and separate-PES boundary remain unchanged; SEAL refusal names a bounded PES or explicitly scoped analysis/planning/decomposition. Either restrictive identity blocks despite conflicting title/body declarations; EEL keeps its diagnostic when both roles occur. Quoted, indented, code-block or later body markers are not identity. Reading linked requirements/evidence remains allowed.

### R64 — Bind issue identity, canonical locator, body

Type: B,H · Scenarios: [A102](#a102), [A103](#a103), [A203](#a203), [A253](#a253)

Bind issue identity, canonical locator, body digest and relevant amendment references into checkpoints, packets and final verification without copying the issue body into state. Recheck at intake, continuation, affected admission and final verification. Body change requires reconciliation; metadata alone does not, except that a title gaining an EEL/SEAL marker stops execution like the body marker, and every caller surfaces the role refusal itself. Forward Governor decisions/execution recheck the checkpoint source before native continuity or remote effects; role changes, body/amendment drift and inaccessible reads hold forward work. Cancellation, outcome recording and provider readback retain their supported paths. Closure alone does not block already authorized Governor work: reconcile closed-issue intent at intake, and rebind reconciled body/amendment changes through the existing checkpoint. Amendments are not checked for markers. Preserve unaffected proof and never replace an admitted uncertain native request with a revised packet.

### R69 — Keep the public product independent of

Type: B,H · Scenarios: [A111](#a111), [A112](#a112)

Keep the public product independent of owner services and machine-local state. Maintain a small generic tracked-source/artifact hygiene audit that reports safe path/category/location without echoing matched credentials and permits product identity, public links and sanitized fixtures. The upstream metadata service remains external governance with only a short contributor notice and no Pod integration or competing writer.

### R70 — README is the practical install and

Type: B · Scenarios: [A113](#a113)

README is the practical install and session guide: one-shot command, open/auth/invoke, direct and issue objectives, Plan Mode, worktrees, visible workers, verification, limits, update/removal and linked installation details. Describe implemented behavior only.

### R71 — Bound the router, execution and authoring guidance

Type: B,H · Scenarios: [A114](#a114), [A115](#a115), [A180](#a180), [A233](#a233)

SKILL has exactly Roles, Done when, Boundaries, Helpers and Read when needed, at most 400 whitespace-delimited words including frontmatter. Each of its eleven references has at most 450 words. The eight execution references have at most 2000 combined; only the three canonical authoring references use the separate allowance of at most 1350 combined. Each has one unique triggered link from SKILL and no reference-to-reference links. The delivery path (SKILL plus issue-intake, planning, orca-boundary, models, verification and governor) is at most 2000 words. Validate frontmatter before word budgets and print every file count and execution, authoring, delivery, all-reference and full installed-guidance totals. Do not move runtime rules into templates or uncounted folders to evade limits; preserve useful meaning. Runtime-required delivery, cleanup, coordination, model and bookkeeping guidance lives in the installed bundle, preserving authority, completion, judgment and recovery; repository docs provide contributor checks and detailed contracts.

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

Type: B · Scenarios: [A192](#a192), [A193](#a193), [A194](#a194)

When status's exact native settlement read fails, every open admission is listed under `assignments.unverified`, not active, and the human summary counts it separately. Status names the failed read's code and the objective's bound and current runtime, or unknown, labels Orca's attention for those rows as Orca's and claims no wait for their delivery; the next action names the unverified settlement. The shared envelope is now `pod-cli/v5`: the genuine 0.6.6 comparison asserts its old v4 marker and the new v5 marker separately, with exact equality of every other comparable projection field and the rendering. This is presentation only: the kernel's outstanding set, settlement rules, logical slots and fail-closed mutation are unchanged, and existing verified fields retain their meanings. Current resource/stage facts are additive under R48; older missing facts stay empty or absent and preserve rendering.

### R99 — Input refusals name their fields

Type: B · Scenarios: [A200](#a200), [A201](#a201), [A227](#a227)

An `exact()` shape refusal lists its record's complete accepted and optional field names from code constants, including for a non-object. Missing/unsupported caller names remain bounded in count and length; all names remain control-stripped, and no values are echoed. A non-object also names the expected type, and a `pod internal` refusal also names the operation. The same inputs are accepted and refused with the same codes; kernel refusals retain codes/details and name record or field plus a known obligation, exact missing/unsupported names, text limit/length or allowed vocabulary. No refused field value is echoed. Detail-specific next actions describe receipt conflicts, history bounds and malformed input; wait lists are complete within their bound and pre-row admission failures explain same-Task retry and Orca settlement. There are no prose examples or generated schemas.

### R106 — Compact successful helper writes at the output boundary

Type: B,I · Scenarios: [A251](#a251)

`pod-cli/v5` versions public envelopes and private JSON output. Successful checkpoint, report, constraint add/revoke, admission and route-failure acknowledgements omit objective/full-row echoes. Checkpoints select explicit next-consumer bindings and notices, with compact continuity/closure identities rather than stored histories. Governor prepare, outcome and classify and continuity/handoff writes compact only stored-row echoes; preflight/correct already return bounded projections. Preserve status, objective, written identities, map seq/revision and next_seq where a map is involved, next safe action, warnings, invalidations, report ingestion/disposition facts and exact request/native/route/recovery references. Admission retains its verification binding (including unknown/null context), effective-route uncertainty/mismatch and inherited evidence, placement bindings and any warnings. Governor prepare retains candidate identity, generation, commit/tree, dirty state, base/branch/workflow bindings, verification/toolchain/environment/policy and superseded validation ids; outcome retains provider facts including merge_commit. Packet, source, map and exactly governor/governor-execute/governor-reconcile result payloads remain equal; status and other bounded read-only projections keep their behavior. In-process handlers retain kernel values before JSON compaction; report identifiers are projected from the authoritative accepted map, including grouped findings and generated proposal ids. Projection changes no persistence, authorization, acceptance/refusal, record bounds, effect order or recovery. Local metadata readback adds no native read/effect; an unavailable readback leaves the write successful with an explicit warning to read map before the next map write. Routine follow-up needs no full-record read; map remains the resume/map_stale path.

### R107 — Trace selected parent requirements through existing sources

Type: B · Scenarios: [A254](#a254)

A child PES identifies its parent and selected requirements/Limits without copying it or retargeting execution. Retain locator, observed body/amendment digests and selected requirement versions in existing references, checkpoint verification dependencies and evidence. Read parents through authorized issue access (for example, `gh issue view`), rather than primary executable intake; this adds no helper parent-fetch interface. Read required parents at intake, continuation and affected verification. A relevant source change requires scoped reconciliation before affected work; inaccessible required sources name an external dependency. Bind proof to its relevant dependencies so unrelated coverage/status changes preserve applicable evidence. No parent registry, recursive crawler, automatic issue publisher or parent runtime is added; source observations confer no execution authority.

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
- <a id="a100"></a>**A100** — An issue for another repository is rejected against actual Git identity before implementation; issue text/comments cannot grant authority.
- <a id="a101"></a>**A101** — A closed issue requires outcome and user-intent reconciliation rather than silently repeating work.
- <a id="a102"></a>**A102** — Body changes require reconciliation while metadata-only updates preserve valid derived work, except a title that gains an EEL/SEAL marker, which is refused; relevant amendment bindings remain bounded.
- <a id="a103"></a>**A103** — Continuation reuses objective/native state across linked worktrees, preserves unaffected evidence and prevents a revised packet from replacing an uncertain same-Task attempt.
- <a id="a111"></a>**A111** — Generic tracked-source/artifact hygiene detects representative private residue while permitting Pod identity, public links and sanitized fixtures.
- <a id="a112"></a>**A112** — Public code and guidance require no external metadata service; AGENTS keeps only the short upstream ownership notice and introduces no competing writer.
- <a id="a113"></a>**A113** — README covers the one-shot installer, normal session journey, TUI, limits, update and removal with linked detailed guidance.
- <a id="a114"></a>**A114** — SKILL is ≤400 words including frontmatter, each reference ≤450, execution references ≤2000, authoring templates ≤1350 and the declared delivery path ≤2000; validation reports counts, rejects missing/duplicate/untriggered links and cross-reference links, and a full-400-word SKILL with duplicate VERSION reports the version restriction first.
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
- <a id="a180"></a>**A180** — The installed five-section router and triggered references validate within fixed budgets, contain completion, delivery, cleanup, critical-path, model and bookkeeping guidance, and need no repository-only runtime instructions.


- <a id="a183"></a>**A183** — In a real terminal, Pin and Preferred moves and clears use marks distinct from focus and route state, survive restart, sorting, filtering, resize, grouped and narrow views and ASCII/monochrome output, and stay visible as hidden summaries; invalidating edits, stale concurrent edits and persistence failures change no settings, and no action starts workers.
- <a id="a192"></a>**A192** — With a changed runtime and N open admissions, status reports 0 active and N under `assignments.unverified`, its render counts the N as unverified, and no progress line or next action waits for their delivery.
- <a id="a193"></a>**A193** — The same status names the failed read's code, the objective's bound and current runtime (unknown when unread) and Orca's attention for each unverified row, labelled as Orca's.
- <a id="a194"></a>**A194** — A verified read explicitly asserts the genuine 0.6.6 v4 and current v5 envelope markers and preserves exact equality of every other comparable projection field and the rendering for the same missing-fact fixture, with additive current resource/stage facts under R48 when observed, including an objective whose checkpoint recorded its consumer generation and has no rebind history; with a changed runtime the kernel's outstanding set is unchanged and status writes nothing. A sanitized R15-shaped incident (5 finished Dispatches, 4 consumed reports) shows 0 active and 5 unverified.
- <a id="a200"></a>**A200** — For every `pod internal` operation, a request missing one required field and carrying one unknown field is refused naming the operation, the record and both fields; a sentinel value in the request appears on neither stdout nor stderr. Every shape refusal also lists complete accepted/optional constant field names; a non-object names its expected type without values.
- <a id="a201"></a>**A201** — The same inputs are accepted and refused with the same codes/details, obligation-kernel referents and corrections are self-describing without values, and numerous, long or control-character caller field names are bounded and cleaned. Accepted/optional constant names remain complete beyond eight entries and control-stripped through both util and kernel refusal paths.
- <a id="a202"></a>**A202** — Issue intake, including `internal issue-intake`, returns no binding and refuses `issue_is_evidence_ledger` for an `EEL:` title, or for the exact format line as the first non-empty unindented body line with or without `<br>` and with LF or CRLF endings; the refusal says the issue is evidence only and implementation needs a separate Pod Execution Spec citing it.
- <a id="a203"></a>**A203** — A bound Pod Execution Spec issue that later gains either marker, including only a title change, is refused at the next recheck; continuation, affected admission, final verification and `internal issue-recheck` surface the EEL refusal, not `issue_reconciliation_required`.
- <a id="a204"></a>**A204** — A PES citing an EEL is accepted unchanged; an indented or later marker line, inside a code block or not, a lowercase or non-prefix title and an amendment do not mark an issue; direct objectives and EEL source references are unaffected; the runtime interpretation rule belongs to intake; the EEL authoring reference contains the copyable marker and role explanation.
- <a id="a216"></a>**A216** — `pod models [--json]`, `pod models refresh [--check] [--json]` and `pod models status [--json]` print the projection, delegate to the refresh and offline status, exit nonzero for a refresh that promotes or checks nothing, report a busy cache or missing observation module as a blocked envelope, and keep `pod update` software-only.
- <a id="a217"></a>**A217** — The table lists exact routes from the shared projection sorted by AA index descending with unknowns last and stable ties; every metric, model, effort and state sort works in both directions with the sort metric visible; the grouped view collapses and keeps routes reachable; filters compose with search; new and unsupported observations are visible by default and non-routable, supported-only filtering remains available, and clearing filters restores the inclusive view; focus and hidden selections survive filtering, sorting and refresh.
- <a id="a218"></a>**A218** — Inspector tabs show the disclaimer, profiles and qualifiers, units, each source's latest attempt with its earlier rows, authored routing guidance and native limits; the status line reports enabled count, Pin, Preferred, data age, refresh state, native status, last save and the config path as width allows, with exact Pin and Preferred keys never shortened, the age always available in Sources and first on the status line of every overlay; table and inspector scroll independently.
- <a id="a219"></a>**A219** — The palette runs the same commands as the shortcuts; bulk scopes preview exact routes and save once; reset enables only the listed shipped routes and keeps Pin and Preferred; a bulk change that disables the pinned route requires an explicit clear; an outside change while the bulk dialog is open saves nothing and re-previews; read-only and setup-required files refuse edits.
- <a id="a220"></a>**A220** — Compare holds at most four routes with bounded point and percentage deltas, unknown or not-comparable values and no zero-baseline percentage; the frontier marks only shown rows within one like-for-like set of source, methodology and profile qualifiers and says it is not a recommendation; differing profiles carry a caveat; changed data and methodology or profile changes are reported against the one previous snapshot.
- <a id="a221"></a>**A221** — Opening the workspace renders cached data and runs one automatic refresh only when due, writing the cache only; manual-only makes no network call until an explicit refresh; data at least seven days old is stale; a reopen after a failure is restrained; quitting cancels without promotion while keys stay responsive; refresh keeps focus, view and compare marks; each outcome is named as it is (updated, no changes, checked, refused with its diagnostic, superseded, cancelled, failed with its code), and a promoted refresh reports AA row additions, changes and removals; refreshed measurements and newly discovered rows appear without restarting or changing discovery filters; stale data stays marked at every supported size.

- <a id="a227"></a>**A227** — Every kernel refusal has a record/field referent and known obligation context, complete accepted/optional shape names, bounded missing/unsupported names, text bounds or allowed vocabulary without values. Detail-specific next actions, complete bounded dependent-wait lists, named admission-map keys and unused Task recovery guidance retain existing codes and authority.

- <a id="a230"></a>**A230** — Status reads exact current release, terminal, ownership, retention and Dispatch stage facts separately from start-time placement warnings, preserves live/older missing-field rendering, never presents a released worker as running, and renders all blocked branches with exit 1. Unknown objectives name the objective, repository context and searched state root; reads write nothing.

- <a id="a233"></a>**A233** — Governor, proof_scope/REUSE, placement and recurring-delta correction guidance remains within SKILL 400 words, each reference 450, execution/delivery path 2000 and authoring templates 1350; the single location-aware anchor table preserves required topics exactly once and all changed offline coverage pointers resolve.

- <a id="a251"></a>**A251** — Production JSON acknowledgements support the same actual subsequent checkpoint/report/disposition, version reload, delivery/closure, constraint revoke, Governor preflight/decision/outcome and same-admission recovery requests as the unprojected handler results, across successful, refused, pending, UNKNOWN and recovered sibling states. Stored bytes and native/provider effects before/after projection remain equal. Exact result equality covers each explicit exception; bound candidate metadata, merge_commit, warnings, invalidations, effective uncertainty/mismatch and recovery references survive. No routine full-record read is needed by the caller.
- <a id="a252"></a>**A252** — `pod models --json --eligible-only` returns exactly the eligible subset of the existing guidance-bearing route rows, preserving metrics, qualifiers, provenance, preferences, diagnostics and full-registry summary. Disabled/not-set routes and unmapped observations are absent; invalid/setup/empty eligibility yields no routes with existing exit behavior. The default JSON and terminal behavior are unchanged, and the option refuses text and refresh/status subcommands without fetching or writing.

- <a id="a253"></a>**A253** — Exact SEAL title/first-line forms refuse primary executable intake; conflicting identity stays restrictive. Role changes, changed bodies/amendments and inaccessible reads block affected forward Governor decisions/effects without new worker/provider effects. Cancellation remains available through the same authority/candidate gates after such changes or closure. Closure alone permits already authorized Governor finalization, while new admission retains its intake reconciliation boundary. Provider readback and outcome recording remain usable. Quoted/code/later markers, linked requirements and scoped analysis remain supported.
- <a id="a254"></a>**A254** — Same-repository and cross-repository parents supply selected requirements/Limits while the child retains its target. Existing checkpoint/evidence dependencies reopen affected proof for relevant changes, preserve proof for unchanged requirements and record a scoped external wait for inaccessible required sources. Unrelated parent coverage/status edits do not invalidate proof.
- <a id="a255"></a>**A255** — Representative single-system and cross-system SEALs, standalone and child PESs and a no-change EEL preserve their section responsibilities, first-line declarations, external titles and optionality. Observed agent reading distinguishes authoring from ordinary execution; static links/counts alone do not certify reading behavior.

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
| `pod status [--objective ID] [--run RUN] [--json]` | Select an objective; report source, worktree, assignments (active, settled, or unverified when the exact native read fails), runtime rebinds, gates, progress, blockers and next action. Shared Runs require explicit selection. |
| `pod doctor [--json]` | Read-only installation, registry, preference, route summary, observation cache, runtime capability and version diagnostics. |
| `pod update` | Runs the installer update path; active coordinators reload, active workers continue. |
| `pod --version` | Reports the installed root `VERSION` before dependency checks. |
| `pod internal <op> --input FILE` or `--input -` | Hidden structured operation for the skill; bounded JSON from a regular file or stdin, with no public command tree. A refusal is a blocked envelope with its code, message and detail; an input-shape refusal names the operation, record and fields, never values. An unexpected failure is the blocked code `internal_error`, with the traceback only on stderr. |

Codex invokes `$pod ...` and Claude Code invokes `/pod ...` inside an existing
conversation. The global launcher is user-local; the one-shot installer from `main`
places both skills through the skills CLI and supplies isolated dependencies. It
may add a minimal owned PATH block when needed. There is no package, tag or release.
