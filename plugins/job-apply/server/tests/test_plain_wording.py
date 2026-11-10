"""Plain wording (the design's "Plain wording", "The test"): no sentence the desk can show sends the person to a file,
a terminal or a command. This reads every sentence in the files the page's words come from, and fails on one that
names profile.yaml, answers.yaml, secrets.yaml, desk.json, companies.yaml, a `settings.` key, "terminal",
"PowerShell" or "/job-apply:", unless that very place is on ALLOWED, the short list of the ones still allowed.
Version C adds the test with the list as it stands; Versions D and F shorten it (an entry that excuses nothing any more
fails, so it must leave the list).

The page (static/desk.html) is read line by line, its styles and comments left out (_page_said)."""

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "job_apply"
# the files whose sentences are read: the server's (Python) and the page's (HTML)
CHECKED = ["desk.py", "config.py", "letters.py", "updates.py", *sorted(p.name for p in SRC.glob("pipeline*.py")),
           "static/desk.html"]
# a file whose words are still to be reworded before it's read (the page was, until Version C's page half): none now
PAGE_LATER: list[str] = []

# (a key as said to the person, "settings.submit_mode": not the page's code reading it, state.settings.dry_run)
NAMES = re.compile(r"profile\.yaml|answers\.yaml|secrets\.yaml|desk\.json|companies\.yaml|(?<![\w.$])settings\.[a-z_]+|"
                   r"\b[Tt]erminal\b|PowerShell|/job-apply:")

# (file, the offending name with enough of its sentence to be one place): each excuses only the names inside it
ALLOWED = [
    # about a broken file: the path is what helps ("Left as they are")
    ("desk.py", "off until profile.yaml is fixed"),
    ("desk.py", "is profile.yaml open in another program?"),
    ("desk.py", "can't tell until profile.yaml is fixed"),
    ("desk.py", "nothing is filled until profile.yaml is fixed."),
    ("pipeline_mail.py", "The desk can't watch your inbox until profile.yaml reads again: "),
    # the settings' warnings: a line of the profile that does nothing, said with the key the person wrote
    ("config.py", "Unknown settings.browser_channel "),
    ("config.py", "settings.cover_letter_model should be a model's name"),
    ("config.py", "settings.phone_alerts_server should be your ntfy server's https address"),
    ("config.py", "settings.phone_alerts_server has a name or password in its address"),
    ("config.py", "Unknown settings.desk_window "),
    ("config.py", "Unknown settings.submit_mode "),
    ("config.py", "settings.phone_summary_at should be a time of day"),
    # the header written at the top of answers.yaml itself, never shown on the desk
    ("config.py", "edit or delete entries freely. profile.yaml's own answers come after these."),
    # the job's log lines that name a setting's key (the live check's audit reads their words), and the mail watch's
    # word to Claude: not in Version D's table, left as they are
    ("pipeline.py", "(manage_accounts: false in profile.yaml leaves this to you)"),
    ("pipeline.py", "(settings.accept_notices)"),
    ("pipeline_accounts.py", "(settings.manage_accounts)"),
    ("pipeline_mail.py", "settings.email_codes is off"),
    # the page's: the Common questions card, the Site passwords card's long paragraph, the Help page's line on answers,
    # the question card for an employee ID, the What's working card, #auto-hint, the What and where window: Version D
    ("static/desk.html", "(kept in <code>~/.job-apply/answers.yaml</code>)"),
    ("static/desk.html", "<code>~/.job-apply/secrets.yaml</code> where there's none"),
    ("static/desk.html", "Answers you give here are saved in <code>~/.job-apply/answers.yaml</code>"),
    ("static/desk.html", "ask Claude to clear it from answers.yaml"),
    ("static/desk.html", "(settings.learn_from_responses: false)"),
    ("static/desk.html", "(settings.submit_mode: dry_run)"),
    ("static/desk.html", "of your own (companies.yaml)"),
    # the page's update notice: Version F
    ("static/desk.html", "in a terminal (PowerShell on Windows)"),
]

