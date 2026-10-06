---
name: find-jobs
description: Find job openings that match the user's profile, using Indeed (through the Indeed connector) and semiconductor-company career sites (ASML, Lam Research, Applied Materials, KLA, Intel, TSMC, Microchip and others), then save the good matches to the job-apply tracker. Use when the user asks to find, search for or line up jobs, e.g. "find field service engineer jobs in Phoenix" or "what's open at Lam and ASML?".
---

# Find jobs

The goal is a short, deduplicated list of real openings saved to the tracker
(status `saved`). The `apply` skill then works through it.

1. **Criteria.** Run `get_profile` and read `preferences.titles` and
   `preferences.locations`. Fill gaps from the request, or ask one question.
   Default to the last 30 days.

2. **Indeed.** If the Indeed connector's tools are available (`search_jobs`,
   `get_job_details`), search each title × location. For each promising result,
   get its details and save it with `add_job(url, title, company, description,
   location, salary, source="indeed")`.

3. **Company career sites.**
   The `companies_file` path that `setup_status` returns
   (`<plugin>/data/companies.yaml`) lists semiconductor employers with
   Arizona sites, with each one's careers URL and ATS. For the companies the user
   cares about:
   - Workday sites (`*.myworkdayjobs.com/<site>`) have a search box. Open the site
     with `open_application(url=...)`, `fill_form` the search field, `click` Search,
     and read results with `page_text`.
   - For other sites, open the careers URL and use the site's own search the same
     way, or use WebSearch with `site:` on the careers domain.
   - `ingest_job(url)` each posting worth saving. It stores the full description.
   Read only the first page or two of results, at a normal pace. This is one
   person's job search, not a crawl.

4. **LinkedIn.** Don't search or scrape LinkedIn automatically. Its terms prohibit
   it, and it restricts accounts that do it. Offer the user a search link to open
   themselves, e.g.
   `https://www.linkedin.com/jobs/search/?keywords=field%20service%20engineer&location=Phoenix%2C%20Arizona&f_TPR=r2592000`,
   and ask them to paste the postings they like. Then run `ingest_job` on each.

5. **Deduplicate and rank.** `list_jobs` shows what's already tracked. The tracker
   also collapses repeated URLs. Rank by fit with the profile: title match,
   location, hard requirements met. Drop roles that need a clearance, degree or
   citizenship the user doesn't have, but mention that you dropped them.

6. **Report.** Give a table with id, company, title, location, ATS and a one-line fit
   note, then ask which ones to apply to. The `apply` skill takes it from there.
