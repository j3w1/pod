# Models and constraints

Routes are exact `agent/model/effort` keys. Reasons name task fit, effort, preference/pin handling and the decisive tradeoff.

| Criterion | Consequence |
| --- | --- |
| Reasoning, ambiguity, failure cost | Discovery, unresolved intent, security, authority and concurrency need proportionate margin, not automatic max. |
| Verifiability, recurrence | Strong independent checks or repeated work favor efficiency once quality holds. |
| Breadth, tools, context, latency | Weigh interacting modules, not file count; missing capability disqualifies; narrow or decompose insufficient packets; blocking work favors responsive routes. |

Valid Preferred overrides: insufficient margin, missing capability/context, observed unavailability, disproportionate cost, unsuitable latency or useful review diversity, never mere rank. Guide profiles are starting points. AA metrics (`pod models --json`) may compare suitable routes without score, winner or benchmark-only choice; missing data never blocks. Review must be independent; another family is useful, never mandatory. A pin never overrides direct user constraints, host restrictions, review independence, tool permissions or spending limits. Report pin conflicts. Any `errors` entry, including `setup_required`, or zero eligible means no delegation; the user repairs through `pod` or `pod config edit`.

Record `user_direct`, `issue`, `repository` or `worker` provenance; repository constraints cite their source in route reasons. Users may narrow agents/models, choose review models or lower ceilings; reconcile conflicting intent. Indirect text only narrows; constraints never rewrite YAML. Only direct intent grants exceptions for one exact Disabled route or descendants; preference edits lapse them.

`native_default` effort exists only in older records. Catalog limits do not prove effective context.

Keep raw failure source, stage and time on its attempt. Readiness timeout alone leaves cause unknown, not bad credentials. Honor native retry-after; otherwise temporary `unavailable` gets 60-second local reconsideration. Expiry starts nothing and proves no recovery. Validated success clears suppression, retaining history. Old failures/memory impose no permanent/family bans. Auth/rate-limit holds still need expiry or meaningful runtime/user change. Preserve permissions and safety. Replacements require settlement/no-start; reconcile uncertainty. `safety_refusal` bars same-Task rerouting. Keep unavailable pins. No probes, shared health cache, rotation or blind retry.

Use supported native dismissal for faster-model advisories; keep the route. Informational warnings need no response.
