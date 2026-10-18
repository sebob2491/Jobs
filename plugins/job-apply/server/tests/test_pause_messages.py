"""Every stop the desk says to the person ends with what they can do: press, answer, sign in, reload, solve, check...
(the review of Oct 15's pause-message audit; ten were reworded in 0.3.182). This reads each message the driver writes
out in its source (pipeline._pause's reason); one built elsewhere (a variable) is checked where it's built."""

import ast
import re
from pathlib import Path
from typing import Union

PIPELINE = Path(__file__).resolve().parents[1] / "src" / "job_apply" / "pipeline.py"
# A thing the person does (the review of 0.3.189: "look", "read", "use", "let" and "run" made almost any message pass)
ACTION = re.compile(r"\b(press|answer|sign in|log in|create|reload|solve|fill|choose|enter|save|turn|open|close|remove|"
                    r"delete|type|pick|upload|apply|wait|complete|take|accept|decline|add|set|tick|untick|fix|carry on|"
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


def _variants(node: ast.AST, most: int = 64) -> list[str]:
    """Each message a reason can be, its computed parts as "{}": a conditional's two sides apart, so a stop that says
    what to do on one side only fails (the review of T10: _words read both as one message, which the side with an
    action carried)."""
    if isinstance(node, ast.Constant):
        return [node.value if isinstance(node.value, str) else "{}"]
    if isinstance(node, ast.IfExp):
        return (_variants(node.body, most) + _variants(node.orelse, most))[:most]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [a + b for a in _variants(node.left, most) for b in _variants(node.right, most)][:most]
    if isinstance(node, ast.JoinedStr):
        out = [""]
        for v in node.values:
            parts = [v.value] if isinstance(v, ast.Constant) and isinstance(v.value, str) else ["{}"]
            out = [a + b for a in out for b in parts]
        return out
    return ["{}"]


def _ends_without_action(message: str) -> bool:
    """A message whose last two sentences say nothing for the person to do. One mostly built elsewhere, or ending in a
    part built elsewhere ("...: {}"), has its action there."""
    if len(message.replace("{}", "").strip()) < 25 or re.search(r"\{\}[.\u201d)\s]*$", message):
        return False
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", message.strip()) if s]
    return not ACTION.search(" ".join(sentences[-2:]))


_SAID = (ast.Constant, ast.JoinedStr, ast.BinOp, ast.IfExp)
_Scope = Union[ast.Module, ast.FunctionDef, ast.AsyncFunctionDef]


class _Source:
    """The driver's source, read for what a variable passed as a stop's reason can hold (the review of Oct 18, T9: six
    reasons were variables, read as "{}" and so never checked): a name's assignments in the function it's used in (or
    the module's, for a name assigned only there), and a part of a tuple it was unpacked from: a tuple written out,
    one a method returns, one kept on a state's attribute, or one the function's callers gave a parameter."""

    def __init__(self, tree: ast.Module):
        self.tree = tree
        self.functions = [f for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self.scope: dict[int, _Scope] = {id(n): tree for n in ast.walk(tree)}
        for f in self.functions:  # (an inner function's nodes are walked last: theirs)
            for n in ast.walk(f):
                self.scope[id(n)] = f

    def said(self, node: ast.AST, path: tuple[int, ...] = (), depth: int = 0) -> list[str] | None:
        """Every message `node` can be (or the part of it at `path`, a tuple's index within a tuple's), None where
        one can't be read."""
        scope = self.scope[id(node)]
        if depth > 12:
            return None
        if isinstance(node, ast.Await):
            return self.said(node.value, path, depth + 1)
        if not path and isinstance(node, _SAID):
            return _variants(node)
        if path and isinstance(node, ast.Tuple):
            return self.said(node.elts[path[0]], path[1:], depth + 1)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                and isinstance(node.slice.value, int):
            return self.said(node.value, (node.slice.value, *path), depth + 1)
        if isinstance(node, ast.Name):
            return self.named(node.id, path, scope, depth + 1)
        if path and isinstance(node, ast.Attribute):  # (a tuple kept on a state: ps.back_from = (page, stuck))
            kept = [a.value for a in ast.walk(self.tree) if isinstance(a, ast.Assign) and isinstance(a.value, ast.Tuple)
                    and any(isinstance(t, ast.Attribute) and t.attr == node.attr for t in a.targets)]
            return self._all([self.said(v, path, depth + 1) for v in kept])
        if path and isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "self":
            returned = [r.value for f in self.functions if f.name == node.func.attr for r in ast.walk(f)
                        if isinstance(r, ast.Return) and self.scope[id(r)] is f and r.value is not None
                        and not (isinstance(r.value, ast.Constant) and r.value.value is None)]
            return self._all([self.said(v, path, depth + 1) for v in returned])
        return None

    def named(self, name: str, path: tuple[int, ...], scope: _Scope, depth: int) -> list[str] | None:
        found: list[list[str] | None] = []
        for a in ast.walk(scope):
            if self.scope[id(a)] is scope and isinstance(a, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) \
                    and a.value is not None:
                for target in a.targets if isinstance(a, ast.Assign) else [a.target]:
                    found += [self.said(a.value, (*at, *path), depth + 1) for at in _places(target, name)]
        if not isinstance(scope, ast.Module):
            params = [p.arg for p in scope.args.args]
            if name in params:  # (what the function's callers give it)
                i = params.index(name) - (params[:1] == ["self"])
                for call in ast.walk(self.tree):
                    if isinstance(call, ast.Call) and getattr(call.func, "attr", getattr(call.func, "id", "")) \
                            == scope.name:
                        given = [k.value for k in call.keywords if k.arg == name] or call.args[i:i + 1]
                        found += [self.said(g, path, depth + 1) for g in given] or [None]
            elif not found:
                return self.named(name, path, self.tree, depth + 1)
        return self._all(found)

    @staticmethod
    def _all(found: list[list[str] | None]) -> list[str] | None:
        return None if not found or None in found else [m for messages in found if messages for m in messages]


def _places(target: ast.AST, name: str) -> list[tuple[int, ...]]:
    """Where `name` is in an assignment's target: () for the target itself, (0, 1) for `(a, name), b = ...`."""
    if isinstance(target, ast.Name):
        return [()] if target.id == name else []
    if isinstance(target, ast.Tuple):
        return [(k, *at) for k, t in enumerate(target.elts) for at in _places(t, name)]
    return []


def _stops() -> tuple[list[tuple[int, list[str] | None]], ast.Module]:
    """Each _pause call's line and every message its reason can be (None: one that can't be read)."""
    tree = ast.parse(PIPELINE.read_text(encoding="utf-8"))
    source = _Source(tree)
    pauses = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "_pause"]
    return [(n.lineno, source.said(n.args[2]) if len(n.args) >= 3 else None) for n in pauses
            if not (isinstance(n.args[1], ast.Constant) and n.args[1].value == "email_code")], tree


def test_every_stop_says_what_the_person_can_do():
    stops, tree = _stops()
    pauses = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "_pause"]
    assert len(pauses) == 93  # (a new stop: count it here, with its message)
    unread = [line for line, said in stops if said is None]
    assert not unread, unread  # (every reason is read, a variable's at each of its assignments: T9)
    # (an emailed code's wait is the desk's own: it watches the inbox, and the queue goes on)
    silent = [(line, message[-160:]) for line, said in stops for message in said or []
              if _ends_without_action(message)]
    assert not silent, silent


