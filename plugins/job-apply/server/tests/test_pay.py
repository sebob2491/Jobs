"""A posting's pay read as figures (pay.py, the design of Oct 18, F1): structured first, then the posting's words."""

import math
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
    assert pay.meets(Pay(26, 28, "hour", sure=True), 60000) is False
    # (the second review) pay read from the text, not after a pay label, is never below a floor: it can't hide a job
    assert pay.meets(Pay(26, 28, "hour"), 60000) is None
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
    fields = {"pay_min": 26, "pay_max": 28, "pay_period": "hour", "pay_source": "text", "pay_sure": True}
    assert pay.view(fields, "$40 - $45 an hour")["from"] == "From the posting's pay line"
    assert pay.view(fields, "$26 - $28 an hour")["from"] == "From the posting's pay line: “$26 - $28 an hour”"
    indeed = {**fields, "pay_source": "indeed"}
    assert pay.view(indeed, "Estimated $30K a year")["from"] == "From Indeed's salary field"
    # (the second review) pay read from the text says so: it never hides the job
    assert pay.view({**fields, "pay_sure": False})["from"] == "From the posting's text (≈ read from the text: Pay at least never hides this job for it)"
    assert pay.view({**fields, "pay_sure": False})["sure"] is False and pay.view(fields)["sure"] is True


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
    assert view is not None and (view["top"], view["sort"], view["from"]) == (None, 45760, "From the posting's text (≈ read from the text: Pay at least never hides this job for it)")
    assert pay.view({}) is None and pay.view({"pay_min": None, "pay_max": None, "pay_period": ""}) is None


