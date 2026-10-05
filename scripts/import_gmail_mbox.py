"""Build a Gmail knowledge-provider snapshot from a Google Takeout mbox (MS5).

    uv run python scripts/import_gmail_mbox.py --mbox ~/Downloads/Takeout/Mail/Sent.mbox \\
        --out imports/gmail/snapshot --account me@example.com --from me@example.com --since 2026-08-28

Keeps messages whose From contains --from (e.g. only what you sent) and whose
Date is on or after --since. The body is the text/plain part, or the HTML part
with tags stripped. Thread id comes from Takeout's X-GM-THRID header when
present. Writes the snapshot format in server/providers/gmail/snapshot.py; the
mbox itself is never modified.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
import hashlib
import html
import mailbox
from pathlib import Path
import re
import sys

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from server.providers.gmail.snapshot import MailMessage, write_snapshot  # noqa: E402


def _header(msg, name: str) -> str:
    raw = msg.get(name)
    return str(make_header(decode_header(raw))) if raw else ""


def _body(msg) -> str:
    plain, rich = None, None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/plain" and plain is None:
            plain = text
        elif part.get_content_type() == "text/html" and rich is None:
            rich = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(style|script).*?</\1>", " ", text)))
    return (plain if plain is not None else rich or "").strip()


def convert(mbox_path: Path, sender_filter: str = "", since: datetime | None = None) -> list[MailMessage]:
    out = []
    for msg in mailbox.mbox(str(mbox_path)):
        sender = _header(msg, "From")
        if sender_filter and sender_filter.lower() not in sender.lower():
            continue
        try:
            sent = parsedate_to_datetime(msg.get("Date"))
        except (TypeError, ValueError):
            continue
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=timezone.utc)
        if since and sent < since:
            continue
        message_id = msg.get("Message-ID") or f"{sender}|{sent.isoformat()}|{_header(msg, 'Subject')}"
        out.append(MailMessage(
            id=hashlib.sha1(message_id.encode()).hexdigest()[:16],
            thread_id=(msg.get("X-GM-THRID") or "").strip() or hashlib.sha1(message_id.encode()).hexdigest()[:16],
            date=sent.isoformat(),
            subject=_header(msg, "Subject"),
            body=_body(msg),
            sender=sender,
            to=[a for _, a in getaddresses(msg.get_all("To", []))],
            cc=[a for _, a in getaddresses(msg.get_all("Cc", []))],
            labels=[x.strip() for x in (msg.get("X-Gmail-Labels") or "").split(",") if x.strip()],
        ))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mbox", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--account", required=True)
    ap.add_argument("--from", dest="sender", default="", help="keep only messages whose From contains this")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD; keep messages sent on or after this date (UTC)")
    args = ap.parse_args()
    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc) if args.since else None
    messages = convert(args.mbox, args.sender, since)
    n = write_snapshot(args.out, messages, {
        "account": args.account, "source": f"takeout:{args.mbox.name}",
        "filter": {"from": args.sender, "since": args.since},
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    print(f"wrote {n} messages to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
