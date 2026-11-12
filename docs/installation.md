# Installing Pod on Linux

Pod's one-shot installer downloads the current `main` source, validates the `skills/pod` bundle, places it through the pinned skills CLI for Codex and Claude Code, and writes a user-local `pod` launcher. Installation does not test account sign-in, connect Orca, start workers, call a model, or edit an agent's settings. It never reads or writes Pod's state directory.

## Prerequisites

- Linux and a non-root user.
- The latest stable CPython as `python3` on PATH, with curses (used by `pod models`). The installer uses that `python3` and refuses a prerelease; it never falls back to another interpreter. Pod needs nothing beyond the standard library, so there is no virtual environment and nothing is installed from PyPI.
- Node and `npx`, plus curl or wget.
- Network access to `raw.githubusercontent.com` for the bootstrap script, `codeload.github.com` for the source, and npm for `skills@1.7.0`.
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

The script refuses root and other systems. It downloads one tarball, rejects unsafe archive paths, then runs the staged installer, which uses only the standard library. In order, the installer:

1. Checks prerequisites and ownership before changing anything: a `pod` command, skill directory or Claude link it did not place is refused, not replaced.
2. Validates the staged bundle.
3. Writes a receipt marked `installing`.
4. Lets the skills CLI copy the skill, then verifies that the copy's digest equals the validated source and that the Claude link points at it.
5. Writes the launcher.
6. Creates the preference file only when none exists.
7. Adds the PATH block where needed.
8. Marks the receipt `installed`.

An incomplete copy blocks the launcher until a rerun repairs it. Output is plain in pipes and logs; a terminal also gets one small ASCII banner.

The bundle is whatever the skill directory holds: every Python, Markdown, JSON and YAML file under `skills/pod`, plus `VERSION`. No list names its files, so a new module is part of the bundle as soon as it is added. `python -m pod.skill_validation` checks the source tree, and `python -m pod.skill_validation --installed PATH` proves a placed copy carries the same bytes.

## Paths and ownership

| Path | Purpose |
| --- | --- |
| `~/.agents/skills/pod` | Canonical skills-CLI copy used by Codex. |
| `${CLAUDE_CONFIG_DIR:-~/.claude}/skills/pod` | Relative link to the canonical copy for Claude Code. |
| `${XDG_DATA_HOME:-~/.local/share}/pod/install.json` | Version and digest receipt; `installed` is written last. |
| `${XDG_DATA_HOME:-~/.local/share}/pod/preserved/skill-*` | A changed canonical copy saved before replacement. |
| `~/.local/bin/pod` | Owned shell launcher: it runs the placed bundle with the `python3` the installer verified. |
| `${XDG_CONFIG_HOME:-~/.config}/pod/config.yaml` | Personal route preferences, Preferred, Pin and who may authorize a merge. Written only when absent; never rewritten by an install or an update. |
| `${XDG_CACHE_HOME:-~/.cache}/pod/` | Cached public model observations. Disposable. |

A first preference file enables each route the shipped registry lists, except `ultra` efforts, which are never enabled by default. If the registry cannot be read it enables nothing and says so. An existing file is byte-identical after every install and update, including a file this version cannot read: that one is reported, yields no eligible route, and waits for `pod config edit`.

The installer may append one guarded PATH block to `.zshrc`, `.bashrc` and a Bash login file, or `.profile`, according to `$SHELL`. It never follows a symlinked rc file. It prints a `fish_add_path` instruction for fish instead of editing fish configuration. Reopen the shell when it adds a block; a child process cannot change its parent shell. A different `pod` earlier on PATH and any duplicate skill copy are reported for inspection, never removed. `CODEX_HOME` is not a second installation destination.

Earlier installers kept a Python environment under `${XDG_DATA_HOME:-~/.local/share}/pod/venv-*`, each with a Pod marker file. A successful install removes those marked directories, because nothing uses them now, and leaves any unmarked directory alone.

## Updating and recovery

`pod update` downloads `main`, runs the same staged checks, and reports “Already current” when the receipt, launcher and copy match. A change someone made to the canonical skill is preserved under `preserved/` before the skills CLI replaces it. Running workers are untouched; reload active coordinator conversations before new starts. `pod update` needs only the installer, so it also repairs a bundle whose package can no longer start.

If a download or a copy is interrupted, rerun the one-shot installer or `pod update`. A receipt still marked `installing` records the previous and the target digests; a copy that matches neither makes every other command answer `install_incomplete` instead of running. Only a copy made for the recorded target is replaced automatically; anything else in that directory is refused and left as it is.

`pod doctor` reads and reports the version, the interpreter, the bundle and its digest, the receipt, launcher ownership, the Claude link, duplicates, whether `orca`, `git` and `gh` are present and what version each prints, the preference file's status, and where configuration, state and cache live. It uses no network and changes nothing. What it cannot observe it reports as unknown.

The installer never overwrites a `~/.local/bin/pod` it did not write or a redirected rc file. If `pod` resolves to another command, inspect `command -v pod` and your PATH order before removing anything.

## Removal

Run `pod doctor` first to identify owned paths. Remove the Pod skill through your skills manager, or remove only the canonical copy and the Claude link named above; inspect any reported duplicate before touching it. Remove the launcher only when `pod doctor` reports it as owned. Remove the `pod` data directory, with its receipt, lock and preserved copies, only after deciding whether to keep those copies. Remove only the guarded PATH block between `# >>> pod path >>>` and `# <<< pod path <<<` from files that contain it. Keep or remove the preference file according to whether you want to retain your choices. The cache holds only public data and can be removed at any time. Do not delete an unrelated executable, another skill copy, or agent application settings.