# The second review of the pay parser (scratchpad review-pay2/probes/p1-p7.py): 404 strings, each with the readings it
# may have (a list: any of them is right; None is "not listed")
REVIEWED_AGAIN = [
    ('Pay Range: $62,400 - $93,600', [(62400, 93600, 'year')]),
    ('Pay Range:\n$62,400.00 - $93,600.00', [(62400, 93600, 'year')]),
    ('Pay Range\n$24.50 - $36.75', [(24.5, 36.75, 'hour')]),
    ('Pay Range: 24.50 - 36.75 USD', [(24.5, 36.75, 'hour')]),
    ('Pay Range Minimum:\n$56,100.00\nPay Range Maximum:\n$98,200.00', [(56100, 98200, 'year')]),
    ('Hiring Range: $95,000 - $120,000 / Annual', [(95000, 120000, 'year')]),
    ('Compensation Range: $1,820.00 - $2,730.00 Biweekly', [(1820, 2730, 'biweekly')]),
    ('Expected Pay Range: $27.88 - $41.82 per hour', [(27.88, 41.82, 'hour')]),
    ('Target Pay Range: $105,000.00 - $157,500.00 annually', [(105000, 157500, 'year')]),
    ('The salary range for this position is $70,200 - $105,300. Individual pay is determined by work location, skills and experience.', [(70200, 105300, 'year')]),
    ('Pay Transparency: The base salary range for this role in California, Colorado, New York City, and Washington is $128,000 - $192,000. In Illinois and Maryland the range is $115,000 - $172,000.', [(115000, 192000, 'year')]),
    ('The pay range for this role is:\n115,200 - 172,800 USD per year (Remote - United States)\n126,700 - 190,100 USD per year (San Francisco, CA)', [(115200, 190100, 'year')]),
    ('Colorado/Washington Pay Range\n$90,000—$135,000 USD\nCalifornia Pay Range\n$99,000—$148,500 USD\nNew York City Pay Range\n$103,500—$155,250 USD', [(90000, 155250, 'year')]),
    ('For roles based in New York State, the hourly pay range is $22.00 to $28.50. For Washington State, $21.00 to $27.00.', [(21, 28.5, 'hour')]),
    ('Minimum Salary: $81,600.00 Maximum Salary: $134,600.00', [(81600, 134600, 'year')]),
    ('Salary Range: $73,600 - $110,400 (CO, CA, NY, WA); this range may vary based on location', [(73600, 110400, 'year')]),
    ("In compliance with Colorado's Equal Pay for Equal Work Act, the annual salary for this role is $65,000 - $78,000 plus a discretionary annual bonus of up to 10%.", [(65000, 78000, 'year')]),
    ('Maryland Pay Range: $46,000—$62,000 USD', [(46000, 62000, 'year')]),
    ('Illinois pay range $19.25/hr - $24.10/hr', [(19.25, 24.1, 'hour')]),
    ('The base compensation range for this role in the posted location is: $88,000.00 - $132,000.00', [(88000, 132000, 'year')]),
    ('Base Salary Range\n$140,000—$175,000 USD', [(140000, 175000, 'year')]),
    ('$140,000 - $175,000 a year', [(140000, 175000, 'year')]),
    ('Annual Salary\n$85,000 - $110,000', [(85000, 110000, 'year')]),
    ('The US base salary range for this full-time position is $96,000 - $132,000 + bonus + equity + benefits.', [(96000, 132000, 'year')]),
    ('Compensation: $120K - $160K + equity', [(120000, 160000, 'year')]),
    ('Compensation: $120–160K, plus equity and benefits', [(120000, 160000, 'year')]),
    ('Salary: $70k–$90k DOE', [(70000, 90000, 'year')]),
    ('$45 - $55 an hour', [(45, 55, 'hour')]),
    ('Hourly Rate\n$18—$22 USD', [(18, 22, 'hour')]),
    ('The expected compensation for this role is $55,000 - $70,000 per annum.', [(55000, 70000, 'year')]),
    ('Salary Range $26.00 - $28.00 Hourly', [(26, 28, 'hour')]),
    ('Salary Range $55,000.00 - $65,000.00 Annually', [(55000, 65000, 'year')]),
    ('Salary Range $17.00 - $19.00 Hourly\nShift: 2nd', [(17, 19, 'hour')]),
    ('Pay Rate: $17.50/Hour', [(17.5, 17.5, 'hour')]),
    ('Pay Type: Hourly\nPay Rate: 21.00', [(21, 21, 'hour')]),
    ('Minimum: 23.40 Maximum: 35.10 Pay Basis: Hourly', [(23.4, 35.1, 'hour'), None]),
    ('Pay Basis: Hourly\nMinimum Pay: $23.40\nMaximum Pay: $35.10', [(23.4, 35.1, 'hour')]),
    ('Compensation: $19.00 - $21.00 per hour depending on experience', [(19, 21, 'hour')]),
    ('Job Salary: $45,000.00 - $52,000.00 Yearly', [(45000, 52000, 'year')]),
    ('Wage: 16.50/hr', [(16.5, 16.5, 'hour')]),
    ('Wage: $16.50 p/h', [(16.5, 16.5, 'hour')]),
    ('Pay: $16.50 ph', [(16.5, 16.5, 'hour')]),
    ('Pay Range: $38.50 - $57.75 per hour based on experience. Shift differentials: Evening $3.00/hr, Night $5.00/hr, Weekend $2.50/hr.', [(38.5, 57.75, 'hour')]),
    ('$38.50 - $57.75 per hour based on experience, plus $4.50/hr night shift differential', [(38.5, 57.75, 'hour')]),
    ('Night Differential: $5.00/hr. Base pay: $41.20 - $61.80 per hour', [(41.2, 61.8, 'hour')]),
    ('Per Diem RN - $65/hr', [(65, 65, 'hour')]),
    ('PRN rate: $55.00 per hour', [(55, 55, 'hour')]),
    ('Travel RN: $2,450/week (includes housing stipend $1,200/wk)', [(2450, 2450, 'week'), None]),
    ('Weekly gross pay: $2,450. Taxable hourly rate: $22/hr; housing stipend: $1,100/wk; meals: $400/wk', [(22, 22, 'hour'), (2450, 2450, 'week'), None]),
    ('Pay is $28.62 per hour. Evening differential $1.75/hr. Night differential $3.25/hr.', [(28.62, 28.62, 'hour')]),
    ('Charge nurse premium $2/hr. Hourly pay $44-$66.', [(44, 66, 'hour')]),
    ('Starting rate $32.15/hr; up to $48.20/hr based on years of experience', [(32.15, 48.2, 'hour'), (32.15, None, 'hour')]),
    ('Earn up to $28/hr!', [(None, 28, 'hour')]),
    ('Up to $85,000 per year DOE', [(None, 85000, 'year')]),
    ('Starting at $19.50 per hour', [(19.5, None, 'hour')]),
    ('Starting pay: $19.50/hr', [(19.5, None, 'hour')]),
    ('Starting wage $17/hour with raises after 90 days', [(17, None, 'hour')]),
    ('Pay starting at $60,000 annually', [(60000, None, 'year')]),
    ('Salary: $60,000+ DOE', [(60000, None, 'year'), (60000, 60000, 'year')]),
    ('Pay: $22+/hr', [(22, None, 'hour')]),
    ('Salary DOE', [None]),
    ('Pay: DOE', [None]),
    ('Salary: Negotiable', [None]),
    ('$22/hr + $2,000 sign-on bonus', [(22, 22, 'hour')]),
    ('$22/hr plus $2,000 sign-on', [(22, 22, 'hour')]),
    ('$3,000 sign-on bonus! Pay: $21 - $24/hr', [(21, 24, 'hour')]),
    ('$5,000 Sign-On Bonus | $24/hr', [(24, 24, 'hour')]),
    ('OTE $120,000 (50/50 base/commission)', [(120000, 120000, 'year'), None]),
    ('Base salary $60,000 + commission, OTE $110,000', [(60000, 60000, 'year'), (60000, 110000, 'year')]),
    ('Base: $55K | OTE: $95K', [(55000, 55000, 'year'), (55000, 95000, 'year')]),
    ('Base pay of $45,000 plus uncapped commission', [(45000, 45000, 'year')]),
    ('Target pay: $85,000 - $95,000 (base + target incentive)', [(85000, 95000, 'year')]),
    ('On-target earnings of $150K–$180K', [(150000, 180000, 'year'), None]),
    ('Annual target total cash: $130,000', [(130000, 130000, 'year'), None]),
    ('Average earnings: $70,000 - $100,000 first year with commission', [(70000, 100000, 'year'), None]),
    ('$58,000 annually', [(58000, 58000, 'year')]),
    ('$58,000 annually, plus benefits', [(58000, 58000, 'year')]),
    ('Salary range: $95k-$115k', [(95000, 115000, 'year')]),
    ('Salary range: 95k-115k', [(95000, 115000, 'year')]),
    ('Salary: 95-115k', [(95000, 115000, 'year')]),
    ('Pay Grade 12: $52,300 - $78,450', [(52300, 78450, 'year')]),
    ('Grade 12: $52,300 - $78,450', [(52300, 78450, 'year'), None]),
    ('Salary Grade: E07 ($64,900 - $97,400)', [(64900, 97400, 'year')]),
    ('Pay Grade: 14\nSalary Range: $61,000 - $91,500', [(61000, 91500, 'year')]),
    ('Union scale per CBA: $24.36 - $31.12/hr (Step 1 - Step 8)', [(24.36, 31.12, 'hour')]),
    ('Wage: $26.81/hr (Local 104 journeyman scale) plus $12.40/hr fringe', [(26.81, 26.81, 'hour')]),
    ('Step 1: $21.05/hr, Step 5: $25.90/hr', [(21.05, 25.9, 'hour'), (21.05, 21.05, 'hour')]),
    ('Part-time, 20 hours per week, $16/hour', [(16, 16, 'hour')]),
    ('Part-time position, 24 hrs/week. Pay rate: $15.50', [(15.5, 15.5, 'hour')]),
    ('Internship pay: $25/hr', [(25, 25, 'hour')]),
    ('Summer Intern - $22.00/hour', [(22, 22, 'hour'), None]),
    ('This internship pays $24 an hour', [(24, 24, 'hour')]),
    ('Compensation: $120,000 - $150,000 plus bonus and equity', [(120000, 150000, 'year')]),
    ('Compensation: $120,000-$150,000, plus annual bonus target of 15% and equity', [(120000, 150000, 'year')]),
    ('The salary for this role is $85,000, with full benefits.', [(85000, 85000, 'year')]),
    ('Salary: $70,000 - $90,000, depending on experience', [(70000, 90000, 'year')]),
    ('Pay: $18 - $22/hr, plus shift differential', [(18, 22, 'hour')]),
    ('Pay range: $18.00 - $22.00, based on experience', [(18, 22, 'hour')]),
    ('Salary range: $95K - $115K, DOE', [(95000, 115000, 'year')]),
    ('We pay $25, $27, or $30 per hour depending on shift', [(25, 30, 'hour'), (30, 30, 'hour'), None]),
    ('Hourly pay: $20.50, Mon-Fri', [(20.5, 20.5, 'hour')]),
    ('Salary is $110,000, and the bonus target is 10%.', [(110000, 110000, 'year')]),
    ('Benefits: Medical, dental and vision starting day one; 401(k) with company match up to 6%; $5,250 annual tuition reimbursement; $1,500 wellness allowance; EAP. Pay: $24/hr.', [(24, 24, 'hour')]),
    ('We offer a 401(k) plan that matches up to 6% of your pay, and an HSA with a $1,000 employer contribution.', [None]),
    ('Our total rewards include an annual bonus, a $2,000 learning budget, and paid parental leave. The salary range is $80,000 - $100,000.', [(80000, 100000, 'year')]),
    ('$5,000 relocation package available. Annual salary $92,000.', [(92000, 92000, 'year')]),
    ('Relocation: up to $5,000. Salary: $92,000/yr', [(92000, 92000, 'year')]),
    ('Tuition assistance of up to $5,250 per year', [None]),
    ('Tuition assistance up to $5,250/year after 6 months of employment; competitive pay', [None]),
    ('Employee Assistance Program (EAP) and $50/month cell phone reimbursement; pay rate $19/hr', [(19, 19, 'hour')]),
    ('Life insurance at 2x annual salary up to $500,000', [None]),
    ('Short-term disability pays 60% of salary up to $1,500 per week', [None]),
    ('Company pays 100% of employee medical premiums; employee pays $25 per paycheck for dependents', [None]),
    ('HSA contribution of $1,200 annually; salary $75k', [(75000, 75000, 'year')]),
    ('We cover $150 per month for commuting expenses. Hourly rate $22.', [(22, 22, 'hour')]),
    ('Commuter benefit: $300/month pre-tax', [None]),
    ('Annual merit increases and a $1,000 annual wellness reimbursement', [None]),
    ('PTO: 120 hours annually. Pay: $21/hr', [(21, 21, 'hour')]),
    ('Earn 4 hours of PTO per pay period; pay $21/hr', [(21, 21, 'hour')]),
    ('Paid weekly. $18 per hour.', [(18, 18, 'hour')]),
    ('Annual uniform allowance of $500; hourly wage $23.75', [(23.75, 23.75, 'hour')]),
    ('Annual boot allowance up to $250', [None]),
    ('Tool allowance $1,000 annually; pay $30/hr', [(30, 30, 'hour')]),
    ('Annual salary increases of 3%; employees receive a $2,500 annual holiday bonus', [None]),
    ('Profit sharing averaging $8,000 per year in addition to an hourly rate of $26', [(26, 26, 'hour')]),
    ('Hourly employees eligible for quarterly incentive of up to $1,200', [None]),
    ('Annual Incentive Plan target: $15,000', [None]),
    ('Spot awards of $250 for great work; competitive pay', [None]),
    ('Pay increase of $1.00/hr after 6 months', [None]),
    ('Raises: $0.50/hr every year. Starting at $17/hr.', [(17, None, 'hour')]),
    ('Starting at $17/hr with a $0.50/hr raise every year', [(17, None, 'hour')]),
    ('Annual raise of $2,000', [None]),
    ('Earn an extra $2/hr for weekend shifts', [None]),
    ('Weekend premium $2/hr; base rate $20/hr', [(20, 20, 'hour')]),
    ('Overtime pay at 1.5x; base rate $20/hr', [(20, 20, 'hour')]),
    ('Bilingual pay: $1.00/hr additional', [None]),
    ('Lead pay: $1.50/hour above base', [None]),
    ('Hazard pay $3/hr', [None]),
    ('Holiday pay at time and a half. Weekly pay of $900.', [(900, 900, 'week')]),
    ('Location: 2200 W Rio Salado Pkwy, Tempe, AZ 85281. Pay: Competitive', [None]),
    ('Tempe, AZ 85281 | Pay: $20/hr', [(20, 20, 'hour')]),
    ('Questions about pay? Call 480-555-0199 ext 204.', [None]),
    ('Pay inquiries: (602) 555-0144', [None]),
    ('Requisition ID: 24051 | Pay Rate: $20.00', [(20, 20, 'hour')]),
    ('Req #: 98765 Salary Range: $90,000 - $110,000', [(90000, 110000, 'year')]),
    ('Job ID 102394 - Annual pay', [None]),
    ('Rate: 1 opening, 40 hrs/week', [None]),
    ('Requires 3-5 years of experience. Salary $65,000 - $75,000', [(65000, 75000, 'year')]),
    ('Salary: 2-5 years experience required', [None]),
    ('Hourly - 40 hours per week - Monday to Friday 8:00 - 4:30', [None]),
    ('Hourly, 2nd shift 3:00 PM - 11:30 PM', [None]),
    ('Hourly position. Shift: 6:00am-2:30pm. Pay: $19.00', [(19, 19, 'hour')]),
    ('Annual Salary Plan: E3 Grade 22', [None]),
    ('Pay Grade: 22', [None]),
    ('Pay Band: P3', [None]),
    ('Salary Grade 112', [None]),
    ('Pay class 4800 - 4900', [None]),
    ('Hourly: 1,920 hours per year', [None]),
    ('Annual hours: 2080', [None]),
    ('Pay is biweekly; 26 pay periods per year', [None]),
    ('Per hour, 12 hour shifts, 36 hours', [None]),
    ('Our 1,200 employees earn competitive pay', [None]),
    ('Earn 15 PTO days per year', [None]),
    ('This position pays per hour. We have 150 locations.', [None]),
    ('Annual revenue exceeds $2 billion; pay competitive', [None]),
    ('Salary: $120,000 - $140,000 (USD)', [(120000, 140000, 'year')]),
    ('Founded in 1998, we pay well', [None]),
    ('Since 2005, our hourly team has grown', [None]),
    ('Rate: $85 - $95 per hour (W2) / $100 - $110 per hour (C2C)', [(85, 110, 'hour'), (85, 95, 'hour')]),
    ('Pay rate: $65/hr on W2', [(65, 65, 'hour')]),
    ('Contract: 6 months, rate $70/hr 1099', [(70, 70, 'hour')]),
    ('Annual bonus of 10% of salary, up to $20,000', [None]),
    ('Bonus: 10% of base salary up to $20,000 annually', [None]),
    ('We match 100% of the first 4% of pay up to $6,000 per year', [None]),
    ('Annual Incentive Plan target: $15,000. Pay: $30/hr', [(30, 30, 'hour')]),
    ('Incentive: up to $8,000 annually. Hourly rate $25 - $28.', [(25, 28, 'hour')]),
    ('Annual commission potential of $40,000; base salary $50,000', [(50000, 50000, 'year'), (40000, 50000, 'year')]),
    ('Commission: $40,000 annually. Base: $50,000', [(50000, 50000, 'year')]),
    ('Profit sharing: $6,000 annually', [None]),
    ('Company contributes $3,000 annually to your HSA', [None]),
    ('HSA: $1,500 annually from the company', [None]),
    ('Medical plan with $1,500 annual deductible; salary $70k', [(70000, 70000, 'year')]),
    ('Copay: $25 per visit; pay rate $20/hr', [(20, 20, 'hour')]),
    ('Health plan costs $40 per paycheck', [None]),
    ('Employee contribution: $40 per pay period', [None]),
    ('Paid time off pays out up to $2,000 annually', [None]),
    ('Car allowance: $600 monthly. Salary: $85,000', [(85000, 85000, 'year')]),
    ('Monthly car allowance $600 and salary $85,000', [(85000, 85000, 'year')]),
    ('$600 monthly car allowance; salary $85,000', [(85000, 85000, 'year')]),
    ('Annual Equity Grant: $25,000', [None]),
    ('Annual stock award valued at $25,000', [None]),
    ('Annual education reimbursement of $3,000', [None]),
    ('Annual Learning & Development fund: $2,500', [None]),
    ('Annual fitness subsidy: $600', [None]),
    ('Annual home office stipend: $500; salary $100k', [(100000, 100000, 'year')]),
    ('Professional development: $1,500 annually', [None]),
    ('Childcare subsidy of $250 per month', [None]),
    ('We pay $250 per month toward childcare', [None]),
    ('We pay up to $10,000 per year for student loan repayment', [None]),
    ('We pay $100/month for internet', [None]),
    ('We pay 100% of premiums and $2,000/yr into your HSA', [None]),
    ('Annual cost of living adjustment: $1,200', [None]),
    ('Annual salary: $1,200 COLA plus base of $52,000', [(52000, 52000, 'year')]),
    ('Annual Performance Award: up to $10,000', [None]),
    ('Hourly Rate: $22.00\nAnnual Bonus: $1,000', [(22, 22, 'hour')]),
    ('Annual Bonus: $1,000\nHourly Rate: $22.00', [(22, 22, 'hour')]),
    ('Annual Bonus Potential\n$15,000\nBase Salary\n$95,000', [(95000, 95000, 'year')]),
    ('Annual Bonus Target\n15%\nBase Salary Range\n$95,000 - $120,000', [(95000, 120000, 'year')]),
    ('Annual Total Rewards Value: $120,000 (salary $90,000 + benefits)', [(90000, 90000, 'year'), None]),
    ('Total compensation package worth $95,000 including salary of $70,000', [(70000, 70000, 'year')]),
    ('Earn $500 for every referral hired; hourly pay $18', [(18, 18, 'hour')]),
    ('Daily pay available! $18/hr', [(18, 18, 'hour')]),
    ('Get paid daily! Earn $18/hr', [(18, 18, 'hour')]),
    ('Weekly pay + $500 sign on. $19/hr', [(19, 19, 'hour')]),
    ('Weekly pay; $200 attendance incentive monthly; $19/hr', [(19, 19, 'hour')]),
    ('Annual salary $1', [None]),
    ('Hourly rate: $1,000', [None]),
    ('Day rate: $450', [(450, 450, 'day')]),
    ('Day rate $450 - $550', [(450, 550, 'day')]),
    ('Pay: $450 per shift', [None]),
    ('Pay: $1,200 per shift (24 hr)', [None]),
    ('Pay: $250 per visit', [None]),
    ('Pay: $0.60 per mile', [None]),
    ('Pay: 65 cents per mile; average $1,500/week', [(1500, 1500, 'week'), None]),
    ('Pay: $0.55-$0.65 CPM', [None]),
    ('Pay: $15 per piece', [None]),
    ('Pay: $75 per session', [None]),
    ('Stipend: $500 per semester. Pay: $15/hr', [(15, 15, 'hour')]),
    ('Pay: $25 per class', [None]),
    ('Pay: $3,500 per course', [None]),
    ('Adjunct pay: $1,100 per credit hour', [None]),
    ('Pay: $2,000 per project', [None]),
    ('Pay per load: $300', [None]),
    ('Commission only: $500 per sale', [None]),
    ('Annual Salary: 52 weeks x $1,000', [None]),
    ('Pay Range: $24.50 - $36.75\nAnnual bonus eligible', [(24.5, 36.75, 'hour')]),
    ('Pay Range: $62,400 - $93,600\nHourly employees are paid weekly', [(62400, 93600, 'year')]),
    ('Hourly Rate: $22.00\nWeekly schedule: Mon-Fri', [(22, 22, 'hour')]),
    ('Salary: $85,000\nYearly review', [(85000, 85000, 'year')]),
    ('Pay: $21/hr\nMonthly team lunches', [(21, 21, 'hour')]),
    ('Salary range: $4,500 - $5,500\nMonthly', [(4500, 5500, 'month'), None]),
    ('Pay Rate: $20.00\n\nAnnual Leave: 15 days', [(20, 20, 'hour')]),
    ('Salary: CAD $85,000 - $95,000', [None]),
    ('Salary: $85,000 - $95,000 CAD', [None]),
    ('Compensation: C$85,000 - C$95,000', [None]),
    ('Salary range: CA$85,000–CA$95,000', [None]),
    ('Pay: $85,000 - $95,000 AUD per annum', [None]),
    ('Salary: AU$90k + super', [None]),
    ('Salary: NZ$80,000', [None]),
    ('Salary: €50.000 - €60.000 per year', [None]),
    ('Salary: €50,000 - €60,000', [None]),
    ('Salary: 50.000 - 60.000 EUR brutto/Jahr', [None]),
    ('Salary: £45,000 - £55,000 per annum', [None]),
    ('Salary: 45,000 - 55,000 GBP', [None]),
    ('Salary: ₹12,00,000 - ₹18,00,000 per annum', [None]),
    ('CTC: INR 12-18 LPA', [None]),
    ('Salary: 12,00,000 - 18,00,000 INR', [None]),
    ('Salary: MXN $25,000 mensuales', [None]),
    ('Salary: S$6,000 - S$8,000 per month', [None]),
    ('Salary: HK$40,000/month', [None]),
    ('Pay: 3,500 zł', [None]),
    ('Salary range: 600,000 - 800,000 JPY', [None]),
    ('Salary: ¥8,000,000', [None]),
    ('US Salary: $120,000 - $150,000 | Canada Salary: CAD $140,000 - $170,000', [(120000, 150000, 'year')]),
    ('Salary range (Canada): $95,000 - $115,000 CAD\nSalary range (US): $85,000 - $105,000 USD', [(85000, 105000, 'year')]),
    ('Toronto, ON - Salary: $95,000 - $115,000', [None, (95000, 115000, 'year')]),
    ('Salary: $72,000 per annum', [(72000, 72000, 'year')]),
    ('Salary: $72,000 p.a.', [(72000, 72000, 'year')]),
    ('Salary: $72,000/yr.', [(72000, 72000, 'year')]),
    ('Salary: $72k/yr', [(72000, 72000, 'year')]),
    ('$72K/year', [(72000, 72000, 'year'), None]),
    ('Pay: $35 an hour', [(35, 35, 'hour')]),
    ('Pay: $35 hourly', [(35, 35, 'hour')]),
    ('Pay: $35 hr', [(35, 35, 'hour')]),
    ('Pay: $35.00 p/hr', [(35, 35, 'hour')]),
    ('Pay: $35/h', [(35, 35, 'hour')]),
    ('Pay: 35 USD/hour', [(35, 35, 'hour')]),
    ('Pay: USD 35 per hour', [(35, 35, 'hour')]),
    ('Pay: US$35/hr', [(35, 35, 'hour')]),
    ('Pay: $35 - $40/hour DOE', [(35, 40, 'hour')]),
    ('Pay: $35.00 – $40.00 hourly (non-exempt)', [(35, 40, 'hour')]),
    ('Salary: $72,000 – $80,000 per year (exempt)', [(72000, 80000, 'year')]),
    ('Pay: $35 to $40 per hr', [(35, 40, 'hour')]),
    ('Pay: between $35 and $40 an hour', [(35, 40, 'hour')]),
    ('Pay: $35-40/hr', [(35, 40, 'hour')]),
    ('Pay: $35.5-40.25/hr', [(35.5, 40.25, 'hour')]),
    ('Pay: $35 – 40', [(35, 40, 'hour')]),
    ('Pay: 35-40/hr', [(35, 40, 'hour'), None]),
    ('Pay: $2,800 - $3,400 monthly', [(2800, 3400, 'month')]),
    ('Pay: $3,400/mo', [(3400, 3400, 'month')]),
    ('Pay: $3,400 a month', [(3400, 3400, 'month')]),
    ('Pay: $1,100 per week', [(1100, 1100, 'week')]),
    ('Pay: $1,100/wk', [(1100, 1100, 'week')]),
    ('Salary: $2,692.31 bi-weekly', [(2692.31, 2692.31, 'biweekly')]),
    ('Salary: $2,916.67 semi-monthly', [(2916.67, 2916.67, 'semimonthly')]),
    ('Salary: $70K-$80K/yr + bonus', [(70000, 80000, 'year')]),
    ('Salary: $70-80k', [(70000, 80000, 'year')]),
    ('Salary: $70 - $80K', [(70000, 80000, 'year')]),
    ('Salary: 70K', [(70000, 70000, 'year')]),
    ('Salary: $70.5K - $82.3K', [(70500, 82300, 'year')]),
    ('Salary: $1.2M', [None]),
    ('$85,000.00 to $95,000.00 annual salary', [(85000, 95000, 'year')]),
    ('$85,000 to $95,000 per year base salary', [(85000, 95000, 'year')]),
    ('Annual base: $85,000 to $95,000', [(85000, 95000, 'year')]),
    ('Why join us? Pay: $25/hr', [(25, 25, 'hour')]),
    ('Interested? Apply today! Pay: $18/hr', [(18, 18, 'hour')]),
    ('Ready to grow your career? Salary range: $80,000 - $100,000', [(80000, 100000, 'year')]),
    ("What's in it for you? Starting pay $21/hr and full benefits", [(21, None, 'hour')]),
    ('Your base pay range: $120,000 - $150,000', [(120000, 150000, 'year')]),
    ('Your Compensation: $95,000 - $115,000 annually', [(95000, 115000, 'year')]),
    ('Your pay: $24/hr', [(24, 24, 'hour')]),
    ('Your salary will be $85,000 per year', [(85000, 85000, 'year')]),
    ('The current pay range for this position is $28.00 - $34.00 per hour.', [(28, 34, 'hour')]),
    ('The current salary range is $70,000 - $85,000', [(70000, 85000, 'year')]),
    ('Current hiring range: $22 - $25/hr', [(22, 25, 'hour')]),
    ('Desired salary: $80,000', [None]),
    ('What are your salary expectations? (e.g. $80,000)', [None]),
    ('Expected Salary: $90,000 - $110,000', [None, (90000, 110000, 'year')]),
    ('Rate: $45 per unit', [None]),
    ('Pay: $30 per appointment', [None]),
    ('Pay: $18 per delivery plus tips', [None]),
    ('Pay: $120 per call-out', [None]),
    ('Pay: $35 per patient visit', [None]),
    ('Pay: $20 per lesson', [None]),
    ('Pay: $2 per mile', [None]),
    ('Pay: $100 per day of training, then $25/hr', [(25, 25, 'hour'), (100, 100, 'day')]),
    ('Life insurance: 1x annual salary up to $250,000', [None]),
    ('401(k): company matches 50% of pay up to 6%; salary $70,000', [(70000, 70000, 'year')]),
    ('Severance of 2 weeks pay per year of service up to $20,000', [None]),
    ('Base pay: $20/hr. Overtime rate: $30/hr', [(20, 20, 'hour')]),
    ('Night differential pay: $8/hr', [None]),
    ('Base pay $20/hr; shift differential pay of $8/hr', [(20, 20, 'hour')]),
    ('Overtime rate $30/hr', [None]),
    ('On-call rate: $5/hr', [None]),
    ('On-call pay: $150/day', [None]),
    ('Callback pay: $40/hr minimum 2 hrs; hourly rate $27', [(27, 27, 'hour')]),
    ('Standby rate $3.50/hr; regular hourly rate $31.20', [(31.2, 31.2, 'hour')]),
    ('Training rate: $15/hr for 2 weeks', [(15, 15, 'hour')]),
    ('Holiday rate: $45/hr', [None]),
    ('Compensation: $18.50/hr (increases to $20.00/hr after 90 days)', [(18.5, 20, 'hour')]),
    ('Pay: $18.50/hr, increasing to $20/hr after training', [(18.5, 20, 'hour')]),
    ('This is a full-time, non-exempt position paying $21.63 per hour ($45,000 annualized).', [(21.63, 21.63, 'hour')]),
    ('Annualized salary: $45,000 ($21.63/hr)', [(45000, 45000, 'year')]),
    ('$45,000/year ($21.63/hour)', [(45000, 45000, 'year')]),
    ('Salary: Starting at $45,000 based on 2,080 hours', [(45000, None, 'year')]),
    ('Salary Range: $45,000 - $55,000 based on 40 hours/week', [(45000, 55000, 'year')]),
    ('Pay: 40 hrs/week at $20/hr', [(20, 20, 'hour')]),
    ('Pay: $20/hr x 40 hrs = $800/week', [(20, 20, 'hour'), (800, 800, 'week')]),
    ('Pay range: $20-$25 per hour, 32-40 hours per week', [(20, 25, 'hour')]),
    ('Annual salary for 0.8 FTE: $60,000', [(60000, 60000, 'year')]),
    ('Salary (1.0 FTE): $75,000 - $90,000; prorated for part-time', [(75000, 90000, 'year')]),
    ('Stipend: $3,000/month (PhD intern)', [None]),
    ('Internship compensation: $7,000 per month', [(7000, 7000, 'month')]),
    ('Intern hourly rates range from $20 to $45 depending on degree program', [(20, 45, 'hour')]),
    ('Salary range: $0 - $0', [None]),
    ('Salary: $00.00', [None]),
    ('Pay rate: $TBD', [None]),
    ('Hourly rate: $XX.XX', [None]),
    ('Salary: $[min] - $[max]', [None]),
    ('Pay: $15.00 - $15.00', [(15, 15, 'hour')]),
    ('Pay: $15 - $1,500', [None]),
    ('Salary: $50,000 - $5,000,000', [None]),
    ('Pay Range: $65,000.00 - $105,000.00 Pay Range: $65,000.00 - $105,000.00', [(65000, 105000, 'year')]),
    ('Pay Range\n$65,000 USD - $105,000 USD', [(65000, 105000, 'year')]),
    ('Pay Range: USD $65,000.00 - USD $105,000.00/Yr.', [(65000, 105000, 'year')]),
    ('Base Pay: $65,000.00/Yr. - $105,000.00/Yr.', [(65000, 105000, 'year')]),
    ('Salary: $65,000.00/Year to $105,000.00/Year', [(65000, 105000, 'year')]),
    ('Pay: $27.50/Hr. - $32.00/Hr.', [(27.5, 32, 'hour')]),
    ('Earn up to $60,000 your first year! Pay: $25/hr', [(25, 25, 'hour'), (None, 60000, 'year')]),
    ('Average annual earnings of $52,000. Hourly pay $25', [(25, 25, 'hour'), (52000, 52000, 'year')]),
    ('Pay: $7,000 - $9,000 per month (equivalent to $84,000 - $108,000 annually)', [(7000, 9000, 'month')]),
    ('Salary: $3,000 monthly. Annual Bonus: 5%', [(3000, 3000, 'month')]),
    ('Salary: $50.000 per year', [None]),
    ('Pay: $1.234,56 per month', [None]),
    ('Pay $25/hr.Shift: nights', [(25, 25, 'hour')]),
    ('PAY: $25/HR', [(25, 25, 'hour')]),
    ('SALARY RANGE: $80,000-$100,000/YEAR', [(80000, 100000, 'year')]),
    ('Salary range:$80,000-$100,000', [(80000, 100000, 'year')]),
    ('Pay: $25-$30/hr ($52,000-$62,400/yr)', [(25, 30, 'hour')]),
    ('Pay: ~$25/hr', [(25, 25, 'hour')]),
    ('Pay: approx. $60k', [(60000, 60000, 'year')]),
    ('Pay: $25/hr (non-negotiable)', [(25, 25, 'hour')]),
    ('Salary: $60k-$70k (negotiable)', [(60000, 70000, 'year')]),
    ('Hourly Pay: 22', [(22, 22, 'hour')]),
    ('Pay: twenty dollars an hour', [None]),
    ('Annual value of benefits: $18,000. Pay: $25/hr', [(25, 25, 'hour')]),
    ('Total annual compensation including benefits averages $72,000; hourly pay $26', [(26, 26, 'hour'), (72000, 72000, 'year')]),
    ('Annual Merit Pool: 3.5%. Pay: $25/hr', [(25, 25, 'hour')]),
    ('Annual Salary: $4,000 per month', [(4000, 4000, 'month')]),
    ('Hourly rate: $28.00 / Annual equivalent: $58,240', [(28, 28, 'hour')]),
    ('Monthly Salary: $6,250 | Annual: $75,000', [(6250, 6250, 'month')]),
    ('Gross pay: $3,500 per month plus housing ($1,200/month value)', [(3500, 3500, 'month')]),
    ('Monthly rent assistance: $1,000; salary $60,000', [(60000, 60000, 'year')]),
    ('Pay: $25/hr; mileage reimbursed at $0.67/mile', [(25, 25, 'hour')]),
    ('Pay: $25/hr; mileage at $0.67 per mile', [(25, 25, 'hour')]),
    ('Pay rate: $24/hr\nMileage: $0.67/mile\nPhone: $50/month', [(24, 24, 'hour')]),
    ('Pay: $24/hr\nPhone stipend: $50/month\nMeal per diem: $59/day', [(24, 24, 'hour')]),
    ('Pay: $24/hr; meals and incidentals $59/day', [(24, 24, 'hour')]),
    ('Salary: $95,000 - $105,000; housing provided ($2,000/mo value)', [(95000, 105000, 'year')]),
    ('Pay: $26.50/hr + $1,500/mo housing', [(26.5, 26.5, 'hour')]),
    ('Pay: $26.50/hr + $1,500/mo lodging', [(26.5, 26.5, 'hour')]),
    ('Annual Bonus Potential: $15,000\nBase Salary: $95,000', [(95000, 95000, 'year')]),
    ('Hourly Rate: $22.00 | Annual Bonus: $1,000', [(22, 22, 'hour')]),
    ('Pay: $22/hr | Annual bonus up to $1,500', [(22, 22, 'hour')]),
]


