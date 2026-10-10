# Jobs: a Claude Code plugin that applies to jobs for you

`job-apply` is a Claude Code plugin. Paste job links (from LinkedIn, Indeed or a
company careers site) and Claude does the rest:

1. **Reads the posting.** It pulls the title, company, location, description,
   and the applicant tracking system (ATS) that runs the application.
2. **Checks fit** against your profile and flags hard blockers, such as an
   export-control or citizenship requirement, a degree, or the amount of travel.
3. **Opens the application in a real Chrome window** on your machine, which you
   can watch, and fills it in from your profile. That covers contact details,
   address, work authorization, sponsorship, voluntary EEO answers, the resume
   upload, and answers you've saved before.
4. **Asks you** about anything your profile doesn't answer. It drafts free-text
   answers from your resume for you to approve.
5. **Stops at the review page** and submits when you say so.
6. **Tracks everything** in a local tracker: saved, in progress, applied,
   interviewing and so on. With your permission it can also check your Gmail for
   replies and update the tracker.

It also fills Workday's "My Experience" section, with one block per job and
degree in your profile, and it can write a tailored resume or cover letter for
each job as a PDF that it uploads in place of your default. In the Job Desk, turn
on "Tailor my resume for each job" and say "tailor my resumes" to Claude: each
picked job waits until Claude has written its resume, then carries on.

It works with any careers site, plus specific handling for the systems common
at semiconductor companies: Workday (Applied Materials, KLA, Intel, Microchip,
NXP, TEL, ADI, Onto Innovation, Axcelis, Thermo Fisher), Eightfold (Lam Research, Micron, Infineon),
SuccessFactors (Edwards, TSMC Arizona, Amkor, Qorvo), Oracle (onsemi, TI), Greenhouse (ASM),
ApplicantStack (SCREEN), iCIMS (Daifuku), Paycom (Ebara), UKG Pro (Nikon Precision), Infor
CloudSuite (Benchmark) and Lever.
`plugins/job-apply/data/companies.yaml` lists 36 semiconductor employers with
Arizona sites, including equipment makers whose field service engineers work at
Arizona fabs, and the suppliers whose technicians run the fabs' gases and chemicals.

Looking for other work, or somewhere else? Copy
[`templates/companies.example.yaml`](plugins/job-apply/templates/companies.example.yaml)
to `~/.job-apply/companies.yaml`. Under `lists:`, name the plugin's lists to search:
- `semiconductor-az`, the list above;
- `phoenix-metro`, large Phoenix-area employers in health care, education, finance,
  utilities, aerospace, retail and staffing.

Add employers of your own under `companies:`, in the same shape. Find jobs and the Job
Desk then search those instead of the default list. Set your target titles
(`preferences.titles`, such as "HR Generalist" or "Recruiter") and places
(`preferences.locations`) in `profile.yaml` to match.

## How submitting works

| Where the application is | What Claude does |
|---|---|
| Company sites (Workday, Greenhouse, Lever, Eightfold, SuccessFactors, …) | Fills everything and shows you the review page. You say "submit" and Claude submits. You can opt in to **auto** mode for chosen ATSs, and then Claude submits complete applications without asking. |
| **LinkedIn Easy Apply** and **Indeed Apply** | Fills everything, then **you click Submit** in the browser. Both sites' terms prohibit automated submission, and they restrict accounts that do it, so the plugin never presses that button. |

Claude never invents experience, credentials or work-authorization answers.
Anything that isn't in your profile or resume is a question for you. CAPTCHAs,
email verification codes and sign-ins also stay with you. Passwords can be typed
from a local secrets file without passing through the chat.

## Install

You need:

- [Claude Code](https://claude.com/claude-code)
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/). It runs the
  plugin's Python server and installs its dependencies on first launch.
- Google Chrome, or Microsoft Edge without it (every Windows computer has Edge). If
  neither starts, run the `playwright install chromium` command that `setup_status`
  shows.

Then, in Claude Code:

```
/plugin marketplace add sebob2491/Jobs
/plugin install job-apply@sebob-jobs
```

Restart Claude Code and run:

```
/job-apply:setup
```

Setup asks for your resume, fills in `~/.job-apply/profile.yaml`, and asks the
questions a resume can't answer: work authorization, sponsorship, US-person
status, relocation, travel, shifts, salary and EEO preferences. It then opens the
browser so you can sign in to LinkedIn and Indeed once. The sign-ins persist.

## Use

```
Apply to these:
https://www.linkedin.com/jobs/view/<job id>/
https://<company>.wd1.myworkdayjobs.com/External/job/<location>/<title>_<req id>
https://job-boards.greenhouse.io/<company>/jobs/<id>
```

