"""Each module's line coverage against its floor (the weekly workflow's coverage job, T5 of the review of Oct 18).

    uv run pytest -q -n 8 --cov=job_apply --cov-report=json:coverage.json
    uv run python scripts/coverage_floor.py coverage.json

A module under its floor (FLOORS, below) fails the run; a module without one is named, not failed, as is a floor for
a module the report no longer has. Then the 20 functions with the most lines the suite never ran (each unrun line
counted in the innermost function or method around it, by the source's syntax tree). All of it goes to stdout and,
on GitHub, to the job's summary ($GITHUB_STEP_SUMMARY).

The floors are a module's measured coverage less two points (the full suite at -n 4, Oct 18, 2026), so a test lost or
a module grown without tests shows, and the day-to-day spread doesn't. Raise one when a module's tests grow:
`--suggest` prints the floors as the report would set them.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from pathlib import Path
from typing import Any, NamedTuple

MARGIN = 2.0  # points under the measured coverage a floor sits
TOP = 20  # the unrun functions listed
# (percent of each module's statements the full suite runs, less MARGIN: measured Oct 18, 2026)
FLOORS: dict[str, float] = {
    "job_apply": 98.0,
    "job_apply.__main__": 0.0,  # (python -m job_apply: run in a process of its own, not measured)
    "job_apply.ats": 95.0,
    "job_apply.autofill": 95.4,
    "job_apply.browser": 89.9,
    "job_apply.browser_blocks": 85.8,
    "job_apply.browser_dates": 91.1,
    "job_apply.browser_menus": 81.2,
    "job_apply.browser_tabs": 93.2,
    "job_apply.browser_uploads": 88.1,
    "job_apply.config": 92.5,
    "job_apply.desk": 90.1,
    "job_apply.doctor": 84.2,
    "job_apply.fixtures": 91.1,
    "job_apply.formjs": 98.0,
    "job_apply.history": 95.0,
    "job_apply.mailbox": 92.3,
    "job_apply.never": 98.0,
    "job_apply.pipeline": 91.5,
    "job_apply.pipeline_accounts": 85.2,  # (the pipeline's own tests alone, Oct 19: the full suite runs at least as much)
    "job_apply.pipeline_filling": 94.3,
    "job_apply.pipeline_mail": 87.9,
    "job_apply.postings": 95.5,
    "job_apply.recommend": 94.5,
    "job_apply.render": 92.2,
    "job_apply.report": 94.8,
    "job_apply.search": 94.5,
    "job_apply.server": 88.7,
    "job_apply.tracker": 93.7,
    "job_apply.updates": 90.1,
}


class Unrun(NamedTuple):
    module: str
    function: str  # its qualified name ("Run.click", "decide.<locals>.label"), or "(module)" for top-level lines
    first: int
    last: int
    unrun: int  # its statements the suite never ran
    statements: int


def module_name(path: str) -> str:
    """A measured file's module: "src/job_apply/pipeline.py" -> "job_apply.pipeline" (the package's __init__ is
    "job_apply")."""
    parts = list(Path(path).with_suffix("").parts)
    if "job_apply" in parts:
        parts = parts[parts.index("job_apply"):]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def line_coverage(summary: dict[str, Any]) -> float:
    """Percent of a file's statements run (lines only, whether or not the report also counts branches); 100 for a
    file with none."""
    statements = summary.get("num_statements") or 0
    return 100.0 if not statements else 100.0 * (summary.get("covered_lines") or 0) / statements


def functions(source: str) -> list[tuple[str, int, int, int]]:
    """Each function and method in `source`: (qualified name, its def's line, its body's first line, its last line).
    Its def line (run when the module loads) and its decorators are its enclosing scope's."""
    found: list[tuple[str, int, int, int]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                found.append((name, child.lineno, child.body[0].lineno, child.end_lineno or child.lineno))
                visit(child, name + ".<locals>.")
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".")
            else:
                visit(child, prefix)

    visit(ast.parse(source), "")
    return found


