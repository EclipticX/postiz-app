import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import StudioStore


class StudioStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "data" / "studio.sqlite3"
        self.store = self.open_store()

    def open_store(self):
        store = StudioStore(self.path)
        store.initialize()
        return store

    def test_idea_decisions_only_queue_accepted_ideas(self):
        accepted = self.store.add_idea("Tell a story about a helpful owl.")
        rejected = self.store.add_idea("A story to reject")
        self.store.decide_idea(accepted, "later")
        self.store.decide_idea(rejected, "reject")
        self.assertEqual(self.store.list_jobs(), [])

        restarted = self.open_store()
        self.assertEqual([idea["status"] for idea in restarted.list_ideas()], ["rejected", "later"])
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _: restarted.decide_idea(accepted, "accept"), range(8)))

        job, = self.open_store().list_jobs()
        self.assertEqual(job["idea_id"], accepted)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(restarted.get_job(job["id"])["title"], "Tell a story about a helpful owl.")
        ideas = {idea["id"]: idea for idea in restarted.list_ideas()}
        self.assertEqual(ideas[accepted]["job_id"], job["id"])
        self.assertIsNone(ideas[rejected]["job_id"])

    def test_idea_rejects_unknown_or_conflicting_decisions(self):
        idea = self.store.add_idea("A prompt")
        with self.assertRaises(ValueError):
            self.store.decide_idea(idea, "maybe")
        with self.assertRaises(LookupError):
            self.store.decide_idea(999, "accept")
        self.store.decide_idea(idea, "reject")
        with self.assertRaises(ValueError):
            self.store.decide_idea(idea, "accept")

    def test_niche_and_asset_seed_is_idempotent(self):
        profile = {
            "id": "kids_3d",
            "name": "Kids 3D",
            "assets": [
                {
                    "id": "owl",
                    "kind": "character",
                    "description": "A small blue owl",
                    "reference_image": "assets/owl.png",
                }
            ],
        }
        self.store.seed_niche(profile)
        self.open_store().seed_niche(profile)
        idea = self.store.add_idea("Use the owl")
        self.store.decide_idea(idea, "accept")
        self.assertEqual(self.store.list_jobs()[0]["niche_id"], "kids_3d")
        with sqlite3.connect(self.path) as db:
            config, = db.execute("SELECT config_json FROM niches").fetchone()
            images, = db.execute("SELECT reference_images_json FROM assets").fetchone()
            self.assertEqual(json.loads(config), profile)
            self.assertEqual(json.loads(images), ["assets/owl.png"])

    def test_100_prompts_dispatch_once_per_local_day(self):
        schedule = self.store.create_schedule(
            [f"Prompt {number}" for number in range(100)],
            "2026-11-09",
            "09:00",
            "Asia/Dhaka",
            1,
        )
        due = datetime(2026, 11, 9, 3, tzinfo=timezone.utc)
        self.assertEqual(self.store.dispatch_due(due), 0)
        self.store.set_schedule_status(schedule, "active")
        self.assertEqual(self.store.dispatch_due(due - timedelta(minutes=1)), 0)
        with ThreadPoolExecutor(max_workers=4) as executor:
            counts = executor.map(lambda _: self.store.dispatch_due(due), range(8))
            self.assertEqual(sum(counts), 1)

        restarted = self.open_store()
        self.assertEqual(restarted.dispatch_due(due + timedelta(hours=5)), 0)
        for day in range(1, 100):
            self.assertEqual(restarted.dispatch_due(due + timedelta(days=day)), 1)
        self.assertEqual(restarted.dispatch_due(due + timedelta(days=100)), 0)
        jobs = restarted.list_jobs()
        self.assertEqual(len(jobs), 100)
        self.assertEqual(jobs[-1]["prompt"], "Prompt 0")
        self.assertEqual(jobs[0]["prompt"], "Prompt 99")
        self.assertEqual(restarted.list_schedules()[0]["progress"]["queued"], 100)

    def test_schedule_pause_and_missed_days_keep_daily_limit(self):
        schedule = self.store.create_schedule(
            [f"Prompt {number}" for number in range(6)],
            "2026-11-09",
            "09:00",
            "Asia/Dhaka",
            2,
        )
        due = datetime(2026, 11, 9, 3, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            self.store.set_schedule_status(schedule, "paused")
        self.store.set_schedule_status(schedule, "active")
        self.assertEqual(self.store.dispatch_due(due), 2)
        self.store.set_schedule_status(schedule, "paused")
        self.assertEqual(self.store.dispatch_due(due + timedelta(days=1)), 0)
        self.store.set_schedule_status(schedule, "active")
        self.assertEqual(self.store.dispatch_due(due + timedelta(days=3)), 2)
        self.assertEqual(self.store.dispatch_due(due + timedelta(days=3, hours=1)), 0)
        self.assertEqual(self.store.list_schedules()[0]["progress"]["pending"], 2)

    def test_schedule_validates_inputs(self):
        with self.assertRaises(ValueError):
            self.store.create_schedule([], "2026-11-09", "09:00", "Asia/Dhaka", 1)
        with self.assertRaises(ValueError):
            self.store.create_schedule(["Prompt"] * 101, "2026-11-09", "09:00", "Asia/Dhaka", 1)
        with self.assertRaises(ValueError):
            self.store.create_schedule(["Prompt"], "2026-11-09", "09:00", "Invalid/Timezone", 1)
        with self.assertRaises(ValueError):
            self.store.create_schedule(["Prompt"], "2026-11-09", "09:00+06:00", "Asia/Dhaka", 1)
        with self.assertRaises(ValueError):
            self.store.create_schedule(["Prompt"], "2026-11-09", "09:00", "Asia/Dhaka", 2)
        with self.assertRaises(ValueError):
            self.store.dispatch_due(datetime(2026, 11, 9, 3))


if __name__ == "__main__":
    unittest.main()
