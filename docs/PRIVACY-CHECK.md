# Checking for sensitive details before publication

Project names, the author's name and machine names are fine to publish. What must
stay out of tracked files is narrower:

- job-search details: target companies, recruiters and other contacts, compensation;
- network identifiers: LAN IPs and the Tailscale tailnet name or hostnames.

Run `python scripts/check_private_mentions.py` before publishing tracked changes.
It reads `privacy-denylist.local.json` (ignored by Git):

```json
{
  "literals": ["Example Company", "192.0.2.10"],
  "patterns": ["\\bexample-tailnet\\b"],
  "allow_paths": []
}
```

Matching is case-insensitive; literals are escaped automatically, so prefer them
for short words that are also common terms. The check reports file, line and rule
number without echoing the matching text. A missing denylist fails;
`--allow-missing` skips the check on public clones (the test suite uses this).
It scans tracked files only, so run it again after adding new files to Git.
Passing means only that the configured identifiers are absent, not that every
personal fact is.
