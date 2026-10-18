"""A mutation pass over the answer rules (T6, the review of Oct 18): small changes to the code that decides an answer
(a comparison flipped, an `if` negated, a `not` dropped, `and` and `or` swapped, a `return Answer(...)` made
`return None`, one `|` alternative taken out of one of never.py's patterns), each run against the answer rules' tests.
A change no test notices (a survivor) is a decision nothing holds: it becomes a test, or, when it changes nothing a
form could see, an entry in mutation_equivalents.txt with the reason.

Each mutant is written into a copy of the package in a temporary folder, put first on PYTHONPATH for the test run:
the checkout's src is never touched. The weekly workflow runs a sample of 120 (seeded by the ISO week, so a week's
run repeats) and fails below FLOOR, listing the survivors with their diffs in its summary.

    uv run python scripts/mutation_pass.py --sample 120               # this week's sample
    uv run python scripts/mutation_pass.py --list                     # every mutant, without running any
    uv run python scripts/mutation_pass.py --targets never --jobs 2   # all of never.py's, two at a time
    uv run python scripts/mutation_pass.py --ids 9a4b70f2e8           # a survivor again, once its test is written
"""

from __future__ import annotations

import argparse
import ast
import datetime
import difflib
import functools
import hashlib
import json
import os
import queue
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

SERVER = Path(__file__).resolve().parents[1]
SRC = SERVER / "src" / "job_apply"
PLUGIN_ROOT = SERVER.parent
EQUIVALENTS = Path(__file__).resolve().parent / "mutation_equivalents.txt"

DEFAULT_TARGETS = ["autofill._resolve_field", "autofill.choose_option", "autofill._answer_bank", "never"]
TESTS = ["tests/test_autofill.py", "tests/test_never_for_them.py"]
TIMEOUT = 60.0  # seconds a mutant's test run may take (the two files take about 15 s on a runner)
# The kill rate (killed of killed and survived) under which the weekly job fails: the first measured rate less five
# points (Oct 18, seed 2026-W42: 112 of 120 killed, 93.3%; its eight survivors became seven tests and an equivalent)
FLOOR = 88.0

# Operators: what each changes
OPS = {
    "flip-compare": "a comparison turned to its opposite (== and !=, < and >=, in and not in, is and is not)",
    "negate-if": "an if's test negated",
    "drop-not": "a not dropped",
    "swap-boolop": "and made or, or or made and",
    "return-none": "a return Answer(...) made return None",
    "drop-alternative": "one | alternative taken out of a pattern (never.py)",
}
_FLIP: dict[type, tuple[type, str]] = {
    ast.Eq: (ast.NotEq, "!="), ast.NotEq: (ast.Eq, "=="), ast.Lt: (ast.GtE, ">="), ast.GtE: (ast.Lt, "<"),
    ast.Gt: (ast.LtE, "<="), ast.LtE: (ast.Gt, ">"), ast.In: (ast.NotIn, "not in"), ast.NotIn: (ast.In, "in"),
    ast.Is: (ast.IsNot, "is not"), ast.IsNot: (ast.Is, "is"),
}
_OP_TOKEN = re.compile(r"==|!=|<=|>=|<|>|\bnot\s+in\b|\bis\s+not\b|\bin\b|\bis\b")


Edit = tuple[int, int, str]  # (start byte, end byte, the text put there)


@dataclass
class Mutant:
    id: str
    file: str  # the module's file name, relative to the package ("autofill.py")
    line: int
    op: str
    before: str  # the source lines the change touches, before and after it
    after: str
    status: str = ""  # killed | survived | timeout (empty until run)
    seconds: float = 0.0
    edits: list[Edit] = field(default_factory=list, repr=False)  # (not written to mutants.json)

    def row(self) -> dict:
        row = asdict(self)
        del row["edits"]
        return row

    def key(self) -> tuple[str, str, str, str]:
        return self.file, self.op, _norm(self.before), _norm(self.after)

    def diff(self) -> str:
        return "".join(difflib.unified_diff(self.before.splitlines(True), self.after.splitlines(True),
                                            f"{self.file}:{self.line}", f"{self.file}:{self.line} ({self.op})"))


