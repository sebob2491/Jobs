# Fixtures captured from real application pages

Each `<name>.html` is a real ATS page saved by `debug_snapshot` (or automatically
when a field failed to fill) and converted with:

    uv run --project plugins/job-apply/server python -m job_apply.fixtures <snapshot dir> <name>

`<name>.expect.json` lists the fields the extractor must keep finding, with the
same label, kind and required flag. `tests/test_live_fixtures.py` checks every pair.

`<name>.decision.json` (written by `scripts/live_smoke.py --fixtures`, `job_apply.fixtures.decide`) is what
the desk decides on the page for the live check's fake applicant: the page's kind, the step button, the
CAPTCHA, security-question, account-and-application and job-search reads, and each box's answer
(filled, asked or skipped, and the rule). `test_live_fixture_decides_the_same` decides it again offline;
an intended change is accepted with `nightly_summary.py --import-fixtures --replace`, or
`JOB_APPLY_REDECIDE=1` on that test for a page no run has captured again.

The converter removes scripts and replaces profile values (name, email, phone,
address, links). Review the HTML for any other personal data before committing.
