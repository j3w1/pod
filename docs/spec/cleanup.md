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
Snapshot, proof, holder, ref, archive and unknown observations all participate.

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
| `holders` | Status and current binding/path/role/liveness/dependency/source observations, reasons and witnesses. Different paths/branches alone do not prove outside dependency. |
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

The source-described positive primitive is the hardened direct local-ref deletion:

```text
git --no-optional-locks --no-replace-objects -c core.hooksPath=/dev/null -c maintenance.auto=false -c gc.auto=0 -C PROJECT update-ref --no-deref -d FULL_REF CURRENT_OID
```

Its current profile is conservative about packed storage, pending locks and the
observing HEAD's extra reflog effect. It clears no branch-config, worktree, cache,
remote or Orca action. Current holder observations always retain `holder_unavailable`; no production
stash-positive plan emits a removal command. Worktrees still become UNIQUE on any
repository stash and existing expanded worktree recovery requires every repository stash
root. Remote branches, including merged remote heads, also hold because their full
executor/footprint is unavailable. Post-worktree bookkeeping supplies no missing
creator/lifecycle/admission predicate, so the local-ref substitution has no
admissible production path. The controlled live-reader material/Git fixture is not
a positive production command or native removal. The separate reversible data below
does not replace this unavailable proof. Wider Orca/fallback/async/plugin/context/completion facts remain
unavailable. Verified empty ordinary plans retain their independent existing guards.

## Inspection and output bounds

Count, byte, content and time limits apply together. A legal count does not promise
that arbitrary names or combined material fit the byte budget. Ordinary plans have
at most 96 resource rows; input archives have at most 96 rows. The complete CLI
response and each stash observation remain bounded to 2,000,000 encoded bytes.
Regressions exercise the production entry with 96 rows and three stash roots, and
with complete 4096-entry maps whose names are 60 characters. Longer combined maps
that exceed the response budget still refuse; no field or distinct witness is
truncated, paginated or replaced with a caller assertion.

Surviving reachability, stash closure and restored archive coverage use one bounded
OID walker. Its separate traversal domain permits at most 100,000 total valid
records **and** 100,000 distinct objects, in fixed reads of at most 65,536 bytes.
The aggregate OID-output ceiling is 6,500,000 bytes for SHA256 and exactly 4,100,000
for SHA1. One absolute 30-second deadline covers format inspection and traversal;
chunks do not reset it. Duplicate, malformed, wrong-format, incomplete, missing-root,
over-count, over-byte, failed or expired observations hold. Arbitrary tree/blob
roots are compared with the complete closure; negative commit subtraction cannot
prove their coverage. Enclosing resource and archive freshness checks still hold
concurrent changes. The walker retains validated OIDs, never a raw-output spool.

Generic metadata input/output, including stderr, remains bounded to 2,000,000 bytes
per process. Complete type checks use bounded batches of at most 4096 objects under
those ordinary limits, sharing one absolute 30-second deadline for the entire type
operation. There is no separate typed-output stream or extra per-chunk time budget.
Traversal and restored-content object limits are independent domains. Git children
retain the existing nonraising address-space/file-size limits and service caps.

Each eligible resource HEAD or private reflog may contain at most 4096 complete
entries, independently of the 96 resource/ref count. Raw metadata bytes, distinct
required roots/objects, private ref and administrative-name counts, incarnation and
time limits still apply. Repeated entries do not create provenance. Unknown
`REBASE_HEAD`, `AUTO_MERGE` and other unsupported administrative roots still hold.
Selected reference-storage logs, including the stash retaining log, keep their
existing 2048-entry limit; 2049 entries hold. The stash snapshot limit remains
independent.

## Archives and consumer handoff

Branch-only rows are exactly `{path, ref, tip}` and prove only their original narrow
coverage. Expanded rows pair `sha256` and `recovery_refs`: the actual current source
ref/tip is distinct from restoration aliases. Verify the complete actual advertised
map, every target/type and required closure through self-contained restoration in an
empty isolated repository. Extra aliases may exist and all must verify, including a
stale source-name advertisement beside a correct current alias; none expands scope
or permission. Unsupported advertisement classes, thin/partial/corrupt/stale/missing
or swapped bundles hold. No production bundle rewrite or duplicate recovery registry
is required.

