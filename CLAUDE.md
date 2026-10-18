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
  - `never.py`: what the desk never agrees to, ticks or presses for the person (one list, every path);
  - `browser.py` and `formjs.py`: the browser and the form reader; the browser's tabs, menus, dates, uploads and
    blocks are in `browser_tabs.py`, `browser_menus.py`, `browser_dates.py`, `browser_uploads.py`, `browser_blocks.py`;
  - `config.py`: paths, the profile, settings and secrets (and `live_test_identity`);
  - `ats.py`: which job system a link is; `postings.py`: a posting read into a record;
  - `search.py`: employer job searches;
  - `recommend.py`: ranking;
  - `mailbox.py`: emailed codes, and the confirmation and password-reset links;
  - `tracker.py`: the SQLite tracker;
  - `report.py`: problem reports;
  - `history.py`: the desk's run history (`runs.jsonl`, the This week window);
  - `render.py`: a resume or cover letter as a PDF;
  - `doctor.py`: the setup check (`job-apply-doctor`, the `doctor` tool);
  - `updates.py`: whether a newer version is out, for the desk to say;
  - `fixtures.py`: the scrubber.
- `data/companies.yaml` is the default employer list (`semiconductor-az`), and
  `data/lists/*.yaml` holds the others (`phoenix-metro`, `las-vegas`). A person's own
  `~/.job-apply/companies.yaml` names lists under `lists:`.
- `data/question_bank.yaml`: the common questions the desk's "Common questions" card asks once (answers go to the
  person's own `answers.yaml` with the entry's pattern). `scripts/question_bank.py <run's artifacts>` lists the
  questions a nightly asked that no entry covers yet. Only questions whose answer is the person's own everywhere.
- `data/desk_handled.yaml`: the employers where the desk goes all the way (the live check's test applicant reached
  the review page there), for the desk's "Only where the desk goes all the way" switch. Refresh it before a release
  from the last full nightly (`scripts/nightly_summary.py --handled`), plus employers a probe since has taken there.
- `skills/`: the plugin's skills (setup, apply, find-jobs, desk, track-responses,
  interview-prep). `templates/`: profile and companies templates.

## Running things (from `plugins/job-apply/server`)

- Tests: `JOB_APPLY_CHROMIUM_PATH=/opt/pw-browsers/chromium uv run pytest -q -n 8 tests`.
  - 8 workers on a 4-core machine (CI's too): the suite waits on pages far more than it computes.
  - The full suite takes a few minutes per part on CI, and 15 to 20 on a 4-core session.
    Locally, run the affected test files, ruff and mypy; the PR's required checks run the full suite.
    Add `-m "not slow"` for a quicker run: it leaves out the tests marked `@pytest.mark.slow`.
  - `uv run python scripts/dev_test.py` runs the tests that import what changed since the branch left
    `origin/main` (`--deep`: and what imports that; `--all`: everything; `--list`: only say which), with
    `-n 8 --ff`; any other argument goes to pytest (`--lf`, `-x`, `-k`).
  - CI runs it in six parts on separate machines: `JOB_APPLY_TEST_SHARD=1/6` (to `6/6`) runs one part.
    Each part starts its slow tests first (`JOB_APPLY_SLOW_FIRST=1` with `--maxschedchunk=1`), so none
    is left running alone at the end. A test that takes 20 s or more in a part's `--durations` list
    gets the mark.
  - CI also runs it on Windows, in four parts (`windows (Python 3.13, part n/4)`), the owner's platform. Those checks
    aren't required (a failure doesn't block the merge), so look at them yourself. Windows reads and
    writes text as cp1252 unless told otherwise: give every text file `encoding="utf-8"`, tests too.
    Build paths with pathlib (`as_posix()` for a relative path shown, `Path.as_uri()` for `file://`).
  - The `weekly` workflow (Mondays, or by hand) audits the lock's packages (pip-audit: a finding fails it) and
    lists the week's failed tests in its summary, flakes first (`scripts/flake_rate.py`: a test that failed on a
    commit whose tests passed in another run or attempt).
