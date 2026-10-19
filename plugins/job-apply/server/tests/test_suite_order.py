"""The suite's own collection hook (conftest.py): a part of the suite (JOB_APPLY_TEST_SHARD) and, asked to, the slow
tests first (JOB_APPLY_SLOW_FIRST), and its shortened waits (FAST_WAITS). Stand-in tests and config, no browser."""
import importlib
from types import SimpleNamespace

import pytest

from conftest import FAST_WAITS, pytest_collection_modifyitems, slow_first


class StandIn:
    """A collected test as the hook sees it: its id and its marks."""

    def __init__(self, nodeid: str, slow: bool) -> None:
        self.nodeid = nodeid
        self.slow = slow

    def get_closest_marker(self, name: str):
        return SimpleNamespace(name=name) if name == "slow" and self.slow else None


def collected() -> list:
    """200 tests in collection order, every seventh slow (some near the end, as test_pipeline.py's are)."""
    return [StandIn(f"tests/test_x.py::test_{i}[case-{i % 3}]", slow=i % 7 == 3) for i in range(200)]


def run_hook(monkeypatch, shard: str | None, slow: str | None) -> tuple[list, list]:
    """The hook run on a fresh collection: the ids it keeps, in order, and the ids it deselects."""
    for name, value in (("JOB_APPLY_TEST_SHARD", shard), ("JOB_APPLY_SLOW_FIRST", slow)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    deselected: list = []
    config = SimpleNamespace(hook=SimpleNamespace(pytest_deselected=lambda items: deselected.extend(items)))
    items = collected()
    pytest_collection_modifyitems(config, items)
    return [i.nodeid for i in items], [i.nodeid for i in deselected]


EVERY = [i.nodeid for i in collected()]
SLOW = [i.nodeid for i in collected() if i.slow]


@pytest.mark.parametrize("off", [None, "", "0"])
def test_the_slow_tests_go_first_only_when_asked(monkeypatch, off):
    kept, deselected = run_hook(monkeypatch, None, off)
    assert kept == EVERY and not deselected  # (not asked: the order stays as collected)

    kept, deselected = run_hook(monkeypatch, None, "1")
    assert sorted(kept) == sorted(EVERY) and not deselected  # (every test, each once)
    assert kept == SLOW + [i for i in EVERY if i not in SLOW]  # (the slow first, each group in its own order)


@pytest.mark.parametrize("parts", [4, 6])  # (CI's Windows parts and its Linux parts: .github/workflows/tests.yml)
def test_each_part_holds_the_same_tests_with_the_slow_ones_first_or_not(monkeypatch, parts):
    seen: list = []
    for part in range(1, parts + 1):
        plain, plain_deselected = run_hook(monkeypatch, f"{part}/{parts}", None)
        first, first_deselected = run_hook(monkeypatch, f"{part}/{parts}", "1")
        assert sorted(first) == sorted(plain) and first_deselected == plain_deselected, part  # (the same split)
        assert first == [i for i in plain if i in SLOW] + [i for i in plain if i not in SLOW], part
        assert sorted(plain + plain_deselected) == sorted(EVERY), part
        # (each part has both: the check means something)
        assert set(plain) & set(SLOW) and set(plain) - set(SLOW), part
        seen += plain
    assert sorted(seen) == sorted(EVERY)  # (every test in exactly one part)


def test_slow_first_keeps_every_test_and_each_groups_order():
    items = collected()
    ordered = slow_first(items)
    assert sorted(map(id, ordered)) == sorted(map(id, items))
    assert [i.nodeid for i in ordered] == SLOW + [i for i in EVERY if i not in SLOW]


def test_the_slow_marker_is_registered(pytestconfig):
    assert any(line.startswith("slow:") for line in pytestconfig.getini("markers"))


@pytest.mark.real_waits  # (the modules' own values, to compare with)
def test_every_shortened_wait_is_one_its_module_has_and_only_shortened():
    """conftest's FAST_WAITS sets each name on its module: each is a wait its module has (a wait renamed in the code
    is said here by name, not as every test's setup error), a number, and only ever shorter in the tests than it is
    for the person (a fast value that grew past the code's would slow the tests, or hide a wait cut in the code)."""
    for module, waits in FAST_WAITS.items():
        mod = importlib.import_module(f"job_apply.{module}")
        for name, fast in waits.items():
            assert hasattr(mod, name), f"{module}.{name} is in FAST_WAITS but not in {module}.py"
            real = getattr(mod, name)
            assert isinstance(real, (int, float)) and isinstance(fast, (int, float)), (module, name, real)
            assert 0 < fast < real, (module, name, fast, real)
