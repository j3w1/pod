"""Disposable decision-boundary fixtures; no host identity or live authority proof."""

import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import yaml

from pod import config

PERSON = {"provenance": "user_direct", "instruction": "Use this exact proposed decision", "channel": "session"}
LOCAL = {"provenance": "agent_decision", "instruction": "Use this exact proposed decision", "channel": "agent_session"}
RELAY = {"provenance": "decision_request", "instruction": "Use this exact proposed decision", "channel": "relayed",
         "relay": {"reference": "fixture-decision", "verification": "decision"}}

VERIFIER = '''import json, sys
from pathlib import Path
request = json.load(sys.stdin)
controls = json.loads(Path(sys.argv[1]).read_text())
if "saved_verdict" in controls:
    print(json.dumps(controls["saved_verdict"]))
    sys.exit()
binding = request["decision"]
principal = {"kind": controls.get("kind", "agent"), "identity": "fixture-agent",
             "scope": {"kinds": binding.get("kinds", [binding["kind"]]), "categories": binding["categories"], "resources": binding["resources"]}}
verdict = {**request, "verified": True, "principal": principal, "decided_at": "2026-10-01T00:00:00Z",
           "single_use": "fixture:" + binding["request_id"]}
for path, value in controls.get("changes", {}).items():
    target = verdict
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
print(json.dumps(verdict))
'''


class MachineCase:
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # `main()` reads the project configuration of the current directory's checkout: never this one.
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        isolate = patch.dict(os.environ, {name: str(self.root / suffix) for name, suffix in (
            ("POD_CONFIG_HOME", "config"), ("POD_STATE_HOME", "state"), ("POD_CACHE_HOME", "cache"))})
        isolate.start()
        self.addCleanup(isolate.stop)
        config.write_defaults(config.personal_path())
        self.controls = self.root / "controls.json"
        self.controls.write_text("{}")
        self.verifier = self.root / "verifier.py"
        self.verifier.write_text(VERIFIER)

    def configure_verifier(self, **controls):
        self.controls.write_text(json.dumps(controls))
        path = config.personal_path()
        document = yaml.safe_load(path.read_text())
        document["authorization_verifier"] = [sys.executable, str(self.verifier), str(self.controls)]
        path.write_text(yaml.safe_dump(document))

    def policy(self, name="hands_off"):
        return config.set_decision_policy(None, name, decision=PERSON)

    def memory(self, mode="ask_each_time", category="secrets"):
        question = config.tool_check(None, "fixture-tool", category, "one exact operation",
                                     spending="new_commitment" if category == "money" else None)
        config.tool_answer(None, question["invocation"], mode, decision=PERSON)
        return question


def save_vouch(case, operation, *, actor="agent"):
    """Save a fixed fixture vouch before the subsequent public mutation request."""
    from copy import deepcopy
    from pod import relay
    captured = []
    class Captured(Exception):
        pass
    def capture(command, request):
        captured.append(deepcopy(request))
        raise Captured()
    with patch.object(relay, "verify_decision", side_effect=capture), case.assertRaises(Captured):
        operation()
    binding = captured[0]["decision"]
    vouch = {**captured[0], "verified": True, "decided_at": "2026-10-01T00:00:00Z",
             "single_use": "saved:" + binding["request_id"],
             "principal": {"kind": actor, "identity": "saved-fixture-actor", "scope": {
                 "kinds": binding.get("kinds", [binding["kind"]]), "categories": binding["categories"],
                 "resources": binding["resources"]}}}
    case.controls.write_text(json.dumps({"saved_verdict": vouch}))
    return vouch
