"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from raisemap.analysis import FunctionReport, Project, analyse_sources

SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".tox",
        "build",
        "dist",
        "site-packages",
    }
)


def load_sources(root: Path) -> dict[str, str]:
    """Return ``{relative_path: source}`` for the ``.py`` files under ``root``.

    Raises
    ------
    FileNotFoundError
        If ``root`` does not exist.
    """
    if not root.exists():
        raise FileNotFoundError(f"No such file or directory: {root}")
    if root.is_file():
        return {root.name: root.read_text(encoding="utf-8")}
    sources: dict[str, str] = {}
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if name not in SKIPPED_DIRECTORIES and not name.startswith(".")
        )
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = Path(directory) / filename
            try:
                sources[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
    return sources


def _caveat(report: FunctionReport) -> str:
    notes = []
    if report.unresolved_calls:
        notes.append(f"{report.unresolved_calls} unresolved call(s)")
    if report.dynamic_raises:
        notes.append(f"{report.dynamic_raises} dynamic raise(s)")
    return f"  [{', '.join(notes)}]" if notes else ""


def render_map(reports: Sequence[FunctionReport], show_all: bool) -> str:
    """Format the exception map as text, one block per function."""
    lines: list[str] = []
    shown = 0
    for report in reports:
        if not report.raises and not show_all:
            continue
        shown += 1
        lines.append(f"{report.path}:{report.line}  {report.name}{_caveat(report)}")
        if not report.raises:
            lines.append("    (no escaping exception found)")
        width = max((len(name) for name in report.raises), default=0)
        for name, origin in report.raises.items():
            lines.append(f"    {name.ljust(width)}  {origin.render()}")
    lines.append("")
    lines.append(f"{shown} of {len(reports)} function(s) shown.")
    return "\n".join(lines)


def check(project: Project, reports: Sequence[FunctionReport]) -> list[dict[str, object]]:
    """Return docstring disagreements as JSON-serialisable records.

    ``RM001`` is an exception that escapes but is not documented. ``RM002`` is a
    documented exception for which no raising path was found.
    """
    problems: list[dict[str, object]] = []
    for report in reports:
        for name in project.undocumented(report):
            problems.append(
                {
                    "rule": "RM001",
                    "function": report.name,
                    "path": report.path,
                    "line": report.line,
                    "exception": name,
                    "message": f"raises {name} ({report.raises[name].render()}) "
                    "but the docstring does not document it",
                }
            )
        for name in project.stale(report):
            suffix = (
                f"; {report.unresolved_calls} call(s) could not be resolved"
                if report.unresolved_calls
                else ""
            )
            problems.append(
                {
                    "rule": "RM002",
                    "function": report.name,
                    "path": report.path,
                    "line": report.line,
                    "exception": name,
                    "message": f"documents {name} but no raising path was found{suffix}",
                }
            )
    return problems


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser."""
    parser = argparse.ArgumentParser(
        prog="raisemap",
        description="Static exception map: which exceptions can escape each function.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("map", "print the exceptions that can escape each function"),
        ("check", "compare escaping exceptions against docstrings"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("path", nargs="?", default=".", help="file or directory")
        command.add_argument("--format", choices=("text", "json"), default="text")
        command.add_argument(
            "--include-private", action="store_true", help="include underscore-prefixed functions"
        )
    commands.choices["map"].add_argument(
        "--all", action="store_true", help="also list functions with no escaping exception"
    )
    commands.choices["check"].add_argument(
        "--strict", action="store_true", help="also fail on RM002 (documented but not found)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return the process exit status."""
    arguments = build_parser().parse_args(argv)
    try:
        sources = load_sources(Path(arguments.path))
    except (FileNotFoundError, UnicodeDecodeError) as error:
        print(f"raisemap: {error}", file=sys.stderr)
        return 2
    project = analyse_sources(sources)
    for error in project.errors:
        print(f"raisemap: not parsed: {error}", file=sys.stderr)
    reports = [
        report for report in project.reports() if arguments.include_private or report.is_public
    ]
    if arguments.command == "map":
        if arguments.format == "json":
            payload = [
                {
                    "function": report.name,
                    "path": report.path,
                    "line": report.line,
                    "raises": {
                        name: {"site": origin.site, "via": origin.via}
                        for name, origin in report.raises.items()
                    },
                    "unresolved_calls": report.unresolved_calls,
                    "dynamic_raises": report.dynamic_raises,
                }
                for report in reports
                if report.raises or arguments.all
            ]
            print(json.dumps(payload, indent=2))
        else:
            print(render_map(reports, arguments.all))
        return 0
    problems = check(project, reports)
    if arguments.format == "json":
        print(json.dumps(problems, indent=2))
    else:
        for problem in problems:
            print(
                f"{problem['path']}:{problem['line']}  {problem['rule']}  "
                f"{problem['function']}  {problem['message']}"
            )
        undocumented = sum(1 for problem in problems if problem["rule"] == "RM001")
        print(
            f"{undocumented} undocumented, {len(problems) - undocumented} documented but not "
            f"found, across {len(reports)} function(s)."
        )
    failing = problems if arguments.strict else [p for p in problems if p["rule"] == "RM001"]
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
