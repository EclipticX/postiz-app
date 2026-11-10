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

    def approve(self, channel=None, *, draft=False, expected_preview_path=None):
        return self.posts.queue(
            self.job, channel or self.channel, "A story", "Caption", ["garden"],
            None if draft else self.when, draft, expected_preview_path=expected_preview_path,
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

    def test_explicit_approval_rejects_missing_or_stale_preview_path(self):
        self.preview(path="1/revised.mp4")
        for path in ("", "1/preview.mp4"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "Reload the workspace"):
                self.approve(expected_preview_path=path)
        self.assertEqual(self.posts.job_posts(self.job), [])
        approved = self.approve(expected_preview_path="1/revised.mp4")
        self.assertEqual(self.posts.next_request()["id"], approved)

    def test_confirmed_failure_can_be_reapproved_with_updated_details(self):
        self.preview()
        approved = self.approve(expected_preview_path="1/preview.mp4")
        self.assertEqual(self.posts.next_request()["id"], approved)
        self.posts.fail(approved, "Upload failed.")
        self.preview(path="1/revised.mp4")

        with self.assertRaisesRegex(ValueError, "Reload the workspace"):
            self.approve(expected_preview_path="1/preview.mp4")
        self.assertEqual(self.posts.job_posts(self.job)[0]["status"], "failed")

        channel = {**self.channel, "name": "Updated Stories"}
        later = self.when + timedelta(hours=1)
        reapproved = self.posts.queue(
            self.job, channel, "Updated story", "New caption", ["updated"],
            later, expected_preview_path="1/revised.mp4",
        )
        self.assertEqual(reapproved, approved)
        post = self.posts.next_request()
        self.assertEqual(post["id"], approved)
        self.assertEqual(post["preview_path"], "1/revised.mp4")
        self.assertEqual(post["channel_name"], "Updated Stories")
        self.assertEqual(post["title"], "Updated story")
        self.assertEqual(post["caption"], "New caption")
        self.assertEqual(post["hashtags"], ["updated"])
        self.assertEqual(datetime.fromisoformat(post["scheduled_at"]), later)
        self.assertIsNone(post["error"])
        self.assertIsNone(self.posts.next_request())

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

    def test_published_posts_only_include_confirmed_releases(self):
        self.preview()
        later = self.when + timedelta(days=1)
        second = self.posts.queue(
            self.job, {**self.channel, "id": "youtube-2"}, "Second release", "Another caption", [],
            later,
        )
        first = self.approve()
        draft = self.approve({"id": "facebook-1", "identifier": "facebook", "name": "Garden Page"}, draft=True)
        scheduled = self.approve({**self.channel, "id": "youtube-3"})
        for request_id in (second, first, draft, scheduled):
            self.assertEqual(self.posts.next_request()["id"], request_id)
            self.posts.finish(request_id, f"post-{request_id}")

        self.assertEqual(self.posts.published_posts(), [])
        self.posts.record_analytics(first, {"views": 0, "likes": None}, self.when)
        self.posts.record_analytics(second, {"views": 4}, later)
        releases = self.posts.published_posts()
        self.assertEqual([post["id"] for post in releases], [second, first])
        self.assertEqual(self.posts.published_posts(self.job), releases)
        self.assertEqual(self.posts.published_posts(self.job + 1), [])
        self.assertEqual(releases[1]["title"], "A story")
        self.assertEqual(releases[1]["job_title"], self.store.get_job(self.job)["title"])
        self.assertEqual(releases[1]["preview_path"], "1/preview.mp4")
        self.assertEqual(releases[1]["metrics"], {"views": 0, "likes": None})

        with self.store._connection(write=True) as db:
            db.execute("DELETE FROM analytics WHERE post_id = ?", (f"post-{second}",))
        release = self.posts.published_posts()[0]
        self.assertIsNone(release["metrics"])
        self.assertIsNone(release["collected_at"])
        self.assertEqual(self.posts.published_history(release), [])

    def test_published_history_limits_and_scopes_snapshots(self):
        self.preview()
        request_id = self.approve()
        self.posts.next_request()
        self.posts.finish(request_id, "shared-post")
        for day in range(15):
            self.posts.record_analytics(
                request_id, {"views": 0 if day == 1 else day, "comments": None},
                self.when + timedelta(days=min(day, 13)),
            )

        idea = self.store.add_idea("Another video")
        self.store.decide_idea(idea, "accept")
        another_job = self.store.list_ideas()[0]["job_id"]
        with self.store._connection(write=True) as db:
            db.executemany(
                "INSERT INTO analytics (job_id, platform, post_id, metrics_json, collected_at) VALUES (?, ?, ?, ?, ?)",
                [
                    (job_id, platform, post_id, '{"views": 999}', "2027-01-01T00:00:00.000000+00:00")
                    for job_id, platform, post_id in (
                        (another_job, "youtube", "shared-post"),
                        (self.job, "instagram", "shared-post"),
                        (self.job, "youtube", "another-post"),
                    )
                ],
            )

        post, = self.posts.published_posts(self.job)
        self.assertEqual(post["metrics"], {"views": 14, "comments": None})
        history = self.posts.published_history(post)
        self.assertEqual([snapshot["metrics"]["views"] for snapshot in history], [0, *range(2, 15)])
        self.assertIsNone(history[0]["metrics"]["comments"])
        self.assertEqual(history[-1]["collected_at"], post["collected_at"])

    def test_delivered_preview_paths_exclude_drafts_and_unsent_requests(self):
        self.preview(path="1/scheduled.mp4")
        scheduled = self.approve()
        self.posts.next_request()
        self.assertEqual(self.posts.delivered_preview_paths(self.job), set())
        self.posts.finish(scheduled, "scheduled-post")

        self.preview(path="1/draft.mp4")
        draft = self.approve({"id": "facebook-1", "identifier": "facebook", "name": "Garden Page"}, draft=True)
        self.posts.next_request()
        self.posts.finish(draft, "draft-post")
        self.preview(path="1/queued.mp4")
        self.approve({**self.channel, "id": "youtube-2"})

        self.assertEqual(self.posts.delivered_preview_paths(self.job), {"1/scheduled.mp4"})
        self.assertEqual(self.posts.delivered_preview_paths(self.job + 1), set())
        self.posts.record_analytics(scheduled, {"views": 0}, self.when)
        self.assertEqual(self.posts.delivered_preview_paths(self.job), {"1/scheduled.mp4"})

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