@pytest.mark.parametrize("words,accept", REVIEWED_AGAIN)
def test_the_second_reviews_strings_read_as_intended(words, accept):
    got = parse_pay(words)
    assert ((got.min, got.max, got.period) if got is not None and got.listed else None) in accept, (words, got)


def _top(reading):
    if reading is None:
        return math.inf
    return yearly(Pay(*reading))[1] or math.inf


@pytest.mark.parametrize("words,accept", REVIEWED_AGAIN)
def test_no_reading_of_the_second_reviews_strings_hides_a_job_it_shouldnt(words, accept):
    """Whatever a string reads as, a floor hides its job only when every right reading of it is below that floor."""
    got = parse_pay(words)
    for floor in (20_000, 40_000, 60_000, 80_000, 120_000, 200_000):
        if pay.meets(got, floor) is False:  # (the first reading is the review's: the others are allowed too)
            assert _top(accept[0]) < floor, (words, got, floor)


# the second review's strings whose reading by the parser before it (89ddfee) hid the job under a floor its pay meets
# (the floor given): none of them does now
HIDES = [
    ('Salary: $60,000+ DOE', 60001),
    ('Pay: $22+/hr', 45761),
    ('Compensation: $120,000-$150,000, plus annual bonus target of 15% and equity', 120001),
    ('Salary: $70,000 - $90,000, depending on experience', 70001),
    ('Salary range: $95K - $115K, DOE', 95001),
    ('Hourly pay: $20.50, Mon-Fri', 41601),
    ('Life insurance at 2x annual salary up to $500,000', 500001),
    ('Short-term disability pays 60% of salary up to $1,500 per week', 78001),
    ('Company pays 100% of employee medical premiums; employee pays $25 per paycheck for dependents', 52001),
    ('Annual Incentive Plan target: $15,000', 15001),
    ('Annual bonus of 10% of salary, up to $20,000', 20001),
    ('Bonus: 10% of base salary up to $20,000 annually', 20001),
    ('Annual Incentive Plan target: $15,000. Pay: $30/hr', 15001),
    ('Employee contribution: $40 per pay period', 83201),
    ('Pay: $250 per visit', 520001),
    ('Pay: $15 per piece', 31201),
    ('Pay: $75 per session', 156001),
    ('Pay: $25 per class', 52001),
    ('Salary: CAD $85,000 - $95,000', 95001),
    ('Salary: $85,000 - $95,000 CAD', 95001),
    ('Pay: $85,000 - $95,000 AUD per annum', 95001),
    ('Salary: AU$90k + super', 90001),
    ('Salary: 50.000 - 60.000 EUR brutto/Jahr', 124801),
    ('Salary: 45,000 - 55,000 GBP', 55001),
    ('Salary: MXN $25,000 mensuales', 25001),
    ('Salary range: 600,000 - 800,000 JPY', 800001),
    ('Toronto, ON - Salary: $95,000 - $115,000', 115001),
    ('Rate: $45 per unit', 93601),
    ('Pay: $30 per appointment', 62401),
    ('Pay: $18 per delivery plus tips', 37441),
    ('Pay: $120 per call-out', 249601),
    ('Pay: $35 per patient visit', 72801),
    ('Pay: $20 per lesson', 41601),
    ('Pay: $100 per day of training, then $25/hr', 26001),
    ('Life insurance: 1x annual salary up to $250,000', 250001),
    ('Severance of 2 weeks pay per year of service up to $20,000', 20001),
    ('Holiday rate: $45/hr', 93601),
    ('Annual value of benefits: $18,000. Pay: $25/hr', 18001),
]


