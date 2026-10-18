"""The questions a live check's runs asked that no common question covers yet (data/question_bank.yaml), most
asked first: what to look at when adding to the bank. Reads a run's saved results, as nightly_summary.py
--artifacts does (a downloaded artifact's zip, or its folder; several may be given).

    uv run python scripts/question_bank.py live-accounts-0.zip live-accounts-1.zip live-accounts-2.zip
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nightly_summary  # (beside this one)

from job_apply import config


def bare(label: str) -> str:
    return " ".join(re.sub(r"\(required\)|\*", " ", label or "", flags=re.I).strip(" :?.").split())


def uncovered(records: list[dict], bank: list[dict]) -> list[tuple[str, set[str], set[str]]]:
    """(question, the employers that asked it, its choices) for each question asked that no bank entry's pattern
    matches, the most asked first."""
    patterns = [re.compile(str(q["match"]), re.I) for q in bank]
    asked: dict[str, tuple[str, set[str], set[str]]] = {}
    for rec in records:
        for rnd in rec.get("rounds") or []:
            for q in rnd.get("questions") or []:
                label = bare(q.get("label") or "")
                # (never an account form's security questions: their answers are that account's alone)
                if not label or q.get("account") or any(p.search(label) for p in patterns):
                    continue
                shown, who, choices = asked.setdefault(label.lower(), (label, set(), set()))
                who.add(str(rec.get("company") or "?"))
                choices.update(str(o) for o in (q.get("options") or [])[:12])
    return sorted(asked.values(), key=lambda a: (-len(a[1]), a[0].lower()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", type=Path, help="a run's saved results: an artifact's zip or folder")
    ap.add_argument("--min", type=int, default=1, help="only questions this many employers asked, or more")
    args = ap.parse_args()
    records = [r for where in args.runs for r in nightly_summary.records(where)]
    for label, who, choices in uncovered(records, config.question_bank()):
        if len(who) >= args.min:
            print(f"{len(who):3}  {label[:150]}" + (f"  {sorted(choices)[:6]}" if choices else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