def _norm(text: str) -> str:
    return " ".join(text.split())


# ---- finding the mutation points ----

def _lines(source: str) -> list[str]:
    """The lines as ast numbers them (str.splitlines would also split at a form feed or a \\u2028 in a string)."""
    return [line.decode("utf-8") for line in source.encode("utf-8").splitlines(True)]


def _byte_offsets(source: str) -> list[int]:
    """The byte offset each line starts at (ast's columns count UTF-8 bytes)."""
    starts, total = [0], 0
    for line in source.encode("utf-8").splitlines(True):
        total += len(line)
        starts.append(total)
    return starts


def _span(node: ast.AST, starts: list[int]) -> tuple[int, int]:
    return (starts[node.lineno - 1] + node.col_offset,  # type: ignore[attr-defined]
            starts[node.end_lineno - 1] + node.end_col_offset)  # type: ignore[attr-defined]


def _alternatives(pattern: str) -> list[str]:
    """A pattern's top-level | alternatives (none inside a group or a class, nor an escaped |)."""
    parts, depth, in_class, i, start = [], 0, False, 0, 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class = True
            if pattern[i + 1:i + 2] == "]":  # ("[]...]": a ] first is the class's own)
                i += 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            parts.append(pattern[start:i])
            start = i + 1
        i += 1
    parts.append(pattern[start:])
    return parts


def _literal(text: str) -> str:
    """A string literal for text, raw where it can be (a pattern reads as it did)."""
    if '"' not in text and "\n" not in text and not text.endswith("\\"):
        return f'r"{text}"'
    return repr(text)


