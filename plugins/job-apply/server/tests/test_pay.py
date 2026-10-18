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
    # (two states' ranges: the widest, so neither hides the job where it meets the floor)
    ("New York pay range: $80,000 - $100,000; California pay range: $85,000 - $105,000", False,
     (80000, 105000, "year")),
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
    # (no currency: dollars only for a posting in the US; no unit: 15,000 or more is a year's)
    got = pay_from_ld([{"value": 95000}], in_us=True)
    assert (got.min, got.max, got.period) == (95000, 95000, "year")
    assert pay_from_ld([{"value": 95000}]) is None
    assert pay_from_ld({"currency": "EUR", "value": {"minValue": 50000, "maxValue": 60000, "unitText": "YEAR"}}) is None
    assert pay_from_ld({"currency": "USD", "value": {"value": 25, "unitText": "YEAR"}}) is None  # (not a year's pay)
    assert pay_from_ld(None) is None and pay_from_ld("") is None


@pytest.mark.parametrize("country,region,figures", [
    ("US", "AZ", (60000, 70000, "year", "structured")), ("", "Arizona", (60000, 70000, "year", "structured")),
    ("CA", "ON", (None, None, "", "")), ("", "", (None, None, "", "")),
])
def test_a_base_salary_naming_no_currency_is_dollars_only_in_the_us(country, region, figures):
    """The review of pay-listing: a baseSalary with no currency was taken for dollars wherever the posting was."""
    ld = {"title": "Technician", "baseSalary": {"value": {"minValue": 60000, "maxValue": 70000, "unitText": "YEAR"}},
          "jobLocation": {"address": {"addressLocality": "Somewhere", "addressRegion": region,
                                      "addressCountry": country}}}
    assert _figures(finalize(posting_from_jsonld(ld, "https://example.com/job/4"))) == figures