```
/job-apply:find-jobs field service engineer roles in Chandler and Phoenix
```

```
/job-apply:track-responses        (did anyone reply? updates the tracker from Gmail)
```

```
What have I applied to this month?   ·   Mark the Lam job as interviewing   ·   Export my tracker to CSV
```

### The Job Desk: recommended jobs and one Apply button

```
/job-apply:desk
```

opens a page on your computer (served by the plugin on `127.0.0.1`):

1. **Find jobs** searches every employer's career site for your target titles in
   your area. Each opening gets a fit score, with the reasons and concerns under it:
   "title matches", "posted this week", "requires a Bachelor's (you have a high
   school diploma)", "senior-level role", "requires an active clearance". Openings that
   weren't there on your last visit are tagged **New**. Ones listed only as "US -
   Multiple Locations", "Remote - US (Field Based)" and the like are kept and marked
   "check the posting": you may be able to do them from your area.
2. Tick the ones you want, or press **Select recommended**, then **Apply to
   selected**. **Select recommended** leaves out openings whose posting asks for a
   degree you don't have (unless it says "or equivalent experience"), and ones the
   desk couldn't open to check, or whose posting puts the job in another state.
   They're under **All**, with the reason, so you can still tick them yourself.
   The desk works through them one at a time in the browser window. It
   clicks into each application, fills every page, adds Workday's work and
   education blocks, and stops at each review page.
3. **Needs you** lists what it can't do alone:
   - Questions your profile doesn't answer. You answer them on the page, and they're
     remembered for later applications unless you untick that. A remembered answer
     fills the same question, worded the same way. An answer about one employer
     ("Why do you want to work here?", "Have you worked for us before?") isn't reused
     for another.
   - A dialog open over a form. You answer it in the browser and press **Resume**, and the
     desk fills the form in behind it. It agrees for you to an employer's notice about AI
     screening of your application (Eightfold's employers show one as your resume goes up),
     and picks an application's attestation that its information is true and complete, or
     your consent to the background check that comes with applying, noting each in the job's
     log. To answer those yourself, set `accept_notices: false` under `settings:` in your
     profile. Practice mode never agrees to them.
   - Sign-ins, bot checks, CAPTCHAs and emailed codes. You deal with those in the
     browser window, and the desk carries on by itself. A link a site emails you to
     confirm your address opens in your usual browser, so reload that job's tab in the
     desk's window once you've opened it. The other jobs wait while you work in that
     tab. If nothing happens in it for 5 minutes, they go ahead without it, so you can
     leave a batch running and come back. Finish each waiting job in its tab later, and
     the desk picks it up again by itself.
   - Press **Alert me** in the desk's header to get a desktop notification whenever a
     job needs you or is ready to submit, so you can leave the desk working in the
     background.
4. Press **Submit** on each finished application, or turn on **Submit for me**, so
   that every application that needs nothing from you is sent. LinkedIn and Indeed
   are always yours to submit.

Each employer has its own account on its job site. In the desk's **Site passwords**
card, save one password for a job system: Workday (13 of the employers), SuccessFactors
(Edwards, Qorvo, Amkor), iCIMS (Daifuku), ApplicantStack (SCREEN), UKG Pro (Nikon
Precision) or Infor (Benchmark). The desk then creates your account at each of that system's
employers (your email, the password, and your name and country from the profile; it ticks
their terms, never a newsletter, and presses the button; a picture code stays yours) and
signs you in after that (pressing the sign-in form's own button, even one that reads
"Submit"). When the password doesn't sign in at an employer, the desk tries it once, then,
with the email app password below saved, resets that employer's password to the saved one
through the site's own "Forgot password" email, which it reads itself. When the site says it
has no account for your email, or no reset email comes in 3 minutes (most likely your first
application there; the other jobs carry on meanwhile), it opens that employer's Create
Account form ("Create an account", "Register", "Don't have an account yet?") and creates the
account. Without the email app password it tries Create Account first, and asks you to
open the reset email where the site says the email already has an account. To do these steps yourself, set
`manage_accounts: false` under `settings:` in your profile: the desk then fills in Create
Account for you to finish. Practice mode never creates an account. On Qorvo's one-page application
it puts the password in both boxes. The password goes from the page straight into
`~/.job-apply/secrets.yaml` on your computer, and is only ever typed into that system's
own addresses (for Workday, `*.myworkdayjobs.com` and `*.myworkday.com`). Claude never sees
it, and the desk only reports whether one is saved.

The same card takes an **email app password** for the address in your profile (for
Gmail, make one at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords);
it needs 2-Step Verification; Yahoo, iCloud and AOL addresses work the same way, but
Outlook.com and Hotmail no longer accept app passwords). With it saved, a job waiting on
an emailed code or confirmation link gets it from your inbox:
- the desk types the code in and presses Verify, Confirm, Continue or Next (never
  Submit), or opens the link in its own browser and reloads the job's tab;