_LOGGERS = {"log", "logger", "logging", "_logger"}


def _text(node: ast.AST) -> str | None:
    """A string expression as one string: a literal (Python joins adjacent ones), an f-string's own words in order
    (its parts as "{}"), and a `+` of them (a part that isn't a string as "{}"). None for anything else."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text(node.left), _text(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else "{}") + (right if right is not None else "{}")
    return None


def _read(node: ast.AST) -> set[int]:
    """The nodes _text read into a string: not a part read as "{}" (a conditional's strings are read on their own)."""
    if isinstance(node, ast.JoinedStr):
        return {id(node)} | {id(v) for v in node.values if isinstance(v, ast.Constant)}
    if isinstance(node, ast.BinOp):
        return {id(node)} | set().union(*(_read(p) for p in (node.left, node.right) if _text(p) is not None))
    return {id(node)}


def _python_said(text: str) -> list[tuple[int, str]]:
    """Every sentence a Python file may say, each string expression read whole (_text), less docstrings and what goes
    only to the program's log (log.warning(...)), which the desk never shows."""
    tree = ast.parse(text)
    skip = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr)}  # (docstrings)
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) \
                and isinstance(call.func.value, ast.Name) and call.func.value.id in _LOGGERS:
            skip |= {id(n) for arg in call.args for n in ast.walk(arg)}
    said = []
    for n in ast.walk(tree):  # (outermost first: a part of a string read whole isn't read again on its own)
        if id(n) in skip:
            continue
        whole = _text(n)
        if whole is not None:
            said.append((n.lineno, whole))
            skip |= _read(n)
    return [(line, s) for line, s in said if " " in s.strip()]  # (a sentence: a file's or a key's name alone isn't)


def _page_said(text: str) -> list[tuple[int, str]]:
    """Every line of the page that may say something: its styles and comments left out. Lines joined by a `+` (one
    ending with it, or the next beginning with it) are read as one, at the first one's number, with the string pieces
    they join run together ("in profile." + "yaml" reads "in profile.yaml")."""
    text = re.sub(r"(?s)<style>.*?</style>|<!--.*?-->|/\*.*?\*/", lambda m: "\n" * m.group().count("\n"), text)
    lines = [(i, re.sub(r"(?<![:\\\"'])//.*", "", line)) for i, line in enumerate(text.splitlines(), 1)]
    said: list[tuple[int, str]] = []
    for i, line in lines:
        if said and (said[-1][1].rstrip().endswith("+") or line.lstrip().startswith("+")):
            said[-1] = (said[-1][0], said[-1][1].rstrip() + " " + line.strip())
        else:
            said.append((i, line))
    return [(i, re.sub(r"[\"'`]\s*\+\s*[\"'`]", "", s)) for i, s in said]


def _said(name: str) -> list[tuple[int, str]]:
    text = (SRC / name).read_text(encoding="utf-8")
    return _page_said(text) if name.endswith(".html") else _python_said(text)


def _offences(name: str, said: list[tuple[int, str]],
              allowed: list[tuple[str, str]]) -> tuple[list[tuple[str, int, str]], set[tuple[str, str]]]:
    """Each name found that no entry of `allowed` (for this file) holds where it's found; and the entries used."""
    bad, used = [], set()
    for line, s in said:
        for m in NAMES.finditer(s):
            holding = [(f, part) for f, part in allowed if f == name and any(
                at <= m.start() and m.end() <= at + len(part)
                for at in (k.start() for k in re.finditer(re.escape(part), s)))]
            if holding:
                used.update(holding)
            else:
                bad.append((name, line, s[max(0, m.start() - 60):m.end() + 60]))
    return bad, used


def _named(names: list[str]) -> tuple[list[tuple[str, int, str]], set[tuple[str, str]]]:
    bad, used = [], set()
    for name in names:
        b, u = _offences(name, _said(name), ALLOWED)
        bad += b
        used |= u
    return bad, used


