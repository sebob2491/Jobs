# Notes for Claude sessions on this repo

This is a Claude Code plugin marketplace (`sebob-jobs`, `.claude-plugin/marketplace.json`)
with two plugins:
- `plugins/job-apply`: finds jobs and fills applications in a real browser (the Job Desk).
- `plugins/dev-kit`: development practices for any project (read its
  `skills/practices/SKILL.md`; they apply here too).

The repo is public: never put anything personal in it (names, addresses, emails,
accounts, a person's education or job search). Pages captured from real sites go through
the scrubber (`python -m job_apply.fixtures`) before they become test fixtures.

## Layout (job-apply)

- `server/src/job_apply/`: the MCP server (`server.py`, Claude's tools) and its parts:
  - `pipeline.py`: the Job Desk's application driver;
  - `desk.py` and `static/desk.html`: the Job Desk page;
  - `autofill.py`: answers from the profile;
  - `browser.py` and `formjs.py`: the browser and the form reader;
  - `search.py`: employer job searches;
  - `recommend.py`: ranking;
  - `mailbox.py`: emailed codes;
  - `tracker.py`: the SQLite tracker;
  - `report.py`: problem reports;
  - `fixtures.py`: the scrubber.
- `data/companies.yaml` is the default employer list (`semiconductor-az`), and
  `data/lists/*.yaml` holds the others (`phoenix-metro`). A person's own
  `~/.job-apply/companies.yaml` names lists under `lists:`.
- `skills/`: the plugin's skills (setup, apply, find-jobs, desk, track-responses,
  interview-prep). `templates/`: profile and companies templates.

## Running things (from `plugins/job-apply/server`)

- Tests: `JOB_APPLY_CHROMIUM_PATH=/opt/pw-browsers/chromium uv run pytest -q -p no:cacheprovider -n auto tests`.
  - The full suite takes about 4 minutes. While iterating, run only the affected test files.
  - CI runs it in two parts on separate machines: `JOB_APPLY_TEST_SHARD=1/2` (or `2/2`) runs one part.
  - CI also runs it on Windows (`windows (Python 3.13, part n/2)`), the owner's platform. Those checks
    aren't required (a failure doesn't block the merge), so look at them yourself. Windows reads and
    writes text as cp1252 unless told otherwise: give every text file `encoding="utf-8"`, tests too.
    Build paths with pathlib (`as_posix()` for a relative path shown, `Path.as_uri()` for `file://`).
- Live checks against real employer sites: `uv run python scripts/live_smoke.py`.
  - Add `--pipeline --fake-passwords --parallel 4` to drive the Job Desk pipeline. The full run takes about 6 minutes.
  - Add `--lists phoenix-metro` to check another list, and `--role hr` (or `--role finance`) to look
    for HR (or finance) jobs as an applicant with that background (the default is technician jobs).
  - It forces a fake profile and `JOB_APPLY_NEVER_SUBMIT=1`, so nothing can be submitted.
  - `--pipeline --test-identity` applies as the test identity instead ("Jobdesk Test", with an inbox
    of its own) at the employers in `scripts/test_identity_employers.yaml`: it makes their accounts
    and reads their emailed codes, and still never submits. It needs `LIVE_TEST_EMAIL`,
    `LIVE_TEST_EMAIL_PASSWORD` and `LIVE_TEST_SITE_PASSWORD` in the environment (setting them up:
    `scripts/TEST_IDENTITY.md`), refuses to start without them, and masks them in all it prints
    and writes. The nightly's `accounts` check runs it once the repository has them as secrets.
  - Compare a run with the previous one per employer. A changed outcome is the signal.
- Plugin manifests: `claude plugin validate --strict .`, plus the same for each plugin folder.

## Shipping a change

1. Work on the session's designated branch. Every fix gets a test that fails on the old
   code: prove it by stashing `src/`.
2. Bump `plugins/<plugin>/.claude-plugin/plugin.json`'s `version` for every change people
   would notice. Without a bump, their app doesn't update.
3. Open a PR to `main`, run the code-review skill on it, and fix what it finds.
4. Turn on auto-merge (squash).
5. `main` has a ruleset requiring the `server (Python 3.10)`, `server (Python 3.13)` and
   `plugin manifests` checks, so the PR merges itself when they pass. There's no need to
   watch it.
6. After the merge, restart the working branch from `main`.
7. Tell the owner the version and how to update:
   `claude plugin marketplace update sebob-jobs`, then
   `claude plugin update job-apply@sebob-jobs`, then restart the app.

## Rules the product keeps (don't weaken them)

- **Submitting:** the Job Desk submits only when the person pressed Submit or turned on
  "Submit for me". LinkedIn and Indeed applications are always submitted by the person.
  Practice mode (`submit_mode: dry_run`, `JOB_APPLY_NEVER_SUBMIT=1`) never sends anything.
- **Bot checks:** never get around them, and never solve CAPTCHAs. The desk pauses for the
  person.
- **Cookie banners** are declined or left to the person, never accepted, unless the person
  turned on `settings.accept_cookies` (off by default): then a banner with no way to decline
  is accepted for them.
- **Notices and attestations:** with `settings.accept_notices` (on unless the person turns it
  off, as the owner chose; never in practice mode, except the live check's test identity: see
  Accounts) the desk agrees to an employer's notice about
  AI screening and to its privacy notice and terms of use (a gate's "I Accept", a dialog's agree
  or Ok, a required box: the owner's call, Oct 10), and picks an application's attestation that
  its information is true (or consent to the background check that comes with applying), logging
  each; with it off they're the person's. Never a newsletter, job alerts, a talent community or
  marketing, and cookie banners keep their own rule. Any other dialog over the form stops the
  desk for the person.
- **Accounts:** with `settings.manage_accounts` (on unless the person turns it off, as the owner
  chose; setup tells each person; never in practice mode, except the live check's test identity)
  the desk creates an account with that system's saved password, ticking only the site's terms
  (never a newsletter), and resets a saved password the site refuses, through the site's own
  "Forgot password" and its emailed link. A CAPTCHA or security question on the way stays the
  person's. With it off, it never creates one.
  - From the profile, it also ticks a "new candidate, not a current employee" box (Banner's) where
    the profile says where the person works and it isn't there.
  - A consent to be considered for other open positions too (Northrop's "Contact Consent") is given
    only when the site won't make the account without it (marked required, or refused until it's
    given: the owner's call, Oct 10); never when its words also sign the person up for job alerts,
    a newsletter, a talent community or marketing.
  - The live check's test identity (the owner's call, Oct 10) is the one exception to "never in
    practice mode", for accounts and notices alike: a clearly fake applicant ("Jobdesk Test")
    with its own inbox, at one or two employers per job system, which never submits. It's on
    only with `JOB_APPLY_LIVE_TEST_IDENTITY=1` and `JOB_APPLY_NEVER_SUBMIT=1` set and the
    profile's email being `LIVE_TEST_EMAIL` (`config.live_test_identity`), so a person's own
    profile never turns it on; Submit stays refused in it.
- **Passwords:**
  - a saved password goes only onto its own system's sites;
  - the inbox app password only reads sign-up codes (and, with `manage_accounts`, the reset
    email a site was just asked for, from that site);
  - never ask for a password in chat (the desk page holds them).
- **Answers come only from the profile or the person.** Never overstate education or
  experience: coursework is not a degree.
