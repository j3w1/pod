# Model preferences

Exact-route authority: the one personal preference file, its editing rules and the upgrade
from earlier files. Selection lives in [routing](routing.md); the registry and observations
live in the [catalog](catalog.md).

## Requirements

### R10 — The user controls one personal pool of exact routes

Type: B,H · Scenarios: [A09](#a09), [A10](catalog.md#a10), [A45](#a45), [A120](#a120), [A181](#a181)

The user controls one personal pool of exact routes. Each supported route is enabled, disabled or not set; only enabled routes are eligible, and a supported route the file does not list is not set. Zero or one enabled Preferred route is a soft preference and zero or one enabled Pinned route is a hard requirement; both may coexist, the pin controls while set, and clearing it restores the saved preference. Missing or null keeps ordinary routing; an invalid pin or preference blocks new delegation and never becomes "no pin". Route state is separate from observed native access, and indirect text cannot expand eligibility. An empty enabled pool is valid and disables delegation only.

### R11 — Read one personal YAML authority

Type: B,H · Scenarios: [A05](../coordination.md#a05), [A32](../interface.md#a32), [A46](#a46), [A76](#a76)

Read one personal `pod/v2` YAML authority for route states, Preferred, Pin, maximum active workers and the automatic-refresh setting. Project `.pod/config.yaml` keeps `schema: pod/v1` and may hold only restrictive Governor policy; the personal file may also carry personal Governor policy. Objective constraints retain provenance without becoming another preference file.

### R12 — Parse bounded YAML safely

Type: H · Scenarios: [A47](#a47)

Parse bounded YAML safely. Reject duplicate keys, invalid types, unsupported schema/policy fields, arbitrary tags, executable includes and resource-exhausting structures. Preference data must never execute shell commands.

### R13 — Indirect material cannot widen the personal pool

Type: H · Scenarios: [A09](#a09), [A12](#a12), [A46](#a46), [A121](#a121)

Project, issue, repository and worker material cannot widen the personal route pool or grant worker delegation. Direct user constraints may narrow it; an explicit direct exception for one exact Disabled route stays scoped and visible. Project Governor policy only narrows personal authority.

### R16 — Read current preferences at selection and final admission

Type: H · Scenarios: [A11](#a11), [A12](#a12), [A49](#a49), [A124](#a124)

Read current preferences before selection and at the serialized final admission boundary, and bind their byte revision to the decision. The personal file is the authority, so any byte change between selection and final admission, including an unrelated route edit or a refresh-setting change, refuses the stale start; a changed, invalid or missing preference file never silently widens the pool. Observation refresh or cache eviction is not an authority change and never invalidates a start. Once a row is written, later edits do not alter that submitted attempt; pending same-request replay never rechecks preferences.

### R20 — A preference edit changes route eligibility only

Type: H · Scenarios: [A17](#a17), [A53](#a53)

A preference edit changes route eligibility only. It does not authorize provider purchases, billing changes, service-limit changes or bypassing host/project restrictions. Existing authenticated native sessions remain native authority.

### R21 — Explicit preference mutations are atomic and targeted

Type: B,H · Scenarios: [A17](#a17), [A22](#a22), [A54](#a54), [A128](#a128)

Explicit preference mutations use a short lock, targeted compare-and-swap against the displayed values and atomic validated replacement. Preserve unrelated valid settings and comments when possible, and never overwrite invalid or concurrently changed target data. Preferred and Pin each move or clear atomically. An edit that would leave either on a route that is not enabled is refused unless the same action clears or replaces it, and the refusal distinguishes creating an ineligible choice from invalidating one. Bulk edits name the exact routes changed and save in one write; they are never wildcards for later routes. Never silently clear a pin or preference, enable a disabled route or read an invalid pin as no pin. Internal operations and observation refresh never write preferences.

### R77 — Objective constraints keep provenance and only narrow

Type: B,H · Scenarios: [A121](#a121), [A141](routing.md#a141)

Objective constraints retain provenance and cannot edit the personal file. Only direct user instructions may permit a scoped exception for one exact Disabled route or descendant delegation. Any later change of the personal file lapses the route exception; indirect text may only narrow. Earlier model-id exceptions stay readable and never match a route.

### R100 — Earlier preference files convert only through explicit setup

Type: B,H · Scenarios: [A205](#a205), [A206](#a206), [A207](#a207), [A208](#a208)

Installation never rewrites an existing preference file. A kept `pod/v1` file is read as setup required: no route is eligible, new delegation is refused with an actionable diagnostic, read-only diagnosis and native recovery still work, and its Governor policy still applies. One bounded, user-confirmed route setup previews a conservative conversion and saves it only against the expected file revision, keeping the original bytes as `config.yaml.pod-v1`. The conversion never infers a Preferred route, never invents a pin effort and never transfers a replaced generation's choice to its successor. Interrupted, concurrent or changed setup fails without loss. Older Pod parsers refuse `pod/v2`. There is no general migration engine and no objective-state conversion.

## Acceptance scenarios

- <a id="a09"></a>**A09** — Project, issue and worker input cannot expand the personal route pool or change host/project permissions.
- <a id="a11"></a>**A11** — Preference edits govern later starts; active attempts retain their original route and decision revision.
- <a id="a12"></a>**A12** — A Disabled route blocks the next start unless direct, scoped user intent permits an exception for that exact route; prior effects are preserved.
- <a id="a17"></a>**A17** — Route-state actions change no provider billing, service or account setting.
- <a id="a22"></a>**A22** — Lost start/request responses retain their logical reservation and recover the same immutable admission through exact Orca request/native identity without a blind replacement or repeated semantic start.
- <a id="a45"></a>**A45** — A fresh install enables every shipped supported route with no Preferred or Pin, `max_active: 2` and `refresh: automatic`, without a model-approval ceremony or model call.
- <a id="a46"></a>**A46** — One personal `pod/v2` YAML stores route states, Preferred, Pin, worker ceiling and refresh setting; project `pod/v1` YAML can only narrow Governor policy, while objective constraints stay local.
- <a id="a47"></a>**A47** — Duplicate/type/unknown-field/executable-tag/include and oversized/recursive YAML fails safely without execution.
- <a id="a49"></a>**A49** — A valid sparse or empty route map makes only explicitly enabled routes eligible and labels omissions “not set (not eligible)”; invalid structure, a missing file or a kept `pod/v1` file disables new delegation while preserving read-only diagnosis and native recovery.
- <a id="a52"></a>**A52** — Current preferences are read at selection and final admission without a daemon or effect on productive workers.
- <a id="a53"></a>**A53** — A route-state change cannot enable paid provider settings or make an unavailable native model launchable.
- <a id="a54"></a>**A54** — Concurrent edits preserve unrelated keys, refuse a stale targeted key and never overwrite invalid YAML.
- <a id="a76"></a>**A76** — `pod config edit` targets the personal file and preserves invalid user edits while blocking delegation.
- <a id="a120"></a>**A120** — One personal file holds enabled and disabled route states plus at most one Preferred and one Pinned route; an unlisted supported route is not set and ineligible.
- <a id="a121"></a>**A121** — Direct constraints narrow the pool; a scoped exception names one exact Disabled route, needs explicit user intent and lapses after any preference file change; earlier model-id exceptions stay readable and inert.
- <a id="a124"></a>**A124** — Preference changes before final admission refuse stale choices; post-row edits preserve an in-flight request and pending replay ignores preferences. Observation refresh or cache eviction between selection and final admission, or an absent observation module, leaves the admission unchanged.
- <a id="a128"></a>**A128** — Two concurrent workspaces preserve unrelated edits and reject a stale targeted edit; failed persistence never reports Saved.
- <a id="a181"></a>**A181** — Missing/null Preferred and Pin preserve ordinary routing; each moves or clears atomically and survives restart, comments and unrelated edits. Creating a Preferred or Pin on a route that is not enabled, and invalidating one by disabling or unsetting its route, are refused with distinct reasons unless the same action clears or replaces it. Unknown, malformed, unsupported, stale-target, invalid YAML and failed saves never widen routing or silently drop the pin, and safely parsed invalid values remain visible as bounded diagnostics.
- <a id="a205"></a>**A205** — Installing over a genuine 0.6.x preference file keeps its bytes; config, status and doctor report setup required with the setup action, admission refuses `setup_required`, and recovery and read-only diagnosis keep working.
- <a id="a206"></a>**A206** — The setup preview is pure and deterministic: supported Available or Preferred bases get every supported effort enabled, Disabled bases get every route disabled, not-set bases and replaced generations stay not set with a note, no Preferred route is inferred, `selection: all` narrows to the saved map, and an earlier pin needs an explicit exact effort of that model or an explicit clear.
- <a id="a207"></a>**A207** — Setup applies only to the expected bytes under the lock; changed bytes, concurrent setups, interruption before the copy or before the swap, and a different existing `config.yaml.pod-v1` lose nothing, and a resumed setup reuses an equal copy.
- <a id="a208"></a>**A208** — The genuine 0.6.7 parser read from Git objects refuses `pod/v2`, and a genuine 0.6.7 installation updated to this version keeps its preference bytes until the explicit setup saves.

## Configuration and defaults

Personal `${XDG_CONFIG_HOME:-~/.config}/pod/config.yaml` is the only model-preference authority.
The resolved actual path appears in the workspace and diagnosis. Process-scoped absolute
`POD_CONFIG_HOME`, `POD_STATE_HOME` and `POD_CACHE_HOME` are for disposable validation only; they
do not redirect agent or Orca profiles. Project `.pod/config.yaml` may contain only `schema:
pod/v1` and restrictive `waste_governor` settings. No per-project route pool or task preference
file exists.

```yaml
schema: pod/v2
routes:                       # an unlisted supported route is not set (ineligible)
  claude/claude-opus-5-5/high: enabled
  codex/gpt-6.1-sol/medium: enabled
  codex/gpt-6-luna/low: disabled
preferred: null               # null or one enabled route key (soft)
pinned: null                  # null or one enabled route key (exact model and effort, or no dispatch)
workers:
  max_active: 2               # 0-8
refresh: automatic            # automatic | manual (manual = no automatic network access)
```

The installer writes this shape when no file exists, with every shipped supported route enabled,
no Preferred, no Pin, `max_active: 2` and `refresh: automatic`; `pod config edit` may create the
same default. Later software or registry updates never extend that authorization. "All routes"
is a workspace view, not a mode. A missing file, invalid or incomplete structure, an unsupported
route key and an invalid Preferred or Pin have no eligible pool and never default to all routes.
An absent `refresh` reads as `automatic`; a file that is not valid allows no automatic network
access.

A write locks briefly, re-reads the targeted values, preserves unrelated changes, validates the
result and atomically replaces the file, editing only the changed lines when it can and reporting
when comments could not be preserved. A conflicting target asks for a fresh action
(`config_changed_elsewhere`). Creating a Preferred or Pin on a route that is not enabled refuses
`preferred_ineligible` or `pin_ineligible`; disabling or unsetting the Preferred or Pinned route
refuses `preferred_invalidated` or `pin_invalidated` unless the same action clears or replaces it.
The personal file's SHA-256 byte revision is `preference_revision`; `policy_revision` is the
digest of effective Governor policy alone, so route edits do not open a Governor candidate
generation.

Invalid preferences block all new delegation and remain byte-preserved. Config, status, doctor
and the workspace show bounded diagnostic `pin_diagnostic` and `preferred_diagnostic` values from
the same safe parse; they are display data, never usable routes. Unsafe YAML is not reparsed for
diagnosis. The workspace and diagnostics name `pod config edit` for invalid-file recovery.

## Upgrade from 0.6.x

A `pod/v1` personal file from 0.6.x keeps its bytes through installation and update. Pod reads
it as `setup_required`, names the action "open pod in a terminal and confirm the route setup",
still applies its personal `waste_governor` policy and reports its earlier `pinned_model` as a
diagnostic. Admission refuses `setup_required` before any capacity check.

The workspace setup screen shows a deterministic preview of the proposed `pod/v2` document with
explicit notes and the choices the user still has to make, then saves it after confirmation.
`pod config edit` remains the manual path. The conversion rules are conservative:

| Earlier state | Proposed setup |
| --- | --- |
| Supported base Available or Preferred | Every supported effort of that base enabled. |
| Supported base Disabled | Every route of that base disabled. |
| Base not set | Not set. |
| Replaced generation (`claude-sonnet-5`, `gpt-6-sol`) | Not transferred; the successor stays not set and a note lists it. |
| Preferred models | Never inferred into a route; a note asks for one exact route if wanted. |
| `pinned_model` | Saved only as an explicitly chosen exact effort of that model, or explicitly cleared; a no-longer-supported pinned base can only be cleared. |
| `selection: all` | Converted from the saved map only, so it can only narrow; a note says so. |
| `workers.max_active`, `waste_governor` | Carried over. |
| Refresh | `automatic`, with a note naming `refresh: manual`. |

Saving takes the lock, compares the expected revision of the kept bytes, copies the original
exclusively and durably to `config.yaml.pod-v1`, and then atomically replaces the file. An equal
earlier copy is reused; a different one stops the setup. Interruption before the copy or before
the swap leaves the kept file intact. Earlier Pod versions refuse `pod/v2` because of its fields and
schema.
