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
        if self.open_criteria() or any(effect["outcome"] is None for effect in merges):
            return False               # a criterion is open, or a merge was asked for and its outcome is not read yet
        return any(self.merged(effect) for effect in merges) or self.objective.get("delivery") == "none"

    def next_id(self, prefix: str, existing) -> str:
        numbers = [int(key[len(prefix):]) for key in existing if key.startswith(prefix) and key[len(prefix):].isdigit()]
        return f"{prefix}{1 + max(numbers or [0])}"

    def author_routes(self, root=None, commit: str | None = None) -> set[str]:
        """Canonical routes with an accepted (integrated) implement assignment on this objective.

        With a root and a commit, an assignment whose integrated work is proven outside that commit's
        history did not author it and is left out; one whose place is not proven still counts."""
        from . import gitio
        routes = set()
        for row in self.assignments.values():
            if row.get("role") != "implement" or row.get("void"):
                continue
            done = self.result(row["id"])
            if not done or done.get("decision") != "integrated":
                continue
            route = row.get("effective") or row.get("route") or ""
            at = done.get("integrated_at")
            if root is not None and commit and done.get("contained") is True and at and at != commit:
                try:
                    if not gitio.is_ancestor(root, at, commit):
                        continue
                except Exception:
                    pass
            if route:
                routes.add(route)
        return routes

    def author_counts(self, root=None, commit: str | None = None) -> dict[str, int]:
        """Integrated implement assignments per model family whose work is in `commit`'s history.

        Used to rank a mixed-author review fallback; without a root and commit nothing is counted."""
        from . import gitio, routefile
        counts: dict[str, int] = {}
        if root is None or not commit:
            return counts
        for row in self.assignments.values():
            if row.get("role") != "implement" or row.get("void"):
                continue
            done = self.result(row["id"])
            route = row.get("effective") or row.get("route") or ""
            at = (done or {}).get("integrated_at")
            if not (route and done and done.get("decision") == "integrated" and done.get("contained") is True and at):
                continue
            try:
                inside = at == commit or gitio.is_ancestor(root, at, commit)
            except Exception:
                inside = False
            if inside:
                family = routefile.route_family(route)
                counts[family] = counts.get(family, 0) + 1
        return counts

    def author_families(self, root=None, commit: str | None = None) -> set[str]:
        """Model families (providers) that authored `commit`: an accepted implement assignment on this
        objective whose work is in its history, or not proven outside it. Without a commit, all of them."""
        from . import routefile
        return {routefile.route_family(route) for route in self.author_routes(root, commit)}

    def implement_dispatches(self) -> set[str]:
        """Dispatch ids of non-void implement assignments on this objective."""
        return {row["dispatch"] for row in self.assignments.values()
                if row.get("role") == "implement" and not row.get("void") and row.get("dispatch")}

    def reviewer_notes(self) -> list[dict]:
        """Persisted reviewer notes and partial findings from review and result facts."""
        notes: list[dict] = []
        for fact in self.facts:
            kind = fact.get("fact")
            if kind == "review" and fact.get("notes"):
                notes.append({
                    "assignment": fact.get("reviewer"),
                    "commit": fact.get("commit"),
                    "verdict": fact.get("verdict"),
                    "notes": fact["notes"],
                })
            elif kind == "result" and fact.get("role") == "review" and (
                fact.get("reviewer_notes") or fact.get("partial_findings")
            ):
                row = self.assignments.get(fact.get("assignment") or "") or {}
                notes.append({
                    "assignment": fact.get("assignment"),
                    "commit": row.get("base"),
                    "verdict": None,
                    "notes": fact.get("reviewer_notes") or "",
                    "partial_findings": list(fact.get("partial_findings") or []),
                })
        return notes


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