- Live checks against real employer sites: `uv run python scripts/live_smoke.py`.
  - Add `--pipeline --fake-passwords --parallel 4` to drive the Job Desk pipeline. The full run takes about 6 minutes.
  - Add `--lists phoenix-metro` to check another list, and `--role hr` (or `--role finance`) to look
    for HR (or finance) jobs as an applicant with that background (the default is technician jobs).
  - It forces a fake profile and `JOB_APPLY_NEVER_SUBMIT=1`, so nothing can be submitted. The fake profile has
    `accept_cookies` on, so a banner with no way to decline (Texas Instruments') is accepted (the owner asked, Oct 13).
  - Each application goes as far as the rules let it, for the most to fix from (the owner, Oct 13): 5 minutes a round,
    up to 5 rounds of questions answered, and one Resume pressed on a stop. Keep it so: what the rules leave to the
    person (bot checks, CAPTCHAs, submitting) still stops it.
    The fake profile has a test resume made for the run. With `LIVE_TEST_EMAIL` set, the employers
    that refuse its example.com address (`live_identity.INBOX_AT`: Onto Innovation, Robert Half) get
    the test inbox's address instead (masked; still no accounts, no mail read: the owner's call, Oct 11).
  - `--pipeline --test-identity` applies as the test identity instead ("Jobdesk Test", with an inbox
    of its own) at the employers in `scripts/test_identity_employers.yaml`: it makes their accounts
    and reads their emailed codes, and still never submits. It needs `LIVE_TEST_EMAIL`,
    `LIVE_TEST_EMAIL_PASSWORD` and `LIVE_TEST_SITE_PASSWORD` in the environment (setting them up:
    `scripts/TEST_IDENTITY.md`), refuses to start without them, and masks them in all it prints
    and writes. The nightly's `accounts` check runs it once the repository has them as secrets, with
    `--everywhere`: at every employer on the lists in `live_identity.LIST_ROLES` (about 100), a code-sending
    system's employers in one `--parallel` group (one inbox).
  - Each check's machine audits what it wrote (`scripts/live_audit.py`): a consent the desk never gives (a newsletter,
    job alerts, a talent community, marketing, texts or calls, arbitration, at-will, a waiver), a submit, or the test
    identity's secrets, an emailed code or a security answer in a file fail that job with an `::error::` line each.
    Its values check reads each review page reached against the profile the check applied with (a job or school
    from or to a date the profile doesn't give, a degree of a level it doesn't have, another role's employer): a
    `::warning::` line each, never a failure; `nightly_summary.py --artifacts <folder>` prints them too. The pages the
    live check saves carry each box's value (a React form's boxes hold theirs as properties), never a password's.
    On the search check's machine, one `::warning::` line a job system whose postings came back without a title,
    a place or their text (the search check reads each as the desk reads a pasted link).
  - Compare a run with the previous one per employer. A changed outcome is the signal. The nightly's changes comment opens with
    **Went backwards** when an employer the last run took to its review page no longer gets there (the regression alarm).
  - The nightly's compare job (`scripts/live_compare.py`) writes two issues: "Nightly live check" (label `live-check`,
    #54: the last run's tables, and a comment when an outcome changed) and "Nightly live trends" (label `live-trends`,
    never `live-check`: the last 60 runs' counts per check and median seconds, each employer's last 20 runs as letters,
    and the employers that flip, 3 changes or more in their last 10 runs). A check that didn't finish adds nothing to
    the history.
  - Run live checks from a worktree of their own (`git worktree add ../Jobs-live origin/main`): a branch
    switch or cherry-pick in the main checkout changes the scripts under a running check.