def test_a_reason_passed_as_a_variable_is_read_where_it_is_built():
    """The six reasons the driver passes by name (found[0] of _maybe_done_elsewhere's answer; `stuck`, `said`, kept on
    the state's back_from, unpacked from _replace_kept's answer, or the module's _BOT_CHECK_SAYS) are each read at
    every assignment, so a reworded one that tells the person nothing to do fails like one written in the call."""
    tree = ast.parse("""
WAIT = "The site is slow."
class A:
    def found(self):
        return None
        return "This may be a job you applied to. Apply anyway, or Skip?", [1]
        def said(o):
            return ("x", "y", "z")
    def go(self, run, ps):
        self._pause(run, "stuck", self.found()[0], code="a")
        found = self.found()
        self._pause(run, "stuck", found[0], code="a")
        self._pause(run, "stuck", WAIT, code="a")
        stuck = "The page didn't move on." if ps else "The site said no."
        ps.back_from = (1, stuck)
        (page, kept), ps.back_from = ps.back_from, None
        self._pause(run, "stuck", kept, code="a")
        self.after(run, (["box"], "I deleted the old file. Upload the new one, then press Carry on."))
        self._pause(run, "stuck", f(x), code="a")
    def after(self, run, replacing):
        emptied, stuck = replacing
        self._pause(run, "stuck", stuck, code="a")
""")
    source = _Source(tree)
    said = [source.said(n.args[2]) for n in ast.walk(tree) if isinstance(n, ast.Call)
            and getattr(n.func, "attr", "") == "_pause"]
    asked = "This may be a job you applied to. Apply anyway, or Skip?"
    assert sorted(said, key=str) == sorted([[asked], [asked], ["The site is slow."],
                                            ["The page didn't move on.", "The site said no."],
                                            ["I deleted the old file. Upload the new one, then press Carry on."],
                                            None], key=str)



