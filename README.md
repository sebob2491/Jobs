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
   interviewing and so on.

It works with any careers site, plus specific handling for the systems common
at semiconductor companies: Workday (Applied Materials, KLA, Intel, Microchip,
NXP, TEL, ADI), Eightfold (Lam Research, Micron, Infineon), SuccessFactors (TSMC
Arizona, Amkor, Qorvo), Oracle (onsemi, TI), Greenhouse (ASM) and Lever.
`plugins/job-apply/data/companies.yaml` lists 21 semiconductor employers with
Arizona sites.

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
https://www.asml.com/en/careers/find-your-job/field-service-engineer-euv-j00329630
https://www.linkedin.com/jobs/view/4402341490/
https://www.linkedin.com/jobs/view/4402347426/
```

```
/job-apply:find-jobs field service engineer roles in Chandler and Phoenix
```

```
What have I applied to this month?   ·   Mark the Lam job as interviewing   ·   Export my tracker to CSV
```

## Where your data lives

Everything personal stays on your machine in `~/.job-apply/`. Set
`JOB_APPLY_HOME` to put it somewhere else.

| Path | Contents |
|---|---|
| `profile.yaml` | Your answers. Edit it any time ([template](plugins/job-apply/templates/profile.example.yaml)). |
| `resume.pdf` | Default resume |
| `secrets.yaml` | Optional career-site passwords (you write this file; run `chmod 600` on it) |
| `tracker.db` | Application tracker (SQLite). `export_jobs_csv` writes a spreadsheet. |
| `browser/` | The automation browser's profile, which keeps your sign-ins |
| `applications/<id>-<company>-<title>/` | Tailored resume and cover letter, screenshots, submission record |

`settings.submit_mode` in `profile.yaml` switches between `review` (the default)
and `auto`. `settings.auto_submit_ats` lists the ATSs auto mode applies to.

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
  data/companies.yaml                semiconductor employers in Arizona, careers URL + ATS
  templates/profile.example.yaml
  server/                            Python MCP server (Playwright browser automation)
```

### MCP tools

| Tool | Purpose |
|---|---|
| `setup_status`, `get_profile` | Check what's missing and read the profile |
| `ingest_job`, `add_job` | Save a posting. Parses Workday, Greenhouse and Lever APIs, schema.org JobPosting data, or page text. |
| `list_jobs`, `get_job`, `update_job`, `export_jobs_csv` | Application tracker |
| `open_application`, `click`, `tabs` | Navigate the browser. `click` refuses final submit buttons. |
| `inspect_form` | Every field on the page, including iframes and custom dropdowns: label, type, options, required |
| `autofill` | Fill everything the profile answers and return the fields that still need a decision |
| `fill_form`, `fill_secret` | Fill specific fields; type a stored password without exposing it |
| `screenshot`, `page_text` | See the page |
| `submit_application` | Final submit, enforcing the rules above, and record the result |

## Development

```
cd plugins/job-apply/server
uv sync --extra dev
uv run playwright install chromium
uv run pytest
```

The tests cover ATS detection, posting parsing, the profile-to-field matching
rules, the tracker, and end-to-end browser runs (headless Chromium) against mock
forms: a generic form, Workday-style dropdowns and search pickers, an embedded
iframe form, the submit guard, and the LinkedIn/Indeed submit rule.

## Limitations

- The tests run against local mock forms. Real ATS pages change often, so expect
  Claude to sometimes fall back to `inspect_form` and `screenshot` and work field
  by field. That fallback is built into the workflow.
- Many company ATSs (Workday, SuccessFactors) need an account per company and an
  emailed verification code. That step is yours.
- The first launch of the MCP server installs its Python dependencies, which takes
  a minute. If `/mcp` shows `job-apply` as failed right after install, reconnect it.
