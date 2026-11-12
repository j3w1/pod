"""The five concepts, folded from an objective's log. Nothing here is stored; it is recomputed.

Entry kinds: `objective` (opens the log, later ones change its fields), `criterion` (latest
entry for an id wins), `assignment` (latest for an id wins), `fact` (immutable) and `effect`
(an intent, then its provider-read outcome).
"""

from dataclasses import dataclass, field

from .errors import PodError

FACTS = ("result", "check", "review", "ci", "authorization", "waiver", "scope", "decision", "note", "takeover")
ROLES = ("implement", "review", "investigate")


@dataclass
class State:
    entries: list = field(default_factory=list)
    objective: dict = field(default_factory=dict)
    criteria: dict = field(default_factory=dict)      # id -> latest criterion entry
    assignments: dict = field(default_factory=dict)   # id -> merged assignment entries
    facts: list = field(default_factory=list)
    effects: dict = field(default_factory=dict)       # id -> {"intent": entry, "outcome": entry | None}

    @property
    def id(self) -> str:
        return self.objective["id"]

    def of(self, kind: str) -> list[dict]:
        return [fact for fact in self.facts if fact["fact"] == kind]

    def result(self, assignment: str) -> dict | None:
        return next((fact for fact in self.of("result") if fact["assignment"] == assignment), None)

    def merges(self) -> list[dict]:
        return [effect for effect in self.effects.values() if effect["intent"]["effect"] == "merge"]

    def merged(self, effect: dict) -> bool:
        return bool(effect["outcome"] and effect["outcome"].get("merged") is True)

    def open_criteria(self, *, before_merge_only: bool = False) -> list[dict]:
        return [row for row in self.criteria.values() if row["state"] == "open"
                and not (before_merge_only and row.get("after_merge"))]

    @property
    def dropped(self) -> bool:
        return bool(self.objective.get("dropped"))

    @property
    def closed(self) -> bool:
        """Derived: no criterion is open and every delivery opened is merged. Nothing writes this.

        An objective that delivers stays open until a delivery has merged; one opened as needing
        no delivery, such as an investigation, is closed once no criterion is open.
        """
        if self.dropped:
            return True
        merges = self.merges()
        if self.open_criteria() or any(not self.merged(effect) for effect in merges):
            return False
        return bool(merges) or self.objective.get("delivery") == "none"

    def next_id(self, prefix: str, existing) -> str:
        numbers = [int(key[len(prefix):]) for key in existing if key.startswith(prefix) and key[len(prefix):].isdigit()]
        return f"{prefix}{1 + max(numbers or [0])}"


def fold(entries: list[dict]) -> State:
    state = State(entries=entries)
    for entry in entries:
        kind = entry.get("kind")
        if kind == "objective":
            state.objective.update({key: value for key, value in entry.items() if key not in ("kind", "n", "at")})
            state.objective.setdefault("opened", entry["at"])
        elif kind == "criterion":
            state.criteria[entry["id"]] = {**state.criteria.get(entry["id"], {}), **entry}
        elif kind == "assignment":
            state.assignments[entry["id"]] = {**state.assignments.get(entry["id"], {}), **entry}
        elif kind == "fact":
            state.facts.append(entry)
        elif kind == "effect":
            slot = state.effects.setdefault(entry["id"], {"intent": None, "outcome": None})
            slot["outcome" if entry["phase"] == "outcome" else "intent"] = entry
        else:
            raise PodError("log_damaged", f"Entry {entry.get('n')} has an unknown kind {kind!r}.",
                           fix="update Pod; this log was written by a newer version")
    if entries and "id" not in state.objective:
        raise PodError("log_damaged", "The log does not begin with an objective.", fix="restore the log file from a copy")
    return state
