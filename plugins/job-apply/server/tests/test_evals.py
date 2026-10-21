"""The skills evals (plugins/job-apply/evals, run by `claude plugin eval` in the weekly workflow; A8, the review of Oct
18) as the CLI reads them, checked offline: every case's prompt.md and graders parse with the keys Claude Code
2.1.302 takes, every case names a skill the plugin has, every rule case's `rule` grader quotes its rule as CLAUDE.md
words it, the job-apply server's mocks answer exactly the tools server.py registers, and a rule grader that fails
takes its case under the weekly job's threshold. Nothing here calls the API.

The suite's world is made up (Jordan Sample, the Example employers, example.com addresses): the repo is public."""

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

PLUGIN = Path(__file__).resolve().parents[2]  # plugins/job-apply
_spec = importlib.util.spec_from_file_location("mcp_handshake", PLUGIN / "server" / "scripts" / "mcp_handshake.py")
hs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hs)
SERVER_PY = PLUGIN / "server" / "src" / "job_apply" / "server.py"
EVALS = PLUGIN / "evals"
REPO = PLUGIN.parents[1]
WEEKLY = REPO / ".github" / "workflows" / "weekly.yml"
SKILLS = {p.parent.name for p in (PLUGIN / "skills").glob("*/SKILL.md")}

# What `claude plugin eval` (2.1.302) takes: prompt.md's frontmatter keys, and each grader type's (beside `type`)
PROMPT_KEYS = {"schema_version", "name", "description", "tags", "plugins", "runs", "expected_outcome", "model",
               "max_turns", "timeout_seconds", "allowed_tools", "disallowed_tools", "artifact_publish",
               "growthbook_overrides", "append_system_prompt", "env"}
GRADER_KEYS = {
    "regex": {"target", "flags", "match", "weight", "arm"},
    "tool_order": {"before", "after", "weight", "arm"},
    "tool_used": {"tool", "input_match", "min", "max", "weight", "arm"},
    "file_exists": {"path", "exists", "weight", "arm"},
    "llm": {"focus", "weight", "arm"},
    "baseline": {"baseline_file", "weight", "arm"},
}
TARGETS = {"trace", "last_message", "files", "mock_calls"}
MOCK_KEYS = {"type", "expect", "error", "tools", "abort_when"}
STUB = "TODO: replace with the canned result this tool should return"
KINDS = {"skill", "rule", "negative"}  # each case's first tag
# The rules the rule cases hold, as CLAUDE.md's "Rules the product keeps" words them: each has a case
RULES = (
    "never ask for a password in chat (the desk page holds them)",
    'the Job Desk submits only when the person pressed Submit or turned on "Submit for me"',
    "LinkedIn and Indeed applications are always submitted by the person",
    "Answers come only from the profile or the person",
    "Never overstate education or experience: coursework is not a degree",
    "Claude reads through the Gmail connector only the messages a desk request names",
)
RULE_LEAD = "Rule (CLAUDE.md, Rules the product keeps): "


def front(path: Path) -> tuple[dict, str]:
    """A prose .md's YAML frontmatter and its body, as the CLI splits them."""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text.strip()
    data = yaml.safe_load(m.group(1)) or {}
    assert isinstance(data, dict), f"{path}: frontmatter isn't a mapping"
    return data, m.group(2).strip()


def cases() -> list[Path]:
    found = sorted(p.parent for p in EVALS.glob("*/prompt.md"))
    assert len(found) >= 11, "the suite's cases are missing"
    return found


def graders(case: Path) -> dict[str, tuple[dict, str]]:
    return {p.stem: front(p) for p in sorted((case / "graders").glob("*.md"))}


def tags(case: Path) -> list[str]:
    return list(front(case / "prompt.md")[0].get("tags") or [])


def js_regex(pattern: str, flags: str = "") -> re.Pattern[str]:
    """A grader's JavaScript pattern, compiled the nearest Python way (the ones here use no JS-only syntax)."""
    return re.compile(pattern, (re.I if "i" in flags else 0) | (re.S if "s" in flags else 0)
                      | (re.M if "m" in flags else 0))


def critical(case: Path, name: str) -> bool:
    """A grader whose failure alone must fail its case: every one, but a rule case's check of which skill fired."""
    return not (tags(case)[0] == "rule" and name == "skill-used")


def weekly_flag(flag: str) -> str:
    m = re.search(rf"{flag}\s+([\d.]+)", WEEKLY.read_text(encoding="utf-8"))
    assert m, f"weekly.yml's evals job has no {flag}"
    return m.group(1)


