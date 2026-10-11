"""B03: Note dates reach the graph through seed_wiki_graph and sweep_wiki_graph."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.seed_wiki_graph import note_date_rows
from scripts.sweep_wiki_graph import refresh_note_dates
from server.providers.wiki.note_dates import NOTE_DATE_FIELDS, write_note_dates


def _wiki(tmp: str) -> Path:
    root = Path(tmp)
    subprocess.run(["git", "-C", tmp, "init", "-q"], check=True)
    for date, files in (("2026-04-22T14:00:00-05:00", {"WIKI/imported.md": "old\n"}),
                        ("2026-09-01T10:00:00-05:00", {"WIKI/new.md": "new\n"})):
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_NAME": "t",
               "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        subprocess.run(["git", "-C", tmp, "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", tmp, "commit", "-qm", date], check=True, env=env)
    return root


class FakeNoteDriver:
    """Holds :Note nodes as dicts; answers the read and the date-write queries."""

    def __init__(self, notes: dict[str, dict]):
        self.notes = notes
        self.writes: list[list[dict]] = []

    async def execute_query(self, q, **p):
        if q.startswith("MATCH (n:Note) RETURN n.note_path AS note_path"):
            return ([{"note_path": path, **{k: props.get(k) for k in NOTE_DATE_FIELDS}}
                     for path, props in self.notes.items()], [], None)
        if "UNWIND $rows AS row MATCH (n:Note" in q:
            self.writes.append(p["rows"])
            matched = 0
            for row in p["rows"]:
                node = self.notes.get(row["note_path"])
                if node is None:
                    continue
                matched += 1
                node.update({k: row[k] for k in NOTE_DATE_FIELDS})
            return ([{"c": matched}], ["c"], None)
        raise AssertionError(f"unexpected query: {q}")


class TestNoteDatesInGraph(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _wiki(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_seed_rows_carry_only_the_two_dates(self):
        rows = note_date_rows([{"note_path": "WIKI/imported.md"}, {"note_path": "WIKI/new.md"}], self.root)
        self.assertEqual(set(rows["WIKI/new.md"]), {"created_at", "updated_at"})
        self.assertEqual(rows["WIKI/imported.md"]["created_at"][:10], "2026-04-22")
        self.assertEqual(rows["WIKI/new.md"]["created_at"][:10], "2026-09-01")
        self.assertEqual(note_date_rows([{"note_path": "WIKI/new.md"}], None), {})

    async def test_dry_run_reports_without_writing(self):
        driver = FakeNoteDriver({"WIKI/imported.md": {}, "WIKI/new.md": {}, "RAW/stub.md": {}})
        result = await refresh_note_dates(driver, self.root, dry_run=True)
        self.assertEqual((result["dated"], result["undated"], result["changed"]), (2, 1, 2))
        self.assertEqual(driver.writes, [])

    async def test_writes_only_changes_and_keeps_known_dates(self):
        driver = FakeNoteDriver({"WIKI/imported.md": {}, "WIKI/new.md": {},
                                 "RAW/deleted.md": {"created_at": "2026-05-01T00:00:00+00:00"}})
        first = await refresh_note_dates(driver, self.root, dry_run=False)
        self.assertEqual((first["changed"], first["written"]), (2, 2))
        self.assertEqual(set(driver.notes["WIKI/new.md"]), {"created_at", "updated_at"})
        self.assertEqual(first["created_sources"], {"git-import-upper-bound": 1, "git": 1})
        # a note with no file and no history keeps the dates it already had
        self.assertEqual(driver.notes["RAW/deleted.md"]["created_at"], "2026-05-01T00:00:00+00:00")

        again = await refresh_note_dates(driver, self.root, dry_run=False)
        self.assertEqual((again["changed"], again["written"]), (0, 0))

    async def test_write_note_dates_batches(self):
        driver = FakeNoteDriver({f"WIKI/{i}.md": {} for i in range(5)})
        rows = [{"note_path": f"WIKI/{i}.md", **{k: "x" for k in NOTE_DATE_FIELDS}} for i in range(5)]
        self.assertEqual(await write_note_dates(driver, rows, batch_size=2), 5)
        self.assertEqual([len(b) for b in driver.writes], [2, 2, 1])


if __name__ == "__main__":
    unittest.main()
