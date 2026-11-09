import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import StudioStore
from app.publishing_store import PublishingStore


class PublishingStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "studio.sqlite3"
        self.store = StudioStore(self.path)
        self.store.initialize()
        self.posts = PublishingStore(self.store)
        self.when = datetime(2026, 11, 10, 3, tzinfo=timezone.utc)
        self.channel = {"id": "youtube-1", "identifier": "youtube", "name": "Stories"}
        idea = self.store.add_idea("Make a short")
        self.store.decide_idea(idea, "accept")
        self.job = self.store.list_ideas()[0]["job_id"]

    def preview(self, status="preview_ready", path="1/preview.mp4"):
        with self.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET status = ?, preview_path = ? WHERE id = ?", (status, path, self.job))

    def approve(self, channel=None, *, draft=False):
        return self.posts.queue(
            self.job, channel or self.channel, "A story", "Caption", ["garden"],
            None if draft else self.when, draft,
        )

    def test_only_final_preview_can_be_approved(self):
        with self.assertRaisesRegex(ValueError, "final video preview"):
            self.approve()
        self.preview(status="qa_pending")
        with self.assertRaisesRegex(ValueError, "final video preview"):
            self.approve()
        self.preview(status="needs_review", path=None)
        with self.assertRaisesRegex(ValueError, "final video preview"):
            self.approve()
        self.preview(status="needs_review")
        approved = self.approve()
        post = self.posts.next_request()
        self.assertEqual(post["id"], approved)
        self.assertEqual(post["status"], "sending")
        self.assertEqual(post["preview_path"], "1/preview.mp4")
        self.assertEqual(post["hashtags"], ["garden"])

    def test_repeat_approval_and_concurrent_workers_do_not_duplicate(self):
        self.preview()
        with ThreadPoolExecutor(max_workers=4) as workers:
            approved = list(workers.map(lambda _: self.approve(), range(8)))
        self.assertEqual(len(set(approved)), 1)
        with self.assertRaisesRegex(ValueError, "already has a publishing request"):
            self.posts.queue(self.job, self.channel, "Different title", "Caption", [], self.when)

        with ThreadPoolExecutor(max_workers=4) as workers:
            claimed = [record for record in workers.map(lambda _: self.posts.next_request(), range(8)) if record]
        self.assertEqual(len(claimed), 1)
        self.posts.finish(approved[0], "post-1")
        self.assertEqual(self.approve(), approved[0])
        post, = self.posts.job_posts(self.job)
        self.assertEqual(post["status"], "scheduled")
        self.assertEqual(post["post_id"], "post-1")
        self.assertIsNone(self.posts.next_request())

    def test_changed_approved_preview_is_held_and_never_sent(self):
        self.preview()
        self.approve()
        self.preview(path="1/revised.mp4")
        self.assertIsNone(self.posts.next_request())
        post, = self.posts.job_posts(self.job)
        self.assertEqual(post["status"], "failed")
        self.assertIn("preview changed", post["error"])

    def test_interrupt_and_uncertainty_never_trigger_automatic_resubmission(self):
        self.preview()
        interrupted = self.approve()
        self.assertEqual(self.posts.next_request()["id"], interrupted)
        restarted_store = StudioStore(self.path)
        restarted_store.initialize()
        restarted = PublishingStore(restarted_store)
        self.assertEqual(restarted.recover_interrupted(), 1)
        self.assertIsNone(restarted.next_request())
        self.assertEqual(restarted.job_posts(self.job)[0]["status"], "needs_review")

        other = self.approve({"id": "tiktok-1", "identifier": "tiktok", "name": "TikTok Stories"})
        self.assertEqual(self.posts.next_request()["id"], other)
        self.posts.fail(other, "Connection ended during the send.", uncertain=True)
        self.assertIsNone(restarted.next_request())
        self.assertEqual([post["status"] for post in restarted.job_posts(self.job)], ["needs_review", "needs_review"])

    def test_draft_excluded_and_analytics_preserve_missing_metrics(self):
        self.preview()
        scheduled = self.approve()
        draft = self.approve({"id": "facebook-1", "identifier": "facebook", "name": "Garden Page"}, draft=True)
        for request_id in (scheduled, draft):
            self.assertEqual(self.posts.next_request()["id"], request_id)
            self.posts.finish(request_id, f"post-{request_id}")
        self.assertEqual(self.posts.due_analytics(self.when - timedelta(seconds=1)), [])
        self.assertEqual([post["id"] for post in self.posts.due_analytics(self.when)], [scheduled])
        self.posts.defer_analytics(scheduled, self.when)
        self.assertEqual(self.posts.due_analytics(self.when + timedelta(minutes=59)), [])

        observed = self.when + timedelta(hours=1)
        self.assertEqual(self.posts.due_analytics(observed)[0]["id"], scheduled)
        self.posts.record_analytics(scheduled, {"views": 0, "likes": None}, observed)
        scheduled_post, draft_post = self.posts.job_posts(self.job)
        self.assertEqual(scheduled_post["metrics"], {"views": 0, "likes": None})
        self.assertEqual(scheduled_post["status"], "published")
        self.assertIsNone(scheduled_post["analytics_error"])
        self.assertEqual(draft_post["status"], "draft")
        self.assertIsNone(draft_post["metrics"])
        self.assertEqual(self.posts.due_analytics(observed), [])
        self.assertEqual(self.posts.due_analytics(observed + timedelta(days=1))[0]["id"], scheduled)

    def test_validates_channel_and_schedule_timezone(self):
        self.preview()
        with self.assertRaisesRegex(ValueError, "channel type"):
            self.approve({"id": "unknown-1", "identifier": "unknown", "name": "Unknown"})
        with self.assertRaisesRegex(ValueError, "with a timezone"):
            self.posts.queue(self.job, self.channel, "A story", "", [], "2026-11-10T09:00")
        request_id = self.posts.queue(self.job, self.channel, "A story", "", "#garden, #stories", "2026-11-10T09:00+06:00")
        post, = self.posts.job_posts(self.job)
        self.assertEqual(post["id"], request_id)
        self.assertEqual(post["scheduled_at"], "2026-11-10T03:00:00.000000+00:00")
        self.assertEqual(post["hashtags"], ["#garden", "#stories"])


if __name__ == "__main__":
    unittest.main()
