"""The weekly mutation pass (scripts/mutation_pass.py): its operators, each on a small made-up module; the accepted
survivors it leaves out; and its runner, against a stand-in test command (a few lines of Python, not the suite)."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_spec = importlib.util.spec_from_file_location("mutation_pass", SCRIPTS / "mutation_pass.py")
mp = importlib.util.module_from_spec(_spec)
sys.modules["mutation_pass"] = mp  # (its dataclass looks its module up)
_spec.loader.exec_module(mp)

RULES = '''\
import re


class Answer:
    def __init__(self, value):
        self.value = value


def decide(x, flag):
    """A docstring with a | in it is no pattern."""
    if x == 1 and not flag:
        return Answer("one")
    if x in (2, 3) or flag:
        return Answer("more")
    return None


def other(x):
    return x < 5
'''
NEVER = '''\
import re

WORDS = re.compile(r"alpha|beta(?:x|y)|ga\\|mma|[|]delta")


def found(text):
    return WORDS.search(text) is not None
'''


@pytest.fixture
def package(tmp_path):
    pkg = tmp_path / "src" / "madeup"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "rules.py").write_text(RULES, encoding="utf-8")
    (pkg / "never.py").write_text(NEVER, encoding="utf-8")
    return pkg


def _afters(found, op):
    return [" ".join(m.after.split()) for m in found if m.op == op]


def test_each_operator_makes_its_change_and_only_in_the_function_named(package):
    found = mp.mutants(package, ["rules.decide"])
    assert {m.op for m in found} == {"flip-compare", "negate-if", "drop-not", "swap-boolop", "return-none"}
    assert all(m.file == "rules.py" and 9 <= m.line <= 15 for m in found)  # (never other(), the next function)
    assert _afters(found, "flip-compare") == ["if x != 1 and not flag:", "if x not in (2, 3) or flag:"]
    assert _afters(found, "negate-if") == ["if not (x == 1 and not flag):", "if not (x in (2, 3) or flag):"]
    assert _afters(found, "drop-not") == ["if x == 1 and (flag):"]
    assert _afters(found, "swap-boolop") == ["if (x == 1 or not flag):", "if (x in (2, 3) and flag):"]
    assert _afters(found, "return-none") == ["return None", "return None"]
    assert [m.line for m in found if m.op == "return-none"] == [12, 14]
    # each mutant's source is the module with that one change, and its diff shows it
    m = next(m for m in found if m.op == "drop-not")
    assert mp._apply(RULES, m.edits) == RULES.replace("and not flag", "and (flag)")
    assert "-    if x == 1 and not flag:\n+    if x == 1 and (flag):" in m.diff()


def test_a_whole_module_is_mutated_when_no_function_is_named(package):
    found = mp.mutants(package, ["rules"])
    assert any(m.line == 19 and m.after.strip() == "return x >= 5" for m in found)


def test_a_pattern_loses_one_top_level_alternative_at_a_time_in_never_only(package):
    found = [m for m in mp.mutants(package, ["never"]) if m.op == "drop-alternative"]
    # (a group's own |, an escaped \\| and a class's [|] are no alternatives of the pattern's)
    assert _afters(found, "drop-alternative") == [
        r'WORDS = re.compile(r"beta(?:x|y)|ga\|mma|[|]delta")',
        r'WORDS = re.compile(r"alpha|ga\|mma|[|]delta")',
        r'WORDS = re.compile(r"alpha|beta(?:x|y)|[|]delta")',
        r'WORDS = re.compile(r"alpha|beta(?:x|y)|ga\|mma")']
    assert not [m for m in mp.mutants(package, ["rules"]) if m.op == "drop-alternative"]  # (never.py's patterns only)
    assert mp._alternatives(r"a(?:b|c)|[|]|\||d") == ["a(?:b|c)", "[|]", r"\|", "d"]


def test_an_and_or_swap_keeps_its_own_parts_grouped():
    source = "def f(a, b, c):\n    return a or b and c\n"
    tree = mp.ast.parse(source)
    swaps = [mp._apply(source, edits) for node, op, edits, _ in mp._points(tree, source, regex=False)
             if op == "swap-boolop"]
    # (the or made and: "a and (b and c)", never "a and b and c"; the and made or: "a or (b or c)")
    assert sorted(s.splitlines()[1].strip() for s in swaps) == ["return (a and (b and c))", "return a or (b or c)"]


def test_unknown_targets_are_refused(package):
    with pytest.raises(SystemExit, match="no function"):
        mp.mutants(package, ["rules.nothing"])
    with pytest.raises(SystemExit, match="no module"):
        mp.mutants(package, ["nothing"])


def _equivalent(m, reason="changes nothing a form can see"):
    before = m.before.strip().splitlines()
    after = m.after.strip().splitlines()
    lines = [f"file: {m.file}", f"op: {m.op}", f"before: {before[0]}", *[f"  {x}" for x in before[1:]],
             f"after: {after[0]}", *[f"  {x}" for x in after[1:]], f"reason: {reason}"]
    return "\n".join(lines) + "\n"


def test_the_accepted_survivors_are_left_out_and_matched_by_their_text_not_their_line(package, tmp_path):
    found = mp.mutants(package, ["rules.decide"])
    accepted = next(m for m in found if m.op == "drop-not")
    path = tmp_path / "equivalents.txt"
    path.write_text("# accepted survivors\n\n" + _equivalent(accepted), encoding="utf-8")
    entries = mp.read_equivalents(path)
    assert entries[0]["reason"] == "changes nothing a form can see"
    chosen = mp.choose(found, mp.equivalent_keys(entries), None, "seed")
    assert accepted not in chosen and len(chosen) == len(found) - 1
    # (lines added above it move it, and it's still the one accepted)
    (package / "rules.py").write_text("\n\n\n" + RULES, encoding="utf-8")
    moved = mp.mutants(package, ["rules.decide"])
    assert [m for m in moved if m.key() in mp.equivalent_keys(entries)][0].line == accepted.line + 3
    # an entry lacking its reason is refused
    path.write_text(_equivalent(accepted).replace("reason: changes nothing a form can see\n", ""), encoding="utf-8")
    with pytest.raises(SystemExit, match="reason"):
        mp.read_equivalents(path)


def test_the_repositorys_equivalents_file_reads_and_each_entry_has_a_reason():
    for entry in mp.read_equivalents(mp.EQUIVALENTS):
        assert entry["reason"] and entry["op"] in mp.OPS and entry["file"] in ("autofill.py", "never.py"), entry


def test_a_sample_is_the_same_for_the_same_seed_and_a_week_names_it(package):
    found = mp.mutants(package, ["rules.decide", "never"])
    one, two = mp.choose(found, set(), 4, "2026-W42"), mp.choose(found, set(), 4, "2026-W42")
    assert len(one) == 4 and [m.id for m in one] == [m.id for m in two]
    assert len(mp.choose(found, set(), 100, "x")) == len(found)
    assert mp.iso_week(mp.datetime.date(2026, 10, 18)) == "2026-W42"


# The stand-in for the tests: decide() must answer "one" for (1, False) and nothing for (5, False); a mutant that
# makes the second answer "slow" sleeps past the timeout
CHECK = '''\
import sys, time
from madeup.rules import decide
a, b = decide(1, False), decide(5, False)
if b is not None and b.value == "slow":
    time.sleep(30)
sys.exit(0 if a is not None and a.value == "one" and b is None else 1)
'''


def test_the_runner_records_killed_and_survived_and_never_touches_the_package(package, tmp_path):
    check = tmp_path / "check.py"
    check.write_text(CHECK, encoding="utf-8")
    found = mp.mutants(package, ["rules.decide"])
    mp.run_all(found, package, [sys.executable, str(check)], tmp_path, timeout=20, jobs=2)
    # (neither (1, False) nor (5, False) tells "x in (2, 3) or flag" from "... and flag", nor reaches Answer("more"))
    assert [(m.op, m.line) for m in found if m.status == "survived"] == [("swap-boolop", 13), ("return-none", 14)]
    assert {m.status for m in found} == {"killed", "survived"} and len(found) == 9
    assert (package / "rules.py").read_text(encoding="utf-8") == RULES  # (only a copy was mutated)
    assert mp.kill_rate(found) == pytest.approx(100 * sum(m.status == "killed" for m in found) / len(found))


def test_a_mutant_that_runs_too_long_is_a_timeout(package, tmp_path):
    (package / "rules.py").write_text(RULES.replace('Answer("more")', 'Answer("slow")'), encoding="utf-8")
    check = tmp_path / "check.py"
    check.write_text(CHECK, encoding="utf-8")
    found = [m for m in mp.mutants(package, ["rules.decide"]) if m.op == "flip-compare" and m.line == 13]
    mp.run_all(found, package, [sys.executable, str(check)], tmp_path, timeout=3, jobs=1)
    assert [m.status for m in found] == ["timeout"]  # (x not in (2, 3): decide(5, False) is "slow")
    assert mp.kill_rate(found) is None


def test_the_runner_refuses_when_the_tests_fail_without_a_mutant(package, tmp_path):
    with pytest.raises(SystemExit, match="fail without any mutant"):
        mp.run_all([], package, [sys.executable, "-c", "raise SystemExit(1)"], tmp_path, timeout=20, jobs=1)


def test_the_command_line_writes_the_rows_and_a_summary_and_checks_the_floor(package, tmp_path):
    check = tmp_path / "check.py"
    check.write_text(CHECK, encoding="utf-8")
    out, summary, equivalents = tmp_path / "mutants.json", tmp_path / "summary.md", tmp_path / "eq.txt"
    found = mp.mutants(package, ["rules.decide"])
    stale = _equivalent(found[0]).replace("before: ", "before: gone ")
    equivalents.write_text(stale, encoding="utf-8")
    # (no backslash for shlex to read, on Windows)
    command = f'"{Path(sys.executable).as_posix()}" "{check.as_posix()}"'
    args = ["--src", str(package), "--targets", "rules.decide", "--command", command,
            "--cwd", str(tmp_path), "--out", str(out), "--summary", str(summary), "--equivalents", str(equivalents),
            "--jobs", "2"]
    assert mp.main([*args, "--check", "--floor", "100"]) == 1  # (a survivor: under a floor of 100%)
    rows = json.loads(out.read_text(encoding="utf-8"))
    assert {tuple(sorted(r)) for r in rows} == {("after", "before", "file", "id", "line", "op", "seconds", "status")}
    assert {r["status"] for r in rows} == {"killed", "survived"}
    text = summary.read_text(encoding="utf-8")
    assert "### Survivors" in text and "```diff" in text and "+    if (x in (2, 3) and flag):" in text
    assert "Equivalents no mutant matches any more" in text  # (the stale entry)
    assert mp.main([*args, "--check", "--floor", "10"]) == 0
    assert mp.main([*args, "--list"]) == 0


def test_the_default_targets_name_functions_the_desk_has():
    for target in mp.DEFAULT_TARGETS:
        module, _, func = target.partition(".")
        tree = mp.ast.parse((mp.SRC / f"{module}.py").read_text(encoding="utf-8"))
        assert mp._scope(tree, func or None) is not None, target
    found = mp.mutants(mp.SRC, ["autofill._answer_bank", "never"])  # (the small ones: all of them take seconds)
    assert {m.file for m in found} == {"autofill.py", "never.py"}
    assert {m.op for m in found} == set(mp.OPS)
