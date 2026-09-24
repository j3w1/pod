# Models and constraints

The coordinator reads `pod config --json` and chooses for each useful assignment.
Consider reasoning difficulty, ambiguity, risk, breadth, expected duration,
required capabilities, verification and available context. Choose a sufficient,
proportionate model and effort, then record a short reason. Preferred is a small
tie-breaker among otherwise suitable models. A reviewer is not automatically a
flagship assignment; another model family can be useful for independent review.
There is no complexity table, numerical formula or rank-driven selection.

The supported base ids are Claude `claude-opus-5-5`, `claude-fable-5-1`,
`claude-sonnet-5`, and Codex `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna`.
One eligible model means use that one for delegation, varying supported effort
as needed. Three means choose within those three. Zero means no delegation;
continue safe coordinator work and disclose any unmet independent-review gate.
All models mode explicitly makes the six Available while retaining saved choices;
My selection uses the saved Preferred/Available/Disabled map. Table sorting,
Artificial Analysis metrics and catalog examples never authorize or dispatch.

A direct user's natural-language instruction may narrow agents/models, exclude a
model, select a review model or lower the worker ceiling. Only direct intent may
make a scoped exception for a named Disabled model or allow descendant
workers. Record provenance: `user_direct`, `issue`, `repository` or `worker`.
Indirect sources can only narrow. The exception lapses if that model's saved
state or selection mode changes; reconcile contradictory user intent. Never
rewrite global YAML for an objective constraint.

Use only documented, Pod-selectable effort values; `native_default` omits
`--effort`. Orca currently exposes no per-worker context flag, so context is
`native_default` and no flag is sent. A documented ceiling or benchmark context
column does not prove an effective window. If actual context is insufficient,
narrow the packet or decompose it. Keep requested, observed effective and unknown
values distinct.

An actual `rate_limited`, `unavailable` or `auth_failed` failure holds the same
route until native retry-after passes or a meaningful runtime/user change clears
it. Another eligible route is considered only after settlement or proven
no-start. A `safety_refusal` bars a same-Task reroute. There is no scheduler,
rotation or blind retry loop.

| Worker interaction | Coordinator response |
| --- | --- |
| Routine question | Answer through `orca orchestration reply`. |
| Owner-only question | Escalate. |
| Faster-model advisory | Keep the route; dismiss natively only if supported. |
| Informational warning | No response. |
| Unknown or permission prompt | Local blocker; never auto-accept. |
| Safety refusal | No reroute. |