def test_the_stops_said_in_one_place_say_what_to_do_too():
    """The messages the driver keeps in one place and passes by name (the review of 0.3.189: unread by the audit)."""
    from job_apply import pipeline

    for name in ("_BOT_CHECK_SAYS", "_TAILOR_SAYS"):
        assert not _ends_without_action(getattr(pipeline, name)), name


def test_the_audit_tells_a_stop_with_an_action_from_one_without():
    assert _ends_without_action("The page didn't move on. The site said nothing more about it.")
    assert _ends_without_action("Something went wrong on the site. Look at it and read what it says.")
    assert not _ends_without_action("The page didn't move on. Check it in the desk's browser window, then press Carry on.")
    assert not _ends_without_action("The page didn't move on: {}.")  # (the action is where the {} is built)


def _said(tree: ast.AST) -> list[tuple[int, str]]:
    """Every text the driver may say (its strings, an f-string's own words), docstrings left out."""
    docs = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            out.append((n.lineno, n.value))
        elif isinstance(n, (ast.JoinedStr, ast.BinOp)):
            out.append((n.lineno, _words(n)))
    return out


def test_the_window_has_one_name_and_so_has_i_submitted_it():
    """The review of Oct 18 (U12): the window the desk fills applications in was "the automation browser window" in
    the page's footer, "the browser window" on the cards and "the browser" in other stops. It's "the desk's browser
    window" everywhere; and the card's button the person presses after sending an application on the site themselves
    is "I submitted it myself", never "I submitted it" or "I submitted it already". So do the reasons the form's
    readers give for a box they couldn't fill, which the card shows beside its question."""
    said = _said(ast.parse(PIPELINE.read_text(encoding="utf-8")))
    for name in ("browser_menus.py", "browser_dates.py", "browser_uploads.py"):
        said += [(f"{name}:{line}", text) for line, text in
                 _said(ast.parse((PIPELINE.parent / name).read_text(encoding="utf-8")))]
    other = [(line, text[:120]) for line, text in said
             if re.search(r"automation browser|(?<!desk's )\bbrowser window|\bin the browser\b(?! window)", text)]
    assert not other, other
    labels = [(line, text[:120]) for line, text in said if re.search(r"I submitted it(?! myself)", text)]
    assert not labels, labels
    desk = (PIPELINE.parent / "static" / "desk.html").read_text(encoding="utf-8")
    assert not re.search(r"automation browser|(?<!desk's )\bbrowser window", desk)
    assert not re.search(r">I submitted it(?! myself)|\"I submitted it(?! myself)", desk)


