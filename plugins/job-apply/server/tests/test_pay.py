"""A posting's pay read as figures (pay.py, the design of Oct 18, F1): structured first, then the posting's words."""

from pathlib import Path

import pytest

from job_apply import pay
from job_apply.pay import Pay, parse_pay, pay_from_ld, yearly
from job_apply.postings import MAX_DESCRIPTION, Posting, finalize, parse_html, posting_from_jsonld

FIXTURES = Path(__file__).parent / "fixtures"

# (words, read as a pay field and not running text, (min, max, period)): the design's strings and the review's
STRINGS = [
    ("Pay: $25-$32/hr depending on experience", False, (25, 32, "hour")),
    ("Salary: $65,000 - $85,000 a year", False, (65000, 85000, "year")),
    ("Compensation: USD 70000", False, (70000, 70000, "year")),
    ("Base Salary Range: $71,360.00 - $107,040.00, offered in good faith", False, (71360, 107040, "year")),
    ("Annual Salary Range for jobs which could be performed in the US: $220,320.00-311,040.00 USD", False,
     (220320, 311040, "year")),
    ("Salary Range $26.00 - $28.00 Hourly", False, (26, 28, "hour")),
    ("Pay rate: $30.00 per hour plus a $2,000 sign-on bonus", False, (30, 30, "hour")),
    ("Hourly pay starting at $22.50", False, (22.5, None, "hour")),
    ("Compensation up to $95K annually", False, (None, 95000, "year")),
    ("401(k) match up to 5% of base salary; salary $55k - $65k", False, (55000, 65000, "year")),
    ("Pay: $4,500 - $5,200 per month", False, (4500, 5200, "month")),
    ("Must be able to lift 50 lbs; pay is $18/hr", False, (18, 18, "hour")),
    ("The salary range for this role is $120,000 to $150,000 and a 10% target bonus", False, (120000, 150000, "year")),
    ("Rate: $45/hr on W2", False, (45, 45, "hour")),
    ("$17.50-$19.00 per hour", False, (17.5, 19, "hour")),
    ("Pay: 70,000 - 90,000 USD per year", False, (70000, 90000, "year")),
    ("New York pay range: $80,000 - $100,000; California pay range: $85,000 - $105,000", False,
     (80000, 100000, "year")),
    ("Department of Energy (DOE) site; pay $31.00 per hour", False, (31, 31, "hour")),
    # a pay field's own words (Indeed's, JSON-LD's printed)
    ("USD 80000-110000 per year", True, (80000, 110000, "year")),
    ("$25 an hour", True, (25, 25, "hour")),
    ("From $22 an hour", True, (22, None, "hour")),
    ("Up to $30 an hour", True, (None, 30, "hour")),
    ("Estimated $45.2K - $57.3K a year", True, (45200, 57300, "year")),
    ("$5,000 a month", True, (5000, 5000, "month")),
    ("$60K-$70K", True, (60000, 70000, "year")),
]


@pytest.mark.parametrize("words,whole,want", STRINGS)
def test_the_designs_strings_read_as_their_pay(words, whole, want):
    got = parse_pay(words, whole=whole)
    assert got is not None and (got.min, got.max, got.period) == want, (words, got)
    assert got.source == "text" and got.text


@pytest.mark.parametrize("words", [
    "We offer competitive pay and a $1,500 referral bonus",
    "Salary: DOE",
    "We offer a competitive salary",
])
def test_pay_said_without_figures_is_pay_not_listed(words):
    got = parse_pay(words)
    assert got is not None and not got.listed and (got.min, got.max, got.period) == (None, None, "")
    assert pay.shown(got) == "" and pay.meets(got, 50000) is None


@pytest.mark.parametrize("words", [
    "Save $500 on tuition; the 2026 fiscal year; 5 years of experience; lift 50 pounds",
    "Requires 3-5 years experience",
    "We pay a $1,500 referral bonus and a $2,000 sign-on bonus",
    "Our 401k plan matches up to 6% of pay",
    "A $15M fab expansion; competitive benefits",
    "Pay for 2 locations; 10,000 square feet",
    "We have 10,000 employees and pay well",
    "Apply structured troubleshooting, DOE methodologies, statistical analysis",
    "What is your desired compensation? $35,000-$50,000 $50,000-$75,000",  # (an application's question)
])
def test_figures_that_arent_pay_are_refused(words):
    got = parse_pay(words)
    assert got is None or not got.listed, (words, got)