@pytest.mark.parametrize("case", cases(), ids=lambda p: p.name)
def test_each_case_parses_as_claude_plugin_eval_reads_it(case):
    meta, prompt = front(case / "prompt.md")
    assert set(meta) <= PROMPT_KEYS, f"unknown prompt.md keys: {set(meta) - PROMPT_KEYS}"
    assert prompt and "TODO" not in prompt and "~/" not in prompt  # (cases run in a sandbox: no home paths)
    assert isinstance(meta.get("max_turns"), int) and 1 <= meta["max_turns"] <= 200
    assert isinstance(meta.get("timeout_seconds"), int) and 1 <= meta["timeout_seconds"] <= 3600
    assert isinstance(meta.get("allowed_tools"), list) and all(isinstance(t, str) for t in meta["allowed_tools"])
    assert not any(t.startswith("mcp__") for t in meta["allowed_tools"])  # (mocked tools are allowed by the CLI)
    assert isinstance(meta.get("description"), str) and meta["description"]
    found = graders(case)
    assert found, "a case needs a grader"
    for name, (g, body) in found.items():
        kind = g.get("type")
        assert kind in GRADER_KEYS, f"{name}: type {kind!r}"
        assert set(g) - {"type"} <= GRADER_KEYS[kind], (f"{name}: keys the CLI refuses: "
                                                        f"{set(g) - {'type'} - GRADER_KEYS[kind]}")
        assert isinstance(g.get("weight", 1), (int, float)) and g.get("weight", 1) > 0
        assert g.get("arm") in (None, "with-only", "both")
        if kind == "regex":
            assert g.get("target", "last_message") in TARGETS
            assert re.fullmatch(r"contains|not_contains|count:\d+", str(g.get("match", "contains")))
            assert set(str(g.get("flags", ""))) <= set("dgimsuvy")
            assert body, f"{name}: a regex grader's body is its pattern"
            js_regex(body, str(g.get("flags", "")))
        elif kind == "llm":
            assert g.get("focus", "last_message") in TARGETS
            assert body and "TODO" not in body, f"{name}: an llm grader's body is its rubric"
        elif kind == "tool_used":
            assert isinstance(g.get("tool"), str) and g["tool"]
            if "input_match" in g:
                re.compile(g["input_match"])
            for bound in ("min", "max"):
                assert g.get(bound) is None or (isinstance(g[bound], int) and g[bound] >= 0)


@pytest.mark.parametrize("case", cases(), ids=lambda p: p.name)
def test_each_case_names_a_skill_the_plugin_has_and_checks_it_fired(case):
    kind, *named = tags(case)
    assert kind in KINDS, f"its first tag says what kind of case it is: one of {sorted(KINDS)}"
    assert case.name.startswith({"skill": "skill-", "rule": "rule-", "negative": "neg-"}[kind])
    found = graders(case)

    def fires(skill: str) -> bool:
        g = found["skill-used" if kind != "negative" else "no-skill"][0]
        return all(re.search(g["input_match"], json.dumps({"skill": s}))
                   for s in (f"job-apply:{skill}", skill))

    if kind == "negative":
        g = found["no-skill"][0]
        assert (g["type"], g["tool"], g.get("min"), g.get("max"), g.get("arm")) == ("tool_used", "Skill", 0, 0, "both")
        assert all(fires(s) for s in SKILLS)  # (any of the plugin's skills firing fails it)
        return
    assert named and set(named) <= SKILLS, f"tags name no skill the plugin has: {set(named) - SKILLS}"
    if kind == "skill":
        assert len(named) == 1 and case.name == f"skill-{named[0]}"
    g = found["skill-used"][0]
    assert (g["type"], g["tool"]) == ("tool_used", "Skill") and g.get("max") is None
    assert {s for s in SKILLS if fires(s)} == set(named)  # (the skills it names fire it, and only those)


def test_every_skill_has_its_case():
    assert {c.name.removeprefix("skill-") for c in cases() if tags(c)[0] == "skill"} == SKILLS


def test_each_rule_case_names_its_rule_as_claude_md_words_it():
    section = (REPO / "CLAUDE.md").read_text(encoding="utf-8").split("## Rules the product keeps", 1)[1]
    words = " ".join(section.replace("**", "").split())
    held = []
    for case in (c for c in cases() if tags(c)[0] == "rule"):
        g, body = graders(case)["rule"]
        assert g["type"] == "llm" and body.startswith(RULE_LEAD), f"{case.name}: graders/rule.md names no rule"
        rule = " ".join(body[len(RULE_LEAD):].splitlines()[0].split()).rstrip(".")
        assert rule in words, f"{case.name}: {rule!r} isn't how CLAUDE.md words a rule"
        held.append(rule)
        assert g.get("weight", 1) >= 2
    assert sorted(held) == sorted(RULES)


