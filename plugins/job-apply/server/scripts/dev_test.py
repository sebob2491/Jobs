"""The tests for what changed, while working: the files changed since this branch left main (its merge-base with
origin/main, committed or not, and new files git doesn't ignore), each to the test files that read it, run with
`pytest -n 8 --ff` (the last run's failures first). The PR's required checks still run the whole suite.

    uv run python scripts/dev_test.py              # the tests that import a changed module directly
    uv run python scripts/dev_test.py --deep       # and those importing a module that imports it, and so on
    uv run python scripts/dev_test.py --all        # the whole suite
    uv run python scripts/dev_test.py --list       # which test files, without running them
    uv run python scripts/dev_test.py --lf -x      # anything else goes to pytest as it is

What reads what: a module (src/job_apply/X.py; the browser_*.py and pipeline_*.py mixins count as browser.py and
pipeline.py) to the tests that import it; scripts/X.py to tests/test_X.py and the tests that import it or name X.py;
a test file to itself and the tests that import it; the desk's page (static/) to test_desk.py and test_pipeline.py;
data/ to the tests that read the plugin's data; any other file of the plugin's (a fixture, a template, a skill) to the
tests that name it.
tests/conftest.py, pyproject.toml and uv.lock run everything. Files outside the plugin run nothing.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
PLUGIN = SERVER.parent

# What runs the whole suite (paths from the plugin's folder)
EVERYTHING = {"server/tests/conftest.py", "server/pyproject.toml", "server/uv.lock"}
# The desk's page has no import: the tests that load it
STATIC_TESTS = {"tests/test_desk.py", "tests/test_pipeline.py"}
# A test that reads the plugin's data (data/companies.yaml, data/lists/, question_bank.yaml, desk_handled.yaml)
DATA_WORDS = re.compile(r"""["'/]data["'/]|\bdata/|companies\.yaml|question_bank|desk_handled|QUESTION_BANK|"""
                        r"""\bHANDLED\b|las-vegas|phoenix-metro|semiconductor-az""")

_FROM_PACKAGE = re.compile(r"^[ \t]*from[ \t]+(?:job_apply|\.)[ \t]+import[ \t]+(\([^)]*\)|[^\n#]+)", re.M)
_FROM_MODULE = re.compile(r"^[ \t]*from[ \t]+(?:job_apply\.|\.)(\w+)[ \t]+import\b", re.M)
_IMPORT_MODULE = re.compile(r"^[ \t]*import[ \t]+job_apply\.(\w+)", re.M)
_BARE = re.compile(r"^[ \t]*(?:import|from)[ \t]+(\w+)\b", re.M)


def module_of(path: str) -> str:
    """The module a source file is, by name ("browser" for browser.py and its browser_*.py mixins, "pipeline" for
    pipeline.py and its pipeline_*.py)."""
    name = Path(path).stem
    return next((m for m in ("browser", "pipeline") if name.startswith(m + "_")), name)


def imports(text: str) -> set[str]:
    """The modules a file imports by name: job_apply's as "job_apply.<name>" (absolute or, inside the package,
    relative; a mixin as its class's module), any other as its bare name (a script, or a test module beside it)."""
    found = {"job_apply." + module_of(m) for m in _FROM_MODULE.findall(text) + _IMPORT_MODULE.findall(text)}
    for names in _FROM_PACKAGE.findall(text):
        for part in names.strip("()").split(","):
            if word := part.split(" as ")[0].strip():
                found.add("job_apply." + module_of(word))
    found |= {m for m in _BARE.findall(text) if m not in ("job_apply", "__future__")}
    return found


def key_of(path: str) -> str | None:
    """A changed Python file's import name (paths from the plugin's folder), or None for anything else."""
    if not path.endswith(".py"):
        return None
    if path.startswith("server/src/job_apply/"):
        return "job_apply." + module_of(path)
    if path.startswith(("server/scripts/", "server/tests/")) and path.count("/") == 2:
        return Path(path).stem
    return None


def importers(keys: set[str], modules: dict[str, str]) -> set[str]:
    """`keys` and every module that imports one of them, and so on: the reverse import closure. `modules`: each
    source module and script by its import name, to its text."""
    seen, todo = set(keys), list(keys)
    graph = {name: imports(text) for name, text in modules.items()}
    while todo:
        key = todo.pop()
        for name, uses in graph.items():
            if key in uses and name not in seen:
                seen.add(name)
                todo.append(name)
    return seen