@pytest.mark.parametrize("words,floor", HIDES)
def test_what_hid_a_job_before_doesnt(words, floor):
    assert pay.meets(parse_pay(words), floor) is not False, (words, parse_pay(words))


def test_only_pay_said_on_its_pay_line_is_sure():
    """The second review's safeguard: free text will always have misses, so only structured pay and a figure right
    after a pay label is `sure`; any other is shown, but never fails a floor (it can't hide a job)."""
    for words in ("Pay Range: $62,400 - $93,600", "Salary Range $26.00 - $28.00 Hourly", "Hourly Rate\n$18—$22 USD",
                  "Starting pay: $19.50/hr", "Maryland Pay Range: $46,000—$62,000 USD", "Base: $55K | OTE: $95K",
                  "Minimum Salary: $81,600.00 Maximum Salary: $134,600.00", "Pay Range:\n\n$24.00\n-\n$30.00",
                  "Compensation & Growth • Base Salary Range: $71,360.00 - $107,040.00"):
        assert parse_pay(words).sure, words
    for words in ("The salary range for this position is $70,200 - $105,300.", "$45 - $55 an hour",
                  "Pay: $15 per hour (training rate), then $20 per hour", "Earn up to $28/hr!",
                  "Annual Salary Range for jobs which could be performed in the US: $220,320.00-311,040.00 USD"):
        got = parse_pay(words)
        assert got.listed and not got.sure, words
        assert pay.meets(got, 10_000_000) is None  # (never below a floor)
    assert parse_pay("$25 an hour", whole=True, source="indeed").sure
    assert not parse_pay("Estimated $45.2K - $57.3K a year", whole=True, source="estimate").sure
    assert pay_from_ld({"currency": "USD", "value": 25}).sure


