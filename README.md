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
each job as a PDF that it uploads in place of your default.

It works with any careers site, plus specific handling for the systems common
at semiconductor companies: Workday (Applied Materials, KLA, Intel, Microchip,
NXP, TEL, ADI, Onto Innovation, Axcelis, Thermo Fisher), Eightfold (Lam Research, Micron, Infineon),
SuccessFactors (TSMC Arizona, Amkor, Qorvo), Oracle (onsemi, TI), Greenhouse (ASM),
ApplicantStack (SCREEN), iCIMS (Daifuku), Paycom (Ebara) and Lever.
`plugins/job-apply/data/companies.yaml` lists 27 semiconductor employers with
Arizona sites, including equipment makers whose field service engineers work at
Arizona fabs.

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
- Google Chrome. If you don't have it, run the `playwright install chromium`
  command that `setup_status` shows.

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
   "title matches", "posted this week", "requires a Bachelor's (you have an
   Associate's)", "senior-level role", "requires an active clearance". Openings that
   weren't there on your last visit are tagged **New**.
2. Tick the ones you want, or press **Select recommended**, then **Apply to
   selected**. The desk works through them one at a time in the browser window. It
   clicks into each application, fills every page, adds Workday's work and
   education blocks, and stops at each review page.
3. **Needs you** lists what it can't do alone:
   - Questions your profile doesn't answer. You answer them on the page, and they're
     remembered for later applications unless you untick that.
   - Sign-ins, bot checks and emailed codes. You deal with those in the browser
     window, and the desk carries on by itself.
4. Press **Submit** on each finished application, or turn on **Submit for me**, so
   that every application that needs nothing from you is sent. LinkedIn and Indeed
   are always yours to submit.

Each employer on Workday has its own account. Save a password in the desk's
**Workday password** card and it fills in their Create Account forms (you tick their
terms and press the button) and signs you in after that. When it doesn't sign in at an
employer, usually because it's your first application there, the desk tries it once and
then fills in that employer's Create Account form instead. The password goes from the page
straight into `~/.job-apply/secrets.yaml` on your computer, and is only ever typed into
Workday's own addresses (`*.myworkdayjobs.com`, `*.myworkday.com`). Claude never sees it,
and the desk only reports whether one is saved.

The desk works in each job's own browser tab and follows only the tabs an application
opens itself, so a tab you open (to check your email, say) is left alone. Jobs already
marked applied are never queued again. If Submit doesn't bring up a confirmation, the job
waits in **Needs you** with **I submitted it** rather than being counted as sent.

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
| `secrets.yaml` | Optional career-site passwords, such as `workday_password`. Save one from the Job Desk, or write the file yourself and run `chmod 600` on it. |
| `tracker.db` | Application tracker (SQLite). `export_jobs_csv` writes a spreadsheet. |
| `answers.yaml` | Answers you gave in the Job Desk, reused on later applications. Edit or delete entries freely. |
| `recommendations.json`, `desk.json` | The Job Desk's last search, and whether "Submit for me" is on |
| `browser/` | The automation browser's profile, which keeps your sign-ins |
| `applications/<id>-<company>-<title>/` | Tailored resume and cover letter, screenshots, submission record, `debug/` snapshots |

Settings in `profile.yaml`:

| Setting | Default | Effect |
|---|---|---|
| `submit_mode` | `review` | Set to `auto` to let Claude submit complete applications without asking, for the ATSs in `auto_submit_ats`. `dry_run` fills everything and never submits, which is good for a first practice run. |
| `email_codes` | `false` | Claude may read the sign-in codes and verification links that career sites email you (needs the Gmail connector) |
| `email_tracking` | `false` | Claude may search your email for replies to your applications (needs the Gmail connector) |

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
uv run pytest
```

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

### Turning a failure into a test

When a field won't fill on a real site, the plugin saves a snapshot to
`~/.job-apply/applications/<job>/debug/<time>/`. To turn it into a regression
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

## Limitations

- So far the tests run only against local mock forms. Real ATS pages change often,
  so expect Claude to sometimes fall back to `inspect_form` and `screenshot` and
  work field by field. The debug snapshots exist to turn those cases into fixes
  quickly.
- Many company ATSs (Workday, SuccessFactors) need an account per company. Creating
  the account is up to you, and so is the emailed verification code unless you
  turn on `email_codes`.
- The first launch of the MCP server installs its Python dependencies, which takes
  a minute. If `/mcp` shows `job-apply` as failed right after install, reconnect it.
