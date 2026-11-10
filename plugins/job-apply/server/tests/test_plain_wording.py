"""Plain wording (the design's "Plain wording", "The test"): no sentence the desk can show sends the person to a file,
a terminal or a command. This reads every sentence in the files the page's words come from, and fails on one that
names profile.yaml, answers.yaml, secrets.yaml, desk.json, companies.yaml, a `settings.` key, "terminal",
"PowerShell" or "/job-apply:", unless it's on ALLOWED, the short list of the ones still allowed. Version C adds the
test with the list as it stands; Versions D and F shorten it (and an allowed sentence no longer said must leave it).

The page (static/desk.html) is read the same way once its own words are changed: until then it's in PAGE_LATER, and
moving it to CHECKED (with its own allowed sentences, if any) switches its part on."""

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "job_apply"
# the files whose sentences are read: the server's (Python) and the page's (HTML)
CHECKED = ["desk.py", "config.py", "letters.py", *sorted(p.name for p in SRC.glob("pipeline*.py"))]
# read like the rest once the page half of Version C has reworded the page's sentences
PAGE_LATER = ["static/desk.html"]

# (a key as said to the person, "settings.submit_mode": not the page's code reading it, state.settings.dry_run)
NAMES = re.compile(r"profile\.yaml|answers\.yaml|secrets\.yaml|desk\.json|companies\.yaml|(?<![\w.$])settings\.[a-z_]+|"
                   r"\b[Tt]erminal\b|PowerShell|/job-apply:")

# (file, a part of the sentence): each sentence still allowed to name one, and why
ALLOWED = [
    # about a broken file: the path is what helps ("Left as they are")
    ("desk.py", "off until profile.yaml is fixed"),
    ("desk.py", "is profile.yaml open in another program?"),
    ("desk.py", "can't tell until profile.yaml is fixed"),
    ("desk.py", "nothing is filled until profile.yaml is fixed."),
    ("pipeline_mail.py", "The desk can't watch your inbox until profile.yaml reads again: "),
    # the Safety window's lines, and the settings' warnings: Version D
    ("desk.py", "profile.yaml, under settings:"),
    ("desk.py", "(kept in desk.json)"),
    ("desk.py", "your usual one (documents.cover_letter in profile.yaml)"),
    ("desk.py", "JOB_APPLY_NEVER_SUBMIT=1 in the environment, which comes before profile.yaml"),
    ("config.py", "Unknown settings.browser_channel "),
    ("config.py", "settings.cover_letter_model should be a model's name"),
    ("config.py", "settings.phone_alerts_server should be your ntfy server's https address"),
    ("config.py", "settings.phone_alerts_server has a name or password in its address"),
    ("config.py", "Unknown settings.desk_window "),
    ("config.py", "Unknown settings.submit_mode "),
    ("config.py", "settings.phone_summary_at should be a time of day"),
    # the header written at the top of answers.yaml itself, never shown on the desk
    ("config.py", "# Answers you gave in the Job Desk."),
    # letters.py's NOT_SIGNED_IN, TOO_OLD and no_model: Version D
    ("letters.py", "In a terminal, run claude and sign in (/login)."),
    ("letters.py", "(run claude update in a terminal)"),
    ("letters.py", "(settings.cover_letter_model in profile.yaml)"),
    # the driver's stops and log lines that name a setting's key or profile.yaml: Version D
    ("pipeline.py", "(manage_accounts: false in profile.yaml leaves this to you)"),
    ("pipeline.py", "ask Claude to clear it from answers.yaml"),
    ("pipeline.py", "(settings.accept_notices)"),
    ("pipeline.py", "with accept_notices: true under settings: in profile"),
    ("pipeline.py", "set accept_cookies: true under settings: in profile.yaml"),
    ("pipeline.py", "ask Claude to set documents.resume (or documents.cover_letter) in profile.yaml"),
    ("pipeline_accounts.py", "(settings.manage_accounts)"),
    ("pipeline_filling.py", "add it to that school under education_history in profile.yaml"),
    ("pipeline_filling.py", "Or write it in full under education_history in profile.yaml"),
    ("pipeline_mail.py", "settings.email_codes is off"),
]