def test_a_job_boards_estimate_ranks_below_the_postings_own_words():
    """The second review: Indeed's "Estimated …" salary is its guess: the posting's words go first, and the estimate,
    when the words say nothing, is never sure."""
    indeed = "https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7b"
    p = finalize(Posting(url=indeed, title="Technician", description="Pay: $20 - $22 per hour. " + "Duties. " * 40,
                         salary="Estimated $45.2K - $57.3K a year", source="indeed", parse_method="manual"))
    assert (p.pay_min, p.pay_max, p.pay_period, p.pay_source, p.pay_sure) == (20, 22, "hour", "text", True)
    p = finalize(Posting(url=indeed, title="Technician", description="Duties. " * 40,
                         salary="Estimated $45.2K - $57.3K a year", source="indeed", parse_method="manual"))
    assert (p.pay_min, p.pay_max, p.pay_period, p.pay_source, p.pay_sure) == (45200, 57300, "year", "estimate", False)
    assert pay.better({"pay_min": 20, "pay_max": 22, "pay_period": "hour", "pay_source": "text"},
                      {"pay_min": 45200, "pay_max": 57300, "pay_source": "estimate"})
    assert not pay.better({"pay_min": 45200, "pay_max": 57300, "pay_period": "year", "pay_source": "estimate"},
                          {"pay_min": 20, "pay_max": 22, "pay_source": "text"}, again=True)
    view = pay.view({"pay_min": 45200, "pay_max": 57300, "pay_period": "year", "pay_source": "estimate"})
    assert view["sure"] is False and view["from"].startswith("Indeed's estimate")


def test_a_day_rate_says_how_its_year_is_counted():
    got = parse_pay("Day rate: $450")
    assert (got.min, got.max, got.period, got.sure) == (450, 450, "day", True)
    view = pay.view(pay.fields(got))
    assert view["shown"] == "$450/day (≈$117k/yr)" and "260 days a year" in view["from"]