- The nightly's results: `uv run python scripts/nightly_summary.py` (counts per check, and the employers still back:
  at the review page in an earlier run, not in the last, with the date each last got there; and `desk_handled.yaml`'s
  drift from the last run's accounts check), `--check accounts`
  (one line per employer), `--changes` (the last run's changes, sorted: "look at" first, likely the desk's; then a site's or the
  rules'; then the better), `--tables` (Where It Stands' employer
  tables), `--artifacts <zip or folder> --companies "A,B"` (each employer's rounds, questions with their
  choices, and the page where it stopped). Use it instead of reading the issue's body or a run's logs.
  `--trend [N]` (from the trend issue: the last N runs, 14 by default, newest first: each check's reached/total and
  median seconds, then the employers that flip; `--trend-file` reads a saved copy). `--handled` leaves out the employers
  that flip in the accounts or pipeline check, one stderr line each (without the trend issue, a warning, and as before).
  A run's artifact downloads with the GitHub tools' artifact link and curl.
  `--timing <zip or folder>` gives each job system's time (middle, 90%) and the slowest employers; with
  `--against <an earlier run's>` (a week before: artifacts are kept 7 days), a system twice as slow says so.
  `--import-fixtures <zip or folder> [--companies "A,B"] [--replace]` copies the scrubbed pages a run
  stopped on into `tests/fixtures/live` (one already there stays unless replaced), so the offline tests
  check them; look each over before committing it (the repo is public). Each page comes with its
  `<name>.decision.json` (what the desk decided there: the page's kind, the button it would press, the
  CAPTCHA, security-question, account and job-search reads, each box's filled/asked/skipped and rule), and
  `test_live_fixtures.py::test_live_fixture_decides_the_same` decides it again offline, naming the page and the
  key that changed. `--replace` is how an intended change is accepted (a run's new capture of the page); for a
  page no run has captured again, `JOB_APPLY_REDECIDE=1` on that test writes its decisions again (look at the diff).
- A probe of a few employers on GitHub (where the test identity's secrets are): run the live-nightly
  workflow by hand with `companies` (and `checks`, e.g. `accounts`). It runs only those, in a few minutes,
  and isn't compared or written to the issue: read its artifacts with `nightly_summary.py --artifacts`.
- The weekly probes (`scripts/live_probes.py`: the live-nightly workflow on Sundays, or by hand with `checks: probes`),
  as the test identity: the password reset at two Workday tenants (pass 1 saves a password derived from the real one,
  which the site refuses and the desk resets it to; pass 2 resets it back) and a tailored resume in the job's folder
  (the review page must show it). Read them with `nightly_summary.py --probes <zip or folder>`.
  - A failed reset pass 2 may leave a tenant on the derived password; the nightly's own reset puts it back (KLA, Oct 18).
- Never wait on the nightly's schedule: GitHub starts it hours late. After each release, run the live-nightly
  workflow by hand with no inputs (a full run, about 15 minutes; it's compared and written to the issue).
  The workflow queues runs, never two at once (one inbox). A routine dispatches the daily run instead of the
  schedule; a scheduled run that comes within 12 hours of a full run skips itself.
- Plugin manifests: `claude plugin validate --strict .`, plus the same for each plugin folder.
- The plugin as a person installs it: `uv run --no-project python scripts/mcp_handshake.py --install --doctor`
  (CI's `install and start` jobs): installs it with the claude CLI in a temporary home, starts its MCP server as
  `.mcp.json` says and lists its tools, then runs the doctor. Without `--install` it checks this checkout's folder.

## Development practices

The dev-kit's practices (`plugins/dev-kit/skills/practices/SKILL.md`) apply here: a failing test first for every
fix, tests in parallel, CI with required checks and auto-merge, a review pass, nothing personal in the repo. Where
this file says otherwise, it wins.

## Shipping a change

1. Work on the session's designated branch. Every fix gets a test that fails on the old
   code: prove it by stashing `src/`.
2. Bump `plugins/<plugin>/.claude-plugin/plugin.json`'s `version` for every change people
   would notice. Without a bump, their app doesn't update. If the last full nightly is newer than
   `data/desk_handled.yaml`'s `as_of`, refresh it (`scripts/nightly_summary.py --handled`) in the same PR.
3. Open a PR to `main` and fix what a review finds: a read-only review (an agent told not to edit) for a
   change to how the desk fills, clicks or decides; none for tests, docs or the live check's scripts.
   Two or three related fixes go in one version and one PR.
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
  or Ok, a required box or a lone "I agree": the owner's call, Oct 10), and picks an application's
  attestation that its information is true (or consent to the background check that comes with
  applying), logging each; with it off they're the person's. Never a newsletter, job alerts, a
  talent community, marketing, texts or automated calls, nor a legal waiver (arbitration, a jury
  trial), and cookie banners keep their own rule. Any other dialog over the form stops the desk
  for the person.
- **Accounts:** with `settings.manage_accounts` (on unless the person turns it off, as the owner
  chose; setup tells each person; never in practice mode, except the live check's test identity)
  the desk creates an account with that system's saved password, ticking only the site's terms
  (never a newsletter), and resets a saved password the site refuses, through the site's own
  "Forgot password" and its emailed link. An account there from before that still needs verifying
  (the site refuses the sign-in for it: Workday's "Verify your account before you sign in") isn't
  reset: with the inbox watched the desk presses the site's own "Resend Account Verification",
  once a job, and opens the new email's link, as it does a new account's. A CAPTCHA on the way stays the person's; security
  questions are the person's to answer, in the browser or on the desk's card (for that form alone,
  never remembered, logged or saved with a page), and the desk never makes up an answer. With it
  off, it never creates one.
  - From the profile, it also ticks a "new candidate, not a current employee" box (Banner's) where
    the profile says where the person works and it isn't there.
  - A consent to be considered for other open positions too (Northrop's "Contact Consent") is given
    only when the site won't make the account without it (marked required, or refused until it's
    given: the owner's call, Oct 10); never when its words also sign the person up for job alerts,
    a newsletter, a talent community or marketing.
  - The live check's test identity (the owner's call, Oct 10) is the one exception to "never in
    practice mode", for accounts and notices alike: a clearly fake applicant ("Jobdesk Test")
    with its own inbox, at the employers in `test_identity_employers.yaml`, or (the nightly's accounts check, `--everywhere`:
    the owner's call, Oct 11) at every employer on the lists in `live_identity.LIST_ROLES`, and which never submits.
    It also takes an employer's assessment with random answers ("Take Assessment": the owner's call, Oct 13), pressing the assessment's own
    Submit or Finish only off the application's site; on the person's desk an assessment stays theirs. And it agrees
    to the notices a person's desk leaves to them (a waiver of rights in Freeport-McMoRan's data privacy statement: the
    owner's call, Oct 13), by an agree button only, never a Submit (`_test_identity_agrees`). With `--security-answers`
    (the nightly's accounts check: the owner's call, Oct 14) the live check answers an account form's security questions
    on the desk's card for it, as a person would there: a question from the list, an answer made from its site password
    (`live_identity.security_answer`), so it goes on into the application. It's on
    only with `JOB_APPLY_LIVE_TEST_IDENTITY=1` and `JOB_APPLY_NEVER_SUBMIT=1` set and the
    profile's email being `LIVE_TEST_EMAIL` (or that inbox's own `+tag` address: the `--plus` probe;
    `config.live_test_identity`), so a person's own profile never turns it on; Submit stays refused in it.
- **Passwords:**
  - a saved password goes only onto its own system's sites;
  - the inbox app password only reads sign-up codes (and, with `manage_accounts`, the reset
    email a site was just asked for, from that site);
  - never ask for a password in chat (the desk page holds them).
- **Answers come only from the profile or the person.** Never overstate education or
  experience: coursework is not a degree.
