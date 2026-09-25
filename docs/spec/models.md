# Models

## Requirements

### R09 — Keep role, agent application, model identity

Type: B · Scenarios: [A05](coordination.md#a05), [A28](#a28), [A44](coordination.md#a44)

Keep role, agent application, model identity and effort distinct. Do not encode a universal intelligence/cost ladder or permanently expensive reviewer role.

### R10 — The user controls one personal pool

Type: B,H · Scenarios: [A09](#a09), [A10](#a10), [A45](#a45), [A120](#a120)

The user controls one personal pool: each supported model is Preferred, Available or Disabled. Preferred is a small suitability tie-breaker. Model state is separate from observed native availability; indirect text cannot expand eligibility.

### R11 — Read one personal YAML authority for

Type: B,H · Scenarios: [A05](coordination.md#a05), [A32](interface.md#a32), [A46](#a46), [A76](#a76)

Read one personal YAML authority for selection, saved model states and maximum active workers. Project YAML may hold only restrictive Governor policy. Objective constraints retain provenance without becoming another preference file.

### R12 — Parse bounded YAML safely. Reject duplicate

Type: H · Scenarios: [A47](#a47)

Parse bounded YAML safely. Reject duplicate keys, invalid types, unsupported schema/policy fields, arbitrary tags, executable includes and resource-exhausting structures. Preference data must never execute shell commands.

### R13 — Project, issue, repository and worker material

Type: H · Scenarios: [A09](#a09), [A12](#a12), [A46](#a46), [A121](#a121)

Project, issue, repository and worker material cannot widen the personal model pool or grant worker delegation. Direct user constraints may narrow it; an explicit direct exception for a Disabled model stays scoped and visible. Project Governor policy only narrows personal authority.

### R14 — Maintain exactly six base model identities

Type: B · Scenarios: [A10](#a10), [A45](#a45), [A48](#a48), [A116](#a116), [A122](#a122), [A178](#a178)

Maintain the six base model identities in the bundled v2 catalog. Attributed provider descriptions (20–70 words) are provider statements; separate Pod guide profiles, effort ladders and examples support judgment; dated AA records are informational. A missing optional measurement is unknown and never blocks eligibility. Effort variants remain detail, not selectable models. The catalog supplies `suggested_use` and `ladder` to dispatch-free config reads.

### R15 — The coordinator selects a suitable eligible

Type: B,H · Scenarios: [A05](coordination.md#a05), [A06](coordination.md#a06), [A07](#a07), [A36](#a36), [A123](#a123), [A179](#a179)

The coordinator selects a suitable eligible agent/model/effort/context for each assignment and records a short reason. A suitable non-Preferred choice names why it fits; a stricter repository rule names its actual source in the route reason with `repository` provenance. A deterministic boundary validates eligibility, constraint, effort, agent and native context support without reranking or another model call. An attempt keeps its route; safe replacement needs a fresh decision. Safety refusal bars rerouting the same Task.

### R16 — Read current preferences before selection and

Type: H · Scenarios: [A11](#a11), [A12](#a12), [A49](#a49), [A124](#a124)

Read current preferences before selection and at the serialized final admission boundary. Bind their byte revision to the decision. A changed, invalid or missing preference file refuses a new start rather than silently widening the pool. Once a row is written, later edits do not alter that submitted attempt; pending same-request replay never rechecks preferences.

### R17 — Expose a dispatch-free current preference/catalog view

Type: B · Scenarios: [A34](interface.md#a34), [A50](#a50), [A125](#a125), [A178](#a178)

Expose a dispatch-free current preference/catalog view, including guide `suggested_use` and `ladder`, and validate coordinator-proposed choices deterministically. Guide order, sorting and benchmark ranks never become routing inputs or trigger model calls.

### R19 — Keep actual rate-limit, unavailable and authentication

Type: B,H · Scenarios: [A13](#a13), [A14](#a14), [A15](#a15), [A16](#a16), [A52](#a52), [A127](#a127)

Keep actual rate-limit, unavailable and authentication failures on the failed attempt with source and native retry-after. The same route is held until retry-after or a meaningful runtime/user change; alternatives require known settlement or proven no-start. There is no rotation or blind retry loop.

### R20 — A preference edit changes model eligibility

Type: H · Scenarios: [A17](#a17), [A53](#a53)

A preference edit changes model eligibility only. It does not authorize provider purchases, billing changes, service-limit changes or bypassing host/project restrictions. Existing authenticated native sessions remain native authority.

### R21 — Explicit preference mutations use a short

Type: B,H · Scenarios: [A17](#a17), [A22](#a22), [A54](#a54), [A128](#a128)

Explicit preference mutations use a short lock, targeted-key comparison and atomic validated replacement. Preserve unrelated valid settings and comments when possible, and never overwrite invalid or concurrently changed target data. Internal operations never write preferences.

### R47 — Record relevant observed outcomes and suggest

Type: B,H · Scenarios: [A37](coordination.md#a37), [A70](#a70)

Record relevant observed outcomes and suggest preferences only after a meaningful pattern. Never silently rewrite preferences, infer savings from model labels or launch paid A/B experiments automatically.

### R72 — The supported catalog is exactly Claude

Type: B,H · Scenarios: [A116](#a116), [A122](#a122), [A129](orca.md#a129)

The supported catalog is exactly Claude Opus 5.5, Fable 5.1, Sonnet 5 and GPT-6 Astra, Sol, Luna at their exact native ids. Select a supported effort or `native_default`; context is `native_default` while Orca lacks a per-worker context flag. Documented ceilings are not effective proof.

### R77 — Objective constraints retain provenance and cannot

Type: B,H · Scenarios: [A121](#a121), [A141](#a141)

Objective constraints retain provenance and cannot edit the personal file. Only direct user instructions may permit a scoped Disabled-model or descendant exception. A later saved-state or mode change lapses the model exception; indirect text may only narrow.

### R78 — Reactive failure records are attempt-local. Honor

Type: B,H · Scenarios: [A127](#a127), [A141](#a141)

Reactive failure records are attempt-local. Honor native retry-after, require settlement before alternatives, and bar same-Task rerouting after safety refusal. Routine questions use Orca reply; advisories keep the route, informational warnings need no input, and unknown or permission prompts block locally.

## Acceptance scenarios

- <a id="a07"></a>**A07** — An explicit model restriction produces no silent substitution.
- <a id="a09"></a>**A09** — Project, issue and worker input cannot expand the personal model pool or change host/project permissions.
- <a id="a10"></a>**A10** — Catalog discovery alone cannot enable a model or prove installed capability.
- <a id="a11"></a>**A11** — Preference edits govern later starts; active attempts retain their original route and decision revision.
- <a id="a12"></a>**A12** — A Disabled state blocks the next start unless direct, scoped user intent permits an exception; prior effects are preserved.
- <a id="a13"></a>**A13** — A recorded native rate limit holds the same route until retry-after or a meaningful change.
- <a id="a14"></a>**A14** — Missing service-limit metadata does not lower ordinary worker concurrency.
- <a id="a15"></a>**A15** — A recorded unavailable or authentication failure is not retried blindly.
- <a id="a16"></a>**A16** — An alternative route waits for exact settlement or proven no-start of the failed attempt.
- <a id="a17"></a>**A17** — Model-state actions change no provider billing, service or account setting.
- <a id="a22"></a>**A22** — Lost start/request responses retain their logical reservation and recover the same immutable admission through exact Orca request/native identity without a blind replacement or repeated semantic start.
- <a id="a28"></a>**A28** — Substantial or high-risk changes receive candidate-bound independent review through recorded assurance obligations, including under stricter project rules.
- <a id="a36"></a>**A36** — No fallback intended to bypass a safety refusal.
- <a id="a45"></a>**A45** — Fresh install writes six Available states without a model-approval ceremony or model call.
- <a id="a46"></a>**A46** — One personal YAML stores model states and worker ceiling; project YAML can only narrow Governor policy, while objective constraints stay local.
- <a id="a47"></a>**A47** — Duplicate/type/unknown-field/executable-tag/include and oversized/recursive YAML fails safely without execution.
- <a id="a48"></a>**A48** — Catalog identities, documented efforts and runtime capability remain separate; unsupported selection is refused.
- <a id="a49"></a>**A49** — A valid sparse custom map makes only explicit eligible states available and labels omissions “Not set (not eligible)”; invalid structure or a missing file disables new delegation while preserving read-only diagnosis and native recovery.
- <a id="a50"></a>**A50** — Captured choices validate against a supplied preference snapshot without a ranking formula or model call.
- <a id="a52"></a>**A52** — Current preferences are read at selection and final admission without a daemon or effect on productive workers.
- <a id="a53"></a>**A53** — A state change cannot enable paid provider settings or make an unavailable native model launchable.
- <a id="a54"></a>**A54** — Concurrent edits preserve unrelated keys, refuse a stale targeted key and never overwrite invalid YAML.
- <a id="a70"></a>**A70** — Feedback may suggest but never auto-edits preferences or launches unauthorized live work.
- <a id="a76"></a>**A76** — `pod config edit` targets the personal file and preserves invalid user edits while blocking delegation.
- <a id="a116"></a>**A116** — Exactly six base ids are selectable; effort variants remain detail, context uses native default without invented flags, and effective values are separately observed.
- <a id="a120"></a>**A120** — One personal file controls the three exclusive states; a fresh install enables all six without a ceremony.
- <a id="a121"></a>**A121** — Direct constraints narrow the pool; a scoped Disabled exception needs explicit user intent and lapses after a conflicting preference change.
- <a id="a122"></a>**A122** — The six-model catalog validates exact identities, official source URLs/dates, native effort labels and separate AA variant metadata.
- <a id="a123"></a>**A123** — Coordinator choice is proportionate and independently reasoned; a deterministic guard validates but never ranks suitability.
- <a id="a124"></a>**A124** — Preference changes before final admission refuse stale choices; post-row edits preserve an in-flight request and pending replay ignores preferences.
- <a id="a125"></a>**A125** — TUI sort, filter and AA metrics cannot influence the selected route or start a worker.
- <a id="a127"></a>**A127** — Actual route failures honor retry-after and require settlement before alternate dispatch; a safety refusal bars same-Task reroute.
- <a id="a128"></a>**A128** — Two concurrent TUIs preserve unrelated edits and reject a stale targeted edit; failed persistence never reports Saved.
- <a id="a141"></a>**A141** — Routine worker questions get coordinator replies; advisories retain route, while permission or unknown prompts block and safety refusal does not reroute.
- <a id="a178"></a>**A178** — The v2 catalog validates six ids and paired AA records; config JSON supplies `suggested_use` and `ladder`, while the TUI shows guide-profile data and adaptive Details from 160×45 through 40×12, including a split view from 140 columns, without a model call or dispatch.
- <a id="a179"></a>**A179** — A suitable non-Preferred worker assignment records a brief task-specific reason; a stricter repository model restriction is recorded with `repository` provenance and its source, while the running coordinator is unchanged.


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
`gpt-6-luna`. `python -m pod.catalog --check` validates it. The bundled AA
capture is dated 2026-09-25. Each effort/profile record keeps index, benchmark
USD per task, first-response and total-response seconds together. Missing or incomplete
optional benchmark rows show unknown metrics as `—` without blocking selection,
admission or config reads. If any score is missing, the six-model ranking is
unknown. Informational ranking otherwise uses each guide profile and competition
rank among the six supported base models, never AA's global rank. The TUI attributes
`https://artificialanalysis.ai/leaderboards/models`, shows benchmark age and
states: “AA metrics show each model's selected reference benchmark profile and
are informational only. Pod chooses effort/context independently for real work.”
Native account usage is separate from these observations.

Worker interaction follows this bounded decision table:

| Situation | Coordinator action |
| --- | --- |
| Routine question | Answer through `orca orchestration reply`. |
| Owner-only question | Escalate. |
| Faster-model advisory | Keep the route; use native dismissal only if available. |
| Informational warning | No response. |
| Unknown or permission prompt | Localized blocker; never auto-accept. |
| Safety refusal | No reroute. |

## Route contract

The coordinator proposes `{agent, model, effort|native_default, context:
native_default, reason}` after reading the pool. Judgment considers reasoning
need, ambiguity, risk, breadth, duration, capabilities, verification and useful
context. There is no formula, complexity tier or role table. Deterministic
selection validates the proposed id, eligibility, constraints, agent, supported
effort and native context control; it does not rank choices. Orca currently
exposes model and effort launch preferences but no per-worker context flag, so
`native_default` omits that flag. Catalog context data and AA metrics are not
native capability proof.
