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


# ---- the answer rules, fuzzed (T7, the review of Oct 18) ----
# Random labels from a pool of the words real questions are made of, as text boxes, lists, radios and boxes, against
# conftest's profile: whatever the desk answers must be what the profile says (a value in it, or a form of one), and
# never a claim it doesn't make (a degree above its own, a Yes to a qualification it doesn't name, a box ticked that
# the person's own answers don't tick). The oracle is the test's own, not the desk's code.

FUZZ_WORDS = ["Master's", "PhD", "Doctorate", "MBA", "Bachelor's", "Associate", "High School", "diploma", "GED",
              "degree", "level", "highest", "education", "years", "of", "experience", "with", "Python", "SAP",
              "clean room", "forklift", "certified", "certification", "license", "CDL", "security clearance",
              "Top Secret", "TS/SCI", "active", "veteran", "disability", "gender", "salary", "desired", "expected",
              "current", "employer", "manager", "supervisor", "name", "phone", "email", "date", "start", "available",
              "relocate", "travel", "shift", "night", "weekend", "overtime", "sponsorship", "visa", "authorized",
              "U.S.", "citizen", "citizenship", "18", "age", "felony", "convicted", "drug", "test", "background",
              "check", "signature", "GPA", "major", "school", "graduation", "year", "month", "city", "state", "zip",
              "country", "address", "LinkedIn", "website", "portfolio", "reference", "how did you hear", "referral",
              "employee ID", "SSN", "Social Security", "passport", "driver's", "work", "lift", "50 lbs", "stand",
              "hours", "willing", "able", "have you", "do you", "are you", "please", "describe", "explain", "why",
              "*", "(Required)", "?"]
LEVELS = ["High School", "Some College", "Associate's Degree", "Bachelor's Degree", "Master's Degree", "Doctorate",
          "MBA"]
YES_NO = ["Yes", "No"]
OPTION_SETS = [LEVELS, YES_NO, ["Yes", "No", "Prefer not to say"], [str(n) for n in range(11)] + ["10+"],
               ["Less than 1 year", "1-3 years", "3-5 years", "5-10 years", "10+ years"],
               ["Day", "Night", "Weekend", "Any"], ["Male", "Female", "Decline to self-identify"]]
KINDS = ["text", "text", "textarea", "select", "radio", "checkbox"]
FUZZ_LABELS = 4000

# A degree's rank, read by the test's own words: what a choice claims
_RANKS = [(re.compile(r"doctor|ph\.?d", re.I), 4), (re.compile(r"master|mba|\bms\b|\bma\b", re.I), 3),
          (re.compile(r"bachelor|\bbs\b|\bba\b|\bb\.s\.", re.I), 2), (re.compile(r"associate", re.I), 1),
          (re.compile(r"high school|\bged\b|diploma|some college", re.I), 0)]
# What a Yes claims that conftest's profile never says: a degree above a bachelor's, a clearance, a license or a
# certification, a passport, a conviction, a veteran's or a disability's status
_CLAIMS = re.compile(r"master|ph\.?d|doctor|mba|clearance|secret|ts/sci|\bcdl\b|licen[cs]|certif|passport|felony|"
                     r"convicted|veteran|disabilit", re.I)
_YES = re.compile(r"^\s*(?:yes|y|true)\b", re.I)
_DECLINE = re.compile(r"decline|prefer not|don't wish|not to (?:say|answer|disclose)", re.I)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _leaves(data):
    if isinstance(data, dict):
        for value in data.values():
            yield from _leaves(value)
    elif isinstance(data, list):
        for value in data:
            yield from _leaves(value)
    elif data is not None:
        yield data


def _rank(text: str) -> int | None:
    return next((rank for pattern, rank in _RANKS if pattern.search(text)), None)


def _years_since(start, end, today) -> float:
    """The years from a job's start to its end ("2021-03", "Jun 2018", "02/2021", "present")."""
    def month(text) -> tuple[int, int]:
        text = str(text)
        if text.lower() == "present":
            return today.year, today.month
        if m := re.fullmatch(r"(\d{4})-(\d{2})", text):
            return int(m[1]), int(m[2])
        if m := re.fullmatch(r"(\d{2})/(\d{4})", text):
            return int(m[2]), int(m[1])
        return int(re.search(r"\d{4}", text)[0]), 1
    (y0, m0), (y1, m1) = month(start), month(end)
    return y1 - y0 + (m1 - m0) / 12