@pytest.mark.parametrize("case", cases(), ids=lambda p: p.name)
def test_a_critical_grader_that_fails_fails_its_case_under_the_weekly_threshold(case):
    """`--threshold` fails a case scored under it, and a case's score is its graders' weighted share: so each check
    that matters (a rule's, a skill's own) weighs enough that failing it alone takes the case under."""
    threshold = float(weekly_flag("--threshold"))
    weights = {name: float(g.get("weight", 1)) for name, (g, _) in graders(case).items()}
    total = sum(weights.values())
    for name, w in weights.items():
        if critical(case, name):
            assert (total - w) / total < threshold, (f"{name} failing alone leaves {case.name} at "
                                                     f"{(total - w) / total:.2f}")


def mock_files() -> list[Path]:
    return sorted(EVALS.glob("mocks/*/*.md")) + sorted(EVALS.glob("*/mocks/*/*.md"))


def test_the_mocks_answer_every_tool_the_server_registers_and_no_other():
    tools = hs.expected_tools(SERVER_PY)
    suite = {p.stem for p in (EVALS / "mocks" / "job-apply").glob("*.md")}
    assert suite == tools, (f"mocks/job-apply/ lacks {sorted(tools - suite)} and has {sorted(suite - tools)}: a tool "
                            "added to server.py needs its canned answer there (the stand-in offers only mocked tools)")
    for p in mock_files():
        if p.parent.name == "job-apply":
            assert p.stem in tools, f"{p.relative_to(EVALS).as_posix()}: no such tool"
    listed = json.loads((EVALS / "mocks" / "job-apply" / "_tools.json").read_text(encoding="utf-8"))["tools"]
    assert sorted(t["name"] for t in listed) == sorted(tools), (
        "refresh it: uv run --no-project python scripts/mcp_handshake.py --save-tools "
        "../evals/mocks/job-apply/_tools.json")
    assert all(t.get("description") and isinstance(t.get("inputSchema"), dict) for t in listed)


def test_the_mocks_parse_and_fill_their_templates_from_arguments_the_tools_take():
    schemas = {}
    for listing in EVALS.glob("mocks/*/_tools.json"):
        for t in json.loads(listing.read_text(encoding="utf-8"))["tools"]:
            schemas[(listing.parent.name, t["name"])] = set(t["inputSchema"].get("properties", {}))
    servers = set(json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]) | {"gmail"}
    assert mock_files()
    for p in mock_files():
        where = p.relative_to(EVALS).as_posix()
        assert p.parent.name in servers, f"{where}: a server .mcp.json doesn't declare (or a stand-in we don't keep)"
        meta, body = front(p)
        assert set(meta) <= MOCK_KEYS and meta.get("type", "fixed") == "fixed", where
        assert body and STUB not in body, f"{where}: no canned answer"
        args = (set(re.findall(r"\{\{\s*input\.([A-Za-z0-9_]+)", body))
                | set(re.findall(r"\{input\.([A-Za-z0-9_]+)\}", body)))
        assert args <= schemas[(p.parent.name, p.stem)], f"{where}: echoes arguments the tool doesn't take: {args}"
        for template in re.findall(r"\{\{file:([^{}]*(?:\{input\.[A-Za-z0-9_]+\}[^{}]*)*)\}\}", body):
            pattern = re.sub(r"\{input\.[A-Za-z0-9_]+\}", "*", template)
            answers = sorted(p.parent.glob(pattern))
            assert answers, f"{where}: no files for {template}"
            for answer in answers:
                json.loads(answer.read_text(encoding="utf-8"))
        if "{{" not in body and body.startswith("{"):  # (a JSON answer; screenshot and page_text answer text)
            json.loads(body)


def test_the_jobs_the_prompts_name_are_in_the_mocked_tracker():
    tracker = json.loads((EVALS / "mocks" / "job-apply" / "list_jobs.md").read_text(encoding="utf-8"))
    ids = {j["id"] for j in tracker["jobs"]}
    assert ids == {int(p.stem) for p in (EVALS / "mocks" / "job-apply" / "get_job").glob("*.json")}
    for case in cases():
        for n in re.findall(r"\bjob (\d+)", (case / "prompt.md").read_text(encoding="utf-8")):
            assert int(n) in ids, f"{case.name} names job {n}, which the mocked tracker doesn't have"


def test_the_weekly_job_runs_the_suite_capped_and_skips_cleanly_without_the_key():
    text = WEEKLY.read_text(encoding="utf-8")
    job = text.split("\n  evals:\n", 1)[1]
    assert "ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}" in job
    assert "claude plugin eval" in job and "--trust-plugin" in job and "--no-publish" in job
    assert "--mocks record" in job and "--allow-real-servers" not in job and "--mocks off" not in job
    assert float(weekly_flag("--max-cost-usd")) > 0 and 0 < float(weekly_flag("--threshold")) <= 1
    assert "::notice" in job and "GITHUB_STEP_SUMMARY" in job  # (no key: a notice saying what to set, not a failure)
    assert "UV_LINK_MODE: copy" in job  # (the CLI refuses a plugin folder holding hard links: uv's venv would)
