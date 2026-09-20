# Pod validation

Each result binds an exact commit and Git tree, host, UTC date, command, outcome and sanitized report reference. Offline fixtures do not prove live Orca, provider behavior, native Windows, hosted CI, independent review or acceptance. All new-candidate rows start `NOT_RUN` until a matching result is recorded.

## Required candidate checks

| Gate | Command or evidence | Current boundary |
| --- | --- | --- |
| Unit and incident suite | `PYTHONPATH=src python -m unittest discover -s tests -v` | Disposable synthetic fixtures |
| Explicit incident discovery | `PYTHONPATH=src:. python -m unittest discover -s tests/incidents -t . -v` | Tests must actually be discovered |
| Compile and diff | `python -m compileall -q src tests`; `git diff --check` | Local syntax/format |
| Skill validation | Skill creator `quick_validate.py src/pod/skill` | Frontmatter and scaffold only |
| Frozen build | `git archive HEAD` into a disposable cache directory, then build a wheel there | Exact committed candidate |
| Fresh isolated install | Install wheel into new disposable venv; smoke `pod --help`, each family help, `pod config --check --json`, `pod doctor --json` | No model/Orca mutation |
| Hosted Windows/Linux | Same suite, incident discovery, frozen wheel and installed CLI smokes in both jobs | Candidate-bound CI |
| Independent audit | Fresh reviewer of exact commit/tree and reproducible evidence | Findings only |
| Live core matrix | Codex and Claude Code, each on native Linux and Windows: discovery, in-session coordination, authorized native worker/effective route, lifecycle, verification and adoption | Separate live authorization and disposable project |
| Project acceptance | Owner governance, merge, release and any publication decisions | External |

The [scenario coverage file](pod-coverage.json) lists all A01–A82. Its test paths identify intended offline cases; the file itself proves only inventory integrity. Candidate-bound live and hosted rows remain `NOT_RUN` until actually exercised. The direct-agent, native-Orca and Pod matched evaluation also remains `NOT_RUN` without bounded live authorization.

`python -m pod.internal release-gate --input RECORD.json` projects these exact rows. A complete set returns `owner_decision_required`; it never authorizes publication, merge or installed-state cutover. Missing live subchecks remain unavailable even if a top-level result says PASS.

## Evidence record

```json
{
  "schema": "pod-validation/v1",
  "candidate": "exact Git commit",
  "tree": "exact Git tree",
  "host": "Linux or Windows",
  "utc": "timestamp",
  "gate": "name",
  "command": "sanitized command",
  "outcome": "PASS | FAILED | NOT_RUN | UNAVAILABLE",
  "report": "sanitized artifact reference and digest"
}
```

Do not store raw environments, credentials, source packets, personal paths or runtime IDs in tracked evidence. A passing offline suite cannot promote live, review, hosted, acceptance, merge or release labels.

## Runtime limits

The current installed Orca worker contract supports workers without terminals. Pod's read adapter treats terminal identity as optional. Its pre-dispatch billing eligibility and hidden descendant fan-out controls have not been verified, so the guarded admission path fails closed. The metadata adapter does not scrape credential stores or undocumented quota endpoints. Reset credits have only an offline intent guard; no redemption transport is installed. WSL and remote ownership, exact live launch and release reconciliation, and both native OS core matrices require separate evidence.
