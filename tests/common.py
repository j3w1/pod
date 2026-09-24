from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

ORCA_VERSION = "1.4.209"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / f"orca-{ORCA_VERSION}"
HOST_HOME = Path.home().resolve()
HOST_POD_DATA = Path(os.environ.get("XDG_DATA_HOME", HOST_HOME / ".local/share")).resolve() / "pod"


def disposable_path(home: Path) -> str:
    """Expose only a disposable launcher directory and system executables."""
    return os.pathsep.join((str(home / ".local/bin"), "/usr/local/bin", "/usr/bin", "/bin"))


def modified_bundle(root: Path) -> Path:
    """An independent bundle copy with changed code bytes and the same VERSION."""
    from pod.bundle import bundle_root
    destination = root / "modified-pod"
    shutil.copytree(bundle_root(), destination,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    module = destination / "operations.py"
    module.write_bytes(module.read_bytes() + b"\n# disposable changed-code proof\n")
    return destination


def receipt(name: str) -> dict:
    """One sanitized capture of the installed Orca runtime's own output."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def envelope(name: str) -> dict:
    """The same capture in the shape read_command returns."""
    body = receipt(name)
    return {"runtime": body["_meta"]["runtimeId"], "result": body["result"]}


@contextmanager
def fixture():
    root = os.environ.get("POD_TEST_ROOT")
    if root:
        Path(root).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as name:
        base = Path(name)
        path = base / "fixture"
        path.mkdir()
        homes = {"HOME": str(base / "home"),
                 "XDG_CONFIG_HOME": str(base / "config-home"),
                 "XDG_DATA_HOME": str(base / "data-home"),
                 "XDG_STATE_HOME": str(base / "state-home"),
                 "CODEX_HOME": str(base / "codex-home"),
                 "CLAUDE_CONFIG_DIR": str(base / "claude-home"),
                 "PATH": disposable_path(base / "home")}
        if (Path(homes["XDG_DATA_HOME"]).resolve(strict=False) / "pod").is_relative_to(HOST_POD_DATA):
            raise AssertionError("Disposable fixture resolved into the host Pod data directory")
        (base / "home").mkdir()
        with patch.dict(os.environ, homes, clear=False):
            os.environ.pop("POD_CONFIG_HOME", None)
            os.environ.pop("POD_STATE_HOME", None)
            yield path


# Canonical 0.6 fixture contracts. A delegated objective always carries an obligation map;
# these helpers build the smallest honest one for a disposable project outside Git.
KERNEL_TASKS = ("task", "task1", "task2", "task3", "task-0", "task-1", "task-2", "task-3", "task-4",
                "task-5", "task-6", "task-7", "task-8", "replacement-task", "shared-task", "disabled-now",
                "other-edit", "bad-effort", "other", "new-task", "first", "second", "third", "t",
                "source-changed", "source-absent", "instruction-changed", "instruction-absent",
                "unavailable", "future-output")


def task_obligation(task: str) -> str:
    return "t-" + task


def kernel_map(criteria=("works",), tasks=KERNEL_TASKS, base_ref=None) -> dict:
    """Intake map: the first criterion is coordinator-held; everything else is sequenced."""
    sequenced = {"class": "sequenced", "referent": "O0"}
    obligations = [{"id": "O0", "kind": "criterion", "provenance": "objective",
                    "source": {"ref": criteria[0]}, "check": f"{criteria[0]} passes",
                    "state": "active", "executor": "coordinator"}]
    obligations += [{"id": f"O{index}", "kind": "criterion", "provenance": "objective",
                     "source": {"ref": criterion}, "check": f"{criterion} passes",
                     "state": "waiting", "wait": dict(sequenced)}
                    for index, criterion in enumerate(criteria[1:], start=1)]
    obligations += [{"id": task_obligation(task), "kind": "subgoal", "provenance": "coordinator",
                     "parent": "O0", "check": f"{task} is delivered", "state": "waiting",
                     "wait": dict(sequenced)} for task in tasks]
    return {"governance": {"base_ref": base_ref}, "obligations": obligations}


def kernel_binding(task: str = "task", *, revision: int = 1, role: str = "implement",
                   boundary: dict | None = None) -> dict:
    return {"serves": [task_obligation(task)], "role": role,
            "boundary": boundary or {"paths": [], "surfaces": []}, "map_revision": revision}


def stored_map(state: dict) -> dict:
    """Round-trip the accepted map fields a coordinator writes back."""
    checkpoint = state["checkpoint"]
    return {"obligations": checkpoint["obligations"], "proposals": checkpoint.get("proposals", [])}


def restated_map(project, objective, port, *, owner="owner", run="run") -> dict | None:
    """Restate obligations whose worker settled as sequenced behind the coordinator-held one."""
    from pod.ledger import _outstanding_ids, binding_valid, read
    state = read(project, objective)
    if not state or not state.get("checkpoint") or state["checkpoint"].get("obligations") is None:
        return None
    outstanding = set(_outstanding_ids(state, fake_native(port, state, owner=owner, run=run)))
    holder = state["checkpoint"].get("coordinator_slot")
    changed = False
    obligations = []
    for row in state["checkpoint"]["obligations"]:
        row = dict(row)
        if row["state"] == "active" and row["executor"] != "coordinator" and row["executor"] not in outstanding:
            row.pop("executor")
            row["state"] = "waiting"
            row["wait"] = {"class": "sequenced", "referent": holder}
            changed = True
        obligations.append(row)
    return {"obligations": obligations} if changed else None


def fake_native(port, state, *, owner="owner", run="run") -> dict:
    """Exact assignment evidence from a fake port without consuming its test callbacks."""
    from pod.errors import PodError
    from pod.ledger import binding_valid
    from pod.operations import _assignment_evidence
    evidence = []
    for row in (state or {}).get("admissions", {}).values():
        binding = row.get("native_binding")
        if row["state"] != "bound" or not binding_valid(binding):
            continue
        if hasattr(port, "workers") and binding["dispatchId"] not in port.workers:
            continue
        try:
            evidence.append(_assignment_evidence(port.show_worker(binding["dispatchId"]), row))
        except (PodError, KeyError):
            continue
    return {"runtime": getattr(port, "runtime", "runtime"), "owner": owner, "authoritative": True,
            "scope": "objective_assignments", "complete": True, "assignments": evidence,
            "physical_capacity": "unavailable"}


def fake_authority(port):
    """A require_authority stand-in that still carries exact assignment evidence."""
    def authority(project, objective, *, owner, state=None, run_id=None, native_port=None):
        from pod.ledger import read
        current = state if state is not None else read(project, objective)
        refs = {row["runId"]: row["runtime"]
                for row in ((current or {}).get("checkpoint") or {}).get("native_refs", [])}
        return {"runtime": "runtime", "run_id": run_id or "run", "references": refs or {"run": "runtime"},
                "native": fake_native(port, current, owner=owner)}
    return authority


VERIFICATION = {"dependencies": ["pyyaml==6.0.3"], "environment": "fixture"}


def proof(obligation: str, candidate: str = "c1", *, status: str = "PASS", policy_revision: str = "p",
          environment: str = "fixture", dependencies=("pyyaml==6.0.3",), sources=(), check: str = "unit") -> dict:
    """A detailed pod-evidence/v1 record joined to one obligation by its id (R41)."""
    return {"schema": "pod-evidence/v1", "criterion": obligation, "candidate": candidate,
            "sources": list(sources), "policy_revision": policy_revision, "dependencies": list(dependencies),
            "environment": environment, "check": check, "command": "python -m unittest", "result": "observed",
            "timestamp": "2026-09-24T00:00:00Z", "status": status, "reference": "log"}
