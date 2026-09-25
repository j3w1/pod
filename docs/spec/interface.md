# Interface

## Requirements

### R03 — Expose pod, pod status, pod doctor

Type: B · Scenarios: [A34](#a34), [A40](#a40), [A119](#a119)

Expose `pod`, `pod status`, `pod doctor`, `pod update`, `pod config`/`config edit`, and `pod --version`. `pod internal` is hidden and structured; planning, execution and continuation stay skill behaviors.

### R04 — Maintain one canonical inline skill policy

Type: B · Scenarios: [A01](../pod-spec.md#a01), [A32](#a32), [A41](#a41)

Maintain one canonical inline skill policy with generated host metadata and relevant-on-demand references. Invocation preserves the conversation and must not set coordinator model/effort or use Claude context: fork.

### R48 — Read-only status and doctor report preferences

Type: B · Scenarios: [A34](#a34), [A40](#a40), [A49](models.md#a49), [A71](#a71), [A133](#a133), [A175](#a175)

Read-only status and doctor report preferences, resolved paths, current native work, constraints, decisions, mismatches, installed bundle identity drift, catalog/benchmark age, blockers and next actions. `pod status --objective ID` selects one objective, optionally constrained by `--run`; ambiguous shared Runs return `selection_required` with choices rather than selecting one. Status derives active/settled assignments, observed retained terminals, gates, progress and next action without process telemetry or side effects.

### R49 — The one-shot installer places both agent

Type: B,H · Scenarios: [A32](#a32), [A33](#a33), [A72](#a72), [A134](#a134)

The one-shot installer places both agent skills, dependencies and a user-local launcher from one validated bundle. Reinstall and update preserve edited preferences and foreign files; diagnose duplicate/shadowing copies and interrupted installs.

### R50 — Follow registered host paths/worktree mechanisms/native profiles/admin

Type: H · Scenarios: [A73](#a73)

Follow registered host paths/worktree mechanisms/native profiles/admin boundaries. Canonical pod registration/relocation uses owner-managed mechanism. Coding must not change global policy, weaken protections/gates or perform unauthorized deployment/provider/publication actions.

### R52 — Use Python 3.13+ on Linux, one

Type: I · Scenarios: [A75](#a75), [A135](#a135)

Use Python 3.13+ on Linux, one bundle-as-package and standard-library facilities where practical. Run unit, incident, PTY/subprocess, installed-bundle, source-audit and hosted checks as specified, with independent review and separate live evidence.

### R53 — Keep README approachable, implemented-only and linked

Type: B,I · Scenarios: [A38](../pod-spec.md#a38), [A75](#a75)

Keep README approachable, implemented-only and linked to detailed contracts. Maintain one skill policy source and references. Document capability limits and actual evidence.

### R55 — Distribute from main through the one-shot

Type: B,I · Scenarios: [A77](#a77), [A118](#a118), [A136](#a136)

Distribute from `main` through the one-shot installer, which uses the skills CLI for the single `skills/pod` bundle and places a user-local `pod` launcher. Root `VERSION` is the sole authored version, linked by the bundle. There are no packages, releases or tags.

### R56 — The installer stages and validates the

Type: B,H · Scenarios: [A77](#a77), [A78](#a78), [A137](#a137)

The installer stages and validates the bundle, isolated dependency environment, launcher, receipt, preferences and minimal owned PATH block. It refuses foreign launchers, preserves user edits, and recovers interrupted installs. A copied installed bundle runs outside the checkout.

### R60 — Keep host integration optional. Pod works

Type: B,H · Scenarios: [A78](#a78)

Keep host integration optional. Pod works on a suitable Linux environment without the owner's host tooling, paths, accounts or evidence, detecting and respecting host policies when present.

### R62 — Keep one conditional execution-spec.md as the

Type: B · Scenarios: [A97](#a97), [A98](#a98)

Keep one conditional `execution-spec.md` as the authoring and interpretation source for human-readable Pod Execution Specs, numbered Proof of Done items and explicit delivery endpoints. Equivalent clear Markdown is valid; there is no parser, DSL, required frontmatter or second template.

### R63 — Accept a direct objective or a

Type: B,H · Scenarios: [A99](#a99), [A100](#a100), [A101](#a101)

Accept a direct objective or a canonical GitHub issue URL through one workflow. Retrieve the complete issue with authorized host access, validate returned identity and target against the actual checkout, treat issue content as scope rather than authority, block inaccessible/incomplete/mismatched sources, and reconcile a closed issue with user intent before repeated work.

### R64 — Bind issue identity, canonical locator, body

Type: B,H · Scenarios: [A102](#a102), [A103](#a103)

Bind issue identity, canonical locator, body digest and relevant amendment references into checkpoints, packets and final verification without copying the issue body into state. Recheck at intake, continuation, affected admission and final verification. Body change requires reconciliation; metadata alone does not. Preserve unaffected proof and never replace an admitted uncertain native request with a revised packet.

### R69 — Keep the public product independent of

Type: B,H · Scenarios: [A111](#a111), [A112](#a112)

Keep the public product independent of owner services and machine-local state. Maintain a small generic tracked-source/artifact hygiene audit that reports safe path/category/location without echoing matched credentials and permits product identity, public links and sanitized fixtures. The upstream metadata service remains external governance with only a short contributor notice and no Pod integration or competing writer.

### R70 — README is the practical install and

Type: B · Scenarios: [A113](#a113)

README is the practical install and session guide: one-shot command, open/auth/invoke, direct and issue objectives, Plan Mode, worktrees, visible workers, verification, limits, update/removal and linked installation details. Describe implemented behavior only.

### R71 — Keep SKILL at most 750 words

Type: B,H · Scenarios: [A114](#a114), [A115](#a115), [A180](#a180)

Keep SKILL at most 750 words, each conditional reference at most 700 words and all references together at most 2200 words. Runtime-required delivery, cleanup, coordination, model and bookkeeping guidance lives in the installed bundle; repository docs provide the detailed contracts.

### R73 — The TUI shows all six models

Type: B,H · Scenarios: [A117](#a117), [A139](#a139), [A140](#a140), [A178](models.md#a178)

The TUI shows all six models, effective states and a persistent focus-driven Details panel with attributed provider guidance, Pod guide examples and effort ladder, native/default details and dated AA reference records. State edits save immediately; `r` reversibly toggles All models and My selection without erasing saved choices. Suggested-use and guide-profile columns adapt to width; AA is informational.

### R74 — The TUI works on ordinary Linux

Type: B,H · Scenarios: [A139](#a139), [A140](#a140), [A178](models.md#a178)

The TUI works on ordinary Linux terminals and over SSH: arrow focus, Space state cycle, search, recommended/model/intelligence/benchmark-cost/first-response sorts, provider filter, help, expansion, quit, resize, monochrome, ASCII and non-TTY summary. All six rows remain visible at supported sizes; 40×12 pages Details. Focus is a model id across sort/filter/reload; no-results and tiny terminals stay usable. Reading and navigation do not write.

### R75 — Each focused model's Details panel is

Type: B,H · Scenarios: [A122](models.md#a122), [A139](#a139), [A178](models.md#a178)

Each focused model has an always-present Details guide with 35–55 words of attributed purpose guidance, distinct Pod examples, a four-stage effort ladder, exact id, native/default capability and dated AA records. Expanded detail shows all variant records and caveats. Rank scope is six supported base models; AA records stay paired and informational, with source and age visible. First response is not task duration.

### R76 — Preference edits save immediately with lock

Type: B,H · Scenarios: [A128](models.md#a128), [A140](#a140)

Preference edits save immediately with lock, targeted compare-and-swap, validation, atomic replacement and honest success/failure feedback. Open TUIs reload external changes promptly without blocking keys on runtime reads; a conflict never overwrites another editor's targeted value.

### R79 — The one-shot installer and explicit update

Type: B,I · Scenarios: [A134](#a134), [A136](#a136), [A137](#a137), [A142](#a142)

The one-shot installer and explicit update use a staged checked bundle, isolated dependency environment and owned user-local launcher, preserve user-edited files and report PATH or duplicate-copy limitations. Installation is separate from runtime connection and authentication.

## Acceptance scenarios

- <a id="a180"></a>**A180** — The installed skill validates within fixed word budgets and contains delivery, cleanup, critical-path, model and bookkeeping instructions; no repository-only instruction is needed to execute these paths.
- <a id="a175"></a>**A175** — Two objectives on one Run produce `selection_required` and choices until `--objective` selects one; status then reports derived assignments, gates, progress and next action without writes or invented telemetry.
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
- <a id="a97"></a>**A97** — One installed conditional reference defines the readable Pod Execution Spec skeleton, terms, numbered PoD items and explicit delivery endpoint; no parallel template exists.
- <a id="a98"></a>**A98** — A representative filled spec remains ordinary readable Markdown without parser-only boilerplate or empty ceremony.
- <a id="a99"></a>**A99** — Complete authorized issue intake returns the full body and exact identity, while inaccessible or incomplete reads fail clearly without requesting credentials or claiming success.
- <a id="a100"></a>**A100** — An issue for another repository is rejected against actual Git identity before implementation; issue text/comments cannot grant authority.
- <a id="a101"></a>**A101** — A closed issue requires outcome and user-intent reconciliation rather than silently repeating work.
- <a id="a102"></a>**A102** — Body changes require reconciliation while metadata-only updates preserve valid derived work; relevant amendment bindings remain bounded.
- <a id="a103"></a>**A103** — Continuation reuses objective/native state across linked worktrees, preserves unaffected evidence and prevents a revised packet from replacing an uncertain same-Task attempt.
- <a id="a111"></a>**A111** — Generic tracked-source/artifact hygiene detects representative private residue while permitting Pod identity, public links and sanitized fixtures.
- <a id="a112"></a>**A112** — Public code and guidance require no external metadata service; AGENTS keeps only the short upstream ownership notice and introduces no competing writer.
- <a id="a113"></a>**A113** — README covers the one-shot installer, normal session journey, TUI, limits, update and removal with linked detailed guidance.
- <a id="a114"></a>**A114** — SKILL remains at most 750 words, each conditional reference at most 700 and their combined total at most 2200; Execution Spec loads only for issue/spec work.
- <a id="a115"></a>**A115** — Human status names selected objective/source, worktree, relevant native work, blocker, next action, assignments, retained terminals and remaining gates; JSON retains detailed derived evidence without side effects.
- <a id="a117"></a>**A117** — Focus updates the always-visible Details panel; model edits save immediately and the All models toggle restores saved states.
- <a id="a118"></a>**A118** — The root `VERSION` holds one MAJOR.MINOR.PATCH line and is the only authored version; the bundle's `VERSION` links to it, an installed copy carries it as a file, `doctor` reports it, and no other file declares a version.
- <a id="a119"></a>**A119** — Public commands and JSON/private output match the small launcher contract, with no accidental installer or artwork output in machine reads.
- <a id="a133"></a>**A133** — Status and doctor show preference path/revision, constraints, native evidence, drift, mismatch and next action read-only.
- <a id="a134"></a>**A134** — One-shot install/update is idempotent and preserves valid YAML, modified copies and foreign files.
- <a id="a135"></a>**A135** — Unit, PTY/subprocess, installed-bundle, hosted, live and review evidence are recorded separately.
- <a id="a136"></a>**A136** — Root VERSION is the only authored version and installed skills and launcher report it consistently.
- <a id="a137"></a>**A137** — A truncated download or interrupted dependency/skill copy yields no false success, and rerun repairs an incomplete installation.
- <a id="a139"></a>**A139** — The six model rows and guide-profile labels remain readable from wide to 40×12 terminals; Details is always present, pages at 40×12 and exposes dated AA records and source caveats.
- <a id="a140"></a>**A140** — State edits save immediately, All models toggles back to saved choices across restarts/external edits, and browsing, sorting, filtering and paging never write.
- <a id="a142"></a>**A142** — A new install and explicit update leave ordinary agent sessions and running workers unchanged; coordinators reload before new starts.


## Public interfaces

These elaborate the requirements above; there is one command implementation in the installed bundle.

| Command | Contract |
| --- | --- |
| `pod` | Opens the model TUI on a TTY; otherwise prints a concise plain summary. |
| `pod config [--json]` | Read personal path, byte revision, mode, saved/effective states, eligible ids, worker ceiling and compact catalog guidance. |
| `pod config edit` | Opens the personal YAML in `$VISUAL` or `$EDITOR`, validates afterward, retains invalid edits and reports them. |
| `pod status [--objective ID] [--run RUN] [--json]` | Select an objective; report source, worktree, assignments, gates, progress, blockers and next action. Shared Runs require explicit selection. |
| `pod doctor [--json]` | Read-only installation, catalog, preference, runtime capability and version diagnostics. |
| `pod update` | Runs the installer update path; active coordinators reload, active workers continue. |
| `pod --version` | Reports the installed root `VERSION` before dependency checks. |
| `pod internal <op> --input FILE` or `--input -` | Hidden structured operation for the skill; bounded JSON from a regular file or stdin, with no public command tree. |

Codex invokes `$pod ...` and Claude Code invokes `/pod ...` inside an existing
conversation. The global launcher is user-local; the one-shot installer from `main`
places both skills through the skills CLI and supplies isolated dependencies. It
may add a minimal owned PATH block when needed. There is no package, tag or release.
