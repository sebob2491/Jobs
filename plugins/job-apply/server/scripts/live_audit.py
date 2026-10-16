"""The live check's rules audit, over what a check's machine wrote. No network.

    uv run python scripts/live_audit.py --log pipeline=live-pipeline/run.log --folder live-pipeline

Reads the check's LIVE_PIPELINE records (as scripts/live_compare.py does) and the files in its folder, and
fails, one ::error:: line a finding, where the run broke a rule the product keeps (CLAUDE.md):
  - consents: a log line saying the desk agreed to, ticked, pressed, chose or accepted something that names
    a newsletter, job alerts, a talent community, marketing, texts or automated calls, arbitration, at-will
    employment or a waiver, which are never the desk's to give;
  - submitting: a round whose status is "submitted" (the live check never submits: JOB_APPLY_NEVER_SUBMIT);
  - secrets: the test identity's email and passwords (LIVE_TEST_EMAIL, LIVE_TEST_EMAIL_PASSWORD and
    LIVE_TEST_SITE_PASSWORD, read from the environment as the check's machine has them) in any file the
    check wrote, unmasked; an emailed code (six digits beside "code") or a security answer the test identity
    made (live_identity.security_answer's shape) in a log line, a reason or a file.
The nightly workflow runs it on each check's machine after the check; a finding fails that job.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_compare  # noqa: E402  (scripts/live_compare.py, beside this one)

SECRETS = ("LIVE_TEST_EMAIL", "LIVE_TEST_EMAIL_PASSWORD", "LIVE_TEST_SITE_PASSWORD")
MAX_FILE = 50 * 1024 * 1024

# the desk's own log lines open with what it did; a consent it gave reads "agreed to …", "ticked …", "pressed …"
_GAVE = re.compile(r"^(?:agreed to|ticked|pressed|chose|accepted|picked)\b", re.I)
_NEVER = re.compile(r"\b(?:newsletters?|job alerts?|talent (?:community|network|pool)|marketing|text messages?|"
                    r"texts|sms|automated (?:calls?|messages?)|arbitration|at[- ]will|waiv(?:e|er|ers)|"
                    r"release of claims)\b", re.I)
_NOT = re.compile(r"\b(?:never|not|didn't|unticked|left|refused|declined|instead of)\b", re.I)
_CODE = re.compile(r"\bcode\b.{0,40}?\b\d{6}\b|\b\d{6}\b.{0,40}?\bcode\b", re.I | re.S)
_ANSWER = re.compile(r"\bjd[0-9a-f]{12}\b")  # live_identity.security_answer: "jd" + 12 hex digits


def consent_given(line: str) -> bool:
    """A log line where the desk gave a consent that is never its to give."""
    return bool(_GAVE.match(line.strip()) and _NEVER.search(line) and not _NOT.search(line))


def record_findings(check: str, rec: dict[str, Any]) -> Iterator[str]:
    """What one employer's record broke, each as one line."""
    name = str(rec.get("company") or "?")
    for i, rnd in enumerate(rec.get("rounds") or [], 1):
        if rnd.get("status") == "submitted":
            yield f"{check}/{name}: round {i} was submitted (the live check never submits)"
        texts = [str(line) for line in rnd.get("log") or []] + [str(rnd.get("reason") or "")]
        for text in texts:
            if consent_given(text):
                yield f"{check}/{name}: a consent the desk never gives: {text[:200]}"
            if _CODE.search(text):
                yield f"{check}/{name}: an emailed code in the log: {text[:200]}"
            if _ANSWER.search(text):
                yield f"{check}/{name}: a security answer in the log"


def log_findings(check: str, text: str) -> list[str]:
    return [f for rec in live_compare.read_log(text, "LIVE_PIPELINE ").values() for f in record_findings(check, rec)]


def _texts(folder: Path) -> Iterator[tuple[Path, str]]:
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        if path.stat().st_size > MAX_FILE:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:  # a screenshot, a zip
            continue
        yield path, data.decode("utf-8", errors="replace")


def folder_findings(folder: Path, secrets: dict[str, str]) -> list[str]:
    """Secrets (by name, never by value), codes and security answers in the files a check wrote."""
    found = []
    for path, text in _texts(folder):
        shown = path.as_posix()
        for name, value in secrets.items():
            if value and len(value) >= 6 and value in text:
                found.append(f"{shown}: {name}'s value, unmasked")
        if _ANSWER.search(text):
            found.append(f"{shown}: a security answer the test identity made")
        if path.name == "run.log":
            continue  # (its log lines are read as records, with the code rule; a page's six digits are its own)
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--log", action="append", default=[], metavar="CHECK=PATH", help="a check's run.log; repeatable")
    ap.add_argument("--folder", action="append", default=[], type=Path, help="a check's output folder; repeatable")
    args = ap.parse_args(argv)
    findings: list[str] = []
    records = 0
    for spec in args.log:
        check, sep, path = spec.partition("=")
        if not sep:
            ap.error(f"--log {spec!r}: expected CHECK=PATH")
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"::warning::{check}: can't read {path} ({e})")
            continue
        records += len(live_compare.read_log(text, "LIVE_PIPELINE "))
        findings += log_findings(check, text)
    secrets = {name: os.environ.get(name, "") for name in SECRETS}
    files = 0
    for folder in args.folder:
        if not folder.is_dir():
            print(f"::warning::{folder.as_posix()}: no such folder")
            continue
        files += sum(1 for _ in _texts(folder))
        findings += folder_findings(folder, secrets)
    for finding in findings:
        print(f"::error::{finding}")
    if findings:
        print(f"audit: {len(findings)} finding{'' if len(findings) == 1 else 's'} ({records} records, {files} files)")
        return 1
    print(f"audit: nothing found ({records} records, {files} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
