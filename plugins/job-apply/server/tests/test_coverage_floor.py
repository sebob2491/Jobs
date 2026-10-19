"""The weekly coverage job's check (scripts/coverage_floor.py): each module's line coverage against its floor, and the
functions with the most lines never run. The coverage reports here are made up, in coverage.py's JSON form."""

import importlib.util
import json
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("coverage_floor", SERVER / "scripts" / "coverage_floor.py")
cf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cf)

ALPHA = '''import os


def top(x):
    if x:
        return 1
    return 2


class Box:
    @property
    def open(self):
        a = 1
        b = 2

        def inner():
            return a + b
        return inner
'''
ALPHA_RAN = [1, 4, 5, 7, 10, 11, 12, 13, 14, 16, 18]  # (11: the decorator, where a decorated def's line is counted)
ALPHA_MISSED = [6, 17]


def report(tmp_path: Path) -> dict:
    (tmp_path / "src" / "job_apply").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "job_apply" / "alpha.py").write_text(ALPHA, encoding="utf-8")

    def file(ran: list[int], missed: list[int]) -> dict:
        return {"executed_lines": ran, "missing_lines": missed, "excluded_lines": [],
                "summary": {"covered_lines": len(ran), "num_statements": len(ran) + len(missed),
                            "percent_covered": 100.0 * len(ran) / max(1, len(ran) + len(missed)),
                            "missing_lines": len(missed), "excluded_lines": 0}}

    return {"meta": {"format": 3, "branch_coverage": False},
            "files": {"src/job_apply/alpha.py": file(ALPHA_RAN, ALPHA_MISSED),  # 11 of 13: 84.6%
                      "src/job_apply/beta.py": file([1, 2, 3], []),
                      "src/job_apply/__init__.py": file([], [])},
            "totals": {"covered_lines": 14, "num_statements": 16, "percent_covered": 87.5}}


def test_a_module_under_its_floor_fails_the_check(tmp_path):
    data = report(tmp_path)
    lines, under = cf.check(data, {"job_apply.alpha": 82.6, "job_apply.beta": 98.0, "job_apply": 0.0}, tmp_path)
    assert under == [], lines
    lines, under = cf.check(data, {"job_apply.alpha": 85.0, "job_apply.beta": 98.0, "job_apply": 0.0}, tmp_path)
    assert under == ["job_apply.alpha"]
    text = "\n".join(lines)
    assert ("**Under the floor:** job_apply.alpha." in text
            and "| job_apply.alpha | 84.6% | 13 | 85.0% | under the floor |" in text), text


def test_a_module_without_a_floor_is_named_not_failed(tmp_path):
    lines, under = cf.check(report(tmp_path), {"job_apply.alpha": 50.0, "job_apply.gone": 90.0}, tmp_path)
    text = "\n".join(lines)
    assert under == []
    assert "No floor yet (not failed; `--suggest` gives one): job_apply, job_apply.beta." in text, text
    assert "Floors for modules the report doesn't have (renamed or removed? take them out): job_apply.gone." in text


def test_unrun_lines_are_counted_in_the_innermost_function_around_them(tmp_path):
    """A nested function's lines are its own, not the method's around it; a def's line and its decorator are the
    enclosing scope's (run when the module loads)."""
    found = cf.unrun_functions(report(tmp_path), tmp_path)
    assert [(u.module, u.function, u.first, u.last, u.unrun, u.statements) for u in found] == [
        ("job_apply.alpha", "top", 4, 7, 1, 3),
        ("job_apply.alpha", "Box.open.<locals>.inner", 16, 17, 1, 1)]
    lines, _ = cf.check(report(tmp_path), {}, tmp_path, top=1)
    table = lines[lines.index("### The 1 functions with the most lines never run"):]
    assert table[4] == "| job_apply.alpha.top | 4-7 | 1 | 3 |" and table[5] == "", table


def test_the_check_writes_the_jobs_summary_and_fails_its_run(tmp_path, monkeypatch, capsys):
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(report(tmp_path)), encoding="utf-8")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(cf, "FLOORS", {"job_apply.alpha": 90.0})
    assert cf.main([str(path), "--root", str(tmp_path)]) == 1
    assert "**Under the floor:** job_apply.alpha." in summary.read_text(encoding="utf-8")
    assert "**Under the floor:**" in capsys.readouterr().out
    monkeypatch.setattr(cf, "FLOORS", {"job_apply.alpha": 80.0})
    assert cf.main([str(path), "--root", str(tmp_path)]) == 0
    assert cf.main([str(path), "--suggest"]) == 0
    assert "    'job_apply.alpha': 82.6," in capsys.readouterr().out


def test_each_floor_names_a_module_that_exists():
    """A floor for a misspelled module would never be checked."""
    src = SERVER / "src"
    assert cf.FLOORS, "no floors"
    for module, floor in cf.FLOORS.items():
        path = src.joinpath(*module.split("."))
        assert path.with_suffix(".py").is_file() or (path / "__init__.py").is_file(), module
        assert 0 <= floor <= 100, (module, floor)
