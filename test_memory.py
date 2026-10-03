import concurrent.futures
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jarvis import memory
from jarvis.memory import MemoryStore


class FixedDatetime(datetime):
    current = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.current.replace(tzinfo=None)
        return cls.current.astimezone(tz)


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data_dir = Path(self.directory.name) / "jarvis"
        self.store = MemoryStore(self.data_dir)
        FixedDatetime.current = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.clock = patch.object(memory, "datetime", FixedDatetime)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_explicit_memories_persist_and_can_be_forgotten(self):
        identifier = self.store.remember("I prefer tea with milk.")
        reopened = MemoryStore(self.data_dir)
        self.assertEqual(
            reopened.recall("tea"), [{"id": identifier, "text": "I prefer tea with milk."}]
        )
        self.assertTrue(reopened.forget(identifier))
        self.assertFalse(self.store.forget(identifier))
        self.assertEqual(self.store.recall(), [])

    def test_database_is_private_including_existing_file(self):
        self.assertEqual(os.stat(self.store.db_path).st_mode & 0o777, 0o600)
        os.chmod(self.store.db_path, 0o644)
        MemoryStore(self.data_dir)
        self.assertEqual(os.stat(self.store.db_path).st_mode & 0o777, 0o600)

    def test_recall_ranks_overlap_and_respects_limit(self):
        exact = self.store.remember("Project Atlas uses Python.")
        self.store.remember("Python is useful for automation.")
        self.store.remember("Project Beagle uses Swift.")
        self.store.remember("I like tea.")
        results = self.store.recall("Atlas Python", limit=2)
        self.assertEqual(results[0]["id"], exact)
        self.assertEqual(len(results), 2)
        self.assertEqual(self.store.recall("nonexistent"), [])
        self.assertEqual(self.store.recall("!!!"), [])

    def test_recall_uses_whole_unicode_tokens_and_parameterized_input(self):
        self.store.remember("Straße is a German word.")
        self.store.remember("We concatenate strings.")
        self.assertEqual(len(self.store.recall("STRASSE")), 1)
        self.assertEqual(self.store.recall("cat"), [])
        self.assertEqual(self.store.recall("' OR 1=1; --"), [])
        self.assertEqual(len(self.store.recall()), 2)

    def test_blank_query_returns_most_recent(self):
        first = self.store.remember("First fact")
        second = self.store.remember("Second fact")
        self.assertEqual([item["id"] for item in self.store.recall()], [second, first])

    def test_history_survives_restarts_and_keeps_complete_pairs(self):
        for index in range(4):
            self.store.save_turn(f"Question {index}", f"Reply {index}")
        reopened = MemoryStore(self.data_dir)
        self.assertEqual(
            reopened.recent_history(limit=5),
            [
                {"role": "user", "content": "Question 2"},
                {"role": "assistant", "content": "Reply 2"},
                {"role": "user", "content": "Question 3"},
                {"role": "assistant", "content": "Reply 3"},
            ],
        )
        self.assertEqual(reopened.recent_history(limit=1), [])
        self.assertEqual(reopened.recall(), [])

    def test_history_retention_is_bounded(self):
        with patch.object(memory, "MAX_HISTORY_PAIRS", 2):
            for index in range(4):
                self.store.save_turn(f"Question {index}", f"Reply {index}")
        self.assertEqual(len(self.store.recent_history(limit=100)), 4)
        self.assertEqual(self.store.recent_history()[0]["content"], "Question 2")

    def test_reminders_normalize_timezone_and_deliver_once_across_stores(self):
        reminder = self.store.add_reminder("Call the dentist", "2030-01-01T14:30:00+02:00")
        self.assertEqual(reminder["due_at"], "2030-01-01T12:30:00.000000+00:00")
        reopened = MemoryStore(self.data_dir)
        self.assertEqual(reopened.list_reminders(), [reminder])
        self.assertEqual(reopened.due_reminders(), [])
        FixedDatetime.current += timedelta(minutes=30)
        self.assertEqual(reopened.due_reminders(), [reminder])
        self.assertEqual(self.store.due_reminders(), [])
        self.assertEqual(self.store.list_reminders(), [])

    def test_reminders_are_claimed_once_under_concurrent_delivery(self):
        reminder = self.store.add_reminder("Stand up", "2030-01-01T12:01:00Z")
        FixedDatetime.current += timedelta(minutes=2)
        stores = [MemoryStore(self.data_dir), MemoryStore(self.data_dir)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda store: store.due_reminders(), stores))
        delivered = [item for result in results for item in result]
        self.assertEqual(delivered, [reminder])

    def test_pending_reminders_are_sorted_and_cancellable(self):
        later = self.store.add_reminder("Later task", "2030-01-01T15:00:00Z")
        earlier = self.store.add_reminder("Earlier task", "2030-01-01T13:00:00Z")
        self.assertEqual(self.store.list_reminders(), [earlier, later])
        self.assertTrue(self.store.cancel_reminder(earlier["id"]))
        self.assertFalse(self.store.cancel_reminder(earlier["id"]))
        FixedDatetime.current += timedelta(hours=5)
        self.assertEqual(self.store.due_reminders(), [later])
        self.assertFalse(self.store.cancel_reminder(later["id"]))

    def test_rejects_invalid_strings_limits_and_identifiers(self):
        invalid_calls = [
            (TypeError, lambda: self.store.remember(None)),
            (ValueError, lambda: self.store.remember("   ")),
            (ValueError, lambda: self.store.remember("x" * 4097)),
            (TypeError, lambda: self.store.recall(None)),
            (ValueError, lambda: self.store.recall("x" * 1025)),
            (TypeError, lambda: self.store.recall(limit=True)),
            (ValueError, lambda: self.store.recall(limit=0)),
            (ValueError, lambda: self.store.recall(limit=101)),
            (TypeError, lambda: self.store.forget("1")),
            (ValueError, lambda: self.store.forget(-1)),
            (ValueError, lambda: self.store.forget(2**63)),
            (ValueError, lambda: self.store.recent_history(limit=0)),
            (ValueError, lambda: self.store.save_turn("", "reply")),
            (ValueError, lambda: self.store.save_turn("question", "x" * 32769)),
            (TypeError, lambda: self.store.cancel_reminder(False)),
            (ValueError, lambda: self.store.cancel_reminder(0)),
        ]
        for expected, call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(expected):
                    call()

    def test_rejects_naive_past_and_invalid_reminder_dates(self):
        for due_at in [
            "2030-01-01T13:00:00",
            "2030-01-01T11:59:00Z",
            "2030-01-01T12:00:00Z",
            "tomorrow afternoon",
            "",
            "9999-12-31T23:59:59-12:00",
        ]:
            with self.subTest(due_at=due_at):
                with self.assertRaises(ValueError):
                    self.store.add_reminder("An invalid reminder", due_at)
        self.assertEqual(self.store.list_reminders(), [])


if __name__ == "__main__":
    unittest.main()