def _docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def _inside_fstrings(tree: ast.AST) -> set[int]:
    """Nodes inside an f-string: Python 3.10 gives them wrong positions, so they're left alone everywhere."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            found.update(id(n) for n in ast.walk(node))
    return found


Change = Callable[[ast.AST], None]  # the same change made to the tree, to check the edit against


def _points(tree: ast.AST, source: str, regex: bool) -> Iterator[tuple[ast.AST, str, list[Edit], Change]]:
    """Every mutation point under tree: (the node, the operator, the source edits, the change to the tree)."""
    starts = _byte_offsets(source)
    raw = source.encode("utf-8")
    skip = _inside_fstrings(tree)
    docs = _docstrings(tree)

    def text(a: int, b: int) -> str:
        return raw[a:b].decode("utf-8")

    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Compare):
            prev = node.left
            for i, (op, right) in enumerate(zip(node.ops, node.comparators)):
                a, b = _span(prev, starts)[1], _span(right, starts)[0]
                gap = text(a, b)
                new_op, token = _FLIP[type(op)]
                m = _OP_TOKEN.search(gap)
                if m is not None:
                    edit = (a, b, gap[:m.start()] + token + gap[m.end():])
                    yield node, "flip-compare", [edit], functools.partial(_set_op, i=i, new_op=new_op)
                prev = right
        elif isinstance(node, ast.If):
            a, b = _span(node.test, starts)
            yield node, "negate-if", [(a, a, "not ("), (b, b, ")")], _negate_test
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            a, b = _span(node, starts)
            oa, ob = _span(node.operand, starts)
            yield node, "drop-not", [(a, b, "(" + text(oa, ob) + ")")], _no_change  # (made by mutants(): _drop_not)
        elif isinstance(node, ast.BoolOp):
            word, other = ("and", "or") if isinstance(node.op, ast.And) else ("or", "and")
            a, b = _span(node, starts)
            # (bracketed, and its own and/or parts too: "x or y and z" made "x or (y or z)", "(x and (y and z))")
            edits = [(a, a, "("), (b, b, ")")]
            for value in node.values:
                if isinstance(value, ast.BoolOp):
                    va, vb = _span(value, starts)
                    edits += [(va, va, "("), (vb, vb, ")")]
            for left, right in zip(node.values, node.values[1:]):
                a, b = _span(left, starts)[1], _span(right, starts)[0]
                edits.append((a, b, re.sub(rf"\b{word}\b", other, text(a, b), count=1)))
            yield node, "swap-boolop", edits, _swap_op
        elif isinstance(node, ast.Return) and isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Name) and node.value.func.id == "Answer":
            a, b = _span(node.value, starts)
            yield node, "return-none", [(a, b, "None")], functools.partial(_set_value, value=ast.Constant(None))
        elif regex and isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            alts = _alternatives(node.value)
            if len(alts) < 2:
                continue
            a, b = _span(node, starts)
            for i, alt in enumerate(alts):
                if not alt:
                    continue
                rest = "|".join(alts[:i] + alts[i + 1:])
                yield node, "drop-alternative", [(a, b, _literal(rest))], functools.partial(_set_value, value=rest)


# The changes to the tree each operator's edit is checked against
def _set_op(n: Any, i: int, new_op: type) -> None:
    n.ops[i] = new_op()


def _negate_test(n: Any) -> None:
    n.test = ast.UnaryOp(ast.Not(), n.test)


def _swap_op(n: Any) -> None:
    n.op = ast.Or() if isinstance(n.op, ast.And) else ast.And()


def _set_value(n: Any, value: Any) -> None:
    n.value = value


def _no_change(n: Any) -> None:
    pass


def _apply(source: str, edits: list[Edit]) -> str:
    raw = source.encode("utf-8")
    for a, b, new in sorted(edits, reverse=True):
        raw = raw[:a] + new.encode("utf-8") + raw[b:]
    return raw.decode("utf-8")


def _same_tree(a: ast.AST, b: ast.AST) -> bool:
    return ast.dump(a, include_attributes=False) == ast.dump(b, include_attributes=False)


def _scope(tree: ast.Module, name: str | None) -> ast.AST | None:
    """The function (or method) a target names in its module, or the whole module."""
    if name is None:
        return tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _top(tree: ast.Module, node: ast.AST) -> ast.stmt:
    """The module-level statement that holds node."""
    for stmt in tree.body:
        if stmt.lineno <= node.lineno <= stmt.end_lineno:  # type: ignore[attr-defined]
            return stmt
    raise ValueError(node)


def mutants(src: Path, targets: Sequence[str]) -> list[Mutant]:
    """Every mutant of the targets ("module" or "module.function"), in the order found. Each edit is checked: its
    statement, mutated, must parse to the original's tree with that one change, or the mutant is dropped."""
    found: list[Mutant] = []
    seen: dict[str, int] = {}
    for target in targets:
        module, _, func = target.partition(".")
        path = src / f"{module}.py"
        if not path.is_file():
            raise SystemExit(f"no module {module!r} in {src}")
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        scope = _scope(tree, func or None)
        if scope is None:
            raise SystemExit(f"no function {func!r} in {path.name}")
        starts = _byte_offsets(source)
        raw = source.encode("utf-8")
        lines = _lines(source)
        points = sorted(_points(scope, source, regex=module == "never"),
                        key=lambda p: (p[0].lineno, p[0].col_offset))  # type: ignore[attr-defined]
        for node, op, edits, change in points:
            # (checked on its own statement, parsed alone: parsing the whole module for each is minutes)
            top = _top(tree, node)
            first_line = min([top.lineno] + [d.lineno for d in getattr(top, "decorator_list", [])])
            a, b = starts[first_line - 1], starts[top.end_lineno]  # type: ignore[index]
            segment = raw[a:b].decode("utf-8")
            at = [i for i, n in enumerate(ast.walk(top)) if n is node][0]
            try:
                got = ast.parse(_apply(segment, [(x - a, y - a, t) for x, y, t in edits]))
            except SyntaxError:
                continue
            want = ast.parse(segment)
            change(list(ast.walk(want.body[0]))[at])
            if op == "drop-not":  # (the not's parent takes its operand)
                want = _drop_not(want, list(ast.walk(want.body[0]))[at])
            if not _same_tree(got, want):
                continue
            mutated = _apply(source, edits)
            shown = node.test if isinstance(node, ast.If) else node  # (an if's own lines: its test's)
            first, last = shown.lineno, shown.end_lineno  # type: ignore[attr-defined]
            added = mutated.count("\n") - source.count("\n")
            before = "".join(lines[first - 1:last])
            after = "".join(_lines(mutated)[first - 1:last + added])
            digest = hashlib.sha1(f"{path.name}\0{op}\0{before}\0{after}".encode()).hexdigest()[:10]
            seen[digest] = seen.get(digest, 0) + 1
            mid = digest if seen[digest] == 1 else f"{digest}-{seen[digest]}"
            found.append(Mutant(mid, path.name, first, op, before, after, edits=edits))
    return found


