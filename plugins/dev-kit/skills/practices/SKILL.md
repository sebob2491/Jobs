---
name: practices
description: Development practices for working on code in any project with Claude Code, so work goes faster and breaks less. Apply them whenever writing, fixing, reviewing, testing or shipping code, opening or merging pull requests, or running long or parallel work - even when the user doesn't mention them.
user-invocable: false
---

# Development practices

Apply these on every coding task. Each one exists because it saved time or caught
mistakes on a real project. Where a project's own CLAUDE.md says otherwise, it wins.

## Tests

1. **Every fix comes with a test that fails on the old code.** Write the test, then prove
   it: stash the fix (`git stash push <source dir>`), run the test and see it fail, pop the
   stash, see it pass. A test that passes either way proves nothing.
2. **Run tests in parallel.** Python: `pytest-xdist` and `pytest -n auto`. JavaScript: the
   runner's workers. A suite that takes minutes serially often takes a quarter of that.
3. **While iterating, run only the tests for what changed.** Run the full suite before a
   larger change merges; CI runs it on every PR anyway.
4. **Checks against real outside systems** (live sites, APIs): split them into groups that
   run at once (each with its own state), and run them on a schedule that alerts only
   when a result changes.

## Pull requests

5. **CI on every PR, with required checks on the default branch.** In GitHub: Settings →
   Rules → a ruleset on the default branch with "Require status checks to pass" (the
   test, lint and type-check jobs). Leave "Require a pull request" off when one person
   owns the repo and PRs are opened under their account: they can't approve their own.
6. **Auto-merge on.** Settings → General → "Allow auto-merge". Then each PR merges
   itself when its required checks pass. With required checks in place, there's no
   need to watch each PR: start the next piece of work.
7. **A review pass on every PR before turning on its auto-merge.** Run the code-review
   skill on the PR (or have a fresh agent review the diff), fix what it finds, or say
   on the PR why not. Your own re-read misses what a fresh look catches.
8. **Lint and type checks in CI**, inside the required jobs: a linter (ruff, eslint) for
   real-bug rules, not mass reformatting, and a type checker (mypy, tsc). They catch a
   number arriving where a list was expected before a person hits it.
9. **Fewer, themed PRs.** Each PR costs a CI run and attention. Group related changes.
10. **Anything people install** (a plugin, an app, a package) gets a version bump with
    every shipped change, or updates don't reach them. Tell them how to update.

## Working with agents

11. **Independent work runs in parallel**: separate agents, each in its own git worktree,
    integrated afterwards onto the working branch. Split by files touched, so their changes
    don't collide.
12. **Match the model to the job.** Research and mechanical checking (finding which
    system a site runs, verifying configs) can run on a faster, cheaper model; code
    changes and reviews stay on the main model.
13. **Keep the main conversation lean.** Let agents read big files and long logs and
    report conclusions; print the tail of long outputs, not all of them.

## Long work

14. **Keep a status and handoff note** as work goes: what shipped, what's in progress,
    what's next, and the standing rules. Public facts go in the repo's CLAUDE.md; anything
    personal stays out of the repo.
15. **Start a fresh session per batch of work** once a session gets long: every turn
    re-reads the whole conversation. The handoff note carries what matters.
16. **Status updates at milestones** (starting a piece of work, shipping it), not every step.

## Safety

17. **Nothing personal in a public repo**: no names, addresses, emails, account details.
    Scrub captured pages and logs before they become test fixtures or issues.
18. **Ask before anything hard to undo or outward-facing**: deleting, force-pushing
    someone else's branch, publishing, sending.