def test_yearly_and_the_rows_words():
    hourly = Pay(26, 28, "hour", "text")
    assert yearly(hourly) == (54080, 58240) and pay.shown(hourly) == "$26–$28/hr (≈$54k–$58k/yr)"
    assert yearly(Pay(4500, 5200, "month")) == (54000, 62400) and yearly(Pay(1000, None, "week")) == (52000, None)
    assert pay.shown(Pay(71360, 107040, "year")) == "$71k–$107k/yr"
    assert pay.shown(Pay(22, None, "hour")) == "from $22/hr (≈$46k/yr)"
    assert pay.shown(Pay(None, 95000, "year")) == "up to $95k/yr"
    assert pay.shown(Pay(17.5, 19, "hour")) == "$17.50–$19/hr (≈$36k–$40k/yr)"
    assert yearly(None) == (None, None) and yearly(Pay(None, None, "")) == (None, None)


def test_meets_says_below_only_when_the_listed_pay_tops_out_under_the_floor():
    assert pay.meets(Pay(26, 28, "hour"), 58000) is True  # (its most: $58,240 a year)
    assert pay.meets(Pay(26, 28, "hour"), 60000) is False
    assert pay.meets(Pay(22, None, "hour"), 60000) is None  # ("from $22": its most isn't said)
    assert pay.meets(Pay(30, None, "hour"), 60000) is True
    assert pay.meets(None, 60000) is None and pay.meets(Pay(26, 28, "hour"), None) is None


def test_the_desired_salary_as_a_yearly_figure():
    for said, want in [("$85,000", 85000), ("85000", 85000), ("85k", 85000), (85000, 85000), ("$40/hr", 83200),
                       (40, 83200), ("", None), (None, None), ("negotiable", None), (True, None)]:
        assert pay.desired_yearly(said) == want, said


def test_json_ld_base_salary_is_kept_as_its_figures():
    assert (lambda p: (p.min, p.max, p.period, p.source))(pay_from_ld(
        {"currency": "USD", "value": {"minValue": 80000, "maxValue": 110000, "unitText": "YEAR"}})) == \
        (80000, 110000, "year", "structured")
    got = pay_from_ld({"currency": "USD", "value": {"value": "27.50", "unitText": "HOUR"}})
    assert (got.min, got.max, got.period) == (27.5, 27.5, "hour")
    got = pay_from_ld([{"value": 95000}])  # (no unit: 10,000 or more is a year's)
    assert (got.min, got.max, got.period) == (95000, 95000, "year")
    assert pay_from_ld({"currency": "EUR", "value": {"minValue": 50000, "maxValue": 60000, "unitText": "YEAR"}}) is None
    assert pay_from_ld({"currency": "USD", "value": {"value": 25, "unitText": "YEAR"}}) is None  # (not a year's pay)
    assert pay_from_ld(None) is None and pay_from_ld("") is None


def _read(name: str, url: str = "https://example.com/job/1") -> Posting:
    return finalize(parse_html((FIXTURES / name).read_text(encoding="utf-8"), url))


def _figures(p: Posting) -> tuple:
    return p.pay_min, p.pay_max, p.pay_period, p.pay_source


def test_the_fixtures_pay_ebara_onto_intel_and_the_json_ld_sample():
    """The design's acceptance: Paycom's posting page (Ebara), Workday's pages read in the browser (Onto, Intel: the
    description in the page's meta tags), and JSON-LD's baseSalary."""
    assert _figures(_read("live/live-ebara-technologies.html")) == (26, 28, "hour", "text")
    assert _figures(_read("live/live-onto-innovation.html")) == (71360, 107040, "year", "text")
    assert _figures(_read("live/live-intel.html")) == (220320, 311040, "year", "text")
    sample = _read("jsonld_posting.html")
    assert _figures(sample) == (80000, 110000, "year", "structured")
    assert sample.salary == "USD 80000-110000 per year"  # (the words, as before)
    assert "full_text" not in sample.to_dict() and sample.full_text == ""  # (read for the pay, then let go)