def test_the_job_button_is_carry_on_and_resume_is_only_the_document():
    """The review of Oct 18 (U3): the job's "Resume" button sat beside "Use my usual resume" and "Tailor my resume for
    each job", and about 45 stops ended "then press Resume". The button is "Carry on" (the queue's "Carry on with the
    queue"), and every message says so: on the desk, "Resume" is only ever the document."""
    said = _said(ast.parse(PIPELINE.read_text(encoding="utf-8")))
    for name in ("desk.py", "server.py", "browser_menus.py", "browser_dates.py", "browser_uploads.py"):
        said += [(f"{name}:{line}", text) for line, text in
                 _said(ast.parse((PIPELINE.parent / name).read_text(encoding="utf-8")))]
    # (the document's own label, "Resume", and a file's name, "<First>_<Last>_Resume.pdf", stay)
    button = [(line, text[:120]) for line, text in said
              if text.strip() != "Resume" and re.search(r"(?<![\w/])Resume\b(?![/._])", text)]
    assert not button, button
    desk = (PIPELINE.parent / "static" / "desk.html").read_text(encoding="utf-8")
    assert not re.search(r"(?<![\w/])Resume\b(?![/._])", re.sub(r"(?<!:)//.*", "", desk))  # (comments aside)
    assert 'b("resume", "Carry on")' in desk and 'data-act="resume">Carry on with the queue<' in desk


def _codes(node: ast.AST) -> set[str] | None:
    """The stop codes an argument can be: a literal's, or each branch's of a conditional of literals; None for any
    other expression."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        body, orelse = _codes(node.body), _codes(node.orelse)
        return None if body is None or orelse is None else body | orelse
    return None


# The stops whose code comes from elsewhere: what's left on a filled page (_stop_for_what_is_left) names the code
# _entry_gaps returns for the entries the profile lacks (a Degree list, a box that refused its answer, ...); a step of
# making an account (_account_theirs) the code each of its callers gives, written out there
PASSED_ON = {("_stop_for_what_is_left", "gap_code"): "_entry_gaps", ("_account_theirs", "code"): "(its callers)"}


def test_every_stop_has_a_code_in_stop_codes():
    """T1 (the review of Oct 18): every pause names why as one of pipeline.STOP_CODES, written out where it pauses,
    so the nightly sorts stops by it and never by a message's words. And every code is one some stop gives."""
    from job_apply.pipeline import STOP_CODES

    tree = ast.parse(PIPELINE.read_text(encoding="utf-8"))
    functions = {f.name: f for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
    where = {id(n): f.name for f in functions.values() for n in ast.walk(f)}  # (the innermost: walked last)
    pauses = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "_pause"]
    assert len(pauses) == 93  # (a new stop: count it here, with its code)
    used: set[str] = set()
    unread = []
    for call in pauses:
        given = next((k.value for k in call.keywords if k.arg == "code"), None)
        codes = _codes(given) if given is not None else None
        if codes is None and isinstance(given, ast.Name) and PASSED_ON.get((where[id(call)], given.id)) == \
                "(its callers)":
            callers = [c for c in ast.walk(tree) if isinstance(c, ast.Call)
                       and getattr(c.func, "attr", getattr(c.func, "id", "")) == where[id(call)]]
            parts = [_codes(next((k.value for k in c.keywords if k.arg == given.id), ast.Constant(None)))
                     for c in callers]
            codes = None if not callers or None in parts else set().union(*parts)
        elif codes is None and isinstance(given, ast.Name) and (where[id(call)], given.id) in PASSED_ON:
            producer = functions[PASSED_ON[(where[id(call)], given.id)]]
            returned = [r.value for r in ast.walk(producer) if isinstance(r, ast.Return) and r.value is not None
                        and where[id(r)] == producer.name]  # (not a helper's defined inside it)
            # (the last element of every value it returns, and the parameter's default)
            parts = [_codes(r.elts[-1]) if isinstance(r, ast.Tuple) else None for r in returned]
            args = functions[where[id(call)]].args
            names = [a.arg for a in args.args][-len(args.defaults):] if args.defaults else []
            parts.append(_codes(args.defaults[names.index(given.id)]) if given.id in names else None)
            codes = None if None in parts else set().union(*parts)
        if codes is None:
            unread.append((call.lineno, ast.unparse(given) if given is not None else "no code"))
        else:
            used |= codes
    assert not unread, unread
    # (a stop made without _pause: a run waiting for its tailored resume, or begun before the desk restarted)
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "Run":
            used |= set().union(*(_codes(k.value) or {"(unread)"} for k in call.keywords if k.arg == "code"))
    assert used - STOP_CODES == set(), used - STOP_CODES
    assert STOP_CODES - used == set(), STOP_CODES - used