### Optional whole-worktree recovery data

Only expanded rows may additionally carry `worktree_archive: {path, sha256}`.
Both fields are strings: the path is normalized absolute text without NUL or parent
traversal, at most 4096 UTF-8 bytes; the digest is exactly 64 lowercase hexadecimal
characters. Extra keys, caller phases, verdicts, footprints and authority refuse.
Without this reference the wire shapes and Git-only root-coverage meanings remain
unchanged. The known-objective placement restriction below also applies to Git-only
objective-selected requests without a file recovery reference.

Matching Git resources gain exactly one `reversible_eligibility` record. Its schema
is `pod-reversible-eligibility/v1`, and its exact fields are:

| Field | Meaning |
| --- | --- |
| `schema`, `status`, `phases` | Status is `data_eligible`, `held` or `unavailable`, describing data only. Internally derived phases are drawn from `git_worktree_remove`, `git_local_ref_delete`, `git_tracking_ref_delete`, `git_remote_ref_delete`. |
| `archive_verified` | Combined Git/file/admin integrity and recovery coverage; separate from the unchanged existing Git-only row field, current freshness and authority. |
| `git_recovery` | Exactly `verified_covering`, `archive_sha256s`; complete declared and actual maps remain in existing archive observations. |
| `file_recovery` | Exactly `status`, `path`, `sha256`, `manifest_sha256`, `entries`, `bytes`, `restore_verified`, `current_match`, `reasons`. Status is `verified`, `held` or `unavailable`; actual current comparison is true, false or null. Unavailable digests/counts are null. |
| `admin_recovery` | Exactly `status`, `reasons`, using the same recovery status vocabulary. |
| `stash_noninterference` | Exactly `status`, `snapshot_sha256`, `support_sha256`, `footprint_sha256`, `reasons`. Status is `preserved_by_known_git_phases`, `overlap` or `unavailable`. |
| `executor_checks_required`, `reasons` | Required executor checks, never pass flags, and bounded reason codes. |

The new Git basis copies the tip, private roots and otherwise-unretained recovery
roots that the known phases destroy. Retained live stash roots remain fully observed
and fingerprinted, including roots retained only by the complete common stash log.
Their exact retaining/log storage identities, bytes and complete surviving closure
participate in reobservation; their presence creates no new copying obligation. Destroyed
private ref names still require recovery even when a surviving ref/log retains their bytes. Every extra
advertised recovery ref and type still verifies. Noninterference compares actual
current immutable stash material, retaining names/logs/object storage and physical
identities with the exact Git effects, in both containment directions. Missing,
changing, aliased or overlapping facts hold. It neither revises existing relations nor
claims complete future dependencies or earlier ownership. The direct-ref profile
still holds packed, pending-lock and observing-HEAD effects, and checks complete
current checkout absence. The remote-server effect profile remains unavailable.

The local-ref phase also destroys the selected common ref's own reflog. Require every
otherwise-lost nonzero old/new root from its complete current log, within the existing
2048-entry, raw-byte, required-root, type, closure and time limits. Preserve the exact
raw log bytes as an ordinary advertised Git blob in the existing complete recovery
map, without prescribing an alias name. Pod derives the literal blob OID using the
actual SHA1/SHA256 object format, restores the bundle in isolation and compares the
recovered blob bytes with the fresh bounded current log. It writes no source Git
object or ref. A source object, alias, digest or root list without actual restored
bytes is insufficient. Successful positive log absence needs no empty blob; unavailable,
changed, malformed, mismatched or over-bound observations hold. No transport field,
additional archive root or metadata-backup receipt substitutes for these bytes.
Both Git and file/admin recovery require complete known-phase reference material
and an actually observed selected-log fact, including positively observed absence.
Missing, malformed or incomplete reference evidence, including an unavailable
retained closure, cannot produce verified file/admin recovery or a verified restore.

The private file artifact is an **uncompressed POSIX pax tar**, with the first
regular member `pod-worktree-archive.json` and payload roots `worktree` and `admin`.
The manifest has exactly `schema`, `source`, `status_sha256`, `entries`; schema is
`pod-worktree-archive/v1`. Source has exactly `path`, `ref`, `tip`, `common_git_dir`,
`git_admin_dir`, `worktree_identity`, `admin_identity`. Identities use the existing
filesystem identity fields. A worktree request binds them to its actual selected
path/private admin, and every kind binds to the actual common repository. An equal
ref/tip in a sibling or foreign repository is insufficient. The status digest covers
exact hardened `status --porcelain=v1 -z --untracked-files=all --ignored` bytes.

