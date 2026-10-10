# Failure, recovery and continuation

On resume read native state first, retaining uncertainty and next safe action. Continue accepted work within scope/authority; restore relevant lost guidance.

For `preference_changed` and `preference_revision_stale`: on preference races, rechoose at most twice, then report the conflict. Once a native start is submitted, retain its route and recover that exact request. No retry counter is added.

Failed, refused or unknown starts require Orca request/readback before replacement. Effect-free native refusals defer; uncertain effects remain unresolved. Preserve immutable response evidence and Orca's UUID; missing storage grants no authority. Follow the helper's diagnosis rather than blind replay.

Terminal reuse requires settled prior attempt and known terminal, copies its effective route, and rechecks eligibility/pin; native `worker-start --terminal` has no model/effort flag. Reuse requires an immediate supported follow-up.

Keep raw failure source, stage and time. Readiness timeout leaves cause unknown. Honor native retry-after; otherwise temporary unavailability gets 60-second local reconsideration. Expiry starts nothing and proves no recovery. Validated success clears suppression while retaining history. Past failures impose no permanent/family ban; auth/rate limits need expiry or meaningful runtime/user change. Preserve unavailable pins; no probes, shared health cache, rotation or blind retry. Actual failures require settlement/no-start before another route. Keep selected models after faster-model advisories; use supported dismissal, informational warnings need no response.

`installed_version_changed` requires skill reload, fresh config read and authorized Pod-stamped checkpoint before new admission/Governor mutation; recovery is exempt.

For a recognized Codex software-update menu, confirm exact live native `terminal read --screen` readback and its displayed **Skip for now** option/control. Select that proven option once through supported `terminal send`, verify readiness, then resume normal admission/recovery at the same model/effort. If unproven, keep the blocker. No software installation, other prompt automation, provider/settings/permission/safety changes, duplicate pending requests or retry/controller loop. Missing live opt-out proof stays NOT_RUN.

Governor failures use `governor-classify`: code defect returns to correction, remote-only names bounded discriminating check/stop, transient follows bounded policy retry, external names dependency; unclassified failures defer.
