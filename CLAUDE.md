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
- Live checks against real employer sites: `uv run python scripts/live_smoke.py`.
  - Add `--pipeline --fake-passwords --parallel 4` to drive the Job Desk pipeline. The full run takes about 6 minutes.
  - Add `--lists phoenix-metro` to check another list, and `--role hr` to look for HR jobs as an
    applicant with an HR background (the default is technician jobs).
  - It forces a fake profile and `JOB_APPLY_NEVER_SUBMIT=1`, so nothing can be submitted.
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
- **Cookie banners** are declined or left to the person, never accepted.
- **Accounts:** the desk never creates an account.
- **Passwords:**
  - a saved password goes only onto its own system's sites;
  - the inbox app password only reads sign-up codes;
  - never ask for a password in chat (the desk page holds them).
- **Answers come only from the profile or the person.** Never overstate education or
  experience: coursework is not a degree.
