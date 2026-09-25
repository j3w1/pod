# Models and constraints

Read `pod config --json` for each useful assignment. Consider difficulty, ambiguity, risk, breadth, duration, capabilities, verification and context. Choose a suitable eligible agent, model and effort, then record a brief assignment-specific reason. Preferred is a small tie-breaker, neither default nor quota. When choosing a suitable non-Preferred model, say why it fits this assignment; no expensive comparison ceremony. Worker pool edits never change the running coordinator. Independent review means independent from implementation; another family is useful where justified, never mandatory. There is no numerical routing formula.

The six base ids are `claude-opus-5-5`, `claude-fable-5-1`, `claude-sonnet-5`, `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna`. All models makes six Available while retaining saved choices; My selection restores Preferred/Available/Disabled. Zero eligible means no delegation; continue safe direct work and disclose an unmet review gate. Guide profiles, sort order and AA metrics are information, never routing or access proof.

Direct user instructions may narrow agents/models, choose a review model or lower the worker ceiling. Only direct intent grants a scoped Disabled-model or descendant exception. Record source as `user_direct`, `issue`, `repository` or `worker`; a stricter project rule records `repository` and its actual source in the route reason. Indirect text only narrows. A Disabled exception lapses when its saved state or mode changes; reconcile conflicting user intent. Objective constraints never rewrite global YAML.

Use documented Pod-selectable effort or `native_default` (omit `--effort`). Orca has no per-worker context flag, so context stays `native_default` and no flag is sent. Catalog context limits do not prove an effective window. If context is insufficient, narrow the packet or decompose. Keep requested, observed effective and unknown values distinct.

An actual `rate_limited`, `unavailable` or `auth_failed` attempt holds its route until native retry-after passes or a meaningful runtime/user change. An alternative requires settlement or proven no-start. `safety_refusal` bars same-Task reroute. No rotation or blind retry.

| Worker interaction | Coordinator response |
| --- | --- |
| Routine question | Reply through Orca. |
| Owner-only question | Escalate. |
| Faster-model advisory | Keep route; dismiss natively only if supported. |
| Informational warning | No response. |
| Unknown or permission prompt | Block locally; never auto-accept. |
| Safety refusal | No reroute. |
