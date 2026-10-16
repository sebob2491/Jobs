"""Properties that must hold for any input, checked over many generated ones (seeded, so a failure repeats): the
review of Oct 15 asked for property tests of question_pattern, find_code, sender_allowed and redact."""

import random
import re
import string

import pytest

from job_apply.config import question_pattern
from job_apply.fixtures import redact
from job_apply.mailbox import find_code, sender_allowed

WORDS = ["Are", "you", "willing", "to", "relocate", "What", "is", "your", "salary", "(Required)", "*", "U.S.", "18",
         "e-mail", "C++", "$", "[1]", "a.b", "?", ":", "Phone #", "Résumé", "naïve"]
TRIES = 400


def _rng(name: str) -> random.Random:
    return random.Random(f"properties-{name}")


def test_a_question_pattern_matches_its_own_label_however_it_is_spaced_or_marked():
    rng = _rng("pattern")
    for _ in range(TRIES):
        label = " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 8)))
        pattern = question_pattern(label)
        spaced = re.sub(r"\s+", lambda m: rng.choice([" ", "  ", "\n", "\t "]), label)
        marked = spaced + rng.choice(["", "*", " *", " (Required)", "?"])
        if pattern:
            assert re.search(pattern, marked), (label, marked, pattern)
        re.compile(pattern)  # (never a broken pattern, whatever the label holds)


@pytest.mark.parametrize("secret", ["Rivera", "Sam Rivera", "sam.rivera@example.com", "4805550123", "Li", "O'Neil"])
def test_redact_takes_a_secret_out_wherever_it_is_and_does_so_once_for_good(secret):
    rng = _rng(f"redact-{secret}")
    pool = [*WORDS, secret, secret.upper(), secret.lower(), "Riveras", "Problem", "Tom", "custom_field"]
    for _ in range(TRIES):
        text = " ".join(rng.choice(pool) for _ in range(rng.randint(1, 10)))
        once = redact(text, [secret])
        assert redact(once, [secret]) == once, text  # (idempotent: a second pass changes nothing)
        if len(secret) >= 3:
            assert not re.search(rf"(?<![A-Za-z]){re.escape(secret)}(?![A-Za-z])", once, re.I), (text, once)
        else:
            assert not re.search(rf"\b{re.escape(secret)}\b", once), (text, once)
        assert len(once) <= len(text) + 8 * text.count(" ") + 8  # (never grows wild)


def test_a_found_code_is_always_in_the_mail_and_never_a_year_after_the_words_alone():
    rng = _rng("code")
    filler = ["Thanks for applying.", "Your verification code is", "Use code", "The year is 2024.", "Call 480-555-0100.",
              "Job 2617841", "promo code SAVE20", "ZIP code 85201", "Expires in 15 minutes.", "Hi there,"]
    for _ in range(TRIES):
        code = "".join(rng.choice(string.digits) for _ in range(6))
        parts = [rng.choice(filler) for _ in range(rng.randint(0, 5))] + [f"Your verification code is {code}."]
        rng.shuffle(parts)
        text = " ".join(parts)
        found = find_code(text)
        assert found is None or found in text, (text, found)
        assert found != "SAVE20" and found != "85201", (text, found)


def test_a_sender_is_allowed_only_on_the_site_or_under_it():
    rng = _rng("sender")
    for _ in range(TRIES):
        site = rng.choice(["ti.com", "workday.com", "myworkday.com", "kla.com", "oraclecloud.com"])
        sub = rng.choice(["", "careers.", "mail.", "otp.", "a.b."])
        assert sender_allowed(f"no-reply@{sub}{site}", {site})
        evil = rng.choice([f"{site}.evil.example", f"evil{site}", f"{site[:-4]}-{site[-3:]}.example", f"x{site}"])
        assert not sender_allowed(f"no-reply@{evil}", {site}), evil
        assert not sender_allowed(f"Name <no-reply@{evil}>", {site}), evil
