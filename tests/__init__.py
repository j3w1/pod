"""Disposable Pod fixture tests."""

from tests import boundary

# First, before any test module, Pod module, fixture or subprocess exists. See tests/boundary.py for what
# it establishes and tests/isolation.py for the cause of #141. Background Git maintenance, which Git
# detaches after a commit and which under a process manager that does not reap orphans leaves a zombie
# for every one, is turned off there with the rest of the Git configuration.
boundary.enter()