# The review of pay-listing's probes (scratchpad review-pay/notes/probe*.py), each with what it should read as: a
# figure that isn't the job's pay (a bonus, an allowance, a stipend, relocation, equity, a match, a count, another
# figure's period, the applicant's own salary) is never its pay, a range said with its period or currency on each
# figure is read whole, several ranges are read from the least to the most, and when unsure it's "not listed"
REVIEWED = [
    # probe.py
    ('Sign-on bonus: $5,000 for this position. Pay is competitive.', None),
    ('We offer a $5,000 sign on bonus and great pay.', None),
    ('Pay: $5,000 bonus after 90 days', None),
    ('401(k) with 6% match. Salary: competitive.', None),
    ('Employees earn up to $3,000 in referral bonuses.', None),
    ('Relocation assistance up to $10,000 is available. Compensation: DOE', None),
    ('Relocation package of $15,000 available for this salaried role.', None),
    ('Tuition reimbursement up to $5,250 per year', None),
    ('What is your desired salary? $____', None),
    ('Desired salary: $80,000', None),
    ('Please state your salary expectations', None),
    ('Requires 5+ years of experience. Pay: competitive', None),
    ('Call (480) 555-1212 about pay.', None),
    ('Phoenix, AZ 85034. Pay rate depends on experience.', None),
    ('Req 123456 Pay grade 12', None),
    ('We are a $15M company with great pay', None),
    ('a $1.5B semiconductor leader offering competitive pay', None),
    ('The company has $500 million in annual revenue and offers great pay', None),
    ('Pay: $20/hr. Shift differential: $2/hr', (20, 20, 'hour')),
    ('Shift differential of $3.00 per hour on nights; base pay range not listed', None),
    ('Shift differential $1.50/hr; Pay rate $22.00 - $26.00/hr', (22, 26, 'hour')),
    ('Technician I: $20-$24/hr. Technician II: $26-$30/hr.', (20, 30, 'hour')),
    ('Compensation: $4,500 - $5,500 per month', (4500, 5500, 'month')),
    ('Monthly salary range $4,500 - $5,500', (4500, 5500, 'month')),
    ('Salary: $55k-$65k', (55000, 65000, 'year')),
    ('Pay range: $55K - $65K annually', (55000, 65000, 'year')),
    ('$65,000 - $85,000 a year', (65000, 85000, 'year')),
    ('Pay: $28.50 an hour', (28.5, 28.5, 'hour')),
    ('Hourly rate: 22.00 - 26.00', (22, 26, 'hour')),
    ('Pay Range: 22.00 - 26.00 USD hourly', (22, 26, 'hour')),
    ('Salary range: 80,000 - 100,000', (80000, 100000, 'year')),
    ('Salary: $80,000.00 - $100,000.00 per annum', (80000, 100000, 'year')),
    ('The pay range for this role is $71,360.00 - $107,040.00. Bonus: up to 10%.', (71360, 107040, 'year')),
    ('Base pay range $120,000 to $150,000 per year plus $10,000 sign-on bonus', (120000, 150000, 'year')),
    ('Starting pay $18/hr', (18, None, 'hour')),
    ('Pay starts at $18 per hour', (18, None, 'hour')),
    ('Up to $32/hr DOE', (None, 32, 'hour')),
    ('Earn up to $1,200 per week', (None, 1200, 'week')),
    ('Weekly pay: $1,200', (1200, 1200, 'week')),
    ('Per diem: $75/day', None),
    ('Daily rate $300', (300, 300, 'day')),
    ('Annual Salary Range (Arizona): $220,320.00-311,040.00 USD', (220320, 311040, 'year')),
    ('Salary: $50,000 + $5,000 bonus', (50000, 50000, 'year')),
    ('Salary: $50k-$60k; 401k match up to $6,000', (50000, 60000, 'year')),
    ('We pay $500 for referrals', None),
    ('Pay: $1,000 retention bonus paid after 6 months, and $19.50/hr', (19.5, 19.5, 'hour')),
    ('Compensation: $19 - 23 per hour', (19, 23, 'hour')),
    ('Pay rate: 19 - 23 $/hr', (19, 23, 'hour')),
    ('$19 to $23 hourly', (19, 23, 'hour')),
    ('This role pays between $70,000 and $90,000 annually', (70000, 90000, 'year')),
    ('Salary: USD 70000', (70000, 70000, 'year')),
    ('Estimated $45.2K - $57.3K a year', (45200, 57300, 'year')),
    ('From $22 an hour', (22, None, 'hour')),
    ('Salary: DOE', None),
    ('Compensation: commensurate with experience', None),
    ('Competitive salary and benefits', None),
    ('Pay: $15 per hour (training rate), then $20 per hour', (15, 20, 'hour')),
    ('Wage: $7.25', (7.25, 7.25, 'hour')),
    ('Salary $2,000,001', None),
    ('rate: $250 per month for phone stipend', None),
    ('Phone allowance $50/month; pay $25/hr', (25, 25, 'hour')),
    ('Health insurance premium $100 per month; pay competitive', None),
    ('Hourly employees receive a $250 annual bonus', None),
    ('The annual base salary range for this position in California is $150,000-$200,000 and in Arizona $120,000-$160,000',
     (120000, 200000, 'year')),
    ('Pay: $30/hr for 40 hours per week', (30, 30, 'hour')),
    ('Rate: 40 hours per week, $25', None),
    ('Salary Range $26.00 - $28.00 Hourly', (26, 28, 'hour')),
    ('Salary grade: 7 ($40,000 - $50,000)', (40000, 50000, 'year')),
    ('Annual pay: $35', None),
    ('Pay: $35/year', None),
    ('Pay: $120,000 hourly', None),
    ('Pay $25 hr', (25, 25, 'hour')),
    ('$25.00/hour', (25, 25, 'hour')),
    ('Pay: $25 - $30 / hour', (25, 30, 'hour')),
    ('Pay: 25-30 dollars per hour', (25, 30, 'hour')),
    ('Compensation $60K/yr', (60000, 60000, 'year')),
    ('Compensation: $100 per hour of on-call', None),
    ('Pay ranges from $50,000 (entry) to $90,000 (senior)', (50000, 90000, 'year')),
    ('We offer annual raises and $2,000 annual education budget', None),
    ('Earn $2,000 sign on', None),
    ('salary: $70,000 - $80,000 (plus 10% annual bonus)', (70000, 80000, 'year')),
    ('Pay transparency: $18.00 - $22.00 per hour. This range is for Arizona.', (18, 22, 'hour')),
    ('The hiring range is $55,000-$75,000; the full salary range is $50,000 - $90,000.', (50000, 90000, 'year')),
    ('Hourly pay: $21.50 Overtime: $32.25', (21.5, 21.5, 'hour')),
    ('Pay: $5K per month', (5000, 5000, 'month')),
    ('Annual salary: 85000', (85000, 85000, 'year')),
    ('salary 85k', (85000, 85000, 'year')),
    ('Pay: $1.2M', None),
    ('Pay range $15M company', None),
    ('Pay: $15,000 - $20,000 per month', (15000, 20000, 'month')),
    ('Pay up to $25,000 annually for travel', None),
    ('Benefits valued at $12,000 per year in addition to pay', None),
    ('Compensation: $18-$22/hour, plus $1.50/hour shift premium', (18, 22, 'hour')),
    ('Salary range: $90,000 – $110,000. Signing bonus $5,000.', (90000, 110000, 'year')),
    ('Pay range: $90–110k', (90000, 110000, 'year')),
    ('Pay range: $90-110K/yr', (90000, 110000, 'year')),
    ('Pay: $40,000-$50,000/year (part-time, prorated)', (40000, 50000, 'year')),
    ('We pay employees $1000 for each referral hired', None),
    ('Annual leave of 20 days. Pay: competitive', None),
    ('Hourly: 3 shifts available, 12 hours each', None),
    ('annual revenue of $300 million; the salary is $90,000', (90000, 90000, 'year')),
    # probe2.py
    ('Phone stipend: $50 per month. Pay: $25/hr', (25, 25, 'hour')),
    ('Uniform allowance of $200 per year and pay of $20/hr', (20, 20, 'hour')),
    ('Stipend: $500 per month; hourly pay listed upon offer', None),
    ('Annual bonus: $20,000. Salary: competitive', None),
    ('Sign-on bonus of $15,000; competitive pay', None),
    ('Sign-On Bonus: $15,000! Competitive pay and benefits.', None),
    ('Relocation: up to $20,000. Competitive salary.', None),
    ('Equity: $50,000 in RSUs; base salary $120,000', (120000, 120000, 'year')),
    ('Base salary $120,000; RSU grant of $50,000', (120000, 120000, 'year')),
    ('What are your salary requirements? Under $30,000 / $30,000-$40,000 / $40,000+', None),
    ('What is your expected salary? e.g. $50,000', None),
    ('Expected pay: $45,000', None),
    ('Your salary history: $40,000', None),
    ('Pay: $90K-110K', (90000, 110000, 'year')),
    ('Pay: $25-30/hr', (25, 30, 'hour')),
    ('Pay: $25 - 30 an hour', (25, 30, 'hour')),
    ('Salary: $100-120k', (100000, 120000, 'year')),
    ('$100k-$120k base salary', (100000, 120000, 'year')),
    ('Salary range $85,000-$105,000/year + bonus', (85000, 105000, 'year')),
    ('Hourly Range: $22.00 to $28.00', (22, 28, 'hour')),
    ('Pay Rate: $22.00 USD per Hour', (22, 22, 'hour')),
    ('Pay: USD $25.00/Hr.', (25, 25, 'hour')),
    ('Salary: 75,000 USD - 95,000 USD', (75000, 95000, 'year')),
    ('Pay: $1,200/week', (1200, 1200, 'week')),
    ('Pay: $1,200 bi-weekly', (1200, 1200, 'biweekly')),
    ('Pay: $2,500 biweekly', (2500, 2500, 'biweekly')),
    ('Pay: $2,500 semi-monthly', (2500, 2500, 'semimonthly')),
    ('Salary: $3,500/mo', (3500, 3500, 'month')),
    ('Compensation: $250/day', (250, 250, 'day')),
    ('Pay Range: $0.00 - $0.00', None),
    ('Pay: $15.50 - $17.50 per hour. Benefits include $5,000 tuition assistance per year.', (15.5, 17.5, 'hour')),
    ('We offer up to $5,250 in tuition reimbursement annually, competitive wages', None),
    ('Earn an extra $3/hr shift differential on top of base pay', None),
    ('Night shift premium $4.00 per hour. Base pay $24/hr', (24, 24, 'hour')),
    ('Base pay: $24/hr + $4/hr night premium', (24, 24, 'hour')),
    ('Pay: $100,000 per year (2 openings)', (100000, 100000, 'year')),
    ('Pay: $22/hr (equivalent to $45,760 annually)', (22, 22, 'hour')),
    ('Pay: $45,760 annually ($22/hr)', (45760, 45760, 'year')),
    ('Lead technician pay is $30/hr and technician pay is $24/hr', (24, 30, 'hour')),
    ('The salary for this role in Seattle is $150,000 - $180,000', (150000, 180000, 'year')),
    ('$25 hourly rate', (25, 25, 'hour')),
    ('Salary: $15.00/hr - $18.00/hr', (15, 18, 'hour')),
    ('Pay: $17 - $20 per hour, plus $500 sign-on bonus after 90 days', (17, 20, 'hour')),
    ('Salary: $55,000-$65,000 DOE', (55000, 65000, 'year')),
    ('Salary: $55,000-$65,000\nBenefits: 401(k) with 4% match up to $10,000', (55000, 65000, 'year')),
    ('Pay: up to $25/hr depending on experience', (None, 25, 'hour')),
    ('Pay: Starting at $20/hr', (20, None, 'hour')),
    ('Starting at $20 - $25/hr', (20, 25, 'hour')),
    ('Min: $20/hr, Max: $25/hr', (20, 25, 'hour')),
    ('Minimum $20/hr Maximum $25/hr', (20, 25, 'hour')),
    ('Salary Min: 60,000.00 Salary Max: 80,000.00', (60000, 80000, 'year')),
    ('Salary range: $60k to $80k per year', (60000, 80000, 'year')),
    ('Hourly rate: $20.00 (non-negotiable)', (20, 20, 'hour')),
    ('Pay: $20/hr; we have 401k with 6% match', (20, 20, 'hour')),
    ('Pay: $401k', None),
    ('Pay: $20/hr. Signing Bonus $401', (20, 20, 'hour')),
    # probe3.py
    ('We offer annual tuition reimbursement up to $20,000.', None),
    ('Annual bonus up to $25,000. Competitive salary and benefits.', None),
    ('Relocation assistance of up to $25,000 is available.', None),
    ('Eligible for a sign-on bonus of up to $20,000 and competitive pay.', None),
    ('New hire equity grant: $40,000. Base salary is competitive.', None),
    ('Referral Bonus: $2,500 | Pay Rate: $24.00/hr', (24, 24, 'hour')),
    ('Overtime pay: time and a half. Shift differential $2/hr. Pay rate $25.00 - $30.00 per hour', (25, 30, 'hour')),
    ('Benefits: 401(k) with company match; annual merit increase; $15,000 relocation', None),
    ('Pay: $22/hr. Cell phone stipend $50/month.', (22, 22, 'hour')),
    ('Monthly car allowance: $500. Salary: $95,000 - $110,000', (95000, 110000, 'year')),
    ('Car allowance $500/month; salary $95,000 - $110,000', (95000, 110000, 'year')),
    ('Salary: $95,000 - $110,000; car allowance $500/month', (95000, 110000, 'year')),
    ('Hiring bonus: $3,000 (paid hourly employees)', None),
    ('Pay range: $24.00/hr to $30.00/hr', (24, 30, 'hour')),
    ('$24.00 per hour - $30.00 per hour', (24, 30, 'hour')),
    ('Hourly Rate: $24.00 Hourly - $30.00 Hourly', (24, 30, 'hour')),
    ('Salary: $24.00 - $30.00 hr', (24, 30, 'hour')),
    ('Pay: $24.00 – $30.00 Per Hour', (24, 30, 'hour')),
    ('Compensation: $85,000.00/yr - $105,000.00/yr', (85000, 105000, 'year')),
    ('Salary: $85,000 / year - $105,000 / year', (85000, 105000, 'year')),
    ('The expected base pay range for this position is $85,000 - $105,000 USD annually.', (85000, 105000, 'year')),
    ('Pay Range: 85,000.00 - 105,000.00 USD Annual', (85000, 105000, 'year')),
    ('Pay Range\n$24.00 - $30.00', (24, 30, 'hour')),
    ('Pay Range:\n\n$24.00\n-\n$30.00', (24, 30, 'hour')),
    ('Compensation\nMinimum: $24.00\nMaximum: $30.00', (24, 30, 'hour')),
    ('Pay: $24.00 – 30.00/hr', (24, 30, 'hour')),
]


