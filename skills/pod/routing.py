"""Choose from enabled routes using a bounded, read-only sample of Pod dispatches."""

from datetime import datetime, timedelta, timezone
from heapq import nlargest

from . import log, routefile
from .errors import PodError
from .state import fold

FAMILIES = ("codex", "claude")
WINDOW = 20
DAYS = 7


def _confirmed(cutoff: datetime):
    for path, size in log.recent_paths(cutoff.timestamp()):
        state = fold(log.read(path, size=size))
        for row in state.assignments.values():
            if row.get("void") or not row.get("dispatch"):
                continue
            started = datetime.fromisoformat(row["at"])
            if started < cutoff:
                continue
            route = row.get("effective", row.get("route"))
            if not isinstance(route, str) or not routefile.ROUTE.fullmatch(route):
                raise ValueError("unreadable dispatch route")
            family = routefile.split(route)[0]
            if family in FAMILIES:
                yield started, str(path), row["n"], family


def history() -> list[str] | None:
    """The newest dispatch families in chronological order. None means the sample is unavailable."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS)
    try:
        return [item[-1] for item in reversed(nlargest(WINDOW, _confirmed(cutoff)))]
    except (KeyError, OSError, PodError, TypeError, ValueError):
        return None


def choose(preferences: routefile.Preferences, eligible: list[str]) -> tuple[str | None, str]:
    """Select a default from the eligible pool and explain which preference or rule applied."""
    if preferences.pinned is not None:
        return (preferences.pinned, "pin") if preferences.pinned in eligible else (None, "pin_ineligible")
    preferred = preferences.preferred if preferences.preferred in eligible else None
    if preferences.routing != "balance":
        return preferred, "preferred"
    routes = {family: [route for route in eligible if route.startswith(family + "/")] for family in FAMILIES}
    families = [family for family in FAMILIES if routes[family]]
    if not families:
        return preferred, "preferred"
    sample = history()
    if sample is None:
        return preferred, "history_unavailable"
    if len(families) == 1:
        family = families[0]
    else:
        # The next dispatch takes the oldest dispatch's place when the window is full.
        prior = sample[-(WINDOW - 1):]
        codex_deficit = preferences.codex_share * (len(prior) + 1) - 100 * prior.count("codex")
        if codex_deficit == 50 and (preferred in routes["codex"] or preferred in routes["claude"]):
            return preferred, "balance"
        family = "codex" if codex_deficit >= 50 else "claude"
    pool = routes[family]
    return (preferred if preferred in pool else pool[0]), "balance"
