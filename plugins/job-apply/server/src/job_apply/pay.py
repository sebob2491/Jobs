"""A job's pay, as its posting gives it: structured first (JSON-LD's baseSalary, a job board's salary field: Indeed's),
then the posting's own words ("Salary Range $26.00 - $28.00 Hourly", "Base Salary Range: $71,360.00 - $107,040.00").

Only US dollars, and only figures that read as the job's pay: a figure must be marked as money ($, USD, 95k, or a pay
label's "Salary range: 80,000 - 100,000") and sit near a pay word, in a band that's pay for its period. A figure that's
a bonus, an allowance, a stipend, a 401(k) match, equity, relocation, a company's millions or a count isn't, whether
the words saying so come before it or after; nor is one under an applicant's question ("What is your desired
salary?"). The period is the one said next to the figure, never another figure's. What isn't found is "not listed",
which never hides or lowers a job; when unsure, a figure is left out rather than shown wrong (the design of Oct 18,
Part 7, and its review)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

# where pay may be stated: the window round each of these words is read for figures
_KEY = re.compile(r"(?i)\b(?:salary|salaries|pay|pays|compensation|wages?|hourly|rate|annual|earn)\b"
                  r"|\b(?:per|an?)\s+(?:hour|year|month|week)\b|/\s?(?:hr|yr|hour|year)\b|\bannually\b")
_BEFORE, _AFTER = 120, 220  # characters read before and after a pay word
# the applicant's own pay, not the job's: before a pay word ("What is your desired compensation?")
_THEIRS_KEY = re.compile(r"(?i)\b(?:desired|current|previous|prior|your)\s+(?:\w+\s+)?$")
# ... and on a figure's own line before it: a question, or the applicant's salary
_THEIRS = re.compile(r"(?i)\?|\b(?:desired|current|previous|prior|your)\s+(?:\w+\s+)?(?:salary|pay|compensation|wages?|"
                     r"rate)\b|\b(?:salary|pay|compensation)\s+(?:expectations?|requirements?|history)\b"
                     r"|\bexpected\s+(?:salary|pay|compensation|wages?)\s*[:?]")
# a figure: "$71,360.00", "USD 80000", "$45.2K", "28.00"; not one in a word or run straight into letters ("$15M")
_TOKEN = re.compile(r"(?<![\w.])(?P<cur>\$|USD\s?|US\$)?\s?(?P<n>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
                    r"(?P<k>[kK](?![A-Za-z]))?(?![\d,]|[A-Za-z])")
# where a clause ends: a figure's own label is after the last of these ("." ends a sentence, not "71,360.00")
_BOUNDARY = re.compile(r"[;|\n!?]|\.(?=\s|$)")
_CLAUSE = 50  # characters a figure's own clause reaches back at most
_PERIOD_WORDS = (r"(?P<biweekly>bi-?\s?weekly|every\s+(?:two|2)\s+weeks)|(?P<semimonthly>semi-?\s?monthly|twice\s+"
                 r"(?:a|per)\s+month)|(?P<hour>hours?|hrs?|hourly)|(?P<year>years?|yrs?|annum|annual(?:ly|ized)?|"
                 r"yearly)|(?P<month>months?|mos?|monthly)|(?P<week>weeks?|wks?|weekly)|(?P<day>days?|daily)")
# the period said right after a figure or a range: "/hr", " per hour", " an hour", " Hourly", " USD annually"
_PERIOD_AFTER = re.compile(rf"(?i)^\s*(?:USD|US dollars|dollars|\$)?\s*(?:/\s*|per\s+|an?\s+)?(?:{_PERIOD_WORDS})\b")
# ... or in a figure's own label before it, the nearest: "Hourly Rate:", "Annual Salary Range (Arizona):", "per week"
_PERIOD_LABEL = re.compile(r"(?i)\b(?:(?P<biweekly>bi-?\s?weekly)|(?P<semimonthly>semi-?\s?monthly)|"
                           r"(?P<hour>hourly|per\s+hour)|(?P<year>annual(?:ly)?|yearly|per\s+(?:year|annum))|"
                           r"(?P<month>monthly|per\s+month)|(?P<week>weekly|per\s+week)|(?P<day>daily|per\s+day))\b")
# between a range's two figures, each perhaps with its period or currency: "$24.00/hr to $30.00/hr", "75,000 USD -"
_SEP = re.compile(r"(?i)^\s*(?:(?:/\s*|per\s+|an?\s+)?[a-z]+\.?\s*|USD\s*)?(?:-|–|—|to|and|through|thru)\s*$")
_MIN_LABEL = re.compile(r"(?i)\bmin(?:imum)?\b\W*$")
_MAX_GAP = re.compile(r"(?i)^[^$\d]{0,40}\bmax(?:imum)?\b[^$\d]{0,15}$")  # "Min: $20/hr, Max: $25/hr"
_CURRENCY_AFTER = re.compile(r"(?i)^\s*(?:USD|US dollars|dollars|\$\s*/)")  # "70,000 - 90,000 USD", "19 - 23 $/hr"
# a pay label right before a figure that marks it as money: "Salary range: 80,000 - 100,000", "Hourly rate: 22.00"
_LABEL = re.compile(r"(?i)\b(?:salary|pay|wages?|compensation|rate)(?:\s+(?:range|rate|min(?:imum)?|max(?:imum)?))?"
                    r"\s*:\s*$")
# a count right after a figure: "5 years", "40 hours", "2 openings", "10%"
_COUNT = re.compile(r"(?i)^\s*\+?\s*(?:%|(?:hours|hrs|years|yrs|days|weeks|months|shifts?|employees|people|"
                    r"locations?|openings?|positions?|sites?|percent)\b)")
# what a figure that isn't the job's pay is ("$2,000 sign-on bonus", "$500/month stipend", "$100 million")
_NONPAY = (r"(?:bonus(?:es)?|sign[- ]?on|signing|hiring\s+bonus|match(?:es|ing)?|million|billion|mm|lbs?|pounds?|"
           r"percent|stipends?|allowances?|reimburse\w*|tuition|deductibles?|premiums?|discounts?|employees|"
           r"referrals?|relocation|assistance|credits?|gifts?|budgets?|revenue|funding|retention|payments?|awards?|"
           r"equity|rsus?|stock|grants?|differentials?|travel|per\s+diem|overtime|on-?call|401\s?\(?k\)?)(?!\w)")
# ... in the two words after it and its period, on its line ("$3/hr shift differential", "$100 per hour of on-call";
# not past "plus", "and", "base salary": "$90,000 base plus bonus" is pay; nor the next item's label, "Overtime:")
_NOT_PAY_AFTER = re.compile(rf"(?i)^[ \t]*(?:(?:/[ \t]*|per[ \t]+|an?[ \t]+)?(?:{_PERIOD_WORDS})\b[ \t]*)?"
                            r"(?:(?!(?:plus|and|with|base|salary|pay|\+)(?!\w))[^\s$.;:|!?,]+[ \t]+){0,2}"
                            rf"{_NONPAY}(?![ \t]*:)")
# ... or in its own clause before it ("Sign-On Bonus: $15,000", "Car allowance $500/month", "RSU grant of $50,000"),
# unless a pay word comes after that ("Retention Specialist pay: $20/hr"; not "bonus pay")
_NOT_PAY_BEFORE = re.compile(rf"(?i)\b{_NONPAY}|\bbenefits?\s+valued|\bvalued\s+at\b|\bworth\b")
_PAY_WORD_BEFORE = re.compile(r"(?i)\b(?:salary|pay|wages?|compensation|rate)\b")
_UP_TO = re.compile(r"(?i)\bup to\b|\bmaximum\b|\bmax\.?\s*:?\s*$|not to exceed")
_FROM = re.compile(r"(?i)\bstarting\b|\bstarts at\b|\bfrom\b|\bminimum\b|\bat least\b|\bmin\.?\s*:?\s*$")
_PAY_WORD = r"(?:salary|pay|compensation|wages?|rate)"
_DOE_WORD = r"(?:DOE|commensurate|competitive|based on experience|negotiable)"
# "Salary: DOE", "competitive pay": only within 25 characters of a pay word ("DOE methodologies" in a chip posting
# isn't)
_DOE = re.compile(rf"(?i)\b{_PAY_WORD}\b[^.\n]{{0,25}}?\b{_DOE_WORD}\b|\b{_DOE_WORD}\b[^.\n]{{0,25}}?\b{_PAY_WORD}\b")
_PAIR_WITHIN = 60  # a lone figure and the next pay figure this near, of its period: a range ("$15/hr ..., then $20/hr")
_READ_MOST = 100_000  # a text's first and last this many characters are read (pay lines sit at the end)
# what one period's pay comes to in a year (shown with ≈)
YEARLY = {"hour": 2080, "year": 1, "month": 12, "week": 52, "day": 260, "biweekly": 26, "semimonthly": 24}
# a figure that's pay for its period: outside these it's something else (a count, a price, a typo)
_BANDS = {"hour": (7, 300), "year": (15_000, 2_000_000), "month": (1_000, 40_000), "week": (250, 10_000),
          "day": (50, 2_000), "biweekly": (500, 20_000), "semimonthly": (500, 20_000)}
# where a pay came from, best first: a posting's own data, a job board's salary field, the posting's words
SOURCES = {"structured": 3, "indeed": 2, "text": 1}
_LD_UNITS = {"HOUR": "hour", "DAY": "day", "WEEK": "week", "MONTH": "month", "YEAR": "year"}
_SUFFIX = {"hour": "/hr", "year": "/yr", "month": "/mo", "week": "/wk", "day": "/day", "biweekly": " every 2 weeks",
           "semimonthly": " twice a month"}


@dataclass
class Pay:
    min: float | None
    max: float | None
    period: str  # hour, day, week, biweekly, semimonthly, month, year; "" for pay without figures ("Salary: DOE")
    source: str = "text"
    text: str = ""  # what it was read from, as said there

    @property
    def listed(self) -> bool:
        """Whether it gives a figure (not only "depends on experience")."""
        return self.min is not None or self.max is not None


def yearly(pay: Pay | None) -> tuple[float | None, float | None]:
    """The pay over a year (hour × 2,080, week × 52, two weeks × 26, month × 12): (None, None) without figures."""
    factor = YEARLY.get(pay.period) if pay is not None else None
    if pay is None or not factor:
        return None, None
    return (pay.min * factor if pay.min is not None else None), (pay.max * factor if pay.max is not None else None)


def meets(pay: Pay | None, floor: float | None) -> bool | None:
    """Whether the pay reaches `floor` a year: True, False when it's listed and its top is below, None when that can't
    be told (not listed, or "starting at" a figure below it). Only False hides a job."""
    if not floor or pay is None or not pay.listed:
        return None
    lo, hi = yearly(pay)
    if hi is not None:
        return hi >= floor
    return True if lo is not None and lo >= floor else None


def _value(m: re.Match[str]) -> float:
    v = float(m.group("n").replace(",", ""))
    return v * 1000 if m.group("k") else v


def _sane(v: float, period: str) -> bool:
    lo, hi = _BANDS.get(period, (1, 0))
    return lo <= v <= hi


def _snippet(text: str, start: int, end: int) -> str:
    """The words round a figure, on its own line: what the desk's tooltip quotes."""
    line_start = text.rfind("\n", 0, start) + 1
    lo = max(line_start, start - 60)
    if lo > line_start:  # (from a word's start)
        space = text.find(" ", lo, start)
        lo = space + 1 if space >= 0 else lo
    line_end = text.find("\n", end)
    hi = min(line_end if line_end >= 0 else len(text), end + 40)
    if hi < (line_end if line_end >= 0 else len(text)):
        space = text.rfind(" ", end, hi)
        hi = space if space >= 0 else hi
    return " ".join(text[lo:hi].split())


