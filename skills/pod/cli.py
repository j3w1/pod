"""The `pod` command. Coordination commands load nothing about models, terminals or the network."""

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from . import __version__
from .errors import PodError

COMMANDS = "open, dispatch, accept, record, status, deliver, resume, drop; config, models, doctor, update, export"


def _list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="pod", description="A lightweight coordinator over Orca. Commands: " + COMMANDS)
    top.add_argument("--version", action="version", version=__version__)
    sub = top.add_subparsers(dest="command", required=True)

    def command(name: str, text: str, *, objective: bool = True) -> argparse.ArgumentParser:
        one = sub.add_parser(name, help=text, description=text)
        one.add_argument("--json", action="store_true", help="print the result as JSON")
        if objective:
            one.add_argument("--objective", "-o", metavar="ID", help="default: the open objective of this worktree")
        return one

    one = command("open", "Open an objective from an issue or a sentence and return its criteria.")
    one.add_argument("--issue", metavar="N", help="issue number, owner/name#number or URL")
    one.add_argument("--text", help="the objective in a sentence, when there is no issue")
    one.add_argument("--criterion", action="append", metavar="TEXT", help="state a criterion yourself (repeatable)")
    one.add_argument("--target", default="main", help="branch the work is delivered to (default: main)")
    one.add_argument("--branch", help="existing branch to continue (default: the current branch)")
    one.add_argument("--pr", type=int, help="existing pull request to continue")
    one.add_argument("--run", help="existing Orca Run to continue")
    one.add_argument("--continues", metavar="REFERENCE", help="the earlier objective this one continues")
    one.add_argument("--no-delivery", action="store_true", help="it ends without a merge, such as an investigation")

    one = command("dispatch", "Start one supervised worker through Orca.")
    one.add_argument("--role", choices=("implement", "review", "investigate"))
    one.add_argument("--title", help="short task title")
    one.add_argument("--spec", help="the task text")
    one.add_argument("--spec-file", metavar="PATH", help="read the task text from a file, or - for standard input")
    one.add_argument("--route", metavar="AGENT/MODEL/EFFORT", help="default: the Pin, else the Preferred route")
    one.add_argument("--paths", type=_list, metavar="A,B", help="implement: the only paths it may change; review: limit the review")
    one.add_argument("--commit", help="review: the commit to review (default: HEAD)")
    one.add_argument("--context", type=_list, metavar="A,B", help="review: paths whose later change lapses this review")
    one.add_argument("--worktree", default="current", help="Orca worktree selector (default: this worktree)")
    one.add_argument("--name", help="display name for the worker")
    one.add_argument("--retry", metavar="ASSIGNMENT", help="after a lost reply: read from Orca whether it started")

    one = command("accept", "Read a finished worker's result and record it with your decision.")
    one.add_argument("assignment")
    one.add_argument("--integrated", metavar="COMMIT", help="the commit that now holds its work")
    one.add_argument("--discarded", action="store_true", help="its work is not used")
    one.add_argument("--why", help="reason, when discarding")

    one = command("record", "Record what only you know: a criterion met, a decision, a person's words, the note.")
    one.add_argument("--met", action="append", metavar="CRITERION")
    one.add_argument("--facts", type=_list, metavar="N,N", help="fact numbers that prove the criteria named with --met")
    one.add_argument("--why", help="what was observed, who decided, or the reason")
    one.add_argument("--drop-criterion", action="append", metavar="CRITERION")
    one.add_argument("--add-criterion", action="append", metavar="TEXT")
    one.add_argument("--after-merge", action="append", metavar="CRITERION", help="it can only be proved after the merge")
    one.add_argument("--check", metavar="COMMAND", help="run a check now and record its observed exit")
    one.add_argument("--waive", metavar="FINDING")
    one.add_argument("--authorize", action="store_true", help="authorize merging the current commit")
    one.add_argument("--standing", action="store_true", help="with --authorize: cover every delivery of this objective")
    one.add_argument("--withdraw", action="store_true", help="withdraw the authorization")
    one.add_argument("--words", help="the person's own words for a waiver, authorization, scope change or decision")
    one.add_argument("--carried", metavar="REFERENCE", help="a review carried from an earlier record")
    one.add_argument("--commit", help="with --carried: the reviewed commit")
    one.add_argument("--paths", type=_list, metavar="A,B", help="with --carried: the reviewed paths")
    one.add_argument("--verdict", choices=("pass", "fail"), help="with --carried")
    one.add_argument("--scope", metavar="TEXT", help="a change of scope")
    one.add_argument("--decision", metavar="TEXT")
    one.add_argument("--note", metavar="TEXT", help="the continuation note: what a fresh conversation needs to go on")
    one.add_argument("--pr", type=int, help="the pull request that delivers this objective")
    one.add_argument("--branch")

    one = command("status", "Show the objective: criteria, workers, findings and what delivery still needs.")
    one.add_argument("--all", action="store_true", help="list every open objective on this host (reads no provider)")
    one.add_argument("--local", action="store_true", help="skip Orca and GitHub reads")

    one = command("deliver", "Run the gate and merge the exact candidate, once.")
    one.add_argument("--check", action="store_true", help="print the verdict and change nothing")
    one.add_argument("--commit", help="with --check: the exact candidate to judge (default: HEAD)")

    command("resume", "Continue an objective from this conversation.")
    one = command("drop", "Stop an objective. A dropped objective holds nothing.")
    one.add_argument("--why", required=True)

    one = command("config", "Show the personal preference file, or open it in your editor.", objective=False)
    one.add_argument("action", nargs="?", choices=("edit",))
    sub.add_parser("models", help="model routes and public observations", add_help=False)
    command("doctor", "Read-only installation and capability diagnostics.", objective=False)
    sub.add_parser("update", help="update the installed bundle", add_help=False)
    sub.add_parser("export", help="export records written by 0.21 to plain JSON", add_help=False)
    return top


