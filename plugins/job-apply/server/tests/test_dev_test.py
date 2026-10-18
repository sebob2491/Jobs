"""The local test loop (scripts/dev_test.py): changed files to the test files that read them. Stand-in files and
changes: no git, no pytest run."""

import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_spec = importlib.util.spec_from_file_location("dev_test", SCRIPTS / "dev_test.py")
dt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dt)

TESTS = {
    "tests/test_config.py": "from job_apply import config\n",
    "tests/test_autofill.py": "from job_apply.autofill import resolve_field\nfrom job_apply import config\n",
    "tests/test_browser_e2e.py": ("import job_apply.browser as browser_module\n"
                                  "def test_x():\n    page = fixture_url('generic_form.html')\n"),
    "tests/test_pipeline.py": "from job_apply import (\n    config,\n    pipeline as pl,\n)\n",
    "tests/test_desk.py": "def _client():\n    from job_apply.desk import Desk\n",
    "tests/test_report.py": "from test_desk import _client\nfrom job_apply import report\n",
    "tests/test_live_smoke.py": "import sys\nsys.path.insert(0, 'scripts')\nimport live_smoke\n",
    "tests/test_flake_rate.py": "spec_from_file_location('flake_rate', SCRIPTS / 'flake_rate.py')\n",
    "tests/test_data_files.py": "QUESTION_BANK = PLUGIN / 'data' / 'question_bank.yaml'\n",
    "tests/test_live_fixtures.py": "PAGES = sorted((FIXTURES / 'live').glob('*.html'))\n",
    "tests/test_setup.py": "TEMPLATE = PLUGIN / 'templates' / 'profile.example.yaml'\n",
    "tests/conftest.py": "from job_apply import config, browser\n",
}
MODULES = {
    "job_apply.config": "import os\n",
    "job_apply.autofill": "from . import config\n",
    "job_apply.browser": "from .autofill import choose_option\nfrom . import config\n",
    "job_apply.pipeline": "from .browser import BrowserSession\n",
    "job_apply.desk": "from . import pipeline\n",
    "job_apply.report": "from . import config\n",
    "live_smoke": "from job_apply import server\nimport live_compare\n",
    "live_compare": "import json\n",
}


def names(found):
    return None if found is None else [Path(f).stem for f in found]


@pytest.mark.parametrize("changed, expected", [
    # a module: the tests that import it, however they spell it (a "from job_apply import (...)" over lines too)
    (["server/src/job_apply/config.py"], ["test_autofill", "test_config", "test_pipeline"]),
    (["server/src/job_apply/pipeline.py"], ["test_pipeline"]),
    (["server/src/job_apply/desk.py"], ["test_desk"]),  # (an import inside a test function counts)
    # a mixin is browser.py's
    (["server/src/job_apply/browser_menus.py"], ["test_browser_e2e"]),
    # a script: its own test file and the tests that import it or name its file
    (["server/scripts/live_smoke.py"], ["test_live_smoke"]),
    (["server/scripts/flake_rate.py"], ["test_flake_rate"]),
    (["server/scripts/live_compare.py"], []),  # (not deep: only the tests importing it directly)
    # a test file: itself, and the tests that import from it
    (["server/tests/test_desk.py"], ["test_desk", "test_report"]),
    (["server/tests/test_config.py"], ["test_config"]),
    # the desk's page, the plugin's data, a fixture (by name, or its folder's), a template
    (["server/src/job_apply/static/desk.html"], ["test_desk", "test_pipeline"]),
    (["data/companies.yaml"], ["test_data_files"]),
    (["data/lists/las-vegas.yaml"], ["test_data_files"]),
    (["server/tests/fixtures/generic_form.html"], ["test_browser_e2e"]),
    (["server/tests/fixtures/live/acme_workday.html"], ["test_live_fixtures"]),
    (["templates/profile.example.yaml"], ["test_setup"]),
    # files outside the plugin, and ones no test reads, run nothing
    (["../README.md", "../.github/workflows/tests.yml"], []),
    (["skills/desk/SKILL.md"], []),
    # several at once: the union, each once
    (["server/src/job_apply/pipeline.py", "server/tests/test_desk.py", "data/companies.yaml"],
     ["test_data_files", "test_desk", "test_pipeline", "test_report"]),
    # what everything rests on: the whole suite
    (["server/tests/conftest.py"], None),
    (["server/pyproject.toml", "server/src/job_apply/config.py"], None),
    (["server/uv.lock"], None),
])
def test_changed_files_to_the_tests_that_read_them(changed, expected):
    assert names(dt.tests_for(changed, TESTS, MODULES)) == expected


@pytest.mark.parametrize("changed, expected", [
    # browser <- pipeline <- desk: the tests of each, and test_report through test_desk's import
    (["server/src/job_apply/browser_uploads.py"], ["test_browser_e2e", "test_desk", "test_pipeline"]),
    # config is imported by autofill, browser and report too, so by everything above them
    (["server/src/job_apply/config.py"],
     ["test_autofill", "test_browser_e2e", "test_config", "test_desk", "test_pipeline", "test_report"]),
    # a script imported by another script: that one's tests too
    (["server/scripts/live_compare.py"], ["test_live_smoke"]),
])
def test_deep_adds_the_tests_of_the_modules_importing_a_changed_one(changed, expected):
    assert names(dt.tests_for(changed, TESTS, MODULES, deep=True)) == expected


def test_what_a_file_imports():
    text = ("from __future__ import annotations\nimport os\nfrom . import browser  # noqa\n"
            "from .formjs import (A, B)\nfrom job_apply.browser_menus import x\nimport job_apply.render as render\n"
            "from job_apply import config as cfg, tracker\n    import live_smoke\n")
    assert dt.imports(text) == {"os", "live_smoke", "job_apply.browser", "job_apply.formjs", "job_apply.render",
                                "job_apply.config", "job_apply.tracker"}


def test_a_mixins_module_is_browser():
    assert dt.module_of("src/job_apply/browser_tabs.py") == "browser" == dt.module_of("browser.py")
    assert dt.module_of("src/job_apply/formjs.py") == "formjs"