def _windows(text: str) -> list[tuple[int, int]]:
    """The stretches round each pay word, overlapping ones joined, in the text's order. Not round the applicant's own
    ("What is your desired compensation?", "current salary": an application's question, not the job's pay)."""
    out: list[tuple[int, int]] = []
    for k in _KEY.finditer(text):
        if _THEIRS_KEY.search(text[max(0, k.start() - 30):k.start()]):
            continue
        lo, hi = max(0, k.start() - _BEFORE), min(len(text), k.end() + _AFTER)
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


class _Reader:
    """One text's money figures, each read for whether it's the job's pay, and its period."""

    def __init__(self, text: str):
        self.text = text
        self.tokens = list(_TOKEN.finditer(text))

    def clause(self, i: int) -> str:
        """The words before figure i in its own clause: back to the clause's start, the figure before it, or
        _CLAUSE characters."""
        start = self.tokens[i].start()
        lo = max(0, start - _CLAUSE, self.tokens[i - 1].end() if i else 0)
        part = self.text[lo:start]
        ends = [m.end() for m in _BOUNDARY.finditer(part)]
        return part[ends[-1]:] if ends else part

    def refused(self, i: int) -> bool:
        """Whether figure i is something other than the job's pay."""
        t, text = self.tokens[i], self.text
        after = text[t.end():t.end() + 60]
        if _COUNT.match(after) or _NOT_PAY_AFTER.match(after):
            return True
        if t.group("n") == "401" and (t.group("k") or not t.group("cur")) \
                or re.search(r"(?i)401\s?\(?k\)?\s*$", text[max(0, t.start() - 8):t.start()]):
            return True
        clause = self.clause(i)
        nonpay = list(_NOT_PAY_BEFORE.finditer(clause))
        if nonpay:
            last = nonpay[-1]
            word = next((w for w in _PAY_WORD_BEFORE.finditer(clause, last.end())), None)
            if word is None or not clause[last.end():word.start()].strip(" -"):
                return True
        line = text[max(text.rfind("\n", 0, t.start()) + 1, t.start() - 150):t.start()]
        return bool(_THEIRS.search(line))

    def marked(self, i: int, end: int) -> bool:
        """Whether figure i reads as money: "$", "USD", "95k", a currency after its range, or a pay label before it."""
        t = self.tokens[i]
        return bool(t.group("cur") or t.group("k") or _CURRENCY_AFTER.match(self.text[end:end + 12])
                    or _LABEL.search(self.clause(i)))

    def period(self, i: int, end: int, a: float, b: float | None, k: bool) -> str:
        """The period of figure i (and its range, to `end`): said right after, else in its own label, else inferred
        (a top under 300 is an hourly rate; 15,000 or more, or "95k", a year's); "" when it can't be told."""
        m = _PERIOD_AFTER.match(self.text[end:end + 30])
        if m:
            return m.lastgroup or ""
        said = list(_PERIOD_LABEL.finditer(self.clause(i)))
        if said:
            return said[-1].lastgroup or ""
        top = b if b is not None else a
        return "hour" if top < 300 else "year" if top >= 15_000 or k else ""

    def pair(self, i: int) -> int | None:
        """The figure after figure i that ends its range, if one does: "$24 - $30", "$24.00/hr to $30.00/hr",
        "Min: $20/hr, Max: $25/hr"."""
        if i + 1 >= len(self.tokens):
            return None
        gap = self.text[self.tokens[i].end():self.tokens[i + 1].start()]
        if _SEP.match(gap) or (_MIN_LABEL.search(self.clause(i)) and _MAX_GAP.match(gap)):
            return None if self.refused(i + 1) else i + 1
        return None

    def read(self, i: int) -> tuple[Pay | None, int]:
        """Figure i as pay (with the next figure ending its range), or None; and the next figure to read."""
        if self.refused(i):
            return None, i + 1
        first = self.tokens[i]
        j = self.pair(i)
        second = self.tokens[j] if j is not None else None
        end = (second or first).end()
        if not self.marked(i, end):
            return None, i + 1
        a, b = _value(first), (_value(second) if second else None)
        if second is not None and second.group("k") and not first.group("k") and a < 1000:
            a *= 1000  # "$90–110k"
        if second is not None and first.group("k") and not second.group("k") and b is not None and b < 1000:
            b *= 1000  # "$90k-110"
        period = self.period(i, end, a, b, bool(first.group("k") or (second and second.group("k"))))
        if not period or not all(_sane(v, period) for v in (a, b) if v is not None):
            return None, (j or i) + 1
        if b is not None:
            return Pay(min(a, b), max(a, b), period, "text", _snippet(self.text, first.start(), end)), (j or i) + 1
        # a lone figure: the next pay figure of its period close after it ends its range ("$15 per hour (training
        # rate), then $20 per hour"; "from $50,000 (entry) to $90,000 (senior)")
        n = i + 1
        if n < len(self.tokens) and self.tokens[n].start() - end <= _PAIR_WITHIN and not self.refused(n):
            other = self.tokens[n]
            c = _value(other)
            if self.marked(n, other.end()) and self.period(n, other.end(), c, None, bool(other.group("k"))) == period \
                    and _sane(c, period):
                return Pay(min(a, c), max(a, c), period, "text",
                           _snippet(self.text, first.start(), other.end())), n + 1
        before = self.clause(i)[-25:]
        lo, hi = (None, a) if _UP_TO.search(before) else (a, None) if _FROM.search(before) else (a, a)
        return Pay(lo, hi, period, "text", _snippet(self.text, first.start(), end)), i + 1


