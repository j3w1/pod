"""Helpers for the model tests: the public table fixture and small pages built to order."""

import html
import json
import os
from pathlib import Path

from tests.support import PodCase

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HEADERS = ["Model", "Context Window", "Creator", "Artificial Analysis Intelligence Index", "Cost per Task USD",
           "Time per Task Minutes", "First Answer Token (s)", "Further Analysis"]
ROWS = [["Claude Fable 5.2 (max)", "1M", "Anthropic", "65", "$26.54", "47.8", "245.58", "Model Providers"],
        ["GPT-6.1 Sol (high)", "1M", "OpenAI", "50", "$0.59", "7.2", "34.91", "Model Providers"],
        ["GPT-6.1 Terra (non-reasoning)", "1M", "OpenAI", "31", "$0.23", "2.4", "2.11", "Model Providers"],
        ["Gemini 4 Carbon (high)", "1M", "Google", "60", "$7.76", "25.5", "67.83", "Model Providers"]]
ROUTES = """schema: pod/v2
routes:
  codex/gpt-6.1-sol/high: enabled
  codex/gpt-6.1-sol/medium: disabled   # kept comment
  claude/claude-opus-5-5/high: enabled
preferred: null
pinned: null
workers:
  max_active: 6
decision_policy:
  preset: hands_off
  categories:
    merge_publish: any_agent
authorization_verifier:
- /usr/local/bin/verifier
"""


def live_page() -> str:
    """The public leaderboard table as published on 2026-11-12, reduced to the table."""
    return (FIXTURES / "leaderboard.html").read_text(encoding="utf-8")


def page(headers=HEADERS, rows=ROWS, *, text="Intelligence Index v5.0.1") -> str:
    head = "".join(f"<th><button>{html.escape(cell)}</button></th>" for cell in headers)
    body = "".join("<tr>" + "".join(f"<td><div>{html.escape(cell)}</div></td>" for cell in row) + "</tr>"
                   for row in rows)
    return f"<html><body><p>{text}</p><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></body></html>"


def without(column: str, headers=HEADERS, rows=ROWS):
    """The headers and rows with one column removed."""
    index = headers.index(column)
    return ([cell for i, cell in enumerate(headers) if i != index],
            [[cell for i, cell in enumerate(row) if i != index] for row in rows])


class ModelCase(PodCase):
    def setUp(self):
        super().setUp()
        os.environ.pop("CODEX_HOME", None)      # the base case restores the environment afterwards

    def codex_catalog(self, models: dict) -> None:
        """Write a stand-in for the installed Codex catalog: {slug: [efforts]}."""
        target = Path(os.environ["HOME"]) / ".codex" / "models_cache.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"models": [
            {"slug": slug, "display_name": slug.upper(), "visibility": "list", "context_window": 500000,
             "supported_reasoning_levels": [{"effort": effort} for effort in efforts]}
            for slug, efforts in models.items()]}), encoding="utf-8")

    def refresh_from(self, text: str) -> dict:
        from pod.models import snapshot
        return snapshot.refresh(lambda: text)
