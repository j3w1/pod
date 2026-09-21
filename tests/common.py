from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
from unittest.mock import patch


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
                 "XDG_STATE_HOME": str(base / "state-home"),
                 "CODEX_HOME": str(base / "codex-home"),
                 "CLAUDE_CONFIG_DIR": str(base / "claude-home")}
        (base / "home").mkdir()
        with patch.dict(os.environ, homes, clear=False):
            os.environ.pop("POD_CONFIG_HOME", None)
            os.environ.pop("POD_STATE_HOME", None)
            yield path


def establishment(route, *, runtime="runtime", billing="included", observed="subscription",
                  hard_stops=(), bucket=...):
    """A minimal route establishment record, as the production port would build one."""
    return {"schema": "pod-route-establishment/v1", "runtime": runtime, "version": "1.4.206",
            "executable": "/fixture/orca", "hard_stops": list(hard_stops), "disclosures": [],
            "route": {"agent": route.get("agent"), "model": route.get("model"),
                      "account": route.get("account"),
                      "bucket": route.get("bucket") if bucket is ... else bucket,
                      "effort": route.get("effort")},
            "controls": {}, "login": {"mode": "host_login", "auth": "oauth", "subscription": True,
                                      "managed_accounts": 0, "identity_digest": None},
            "billing": {"observed": observed, "approved": billing}}