Each entry has exactly `name`, `type`, `size`, `sha256`, `link`, `exec`, `source_stat`.
Stat has exactly `dev`, `ino`, `mode`, `nlink`, `size`, `mtime_ns`, `ctime_ns`, all
nonnegative integers. Names are normalized relative payload paths, without duplicate,
missing-parent or symlink-ancestor members. Types are file/directory/symlink/hardlink,
spelled `file`, `dir`, `symlink`, `hardlink`. Directories have zero size and null
digest/link; regular files have a byte digest and null link; symlinks hash the target
bytes. Only normalized relative symlinks without parent traversal and earlier regular
hardlink targets within the same prefix are supported. Hardlinks agree on data/stat,
and all current aliases must be included. Unsupported links hold.

The complete no-follow current walk must equal the manifest, including ignored and
untracked data, stat identities and all safe `0777` permission bits. `exec` must equal
`bool(source_stat.mode & 0o111)`. Validate every header/member before any extraction;
use the standard-library data filter in empty private scratch, restore validated
file/directory modes after extraction, and compare types, bytes, sizes, targets and
every permission bit. Symlink mode follows Linux semantics and is never chmodded
through. Special files, unsafe special mode bits, ACLs/xattrs, unknown/GNU extensions,
mounts, external aliases and unavailable metadata hold. The source and archive are
never chmodded. Artifact/file/ancestor inode, birth and mount identity, stable bytes,
private containing directory and placement outside removable worktree/admin/common
Git paths bind the existing material diagnostic and expect. Private manifest names,
raw status, config and log bytes never enter public output.

The admin is a distinct linked-worktree directory. Allowed roots are exactly `HEAD`,
`gitdir`, `commondir`, `index`, `config.worktree`, `logs`, `refs`, `ORIG_HEAD`,
`FETCH_HEAD`, `COMMIT_EDITMSG`; the first three and the regular worktree `.git` marker
are required and their actual control correspondence is checked. Capture every
existing allowlisted entry. Private refs are nonsymbolic files only in `refs/bisect/`,
`refs/worktree/`, `refs/rewritten/`; logs are `HEAD` or corresponding private namespaces,
including old logs with absent current refs. Intermediate directories must belong to
those namespaces. Physical enumeration must equal current Git logical/private-log
observations. Admin links, unknown roots/namespaces, locks and operation state hold.
Existing 96 admin-name/ref, 2,000,000 aggregate raw-metadata-byte, 4096 entries per
HEAD/private log and 1024 distinct-root limits apply together. No additional pseudoref
or operation root is admitted.

Raw `.git`, admin and configuration are recovered **as data** only. A separate empty
Git repository restores the verified bundle, regenerates linked-worktree registration
and index, restores workfiles without replacing those regenerated control pointers,
and compares recovered bytes/modes/status. Any nested worktree `.git` file, directory
or link is unsupported and rejected before extraction. Gitlinks in the recovery tip
are rejected before recovery worktree creation; an index containing gitlinks also
holds. The archived index remains raw data, never the recovery index. Recovery Git
disables submodule recursion and ignores submodules when comparing status. These
constraints do not suppress ordinary live dirty/submodule checks. No archived config is loaded or executed,
and no historic native or metadata identity is claimed restored.

Verified file/admin integrity is separate from the live source comparison and its
retained-material recheck. A current walk or recheck failure, including recursion
exhaustion, retains verified recovery, reports null `current_match` with
`file_current_unavailable` and holds eligibility. Archive validation, extraction and
restored-walk failures still invalidate integrity. Final
artifact identity, placement and byte checks still run; actual artifact tamper
invalidates integrity. Current unsupported links, mounts, metadata or file types
remain holds even when the immutable archive still verifies.