def _widest(found: list[Pay]) -> Pay:
    """The pays a text states for the first one's period as one: from the least to the most (several states' or levels'
    ranges: the widest, so none of them is hidden by a floor the job meets somewhere). An open end stays open."""
    first = found[0]
    same = [p for p in found if p.period == first.period]
    lo = None if any(p.min is None for p in same) else min(p.min for p in same if p.min is not None)
    hi = None if any(p.max is None for p in same) else max(p.max for p in same if p.max is not None)
    return Pay(lo, hi, first.period, first.source, first.text)


def parse_pay(text: str, whole: bool = False, source: str = "text") -> Pay | None:
    """The pay `text` states, or None. `whole`: the text is a pay field (Indeed's "$25 an hour", JSON-LD's printed
    "USD 80000-110000 per year"), not running text, so no pay word is needed near it. Several ranges (one a state,
    one a level) are read as one, from the least to the most. Only the first and last _READ_MOST characters of a very
    long text are read."""
    text = (text or "").replace(" ", " ")
    if len(text) > 2 * _READ_MOST:
        text = text[:_READ_MOST] + "\n" + text[-_READ_MOST:]
    if not text.strip():
        return None
    reader = _Reader(text)
    windows = [(0, len(text))] if whole else _windows(text)
    found: list[Pay] = []
    i = 0
    while i < len(reader.tokens):
        start = reader.tokens[i].start()
        if not any(lo <= start < hi for lo, hi in windows):
            i += 1
            continue
        pay, i = reader.read(i)
        if pay is not None:
            found.append(pay)
    if found:
        pay = _widest(found)
        pay.source = source
        return pay
    m = _DOE.search(text)
    return Pay(None, None, "", source, " ".join(m.group(0).split())) if m else None


