# Cleanup

Before cleanup, run `pod sweep --repository PATH --json` for the repository. Repeat `--repository PATH` for other repositories the host registers; Pod has no host registry and otherwise defaults to the current repository. Optional repeated `--objective NAME` narrows the sweep. To inspect proof without writing, use `pod status --list --delivered --json`; the plain list reads records only.

The sweep closes every proved delivered objective whose coordinator is gone, tails before predecessors, each with its own immutable evidence closure. It reports `closed`, `already_closed`, `not_proved`, `unverified` or `skipped` with conditions; a repeated run writes nothing. Unproved lost work stays open for the project lead, never an owner question. A completed sweep exits zero whatever it found; invocation or scope errors exit nonzero. Read the result before the cleanup plan. Closure changes only Pod's reservation; it grants no deletion authority.

`internal cleanup-plan` is read-only and objective-scoped: it shows integrated, merged remote head, unique/protected resources, dirty data, stashes, other worktrees, refs and terminals.

Obtain one batched confirmation for the exact deletion classes; an applicable exact-scope instruction counts. Retain unique work, verify an archive bundle before deleting refs, or obtain separate discard consent.

While yolo is active, the cleanup plan's `standing_authorization` fields `covers` and `asks` are the applicable exact-scope instruction. Covered classes need no further owner confirmation; asks still require it. Use the fresh plan for the actual resources, preserving the same guards below.

Rerun with `expect` immediately before deletion; changed facts stop cleanup. Execute only listed guarded Orca/Git commands with exact ref tips and remote leases. Never add `--force` or delete a directory directly. Protected/uncertain resources stay. Native settlement and Delivery acknowledgment follow Orca; worker release follows the explicit coordinator step in the worker-tab guidance.