@pytest.mark.parametrize("words,want", REVIEWED)
def test_the_reviews_strings_read_as_intended(words, want):
    got = parse_pay(words)
    assert ((got.min, got.max, got.period) if got is not None and got.listed else None) == want, (words, got)


def test_another_figures_period_or_a_non_pay_figure_never_hides_a_job():
    """The review's HIGH findings, by the floor: none of these hides a job paying $60,000 a year or more."""
    for words in ("Car allowance $500/month; salary $95,000 - $110,000", "Annual bonus up to $25,000. Competitive "
                  "salary and benefits.", "Sign-On Bonus: $15,000!", "New hire equity grant: $40,000.",
                  "annual tuition reimbursement up to $20,000", "Earn up to $20,000 in bonuses annually",
                  "$20,000 retention payments per year", "$24.00/hr to $30.00/hr", "$24.00 per hour - $30.00 per hour",
                  "Hourly Rate: $24.00 Hourly - $30.00 Hourly", "Min: $20/hr, Max: $35/hr",
                  "$85,000.00/yr - $105,000.00/yr", "Salary: 75,000 USD - 95,000 USD"):
        assert pay.meets(parse_pay(words), 60000) is not False, words


def test_two_weeks_and_twice_a_month_are_their_own_periods():
    biweekly, semimonthly = parse_pay("Pay: $2,500 biweekly"), parse_pay("Pay: $2,500 semi-monthly")
    assert (biweekly.period, yearly(biweekly)) == ("biweekly", (65000, 65000))
    assert (semimonthly.period, yearly(semimonthly)) == ("semimonthly", (60000, 60000))
    assert pay.shown(biweekly) == "$2,500 every 2 weeks (≈$65k/yr)"
    assert pay.shown(parse_pay("Pay: $1,200 bi-weekly")) == "$1,200 every 2 weeks (≈$31k/yr)"


