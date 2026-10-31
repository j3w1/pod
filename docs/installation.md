# Installing Pod on Linux

Pod's one-shot installer downloads the current `main` source, validates the `skills/pod` bundle, installs it through the pinned skills CLI for Codex and Claude Code, and places a user-local `pod` launcher. Installation does not test account sign-in, connect Orca, start workers, call a model, or edit an agent's settings.

## Prerequisites

- Linux and a non-root user.
- The latest stable CPython as `python3` on PATH, with `venv`, `ensurepip`, and curses. The installer uses that `python3` and refuses a prerelease or a missing module; it never falls back to another interpreter. On Debian-derived systems the matching `venv` package may be needed. Provision the interpreter through the host's approved runtime provisioning, not through the installer.
- PyYAML is installed from a prebuilt wheel. Only when pip finds no wheel for the running interpreter (a new CPython often precedes its wheels), and only for the exact pinned `PyYAML==6.0.3`, the installer builds PyPI's source archive `pyyaml-6.0.3.tar.gz`, accepted solely by its pinned SHA-256; a hash mismatch or a failed build refuses. That build downloads its build tools from PyPI.
- Node and `npx`, plus curl or wget.
- Network access to `raw.githubusercontent.com` for the bootstrap script, `codeload.github.com` for source, PyPI for the pinned PyYAML wheel, and npm for `skills@1.7.0`.
- Orca and an authenticated Codex or Claude Code session for live delegation. Installation itself does not require them to be running.

## One command or inspect first

The normal command is:

```sh
curl -fsSL https://raw.githubusercontent.com/j3w1/pod/main/install.sh | sh
```

To read the script before running it:

```sh
curl -fsSL https://raw.githubusercontent.com/j3w1/pod/main/install.sh -o pod-install.sh
less pod-install.sh
sh pod-install.sh
```

The script refuses root and non-Linux systems. It downloads one tarball, rejects unsafe archive paths, then runs the staged stdlib installer. The installer checks prerequisites and ownership before changing Pod-owned paths, creates a keyed venv with the PyYAML pin declared in `skills/pod/installer.py`, validates the staged skill/catalog, and writes an `installing` receipt before the skills CLI copies the canonical skill. It verifies the copy, Claude link and launcher, creates default preferences only when absent, handles PATH, and writes `installed` last. An incomplete copy blocks the launcher until a rerun repairs it. Output is plain in pipes and logs; a terminal also gets one small ASCII orca-pod banner.

## Paths and ownership

| Path | Purpose |
| --- | --- |
| `~/.agents/skills/pod` | Canonical skills-CLI copy used by Codex. |
| `${CLAUDE_CONFIG_DIR:-~/.claude}/skills/pod` | Relative link to the canonical copy for Claude Code. |
| `${XDG_DATA_HOME:-~/.local/share}/pod/venv-*` | Isolated, keyed PyYAML environment with a Pod marker. |
| `${XDG_DATA_HOME:-~/.local/share}/pod/install.json` | Version and digest receipt; `installed` is written last. |
| `${XDG_DATA_HOME:-~/.local/share}/pod/preserved/skill-*` | A changed canonical copy saved before replacement. |
| `~/.local/bin/pod` | Owned shell launcher into the one installed bundle. |
| `${XDG_CONFIG_HOME:-~/.config}/pod/config.yaml` | Personal `pod/v2` route preferences, Preferred and Pin, worker ceiling and refresh setting; written only when absent, never rewritten by the installer. |
| `${XDG_CACHE_HOME:-~/.cache}/pod/models/` | Cached public model observations: `current.json`, `previous.json`, `refresh.json` and a lock. Disposable. |

The installer may append one guarded PATH block to `.zshrc`, `.bashrc` and a Bash login file, or `.profile`, according to `$SHELL`. It never follows a symlinked rc file. It prints a `fish_add_path` instruction for fish instead of editing fish configuration. Reopen the shell when it adds a block; a child installer cannot change its parent shell. A different `pod` earlier on PATH and any duplicate skill copy are reported for inspection, never removed automatically. `CODEX_HOME` is not a second installation destination.

## Updating and recovery

`pod update` downloads `main`, runs the same staged checks, and returns “Already current” when the receipt, launcher and copy match. A change to the canonical skill is preserved under `preserved/` before the skills CLI replaces it. Running native workers remain untouched; reload active coordinator conversations before new starts.

Status and doctor list objective state in an older schema, block only that objective and never convert it; existing native workers remain Orca-owned.

If a download, dependency install, or copy is interrupted, rerun the one-shot installer or `pod update`. A receipt still marked `installing` records the previous and target digests. A bundle with neither digest yields `install_incomplete` instead of false readiness. `pod doctor --json` reports the receipt, canonical digest, venv, launcher ownership, placements, duplicates, and relevant next action.

An invalid preference file is kept untouched, with no eligible routes; use `pod config edit` to correct it. A 0.6.x `pod/v1` file is also kept byte for byte and reported as needing route setup: until you confirm the setup, no route is eligible for new delegation, while diagnosis and recovery of existing native work continue. Open `pod` in a terminal to review the proposed exact routes, the notes and the choices you still have to make, such as an exact effort for an earlier pin; saving keeps the original as `config.yaml.pod-v1` next to the new file. The setup never infers a Preferred route, invents a pin effort or carries a choice over to a replacement model generation; Sonnet 5.5 and GPT-6.1 Sol are not enabled from earlier Sonnet 5 or GPT-6 Sol choices. `pod config edit` remains the manual path. The installer never overwrites a foreign `~/.local/bin/pod` or a redirected rc file. If `pod` resolves to another command, inspect `command -v pod` and your PATH order before removing anything.

