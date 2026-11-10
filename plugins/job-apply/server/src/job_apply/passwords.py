"""The passwords the desk makes for the person: one strong password for each job system, made on this computer.

A made password is 16 characters from four groups, left without the characters easily taken for one another when it's
read off the page and typed by hand (no l or o, no I or O, no 0 or 1): a letter first, two or more from each group,
and never the same character twice in a row. That meets the rules of every job system the desk signs in to: the
strictest, NEOGOV, asks for 12 with upper case, lower case, digits and symbols.

Every character is drawn with secrets.choice (the operating system's own randomness), and nothing else: this module
never imports `random`. A password is never logged or kept here: config.make_site_password saves it as a typed one is
saved, and it's typed only onto its own job system's sites (pipeline.password_for).
"""

from __future__ import annotations

import secrets

LENGTH = 16
LOWER = "abcdefghijkmnpqrstuvwxyz"  # (no l, no o)
UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # (no I, no O)
DIGITS = "23456789"  # (no 0, no 1)
SYMBOLS = "!@#$%*"
GROUPS = (LOWER, UPPER, DIGITS, SYMBOLS)
EVERY = "".join(GROUPS)  # the 62 characters a draw is from, each once: none likelier than another
OF_EACH = 2  # at least this many from each group


def make() -> str:
    """A new password: 16 characters drawn one by one, and all 16 drawn again until the rules hold (one draw in seven
    does), so every password that keeps the rules is as likely as any other (about 92 bits)."""
    while True:
        drawn = "".join(secrets.choice(EVERY) for _ in range(LENGTH))
        if (drawn[0] in LOWER + UPPER and all(sum(c in group for c in drawn) >= OF_EACH for group in GROUPS)
                and all(a != b for a, b in zip(drawn, drawn[1:]))):
            return drawn