def test_a_very_long_text_is_read_at_its_start_and_end_only():
    """The review: parse_pay's work is bounded; pay lines sit at the end, which is read."""
    filler = "Maintain the tools. " * 20_000  # (400,000 characters)
    got = parse_pay(filler + "Pay: $25 - $30 per hour")
    assert got is not None and (got.min, got.max, got.period) == (25, 30, "hour")
    assert parse_pay("Pay: $25 - $30 per hour " + filler).listed
    assert parse_pay(filler[:200_000] + "Pay: $25 - $30 per hour " + filler[:200_000]) is None  # (the middle isn't)


def test_the_tooltip_quotes_salary_words_only_when_they_say_the_pay_shown():
    """The review: a job's salary words from one source were quoted beside pay read from another."""
    fields = {"pay_min": 26, "pay_max": 28, "pay_period": "hour", "pay_source": "text"}
    assert pay.view(fields, "$40 - $45 an hour")["from"] == "From the posting's text"
    assert pay.view(fields, "$26 - $28 an hour")["from"] == "From the posting's text: “$26 - $28 an hour”"
    indeed = {**fields, "pay_source": "indeed"}
    assert pay.view(indeed, "Estimated $30K a year")["from"] == "From Indeed's salary field (it may be Indeed's estimate)"


def test_the_desired_salary_refuses_what_isnt_a_yearly_figure():
    for said, want in [("$80k", 80000), ("25/hr", 52000), ("$70,000-$90,000", 70000), ("garbage", None), ("  ", None),
                       (0, None), (-5, None), ("80,000", 80000), ("eighty thousand", None), ("1e9", None),
                       ("nan", None), ("inf", None), (float("nan"), None), (float("inf"), None), (1e9, None),
                       ("$85,000/yr", 85000), ("85000 per year", 85000), ([1], None), ({"a": 1}, None)]:
        assert pay.desired_yearly(said) == want, said


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
