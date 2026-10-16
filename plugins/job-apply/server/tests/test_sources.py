"""The server's own sources: what every file of them must keep to."""

import ast
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
ROOTS = ("src", "scripts", "tests")
# owners whose .open() isn't a file's: the browser's and the desk's own, os.open (a file descriptor, binary)
NOT_A_FILE = {"os", "webbrowser", "self", "srv", "browser", "page", "b", "desk", "applier", "server"}


def _text_calls_without_encoding(tree: ast.AST):
    """Path.read_text / write_text, open() and Path.open() in text mode, with no encoding named."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
        if name in ("read_text", "write_text") and isinstance(f, ast.Attribute):
            kind = name
        elif name == "open" and isinstance(f, ast.Name):
            kind = "open"
        elif name == "open" and isinstance(f, ast.Attribute):
            owner = f.value
            oname = owner.attr if isinstance(owner, ast.Attribute) else owner.id if isinstance(owner, ast.Name) else ""
            if oname in NOT_A_FILE:
                continue
            kind = "open"
        else:
            continue
        if any(k.arg == "encoding" for k in node.keywords):
            continue
        if kind == "open":
            mode = None
            position = 1 if isinstance(f, ast.Name) else 0  # open(path, mode) / path.open(mode)
            if len(node.args) > position and isinstance(node.args[position], ast.Constant):
                mode = node.args[position].value
            for k in node.keywords:
                if k.arg == "mode" and isinstance(k.value, ast.Constant):
                    mode = k.value.value
            if isinstance(mode, str) and "b" in mode:
                continue
        yield kind, node.lineno


def test_every_text_file_is_read_and_written_as_utf_8():
    """Windows reads and writes text as cp1252 unless told otherwise (the owner's platform, where the desk runs):
    a profile's answer with a character outside it fails to save, and a file written there reads back wrong
    on another computer. Every text read or write in the server's sources, scripts and tests names its
    encoding (the whole-program review of Oct 15: 226 of them didn't, in the scripts and tests)."""
    missing = []
    for root in ROOTS:
        for path in sorted((SERVER / root).rglob("*.py")):
            if "fixtures" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            missing += [f"{path.relative_to(SERVER).as_posix()}:{line} ({kind})"
                        for kind, line in _text_calls_without_encoding(tree)]
    assert not missing, "text read or written with no encoding (add encoding=\"utf-8\"):\n" + "\n".join(missing)
