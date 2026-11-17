import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.db import StudioStore
from app.niche import load_profile
from app.postiz_client import PostizError
from app.publishing import StudioPublishing
from app.settings import StudioSettings


NOW = datetime(2026, 11, 9, 3, tzinfo=timezone.utc)


class PublishingFlowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name)
        self.store = StudioStore(path / "studio.sqlite3")
        self.store.initialize()
        self.store.seed_niche(load_profile())
        settings = StudioSettings(self.store, path / ".env")
        settings.seed()
        settings.set_key("POSTIZ_API_KEY", "test-fixture-key")
        media_root = path / "media"
        self.publishing = StudioPublishing(self.store, settings, media_root, "http://127.0.0.1:4007")
        idea = self.store.add_idea("A garden tale")
        self.store.decide_idea(idea, "accept")
        self.job = self.store.list_jobs()[0]["id"]
        self.preview = media_root / str(self.job) / "preview.mp4"
        self.preview.parent.mkdir(parents=True)
        self.preview.write_bytes(b"approved local MP4 fixture")

    def set_preview(self):
        with self.store._connection(write=True) as db:
            db.execute(
                "UPDATE jobs SET status = 'preview_ready', preview_path = ? WHERE id = ?",
                (f"{self.job}/preview.mp4", self.job),
            )

    def approve(self, channel, draft):
        return self.publishing.approve(
            self.job, channel, "Garden story", "Tavi explores the garden.", "#animation, #garden",
            "2099-01-01T09:00", "Asia/Dhaka", draft,
        )

    def test_final_approval_queues_one_draft_for_each_channel(self):
        with patch("app.publishing.PostizClient") as cls:
            client = cls.return_value
            client.channels.return_value = [
                {"id": name, "identifier": name, "name": name.title()}
                for name in ("youtube", "facebook", "instagram", "tiktok")
            ]
            with self.assertRaises(ValueError):
                self.approve("youtube", True)
            self.set_preview()
            with self.assertRaises(ValueError):
                self.approve("tiktok", False)
            for channel in ("youtube", "facebook", "instagram", "tiktok"):
                first = self.approve(channel, True)
                self.assertEqual(self.approve(channel, True), first)
            client.create_post.assert_not_called()
            client.upload.return_value = {"id": "uploaded", "path": "http://localhost/media.mp4"}
            client.create_post.side_effect = ["one", "two", "three", "four"]
            for _ in range(5):
                self.publishing.process(NOW)
            self.assertEqual(client.create_post.call_count, 4)
            self.assertEqual(client.upload.call_count, 4)
            self.assertTrue(all(post["status"] == "draft" for post in self.publishing.posts.job_posts(self.job)))

    def test_disabled_publishing_category_holds_approved_post_until_reenabled(self):
        self.set_preview()
        settings = self.publishing.settings
        with patch("app.publishing.PostizClient") as cls:
            client = cls.return_value
            client.channels.return_value = [{"id": "youtube", "identifier": "youtube", "name": "YouTube"}]
            self.approve("youtube", True)
            settings.update_platform_categories("POSTIZ_API_KEY", [])

            self.publishing.process(NOW)

            client.upload.assert_not_called()
            client.create_post.assert_not_called()

            settings.update_platform_categories("POSTIZ_API_KEY", ["publishing"])
            client.upload.return_value = {"id": "uploaded", "path": "http://localhost/media.mp4"}
            client.create_post.return_value = "test-post"
            self.publishing.process(NOW)

            client.create_post.assert_called_once()
            post, = self.publishing.posts.job_posts(self.job)
            self.assertEqual(post["status"], "draft")

    def test_uncertain_scheduled_handoff_is_never_resubmitted(self):
        self.set_preview()
        with patch("app.publishing.PostizClient") as cls:
            client = cls.return_value
            client.channels.return_value = [{"id": "youtube", "identifier": "youtube", "name": "YouTube"}]
            approved = self.approve("youtube", False)
            client.upload.return_value = {"id": "uploaded", "path": "http://localhost/media.mp4"}
            client.create_post.side_effect = PostizError("Postiz response was interrupted", uncertain=True)
            self.publishing.process(NOW)
            self.assertEqual(self.approve("youtube", False), approved)
            self.publishing.process(NOW)
            client.create_post.assert_called_once()
            post, = self.publishing.posts.job_posts(self.job)
            self.assertEqual(post["status"], "needs_review")
            self.assertEqual(datetime.fromisoformat(post["scheduled_at"]), datetime(2099, 1, 1, 3, tzinfo=timezone.utc))

    def test_expired_schedule_does_not_turn_into_an_immediate_post(self):
        self.set_preview()
        with patch("app.publishing.PostizClient") as cls:
            client = cls.return_value
            client.channels.return_value = [{"id": "youtube", "identifier": "youtube", "name": "YouTube"}]
            self.approve("youtube", False)
            self.publishing.process(datetime(2099, 1, 2, tzinfo=timezone.utc))
            client.upload.assert_not_called()
            client.create_post.assert_not_called()
            post, = self.publishing.posts.job_posts(self.job)
            self.assertEqual(post["status"], "failed")

    def test_missed_handoff_is_recorded_when_postiz_is_disabled(self):
        self.set_preview()
        with patch("app.publishing.PostizClient") as cls:
            cls.return_value.channels.return_value = [{"id": "youtube", "identifier": "youtube", "name": "YouTube"}]
            self.approve("youtube", False)
            self.publishing.settings.update_platform_categories("POSTIZ_API_KEY", [])
            self.publishing.process(datetime(2099, 1, 2, tzinfo=timezone.utc))
            cls.return_value.upload.assert_not_called()
            post, = self.publishing.posts.job_posts(self.job)
            self.assertEqual(post["status"], "failed")
            self.assertIn("time passed", post["error"])

    def test_schedule_expiring_during_upload_does_not_create_post(self):
        self.set_preview()
        scheduled = datetime(2099, 1, 1, 3, tzinfo=timezone.utc)
        before = scheduled - timedelta(seconds=1)
        after = scheduled + timedelta(seconds=1)
        with patch("app.publishing.PostizClient") as cls, patch("app.publishing.datetime", wraps=datetime) as clock:
            client = cls.return_value
            client.channels.return_value = [{"id": "youtube", "identifier": "youtube", "name": "YouTube"}]
            clock.now.return_value = before
            self.approve("youtube", False)
            clock.now.side_effect = (before, after)
            client.upload.return_value = {"id": "uploaded", "path": "http://localhost/media.mp4"}
            self.publishing.process(before)
            client.upload.assert_called_once()
            client.create_post.assert_not_called()
            post, = self.publishing.posts.job_posts(self.job)
            self.assertEqual(post["status"], "failed")
            self.assertIn("time passed", post["error"])

    def test_multi_channel_schedule_uses_overrides_and_confirms_each_release(self):
        self.set_preview()
        with patch("app.publishing.PostizClient") as cls:
            client = cls.return_value
            client.channels.return_value = [
                {"id": name, "identifier": name, "name": name.title()}
                for name in ("youtube", "facebook", "instagram", "tiktok")
            ]
            base = (self.job, "Garden story", "Caption", "#garden", "2099-01-01T09:00", "Asia/Dhaka", False)
            channel_selection = ["youtube", "facebook", "instagram"]
            options = {
                "channel_times": {"facebook": "2099-01-01T10:00"},
                "expected_preview_path": f"{self.job}/preview.mp4",
            }
            with self.assertRaisesRegex(ValueError, "approved Direct Post"):
                self.publishing.approve_channels(base[0], [*channel_selection, "tiktok"], *base[1:], **options)
            self.assertEqual(self.publishing.posts.job_posts(self.job), [])
            self.assertFalse(self.publishing.channels()[-1]["can_schedule"])

            ids = self.publishing.approve_channels(base[0], channel_selection, *base[1:], **options)
            self.assertEqual(ids, self.publishing.approve_channels(base[0], channel_selection, *base[1:], **options))
            client.upload.return_value = {"id": "uploaded", "path": "https://cdn.example.com/media.mp4"}
            client.create_post.side_effect = ["yt", "fb", "ig"]
            for _ in ids:
                self.publishing.process(NOW)
            self.assertEqual(
                [call.args[5] for call in client.create_post.call_args_list],
                ["2099-01-01T03:00:00.000000+00:00", "2099-01-01T04:00:00.000000+00:00",
                 "2099-01-01T03:00:00.000000+00:00"],
            )
            self.assertEqual(client.create_post.call_count, 3)

            client.post_status.side_effect = [
                ("PUBLISHED", "https://www.youtube.com/shorts/garden"),
                ("PUBLISHED", "https://www.instagram.com/reel/garden"),
                ("ERROR", None),
            ]
            client.analytics.return_value = []
            self.publishing.process(datetime(2099, 1, 1, 4, tzinfo=timezone.utc))
            youtube, facebook, instagram = self.publishing.posts.job_posts(self.job)
            self.assertEqual([youtube["status"], facebook["status"], instagram["status"]],
                             ["published", "failed", "published"])
            self.assertIn("publishing failure", facebook["error"])
            self.assertEqual(youtube["release_url"], "https://www.youtube.com/shorts/garden")
            self.assertEqual(self.publishing.original_post_url(instagram), "https://www.instagram.com/reel/garden")
            client.original_post_url.assert_not_called()
