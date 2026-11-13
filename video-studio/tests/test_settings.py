import sqlite3
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import SCHEMA, StudioStore
from app.settings import StudioSettings


class StudioSettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "studio.sqlite3"
        self.env_path = self.path.with_name(".env")
        self.store = StudioStore(self.path)
        self.store.initialize()
        self.settings = StudioSettings(self.store, self.env_path)
        self.settings.seed()

    def provider(self, name):
        return next(row for row in self.settings.list_providers() if row["name"] == name)

    def new_job(self):
        idea_id = self.store.add_idea("Story prompt")
        self.store.decide_idea(idea_id, "accept")
        return self.store.list_ideas()[0]["job_id"]

    def test_keys_persist_locally_and_preserve_provider_preferences(self):
        self.assertFalse(self.provider("fal-veo")["enabled"])
        self.assertIsNone(self.settings.key("FAL_KEY"))
        self.env_path.write_text("STUDIO_PORT=8400\n", encoding="utf-8")
        self.settings.set_key("FAL_KEY", "test-secret-key")
        self.assertTrue(self.provider("fal-veo")["enabled"])
        self.assertTrue(self.provider("fal-veo")["configured"])
        self.assertTrue(self.provider("fal-elevenlabs")["enabled"])
        self.assertFalse(self.provider("fal-kling")["enabled"])
        self.assertIn("STUDIO_PORT=8400", self.env_path.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)
        self.assertNotIn("test-secret-key", str(self.settings.list_providers()))
        self.assertNotIn(b"test-secret-key", self.path.read_bytes())

        restarted = StudioSettings(StudioStore(self.path), self.env_path)
        restarted.seed()
        self.assertEqual(restarted.key("FAL_KEY"), "test-secret-key")
        self.settings.update_provider("fal-veo", False, 3, 123)
        self.settings.set_key("FAL_KEY", "replacement")
        self.assertEqual(restarted.key("FAL_KEY"), "replacement")
        self.settings.set_key("FAL_KEY", "")
        self.assertIsNone(restarted.key("FAL_KEY"))
        self.assertFalse(self.provider("fal-veo")["configured"])
        self.settings.set_key("FAL_KEY", "re-added")
        self.assertFalse(self.provider("fal-veo")["enabled"])
        self.assertEqual(self.provider("fal-veo")["priority"], 3)
        self.assertEqual(self.provider("fal-veo")["rate_micros"], 123)

    def test_simultaneous_credential_edits_preserve_both_keys(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.settings.set_key, "FAL_KEY", "first-test-key")
            second = executor.submit(self.settings.set_key, "EVOLINK_API_KEY", "second-test-key")
            first.result()
            second.result()
        self.assertEqual(self.settings.key("FAL_KEY"), "first-test-key")
        self.assertEqual(self.settings.key("EVOLINK_API_KEY"), "second-test-key")

    def test_invalid_key_or_provider_does_not_change_settings(self):
        with self.assertRaises(ValueError):
            self.settings.set_key("SOME_OTHER_ENV", "test-key")
        with self.assertRaises(ValueError):
            self.settings.set_key("FAL_KEY", "fake\nPOSTIZ_API_KEY=bad")
        with self.assertRaises(ValueError):
            self.settings.update_provider("fal-veo", True, -1, 0)
        with self.assertRaises(LookupError):
            self.settings.update_provider("unknown", True, 1, 0)
        self.assertFalse(self.env_path.exists())

    def test_daily_budget_rolls_over_in_dhaka_but_video_budget_persists(self):
        job = self.new_job()
        before_midnight = datetime(2026, 11, 9, 17, 59, tzinfo=timezone.utc)
        self.assertEqual(self.settings.get_budgets(), {"daily_micros": 0, "video_micros": 0})
        with self.assertRaisesRegex(ValueError, "Daily budget"):
            self.settings.reserve(None, "claude-sonnet", 1, before_midnight)

        self.settings.set_budgets(500, 400)
        idea_spend = self.settings.reserve(None, "claude-sonnet", 150, before_midnight)
        self.settings.settle(idea_spend)
        clip_spend = self.settings.reserve(job, "fal-veo", 350, before_midnight)
        self.settings.settle(clip_spend)
        with self.assertRaisesRegex(ValueError, "Daily budget"):
            self.settings.reserve(None, "claude-sonnet", 1, before_midnight)

        after_midnight = before_midnight + timedelta(minutes=1)
        with self.assertRaisesRegex(ValueError, "Per-video budget"):
            self.settings.reserve(job, "fal-veo", 51, after_midnight)
        self.settings.reserve(job, "fal-veo", 50, after_midnight)
        self.settings.reserve(None, "claude-sonnet", 450, after_midnight)

    def test_concurrent_workers_cannot_overspend_and_can_reconcile(self):
        job = self.new_job()
        now = datetime(2026, 11, 9, 3, tzinfo=timezone.utc)
        self.settings.set_budgets(1_000, 1_000)

        def reserve(_):
            try:
                return self.settings.reserve(job, "fal-veo", 400, now)
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=4) as executor:
            reservations = [result for result in executor.map(reserve, range(8)) if result is not None]
        first, second = reservations
        self.settings.release(first)
        self.settings.release(first)
        self.settings.settle(second, 100)
        self.settings.settle(second)
        with self.assertRaises(ValueError):
            self.settings.release(second)
        with self.assertRaises(ValueError):
            self.settings.settle(first, 1)
        self.settings.reserve(job, "fal-veo", 900, now)
        with self.assertRaisesRegex(ValueError, "Daily budget"):
            self.settings.reserve(job, "fal-veo", 1, now)

    def test_migration_preserves_v1_data(self):
        old_path = self.path.with_name("old.sqlite3")
        with sqlite3.connect(old_path) as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO ideas (title, prompt, source) VALUES ('Old', 'Old prompt', 'manual')")
        old_store = StudioStore(old_path)
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: old_store.initialize(), range(2)))
        self.assertEqual(old_store.list_ideas()[0]["prompt"], "Old prompt")
        with sqlite3.connect(old_path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 8)
            self.assertIsNotNone(db.execute("SELECT COUNT(*) FROM idea_generation_requests").fetchone())
        old_settings = StudioSettings(old_store, self.env_path)
        self.assertEqual(old_settings.get_budgets(), {"daily_micros": 0, "video_micros": 0})


if __name__ == "__main__":
    unittest.main()
