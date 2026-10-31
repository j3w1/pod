"""Disposable Pod fixture tests."""

import os


def _isolate_git_and_snapshot_host() -> None:
    """Keep every Git command in the suite inside its fixture, and record the host state to guard.

    The snapshot is taken first, from the environment the suite was started in. See tests/isolation.py
    for the cause of #141 and for why maintenance is turned off (Git detaches it after a commit and,
    under a process manager that does not reap orphans, each one stays behind as a zombie).
    """
    from tests import isolation
    isolation.snapshot_host()
    isolation.scrub_git_environment(os.environ)


_isolate_git_and_snapshot_host()
