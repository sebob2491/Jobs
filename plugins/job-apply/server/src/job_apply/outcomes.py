"""What has worked for the person: their own applications' outcomes, by group.

The tracker keeps each application sent in and what came back (Tracker.outcomes). Here they're counted by the job
system it went through, the employer, where it was found, the kind of title (which of the person's target titles it
is), its pay and how far away it is: how many heard back (an assessment, an interview, an offer or a rejection), how
many went on to an interview or a test, and how long the first answer took. The desk's What's working card says it,
for groups with MIN_GROUP applications or more only.

A difference between groups is said (a takeaway), or leant on (recommend.worked_before places an opening a little
higher or lower in the list), only where it's unlikely to be luck: each group of a kind (a job system, say) is compared
with the rest of its kind by Fisher's exact test, every comparison made at once corrected by Holm's method, so on
numbers with no real difference nothing is said at least 19 times in 20. Employers aren't compared (too many, each too
small); the card's table still shows them.

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

MIN_GROUP = 5  # applications a group needs before the card shows it (and answers, before their median days)
SETTLED_DAYS = 21  # an application with no answer counts as unanswered once it's had this long; until then, it waits
TAKEAWAYS = 2  # the most takeaways said
TEST_MIN = 10  # applications a group, and the rest of its kind, each need before they're compared
TEST_HITS = 3  # and the better of the two, answers (or interviews and tests)
ALPHA = 0.05  # the chance, every comparison taken together, of saying a difference that's only luck

# the groups, in the card's order, each with its heading; and the kinds compared
DIMENSIONS = {"system": "By job system", "employer": "By employer", "source": "By where it was found",
              "title": "By job title", "pay": "By pay", "distance": "By distance"}
COMPARED = ("system", "source", "title", "pay", "distance")
# the measures, as a takeaway says them (what happened, never why)
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
    (`answered`), how many went on to an interview or a test (`further`), and the days each answer took (`days`: only
    where the day it was applied and the answer email's time are both known)."""
    label: str = ""  # as the card shows it ("Workday")
    phrase: str = ""  # as a takeaway says it ("through Workday")
    n: int = 0
    answered: int = 0
    further: int = 0
    days: list[float] = field(default_factory=list)
    key: str = ""

    def count(self, measure: str) -> int:
        return self.further if measure == "further" else self.answered

    def median_days(self) -> int | None:
        """The median days to a first answer, with MIN_GROUP answers or more to tell it from; else None."""
        return round(statistics.median(self.days)) if len(self.days) >= MIN_GROUP else None


@dataclass
class Finding:
    """A group whose `measure` differs from the rest of its kind's (`rest_n` applications, `rest_k` of them answered or
    further), by Fisher's exact test (`p`, before the correction); `versus`: the one other group, where the rest is
    one."""
    dimension: str
    measure: str
    group: Tally
    rest_n: int
    rest_k: int
    p: float
    versus: Tally | None = None

    @property
    def higher(self) -> bool:
        return self.group.count(self.measure) * self.rest_n > self.rest_k * self.group.n


