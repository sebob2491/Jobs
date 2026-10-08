---
name: check
description: Audit the current project against the dev-kit practices (parallel tests, CI, lint and type checks, required checks, auto-merge, CLAUDE.md, regression-test habit) and set up what's missing, with the user's OK.
disable-model-invocation: true
---

# /dev-kit:check

Audit this project and set up what's missing. Keep it short: a table, then fixes the
user agrees to.

## 1. Look

Find the stack (pyproject.toml / package.json / go.mod …) and check each item:

| Item | How to tell |
|---|---|
| Tests exist | a tests folder or test files; the command that runs them |
| Tests run in parallel | pytest-xdist and `-n auto` (Python), the runner's workers (JS) |
| CI on PRs | `.github/workflows/*` running the tests on `pull_request` |
| Lint and type checks in CI | ruff/eslint and mypy/tsc steps inside the CI test job |
| Required checks on the default branch | GitHub ruleset or branch protection requiring the CI jobs (check with the GitHub tools when available; otherwise ask the user to look under Settings → Rules) |
| Auto-merge allowed | repo setting "Allow auto-merge" (GitHub tools, or ask) |
| CLAUDE.md | how to run tests and checks, how to ship (version bump, PR, review, auto-merge), the project's standing rules, and a "Development practices" section |
| Version bump habit | for anything people install: a version field that changes with each release |

## 2. Report

One table: item, ok / missing / can't tell, and the one-line fix.

## 3. Fix, with the user's OK

Offer each missing item; do the ones they agree to, in one PR:
- parallel tests (add pytest-xdist to the dev dependencies, `-n auto` in CI);
- a CI workflow if there's none;
- lint and type-check steps in the CI test job (real-bug lint rules only, no mass reformatting; fix or narrowly ignore what they find, with a regression test for each real bug);
- CLAUDE.md: write or update it, including a "Development practices" section summarizing the dev-kit practices skill, so cloud sessions on this repo follow them too (cloud sessions read a repo's CLAUDE.md but don't load locally installed plugins);
- GitHub settings (required checks, auto-merge): walk the user through them; they're repo settings only they can change.

## 4. Record

Write `.claude/dev-kit.json` in the project:
`{"checked": "<YYYY-MM-DD>", "dev_kit": "<this plugin's version>", "missing": [<items still missing>]}`
so the start-of-session reminder knows when this project was last checked. Commit it with
the rest.
