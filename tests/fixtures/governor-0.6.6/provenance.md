These sanitized Governor journals were emitted by the unmodified Pod 0.6.6 writer at
commit `f4101feb8e517382cb4cc626a1afc702c6429f37`, run from a source snapshot of that
commit with stubbed native authority, a disposable state directory and an in-memory
GitHub port whose runs carry synthetic events. Nothing was dispatched remotely.

`journal.json` is the 0.6.6 `pod-governor/v2` journal for one prepared unit:

- a `pr_update` whose ci.yml run was a `pull_request` run, and the publication-derived
  ci.yml row 0.6.6 journaled for it, read back to PASS without its event;
- a requested release.yml dispatch settled PASS by 0.6.6 readback, without its event;
- requested dispatches still pending: nightly.yml with no bound run, full.yml bound by
  0.6.6's newest-run pick to a `pull_request` run that appeared after the dispatch, and
  deep.yml bound to its `workflow_dispatch` run;
- a remote diagnostic answered PASS.

`provider.json` names those rows and holds the port's synthetic runs, so readback tests
can serve the same runs. Candidate hashes, run ids, URLs and the authorization actor are
fixture values. This shows records the released writer can produce; it is not hosted CI,
live provider evidence or an observation of owner state.