def _number(v: Any) -> float | None:
    """A figure over 0 (and finite), or None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        n = float(v)
    else:
        try:
            n = float(str(v).replace(",", "").replace("$", "").strip())
        except ValueError:
            return None
    return n if math.isfinite(n) and n > 0 else None


def pay_from_ld(salary: Any, in_us: bool = False) -> Pay | None:
    """JSON-LD's baseSalary ({"currency": "USD", "value": {"minValue": 80000, "maxValue": 110000, "unitText": "YEAR"}}),
    as its figures: US dollars only (one that names no currency only for a posting in the US: `in_us`), and only
    figures that are pay for their period (an hourly rate given as a year's isn't). None for anything else, for the
    posting's words to say."""
    if isinstance(salary, list):
        salary = salary[0] if salary else None
    if not isinstance(salary, dict):
        return None
    currency = str(salary.get("currency") or ("USD" if in_us else "")).strip().upper()
    if currency not in ("USD", "US$", "$"):
        return None
    value = salary.get("value")
    unit = str(salary.get("unitText") or "")
    if isinstance(value, dict):
        unit = str(value.get("unitText") or unit)
        lo, hi = _number(value.get("minValue")), _number(value.get("maxValue"))
        one = _number(value.get("value"))
        if lo is None and hi is None:
            lo = hi = one
    else:
        lo = hi = _number(value)
    if lo is None and hi is None:
        return None
    period = _LD_UNITS.get(unit.strip().upper(), "")
    if not period:
        top = hi if hi is not None else lo
        period = "hour" if top is not None and top < 300 else "year" if top is not None and top >= 15_000 else ""
    if not period or not all(_sane(v, period) for v in (lo, hi) if v is not None):
        return None
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    shown = "-".join(f"{v:g}" for v in dict.fromkeys(v for v in (lo, hi) if v is not None))
    return Pay(lo, hi, period, "structured", f"{currency} {shown} per {period}")