def _drop_not(tree: ast.Module, target: ast.AST) -> ast.Module:
    class Drop(ast.NodeTransformer):
        def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
            self.generic_visit(node)
            return node.operand if node is target else node

    return Drop().visit(tree)


# ---- the accepted survivors ----

def read_equivalents(path: Path) -> list[dict[str, str]]:
    """mutation_equivalents.txt: blocks of "file:", "op:", "before:", "after:" and "reason:" lines, a blank line
    between blocks; a before or after of several lines goes on indented lines under its key. # starts a comment line."""
    if not path.is_file():
        return []
    entries: list[dict[str, str]] = []
    entry: dict[str, str] = {}
    key = ""
    for line in path.read_text(encoding="utf-8").splitlines() + [""]:
        if line.startswith("#"):
            continue
        if not line.strip():
            if entry:
                entries.append(entry)
            entry, key = {}, ""
        elif line[:1].isspace() and key:
            entry[key] += "\n" + line.strip()
        else:
            key, _, value = line.partition(":")
            key = key.strip()
            entry[key] = value.strip()
    for e in entries:
        missing = {"file", "op", "before", "after", "reason"} - set(e)
        if missing:
            raise SystemExit(f"{path.name}: an entry lacks {', '.join(sorted(missing))}: {e}")
    return entries


def equivalent_keys(entries: list[dict[str, str]]) -> set[tuple[str, str, str, str]]:
    return {(e["file"], e["op"], _norm(e["before"]), _norm(e["after"])) for e in entries}


# ---- running ----

def _env(copy_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(copy_root), env.get("PYTHONPATH", "")) if p)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("CLAUDE_PLUGIN_ROOT", str(PLUGIN_ROOT))  # (the copy's own parents hold no templates or data)
    return env