- it reads only mail from that job's site or its job system, sent after the job began
  waiting, for up to 15 minutes;
- it opens the inbox read-only, so nothing is marked read, moved or deleted.

If the app password is turned down, the desk page says so and stops trying until you
save it again (a new one, or the same one if the mail service was only having trouble).
A link is only opened when its address is the job's own site. The app password is only
ever used to read those emails, and the password-reset email a site was just asked for:
Claude can't have it typed into any page.

The desk works in each job's own browser tab and follows only the tabs an application
opens itself, so a tab you open (to check your email, say) is left alone. Once a job has
gone in, its tab stays open for you to see the confirmation while it's among the three newest
that did; older ones are closed, so a long queue doesn't leave dozens of tabs open. A job
waiting on you, or that failed, keeps its tab. Jobs already marked applied are never queued
again. If Submit doesn't bring up a confirmation, the job
waits in **Needs you** with **I submitted it** rather than being counted as sent, and
**Submit for me** never presses that job's Submit again (even after a restart): picked
again, it stops at the review page for you. A job-alerts or newsletter box with its own
Submit (in a site's footer, say) is never taken for the application's.

To add jobs from anywhere else, paste their links (LinkedIn, Indeed or any company
site) into the box above the list. The desk reads each posting, scores it and ticks it
for Apply. Jobs Claude saves from Indeed or LinkedIn show up in the same list. Without
Claude Code, run `uv run --project <plugin>/server job-apply-desk`.

**Bot checks.** The desk never tries to get around Cloudflare, CAPTCHAs or other bot
checks: no disguised browser, no CAPTCHA-solving services. When a site shows one, it
pauses that job and brings its tab to the front for you. The automation browser keeps
its own profile, so once you've passed a site's check, that site usually lets it
through for a while. TSMC's careers site turns automated browsers away entirely, so
apply there in your everyday browser, or through the same posting on Indeed.

## Where your data lives

Everything personal stays on your machine in `~/.job-apply/`. Set
`JOB_APPLY_HOME` to put it somewhere else.

| Path | Contents |
|---|---|
| `profile.yaml` | Your answers. Edit it any time ([template](plugins/job-apply/templates/profile.example.yaml)). |
| `resume.pdf` | Default resume |
| `secrets.yaml` | Optional career-site passwords, such as `workday_password`. Save one from the Job Desk, or write the file yourself and run `chmod 600` on it. A password named for a job system goes only onto that system's sites; any other only onto the site it's named for (`acme_password` on an address containing `acme`). |
| `tracker.db` | Application tracker (SQLite). `export_jobs_csv` writes a spreadsheet. |
| `answers.yaml` | Answers you gave in the Job Desk, reused on later applications. Edit or delete entries freely. |
| `companies.yaml` | Optional: which of the plugin's employer lists to search (`lists:`) and employers of your own, in place of the default list. |
| `recommendations.json`, `desk.json` | The Job Desk's last search, and whether "Submit for me" is on |
| `browser/` | The automation browser's profile, which keeps your sign-ins |
| `applications/<id>-<company>-<title>/` | Tailored resume and cover letter, screenshots, submission record, `debug/` snapshots |

Settings in `profile.yaml`:

| Setting | Default | Effect |
|---|---|---|
| `submit_mode` | `review` | Set to `auto` to let Claude submit complete applications without asking, for the ATSs in `auto_submit_ats`. `dry_run` fills everything and never submits, which is good for a first practice run. |
| `email_codes` | `false` | Claude may read the sign-in codes and verification links that career sites email you (needs the Gmail connector) |
| `email_tracking` | `false` | Claude may search your email for replies to your applications (needs the Gmail connector) |
| `accept_notices` | `true` | The Job Desk agrees for you to an employer's notice about AI screening of your application, and picks an application's attestation that its information is true (or your consent to its background check), noting each in the job's log. Never in practice mode. `false` leaves them to you. |

## What's inside

```
.claude-plugin/marketplace.json      marketplace entry (sebob-jobs)
plugins/job-apply/
  .claude-plugin/plugin.json
  .mcp.json                          starts the MCP server with uv
  skills/
    setup/        build the profile from your resume, sign in to sites
    apply/        the application workflow + per-ATS notes (references/ats-notes.md)
    find-jobs/    search Indeed (via the Indeed connector) and company career sites
    track-responses/  read replies from Gmail and update the tracker
    interview-prep/   research, likely questions and STAR stories for an interview
    desk/         open the Job Desk page (recommended jobs, one-button applying)
  data/companies.yaml                semiconductor employers in Arizona, careers URL + ATS
  templates/profile.example.yaml
  server/                            Python MCP server (Playwright browser automation)
```

### MCP tools

| Tool | Purpose |
|---|---|
| `setup_status`, `get_profile` | Check what's missing and read the profile |
| `search_company_jobs` | Search employers' own job boards (Workday, Greenhouse, Lever, Eightfold, SmartRecruiters, Oracle, and ASML's site) by keyword and location; sites that refuse direct requests are read through a background browser tab |
| `ingest_job`, `add_job` | Save a posting. Parses Workday, Greenhouse, Lever and SmartRecruiters APIs, schema.org JobPosting data, or page text. |
| `list_jobs`, `get_job`, `update_job`, `export_jobs_csv` | Application tracker |
| `open_application`, `click`, `tabs` | Navigate the browser. `click` refuses final submit buttons. |
| `inspect_form` | Every field on the page, including iframes and custom dropdowns: label, type, options, required |
| `autofill` | Fill everything the profile answers and return the fields that still need a decision |
| `add_entries` | Click "Add" until there's one Work Experience / Education block per profile entry |
| `fill_form`, `fill_secret` | Fill specific fields; type a stored password without exposing it |
| `screenshot`, `page_text` | See the page |
| `debug_snapshot` | Save the page (HTML of every frame, a screenshot, the extracted fields). This also happens automatically when a field fails. |
| `render_document` | Markdown resume or cover letter → PDF in the job folder |
| `submit_application` | Final submit, enforcing the rules above, and record the result |
| `log_email`, `logged_emails` | Record employer replies; each email counts once and statuses only move forward |
| `open_job_desk` | Open the Job Desk page: recommended openings ranked against the profile, one-button applying |