def pay_from_posting(posting: Any) -> Pay | None:
    """A read posting's pay (postings.Posting): what its structured data gave (posting_from_jsonld fills it in), else
    its salary field (JSON-LD's printed one, or a job board's: add_job's from Indeed), else its words: the whole text,
    before MAX_DESCRIPTION's cut (pay is often the last thing a long posting says), then the page's description."""
    given = from_fields({k: getattr(posting, k, None) for k in ("pay_min", "pay_max", "pay_period", "pay_source")})
    if given is not None and given.source == "structured":
        return given
    salary = str(getattr(posting, "salary", "") or "")
    if salary:
        board = "indeed" in {str(getattr(posting, "source", "") or "").lower(), getattr(posting, "ats", "")} \
            or "indeed." in str(getattr(posting, "url", "")).lower()
        source = "indeed" if board else "structured" if getattr(posting, "parse_method", "") == "json-ld" else "text"
        found = parse_pay(salary, whole=True, source=source)
        if found is not None and found.listed:
            return found
    text = getattr(posting, "full_text", "") or getattr(posting, "description", "")
    return parse_pay(text)


def from_fields(fields: dict[str, Any]) -> Pay | None:
    """The pay a listing or the tracker's job holds (pay_min, pay_max, pay_period, pay_source), or None."""
    lo, hi = _number(fields.get("pay_min")), _number(fields.get("pay_max"))
    period = str(fields.get("pay_period") or "")
    if lo is None and hi is None or period not in YEARLY:
        return None
    return Pay(lo, hi, period, str(fields.get("pay_source") or "text"))


