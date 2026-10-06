# Fixtures captured from real application pages

Each `<name>.html` is a real ATS page saved by `debug_snapshot` (or automatically
when a field failed to fill) and converted with:

    uv run --project plugins/job-apply/server python -m job_apply.fixtures <snapshot dir> <name>

`<name>.expect.json` lists the fields the extractor must keep finding, with the
same label, kind and required flag. `tests/test_live_fixtures.py` checks every pair.

The converter removes scripts and replaces profile values (name, email, phone,
address, links). Review the HTML for any other personal data before committing.
