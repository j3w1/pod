"""Disposable Pod fixture tests."""

import os


def _without_background_git_maintenance() -> None:
    """Disposable test repositories never need automatic maintenance.

    Git detaches that maintenance into a background process after a commit. Under a process
    manager that does not reap orphans, each one stays behind as a zombie, so a suite run would
    leave thousands of them on the host.
    """
    count = int(os.environ.get("GIT_CONFIG_COUNT", "0") or 0)
    if any(os.environ.get(f"GIT_CONFIG_KEY_{index}") == "maintenance.auto" for index in range(count)):
        return
    os.environ[f"GIT_CONFIG_KEY_{count}"] = "maintenance.auto"
    os.environ[f"GIT_CONFIG_VALUE_{count}"] = "false"
    os.environ["GIT_CONFIG_COUNT"] = str(count + 1)


_without_background_git_maintenance()