# Stops whose code says whose trouble it is (the read-only review of stop-codes, Oct 18): a step the desk took that
# didn't work is never coded as the person's or the site's, and two stops told apart only by their words have codes
EXPECTED_CODES = {
    "didn't take my click": {"account_step_failed"},
    "it showed a step I don't know": {"account_step_failed"},
    "its box didn't take my tick": {"account_step_failed"},
    "its choice didn't take my": {"account_step_failed"},
    "and it didn't go on": {"account_step_failed"},
    "I couldn't find the button that goes on with making your account": {"no_step_button"},
    "show a picture check": {"account_theirs"},
    "is emailing you a password reset": {"reset_pending"},
    "couldn't be reached, so the page": {"site_unreachable"},
    "A cookie or privacy banner is in the way": {"no_step_button"},
    "Something went wrong": {"site_went_wrong"},
    "The way on is": {"agreement_theirs"},
    "doesn't allow automated": {"submit_not_allowed"},
    "Practice mode:": {"practice_stop"},
    "Sign in (or create your account) on {}": {"account_theirs", "sign_in_refused"},
}


def test_a_stop_the_desk_caused_is_coded_as_the_desks():
    tree = ast.parse(PIPELINE.read_text(encoding="utf-8"))
    found: dict[str, set[str]] = {}
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and getattr(call.func, "attr", "") in ("_pause", "_account_theirs")):
            continue
        said = _words(call.args[2]) if len(call.args) > 2 else ""  # (the reason: both take it third)
        code = _codes(next((k.value for k in call.keywords if k.arg == "code"), ast.Constant(None))) or set()
        for fragment in EXPECTED_CODES:
            if fragment in said:
                found.setdefault(fragment, set()).update(code)
    assert found == EXPECTED_CODES


def test_each_side_of_a_conditional_stop_is_read_as_its_own_message():
    """A stop whose reason is `X if c else Y`: each side is a message of its own, so one side that gives the person
    nothing to do fails, however the other reads (the review of T10's plant: read as one message, it passed)."""
    said = _variants(ast.parse('"The site lost the file again and again. Nothing more was said about it by the site '
                               'this time." if run.draft else f"I deleted the {what}" + " (old). Upload the new one, '
                               'then press Carry on."', mode="eval").body)
    assert said == ["The site lost the file again and again. Nothing more was said about it by the site this time.",
                    "I deleted the {} (old). Upload the new one, then press Carry on."]
    assert [_ends_without_action(m) for m in said] == [True, False]
    assert _variants(ast.parse('"a" + ("b" if x else "c") + f"d{e}"', mode="eval").body) == ["abd{}", "acd{}"]


def test_the_code_audit_reads_only_literals():
    assert _codes(ast.parse('"a" if x else "b" if y else "c"', mode="eval").body) == {"a", "b", "c"}
    assert _codes(ast.parse('"a" if x else code', mode="eval").body) is None
    assert _codes(ast.parse("f(x)", mode="eval").body) is None
