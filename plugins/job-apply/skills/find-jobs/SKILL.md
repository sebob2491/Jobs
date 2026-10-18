---
name: find-jobs
description: Find job openings that match the user's profile, using Indeed (through the Indeed connector) and employers' own career sites (Arizona semiconductor companies such as ASML, Lam Research, Applied Materials, KLA, Intel, TSMC and Microchip by default, or large Phoenix-area employers in health care, finance, government and more on the Phoenix list), then save the good matches to the job-apply tracker. Use when the user asks to find, search for or line up jobs, e.g. "find field service engineer jobs in Phoenix", "find HR or accounting jobs in Phoenix" or "what's open at Lam and ASML?".
---

# Find jobs

The goal is a short, deduplicated list of real openings saved to the tracker
(status `saved`). The `apply` skill then works through it.

1. **Criteria.** Run `get_profile` and read `preferences.titles` and
   `preferences.locations`. Fill gaps from the request, or ask one question.
   Default to the last 30 days (`posted_within_days=30` below). A pay floor the user names
   ("at least $60k", "$25 an hour or more") is `min_pay`, in dollars a year (an hourly rate
   × 2,080).

2. **Indeed.** If the Indeed connector's tools are available (`search_jobs`,
   `get_job_details`), search each title × location. For each promising result,
   get its details and save it with `add_job(url, title, company, description,
   location, salary, source="indeed")`.

3. **Company career sites.** Run `search_company_jobs` with `location="AZ"` (or the
   user's area) and a query that covers how each employer names the role. For
   field service that's
   `"field service | customer service engineer | customer engineer | equipment technician | equipment engineer"`.
   KLA says "Customer Service Engineer", Applied Materials says "Customer Engineer",
   and TEL often says "Field Engineer". Results whose title matches come first
   (`title_match: true`). The others matched on the description, so read their
   titles before discarding them.
   It queries each company's own job search directly: Workday, Greenhouse, Lever,
   Eightfold, SmartRecruiters, Oracle, ApplicantStack, iCIMS, Paycom, UKG Pro, SuccessFactors
   (Edwards, Qorvo, Amkor), Infor CloudSuite (Benchmark), Taleo (Kforce), Talemetry
   (Valleywise Health), iCIMS Jibe (PetSmart, Sprouts, State Farm), Jobvite (Knight-Swift),
   Cornerstone (Linde, Matheson), Phenom (EMD Electronics) and Avature (Deloitte) sites, amazon.jobs, Edward Jones' search and Phoenix Children's, Randstad's
   and KPMG's own job sites, for the companies in the employer list `companies_file` in
   `setup_status` names: the plugin's `data/companies.yaml`, or the user's own
   `~/.job-apply/companies.yaml` when they have one (for other fields or places; the same shape).
   Pass `companies=[...]` to limit it to particular employers.
   - `posted_within_days=30` (or 7, 14) leaves out older openings. An opening whose site
     gives no date (ApplicantStack, Talemetry, Jobvite, Avature, KPMG, Phoenix Children's and
     careers pages don't) stays: say its date is unknown. Greenhouse's date is the day the
     posting was last updated.
   - `min_pay` leaves out an opening whose pay is listed and below it (`below_min_pay` counts
     them). Most employers' lists don't give pay, so most results have no `pay`: they stay,
     and the posting says (`ingest_job` reads it: `pay` on the saved job, from the posting's
     own data or its words, such as "Salary Range $26.00 - $28.00 Hourly"). Only pay the posting
     states on its pay line (or in its own data) can leave a job out; pay only read from its
     other words is shown with "≈" and never does. Never drop a job for pay it doesn't list.
     `list_jobs(min_pay=...)` does the same over the tracker.
   - Results already in the tracker carry `tracked`. Skip those.
   - Eightfold sites (Lam Research, Micron, Infineon) refuse direct API calls, and
     ASML's, Daifuku's (iCIMS), Ebara's (Paycom), Nikon Precision's (UKG Pro), Edwards' and
     Amkor's (SuccessFactors) and Benchmark's (Infor) searches only answer their own pages. For these the tool loads the site's search page in a
     background browser tab and reads the results from it, which takes a few seconds per company. Amkor's
     list doesn't say where each job is, so each matching posting is read for the place it names.
   - Workday lists a job in several places as "7 Locations". The search reads those postings
     for their places, the ones in the area first ("Phoenix, AZ; Austin, TX; ...").
   - Deloitte lists most jobs as "Multiple Locations", and its Arizona filter also finds some
     that aren't offered there. The search reads 20 of those postings for their places ("Tempe,
     Arizona, United States (+38 more)") and leaves out the ones not in the area. The rest say
     "check the posting".
   - `errors` lists companies whose search failed. Fall back to the browser for
     those: open the careers URL with `open_application(url=...)`, use the site's search
     box with `fill_form` and `click`, and read the results with `page_text`. WebSearch
     with `site:` on the careers domain also works.
   - `browser_only` lists employers with no search the plugin can use: TSMC Arizona in the
     default list, and with the `phoenix-metro` list four more (a system it can't
     search, such as PeopleSoft's City of Phoenix site, or a site that refuses automated browsers, such as
     ADP's and Maricopa Community Colleges' Cloudflare checks). Nothing tries to get around those. Don't open
     them in the desk's browser window: give the user their careers links, a short list, to
     search in their own browser, or look for their postings on Indeed.
   - `ingest_job(url)` each posting worth saving. It stores the full description.
   Look at the first page or two of results only, at a normal pace. This is one
   person's job search, not a crawl.

4. **LinkedIn.** Don't search or scrape LinkedIn automatically. Its terms prohibit
   it, and it restricts accounts that do it. Offer the user a search link to open
   themselves, e.g.
   `https://www.linkedin.com/jobs/search/?keywords=field%20service%20engineer&location=Phoenix%2C%20Arizona&f_TPR=r2592000`,
   and ask them to paste the postings they like. Then run `ingest_job` on each.

5. **Deduplicate and rank.** `list_jobs` shows what's already tracked. The tracker
   also collapses repeated URLs, and keeps the same job from Indeed (or LinkedIn) and the
   employer's own site as one job when it's surely the same (same employer, title, city and
   requisition number, saved within 30 days, the Indeed job not begun), applied for on the
   employer's site, with the board's link kept as an alias. Otherwise both are kept, marked
   as maybe the same, and the Job Desk asks before applying to one while the other is begun
   or applied to. So save the employer's posting of an Indeed job too. Rank by fit with the profile: title match,
   location, hard requirements met. Roles that need a clearance, degree or citizenship
   the user doesn't have (and don't accept equivalent experience) go below the ranked
   list, each with its reason, as the Job Desk shows them under **All**: the user decides.

6. **Report.** Give a table with id, company, title, location, pay ("not listed" where
   the posting doesn't say), job system and a one-line fit note, then ask which ones to
   apply to. The Job Desk shows the same pay on each opening, sorts by fit, pay or newest,
   and has "Pay at least $__/yr" and "Posted: 7 / 14 / 30 days" filters that never hide an
   opening that doesn't say. The `apply` skill takes it from there.

7. **Each morning.** Offer once: "each morning: search, then whats_new". After a search
   (this skill's, or the Job Desk's Find jobs), `whats_new()` lists the openings first
   saved since its last call, best score first, leaving out the ones the user marked Not
   interested on the desk. It never searches by itself.