@dataclass
class Learned:
    """Every application's tally (`overall`), each group's (by dimension and key), and how many sent in still wait
    (`waiting`: under SETTLED_DAYS, nothing heard)."""
    overall: Tally = field(default_factory=Tally)
    groups: dict[tuple[str, str], Tally] = field(default_factory=dict)
    waiting: int = 0
    _found: list[Finding] | None = field(default=None, repr=False)

    def tally(self, dimension: str, key: str | None) -> Tally | None:
        return self.groups.get((dimension, key)) if key else None

    def findings(self) -> list[Finding]:
        """The differences that hold up, surest first (worked out once)."""
        if self._found is None:
            self._found = _holm(_comparisons(self))
        return self._found

    def leaning(self) -> dict[tuple[str, str], bool]:
        """The groups whose interviews and tests surely differ from the rest of their kind's: by dimension and key,
        whether theirs are more (True) or fewer. Of a kind with two groups, both (one more, the other fewer)."""
        out: dict[tuple[str, str], bool] = {}
        for f in self.findings():
            if f.measure != "further":
                continue
            out.setdefault((f.dimension, f.group.key), f.higher)
            if f.versus is not None:
                out.setdefault((f.dimension, f.versus.key), not f.higher)
        return out


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
    An application withdrawn with nothing heard isn't counted (the person's choice, not an answer). The days to a first
    answer run from the day it was applied (`applied_at`) to the answer email's time (`first_answer`): one without
    either still counts, but not in the days."""
    now = now or datetime.now(timezone.utc)
    learned = Learned()
    for row in rows:
        if row.get("status") == "withdrawn" and not row.get("answered"):
            continue
        sent = _moment(row.get("sent_at"))
        if not row.get("answered") and (sent is None or (now - sent).days < SETTLED_DAYS):
            learned.waiting += 1
            continue
        first, applied = _moment(row.get("first_answer")), _moment(row.get("applied_at"))
        days = max(0.0, (first - applied).total_seconds() / 86400) if first and applied else None
        keys = keys_of(row, titles, miles_of(row) if miles_of else None)
        for tally in [learned.overall, *(learned.groups.setdefault((dim, key), Tally(label, phrase, key=key))
                                         for dim, (key, label, phrase) in keys.items())]:
            tally.n += 1
            tally.answered += bool(row.get("answered"))
            tally.further += bool(row.get("further"))
            if row.get("answered") and days is not None:
                tally.days.append(days)
    return learned


def _log_choose(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def fisher(k: int, n: int, rest_k: int, rest_n: int) -> float:
    """Fisher's exact test, two-sided: the chance of counts at least as far apart as `k` in `n` against `rest_k` in
    `rest_n` by luck alone, with every application as likely as any other to be among the hits (the hypergeometric
    chances of every table as likely as this one, or less)."""
    hits, total = k + rest_k, n + rest_n
    lo, hi = max(0, hits - rest_n), min(hits, n)

    def chance(x: int) -> float:
        return _log_choose(n, x) + _log_choose(rest_n, hits - x) - _log_choose(total, hits)

    seen = chance(k)
    return min(1.0, sum(math.exp(c) for c in map(chance, range(lo, hi + 1)) if c <= seen + 1e-7))


def _comparisons(learned: Learned) -> list[Finding]:
    """Every comparison made: in each kind compared (COMPARED) with two groups or more, each group against the rest of
    its kind (for two, one of them: the other is its rest), for each measure, where both have TEST_MIN applications
    or more. One whose better side has fewer than TEST_HITS, or that doesn't differ at all, is made but says nothing
    (its p is 1)."""
    out = []
    for dimension in COMPARED:
        mine = sorted((t for (dim, _), t in learned.groups.items() if dim == dimension), key=lambda t: (-t.n, t.key))
        if len(mine) < 2:
            continue
        total_n = sum(t.n for t in mine)
        for measure in MEASURES:
            total_k = sum(t.count(measure) for t in mine)
            for g in mine[:1] if len(mine) == 2 else mine:
                k, rest_n = g.count(measure), total_n - g.n
                rest_k = total_k - k
                if g.n < TEST_MIN or rest_n < TEST_MIN:
                    continue
                # (one too few to say anything still counts among the comparisons made: which ones are made is
                # decided by their sizes alone, never by how they came out, or the correction would be too small)
                said = k * rest_n != rest_k * g.n and (k if k * rest_n > rest_k * g.n else rest_k) >= TEST_HITS
                p = fisher(k, g.n, rest_k, rest_n) if said else 1.0
                out.append(Finding(dimension, measure, g, rest_n, rest_k, p, mine[1] if len(mine) == 2 else None))
    return out


def _holm(tests: list[Finding]) -> list[Finding]:
    """The comparisons that hold up after Holm's correction for making them all at once (ALPHA in all), surest
    first."""
    held = []
    for i, f in enumerate(sorted(tests, key=lambda f: f.p)):
        if f.p >= 1 or f.p > ALPHA / (len(tests) - i):
            break
        held.append(f)
    return held


def _rate(k: int, n: int) -> str:
    return f"{round(100 * k / n)}%" if n else ""


def _said(f: Finding) -> str:
    """A finding in words: what happened, never why."""
    verb, g, m = MEASURES[f.measure], f.group, f.measure
    if f.versus is not None:
        hi, lo = (g, f.versus) if f.higher else (f.versus, g)
        return (f"Applications {hi.phrase} {verb} more often than those {lo.phrase}: {hi.count(m)} of {hi.n} "
                f"({_rate(hi.count(m), hi.n)}), against {lo.count(m)} of {lo.n} ({_rate(lo.count(m), lo.n)}).")
    return (f"Applications {g.phrase} {verb} {'more' if f.higher else 'less'} often than your others: "
            f"{g.count(m)} of {g.n} ({_rate(g.count(m), g.n)}), against {f.rest_k} of {f.rest_n} "
            f"({_rate(f.rest_k, f.rest_n)}).")


def takeaways(learned: Learned) -> list[str]:
    """At most TAKEAWAYS lines, the surest first, each a difference that holds up (Learned.findings), one for a group
    (its surer measure). None when nothing does: small numbers, or no real difference, say nothing."""
    said: dict[tuple[str, str], str] = {}
    for f in learned.findings():
        said.setdefault((f.dimension, f.group.key), _said(f))
    return list(said.values())[:TAKEAWAYS]


def summary(learned: Learned, nudging: bool, cap: int) -> dict[str, Any] | None:
    """What the desk's What's working card shows (None, and the card hidden, before any application is sent in): how
    many applications have an outcome and how many still wait; how many heard back and went further, with their rates;
    the median days to a first answer; each dimension's groups with MIN_GROUP applications or more (a dimension only
    where the applications are in more than one group); the takeaways; and whether the list's order leans on it
    (`nudging`: settings.learn_from_responses), by at most `cap` points' worth."""
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