def fields(pay: Pay | None) -> dict[str, Any]:
    """A pay as the four fields a posting, a listing and the tracker keep."""
    if pay is None or not pay.listed:
        return {"pay_min": None, "pay_max": None, "pay_period": "", "pay_source": ""}
    return {"pay_min": pay.min, "pay_max": pay.max, "pay_period": pay.period, "pay_source": pay.source}


def better(new: dict[str, Any], old: Any, again: bool = False) -> bool:
    """Whether a posting's pay fields (`new`) should replace a job's (`old`, the tracker's row): when the job has none,
    or the new one's source ranks higher (structured > indeed > text), or, for the same posting read again (`again`),
    ranks the same (the newer read: a parser's fix corrects what an older one read). Never a lower rank, and never by
    no pay (Tracker clears a text pay a new read of its own posting no longer finds)."""
    if _number(new.get("pay_min")) is None and _number(new.get("pay_max")) is None:
        return False
    if _number(old["pay_min"]) is None and _number(old["pay_max"]) is None:
        return True
    rank, was = SOURCES.get(str(new.get("pay_source") or ""), 0), SOURCES.get(str(old["pay_source"] or ""), 0)
    return rank > was or again and rank == was


def _money(v: float, period: str) -> str:
    if period == "year" or v >= 10_000 and period in ("month", "week"):
        return f"${round(v / 1000):,}k"
    return f"${v:,.2f}" if v != int(v) else f"${int(v):,}"


