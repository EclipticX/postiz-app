import re
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from app.db import StudioStore
from app.niche import load_profile
from app.publishing_store import PublishingStore
from app.server import StudioServer, run_schedule_clock
from test_asset_library import REQUIRES_FFMPEG, png, wav


class StudioScheduleClockTests(unittest.TestCase):
    def test_later_stages_and_next_tick_run_after_one_stage_fails(self):
        server = Mock()
        stopped = Mock()
        stopped.is_set.side_effect = [False, False, True]
        server.production.advance.side_effect = [TypeError("Unexpected production response"), None]

        with self.assertLogs(level="ERROR") as logs:
            run_schedule_clock(server, stopped)

        self.assertEqual(server.production.advance.call_count, 2)
        self.assertEqual(server.publishing.process.call_count, 2)
        self.assertEqual(stopped.wait.call_count, 2)
        self.assertIn("media production", logs.output[0])

    def test_publishing_uses_the_time_after_production(self):
        server = Mock()
        stopped = Mock()
        stopped.is_set.side_effect = [False, True]
        before = datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc)
        after = datetime(2026, 11, 9, 3, 1, tzinfo=timezone.utc)
        with patch("app.server.datetime") as clock:
            clock.now.return_value = before

            def advance(now):
                self.assertEqual(now, before)
                clock.now.return_value = after

            server.production.advance.side_effect = advance
            run_schedule_clock(server, stopped)

        server.publishing.process.assert_called_once_with(after)


