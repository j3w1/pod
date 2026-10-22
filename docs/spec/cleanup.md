# Cleanup observation records

This is the detailed record contract for [R94](delivery.md#r94--objective-cleanup-plan-and-consent).
`internal cleanup-plan` observes facts and describes guarded commands. It executes no
cleanup, records no lifecycle event and grants no permission.

## Requests

Ordinary requests require `project` and `objective`, with optional `worker`, `archives`
and `expect` as R94 defines. `project` is the checkout path, not a project slug.

Optional `observe: {kind, name}` selects one current resource. Both fields are bounded
nonempty strings; no extra selector keys are accepted. The kinds are:

| Kind | Exact name |
| --- | --- |
| `worktree` | Normalized absolute checkout path, without parent traversal. |
| `local_branch` | Full `refs/heads/...` name, never a revision expression. |
| `remote_branch` | Exact `remote/branch` name. |
| `cache` | Normalized absolute directory path, without parent traversal. |
| `native` | Exact Dispatch id from the selected objective's admission. |

A worker selection cannot expand to another placement, ref or Dispatch. The selector
supplies no relatedness, footprint, original identity, previous action or authority.
`phase`, `profile`, `relation`, `footprint`, `required_roots` and authority assertions
are not inputs.

Only a selected request may omit `objective`. Supplied null or empty objectives are
invalid, and an omitted objective cannot be combined with `worker`. This project-only
mode observes current project/Git/material facts without reading a Pod objective or
guessing an association. It returns unavailable provenance, no lifecycle authority
and null deletion, archival and release commands. It does not establish that no Pod
association exists elsewhere.

## Selected plan and fingerprints

Success retains the `pod-cli/v5` outer `status: ok` and `result`. A blocked/nonzero
response is not an empty successful observation. The selected result is
`pod-cleanup-plan/v1` with exactly one resource, the existing target, notes,
standing-authorization projection, digest and expect, plus:

```json
{"observation":{"schema":"pod-cleanup-observation/v1","selector":{"kind":"local_branch","name":"refs/heads/example"},"read_only":true}}
```

Each resource retains `id`, `kind`, `class`, `reasons`, `identity`, `delete_argv`,
`archive_argv`, `archive_verified`, `archives` and `fingerprint`, and includes
`stash_observation`. Classes remain `integrated`, `merged_remote_head`, `unique` and
`protected`; there is no archived/safe/absent class. Known UNIQUE work stays UNIQUE
when an independent hold prevents removal. Top-level `archive_verified` requires
verified **and covering** evidence; a narrower valid per-archive proof is separate.

Every selected identity includes these internally computed records:

| Record | Exact fields and meaning |
| --- | --- |
| `observation_scope` | `mode`, `objective`. Mode is `objective` with the validated supplied name, or `project_only` with null. This is context, not permission. |
| `provenance` | `status`, `source`, `reference`. Status is `observed` or `unavailable`; source is `objective_checkpoint`, `admission`, `governor` or null. Reference is the exact existing objective, admission or Governor unit identifier respectively. It identifies selection bookkeeping, not creation before this observation or ownership. A native selector/identity names a Dispatch; its admission provenance reference is an admission id, not that Dispatch id. |
| `current` | `status`, `path`, `ref`, `oid`. Status is `present`, `absent` or `unavailable`; values are actual current facts or null. Successful absence is distinct from a failed read. |

Worktree identities additionally carry path/branch/tip, terminal/shared-checkout
facts, Git material and optional worker facts. Local branches carry ref/tip,
checked-out paths, Git/worker facts and `ref_storage`. Remote branches carry
remote/ref/tip and Git/worker facts. Cache identities carry path/filesystem identity;
native identities carry Dispatch and sanitized native/worker observations. Unknown
values remain null. Existing Git/native/worker/archive diagnostic internals are
bounded opaque facts for consumers: retain and fingerprint them, but never use an
unrecognized diagnostic field to replace a missing structured proof.

`identity.git` is a compact opaque diagnostic: `sha256`, `complete`, `kind` and
sanitized `reasons`. Its SHA256 uses the same canonical JSON encoding as the
fingerprint, over the **complete actual internal material observation**, including
nested repository, ref, root, commit, metadata, storage and unavailable facts.
Decisions, archive verification and reobservation still use those full facts.
Both ordinary and selected responses compact this diagnostic before the final
whole-resource fingerprint. A hidden material change therefore invalidates
expect; diagnostic completeness or a digest never supplies a structured proof.
All structured identity, current, provenance, ref-storage and stash records remain
complete, including every distinct relation witness and its basis.

Project-only results have `objective: null`, `record_class: unavailable`, no closure
basis or top-level worker, null target ref/commit and no observed delivered movement.
Their standing projection is normal, null decision, no covered resource and the
selected id in asks. No actual global/default grant is inferred.

The existing fingerprint hashes the whole resource except its fingerprint field,
using SHA256 of UTF-8 JSON with sorted keys, compact separators and unescaped Unicode.
The plan digest hashes the sorted id/fingerprint pairs and target. `expect.plan`
equals the digest and `expect.resources` contains the exact id/fingerprint map.
Selected `identity.observation_scope` therefore binds mode and objective name even
when all other protected facts are identical. Top-level context alone is not hashed.
Snapshot, proof, holder, ref, archive, completion and unknown observations all participate.

Recheck immediately before an effect. Changed facts refuse `cleanup_changed`. After
a worktree disappears, derive a **new current remaining-resource** expect; the old
whole-worktree expect cannot establish prior removal, release or continuity.

## Logical refs and physical storage

A local branch's `identity.ref_storage` has schema `pod-ref-storage/v1` and exact
fields `schema`, `status`, `backend`, `ref`, `current`, `loose`, `packed`, `reflog`,
`freshness`, `reasons`. The supported backend is files; unsupported or uncertain
storage holds. `current` is a logical Ref with `name`, `status`, `oid`, `object_type`,
`symref`, `resolution`. A name/OID alone is not physical continuity.

Each storage slot has `path`, `status`, `identity`, `ancestors`, `sha256`. Observe
both loose and packed representations and the selected ref's log, including a loose
ref shadowing an older packed value. Packed-file presence alone is not selected-ref
presence. Reads are bounded and no-follow; duplicate/malformed packed entries,
redirects and incomplete symbolic resolution hold. An absent slot needs a successful
absence observation and its existing parent chain. A missing loose file is not an
absent logical ref.

Identity records contain `path`, `device`, `inode`, `mode`, `size`, `mtime_ns`,
`ctime_ns`, `mount_id`, `incarnation`. The final two may be null when unavailable;
missing indispensable facts hold. Directory birth time is actually read with Linux
statx, not synthesized from an OID, name or digest. File identity and containing
directory incarnation detect replacements, including equal-byte ancestor recreation.
Caller-side mount/birth facts alone prove neither another executor's namespace nor
identity before this observation. For local branches, the stash record's resource path/identity
describe the observing project; the physical ref fact is `identity.ref_storage`.

## Stash observation

`stash_observation` uses schema `pod-stash-observation/v1` and exact top-level fields
`schema`, `complete`, `snapshot`, `reflog`, `resource`, `executor`, `footprint`,
`support`, `holders`, `freshness`, `relations`.

| Record | Contents |
| --- | --- |
| `snapshot` | Unique immutable `oid`, `tree`, `parents`, `parent_trees` rows. Root/parent/tree types and supported index/untracked shape are checked. |
| `reflog` | `status`, logical `ref`, ordered repeated new-OID `entries`, `ref_identity`, `log_identity`, raw-byte `sha256`, `reasons`, `witnesses`. Snapshot/support also retain the current ref and every nonzero old/new log root. |
| `resource` | Current status/path/ref/tip/HEAD, identity/private/common dirs, applicable local refs, origin digest, reasons and witnesses. |
| `executor` | Status/kind/runtime/version/profile, actual artifact/source-binding digests, reasons and witnesses. A version label alone is not implementation correspondence. |
| `footprint` | Status and typed path/ref/config/native effects, opaque effects, digest, reasons and witnesses. It covers the entire recognized operation. |
| `support` | Status/object format, object dirs/alternates, retaining refs/log/context/config digests, complete required closure, unsupported facts, reasons and witnesses. |
| `holders` | Status and current binding/path/role/liveness/dependency/source observations for the effect's selected resource and actual overlaps, reasons and witnesses, including an exclusion witness for every other registered worktree. Different paths/branches alone do not prove outside dependency. |
| `freshness` | Before/after digests, stable boolean and reasons. Equal hashes of unavailable values do not establish complete stable proof. |
| `relations` | Exactly one `oid`, `relation`, `basis` row per unique snapshot OID. Relation is `related`, `unrelated` or `unknown`; basis has `kind`, `reasons`, `witnesses`. |

Witnesses contain only kind/operation/status/digest/reference, never authority or raw
private content. Raw object payloads, environment/config values, authors and subjects
are not exported or spooled. Closure evidence contains roots/count/digest/integrity.
Inspection budgets, errors, unsupported schemas/reasons/profiles, missing fields and
concurrent change remain incomplete/unknown; truncation never means success.

Verified empty needs successful direct retaining-ref **and** reflog absence. A
present `refs/stash` with a missing log is unknown. `complete` means inspection
completeness, not admissibility. Related requires a proved removal dependency;
unrelated requires complete stable material, identity, storage, full-footprint and
current-holder exclusion for the exact primitive. Shared base/history, branch or
display labels, archive validity and complete map keys establish none of that.

The recognized local-ref primitive is internally constructed:

```text
git --no-optional-locks --no-replace-objects -c core.hooksPath=/dev/null -c maintenance.auto=false -c gc.auto=0 -C PROJECT update-ref --no-deref -d FULL_REF CURRENT_OID
```

Its `git-local-ref-delete/v1` profile requires a loose, non-symbolic selected ref.
A `packed-refs` file is not a packed entry: when the file exists, its parsed content
must positively lack the selected name, its identity must be reliable and no pending
packed or loose lock may exist. Git then only takes and releases the packed lock; the
file's bytes and identity stay bound through ref storage, freshness and `expect`.
A packed or shadowed selected entry, a pending lock or an unparsable file holds;
nothing rewrites or repacks storage. The observing HEAD's additional reflog effect
is not profiled. Every checkout or symbolic/private alias of the deleted name in the
complete Git catalog below remains a dependency, even when the stash is verified empty.

Holder facts are scoped to the exact effect, never to the whole repository. A
complete Git-only catalog of every registered worktree is read under the shared
30-second deadline: matching common/object directories, reliable checkout and
administrative identities, HEAD binding (a detached HEAD is a supported
non-symbolic binding), and private and symbolic refs. A repeated, bare, prunable or
unreadable row, a catalog error or an exhausted bound holds. A worktree is relevant
when its checkout or ref chain names a deleted ref, or when the footprint reaches its
working files or administrative tree by lexical or mount-coordinate containment;
physical overlap holds. Native terminal reads happen only for the selected resource
and name-overlapping worktrees, within the existing 32-read bound; any terminal there
depends on the effect and unreadable or unverifiable facts are unknown. Every other
worktree is excluded by these complete facts and appears only in an exclusion
witness. Its terminals are not read: a coordinator or exited terminal, a detached
checkout or the number of irrelevant worktrees creates no veto, and no process
absence, exit, release or ownership is claimed for it.

The worktree profile `git-worktree-remove/v1` recognizes two internally built forms
with the same hardened Git prefix, which adds `-c core.fsmonitor=false` immediately
after `-c core.hooksPath=/dev/null`, so no configured fsmonitor program starts:

```text
git --no-optional-locks --no-replace-objects -c core.hooksPath=/dev/null -c core.fsmonitor=false -c maintenance.auto=false -c gc.auto=0 -C PROJECT worktree remove [--force] EXACT_PATH
```

The unforced form runs Git's clean check (`git status`) in the selected checkout, which
can start an attribute filter driver's external helper for a tracked path. Before
returning that command, Pod reads the selected checkout's current configured driver
names (`filter.<driver>.clean`, `.smudge` or `.process`) and, when any exist, the
`filter` attribute of every tracked path, within the generic output bound, 4096-path
query batches and one 30-second deadline. A driver that a tracked path selects holds
the effect with the exact driver names; unreadable, malformed or over-budget facts hold
with their reason. A configured driver that no tracked path selects, or an attribute
naming an undefined driver, is inactive and holds nothing. The facts bind `expect` and
are read again before the response; a change refuses `cleanup_changed`. No helper is
run to test it. The forced completed form and the local-ref form run no clean check or
conversion, so attribute helpers are outside their path. Pod's general status
inspection is unchanged: it can itself run a configured helper for a stat-dirty tracked
path. That pre-existing advisory is not covered or fixed by this guard.
A clean integrated exact worker, with its recorded
report, fresh exact native settlement, positive release, exited liveness and empty
current attachment inventory, uses `worktree remove EXACT_PATH` without force.
Its native worktree/runtime must match the admission, and report/release/placement
facts are rechecked through the observation. Unrelated stashes impose no extra hold;
dirty, unmerged or otherwise unique ordinary work receives no such command.
The completed-task default uses `worktree remove --force EXACT_PATH`. One force
permits completed-task files; locked worktrees never receive a second force.
Its footprint is the exact linked checkout/private administrative directory and
qualified private refs, excluding common objects and stash retaining ref/log slots
in both containment directions and through current physical coordinates. Common
source branches survive this phase. Submodule, unsupported administrative, aliased,
missing or changing effects hold, including with a verified empty stash. This Git
phase leaves the common branch intact; fresh ordinary/worker observations after
checkout absence retain their existing association holds. An open integration,
reservation, reused or concurrently held placement, live/attached/role resource,
default/target/shared branch or changed current identity remains protected.
Other ordinary Orca worktree removal remains opaque; neither release nor
completion can make that effect known or make a stash unrelated. Remote effects
with unproved full footprints stay unknown when a stash is present.

Roots retained only by the complete common stash log are not a linked checkout's
otherwise-lost roots: that log lies outside the checkout's administrative tree, and an
effect relies on it only through the stash record's exact storage exclusion. Private
names and HEAD-log roots outside every surviving retention remain additional roots.

Complete terminal inventories and Git reference observations are bounded and fresh.
A successful terminal-less inventory supplies no arbitrary process dependency claim;
CE's current privileged process and association/exclusion guards still precede every
effect. No producer observation passes those checks for the executor.

## Completion and execution handoff

R94's default uses validated coordinator closure, current quiet supported public
status and actual live source agreement with every current mapped/proven PoD.
No checkbox-only, empty or missing tick set, retirement, continuation or evidence
closure supplies this condition. The complete internal material includes the
observed record/status/source/completion facts before its opaque Git digest is
computed, and a second read checks the condition in the same call. A stale `expect`
therefore cannot cross a completion or source change.

The exact task association comes from current supported checkpoint placement,
admission placement and actual Governor publications. A missing checkout's null
branch cannot associate a remaining ref. For the completed-only default, an exact
unambiguous current checkpoint/admission placement branch binding can select its
remaining local ref after positive filesystem/Git absence of that recorded checkout.
The same path cannot have conflicting branches, and multiple recorded paths cannot
supply one unambiguous binding. The recorded branch and source/reference are current
selection data, never creation before this observation, ownership or a completed prior phase.

A fresh remaining row retains its existing current/provenance/ref-storage/stash/expect
shapes and UNIQUE truth, without archives. Its hardened no-deref exact-OID command
requires complete current checkout absence, target/default exclusion, stable logical
and physical ref storage, a quiet exact native entry and the actual known Git
primitive, including when the stash is verified empty. Native branch null is not the
association source; unknown, contradictory, attached or role facts hold. Current
association/native/storage facts, including held observations, bind the opaque
material/expect and are rechecked. A newly attached checkout or changed current fact
refuses the stale observation. An unavailable native read is never quiet absence.
CE consumes only the fixed-template eligibility, never arbitrary returned argv.

CE alone retains its current new-operation finite scope and actual phase outcomes, checks
current process/attachment/reservation/role/default/shared facts and performs known
Git phases. Complete checkout absence and atomic expected-OID no-deref deletion
remain required. Remote origin, policy, open-PR and lease checks remain required.
New facts, matching names/OIDs or absence never repair unknown earlier effects.

Pod has no supported ordinary scratch inventory or cache/native removal primitive.
CE uses its existing current registered task/scratch/native association or holds the
missing fact. After the Git worktree/ref phases, inspect public Orca absence or
retain a persistent entry. There is no metadata-only forget, broad Orca fallback,
raw directory removal or recreated-path recipe. No proof API, registry, certificate,
journal or caller completion/provenance flag is added.

## Inspection and output bounds

Count, byte and time limits apply together. Ordinary plans have at most 96 resource
rows and archive input has at most 96 rows. The whole CLI response and each complete
stash observation remain bounded to 2,000,000 encoded bytes. Count-legal combined
overflow refuses; structured fields and distinct witnesses are never truncated.
Compaction replaces only the opaque Git diagnostic with a canonical digest of all
actual internal material. Current unavailable facts participate in that digest too.

Complete Git worktree inventory is streamed under its existing contract, retaining
all checkouts of the selected refs without imposing a new repository-size limit.
Native worker pages retain exact Run/runtime scope and require explicit boolean
continuation, nonempty advancing pages, unique Dispatch identities and bounded
cursors; at most 100 pages of 100 rows are consumed. A missing/interrupted/duplicate,
changed or truncated page holds. Display retains its independent 1000-row complete
worktree-list/host-scope contract. No native pagination capability is invented.

OID closure traversal permits at most 100,000 valid records and distinct objects,
in fixed reads of at most 65,536 bytes. OID-output ceilings stay 6,500,000 bytes for
SHA256 and 4,100,000 for SHA1. One absolute 30-second deadline covers format and
traversal. Duplicates, malformed/wrong-format/incomplete records, missing roots,
count/byte overflow, failure, change or expiration hold. Arbitrary tree/blob roots
are checked against the complete surviving closure, not negative commit subtraction.
The reader retains validated OIDs and never a raw-output spool.

Generic metadata input/output, including stderr, remains 2,000,000 bytes per process.
Complete type queries use batches of at most 4096 under one absolute 30-second
deadline. Git children never raise tighter inherited address-space/file-size limits
or service caps. Current workfile freshness reads only Git's complete bounded
changed/ignored/untracked list, with at most 4096 names, 4096 UTF-8 bytes per path,
2,000,000 bytes per file read and 128 MiB aggregate content under 30 seconds.
Unsupported/redirected file facts hold. Only digests/identity enter the opaque
material diagnostic; no file capture or restoration artifact is created.

Selected ref-storage logs, including the stash retaining log, keep their existing
2048-entry limit; 2049 holds. Ordinary private resource metadata keeps its existing
ref/name/root/raw-byte/time limits and its independent 4096-entry per-log ceiling.
These current readers establish no earlier worktree creator, release or transfer.
Unknown operation roots such as `REBASE_HEAD` and `AUTO_MERGE` remain unsupported.

## Ordinary branch preservation

Ordinary `archives` rows remain exactly `{path, ref, tip}`. Their verified-and-covering
Git meaning stays separate from UNIQUE truth, authority and lifecycle guards.
A branch-only proof does not preserve uncommitted files or otherwise-lost private
names/roots. It supplies no stash provenance. Complete actual advertisements and
required types are checked through self-contained restoration in an empty isolated
repository; source objects cannot fill a thin/incomplete artifact. Corrupt, stale,
missing, swapped or unsupported artifacts hold. Extra advertisements grant no wider
selection or permission. Expanded recovery-map/file references are unsupported.

The existing 128 MiB input, 4096 advertised-ref, restored object/count/byte and
process limits stay conjunctive. Stable no-follow bytes/identities and placement
outside every known objective checkout/private/common directory bind verification
and expect. Project-only placement knows only its selected/common boundary; an
executor still checks its entire authenticated finite scope. A valid narrower
per-archive observation never becomes covering evidence by a boolean assertion.