def tests_for(changed: list[str], tests: dict[str, str], modules: dict[str, str] | None = None,
              deep: bool = False) -> list[str] | None:
    """The test files (as "tests/test_x.py") that read the changed files, or None for the whole suite.

    changed: paths from the plugin's folder ("server/src/job_apply/config.py", "data/companies.yaml"); tests: each test
    file and test helper module under tests/ ("tests/test_x.py") to its text; modules: the source modules and scripts
    by import name to their text, for `deep`."""
    if any(path in EVERYTHING for path in changed):
        return None
    wanted: set[str] = set()
    keys: set[str] = set()
    for path in changed:
        server_path = path.removeprefix("server/")
        if path.startswith("server/tests/") and server_path in tests:
            wanted.add(server_path)
        if path.startswith("server/scripts/") and (own := f"tests/test_{Path(path).stem}.py") in tests:
            wanted.add(own)
        if (key := key_of(path)) is not None:
            keys.add(key)
        elif path.startswith("server/src/job_apply/static/"):
            wanted |= STATIC_TESTS & set(tests)
        elif path.startswith("data/"):
            wanted |= {name for name, text in tests.items() if DATA_WORDS.search(text)}
        elif not path.startswith("../"):
            named = Path(path).name
            wanted |= {name for name, text in tests.items() if named in text}
            if path.startswith("server/tests/fixtures/") and path.count("/") > 3:
                # (a fixture folder's files, read by a glob of the folder: tests/fixtures/live/*.html)
                sub = re.escape(path.split("/")[3])
                folder = re.compile(rf"""fixtures/{sub}\b|["']{sub}["']""")
                wanted |= {name for name, text in tests.items() if folder.search(text)}
    if deep and modules:
        keys = importers(keys, modules)
    for name, text in tests.items():
        if keys & imports(text):
            wanted.add(name)
        elif any(re.search(rf"\b{re.escape(key)}\.py\b", text) for key in keys if not key.startswith("job_apply.")):
            wanted.add(name)  # (a script loaded from its file: importlib, or a subprocess)
    return sorted(name for name in wanted if Path(name).name.startswith("test_"))


def _read(folder: Path, pattern: str) -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8") for p in sorted(folder.glob(pattern))}


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          check=True).stdout


def changed_files(base: str) -> list[str]:
    """The files changed since this branch left `base` (committed, staged, unstaged and new), from the plugin's
    folder; a file outside it as "../<its path from the repository's root>"."""
    top = Path(_git("rev-parse", "--show-toplevel", cwd=SERVER).strip())
    try:
        since = _git("merge-base", "HEAD", base, cwd=top).strip()
    except subprocess.CalledProcessError:
        print(f"(no merge-base with {base}: the changes since the last commit only)", file=sys.stderr)
        since = "HEAD"
    paths = _git("diff", "--name-only", since, cwd=top).splitlines()
    paths += _git("ls-files", "--others", "--exclude-standard", cwd=top).splitlines()
    plugin = PLUGIN.resolve().relative_to(top.resolve()).as_posix() + "/"
    return sorted({p.removeprefix(plugin) if p.startswith(plugin) else "../" + p for p in paths if p})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The tests for what changed since this branch left main.",
                                     epilog="Any other argument goes to pytest (--lf, -x, -k, ...).")
    parser.add_argument("--deep", action="store_true", help="also the tests of the modules importing a changed one")
    parser.add_argument("--all", action="store_true", help="the whole suite")
    parser.add_argument("--list", action="store_true", help="say which test files, without running them")
    parser.add_argument("--base", default="origin/main", help="the branch this one left (default: origin/main)")
    args, extra = parser.parse_known_args(argv)

    targets: list[str] | None = None
    if not args.all:
        changed = changed_files(args.base)
        tests = {p.relative_to(SERVER).as_posix(): text for p, text in _read(SERVER / "tests", "*.py").items()}
        modules: dict[str, str] = {}
        # (a mixin's imports are its class's module's)
        for p, text in _read(SERVER / "src" / "job_apply", "*.py").items():
            key = "job_apply." + module_of(p.name)
            modules[key] = modules.get(key, "") + text
        modules |= {p.stem: text for p, text in _read(SERVER / "scripts", "*.py").items()}
        targets = tests_for(changed, tests, modules, deep=args.deep)
        print("Changed: " + (", ".join(changed) or "nothing"))
    if targets is None:
        print("Tests: the whole suite")
        targets = ["tests"]
    elif not targets:
        print("Tests: none read the changed files (--all runs the whole suite)")
        return 0
    else:
        print(f"Tests ({len(targets)}): " + " ".join(Path(t).name for t in targets))
    if args.list:
        return 0
    command = [sys.executable, "-m", "pytest", "-q", "-n", "8", "--ff", *targets, *extra]
    return subprocess.run(command, cwd=SERVER).returncode


if __name__ == "__main__":
    sys.exit(main())