File verification has conjunctive ceilings: 128 MiB artifact, per-file and aggregate
payload bytes; 4096 payload entries; 2,000,000 manifest bytes; 4096 UTF-8 bytes per
path/link; and one absolute 30-second deadline covering opening, hashing, parsing,
extraction, recovery and current comparison. Existing physical/process, Git-object,
metadata and final-response caps still apply and are never raised. Exhaustion holds.

The executor must still perform exactly the named checks:
`authenticated_finite_owner_scope`, `current_inode_process_scan`,
`no_attached_orca_terminal`, `no_open_live_keep_reuse_role_default_or_shared_binding`,
`current_source_bytes_status_tip_identity`, `current_checkout_elsewhere`,
`remote_origin_lease_policy_open_pr`, `new_operation_archive_scope_phase_continuity`,
`native_public_absence_or_hold`. Pod supplies no privileged process scan, global
association registry, authority service, journal or execution API. Existing guards,
UNIQUE truth, unavailable history and null commands remain unchanged. Dirty, untracked,
staged and locked worktrees remain held; ignored data requires complete recovery.

Ordinary objective plans pass every selected worktree, private admin and common Git
directory exclusion to both artifact verifiers and their final placement rechecks.
An objective-selected observation retains all those known ordinary boundaries.
This includes a Git-only selection outside the ordinary resource list with no file
recovery reference: its bundle inside another known objective checkout holds; an
equivalent bundle outside the known set retains its narrow Git coverage. This is an
intentional conservative compatibility restriction of the earlier selected-only
placement domain. It changes no wire key, Git-only boolean meaning, root-copy policy,
classification, relation or command authority.
Project-only selection knows only the selected resource/common boundary. Neither
projection establishes the executor's whole finite removal scope: the executor must
verify both original artifacts outside every resource/admin/common path in that
authenticated finite scope before each effect.

After actual worktree absence, immutable recovery integrity may remain verified while
`current_match` is null. A new remaining-ref expect binds actual current ref/tip/storage
and archive facts, without proving any earlier phase. Only the executor validates its
ORIGINAL authenticated finite scope, unchanged artifacts and committed phases of this
new operation. Missing/unknown old phases cannot be replayed or backfilled. Local-ref
deletion requires complete fresh checkout exclusion plus atomic expected-OID
`update-ref --no-deref -d`; branch config is a separately backed-up phase or leftover.
The executor creates and freezes the original selected-log blob/root recovery and
file artifacts before any worktree phase, then rechecks current log identity/bytes
and remaining-ref expect at the branch boundary. Existing archives are not rewritten
by Pod, and absent old phases are never manufactured from a new artifact.
Remote origin/policy/open-PR/lease checks stay required. Observe public Orca absence
after known Git phases; a persistent entry is retained and reported held. There is
no proved metadata-only forget and no broad `orca worktree rm` or recreated-path recipe
for this route. Current closure/retirement observations and supported coordinator
reports remain the reconciliation interface.

Both the declared `recovery_refs` map and the complete actual `recovery_refs` map
remain in every archive observation. The optional repeated `recovery_types`
diagnostic is omitted to fit the combined byte budget; every advertised target and
required object type is still independently and completely verified.

Resource/archive-row count remains 96; advertised/expected recovery maps are bounded
to 4096 entries. Compressed input remains 128 MiB, with separate bounded logical
restoration/object/count controls and the conjunctive inspection/output bounds above.
Git inspection never raises tighter inherited soft/hard process limits.
Archives must remain outside removable resource/admin paths with stable bytes,
identity and mount placement. Git artifact and ancestor descriptor mount IDs, plus
relevant placement mount/parent IDs, bind the existing diagnostic and expect alongside
physical coordinates, no-follow aliases and birth identity. Unavailable mount
incarnation holds; an ID-only change invalidates old expect. Volatile-media crash durability is not established by
the outside-removal check. A verified Git archive preserves Git data; it grants no integration,
unique-work discard, release, live-work clearance or uncommitted-file preservation.

An existing engine independently rechecks its actual **original** admitted
scope/archive/resource/ref identity, committed phase outcomes and continuity, then
fresh current facts and its full wider operation before each mutation, including
fetch/fallback/async effects. It retains its existing journal; Pod adds none. An
absent path, same name/OID, matched intent or new expect cannot supply missing
originals or successful removal/release. Old runs missing indispensable facts stay
held. Project-only protected/null output is data, never deletion clearance.
