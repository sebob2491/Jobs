"""Every stop the desk says to the person ends with what they can do: press, answer, sign in, reload, solve, check...
(the review of Oct 15's pause-message audit; ten were reworded in 0.3.182). This reads each message the driver writes
out in its source (pipeline._pause's reason); one built elsewhere (a variable) is checked where it's built."""

import ast
import re
from pathlib import Path

PIPELINE = Path(__file__).resolve().parents[1] / "src" / "job_apply" / "pipeline.py"
# A thing the person does (the review of 0.3.189: "look", "read", "use", "let" and "run" made almost any message pass)
ACTION = re.compile(r"\b(press|answer|sign in|log in|create|reload|solve|fill|choose|enter|save|turn|open|close|remove|"
                    r"delete|type|pick|upload|apply|wait|complete|take|accept|decline|add|set|tick|untick|fix|resume|"
                    r"submit|put|give|tell|start|reset|confirm|agree|select|scroll|install|update|copy|paste|switch|"
                    r"leave|skip|mark|check|try)\b", re.I)


def _words(node: ast.AST) -> str:
    """A message's own words, its computed parts as "{}" (both branches of a conditional)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_words(v) if not isinstance(v, ast.FormattedValue) else "{}" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _words(node.left) + _words(node.right)
    if isinstance(node, ast.IfExp):
        return _words(node.body) + " | " + _words(node.orelse)
    return "{}"


def _ends_without_action(message: str) -> bool:
    """A message whose last two sentences say nothing for the person to do. One mostly built elsewhere, or ending in a
    part built elsewhere ("...: {}"), has its action there."""
    if len(message.replace("{}", "").strip()) < 25 or re.search(r"\{\}[.\u201d)\s]*$", message):
        return False
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", message.strip()) if s]
    return not ACTION.search(" ".join(sentences[-2:]))


def test_every_stop_says_what_the_person_can_do():
    tree = ast.parse(PIPELINE.read_text(encoding="utf-8"))
    pauses = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "_pause"
              and len(n.args) >= 3]
    assert len(pauses) > 50  # (the reader still finds the driver's stops)
    # (an emailed code's wait is the desk's own: it watches the inbox, and the queue goes on)
    silent = [(n.lineno, _words(n.args[2])[-160:]) for n in pauses
              if not (isinstance(n.args[1], ast.Constant) and n.args[1].value == "email_code")
              and _ends_without_action(_words(n.args[2]))]
    assert not silent, silent


def test_the_stops_said_in_one_place_say_what_to_do_too():
    """The messages the driver keeps in one place and passes by name (the review of 0.3.189: unread by the audit)."""
    from job_apply import pipeline

    for name in ("_BOT_CHECK_SAYS", "_TAILOR_SAYS"):
        assert not _ends_without_action(getattr(pipeline, name)), name


def test_the_audit_tells_a_stop_with_an_action_from_one_without():
    assert _ends_without_action("The page didn't move on. The site said nothing more about it.")
    assert _ends_without_action("Something went wrong on the site. Look at it and read what it says.")
    assert not _ends_without_action("The page didn't move on. Check it in the browser window, then press Resume.")
    assert not _ends_without_action("The page didn't move on: {}.")  # (the action is where the {} is built)
