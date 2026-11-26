"""The shipped registry: which exact agent, model and effort combinations Pod can route.

An entry is an id, the public display name, the agent, the provider, the efforts and optional
wire metadata. Adding a model is a change to `registry.json` alone. Being listed here makes a
route routable; it never enables one. Only the personal preference file does that.
"""

from dataclasses import dataclass
import json
from pathlib import Path
import re

from .. import routefile
from ..errors import PodError

EFFORTS = routefile.EFFORTS
SUPPORTED_PROVIDERS = frozenset({"Anthropic", "Google", "OpenAI"})
SUPPORTED_AGENTS = frozenset({"antigravity", "claude", "codex"})
WIRE_MODES = ("effort-flag", "model-suffix")
FILE = Path(__file__).with_name("registry.json")
_ID = re.compile(r"[a-z][a-z0-9.\-]{1,63}")
_ROW = re.compile(r"(?P<base>.+?)(?: \((?P<qualifier>[^()]+)\))?")


@dataclass(frozen=True)
class Model:
    id: str
    name: str
    agent: str
    provider: str
    efforts: tuple
    wire: str = "effort-flag"

    def route(self, effort: str) -> str:
        return f"{self.agent}/{self.id}/{effort}"


@dataclass(frozen=True)
class RowName:
    """A public row name read by the one naming rule: `Name (effort)`."""
    base: str
    effort: str | None = None       # a known effort word, when the row states one
    fallback: bool = False          # "(effort with fallback)"
    non_reasoning: bool = False     # "(non-reasoning)"
    qualifier: str | None = None    # the parenthesis text when it is none of the above


def _validate_defaults(section: object) -> dict:
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError("defaults must be an object")
    if section.get("schema", "pod/v2") != "pod/v2":
        raise ValueError("defaults.schema")
    if section.get("merge_authorization", "person") != "person":
        raise ValueError("defaults.merge_authorization")
    review = section.get("review_agents", [])
    if not isinstance(review, list) or any(not isinstance(item, str) or not routefile.AGENT.fullmatch(item) for item in review):
        raise ValueError("defaults.review_agents")
    profiles = section.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("defaults.profiles")
    for name, spec in profiles.items():
        routefile.parse_profile(name, spec)
    return section


def load(path: Path = FILE) -> list[Model]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        models = []
        for entry in document["models"]:
            wire = entry.get("wire", "effort-flag")
            model = Model(entry["id"], entry["name"], entry["agent"], entry["provider"], tuple(entry["efforts"]), wire)
            if (not _ID.fullmatch(model.id) or not _ID.fullmatch(model.agent) or not model.name.strip()
                    or model.agent not in SUPPORTED_AGENTS or model.provider not in SUPPORTED_PROVIDERS
                    or model.wire not in WIRE_MODES
                    or not model.efforts or len(set(model.efforts)) != len(model.efforts)
                    or any(effort not in EFFORTS for effort in model.efforts)):
                raise ValueError(model.id)
            models.append(model)
        if document.get("schema") != "pod-registry/v1" or len({m.id for m in models}) != len(models) \
                or len({m.name.casefold() for m in models}) != len(models):
            raise ValueError("schema or duplicate")
        _validate_defaults(document.get("defaults"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PodError("registry_invalid", f"The model registry cannot be read ({exc}).",
                       fix="reinstall Pod; the registry ships with it") from exc
    return models


def defaults(path: Path = FILE) -> dict:
    """Return the validated defaults section shipped in `registry.json`."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        return _validate_defaults(document.get("defaults"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PodError("registry_invalid", f"The model registry defaults cannot be read ({exc}).",
                       fix="reinstall Pod; the registry ships with it") from exc


def routes(models: list[Model]) -> dict:
    """Every routable key, mapped to its (model, effort)."""
    return {model.route(effort): (model, effort) for model in models for effort in model.efforts}


def read_name(name: str) -> RowName:
    """Split a public row name. Nothing here decides whether a route exists."""
    match = _ROW.fullmatch(name.strip())
    base, qualifier = (match["base"], match["qualifier"]) if match else (name.strip(), None)
    if qualifier is None:
        return RowName(base)
    words = qualifier.strip().lower()
    if words == "non-reasoning":
        return RowName(base, non_reasoning=True)
    fallback = words.endswith(" with fallback")
    effort = words[:-len(" with fallback")] if fallback else words
    if effort in EFFORTS:
        return RowName(base, effort=effort, fallback=fallback)
    return RowName(base, qualifier=qualifier)


def find(models: list[Model], base: str) -> Model | None:
    wanted = " ".join(base.split()).casefold()
    return next((model for model in models if model.name.casefold() == wanted), None)
