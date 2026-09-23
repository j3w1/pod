from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

ORCA_VERSION = "1.4.209"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / f"orca-{ORCA_VERSION}"


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
    return {"schema": "pod-route-establishment/v1", "runtime": runtime, "version": ORCA_VERSION,
            "executable": "/fixture/orca", "hard_stops": list(hard_stops), "disclosures": [],
            "route": {"agent": route.get("agent"), "model": route.get("model"),
                      "account": route.get("account"),
                      "bucket": route.get("bucket") if bucket is ... else bucket,
                      "effort": route.get("effort"), "context": route.get("context"),
                      "effective_context": route.get("effective_context")},
            "controls": {"account_identity": {"tier": "runtime_observation", "matched": True},
                         "context_window": {"tier": "enforceable_control",
                                            "source": "explicit synthetic fixture"}},
            "login": {"mode": "host_login", "auth": "oauth", "subscription": True,
                      "managed_accounts": 0, "identity_digest": route.get("account")},
            "billing": {"observed": observed, "approved": billing}}
