"""B03: wiki note dates (server/providers/wiki/note_dates.py)."""

import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from server.providers.wiki.note_dates import (
    GitDateIndex, describe_age, filename_date, frontmatter_dates, parse_date, resolve_note_dates,
)

IMPORT = "2026-04-22T14:00:00-05:00"


class _Repo:
    def __init__(self, root: Path):
        self.root = root
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)

    def commit(self, date: str, write=None, remove=(), move=None, msg=None):
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_NAME": "t",
               "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
        for name, text in (write or {}).items():
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        for name in remove:
            subprocess.run(["git", "-C", str(self.root), "rm", "-q", name], check=True, env=env)
        if move:
            (self.root / move[1]).parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "-C", str(self.root), "mv", move[0], move[1]], check=True, env=env)
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", msg or date], check=True, env=env)


BODY = "A note with enough body text that git rename detection pairs the old and new paths.\n" * 4


class TestNoteDates(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = _Repo(self.root)
        self.repo.commit(IMPORT, {"WIKI/imported.md": BODY, "WIKI/moved.md": BODY + "m\n",
                                  "WIKI/gone.md": BODY + "g\n"}, msg="Initial Commit")
        self.repo.commit("2026-05-01T10:00:00-05:00", {"WIKI/new.md": "new\n",
                                                        "WIKI/fm.md": "---\ncreated: 2025-12-01\nupdated: 2026-01-02\n---\nx\n"})
        self.repo.commit("2026-06-01T10:00:00-05:00", {"WIKI/new.md": "new, edited\n"})
        self.repo.commit("2026-07-01T10:00:00-05:00", move=("WIKI/moved.md", "WIKI/sub/renamed.md"))
        self.repo.commit("2026-08-01T10:00:00-05:00", remove=["WIKI/gone.md"])
        self.repo.commit("2026-09-01T10:00:00-05:00", {"WIKI/gone.md": "back again\n"})
        self.index = GitDateIndex.build(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def dates(self, path):
        return resolve_note_dates(self.root, path, self.index)

    def test_initial_import_is_an_upper_bound(self):
        d = self.dates("WIKI/imported.md")
        self.assertEqual(d["created_source"], "git-import-upper-bound")
        self.assertEqual(d["created_dt"], parse_date(IMPORT))

    def test_later_add_uses_its_commit_and_updated_is_last_commit(self):
        d = self.dates("WIKI/new.md")
        self.assertEqual((d["created_source"], d["created_at"][:10]), ("git", "2026-05-01"))
        self.assertEqual((d["updated_source"], d["updated_at"][:10]), ("git", "2026-06-01"))

    def test_frontmatter_created_wins_but_git_wins_for_updated(self):
        d = self.dates("WIKI/fm.md")
        self.assertEqual((d["created_source"], d["created_at"][:10]), ("frontmatter", "2025-12-01"))
        self.assertEqual((d["updated_source"], d["updated_at"][:10]), ("git", "2026-05-01"))

    def test_rename_keeps_the_original_add(self):
        d = self.dates("WIKI/sub/renamed.md")
        self.assertEqual(d["created_source"], "git-import-upper-bound")
        self.assertEqual(d["updated_at"][:10], "2026-07-01")
        self.assertNotIn("WIKI/moved.md", self.index.first)

    def test_delete_then_readd_counts_from_the_readd(self):
        d = self.dates("WIKI/gone.md")
        self.assertEqual((d["created_source"], d["created_at"][:10]), ("git", "2026-09-01"))

    def test_untracked_file_uses_disk_and_missing_file_has_no_dates(self):
        (self.root / "WIKI/loose.md").write_text("never committed\n")
        self.assertEqual(self.dates("WIKI/loose.md")["created_source"], "disk")
        d = self.dates("WIKI/nowhere.md")
        self.assertEqual((d["created_at"], d["updated_at"], d["created_source"]), (None, None, None))

    def test_disk_birth_before_import_is_used(self):
        p = self.root / "WIKI/imported.md"
        old = datetime(2025, 3, 1, tzinfo=timezone.utc).timestamp()
        os.utime(p, (old, old))  # on APFS/HFS+ an mtime earlier than birth moves birth back too
        if getattr(p.stat(), "st_birthtime", None) is None or p.stat().st_birthtime > old + 1:
            self.skipTest("filesystem does not expose or move birth time")
        d = self.dates("WIKI/imported.md")
        self.assertEqual((d["created_source"], d["created_at"][:10]), ("disk-pre-import", "2025-03-01"))

    def test_incremental_refresh_matches_a_full_rebuild(self):
        self.repo.commit("2026-10-01T10:00:00-05:00", {"WIKI/later.md": "later\n", "WIKI/new.md": "edited again\n"})
        before = self.index.head
        self.index.refresh()
        self.assertNotEqual(self.index.head, before)
        full = GitDateIndex.build(self.root)
        self.assertEqual(self.index.first, full.first)
        self.assertEqual(self.index.last, full.last)
        self.assertEqual(self.dates("WIKI/later.md")["created_at"][:10], "2026-10-01")

    def test_earlier_filename_date_wins_later_one_never_does(self):
        self.repo.commit("2026-07-27T08:00:00-05:00", {
            "WIKI/Report-2026-07-04.md": "written early, committed late\n",
            "WIKI/Trip-Plan-2026-12-01.md": "a plan for a future day\n",
            "WIKI/Same-Day-2026-07-27.md": "same day as its commit\n",
            "WIKI/Dated-2026-01-05.md": "---\ncreated: 2026-03-01\n---\nfrontmatter says otherwise\n",
        })
        self.index.refresh()
        d = self.dates("WIKI/Report-2026-07-04.md")
        self.assertEqual((d["created_source"], d["created_at"][:10]), ("filename", "2026-07-04"))
        self.assertEqual(self.dates("WIKI/Trip-Plan-2026-12-01.md")["created_source"], "git")
        self.assertEqual(self.dates("WIKI/Same-Day-2026-07-27.md")["created_source"], "git")
        self.assertEqual(self.dates("WIKI/Dated-2026-01-05.md")["created_source"], "frontmatter")

    def test_filename_date_before_the_import_beats_the_upper_bound(self):
        root = Path(self._tmp.name) / "second"
        root.mkdir()
        repo = _Repo(root)
        repo.commit(IMPORT, {"WIKI/Meeting-2026-03-10.md": "imported\n", "WIKI/plain.md": "imported\n"}, msg="Initial Commit")
        index = GitDateIndex.build(root)
        d = resolve_note_dates(root, "WIKI/Meeting-2026-03-10.md", index)
        self.assertEqual((d["created_source"], d["created_at"][:10]), ("filename", "2026-03-10"))
        self.assertEqual(resolve_note_dates(root, "WIKI/plain.md", index)["created_source"], "git-import-upper-bound")

    def test_filename_date_parsing(self):
        self.assertEqual(filename_date("WIKI/x/Notes 2025-11-02 and 2026-01-01.md").date().isoformat(), "2025-11-02")
        self.assertEqual(filename_date("WIKI/Bad-2026-13-40 then 2026-02-03.md").date().isoformat(), "2026-02-03")
        self.assertIsNone(filename_date("WIKI/2025-11-02/undated.md"))  # folders don't count
        self.assertIsNone(filename_date("WIKI/v12026-01-019.md"))  # digits glued on both sides

    def test_not_a_git_repo_falls_back_to_frontmatter_then_disk(self):
        with tempfile.TemporaryDirectory() as plain:
            Path(plain, "a.md").write_text("---\ncreated: 2026-05-15 10:00:00\nupdated: 2026-06-20\n---\n")
            d = resolve_note_dates(Path(plain), "a.md", GitDateIndex.build(Path(plain)))
            self.assertEqual((d["created_source"], d["updated_source"]), ("frontmatter", "frontmatter"))
            self.assertEqual(d["created_at"][:10], "2026-05-15")


class TestHelpers(unittest.TestCase):
    def test_parse_date_forms(self):
        self.assertEqual(parse_date("2026-05-15").tzinfo, timezone.utc)
        self.assertEqual(parse_date("'2026-05-15T10:00:00Z'").hour, 10)
        self.assertIsNone(parse_date("someday"))
        self.assertIsNone(parse_date(None))

    def test_frontmatter_only_reads_the_leading_block(self):
        self.assertEqual(frontmatter_dates("no block\ncreated: 2020-01-01\n"), {"created": None, "updated": None})

    def test_describe_age(self):
        now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
        dates = {"created_dt": parse_date(IMPORT), "created_source": "git-import-upper-bound",
                 "updated_dt": datetime(2026, 10, 9, 8, tzinfo=timezone.utc)}
        self.assertEqual(describe_age(dates, now), "created 2026-04-22 (on or before) | updated 2026-10-09 (1 day ago)")
        self.assertIsNone(describe_age({}, now))


if __name__ == "__main__":
    unittest.main()
