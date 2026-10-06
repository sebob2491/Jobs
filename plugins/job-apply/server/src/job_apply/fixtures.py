"""Turn a debug snapshot of a real application page into a regression-test fixture.

    uv run --project <plugin>/server python -m job_apply.fixtures <snapshot dir> <name>

The page's DOM is kept (scripts removed) so tests can check that field labels,
kinds and required flags are still extracted correctly. Profile values (name,
email, phone, address, links) are replaced before anything is written. Check the
output for other personal data before committing it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .config import Profile

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "live"
REDACTED = "REDACTED"


def personal_strings(prof: Profile) -> list[str]:
    keys = [
        "personal.first_name", "personal.middle_name", "personal.last_name", "personal.preferred_name",
        "personal.email", "personal.phone", "personal.address.line1", "personal.address.line2",
        "personal.address.postal_code", "personal.linkedin_url", "personal.github_url", "personal.website",
    ]
    values = [str(prof.get(k)) for k in keys if prof.get(k)]
    if prof.full_name:
        values.append(prof.full_name)
    phone = re.sub(r"\D", "", str(prof.get("personal.phone") or ""))
    if len(phone) >= 7:
        values.append(phone)
    # longest first so "Sam Rivera" is replaced before "Sam"
    return sorted({v for v in values if len(v) >= 3}, key=len, reverse=True)


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        text = re.sub(re.escape(s), REDACTED, text, flags=re.I)
    return text


def clean_html(raw: str, secrets: list[str], frame_map: dict[str, str] | None = None, base_url: str = "") -> str:
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup.find_all(["script", "noscript", "base"]):
        tag.decompose()
    # Keep fixtures hermetic: nothing may load from the network when a test opens them.
    for tag in soup.find_all("link"):
        if str(tag.get("href", "")).startswith(("http:", "https:", "//")):
            tag.decompose()
    for tag in soup.find_all(["img", "source", "video", "audio"]):
        tag.attrs.pop("src", None)
        tag.attrs.pop("srcset", None)
    for tag in soup.find_all(True):
        for attr in [a for a in tag.attrs if a.startswith("data-ja-") or a.startswith("on")]:
            del tag[attr]
        if tag.name == "input" and tag.get("type") not in ("radio", "checkbox", "submit", "button"):
            tag.attrs.pop("value", None)
        if tag.name == "iframe" and frame_map and tag.get("src"):
            absolute = urljoin(base_url, tag["src"])
            if absolute in frame_map:
                tag["src"] = frame_map[absolute]
    return redact(str(soup), secrets)


def convert(snapshot: Path, name: str, out_dir: Path = DEFAULT_DIR, prof: Profile | None = None) -> list[Path]:
    meta = json.loads((snapshot / "snapshot.json").read_text(encoding="utf-8"))
    secrets = personal_strings(prof or Profile.load())
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    frame_map: dict[str, str] = {}
    for fr in meta.get("frames", []):
        if fr["file"] != "page.html" and fr.get("url"):
            frame_map[fr["url"]] = f"{name}.{fr['file']}"
    for fr in meta.get("frames", []):
        src = snapshot / fr["file"]
        if not src.exists():
            continue
        target = out_dir / (f"{name}.html" if fr["file"] == "page.html" else frame_map.get(fr.get("url", ""), f"{name}.{fr['file']}"))
        html = clean_html(src.read_text(encoding="utf-8"), secrets, frame_map, fr.get("url") or meta.get("url", ""))
        target.write_text(html, encoding="utf-8")
        written.append(target)

    expect: dict[str, Any] = {
        "source_url": redact(meta.get("url", ""), secrets),
        "note": meta.get("note", ""),
        "fields": [
            {k: f[k] for k in ("label", "kind", "required") if k in f}
            for f in meta.get("fields", [])
            if f.get("label")
        ],
    }
    expect_path = out_dir / f"{name}.expect.json"
    expect_path.write_text(redact(json.dumps(expect, indent=2), secrets), encoding="utf-8")
    written.append(expect_path)
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snapshot", type=Path, help="a debug/<timestamp> folder from a job's application folder")
    ap.add_argument("name", help="fixture name, e.g. workday-my-information")
    ap.add_argument("--out", type=Path, default=DEFAULT_DIR)
    args = ap.parse_args(argv)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.name):
        ap.error("name must be lowercase letters, digits, - or _")
    for p in convert(args.snapshot, args.name, args.out):
        print(p)
    print("\nEdit the .expect.json to keep only fields you've checked, and look the HTML over for "
          "personal data the profile didn't cover before committing.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
