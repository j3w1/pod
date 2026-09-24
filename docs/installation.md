# Installing Pod on Linux

Pod's one-shot installer downloads the current `main` source, validates the `skills/pod` bundle, installs it through the pinned skills CLI for Codex and Claude Code, and places a user-local `pod` launcher. Installation does not test account sign-in, connect Orca, start workers, call a model, or edit an agent's settings.

## Prerequisites

- Linux and a non-root user.
- Python 3.13 or newer with `venv`, `ensurepip`, and curses. On Debian-derived systems, the matching `python3.13-venv` package may be needed. A user-managed Python such as `uv python install 3.13` also works when it is the `python3` found on PATH.
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
| `${XDG_CONFIG_HOME:-~/.config}/pod/config.yaml` | Personal model selection and worker ceiling; kept byte-for-byte if valid. |

The installer may append one guarded PATH block to `.zshrc`, `.bashrc` and a Bash login file, or `.profile`, according to `$SHELL`. It never follows a symlinked rc file. It prints a `fish_add_path` instruction for fish instead of editing fish configuration. Reopen the shell when it adds a block; a child installer cannot change its parent shell. A different `pod` earlier on PATH and any duplicate skill copy are reported for inspection, never removed automatically. `CODEX_HOME` is not a second installation destination.

## Updating and recovery

`pod update` downloads `main`, runs the same staged checks, and returns “Already current” when the receipt, launcher and copy match. A change to the canonical skill is preserved under `preserved/` before the skills CLI replaces it. Running native workers remain untouched; reload active coordinator conversations before new starts.

If a download, dependency install, or copy is interrupted, rerun the one-shot installer or `pod update`. A receipt still marked `installing` records the previous and target digests. A bundle with neither digest yields `install_incomplete` instead of false readiness. `pod doctor --json` reports the receipt, canonical digest, venv, launcher ownership, placements, duplicates, and relevant next action.

An invalid or older-shaped preference file is kept untouched, with no eligible models. Use `pod config edit` to correct it; Pod performs no automatic migration. The installer never overwrites a foreign `~/.local/bin/pod` or a redirected rc file. If `pod` resolves to another command, inspect `command -v pod` and your PATH order before removing anything.

## Removal

Run `pod doctor --json` first to identify owned paths. Remove the Pod skill through your skills manager or remove only the canonical copy and Claude link identified above; inspect any reported duplicate before touching it. Remove the launcher only when its content matches the Pod-owned template. Remove the `pod` data directory for the venv, receipt, lock, and preserved copies only after deciding whether to keep those backups. Remove only the guarded PATH block between `# >>> pod path >>>` and `# <<< pod path <<<` from files that contain it. Keep or remove the personal config separately according to whether you want to retain your choices. Do not delete an unrelated executable, another skill copy, or agent application settings.

## Catalog maintenance

The six supported model definitions, official guidance, checked source dates and Artificial Analysis snapshot live in `skills/pod/catalog.json`. Update that one file from checked sources, then run `PYTHONPATH=skills python -m pod.catalog --check` and the repository validation gates. Benchmark rows are reference data; they do not prove native access or effective model settings.
