import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import (
    LIBRARY_SCHEMA,
    MEDIA_SCHEMA,
    PUBLISHING_SCHEMA,
    SCHEMA,
    SETTINGS_AND_PLANNING_SCHEMA,
    StudioStore,
    _apply_schema,
)


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

    def test_inbox_upgrade_keeps_existing_records_and_active_queue(self):
        legacy = StudioStore(self.path.with_name("legacy.sqlite3"))
        with legacy._connection(write=True) as db:
            for schema in (SCHEMA, SETTINGS_AND_PLANNING_SCHEMA, MEDIA_SCHEMA, PUBLISHING_SCHEMA, LIBRARY_SCHEMA):
                _apply_schema(db, schema)
            db.execute(
                "INSERT INTO ideas (id, title, prompt, status, source) VALUES (7, 'Old idea', 'Old prompt', 'later', 'manual')"
            )
            db.execute(
                """
                INSERT INTO prompt_schedules (id, status, start_date, run_at, timezone, daily_limit)
                VALUES (8, 'active', '2026-11-09', '09:00', 'Asia/Dhaka', 1)
                """
            )
            db.execute("INSERT INTO schedule_entries (id, schedule_id, prompt) VALUES (9, 8, 'Old batch prompt')")

        legacy.initialize()
        legacy.initialize()
        idea, = legacy.list_ideas()
        schedule, = legacy.list_schedules()
        self.assertEqual((idea["id"], idea["prompt"], idea["status"]), (7, "Old prompt", "later"))
        self.assertEqual((schedule["id"], schedule["status"], schedule["entries"][0]["id"]), (8, "active", 9))
        self.assertIsNone(schedule["title"])
        self.assertIsNone(schedule["created_at"])
        self.assertEqual(legacy.dispatch_due(datetime(2026, 11, 9, 3, tzinfo=timezone.utc)), 1)
        self.assertEqual(legacy.list_jobs()[0]["prompt"], "Old batch prompt")

    def test_idea_delete_and_restore_preserve_decision(self):
        idea_id = self.store.add_idea("Put this idea aside")
        self.store.decide_idea(idea_id, "later")
        self.store.set_inbox_deleted("idea", idea_id, True)
        deleted_at = self.store.get_idea(idea_id)["deleted_at"]
        self.store.set_inbox_deleted("idea", idea_id, True)
        self.assertTrue(deleted_at)
        self.assertEqual(self.open_store().list_ideas(), [])
        self.assertEqual(self.store.list_ideas(include_deleted=True)[0]["deleted_at"], deleted_at)
        with self.assertRaises(ValueError):
            self.store.decide_idea(idea_id, "accept")
        self.assertEqual(self.store.list_jobs(), [])

        self.store.set_inbox_deleted("idea", idea_id, False)
        self.assertEqual(self.store.get_idea(idea_id)["status"], "later")
        self.store.decide_idea(idea_id, "accept")
        with self.assertRaises(ValueError):
            self.store.set_inbox_deleted("idea", idea_id, True)
        self.assertEqual(len(self.store.list_jobs()), 1)

    def test_schedule_reject_delete_and_restore_never_activate_a_rejected_draft(self):
        schedule_id = self.store.create_schedule(
            ["An owl finds its way home"], "2026-11-09", "09:00", "Asia/Dhaka", 1, title="  Owl batch  ",
        )
        schedule = self.store.get_schedule(schedule_id)
        self.assertEqual(schedule["title"], "Owl batch")
        self.assertEqual(datetime.fromisoformat(schedule["created_at"].replace("Z", "+00:00")).utcoffset(), timedelta())
        self.store.set_inbox_deleted("schedule", schedule_id, True)
        with self.assertRaises(ValueError):
            self.store.set_schedule_status(schedule_id, "active")
        self.store.set_inbox_deleted("schedule", schedule_id, False)
        self.store.set_schedule_status(schedule_id, "rejected")
        rejected_at = self.store.get_schedule(schedule_id)["rejected_at"]
        self.store.set_schedule_status(schedule_id, "rejected")
        self.assertTrue(rejected_at)
        self.store.set_inbox_deleted("schedule", schedule_id, True)
        self.assertEqual(self.store.list_schedules(), [])
        self.assertEqual(self.open_store().list_schedules(include_deleted=True)[0]["rejected_at"], rejected_at)
        self.store.set_inbox_deleted("schedule", schedule_id, False)
        with self.assertRaises(ValueError):
            self.store.set_schedule_status(schedule_id, "active")
        self.assertEqual(self.store.dispatch_due(datetime(2026, 11, 9, 3, tzinfo=timezone.utc)), 0)

    def test_publish_dates_are_the_first_confirmed_postiz_dates(self):
        idea_id = self.store.add_idea("An idea with publishing requests")
        self.store.decide_idea(idea_id, "accept")
        schedule_id = self.store.create_schedule(
            ["First batch item", "Second batch item"], "2026-11-09", "09:00", "Asia/Dhaka", 2,
        )
        self.store.set_schedule_status(schedule_id, "active")
        self.store.dispatch_due(datetime(2026, 11, 9, 3, tzinfo=timezone.utc))
        idea_job = self.store.get_idea(idea_id)["job_id"]
        first_batch_job, second_batch_job = [entry["job_id"] for entry in self.store.get_schedule(schedule_id)["entries"]]
        self.assertIsNone(self.store.get_idea(idea_id)["publish_at"])
        self.assertIsNone(self.store.get_schedule(schedule_id)["publish_at"])
        first_publish = "2026-11-14T09:00:00.000000+00:00"
        second_publish = "2026-11-15T09:00:00.000000+00:00"
        earlier_unconfirmed = "2026-11-13T09:00:00.000000+00:00"
        with self.store._connection(write=True) as db:
            db.executemany(
                """
                INSERT INTO publishing_requests (job_id, channel_id, identifier, channel_name, preview_path,
                    title, caption, hashtags_json, scheduled_at, draft, status)
                VALUES (?, ?, 'youtube', 'Channel', 'preview.mp4', 'Title', '', '[]', ?, ?, ?)
                """,
                [
                    (idea_job, "draft", earlier_unconfirmed, 1, "draft"),
                    (idea_job, "queued", earlier_unconfirmed, 0, "queued"),
                    (idea_job, "confirmed", first_publish, 0, "scheduled"),
                    (first_batch_job, "first", second_publish, 0, "scheduled"),
                    (second_batch_job, "second", first_publish, 0, "published"),
                ],
            )
        self.assertEqual(self.store.list_ideas()[0]["publish_at"], first_publish)
        self.assertEqual(self.store.list_schedules()[0]["publish_at"], first_publish)

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
        with self.assertRaises(ValueError):
            self.store.set_inbox_deleted("schedule", schedule, True)
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
            self.store.create_schedule(["Prompt"], "2026-11-09", "09:00", "Asia/Dhaka", 1, title="x" * 101)
        with self.assertRaises(ValueError):
            self.store.dispatch_due(datetime(2026, 11, 9, 3))


if __name__ == "__main__":
    unittest.main()