class StudioHTTPTest(unittest.TestCase):
    def setUp(self):
        data = tempfile.TemporaryDirectory()
        self.addCleanup(data.cleanup)
        self.store = StudioStore(Path(data.name) / "studio.sqlite3")
        self.store.initialize()
        self.store.seed_niche(load_profile())
        try:
            server = StudioServer(("127.0.0.1", 0), self.store)
        except PermissionError:
            self.skipTest("Local HTTP requires permission to bind a loopback socket")
        self.port = server.server_port
        self.addCleanup(server.server_close)
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        serving.start()
        self.addCleanup(serving.join)
        self.addCleanup(server.shutdown)

        status, html = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn('href="/inbox/create"', html)
        status, html = self.request("GET", "/inbox/create")
        self.assertEqual(status, 200)
        self.csrf = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    def request(self, method, path, fields=None, headers=None, *, payload=None, binary=False):
        headers = dict(headers or {})
        if fields is not None:
            payload = urlencode(fields)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request(method, path, payload, headers)
            response = connection.getresponse()
            body = response.read()
            return response.status, body if binary else body.decode("utf-8")
        finally:
            connection.close()

    def post(self, path, **fields):
        return self.request("POST", path, {"csrf_token": self.csrf, **fields})[0]

    def upload(self, path, fields, files):
        boundary = "studio-test-form-boundary"
        body = bytearray()
        for name, value in {"csrf_token": self.csrf, **fields}.items():
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        for name, (filename, data) in files.items():
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
            body.extend(b"Content-Type: application/octet-stream\r\n\r\n" + data + b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode())
        return self.request("POST", path, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                            payload=bytes(body))[0]

    def test_inbox_approval_and_job_workspace(self):
        self.assertEqual(self.post("/ideas", title="A forest visit", prompt="<script>"), 303)
        status, html = self.request("GET", "/?q=I-001&type=manual&status=pending")
        self.assertEqual(status, 200)
        self.assertIn('href="/ideas/1"', html)
        self.assertIn("A forest visit", html)
        self.assertIn("Publish date", html)
        self.assertNotIn('action="/ideas"', html)
        status, html = self.request("GET", "/ideas/1")
        self.assertEqual(status, 200)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertEqual(self.post("/ideas/1/decision", decision="later"), 303)
        self.assertEqual(self.store.list_jobs(), [])
        self.assertEqual(self.post("/ideas/1/decision", decision="accept"), 303)
        self.assertIn("A forest visit", self.request("GET", "/?status=approved")[1])
        status, html = self.request("GET", "/workspace?job=1")
        self.assertEqual(status, 200)
        self.assertIn("A forest visit", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertEqual(self.request("GET", "/studio.css")[0], 200)

    def test_prompt_schedule_requires_activation(self):
        status, html = self.request("GET", "/inbox/create?type=schedule")
        self.assertEqual(status, 200)
        self.assertIn('action="/schedules"', html)
        self.assertEqual(self.post(
            "/schedules", title="Forest and library", prompts="Forest scene\nLibrary scene", start_date="2026-11-09",
            run_at="09:00", timezone="Asia/Dhaka", daily_limit="1",
        ), 303)
        due = datetime(2026, 11, 9, 3, 15, tzinfo=timezone.utc)
        self.assertEqual(self.store.dispatch_due(due), 0)
        self.assertIn('href="/schedules/1"', self.request("GET", "/?type=schedule")[1])
        status, html = self.request("GET", "/schedules/1")
        self.assertEqual(status, 200)
        self.assertIn("Forest and library", html)
        self.assertIn("Library scene", html)
        self.assertIn("Approve and activate", html)
        self.assertEqual(self.post("/schedules/1/status", status="active"), 303)
        self.assertEqual(self.store.dispatch_due(due), 1)
        self.assertIn("Forest scene", self.request("GET", "/workspace?job=1")[1])

    def test_deleted_inbox_record_can_be_restored_from_its_details(self):
        self.assertEqual(self.post("/ideas", title="A reversible draft", prompt="Demo prompt"), 303)
        self.assertEqual(self.post("/ideas/1/archive", action="delete"), 303)
        self.assertNotIn("A reversible draft", self.request("GET", "/")[1])
        self.assertIn("A reversible draft", self.request("GET", "/?status=deleted")[1])
        self.assertIn("Restore", self.request("GET", "/ideas/1")[1])
        self.assertEqual(self.post("/ideas/1/archive", action="restore"), 303)
        self.assertIn("A reversible draft", self.request("GET", "/")[1])
        self.assertNotIn("A reversible draft", self.request("GET", "/?status=deleted")[1])
        self.assertEqual(self.request("GET", "/ideas/999")[0], 404)

    def test_published_list_links_to_platform_history_and_the_delivered_video(self):
        idea = self.store.add_idea("A garden video")
        self.store.decide_idea(idea, "accept")
        job_id = self.store.list_jobs()[0]["id"]
        detail = f"/published-posts/{job_id}"
        delivered = f"{job_id}/preview.mp4"
        media = self.store.path.parent / "media" / delivered
        media.parent.mkdir(parents=True)
        media.write_bytes(b"0123456789")
        with self.store._connection(write=True) as db:
            db.execute(
                "UPDATE jobs SET status = 'preview_ready', preview_path = ? WHERE id = ?", (delivered, job_id),
            )
        posts = PublishingStore(self.store)
        release = datetime(2026, 11, 9, 3, tzinfo=timezone.utc)
        youtube = posts.queue(job_id, {"id": "yt", "identifier": "youtube"}, "Title", "YouTube caption", [], release)
        posts.next_request()
        posts.finish(youtube, "youtube-post")
        self.assertEqual(self.request("GET", detail)[0], 404)
        self.assertNotIn("A garden video", self.request("GET", "/published-posts")[1])
        posts.record_analytics(youtube, {"views": 20, "likes": 3, "comments": 0}, release)

        instagram = posts.queue(
            job_id, {"id": "ig", "identifier": "instagram"}, "Title", "Instagram caption", ["garden"], release,
        )
        posts.next_request()
        posts.finish(instagram, "instagram-post")
        posts.record_analytics(instagram, {"views": 10, "likes": 2, "comments": None}, release)
        with self.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET preview_path = ? WHERE id = ?", (f"{job_id}/new-preview.mp4", job_id))

        status, html = self.request("GET", "/published-posts?platform=instagram&q=P-001")
        self.assertEqual(status, 200)
        self.assertEqual(html.count(f'href="{detail}"'), 1)
        self.assertIn("A garden video", html)
        self.assertIn("Partial total", html)
        status, html = self.request("GET", f"{detail}?channel={instagram}&metric=views")
        self.assertEqual(status, 200)
        self.assertIn("Instagram caption", html)
        self.assertIn("Views totals by snapshot date", html)
        self.assertIn(f'src="/media/{delivered}"', html)
        self.assertEqual(self.request("GET", f"/media/{delivered}", headers={"Range": "bytes=1-3"}, binary=True), (206, b"123"))

        with patch("app.publishing.StudioPublishing.original_post_url", return_value="https://www.youtube.com/shorts/example") as original:
            self.assertEqual(self.request("GET", f"{detail}/original/{youtube}")[0], 303)
            self.assertEqual(original.call_args.args[0]["post_id"], "youtube-post")
            self.assertEqual(self.request("GET", f"{detail}/original/999")[0], 404)
            self.assertEqual(original.call_count, 1)

    def test_rejects_cross_site_posts_and_nonlocal_host(self):
        self.assertEqual(self.request("POST", "/ideas", {"prompt": "Unapproved"})[0], 403)
        self.assertEqual(self.request("GET", "/", headers={"Host": "other.example"})[0], 403)
        self.assertEqual(self.store.list_ideas(), [])

    def test_settings_add_replace_and_remove_keys_with_masking(self):
        path = "/settings/keys/ANTHROPIC_API_KEY"
        self.assertEqual(self.post(path, action="save", value="fixture-first"), 303)
        status, html = self.request("GET", "/settings")
        self.assertEqual(status, 200)
        self.assertIn("Configured · ••••••••", html)
        self.assertNotIn("fixture-first", html)
        self.assertEqual(self.post(path, action="save", value="fixture-replacement"), 303)
        self.assertEqual(self.post(path, action="remove"), 303)
        self.assertNotIn("fixture-replacement", self.request("GET", "/settings")[1])
        self.assertNotIn("Configured · ••••••••", self.request("GET", "/settings")[1])

    def test_workspace_preview_supports_byte_ranges_and_only_serves_registered_media(self):
        idea = self.store.add_idea("A preview test")
        self.store.decide_idea(idea, "accept")
        job_id = self.store.list_jobs()[0]["id"]
        preview = self.store.path.parent / "media" / str(job_id) / "preview.mp4"
        preview.parent.mkdir(parents=True)
        preview.write_bytes(b"example")
        with self.store._connection(write=True) as db:
            db.execute(
                "UPDATE jobs SET status = 'preview_ready', preview_path = ? WHERE id = ?",
                (f"{job_id}/preview.mp4", job_id),
            )
        self.assertEqual(
            self.request("GET", f"/media/{job_id}/preview.mp4", headers={"Range": "bytes=1-3"}),
            (206, "xam"),
        )
        self.assertIn("Load connected Postiz channels", self.request("GET", f"/workspace?job={job_id}")[1])
        self.assertEqual(self.request("GET", f"/media/{job_id}/.env")[0], 404)

    def test_publishing_approval_rejects_a_stale_preview_form(self):
        idea = self.store.add_idea("A garden tale")
        self.store.decide_idea(idea, "accept")
        job_id = self.store.list_jobs()[0]["id"]
        original = f"{job_id}/preview.mp4"
        replacement = f"{job_id}/preview.revised.mp4"
        with self.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET status = 'preview_ready', preview_path = ? WHERE id = ?", (original, job_id))

        channels = [{"id": "youtube", "identifier": "youtube", "name": "YouTube"}]
        with patch("app.publishing.StudioPublishing.channels", return_value=channels):
            _, html = self.request("GET", f"/workspace?job={job_id}&channels=1")
            reviewed = re.search(r'name="preview_path" value="([^"]+)"', html).group(1)
            self.assertEqual(reviewed, original)
            with self.store._connection(write=True) as db:
                db.execute("UPDATE jobs SET preview_path = ? WHERE id = ?", (replacement, job_id))

            fields = {"channel": "youtube", "mode": "draft", "title": "A garden tale"}
            path = f"/workspace/{job_id}/publish"
            self.assertEqual(self.post(path, preview_path=reviewed, **fields), 400)
            self.assertEqual(self.post(path, **fields), 400)
            self.assertEqual(self.post(path, preview_path=replacement, **fields), 303)
        with self.store._connection() as db:
            previews = db.execute("SELECT preview_path FROM publishing_requests").fetchall()
        self.assertEqual([row["preview_path"] for row in previews], [replacement])

    @REQUIRES_FFMPEG
    def test_asset_upload_and_per_video_options_through_http(self):
        self.assertEqual(self.post("/settings/assets", asset_id="mother", kind="character", name="Mother",
                                   description="Clay character with blue glasses"), 303)
        ref_path = "/settings/assets/mother/references"
        fields = {"angle": "front", "variant_id": "", "primary": "yes"}
        self.assertEqual(self.upload(ref_path, {**fields, "csrf_token": "wrong"}, {"image": ("mother.png", png())}), 403)
        self.assertEqual(self.upload(ref_path, fields, {"image": ("../../untrusted.png", png())}), 303)
        self.assertEqual(self.upload(ref_path, {**fields, "angle": "side", "primary": ""},
                                     {"image": ("side.png", png(70))}), 303)
        status, html = self.request("GET", "/settings")
        self.assertEqual(status, 200)
        self.assertIn("Asset Library", html)
        references = re.findall(r'src="(/asset-reference/mother/[^" ]+)"', html)
        self.assertEqual(len(references), 2)
        self.assertEqual(self.request("GET", references[0], binary=True), (200, png()))
        self.assertEqual(self.post("/settings/assets/mother/voices", style_id="warm", name="Warm", voice="Rachel",
                                   speed="0.95", expressiveness="0.2", is_default="yes"), 303)
        self.assertEqual(self.upload("/settings/presets/audio", {"preset_id": "opening", "name": "Opening"},
                                     {"sfx": ("opening.wav", wav())}), 303)
        self.assertEqual(self.post("/settings/presets/audio/opening/default"), 303)
        idea = self.store.add_idea("A garden story")
        self.store.decide_idea(idea, "accept")
        job = self.store.list_jobs()[0]["id"]
        self.assertEqual(self.post(f"/workspace/{job}/options", **{"voice.mother": "warm", "preset.audio": "opening"}), 303)
        self.assertIn('value="warm" selected', self.request("GET", f"/workspace?job={job}")[1])
