import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from town_mirror import db, query
from town_mirror.crawl import Crawler
from town_mirror.stream import Stream


class MirrorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, value in (("MIRROR_DIR", root), ("MIRROR_DB", root / "town.sqlite")):
            p = patch.object(db, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_idle_firehose_does_not_block_another_writer(self):
        stream = Stream()
        self.addCleanup(stream.db.close)
        other = db.connect_rw()
        self.addCleanup(other.close)
        other.execute("PRAGMA busy_timeout=50")
        stream.handle({"t": "#identity"}, {"did": "did:test", "handle": "test", "seq": 1})
        self.assertFalse(stream.db.in_transaction)
        other.execute("UPDATE accounts SET handle='updated'")
        other.commit()
        self.assertEqual(other.execute("SELECT value FROM crawl_state WHERE key='firehose_seq'").fetchone()[0], '1')

    def test_failed_frame_rolls_back_before_cursor_advances(self):
        stream = Stream()
        self.addCleanup(stream.db.close)
        with self.assertRaises(Exception):
            stream.handle({"t": "#commit"},
                          {"repo": "did:broken", "seq": 2, "blocks": b"invalid CAR"})
        self.assertFalse(stream.db.in_transaction)
        self.assertEqual(stream.db.execute("SELECT count(*) FROM accounts").fetchone()[0], 0)
        self.assertIsNone(stream.db.execute("SELECT value FROM crawl_state WHERE key='firehose_seq'").fetchone())

    def test_live_event_does_not_hide_a_reconcile_gap(self):
        stream = Stream()
        self.addCleanup(stream.db.close)
        stream.db.execute("INSERT INTO accounts(did, rev) VALUES ('did:test', 'last-complete-crawl')")
        stream.db.commit()
        stream.handle({"t": "#commit"},
                      {"repo": "did:test", "seq": 3, "rev": "new-live-rev", "ops": []})
        self.assertEqual(stream.db.execute("SELECT rev FROM accounts").fetchone()[0],
                         'last-complete-crawl')

    def test_partial_pagination_is_not_a_complete_collection(self):
        crawler = Crawler()
        self.addCleanup(crawler.db.close)
        self.addCleanup(crawler.http.close)
        with patch.object(crawler, '_get', side_effect=[
                {"records": [{"uri": "at://existing"}], "cursor": "next"}, None]):
            self.assertIsNone(crawler.list_records("did:test", "town.delve.feed.post"))

    def test_failed_description_does_not_remove_records(self):
        crawler = Crawler()
        self.addCleanup(crawler.db.close)
        self.addCleanup(crawler.http.close)
        with patch.object(crawler, '_get', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'keeping existing records'):
                crawler.crawl_repo('did:test', 'rev')
        self.assertFalse(crawler.db.in_transaction)

    def test_inactive_account_does_not_lock_next_network_crawl(self):
        crawler = Crawler()
        self.addCleanup(crawler.db.close)
        self.addCleanup(crawler.http.close)
        other = db.connect_rw()
        self.addCleanup(other.close)
        other.execute("PRAGMA busy_timeout=50")

        def crawl(*args):
            other.execute("INSERT INTO accounts(did) VALUES ('did:concurrent')")
            other.commit()
            return {"posts+": 0, "posts-": 0}

        with patch.object(crawler, 'list_repos', return_value=[
                {"did": "did:inactive", "active": False}, {"did": "did:active", "rev": "new"}]), \
                patch.object(crawler, 'crawl_repo', side_effect=crawl):
            crawler.cycle()

    def test_sql_reader_is_closed_and_does_not_claim_live(self):
        writer = db.connect_rw()
        self.addCleanup(writer.close)
        writer.execute("INSERT INTO crawl_state VALUES ('firehose_at', '2020-01-01T00:00:00+00:00')")
        writer.commit()
        reader = db.connect_ro()
        with patch.object(query, 'connect_ro', return_value=reader):
            result = query.sql('SELECT * FROM crawl_state')
        self.assertNotIn('live', result)
        with self.assertRaises(sqlite3.ProgrammingError):
            reader.execute('SELECT 1')


if __name__ == '__main__':
    unittest.main()
