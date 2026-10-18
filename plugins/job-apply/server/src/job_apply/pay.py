"""A job's pay, as its posting gives it: structured first (JSON-LD's baseSalary, a job board's salary field: Indeed's),
then the posting's own words ("Salary Range $26.00 - $28.00 Hourly", "Base Salary Range: $71,360.00 - $107,040.00").

Only US dollars, and only figures that read as pay: a figure must be marked as money ($, USD, 95k) and sit near a pay
word, in a band that's pay for its period; a figure that's a bonus, a 401(k) match, a company's millions or a weight
isn't. What isn't found is "not listed", which never hides or lowers a job (the design of Oct 18, Part 7)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# where pay may be stated: the window round each of these words is read for figures
_KEY = re.compile(r"(?i)\b(?:salary|salaries|pay|compensation|wages?|hourly|rate|annual|earn)\b"
                  r"|\b(?:per|an?)\s+(?:hour|year|month|week)\b|/\s?(?:hr|yr|hour|year)\b|\bannually\b")
_BEFORE, _AFTER = 120, 220  # characters read before and after a pay word
_THEIRS = re.compile(r"(?i)\b(?:desired|current|previous|prior)\s+(?:\w+\s+)?$")  # before a pay word
# a figure: "$71,360.00", "USD 80000", "$45.2K", "28.00"; not one in a word or run straight into letters ("$15M")
_TOKEN = re.compile(r"(?<![\w.])(?P<cur>\$|USD\s?|US\$)?\s?(?P<n>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
                    r"(?P<k>[kK](?![A-Za-z]))?(?![\d,]|[A-Za-z])")
_SEP = re.compile(r"^\s*(?:-|–|—|to|and|through|thru)\s*$")  # between a range's two figures
_CURRENCY_AFTER = re.compile(r"^\s*(?:USD|US dollars|dollars)\b")  # "70,000 - 90,000 USD per year"
_PERIODS = [("hour", r"(?i)(?:\b(?:per|an|a)\s+|/\s?)(?:hour|hr)s?\b|\bhourly\b"),
            ("year", r"(?i)(?:\b(?:per|a)\s+|/\s?)(?:year|yr|annum)\b|\bannual(?:ly|ized)?\b|\byearly\b"),
            ("month", r"(?i)(?:\b(?:per|a)\s+|/\s?)(?:month|mo)\b|\bmonthly\b"),
            ("week", r"(?i)(?:\b(?:per|a)\s+|/\s?)(?:week|wk)\b|\bweekly\b"),
            ("day", r"(?i)(?:\b(?:per|a)\s+|/\s?)(?:day|diem)\b|\bdaily\b")]
# the words right after a figure that say it isn't pay: "$2,000 sign-on bonus", "$1,500 referral bonus", "$100 million"
_NOT_PAY = re.compile(r"(?i)^\s*(?:\w+[-\w]*\s+)?(?:bonus|sign[- ]?on|match\w*|million|billion|lbs?|pounds?|"
                      r"percent|stipend|allowance|reimburse\w*|tuition|deductible|premium|discount|employees|referral|"
                      r"relocation|assistance|credits?|gift|budget|revenue|sales|in\s+(?:revenue|sales|funding))\b")
_401K = re.compile(r"(?i)401\s?\(?k\)?\s*$")
_PAY_WORD = r"(?:salary|pay|compensation|wages?|rate)"
_DOE_WORD = r"(?:DOE|commensurate|competitive|based on experience|negotiable)"
# "Salary: DOE", "competitive pay": only within 25 characters of a pay word ("DOE methodologies" in a chip posting
# isn't)
_DOE = re.compile(rf"(?i)\b{_PAY_WORD}\b[^.\n]{{0,25}}?\b{_DOE_WORD}\b|\b{_DOE_WORD}\b[^.\n]{{0,25}}?\b{_PAY_WORD}\b")
# what one period's pay comes to in a year (shown with ≈)
YEARLY = {"hour": 2080, "year": 1, "month": 12, "week": 52, "day": 260}
# a figure that's pay for its period: outside these it's something else (a count, a price, a typo)
_BANDS = {"hour": (7, 500), "year": (15_000, 2_000_000), "month": (1_000, 150_000), "week": (250, 40_000),
          "day": (50, 5_000)}
# where a pay came from, best first: a posting's own data, a job board's salary field, the posting's words
SOURCES = {"structured": 3, "indeed": 2, "text": 1}
_LD_UNITS = {"HOUR": "hour", "DAY": "day", "WEEK": "week", "MONTH": "month", "YEAR": "year"}
_SHORT = {"hour": "hr", "year": "yr", "month": "mo", "week": "wk", "day": "day"}


@dataclass
class Pay:
    min: float | None
    max: float | None
    period: str  # hour, day, week, month, year; "" for pay without figures ("Salary: DOE")
    source: str = "text"
    text: str = ""  # what it was read from, as said there

    @property
    def listed(self) -> bool:
        """Whether it gives a figure (not only "depends on experience")."""
        return self.min is not None or self.max is not None


def yearly(pay: Pay | None) -> tuple[float | None, float | None]:
    """The pay over a year (hour × 2,080, week × 52, month × 12): (None, None) without figures."""
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


def _period(text: str, start: int, end: int) -> str:
    after, before = text[end:end + 40], text[max(0, start - 60):start]
    for part in (after, before):
        for name, pattern in _PERIODS:
            if re.search(pattern, part):
                return name
    return ""


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
        if _THEIRS.search(text[max(0, k.start() - 30):k.start()]):
            continue
        lo, hi = max(0, k.start() - _BEFORE), min(len(text), k.end() + _AFTER)
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def parse_pay(text: str, whole: bool = False, source: str = "text") -> Pay | None:
    """The first pay `text` states, or None. `whole`: the text is a pay field (Indeed's "$25 an hour", JSON-LD's printed
    "USD 80000-110000 per year"), not running text, so no pay word is needed near it."""
    text = (text or "").replace(" ", " ")
    if not text.strip():
        return None
    for lo, hi in [(0, len(text))] if whole else _windows(text):
        window = text[lo:hi]
        tokens = [t for t in _TOKEN.finditer(window)
                  if not _NOT_PAY.search(window[t.end():t.end() + 40])
                  and not (t.group("n") == "401" and not t.group("cur"))  # (a 401k)
                  and not _401K.search(window[max(0, t.start() - 8):t.start()])]
        i = 0
        while i < len(tokens):
            first = tokens[i]
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            second = nxt if nxt is not None and _SEP.match(window[first.end():nxt.start()]) else None
            span_end = (second or first).end()
            # a bare number ("5 years", "2 locations", "10,000 square feet") is never pay; a range's second figure
            # may be bare ("$220,320.00-311,040.00"), and a range may say its currency after ("70,000 - 90,000 USD")
            if not (first.group("cur") or first.group("k") or _CURRENCY_AFTER.match(window[span_end:])):
                i += 1
                continue
            a, b = _value(first), (_value(second) if second else None)
            period = _period(window, first.start(), span_end)
            if not period:  # none said: a top under 300 is an hourly rate, 10,000 or more a year's
                top = b if b is not None else a
                period = "hour" if top < 300 else "year" if top >= 10_000 or first.group("k") else ""
            if period and all(_sane(v, period) for v in (a, b) if v is not None):
                before = window[max(0, first.start() - 25):first.start()].lower()
                if second is None and re.search(r"\bup to\b|\bmaximum\b|\bmax\.?\s*$|not to exceed", before):
                    mn, mx = None, a
                elif second is None and re.search(r"\bstarting\b|\bstarts at\b|\bfrom\b|\bminimum\b|\bat least\b"
                                                  r"|\bmin\.?\s*$", before):
                    mn, mx = a, None
                elif second is None:
                    mn = mx = a
                else:
                    mn, mx = min(a, b or a), max(a, b or a)
                return Pay(mn, mx, period, source, _snippet(window, first.start(), span_end))
            i += 2 if second else 1
    m = _DOE.search(text)
    return Pay(None, None, "", source, " ".join(m.group(0).split())) if m else None


def _number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    try:
        n = float(str(v).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None
    return n if n > 0 else None


def pay_from_ld(salary: Any) -> Pay | None:
    """JSON-LD's baseSalary ({"currency": "USD", "value": {"minValue": 80000, "maxValue": 110000, "unitText": "YEAR"}}),
    as its figures: US dollars only, and only figures that are pay for their period (an hourly rate given as a year's
    isn't). None for anything else, for the posting's words to say."""
    if isinstance(salary, list):
        salary = salary[0] if salary else None
    if not isinstance(salary, dict):
        return None
    currency = str(salary.get("currency") or "USD").strip().upper()
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
        period = "hour" if top is not None and top < 300 else "year" if top is not None and top >= 10_000 else ""
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


def better(new: dict[str, Any], old: Any) -> bool:
    """Whether a posting's pay fields (`new`) should replace a job's (`old`, the tracker's row): when the job has none,
    or the new one's source ranks higher (structured > indeed > text). A pay with figures is never replaced by none."""
    if _number(new.get("pay_min")) is None and _number(new.get("pay_max")) is None:
        return False
    if _number(old["pay_min"]) is None and _number(old["pay_max"]) is None:
        return True
    return SOURCES.get(str(new.get("pay_source") or ""), 0) > SOURCES.get(str(old["pay_source"] or ""), 0)


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
    said = _span(pay.min, pay.max, lambda v: _money(v, pay.period)) + f"/{_SHORT[pay.period]}"
    if pay.period == "year":
        return said
    lo, hi = yearly(pay)
    return f"{said} (≈{_span(lo, hi, lambda v: _money(v, 'year'), words=False)}/yr)"


_SOURCE_WORDS = {"structured": "From the posting's own data",
                 "indeed": "From Indeed's salary field (it may be Indeed's estimate)",
                 "text": "From the posting's text"}


def view(fields_of: dict[str, Any], salary: str = "") -> dict[str, Any] | None:
    """What the desk's row says of a job's pay (a listing's fields, or the tracker's job's), or None when it isn't
    listed: the words, where they came from (with the salary field's own words, `salary`), and the yearly figures to
    sort and filter by: `top`, the most it pays a year (None for "from $X"), and `sort`, that or its least."""
    pay = from_fields(fields_of)
    if pay is None:
        return None
    lo, hi = yearly(pay)
    said = " ".join(str(salary or "").split()) if pay.source != "text" else ""
    return {"shown": shown(pay), "source": pay.source,
            "from": _SOURCE_WORDS.get(pay.source, pay.source) + (f": “{said}”" if said else ""),
            "yearly_min": lo, "top": hi, "sort": hi if hi is not None else lo}


def desired_yearly(value: Any) -> float | None:
    """preferences.desired_salary as a yearly figure: "$85,000", "85000", "85k", "$40/hr" (× 2,080)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        return n * 2080 if 0 < n < 300 else n if n > 0 else None
    said = str(value).strip()
    pay = parse_pay(said, whole=True)
    if pay is None or not pay.listed:
        pay = parse_pay(f"${said.lstrip('$')}", whole=True)  # (a bare "85000")
    if pay is None or not pay.listed:
        return None
    lo, hi = yearly(pay)
    return lo if lo is not None else hi