## Removal

Run `pod doctor --json` first to identify owned paths. Remove the Pod skill through your skills manager or remove only the canonical copy and Claude link identified above; inspect any reported duplicate before touching it. Remove the launcher only when its content matches the Pod-owned template. Remove the `pod` data directory for the venv, receipt, lock, and preserved copies only after deciding whether to keep those backups. Remove only the guarded PATH block between `# >>> pod path >>>` and `# <<< pod path <<<` from files that contain it. Keep or remove the personal config, and any `config.yaml.pod-v1` copy, separately according to whether you want to retain your choices. The model observation cache under `${XDG_CACHE_HOME:-~/.cache}/pod/models/` holds only public data and can be removed at any time. Do not delete an unrelated executable, another skill copy, or agent application settings.

## Model data and refresh

`pod models` shows shipped routes and newly discovered or unsupported Anthropic and OpenAI observations by default. Press `o` to cycle discovery filters, including supported routes only; `F` clears filters back to the inclusive view. Press `R` to fetch public data now; the result reports AA rows added, changed and removed. Observations stay read-only until Pod ships verified routes. Claude Haiku 5.5 is supported at all five efforts; an existing installation leaves its new routes not set until you explicitly enable them. Update Pod and reload your coordinators before enabling newly supported routes; an older reader rejects unsupported route keys. With the default `refresh: automatic`, opening the workspace in a terminal starts one bounded background refresh when there is no local observation cache or the cached data is at least 24 hours old. It reads three fixed public HTTPS pages: the Artificial Analysis leaderboard at `artificialanalysis.ai` (required), Anthropic's models overview at `platform.claude.com` and OpenAI's models page at `developers.openai.com`. The whole refresh is bounded to about 20 seconds. It honors a server's Retry-After, waits six hours after a failed attempt (ten minutes after a cancelled one), and writes only the observation cache; it never writes preferences, starts workers or calls a model. The workspace renders cached data first and shows data at least seven days old as stale. Set `refresh: manual` in the personal file to disable automatic network access; `pod models refresh` then remains an explicit command, and `pod models refresh --check` validates and compares without saving. `pod config`, `pod status`, `pod doctor` and worker admission never fetch. Without a cache Pod uses the small bundled snapshot.

## Registry and observation maintenance

The supported models live in the `pod-catalog/v3` registry `skills/pod/catalog.json`: exact ids, verified efforts, explicit per-source aliases, attributed provider guidance of 20–70 words, provider sources with checked dates, guide profiles and documented context ceilings. It holds no benchmark measurements. Verify an id and its efforts against the provider page and the installed adapter's help or bundled catalog before editing; adding an ordinary model is a registry edit. Run `PYTHONPATH=skills python -m pod.catalog --check`, the focused registry, route and workspace tests, visual snapshots and the repository gates.

The bundled fallback `skills/pod/observations.json` comes from one real refresh. To regenerate it, run one `pod models refresh` into a disposable `POD_CACHE_HOME`, copy that run's `current.json` unchanged, and keep it under the 64 KB bundle text limit. Observations never prove native access, billing or effective model settings.

## Decision records and rollback

An admission carrying `abandoned_accounting` requires a reader that supports fenced abandoned-assignment accounting. Preceding readers refuse that explicit field without changing the record; they must not reinterpret it as a started worker or a reusable terminal. Admissions without the field retain their existing representation and readability. Preserve the evidence and use a compatible reader rather than deleting the field for rollback.

Machine decision policy and remembered sensitive tool permissions are chosen by answering Pod, using `pod policy` and the tool decision flow; installation does not answer for the person. They live alongside route preferences in the existing personal config. A skipped first setup retains Careful. Existing session authentication, tool access and host policy remain external to Pod.

New decision records carry `decision_channels: 5`. Once marker 5 is actually written, the records require Pod 0.21.18 or newer; preceding readers refuse them without rewriting. That semantic marker is independent of the current physical storage format below; current physical publications deliberately require its compatible reader. Lossless placement preserves all existing decision markers and their semantic floors. It cannot remove an existing marker or its floor. The new personal fields `decision_policy`, `decision_generation`, `tool_permissions` and `pending_decisions` also require a compatible config reader. Removing those keys does not downgrade records already written. Preserve authority history and use a compatible reader; the installer performs no record migration or rollback conversion. Older supported records remain readable without rewriting.


## Retained evidence storage

Install a bundle implementing `pod-history-record/v1` (kind `context` or `governor`) before using `pod internal storage` to migrate an objective context or Governor journal. Inspection and full byte/structure audit are read-only; migration binds the exact inspected source revision and SHA256, preserves every logical value and retains the source revision and SHA256 as migration identity, and switches only that record's authoritative file. The installer never migrates records. Its schema-family notice recognizes the implemented format without certifying chunks, proof or live readiness.

Migrated records deliberately refuse preceding readers and normal writers. A version label alone does not establish compatibility: retain the exact compatible bundle identity. Restoring an older source after accepted new revisions would discard evidence and is not a supported downgrade. Use a compatible reader or an independently reviewed lossless export. The detailed format, attribution unknowns, operating budgets and fault behavior are [R180](spec/interface.md#r180--grow-retained-evidence-through-immutable-revisions).

Install/update also attempts queued immutable-cache reclamation outside objective writer locks. It never migrates a record or changes logical state. Deferred cleanup and queued bytes remain visible in `pod doctor`; explicit `storage` action `gc` retries a corruption-safe sweep. Earlier binaries may advise starting another objective or archiving/removing the record when they refuse this format. Do not follow that obsolete advice; continue with a compatible reader.
