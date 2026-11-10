import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path

from app.db import StudioStore
from app.publishing_store import PublishingStore
from app.server import StudioServer
from app.settings import StudioSettings
from app.watchdog import get_issue, list_issues


class StudioWatchDogTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "studio.sqlite3"
        self.store = StudioStore(path)
        self.store.initialize()
        self.settings = StudioSettings(self.store, path.with_name(".env"))
        self.settings.seed()

    def make_job(self):
        idea_id = self.store.add_idea("A short test video")
        self.store.decide_idea(idea_id, "accept")
        return self.store.list_jobs()[0]["id"]

    def add_failed_media_task(self, job_id):
        with self.store._connection(write=True) as db:
            return db.execute(
                """INSERT INTO media_tasks (job_id, scene_id, kind, status, attempts, error)
                VALUES (?, 'scene-1', 'video', 'failed', 2, 'Provider rate limit reached.')""",
                (job_id,),
            ).lastrowid

    def test_collects_existing_issues_without_exposing_raw_diagnostics(self):
        job_id = self.make_job()
        media_id = self.add_failed_media_task(job_id)
        private_detail = "/tmp/private-render-input.mp4?token=fixture-secret"
        idea_error = "Provider denied access. Check the API key and model permissions."
        publish_error = "Postiz rate limit reached. Try again later."
        with self.store._connection(write=True) as db:
            idea_id = db.execute(
                "INSERT INTO idea_generation_requests (trends, status, error) VALUES ('Test', 'failed', ?)",
                (idea_error,),
            ).lastrowid
            db.execute(
                "UPDATE jobs SET status = 'needs_review', plan_json = '[]', stage_error = ?, preview_path = ? WHERE id = ?",
                (f"ffmpeg failed. {private_detail}", f"{job_id}/preview.mp4", job_id),
            )

        posts = PublishingStore(self.store)
        release = datetime(2026, 11, 10, 3, tzinfo=timezone.utc)
        failed = posts.queue(job_id, {"id": "yt-a", "identifier": "youtube"}, "Video", "", [], release)
        self.assertEqual(posts.next_request()["id"], failed)
        posts.fail(failed, publish_error)
        scheduled = posts.queue(job_id, {"id": "yt-b", "identifier": "youtube"}, "Video", "", [], release)
        self.assertEqual(posts.next_request()["id"], scheduled)
        posts.finish(scheduled, "test-post")
        posts.defer_analytics(scheduled, release)

        issues = {issue["id"]: issue for issue in list_issues(self.store, self.settings)}
        self.assertEqual(set(issues), {
            f"idea-{idea_id}", f"job-{job_id}", f"media-{media_id}", f"publish-{failed}", f"analytics-{scheduled}",
        })
        self.assertEqual(issues[f"idea-{idea_id}"]["reason"], idea_error)
        self.assertEqual(issues[f"publish-{failed}"]["reason"], publish_error)
        self.assertEqual(issues[f"media-{media_id}"]["attempts"], 2)
        self.assertEqual(issues[f"job-{job_id}"]["origin_url"], f"/workspace?job={job_id}")
        self.assertIn("FFmpeg", issues[f"job-{job_id}"]["reason"])
        self.assertNotIn("private-render-input.mp4", repr(issues))
        self.assertNotIn("fixture-secret", repr(issues))
        self.assertTrue(all(issue["occurred_at"] is None for issue in issues.values()))
        self.assertEqual(get_issue(self.store, self.settings, f"media-{media_id}"), issues[f"media-{media_id}"])

    def test_only_enabled_unready_providers_are_reported(self):
        self.settings.update_provider("fal-veo", True, 10, 10)
        self.settings.set_key("ANTHROPIC_API_KEY", "anthropic-test-credential")
        self.settings.set_key("POSTIZ_API_KEY", "postiz-test-credential")
        self.settings.update_provider("evolink-seedance", False, 20, 0)

        issues = {issue["id"]: issue for issue in list_issues(self.store, self.settings)}
        self.assertEqual(set(issues), {"provider-fal-veo", "provider-claude-sonnet"})
        self.assertIn("API key", issues["provider-fal-veo"]["reason"])
        self.assertIn("estimated rate", issues["provider-claude-sonnet"]["reason"])
        self.assertEqual(issues["provider-fal-veo"]["status"], "blocked")
        self.assertNotIn("test-credential", repr(issues))
        self.settings.update_provider("claude-sonnet", True, 10, 10)
        self.assertIsNone(get_issue(self.store, self.settings, "provider-claude-sonnet"))

    def test_list_is_bounded_but_an_older_issue_can_still_be_opened(self):
        with self.store._connection(write=True) as db:
            db.executemany(
                "INSERT INTO idea_generation_requests (trends, status, error) VALUES (?, 'failed', 'Provider connection failed.')",
                [(f"Trend {i}",) for i in range(30)],
            )

        issues = list_issues(self.store, self.settings)
        self.assertEqual(len(issues), 25)
        self.assertEqual(issues[0]["id"], "idea-30")
        self.assertNotIn("idea-1", {issue["id"] for issue in issues})
        self.assertEqual(get_issue(self.store, self.settings, "idea-1")["id"], "idea-1")

    def test_sidebar_links_to_report_and_report_links_to_details(self):
        job_id = self.make_job()
        media_id = self.add_failed_media_task(job_id)
        self.settings.update_provider("fal-veo", True, 10, 10)
        try:
            server = StudioServer(("127.0.0.1", 0), self.store)
        except PermissionError:
            self.skipTest("Local HTTP requires permission to bind a loopback socket")
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        serving.start()
        self.addCleanup(server.server_close)
        self.addCleanup(serving.join)
        self.addCleanup(server.shutdown)

        def request(path, headers=None):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                connection.request("GET", path, headers=headers or {})
                response = connection.getresponse()
                return response.status, response.read().decode("utf-8")
            finally:
                connection.close()

        path = f"/watch-dog/media-{media_id}"
        self.assertIn('href="/watch-dog"', request("/")[1])
        status, page = request("/watch-dog")
        self.assertEqual(status, 200)
        self.assertIn('href="/watch-dog" aria-current="page"', page)
        self.assertIn("<table", page)
        self.assertIn(f'href="{path}"', page)
        self.assertIn("Provider rate limit reached.", page)

        status, page = request(path)
        self.assertEqual(status, 200)
        self.assertIn("Back to Watch Dog", page)
        self.assertIn("Provider rate limit reached.", page)
        self.assertIn("Attempts", page)
        self.assertIn(f'href="/workspace?job={job_id}"', page)
        status, page = request("/watch-dog/provider-fal-veo")
        self.assertEqual(status, 200)
        self.assertIn('href="/providers"', page)
        self.assertIn(request("/providers")[0], (200, 303))
        self.assertEqual(request("/watch-dog/media-999")[0], 404)
        self.assertEqual(request("/watch-dog/media-9223372036854775808")[0], 404)
        self.assertEqual(request(path, {"Host": "outside.example"})[0], 403)