## Development

```
cd plugins/job-apply/server
uv sync --extra dev
uv run playwright install chromium
uv run pytest -n auto
```

`-n auto` runs the tests across every CPU core (pytest-xdist); each test has its own
temporary `JOB_APPLY_HOME` and browser, so they don't get in each other's way. Plain
`uv run pytest` runs them one at a time.

`uv run ruff check .` (lint for bugs: unused or undefined names, bugbear's patterns; it never
reformats) and `uv run mypy src/job_apply` (type check) run in CI ahead of the tests; run them
before opening a PR.

The tests cover ATS detection, posting parsing, the profile-to-field matching
rules, the tracker and email log, PDF rendering, fixture redaction, and
end-to-end browser runs (headless Chromium) against mock forms: a generic form,
Workday-style dropdowns, search pickers and My Experience blocks, an embedded
iframe form, the submit guard, and the LinkedIn/Indeed submit rule. GitHub
Actions runs them on Python 3.10 and 3.13 and validates the plugin manifests.

### Testing against real career sites

The `live-smoke` workflow (Actions tab → live-smoke → Run workflow) runs
`scripts/live_smoke.py` on GitHub's runners, which can reach the job sites. For
every company in `companies.yaml` it:
- searches for Arizona field-service and equipment roles;
- reads one real posting;
- opens its application in headless Chromium;
- clicks through "Apply" and "Apply Manually";
- lists the form's fields and autofills them with a fake test profile.

It is read-only. Submitting is disabled, nothing is uploaded, it never signs in or
creates accounts, and it never contacts LinkedIn or Indeed. The job summary shows
a table, and the log has one `LIVE_RESULT` line of JSON per company. Tick
"Commit captured pages" to save the pages as regression fixtures.

To run it yourself, add `--pipeline --fake-passwords` to drive the Job Desk's
one-button pipeline instead. It checks the default semiconductor list; add
`--lists phoenix-metro` (or `--lists phoenix-metro,semiconductor-az`) to check other lists. Add `--parallel 4` to check four employers at a time,
each in a browser of its own; every site is still visited once. A full run then
takes about 6 minutes instead of 22.

**Nightly live check.** The `live-nightly` workflow runs every night at 3:17 AM
Arizona time (or from the Actions tab), four employers at a time: the pipeline check on
the semiconductor list (technician jobs), the pipeline check on the phoenix-metro list
for HR jobs (`--role hr`, an applicant with an HR background), and the search check on
the phoenix-metro list. Each has its own table in the issue.
`scripts/live_compare.py` boils each employer down to one result that holds steady
from night to night: where the pipeline ended (`ready`, `needs_you sign_in`,
`needs_you stuck`, ...), or whether the search works (`works`, `error`, or `no search`).
How many openings a search finds changes daily and is never compared. The issue titled
"Nightly live check" (label `live-check`) keeps the last results in its body, and gets
a comment, which notifies you, only when an employer's result changes or an employer
joins or leaves a list. It's reopened if it was closed. A check that doesn't finish
fails the run, and the employers it didn't reach keep their last result. Each run's
reports and logs are kept as an artifact for 7 days. GitHub pauses scheduled workflows
in a repository with no activity for 60 days.