def test_no_sentence_sends_the_person_to_a_file_or_a_terminal():
    bad, _ = _named(CHECKED)
    assert not bad, bad


def test_each_allowed_entry_excuses_something_said():
    """An allowed sentence reworded (in Version D, say) comes off ALLOWED, so the list only ever shortens; and each
    entry holds the name it excuses."""
    _, used = _named(CHECKED)
    assert [a for a in ALLOWED if a not in used] == []
    assert [a for a in ALLOWED if a[0] not in CHECKED or not NAMES.search(a[1])] == []


def test_the_test_finds_what_it_looks_for():
    """It reads a stop's words, an f-string's own words, a phrase split across `+` or across a string's pieces, and a
    page's text; not a docstring or a log line. An allowed entry excuses only the place it names."""
    said = _python_said('"""About profile.yaml."""\n'
                        'log.warning("secrets.yaml went %s", x)\n'
                        'x = f"Edit {path}: profile.yaml, under settings:"\n'
                        'y = "Run it in a terminal, then press Carry on."\n'
                        'z = "desk.json"\n'
                        'w = ("Set it in profile." + "yaml, then press Carry on.")\n'
                        'v = ("Set it in profile." "yaml, " + f"then {press} Carry on.")\n'
                        'u = "Fixed. " + ("Read desk.json again." if a else "Done.")\n')
    assert [s for _, s in said if NAMES.search(s)] == ["Edit {}: profile.yaml, under settings:",
                                                       "Run it in a terminal, then press Carry on.",
                                                       "Set it in profile.yaml, then press Carry on.",
                                                       "Set it in profile.yaml, then {} Carry on.",
                                                       "Read desk.json again."]
    # (one allowed place doesn't excuse another name in the same string)
    two = [(1, "Fix profile.yaml by hand. Or set it in answers.yaml.")]
    bad, used = _offences("f.py", two, [("f.py", "Fix profile.yaml by hand.")])
    assert [b[2] for b in bad] == ["Fix profile.yaml by hand. Or set it in answers.yaml."]
    assert used == {("f.py", "Fix profile.yaml by hand.")}
    bad, used = _offences("f.py", [(1, "Fix profile.yaml by hand.")], [("f.py", "profile.yaml by hand. Or")])
    assert len(bad) == 1 and used == set()
    page = _page_said("<style>\n.a { content: 'profile.yaml' }\n</style>\n<!-- desk.json -->\n"
                      "<p>Open PowerShell</p>\nconst a = 1; // settings.submit_mode\nconst u = \"https://x.example/\";")
    assert [s for _, s in page if NAMES.search(s)] == ["<p>Open PowerShell</p>"]
    assert any("https://x.example/" in s for _, s in page)
    # (a name split across the lines of a `+` join, the `+` ending a line or beginning the next, at the first line)
    joined = _page_said('<p>One</p>\nconst a = "Set it in profile." +\n  "yaml, then press Carry on.";\n'
                        'const b = `Open the ${x} desk.` \n  + `json file`;\nconst c = "Nothing here.";')
    assert [(i, s) for i, s in joined if NAMES.search(s)] == [
        (2, 'const a = "Set it in profile.yaml, then press Carry on.";'),
        (4, "const b = `Open the ${x} desk.json file`;")], joined
    assert NAMES.search("Use settings.submit_mode") and not NAMES.search("Turn it on in Settings. Then press Carry on.")
    assert not NAMES.search("const on = state.settings.auto_submit && s.settings.dry_run;")
    # (every file named is there, and the page is read in one list or the other, never both)
    assert all((SRC / name).is_file() for name in CHECKED + PAGE_LATER)
    assert not set(CHECKED) & set(PAGE_LATER) and "static/desk.html" in CHECKED + PAGE_LATER
    assert "static/desk.html" in CHECKED and PAGE_LATER == []  # (the page's part is on: Version C's page half)
