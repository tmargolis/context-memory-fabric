"""Check tracked text against an operator's ignored, local privacy denylist.

Run before publishing: python scripts/check_private_mentions.py
Reports locations and rule numbers, never the sensitive matching text.
Missing configuration fails explicitly; a public clone can pass --allow-missing.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess


def scan(text: str, patterns: list[str]) -> list[tuple[int, int]]:
    rules = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    return [(line_no, rule_no) for line_no, line in enumerate(text.splitlines(), 1)
            for rule_no, rule in enumerate(rules, 1) if rule.search(line)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--denylist", type=Path, default=Path("privacy-denylist.local.json"))
    parser.add_argument("--allow-missing", action="store_true")
    args = parser.parse_args()
    if not args.denylist.exists():
        print("Privacy denylist unavailable; no privacy check was performed.")
        return 0 if args.allow_missing else 2
    config = json.loads(args.denylist.read_text())
    patterns = [re.escape(value) for value in config.get("literals", [])] + config.get("patterns", [])
    if not patterns:
        parser.error("denylist must contain at least one literal or pattern")
    paths = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
    failures = 0
    for name in paths:
        if not name or name in config.get("allow_paths", []):
            continue
        path = Path(name)
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line, rule in scan(text, patterns):
            print(f"{name}:{line}: private-mention rule {rule}")
            failures += 1
    print(f"Privacy check: {failures} matches in tracked files.")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