def _span(lo: float | None, hi: float | None, money: Any, words: bool = True) -> str:
    if lo is not None and hi is not None:
        return money(lo) if money(lo) == money(hi) else f"{money(lo)}–{money(hi)}"
    one = money(lo if lo is not None else hi)
    return one if not words else f"from {one}" if lo is not None else f"up to {one}"


def shown(pay: Pay | None) -> str:
    """A pay in a few words for the desk's row: "$26–$28/hr (≈$54k–$58k/yr)", "$71k–$107k/yr",
    "from $22/hr (≈$46k/yr)"; "" without figures."""
    if pay is None or not pay.listed or pay.period not in YEARLY:
        return ""
    said = _span(pay.min, pay.max, lambda v: _money(v, pay.period)) + _SUFFIX[pay.period]
    if pay.period == "year":
        return said
    lo, hi = yearly(pay)
    return f"{said} (≈{_span(lo, hi, lambda v: _money(v, 'year'), words=False)}/yr)"


_SOURCE_WORDS = {"structured": "From the posting's own data",
                 "indeed": "From Indeed's salary field (it may be Indeed's estimate)",
                 "text": "From the posting's text"}


def view(fields_of: dict[str, Any], salary: str = "") -> dict[str, Any] | None:
    """What the desk's row says of a job's pay (a listing's fields, or the tracker's job's), or None when it isn't
    listed: the words, where they came from (quoting the salary field's own words, `salary`, only when they say these
    figures: never another source's), and the yearly figures to sort and filter by: `top`, the most it pays a year
    (None for "from $X"), and `sort`, the most it pays a year or, for "from $X", X a year."""
    pay = from_fields(fields_of)
    if pay is None:
        return None
    lo, hi = yearly(pay)
    said = " ".join(str(salary or "").split())
    same = parse_pay(said, whole=True) if said else None
    if same is None or (same.min, same.max, same.period) != (pay.min, pay.max, pay.period):
        said = ""
    return {"shown": shown(pay), "source": pay.source,
            "from": _SOURCE_WORDS.get(pay.source, pay.source) + (f": “{said}”" if said else ""),
            "yearly_min": lo, "top": hi, "sort": hi if hi is not None else lo}


def desired_yearly(value: Any) -> float | None:
    """preferences.desired_salary as a yearly figure: "$85,000", "85000", "85k", "$40/hr" (× 2,080)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = _number(value)
        return None if n is None or n > 10_000_000 else n * 2080 if n < 300 else n
    said = str(value).strip()
    pay = parse_pay(said, whole=True)
    if pay is None or not pay.listed:
        pay = parse_pay(f"${said.lstrip('$')}", whole=True)  # (a bare "85000")
    if pay is None or not pay.listed:
        return None
    lo, hi = yearly(pay)
    return lo if lo is not None else hi