class ProfileOracle:
    """What a profile says, in every form the desk may write it, and the claims it makes."""

    def __init__(self, data: dict):
        import datetime

        today = datetime.date.today()
        personal, address = data["personal"], data["personal"]["address"]
        self.known = {str(v).strip().lower() for v in _leaves(data) if not isinstance(v, bool)}
        first, last = personal["first_name"], personal["last_name"]
        self.known |= {f"{first} {last}".lower(), f"{last}, {first}".lower(), "yes", "no",
                       f"{address['city']}, {address['state']}".lower()}
        d = re.sub(r"\D", "", personal["phone"])
        self.known |= {d, f"({d[:3]}) {d[3:6]}-{d[6:]}", f"{d[:3]}-{d[3:6]}-{d[6:]}", f"{d[:3]} {d[3:6]} {d[6:]}",
                       f"{d[:3]}.{d[3:6]}.{d[6:]}", f"+1{d}", f"+1 {d[:3]}-{d[3:6]}-{d[6:]}", f"+1 {d}"}
        self.known |= {today.strftime(f).lower() for f in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%y",
                                                           "%B %d, %Y", "%m-%d-%Y", "%d.%m.%Y")}
        # (a state's and a country's other names: conftest's AZ is Arizona, its United States the US)
        self.known |= {"arizona"} if address["state"] == "AZ" else set()
        if address["country"] == "United States":
            self.known |= {"us", "usa", "u.s.", "united states of america"}
        # the numbers it holds, and the years of experience its jobs make (each job's, and since the first began)
        self.numbers = {float(x) for v in _leaves(data) if not isinstance(v, bool) for x in _NUMBER.findall(str(v))}
        jobs = data.get("work_history") or []
        spans = [_years_since(j["start"], j["end"], today) for j in jobs]
        spans += [max((_years_since(j["start"], "present", today) for j in jobs), default=0.0)]
        self.numbers |= {round(s, 1) for s in spans} | {float(int(s)) for s in spans}
        self.rank = max((_rank(str(e.get("degree"))) or 0 for e in data.get("education_history") or []), default=0)
        self.bank_yes: set[str] = set()

    def load(self, profile) -> None:
        """The person's own answers as the desk reads them (Profile.load brings old patterns up to date)."""
        self.profile = profile
        self.bank_yes = {str(a["match"]) for a in profile.get("answers") or []
                         if a.get("answer") is not None and _YES.match(str(a["answer"]))}

    def profile_says_yes(self, rule: str) -> bool:
        """Is this rule's Yes the profile's: its getter says yes from this profile, and nothing from an empty one?"""
        from job_apply.autofill import RULES
        from job_apply.config import Profile

        job = {"company": "Example Litho"}
        for name, _, getter, _, _ in RULES:
            if name == rule:
                return bool(_YES.match(str(getter(self.profile, job)))) and \
                    not _YES.match(str(getter(Profile({}), job)))
        return False

    def number_fits(self, choice: str) -> bool:
        """Is some number the profile holds in this choice's range ("3", "10+", "3-5 years", "Less than 1 year")?"""
        nums = [float(x) for x in _NUMBER.findall(choice)]
        if re.search(r"less than|under", choice, re.I):
            low, high = 0.0, nums[0]
        elif "+" in choice or re.search(r"or more|more than", choice, re.I):
            low, high = nums[0], float("inf")
        elif len(nums) >= 2:
            low, high = nums[0], nums[1]
        else:
            low, high = nums[0], nums[0] + 1
        return any(low <= n <= high for n in self.numbers)

    def wrong(self, field: dict, value, rule: str) -> str | None:
        """Why this answer says more than the profile does, or None."""
        label, kind = field["label"], field["kind"]
        from_bank = any(rule == f"answers[{m}]" for m in self.bank_yes)
        if kind == "checkbox" or isinstance(value, bool):
            if value is True and not from_bank:
                return "a box ticked (or a True) that no Yes of the person's own answers ticks"
            return None
        text = str(value).strip()
        if _YES.match(text) and not from_bank and _CLAIMS.search(label) and not self.profile_says_yes(rule):
            return "a Yes to a qualification or a status the profile doesn't name"
        options = field.get("options") or []
        if text in options:
            rank = _rank(text)
            if rank is not None:
                return "a degree above the profile's" if rank > self.rank else None
            if _NUMBER.search(text):
                return None if self.number_fits(text) else "a count or a span no number of the profile's is in"
            if _YES.match(text) or _DECLINE.search(text) or text.lower() in self.known:
                return None
            return "a choice the profile doesn't make"
        if text.lower() in self.known or _NUMBER.fullmatch(text) and float(text) in self.numbers:
            return None
        return "a value that is no form of anything in the profile"


def _fuzz(oracle: ProfileOracle, profile, count: int = FUZZ_LABELS) -> tuple[int, list[tuple]]:
    from job_apply.autofill import resolve_field

    rng = _rng("resolve-field")
    answered, wrong = 0, []
    for i in range(count):
        label = " ".join(rng.choice(FUZZ_WORDS) for _ in range(rng.randint(1, 7)))
        field = {"id": f"f{i}", "label": label, "kind": rng.choice(KINDS)}
        if field["kind"] in ("select", "radio"):
            field["options"] = rng.choice(OPTION_SETS)
        answer = resolve_field(field, profile, {"company": "Example Litho", "title": "Field Service Engineer"})
        if answer is None:
            continue
        answered += 1
        why = oracle.wrong(field, answer.value, answer.rule)
        if why:
            wrong.append((why, label, field["kind"], answer.value, answer.rule))
    return answered, wrong


def _oracle_and_profile():
    from conftest import PROFILE

    from job_apply.config import Profile

    oracle, profile = ProfileOracle(PROFILE), Profile.load()
    oracle.load(profile)
    return oracle, profile


def test_resolve_field_never_claims_more_than_the_profile_says():
    import time

    oracle, profile = _oracle_and_profile()
    start = time.monotonic()
    answered, wrong = _fuzz(oracle, profile)
    took = time.monotonic() - start
    assert not wrong, "\n".join(map(repr, wrong[:20]))
    assert answered > FUZZ_LABELS // 10  # (the fuzz reaches the rules: about one label in six is answered)
    assert took < 5, took


def test_the_fuzz_fails_a_planted_rule_that_claims_a_masters(monkeypatch):
    """The oracle bites: a rule answering Yes to any Master's question (planted here, never in the desk) fails it."""
    from job_apply import autofill

    planted = ("planted_masters", r"master", lambda prof, job: "Yes", None, None)
    monkeypatch.setattr(autofill, "RULES", [planted, *autofill.RULES])
    oracle, profile = _oracle_and_profile()
    _, wrong = _fuzz(oracle, profile)
    assert any(why.startswith("a Yes") and rule == "planted_masters" for why, *_, rule in wrong), wrong[:5]