### Reporting a problem

When the Job Desk gets a job wrong, press **Report a problem** on that job. It writes a
report to `~/.job-apply/reports/<job>-<time>/`:
- what the desk did, step by step;
- each page it saved (the last pages it stopped on for you, and any page a fill failed on),
  with every field's label and whether it was filled (never what was in it);
- `report.zip`, which also holds the saved pages.

The profile's personal details, plus your city, schools and employers, are replaced by
`REDACTED`. Web addresses lose their session codes, and screenshots are never included.
The desk then shows you the whole report, with a link that opens a new issue on this
repository with the report's text filled in. Nothing is sent unless you follow the link and
file the issue. GitHub issues are public: anyone can read one, and see which job you
reported, so read it over first. Tick **Don't say which job it was** in the report to make it
again with only the job system (Workday, iCIMS, ...) and where it stopped: the employer, the
job's title, its addresses and its requisition number are left out of the issue and the saved
pages. The saved pages in `report.zip` aren't in the issue, because a filled-in form can
still show your answers; they stay on your computer. Claude can make the same report with
`report_problem` (`anonymous=True` for one that doesn't say which job it was).

The desk also takes a note by itself, with nothing pressed, each time a job stops on
something it most likely got wrong: it's stuck, at a sign-in, its Submit didn't go through
or showed no confirmation, or an answer didn't go in. Bot checks, CAPTCHAs, emailed codes
and questions your profile doesn't answer are yours, so they aren't noted. **Notes (N)** in
the desk's header shows them all as one issue: **File on GitHub** opens it, **Copy all**
copies the whole text (for when it's too long for the link), and **Clear** removes them once
they're filed. A note holds less than a report: none of your answers, and each employer said
by its job system ("a Workday employer") rather than named, since one issue holds many jobs
(the addresses of the pages it stopped on still show the site). The newest 50 are kept in
`~/.job-apply/notes/`.

### Turning a failure into a test

When a field won't fill on a real site, the plugin saves a snapshot to
`~/.job-apply/applications/<job>/debug/<time>/` (and the Job Desk keeps the last three pages
a job stopped on for you, in `debug/<time>-stop/`). To turn it into a regression
test, run:

```
cd plugins/job-apply/server
uv run python -m job_apply.fixtures ~/.job-apply/applications/<job>/debug/<time> workday-my-information
```

This writes `tests/fixtures/live/<name>.html`, with scripts removed and your
profile details replaced by `REDACTED`, plus `<name>.expect.json`, which lists the
fields the extractor must keep finding. Fix the extractor or the matching rules,
trim the expectations to fields you've checked, look the HTML over for any other
personal data, and commit. `tests/test_live_fixtures.py` picks it up
automatically.

## dev-kit: development practices for any project

This marketplace also carries **dev-kit**, a small plugin for working on code with
Claude Code in any project. It isn't specific to job applications. It holds:
- a practices skill Claude follows while it works: tests that run in parallel, CI with
  required checks and auto-merge, a review pass on every PR, a test for every fix that
  fails on the old code, fewer themed PRs, version bumps, parallel agents, handoff notes;
- `/dev-kit:check`, which audits a repo against those practices and sets up what's
  missing with your OK, including a `CLAUDE.md`, so cloud sessions on that repo follow them too;
- a one-line reminder at the start of each session to run the check on projects that
  haven't had one in 30 days.

Install it like job-apply: `/plugin install dev-kit@sebob-jobs` in Claude Code, or
`claude plugin install dev-kit@sebob-jobs` in a terminal.

## Limitations

- So far the tests run only against local mock forms. Real ATS pages change often,
  so expect Claude to sometimes fall back to `inspect_form` and `screenshot` and
  work field by field. The debug snapshots exist to turn those cases into fixes
  quickly.
- Many company ATSs (Workday, SuccessFactors) need an account per company. Creating
  the account is up to you. So is the emailed verification code, unless you save an
  email app password on the Job Desk (or turn on `email_codes` when working with Claude).
- The first launch of the MCP server installs its Python dependencies, which takes
  a minute. If `/mcp` shows `job-apply` as failed right after install, reconnect it.
