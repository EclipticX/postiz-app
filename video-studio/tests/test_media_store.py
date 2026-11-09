import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from app.db import StudioStore
from app.media_store import MediaStore
from app.settings import StudioSettings


class MediaStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "studio.sqlite3"
        self.store = StudioStore(self.path)
        self.store.initialize()
        self.settings = StudioSettings(self.store, self.path.with_name(".env"))
        self.settings.seed()
        self.settings.set_budgets(1_000, 1_000)
        self.media = MediaStore(self.store)
        self.now = datetime(2026, 11, 9, 3, tzinfo=timezone.utc)

    def planned_job(self):
        idea = self.store.add_idea("Make a short")
        self.store.decide_idea(idea, "accept")
        job = self.store.list_ideas()[0]["job_id"]
        scenes = [
            {"scene_id": "scene_01", "dialogue": "Let's water the garden."},
            {"scene_id": "scene_02", "dialogue": ""},
        ]
        with self.store._connection(write=True) as db:
            db.execute(
                "UPDATE jobs SET status = 'planned', plan_json = ? WHERE id = ?",
                (json.dumps(scenes), job),
            )
        return job

    def begin(self, task):
        provider = {
            "keyframe": "fal-nano-banana-pro",
            "video": "fal-veo",
            "voice": "fal-elevenlabs",
        }[task["kind"]]
        reservation = self.settings.reserve(task["job_id"], provider, 10, self.now)
        self.assertTrue(self.media.begin(task["id"], provider, reservation))
        return reservation

    def test_keyframes_gate_video_and_voice_and_advance_job_to_render(self):
        job = self.planned_job()
        self.assertEqual(self.media.queue_keyframes(), 1)
        self.assertEqual(self.media.queue_keyframes(), 0)
        first, second = self.media.tasks()
        self.assertEqual([task["kind"] for task in (first, second)], ["keyframe", "keyframe"])
        self.assertEqual(first["job_status"], "making_keyframes")
        with self.assertRaises(ValueError):
            self.media.approve_keyframes(job)

        for task in (first, second):
            reservation = self.begin(task)
            self.media.pending(task["id"], f"provider-{task['id']}")
            self.media.complete(task["id"], f"{task['scene_id']}.png")
            self.settings.settle(reservation)
        self.assertEqual(self.media.tasks(), [])
        self.assertEqual(self.store.get_job(job)["status"], "review_keyframes")

        self.media.approve_keyframes(job)
        with self.assertRaises(ValueError):
            self.media.approve_keyframes(job)
        tasks = self.media.tasks()
        self.assertEqual(
            [(task["scene_id"], task["kind"]) for task in tasks],
            [("scene_01", "video"), ("scene_01", "voice"), ("scene_02", "video")],
        )
        for task in tasks:
            reservation = self.begin(task)
            self.media.complete(task["id"], f"{task['scene_id']}-{task['kind']}.mp4")
            self.settings.settle(reservation)
        self.assertEqual(self.store.get_job(job)["status"], "render_ready")
        self.assertTrue(all(task["local_path"] for task in self.media.job_tasks(job)))

    def test_begin_claims_once_and_uses_matching_active_reservation(self):
        job = self.planned_job()
        other_job = self.planned_job()
        self.media.queue_keyframes()
        first = self.media.job_tasks(job)[0]
        other_reservation = self.settings.reserve(other_job, "fal-nano-banana-pro", 10, self.now)
        self.assertFalse(self.media.begin(first["id"], "fal-nano-banana-pro", other_reservation))
        reservation = self.settings.reserve(job, "fal-nano-banana-pro", 10, self.now)
        self.assertFalse(self.media.begin(first["id"], "fal-veo", reservation))
        with ThreadPoolExecutor(max_workers=4) as executor:
            claims = list(executor.map(
                lambda _: self.media.begin(first["id"], "fal-nano-banana-pro", reservation), range(8),
            ))
        self.assertEqual(sum(claims), 1)
        self.assertEqual(self.media.job_tasks(job)[0]["attempts"], 1)

    def test_retries_remember_provider_and_stop_after_three_attempts(self):
        job = self.planned_job()
        self.media.queue_keyframes()
        task = self.media.job_tasks(job)[0]
        self.media.blocked(task["id"], "Budget unavailable.")
        self.assertEqual(self.media.job_tasks(job)[0]["attempts"], 0)
        self.assertEqual(self.media.job_tasks(job)[0]["error"], "Budget unavailable.")

        for attempt in range(1, 4):
            reservation = self.begin(task)
            self.media.pending(task["id"], f"provider-{attempt}")
            self.settings.settle(reservation)
            self.media.problem(task["id"], "Provider processing failed.", "queued")
            saved = self.media.job_tasks(job)[0]
            self.assertEqual(saved["attempts"], attempt)
            self.assertEqual(saved["provider_name"], "fal-nano-banana-pro")
            if attempt < 3:
                self.assertEqual(saved["status"], "queued")
                self.assertIsNone(saved["provider_task_id"])
                self.assertIsNone(saved["reservation_id"])

        self.assertEqual(saved["status"], "failed")
        self.assertEqual(self.store.get_job(job)["status"], "failed")
        self.assertEqual(self.store.get_job(job)["stage_error"], "Provider processing failed.")
        self.assertEqual(self.media.tasks(), [])

    def test_restart_preserves_pending_task_and_holds_unknown_submission(self):
        job = self.planned_job()
        self.media.queue_keyframes()
        pending, unknown = self.media.job_tasks(job)
        reservation = self.begin(pending)
        self.media.pending(pending["id"], "existing-provider-job")
        self.begin(unknown)

        restarted_store = StudioStore(self.path)
        restarted_store.initialize()
        restarted_media = MediaStore(restarted_store)
        self.assertEqual(restarted_media.recover_interrupted(), 1)
        task, = restarted_media.tasks()
        self.assertEqual(task["provider_task_id"], "existing-provider-job")
        self.assertEqual(task["reservation_id"], reservation)
        self.assertEqual(task["job_status"], "needs_review")
        self.assertEqual(restarted_media.job_tasks(job)[1]["status"], "needs_review")
        self.assertEqual(restarted_media.recover_interrupted(), 0)
        restarted_media.complete(pending["id"], "existing.png")
        self.assertEqual(restarted_store.get_job(job)["status"], "needs_review")

    def test_bad_saved_plan_does_not_stop_other_jobs(self):
        invalid = self.planned_job()
        valid = self.planned_job()
        with self.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET plan_json = '{}' WHERE id = ?", (invalid,))
        self.assertEqual(self.media.queue_keyframes(), 1)
        self.assertEqual(self.store.get_job(invalid)["status"], "needs_review")
        self.assertEqual(self.store.get_job(valid)["status"], "making_keyframes")


if __name__ == "__main__":
    unittest.main()