def _config(edit: bool) -> dict:
    from . import paths, routefile
    target = paths.route_file()
    if edit:
        editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
        if not target.exists():
            raise PodError("config_absent", f"There is no preference file at {target}.", fix="run `pod models` to create one")
        subprocess.run([*editor, str(target)], check=False)
    found = routefile.load(target)
    return {"path": str(target), "status": found.status, **({"problem": found.problem} if found.problem else {}),
            "enabled": found.enabled(), "preferred": found.preferred, "pinned": found.pinned,
            "merge_authorization": found.merge_authorization,
            "authorization_verifier": "configured" if found.authorization_verifier else None}


def execute(args: argparse.Namespace, cwd: Path) -> dict:
    name = args.command
    if name == "config":
        return _config(args.action == "edit")
    if name == "doctor":
        from . import maintenance
        return maintenance.doctor()
    from . import commands, gate
    if name == "status" and args.all:
        return commands.listing()
    if name == "open":
        return commands.open_(cwd, source=args.issue, text=args.text, objective=args.objective, target=args.target,
                              branch=args.branch, pr=args.pr, run=args.run, continues=args.continues, criteria=args.criterion,
                              delivery=not args.no_delivery)
    if name == "dispatch":
        spec = args.spec
        if args.spec_file:
            spec = sys.stdin.read() if args.spec_file == "-" else Path(args.spec_file).read_text(encoding="utf-8")
        return commands.dispatch(cwd, args.objective, role=args.role, title=args.title, spec=spec, route=args.route,
                                 paths_=args.paths, commit=args.commit, context=args.context, worktree=args.worktree,
                                 retry=args.retry, name=args.name)
    if name == "accept":
        return commands.accept(cwd, args.objective, args.assignment, integrated=args.integrated,
                               discarded=args.discarded, why=args.why)
    if name == "record":
        given = {key: value for key, value in vars(args).items() if key not in ("command", "json", "objective")}
        return commands.record(cwd, args.objective, **given)
    if name == "status":
        return commands.status(cwd, args.objective, remote=not args.local)
    if name == "deliver":
        return gate.deliver(cwd, args.objective, check=args.check, commit=args.commit)
    if name == "resume":
        return commands.resume(cwd, args.objective)
    return commands.drop(cwd, args.objective, why=args.why)


def render(value, indent: int = 0) -> str:
    """Compact text for a model or a person: one line per fact, lists as dashes."""
    pad = "  " * indent
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if item is None or item == [] or item == {}:
                continue
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.append(render(item, indent + 1))
            else:
                lines.append(f"{pad}{key}: {item}")
        return "\n".join(lines)
    if isinstance(value, list):
        return "\n".join(f"{pad}- {render(item, indent + 1).strip() if isinstance(item, (dict, list)) else item}" for item in value)
    return f"{pad}{value}"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["models"]:
        from .models import cli as models_cli
        return models_cli.main(argv[1:])
    if argv[:1] == ["update"]:
        from . import maintenance
        return maintenance.update(argv[1:])
    if argv[:1] == ["export"]:
        from . import export
        return export.main(argv[1:])
    args = parser().parse_args(argv)
    try:
        result = execute(args, Path.cwd())
    except PodError as exc:
        if args.json:
            print(json.dumps({"refused": exc.as_dict()}, indent=2))
        else:
            print(f"pod: refused ({exc.code}): {exc}", file=sys.stderr)
            for need in (exc.detail or {}).get("needs", []):
                print(f"  needs: {need}", file=sys.stderr)
            if exc.fix:
                print(f"  fix: {exc.fix}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0 if result.get("pass", True) is not False else 1