_LOGGERS = {"log", "logger", "logging", "_logger"}


def _python_said(text: str) -> list[tuple[int, str]]:
    """Every sentence a Python file may say: its strings (an f-string's own words, its parts as "{}"), less
    docstrings and what goes only to the program's log (log.warning(...)), which the desk never shows."""
    tree = ast.parse(text)
    skip = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr)}  # (docstrings)
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) \
                and isinstance(call.func.value, ast.Name) and call.func.value.id in _LOGGERS:
            skip |= {id(n) for arg in call.args for n in ast.walk(arg)}
    skip |= {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    said = []
    for n in ast.walk(tree):
        if id(n) in skip:
            continue
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            said.append((n.lineno, n.value))
        elif isinstance(n, ast.JoinedStr):
            said.append((n.lineno, "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in n.values)))
    return [(line, s) for line, s in said if " " in s.strip()]  # (a sentence: a file's or a key's name alone isn't)


def _page_said(text: str) -> list[tuple[int, str]]:
    """Every line of the page that may say something: its styles and comments left out."""
    text = re.sub(r"(?s)<style>.*?</style>|<!--.*?-->|/\*.*?\*/", lambda m: "\n" * m.group().count("\n"), text)
    return [(i, re.sub(r"(?<![:\\\"'])//.*", "", line)) for i, line in enumerate(text.splitlines(), 1)]


def _said(name: str) -> list[tuple[int, str]]:
    text = (SRC / name).read_text(encoding="utf-8")
    return _page_said(text) if name.endswith(".html") else _python_said(text)


def _named(names: list[str]) -> tuple[list[tuple[str, int, str]], set[tuple[str, str]]]:
    """The sentences that name a file, a key or a terminal and aren't allowed; and the allowed ones met."""
    bad, met = [], set()
    for name in names:
        for line, s in _said(name):
            if not NAMES.search(s):
                continue
            allowed = [(f, part) for f, part in ALLOWED if f == name and part in s]
            if allowed:
                met.update(allowed)
            else:
                bad.append((name, line, s[:160]))
    return bad, met


def test_no_sentence_sends_the_person_to_a_file_or_a_terminal():
    bad, _ = _named(CHECKED)
    assert not bad, bad


def test_each_allowed_sentence_is_still_said():
    """An allowed sentence reworded (in Version D, say) comes off ALLOWED, so the list only ever shortens."""
    _, met = _named(CHECKED)
    assert [a for a in ALLOWED if a not in met] == []
    assert all(f in CHECKED for f, _ in ALLOWED)


def test_the_test_finds_what_it_looks_for():
    """It reads a stop's words, an f-string's own words and a page's text, and not a docstring or a log line."""
    said = _python_said('"""About profile.yaml."""\n'
                        'log.warning("secrets.yaml went %s", x)\n'
                        'x = f"Edit {path}: profile.yaml, under settings:"\n'
                        'y = "Run it in a terminal, then press Carry on."\n'
                        'z = "desk.json"\n')
    assert [s for _, s in said if NAMES.search(s)] == ["Edit {}: profile.yaml, under settings:",
                                                       "Run it in a terminal, then press Carry on."]
    page = _page_said("<style>\n.a { content: 'profile.yaml' }\n</style>\n<!-- desk.json -->\n"
                      "<p>Open PowerShell</p>\nconst a = 1; // settings.submit_mode\nconst u = \"https://x.example/\";")
    assert [s for _, s in page if NAMES.search(s)] == ["<p>Open PowerShell</p>"]
    assert any("https://x.example/" in s for _, s in page)
    assert NAMES.search("Use settings.submit_mode") and not NAMES.search("Turn it on in Settings. Then press Carry on.")
    assert not NAMES.search("const on = state.settings.auto_submit && s.settings.dry_run;")
    assert PAGE_LATER == ["static/desk.html"] or "static/desk.html" in CHECKED  # (the page: read once switched on)