def unrun_functions(report: dict[str, Any], root: Path) -> list[Unrun]:
    """Every function with a line the suite never ran, from the report's files read under `root` (where pytest ran)."""
    out: list[Unrun] = []
    for path, data in (report.get("files") or {}).items():
        missing = set(data.get("missing_lines") or [])
        if not missing:
            continue
        statements = missing | set(data.get("executed_lines") or [])
        try:
            spans = functions((root / path).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            spans = []

        def innermost(line: int, spans: list[tuple[str, int, int, int]] = spans) -> tuple[str, int, int]:
            around = [s for s in spans if s[2] <= line <= s[3]]
            name, first, _, last = min(around, key=lambda s: s[3] - s[2]) if around else ("(module)", 0, 0, 0)
            return name, first, last

        tally: dict[tuple[str, int, int], list[int]] = {}
        for line in statements:
            counts = tally.setdefault(innermost(line), [0, 0])
            counts[1] += 1
            if line in missing:
                counts[0] += 1
        out += [Unrun(module_name(path), name, first, last, unrun, total)
                for (name, first, last), (unrun, total) in tally.items() if unrun]
    out.sort(key=lambda u: (-u.unrun, u.module, u.first))
    return out


def check(report: dict[str, Any], floors: dict[str, float], root: Path, top: int = TOP) -> tuple[list[str], list[str]]:
    """The summary's lines (Markdown) and the modules under their floors."""
    measured = {module_name(path): (line_coverage(data.get("summary") or {}), data.get("summary") or {})
                for path, data in (report.get("files") or {}).items()}
    under = sorted(m for m, (pct, _) in measured.items() if m in floors and pct < floors[m])
    unfloored = sorted(m for m in measured if m not in floors)
    gone = sorted(m for m in floors if m not in measured)
    lines = ["## Coverage", ""]
    totals = report.get("totals") or {}
    if totals:
        lines += [f"The suite ran {line_coverage(totals):.1f}% of job_apply's {totals.get('num_statements', 0)} "
                  "statements.", ""]
    if under:
        lines += [f"**Under the floor:** {', '.join(under)}.", ""]
    lines += ["| Module | Lines run | Statements | Floor | |", "|---|---:|---:|---:|---|"]
    for m, (pct, summary) in sorted(measured.items()):
        floor = floors.get(m)
        said = "under the floor" if m in under else ("no floor yet" if floor is None else "")
        lines.append(f"| {m} | {pct:.1f}% | {summary.get('num_statements', 0)} | "
                     f"{'' if floor is None else f'{floor:.1f}%'} | {said} |")
    lines.append("")
    if unfloored:
        lines += [f"No floor yet (not failed; `--suggest` gives one): {', '.join(unfloored)}.", ""]
    if gone:
        lines += [f"Floors for modules the report doesn't have (renamed or removed? take them out): "
                  f"{', '.join(gone)}.", ""]
    biggest = unrun_functions(report, root)[:top]
    if biggest:
        lines += [f"### The {len(biggest)} functions with the most lines never run", "",
                  "| Function | Lines | Unrun | Of |", "|---|---|---:|---:|"]
        lines += [f"| {u.module}.{u.function} | {u.first}-{u.last} | {u.unrun} | {u.statements} |"
                  if u.first else f"| {u.module} (outside any function) | | {u.unrun} | {u.statements} |"
                  for u in biggest]
        lines.append("")
    return lines, under


def suggest(report: dict[str, Any], margin: float = MARGIN) -> dict[str, float]:
    """The floors the report would set: each module's coverage less `margin`, rounded down to a tenth."""
    return {module_name(path): max(0.0, int((line_coverage(data.get("summary") or {}) - margin) * 10) / 10)
            for path, data in sorted((report.get("files") or {}).items())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report", type=Path, help="coverage.py's JSON report (pytest --cov-report=json:coverage.json)")
    ap.add_argument("--root", type=Path, default=Path("."), help="where the report's paths start (where pytest ran)")
    ap.add_argument("--top", type=int, default=TOP, help=f"how many unrun functions to list (default {TOP})")
    ap.add_argument("--suggest", action="store_true", help="print the floors this report would set, and stop")
    args = ap.parse_args(argv)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    if args.suggest:
        print("FLOORS: dict[str, float] = {")
        print("\n".join(f"    {m!r}: {floor}," for m, floor in suggest(report).items()))
        print("}")
        return 0
    lines, under = check(report, FLOORS, args.root, args.top)
    text = "\n".join(lines) + "\n"
    sys.stdout.write(text)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text)
    return 1 if under else 0


if __name__ == "__main__":
    sys.exit(main())
