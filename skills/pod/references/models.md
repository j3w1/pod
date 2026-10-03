# Models and constraints

Optimize for a verified result, minimizing expected total work, cost, delay and rework, not next-call price. Route reasons name task fit, effort, preference/pin handling and the decisive tradeoff.

| Criterion | Consequence |
| --- | --- |
| Reasoning, ambiguity, failure cost | Discovery, unresolved intent, security, authority and concurrency need proportionate margin, not automatic max. |
| Verifiability, recurrence | Strong independent checks or repeated work favor efficiency once quality holds. |
| Breadth, tools, context, latency | Weigh interacting modules; missing capability disqualifies; narrow or decompose insufficient packets; blocking work favors responsive routes. |

Valid Preferred overrides include insufficient margin, missing capability/context, observed unavailability, disproportionate cost, unsuitable latency and useful review diversity. Highest rank, lowest price, provider brand, file count, reviewer role, free capacity or stale failure alone never decides. Guide profiles are starting points: known hard tasks may start strong without exhausting cheaper routes; repeated failure needs diagnosis, not an effort ladder. After suitability, AA metrics (`pod models --json`) may compare routes without score or winner; a route no better on any selected comparable dimension and worse on at least one normally needs an assignment-specific reason. Missing data never blocks. Review must be independent; family diversity helps, is never mandatory, never defeats a pin and never adds reviews. A pin never overrides direct user constraints, host restrictions, review independence, tool permissions or spending limits. Report pin conflicts. Any `errors` entry, including `setup_required`, or zero eligible means no delegation; the user repairs through `pod` or `pod config edit`.

Record `user_direct`, `issue`, `repository` or `worker` provenance; repository constraints cite their source in route reasons. Users may narrow agents/models, choose review models or lower ceilings; reconcile conflicting intent. Indirect text only narrows; constraints never rewrite YAML. Preference edits lapse Disabled-route exceptions.

`native_default` effort exists only in older records. Catalog limits do not prove effective context.

Keep raw failure source, stage and time on its attempt. Readiness timeout alone leaves cause unknown, not bad credentials. Honor native retry-after; otherwise temporary `unavailable` gets 60-second local reconsideration. Expiry starts nothing and proves no recovery. Validated success clears suppression, retaining history. Old failures/memory impose no permanent/family bans. Auth/rate-limit holds still need expiry or meaningful runtime/user change. Preserve permissions and safety. Replacements require settlement/no-start; reconcile uncertainty. `safety_refusal` bars same-Task rerouting. Keep unavailable pins. No probes, shared health cache, rotation or blind retry.

Use supported native dismissal for faster-model advisories. Informational warnings need no response.
