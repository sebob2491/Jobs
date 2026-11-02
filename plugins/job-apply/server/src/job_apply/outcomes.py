"""What has worked for the person: their own applications' outcomes, by group.

The tracker keeps each application sent in and what came back (Tracker.outcomes). Here they're counted by the job
system it went through, the employer, where it was found, the kind of title (which of the person's target titles it
is), its pay and how far away it is: how many heard back (an assessment, an interview, an offer or a rejection), how
many went on to an interview or a test, and how long the first answer took. The desk's What's working card says it,
for groups with MIN_GROUP applications or more only, with a takeaway or two where the numbers hold one up; and the
ranking's "worked before" part (recommend.worked_before) leans on it a little.

Nothing here leaves the person's computer: it's worked out from the tracker each time the desk asks, never written down,
logged or put in a problem report.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import pay
from .ats import ATS_NAMES, JOB_BOARDS
from .search import title_matches
from .tracker import company_key

MIN_GROUP = 5  # applications a group needs before it's shown, compared or leant on
SETTLED_DAYS = 21  # an application with no answer counts as unanswered once it's had this long; until then, it waits
TAKEAWAYS = 2  # the most takeaways said
_SURE = 2.0  # how far apart (in standard errors) two groups' rates are before a takeaway says so
_TAKEAWAY_HITS = 3  # and the better group's own count of them, at least

# the groups, in the card's order, each with its heading
DIMENSIONS = {"system": "By job system", "employer": "By employer", "source": "By where it was found",
              "title": "By job title", "pay": "By pay", "distance": "By distance"}
# the measures, as a takeaway says them
MEASURES = {"further": "led to an interview or a test", "answered": "got an answer"}

_NOT_A_SYSTEM = {"", "unknown"}
_PAY_BANDS = ((40_000, "under $40k", "paying under $40k a year"),
              (60_000, "$40k to $60k", "paying $40k to $60k a year"),
              (80_000, "$60k to $80k", "paying $60k to $80k a year"),
              (100_000, "$80k to $100k", "paying $80k to $100k a year"),
              (math.inf, "$100k and up", "paying $100k a year or more"))
_MILES = ((10, "within 10 miles", "within 10 miles of home"),
          (25, "10 to 25 miles", "10 to 25 miles from home"),
          (50, "25 to 50 miles", "25 to 50 miles from home"),
          (math.inf, "over 50 miles", "over 50 miles from home"))


@dataclass
class Tally:
    """A group's applications with an outcome (`n`: answered, or sent SETTLED_DAYS ago or more), how many heard back
    (`answered`), how many went on to an interview or a test (`further`), and the days each answer took (`days`)."""
    label: str = ""  # as the card shows it ("Workday")
    phrase: str = ""  # as a takeaway says it ("through Workday")
    n: int = 0
    answered: int = 0
    further: int = 0
    days: list[float] = field(default_factory=list)

    def count(self, measure: str) -> int:
        return self.further if measure == "further" else self.answered

    def median_days(self) -> int | None:
        """The median days to a first answer, with MIN_GROUP answers or more to tell it from; else None."""
        return round(statistics.median(self.days)) if len(self.days) >= MIN_GROUP else None


@dataclass
class Learned:
    """Every application's tally (`overall`), each group's (by dimension and key), and how many sent in still wait
    (`waiting`: under SETTLED_DAYS, nothing heard)."""
    overall: Tally = field(default_factory=Tally)
    groups: dict[tuple[str, str], Tally] = field(default_factory=dict)
    waiting: int = 0

    def tally(self, dimension: str, key: str | None) -> Tally | None:
        return self.groups.get((dimension, key)) if key else None


def _moment(said: Any) -> datetime | None:
    if not said:
        return None
    try:
        t = datetime.fromisoformat(str(said).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _band(value: float, bands: tuple[tuple[float, str, str], ...]) -> tuple[str, str, str]:
    _, label, phrase = next(b for b in bands if value < b[0])
    return label.replace(" ", "_"), label[:1].upper() + label[1:], phrase


def keys_of(job: dict[str, Any], titles: list[str], miles: float | None = None) -> dict[str, tuple[str, str, str]]:
    """The groups a job (an application, or an opening on the desk) is in: by dimension, its key, its label and its
    phrase. `miles`: how far it is from home (places.away's), None where that isn't known. A dimension the job doesn't
    say (no job system, no source, a place not known) is left out."""
    out: dict[str, tuple[str, str, str]] = {}
    ats = str(job.get("ats") or "").strip().lower()
    if ats not in _NOT_A_SYSTEM:
        name = ATS_NAMES.get(ats, ats)
        out["system"] = (ats, name, "through an employer's own careers site" if ats == "company_site"
                         else f"through {name}")
    company = str(job.get("company") or "").strip()
    if company_key(company):
        out["employer"] = (company_key(company), company, f"to {company}")
    source = str(job.get("source") or "").strip().lower()
    if source:
        name = "The desk's search" if source == "job desk" else JOB_BOARDS.get(source, source.title())
        out["source"] = (source, name, "found by the desk's search" if source == "job desk" else f"found on {name}")
    title = str(job.get("title") or "")
    hit = next((t for t in titles if title and title_matches(title, t)), None)
    out["title"] = ((hit.casefold(), hit, f"for {hit} jobs") if hit
                    else ("other", "Other titles", "for other titles"))
    lo, hi = pay.yearly(pay.from_fields(job))
    top = hi if hi is not None else lo
    out["pay"] = _band(top, _PAY_BANDS) if top is not None else ("none", "Pay not listed", "with pay not listed")
    if str(job.get("workplace") or "") == "remote":
        out["distance"] = ("remote", "Remote", "for remote jobs")
    elif miles is not None:
        out["distance"] = _band(miles, _MILES)
    return out


def learn(rows: list[dict[str, Any]], titles: list[str], miles_of: Any = None,
          now: datetime | None = None) -> Learned:
    """The tallies from the tracker's applications (Tracker.outcomes). `titles`: the person's target titles;
    `miles_of`: a job's distance from home, or None (places.away's miles), where the person's home town is known.
    An application withdrawn with nothing heard isn't counted (the person's choice, not an answer)."""
    now = now or datetime.now(timezone.utc)
    learned = Learned()
    for row in rows:
        if row.get("status") == "withdrawn" and not row.get("answered"):
            continue
        sent = _moment(row.get("sent_at"))
        if not row.get("answered") and (sent is None or (now - sent).days < SETTLED_DAYS):
            learned.waiting += 1
            continue
        first = _moment(row.get("first_answer"))
        days = max(0.0, (first - sent).total_seconds() / 86400) if first is not None and sent is not None else None
        keys = keys_of(row, titles, miles_of(row) if miles_of else None)
        for tally in [learned.overall, *(learned.groups.setdefault((dim, key), Tally(label, phrase))
                                         for dim, (key, label, phrase) in keys.items())]:
            tally.n += 1
            tally.answered += bool(row.get("answered"))
            tally.further += bool(row.get("further"))
            if row.get("answered") and days is not None:
                tally.days.append(days)
    return learned


def _rate(k: int, n: int) -> str:
    return f"{round(100 * k / n)}%" if n else ""


def _times(ratio: float) -> str:
    halves = round(ratio * 2) / 2
    return "twice" if halves == 2 else f"{halves:g} times"


def takeaways(learned: Learned) -> list[str]:
    """At most TAKEAWAYS lines the numbers hold up: two groups of one kind (two job systems, say), each with MIN_GROUP
    applications or more, whose rates differ by _SURE standard errors or more, the better one with _TAKEAWAY_HITS
    or more; one line for a pair of groups (its stronger measure). None when nothing does: small numbers say
    nothing."""
    found: list[tuple[float, tuple[str, str, str], str]] = []
    for dimension in DIMENSIONS:
        big = [t for (dim, _), t in learned.groups.items() if dim == dimension and t.n >= MIN_GROUP]
        for measure, verb in MEASURES.items():
            for i, a in enumerate(big):
                for b in big[i + 1:]:
                    hi, lo = (a, b) if a.count(measure) / a.n >= b.count(measure) / b.n else (b, a)
                    kh, kl = hi.count(measure), lo.count(measure)
                    if kh < _TAKEAWAY_HITS:
                        continue
                    p = (kh + kl) / (hi.n + lo.n)
                    spread = math.sqrt(p * (1 - p) * (1 / hi.n + 1 / lo.n))
                    z = (kh / hi.n - kl / lo.n) / spread if spread else 0.0
                    if z < _SURE:
                        continue
                    ratio = (kh / hi.n) / (kl / lo.n) if kl else math.inf
                    if not kl:
                        said = (f"Applications {hi.phrase} {verb} {kh} times in {hi.n}; those {lo.phrase}, "
                                f"not once in {lo.n}.")
                    elif ratio >= 1.5:
                        said = (f"Applications {hi.phrase} {verb} {_times(ratio)} as often as those {lo.phrase} "
                                f"({hi.n} vs {lo.n} applications).")
                    else:  # (rates both high: "1 times as often" says nothing)
                        said = (f"Applications {hi.phrase} {verb} {_rate(kh, hi.n)} of the time ({kh} in {hi.n}); "
                                f"those {lo.phrase}, {_rate(kl, lo.n)} ({kl} in {lo.n}).")
                    first, second = sorted((hi.label, lo.label))
                    found.append((z, (dimension, first, second), said))
    found.sort(key=lambda x: -x[0])
    said_of: dict[tuple[str, str, str], str] = {}
    for _, pair, said in found:
        said_of.setdefault(pair, said)
    return list(said_of.values())[:TAKEAWAYS]


def summary(learned: Learned, nudging: bool, cap: int) -> dict[str, Any] | None:
    """What the desk's What's working card shows (None, and the card hidden, before any application is sent in): how
    many applications have an outcome and how many still wait; how many heard back and went further, with their rates;
    the median days to a first answer; each dimension's groups with MIN_GROUP applications or more (a dimension only
    where the applications are in more than one group); the takeaways; and whether the ranking leans on it
    (`nudging`: settings.learn_from_responses), by at most `cap` points."""
    o = learned.overall
    if not o.n and not learned.waiting:
        return None
    groups = []
    for dimension, heading in DIMENSIONS.items():
        mine = [t for (dim, _), t in learned.groups.items() if dim == dimension]
        shown = sorted((t for t in mine if t.n >= MIN_GROUP), key=lambda t: (-t.n, t.label.casefold()))
        if shown and len(mine) > 1:
            groups.append({"dimension": dimension, "heading": heading,
                           "rows": [{"label": t.label, "n": t.n, "answered": t.answered, "further": t.further,
                                     "answered_rate": _rate(t.answered, t.n), "further_rate": _rate(t.further, t.n),
                                     "median_days": t.median_days()} for t in shown]})
    return {"n": o.n, "waiting": learned.waiting, "answered": o.answered, "further": o.further,
            "answered_rate": _rate(o.answered, o.n), "further_rate": _rate(o.further, o.n),
            "median_days": o.median_days(), "answers_timed": len(o.days),
            "min_group": MIN_GROUP, "settled_days": SETTLED_DAYS, "enough": o.n >= MIN_GROUP,
            "groups": groups if o.n >= MIN_GROUP else [], "takeaways": takeaways(learned) if o.n >= MIN_GROUP else [],
            "nudging": nudging, "cap": cap}
