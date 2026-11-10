"""Passwords made for you (the design's Version E, its server half): the generator (passwords.py). Every password
in it is made up, or made in the test."""

import ast
from pathlib import Path

# The design's table for a made password (E2), spelled out here, not read from the module it checks
LOWER, UPPER = "abcdefghijkmnpqrstuvwxyz", "ABCDEFGHJKLMNPQRSTUVWXYZ"  # (no l, no o; no I, no O)
DIGITS, SYMBOLS = "23456789", "!@#$%*"  # (no 0, no 1)
GROUPS = (LOWER, UPPER, DIGITS, SYMBOLS)
ALLOWED = "".join(GROUPS)


def keeps_the_rules(password: str) -> bool:
    """A password as the design's table makes one: 16 long, a letter first, two or more from each group, only the
    allowed characters, and never the same character twice in a row."""
    return (len(password) == 16 and password[0] in LOWER + UPPER and set(password) <= set(ALLOWED)
            and all(sum(c in group for c in password) >= 2 for group in GROUPS)
            and all(a != b for a, b in zip(password, password[1:])))


# ------------------------------------------------------------------ the generator (E7's tests 1 and 2)
def test_two_thousand_made_passwords_each_keep_every_rule_and_no_two_are_the_same():
    from job_apply import passwords

    made = [passwords.make() for _ in range(2000)]
    for password in made:
        assert len(password) == 16, password
        assert password[0] in LOWER + UPPER, password  # a letter first
        assert set(password) <= set(ALLOWED), password  # (and so no l, o, I, O, 0 or 1)
        for group in GROUPS:
            assert sum(c in group for c in password) >= 2, (password, group)
        assert all(a != b for a, b in zip(password, password[1:])), password  # never the same character twice in a row
    assert len(set(made)) == 2000
    # (and every character of the table is in use: none of its four groups is cut short)
    assert set("".join(made)) == set(ALLOWED)


# A draw that keeps every rule, one that only just does (two digits, two symbols), and one that breaks each rule in turn
# (all made up for the test)
GOOD, JUST = "aB3!cD4@eF5#gH6$", "aBcDeFgHjKmN2#3%"
BREAKS = {"a digit first": "9aB3!cD4@eF5#gH6", "a symbol first": "!aB3cD4@eF5#gH6$",
          "one lower-case letter": "aB3!CD4@EF5#GH6$", "one capital": "aB3!cd4@ef5#gh6$",
          "one digit": "aB3!cD@eFg#hJ$kL", "one symbol": "aB3!cD4eF5gH6jK7",
          "the same character twice in a row": "aB3!cD4@eF5#gH66", "the same symbol twice in a row": "aB3!!cD4@eF5gH6$"}


def test_a_made_password_is_drawn_with_secrets_choice_and_the_module_never_imports_random(monkeypatch):
    """E7's test 2: the module imports `secrets` and nothing else that could draw, every draw is secrets.choice over
    the table's 62 characters, and what make() gives is those draws, in the order drawn. A draw that breaks a rule is
    thrown away whole and all 16 are drawn again."""
    from job_apply import passwords

    tree = ast.parse(Path(passwords.__file__).read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {(node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported == {"__future__", "secrets"}, imported  # never random (nor os, time, uuid or hashlib)
    used = {node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "secrets"}
    assert used == {"choice"}, used
    assert not hasattr(passwords, "random")
    # (the test's own draws: each 16 long, and kept or thrown away as it says)
    assert keeps_the_rules(GOOD) and keeps_the_rules(JUST)
    assert all(len(bad) == 16 and set(bad) <= set(ALLOWED) and not keeps_the_rules(bad) for bad in BREAKS.values())

    for why, drawn, made in [(why, bad + GOOD, GOOD) for why, bad in BREAKS.items()] + [("just enough", JUST, JUST)]:
        script, asked = iter(drawn), []

        def choice(characters, script=script, asked=asked):
            asked.append(characters)
            return next(script)

        monkeypatch.setattr(passwords.secrets, "choice", choice)
        assert passwords.make() == made, why
        assert len(asked) == len(drawn), why  # (16 thrown away and 16 kept; or the 16 kept alone)
        # each draw is from all 62 characters, each listed once: none is likelier than another
        assert all(len(characters) == 62 and set(characters) == set(ALLOWED) for characters in asked), why