def _copy(src: Path, into: Path) -> Path:
    shutil.copytree(src, into / src.name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return into / src.name


def run_one(m: Mutant, package: Path, command: Sequence[str], cwd: Path, timeout: float) -> str:
    """Write the mutant into the package copy, run the command, put the module back: killed, survived or timeout."""
    path = package / m.file
    original = path.read_text(encoding="utf-8")
    path.write_text(_apply(original, m.edits), encoding="utf-8")
    start = datetime.datetime.now()
    try:
        done = subprocess.run(list(command), cwd=cwd, env=_env(package.parent), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout)
        status = "survived" if done.returncode == 0 else "killed"
    except subprocess.TimeoutExpired:
        status = "timeout"
    finally:
        path.write_text(original, encoding="utf-8")
    m.seconds = round((datetime.datetime.now() - start).total_seconds(), 1)
    return status


def run_all(chosen: list[Mutant], src: Path, command: Sequence[str], cwd: Path, timeout: float, jobs: int,
            progress: Callable[[str], None] = lambda line: None) -> None:
    """Run each mutant against the command, `jobs` at a time (each worker its own copy of the package). The
    unchanged copy runs first and must pass, and must be the package the command imports."""
    with tempfile.TemporaryDirectory(prefix="mutation-") as tmp:
        made = [_copy(src, Path(tmp) / f"w{i}") for i in range(max(1, jobs))]
        copies: queue.Queue[Path] = queue.Queue()
        for package in made:
            copies.put(package)
        first = made[0]
        where = subprocess.run([sys.executable, "-c", f"import {src.name}; print({src.name}.__file__)"], cwd=cwd,
                               env=_env(first.parent), capture_output=True, text=True, encoding="utf-8")
        if Path(where.stdout.strip() or "/").resolve().parent != first.resolve():
            raise SystemExit(f"the tests would import {src.name} from {where.stdout.strip() or where.stderr.strip()}, "
                             f"not the mutated copy")
        base = subprocess.run(list(command), cwd=cwd, env=_env(first.parent), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout * 3)
        if base.returncode != 0:
            raise SystemExit("the tests fail without any mutant:\n" + (base.stdout + base.stderr)[-3000:])

        def work(m: Mutant) -> None:
            package = copies.get()
            try:
                m.status = run_one(m, package, command, cwd, timeout)
            finally:
                copies.put(package)
            progress(f"{m.status:8} {m.seconds:5.1f}s {m.file}:{m.line} {m.op} {m.id}")

        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            list(pool.map(work, chosen))


# ---- reporting ----

def kill_rate(chosen: list[Mutant]) -> float | None:
    """Killed of killed and survived, in percent (a timeout is neither: it may be the machine)."""
    killed = sum(m.status == "killed" for m in chosen)
    decided = killed + sum(m.status == "survived" for m in chosen)
    return 100.0 * killed / decided if decided else None


def summary(chosen: list[Mutant], total: int, skipped: int, stale: list[dict[str, str]], floor: float,
            seed: str) -> str:
    rate = kill_rate(chosen)
    counts = {s: sum(m.status == s for m in chosen) for s in ("killed", "survived", "timeout")}
    out = ["## Mutation pass (the answer rules)", "",
           f"{len(chosen)} mutants of {total} (seed `{seed}`; {skipped} accepted as equivalent, not run): "
           f"{counts['killed']} killed, {counts['survived']} survived, {counts['timeout']} timed out. "
           + (f"Kill rate **{rate:.1f}%** (floor {floor:.0f}%)." if rate is not None else "Nothing decided."), ""]
    by_op: dict[str, tuple[int, int]] = {}
    for m in chosen:
        k, s = by_op.get(m.op, (0, 0))
        by_op[m.op] = (k + (m.status == "killed"), s + (m.status == "survived"))
    if by_op:
        out += ["| operator | killed | survived |", "|---|---|---|"]
        out += [f"| {op} | {k} | {s} |" for op, (k, s) in sorted(by_op.items())]
        out.append("")
    survivors = [m for m in chosen if m.status == "survived"]
    if survivors:
        out += ["### Survivors", "", "Each is a decision no test holds: a test for it, or an entry in "
                "`scripts/mutation_equivalents.txt` with the reason it changes nothing.", ""]
        for m in survivors:
            out += [f"`{m.id}` {m.file}:{m.line} {m.op}", "", "```diff", m.diff().rstrip(), "```", ""]
    timeouts = [m for m in chosen if m.status == "timeout"]
    if timeouts:
        out += ["### Timed out", ""] + [f"- `{m.id}` {m.file}:{m.line} {m.op}" for m in timeouts] + [""]
    if stale:
        out += ["### Equivalents no mutant matches any more", "",
                "The code moved on: take these out of `scripts/mutation_equivalents.txt`.", ""]
        out += [f"- {e['file']} {e['op']}: `{_norm(e['before'])[:100]}`" for e in stale] + [""]
    return "\n".join(out)


def iso_week(day: datetime.date | None = None) -> str:
    year, week, _ = (day or datetime.date.today()).isocalendar()
    return f"{year}-W{week:02d}"


def choose(found: list[Mutant], equivalents: set[tuple[str, str, str, str]], sample: int | None,
           seed: str) -> list[Mutant]:
    """The mutants to run: those not accepted as equivalent, a seeded sample of them when asked, in found order."""
    pool = [m for m in found if m.key() not in equivalents]
    if sample is not None and sample < len(pool):
        picked = set(id(m) for m in random.Random(seed).sample(pool, sample))
        pool = [m for m in pool if id(m) in picked]
    return pool


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--targets", nargs="+", default=DEFAULT_TARGETS,
                    help="modules or module.function (default: %(default)s)")
    ap.add_argument("--src", type=Path, default=SRC, help="the package mutated (a copy of it)")
    ap.add_argument("--sample", type=int, help="run this many, picked by --seed (default: all)")
    ap.add_argument("--ids", nargs="+", help="run only these mutants (their ids in a summary or mutants.json)")
    ap.add_argument("--seed", default=None, help="the sample's seed (default: the ISO week, e.g. 2026-W42)")
    ap.add_argument("--jobs", type=int, default=1, help="mutants run at once (default 1)")
    ap.add_argument("--timeout", type=float, default=TIMEOUT, help="seconds per mutant (default %(default)s)")
    ap.add_argument("--command", help="the test command, run from --cwd (default: pytest on "
                    + " and ".join(TESTS) + ", stopping at the first failure)")
    ap.add_argument("--cwd", type=Path, default=SERVER)
    ap.add_argument("--equivalents", type=Path, default=EQUIVALENTS)
    ap.add_argument("--out", type=Path, default=Path("mutants.json"))
    ap.add_argument("--summary", type=Path, help="append the Markdown summary here (the workflow's step summary)")
    ap.add_argument("--floor", type=float, default=FLOOR, help="the kill rate under which --check fails")
    ap.add_argument("--check", action="store_true", help="exit 1 when the kill rate is under the floor")
    ap.add_argument("--list", action="store_true", help="list the mutants and stop")
    args = ap.parse_args(argv)

    seed = args.seed or iso_week()
    command = shlex.split(args.command) if args.command else \
        [sys.executable, "-m", "pytest", *TESTS, "-x", "-q", "-p", "no:cacheprovider"]
    found = mutants(args.src, args.targets)
    entries = read_equivalents(args.equivalents)
    keys = equivalent_keys(entries)
    found_keys = {m.key() for m in found}
    stale = [e for e in entries if (e["file"], e["op"], _norm(e["before"]), _norm(e["after"])) not in found_keys]
    chosen = [m for m in found if m.id in args.ids] if args.ids else choose(found, keys, args.sample, seed)
    if args.ids and len(chosen) < len(set(args.ids)):
        raise SystemExit(f"no mutant {', '.join(sorted(set(args.ids) - {m.id for m in chosen}))} (the code moved on?)")
    skipped = sum(m.key() in keys for m in found)
    if args.list:
        for m in chosen:
            print(f"{m.id} {m.file}:{m.line} {m.op}: {_norm(m.before)[:70]} -> {_norm(m.after)[:70]}")
        print(f"{len(chosen)} of {len(found)} mutants ({skipped} equivalent)")
        return 0
    print(f"{len(chosen)} of {len(found)} mutants (seed {seed}, {skipped} equivalent), {args.jobs} at a time",
          flush=True)
    run_all(chosen, args.src, command, args.cwd, args.timeout, args.jobs, lambda line: print(line, flush=True))
    args.out.write_text(json.dumps([m.row() for m in chosen], indent=1) + "\n", encoding="utf-8")
    text = summary(chosen, len(found), skipped, stale, args.floor, seed)
    print(text)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    rate = kill_rate(chosen)
    if args.check and (rate is None or rate < args.floor):
        print(f"kill rate {rate if rate is None else round(rate, 1)} under the floor {args.floor}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