@pytest.mark.parametrize("name", ["live/live-lam-research.html", "live/live-asm-asm-america.html",
                                  "live/pipeline-onsemi.html", "live/pipeline-micron.html",
                                  "live/pipeline-infineon.html"])
def test_postings_that_list_no_pay_have_none(name):
    """Lam's "401k", ASM's "DOE methodologies": no false pay."""
    assert _figures(_read(name)) == (None, None, "", "")


def test_pay_at_the_end_of_a_long_posting_is_read_before_the_texts_cut():
    """html_to_text cuts a posting's text at MAX_DESCRIPTION characters, and pay is often the last thing a long one
    says: it's read from the whole text."""
    body = "<p>" + "Maintain and repair complex equipment in a cleanroom. " * 600 + "</p>"
    raw = (f"<html><head><title>Technician</title></head><body><main><h1>Technician</h1>{body}"
           "<p>Pay Range: $31.00 - $36.50 per hour</p></main></body></html>")
    p = _read_raw(raw)
    assert len(p.description) == MAX_DESCRIPTION and "Pay Range" not in p.description
    assert _figures(p) == (31, 36.5, "hour", "text")
    ld = posting_from_jsonld({"title": "Technician", "description": body + "<p>Salary: $60,000 - $70,000</p>"},
                             "https://example.com/job/2")
    assert _figures(finalize(ld)) == (60000, 70000, "year", "text")


def test_a_workday_postings_pay_is_read_from_its_api_text_whatever_its_length():
    """Workday gives pay only in the description's words (Onto's "Base Salary Range"), here at the end of a posting
    longer than the text kept."""
    import asyncio

    import httpx

    from job_apply.postings import fetch_posting

    url = "https://example-semi.wd1.myworkdayjobs.com/External/job/Phoenix-AZ/Process-Technician_R1001"
    body = "<p>" + "Run and sustain metrology tools across shifts. " * 500 + "</p>"
    description = body + ("<p>Compensation &amp; Growth • Base Salary Range: $71,360.00 - $107,040.00, offered in good "
                          "faith</p>")

    def answer(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/wday/cxs/example-semi/External/job/Phoenix-AZ/Process-Technician_R1001")
        return httpx.Response(200, json={"jobPostingInfo": {"title": "Process Technician", "location": "Phoenix, AZ",
                                                            "jobDescription": description}})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await fetch_posting(url, client=client)

    p = asyncio.run(go())
    assert p.parse_method == "workday-api" and len(p.description) == MAX_DESCRIPTION
    assert _figures(p) == (71360, 107040, "year", "text")


def _read_raw(raw: str) -> Posting:
    return finalize(parse_html(raw, "https://example.com/job/1"))


def test_a_salary_given_with_a_job_is_indeeds_and_ranks_above_the_words():
    """add_job's salary from the Indeed connector: Indeed's salary field, read before the posting's words."""
    p = finalize(Posting(url="https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7b", title="Technician",
                         description="Pay: $20 - $22 per hour. " + "Duties. " * 40, salary="$25 - $27 an hour",
                         source="indeed", parse_method="manual"))
    assert _figures(p) == (25, 27, "hour", "indeed")
    p = finalize(Posting(url="https://careers.example.com/job/3", title="Technician",
                         description="Pay: $20 - $22 per hour. " + "Duties. " * 40, parse_method="manual"))
    assert _figures(p) == (20, 22, "hour", "text")


def test_the_rows_view_says_where_the_pay_came_from():
    view = pay.view({"pay_min": 25, "pay_max": 27, "pay_period": "hour", "pay_source": "indeed"}, "$25 - $27 an hour")
    assert view is not None and view["shown"] == "$25–$27/hr (≈$52k–$56k/yr)"
    assert view["from"].startswith("From Indeed's salary field") and "“$25 - $27 an hour”" in view["from"]
    assert (view["top"], view["sort"]) == (56160, 56160)
    view = pay.view({"pay_min": 22, "pay_max": None, "pay_period": "hour", "pay_source": "text"})
    assert view is not None and (view["top"], view["sort"], view["from"]) == (None, 45760, "From the posting's text")
    assert pay.view({}) is None and pay.view({"pay_min": None, "pay_max": None, "pay_period": ""}) is None
