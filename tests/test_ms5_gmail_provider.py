"""MS5 — Gmail knowledge provider: conformance, ranking, provenance, Takeout import.

Synthetic mail only; the real sent-mail check is a manual step recorded in
docs (it reads a gitignored snapshot under imports/).
"""

from __future__ import annotations

from email.message import EmailMessage
import hashlib
import mailbox
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone

from scripts.import_gmail_mbox import convert
from server.core.errors import ProviderConfigurationError
from server.knowledge import load_knowledge_sources, propose_knowledge_change, search_knowledge
from server.providers.gmail.provider import GmailKnowledgeProvider
from server.providers.gmail.snapshot import MailMessage, read_snapshot, write_snapshot
from tests.conformance.knowledge_source import KnowledgeSourceConformance

MESSAGES = [
    MailMessage(id="m1", thread_id="t1", date="2026-09-02T10:00:00-05:00", subject="EV charger quote",
                body="Attached is the quote for the EV charger install in the garage. Level 2, 48A.",
                sender="me@example.com", to=["board@condo.example"]),
    MailMessage(id="m2", thread_id="t2", date="2026-09-05T09:30:00-05:00", subject="Dinner Saturday",
                body="Can we move dinner to 7? The EV charger electrician is coming at 5.",
                sender="me@example.com", to=["friend@example.com"]),
    MailMessage(id="m3", thread_id="t3", date="2026-09-10T15:00:00-05:00", subject="Telescope mount",
                body="Ordered the new mount for astrophotography; arrives Friday.",
                sender="me@example.com", to=["club@example.com"]),
]


def _snapshot(tmp: Path) -> Path:
    write_snapshot(tmp, MESSAGES, {"account": "me@example.com"})
    return tmp


class TestGmailConformance(KnowledgeSourceConformance, unittest.TestCase):
    known_query = "EV charger"

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = _snapshot(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def make_source(self):
        return GmailKnowledgeProvider(snapshot_dir=str(self.dir))

    def make_unconfigured_source(self):
        return GmailKnowledgeProvider(snapshot_dir="")

    def fingerprint(self):
        return hashlib.sha256(b"".join(p.read_bytes() for p in sorted(self.dir.rglob("*.json")))).hexdigest()


class TestGmailProvider(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = _snapshot(Path(self._tmp.name))
        self.source = GmailKnowledgeProvider(snapshot_dir=str(self.dir))

    def tearDown(self):
        self._tmp.cleanup()

    def test_subject_match_outranks_passing_mention(self):
        self.assertEqual([r.document_id for r in self.source.query("EV charger")], ["m1", "m2"])

    def test_provenance(self):
        r = self.source.query("telescope")[0]
        self.assertEqual(r.scope, "private:gmail:me@example.com")
        self.assertEqual(r.uri, "https://mail.google.com/mail/u/0/#all/t3")
        self.assertEqual(r.source_version, "2026-09-10T15:00:00-05:00")
        self.assertEqual(r.metadata["to"], "club@example.com")
        self.assertEqual(r.retrieval_method, "bm25")

    def test_account_override(self):
        r = GmailKnowledgeProvider(snapshot_dir=str(self.dir), account="work@example.com").query("telescope")[0]
        self.assertEqual(r.scope, "private:gmail:work@example.com")

    def test_stopword_only_query_returns_nothing(self):
        self.assertEqual(self.source.query("the and of"), [])

    def test_missing_snapshot_is_a_configuration_error(self):
        with self.assertRaises(ProviderConfigurationError):
            GmailKnowledgeProvider(snapshot_dir=str(self.dir / "nope")).query("anything")

    def test_new_messages_are_picked_up(self):
        self.source.query("mount")
        write_snapshot(self.dir, [MailMessage(id="m4", thread_id="t4", date="2026-09-20T08:00:00-05:00",
                                              subject="Mount arrived", body="The mount arrived.")],
                       {"account": "me@example.com"})
        self.assertIn("m4", [r.document_id for r in self.source.query("mount")])

    def test_read_only_for_proposals(self):
        self.assertEqual(propose_knowledge_change("gmail", sources=[self.source])["status"], "unsupported")

    def test_enabled_by_config_alongside_the_wiki(self):
        spec = "server.providers.wiki.provider:FileKnowledgeProvider,server.providers.gmail.provider:GmailKnowledgeProvider"
        self.assertEqual([s.name for s in load_knowledge_sources(spec)], ["wiki", "gmail"])

    def test_unsafe_ids_are_refused(self):
        with self.assertRaises(ValueError):
            write_snapshot(self.dir, [MailMessage(id="../x", thread_id="t", date="2026-01-01T00:00:00+00:00",
                                                  subject="s", body="b")], {})


class TestTakeoutImport(unittest.TestCase):
    def test_keeps_only_matching_sender_since_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = mailbox.mbox(str(Path(tmp) / "Sent.mbox"))
            for sender, date, subject, gm in [
                ("Me <me@example.com>", "Tue, 02 Sep 2026 10:00:00 -0500", "EV charger quote", "111"),
                ("Other <other@example.com>", "Wed, 03 Sep 2026 10:00:00 -0500", "Not mine", "222"),
                ("Me <me@example.com>", "Fri, 01 Aug 2026 10:00:00 -0500", "Too old", "333"),
            ]:
                m = EmailMessage()
                m["From"], m["To"], m["Date"], m["Subject"] = sender, "board@condo.example", date, subject
                m["Message-ID"], m["X-GM-THRID"], m["X-Gmail-Labels"] = f"<{gm}@x>", gm, "Sent,Important"
                m.set_content(f"Body of {subject}")
                m.add_alternative(f"<p>Body of <b>{subject}</b></p>", subtype="html")
                box.add(m)
            box.flush()
            msgs = convert(Path(tmp) / "Sent.mbox", "me@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc))
            self.assertEqual([m.subject for m in msgs], ["EV charger quote"])
            self.assertEqual(msgs[0].thread_id, "111")
            self.assertEqual(msgs[0].labels, ["Sent", "Important"])
            self.assertEqual(msgs[0].body, "Body of EV charger quote")

            write_snapshot(Path(tmp) / "snap", msgs, {"account": "me@example.com"})
            source = GmailKnowledgeProvider(snapshot_dir=str(Path(tmp) / "snap"))
            self.assertEqual(search_knowledge("charger", sources=[source]).results[0].title, "EV charger quote")
            self.assertEqual(len(read_snapshot(Path(tmp) / "snap")[1]), 1)


if __name__ == "__main__":
    unittest.main()
