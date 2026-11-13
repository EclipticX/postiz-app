import json
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

    def upload(self, path, fields, files, *, accept=None):
        boundary = "studio-test-form-boundary"
        body = bytearray()
        for name, value in {"csrf_token": self.csrf, **fields}.items():
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        for name, (filename, data) in files.items():
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
            body.extend(b"Content-Type: application/octet-stream\r\n\r\n" + data + b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode())
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if accept:
            headers["Accept"] = accept
        return self.request("POST", path, headers=headers, payload=bytes(body))[0]

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
        status, html = self.request("GET", "/ideas/1")
        self.assertNotIn("Open video job", html)
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

    def test_providers_add_replace_and_remove_keys_with_masking(self):
        path = "/settings/keys/ANTHROPIC_API_KEY"
        self.assertEqual(self.post(path, action="save", value="fixture-first"), 303)
        status, html = self.request("GET", "/providers")
        self.assertEqual(status, 200)
        self.assertIn('href="/providers" aria-current="page"', html)
        self.assertIn("Configured · ••••••••", html)
        self.assertNotIn("fixture-first", html)
        self.assertNotIn("Save key", self.request("GET", "/settings")[1])
        self.assertEqual(self.post(path, action="save", value="fixture-replacement"), 303)
        self.assertEqual(self.post(path, action="remove"), 303)
        self.assertNotIn("fixture-replacement", self.request("GET", "/providers")[1])
        self.assertNotIn("Configured · ••••••••", self.request("GET", "/providers")[1])

    def test_providers_category_tabs_and_run_order(self):
        status, js = self.request("GET", "/providers.js")
        self.assertEqual(status, 200)
        self.assertIn("switchTab", js)
        self.assertIn("data-remove-key", js)
        self.assertIn("data-remove-platform", js)

        status, html = self.request("GET", "/providers?tab=publishing")
        self.assertEqual(status, 200)
        self.assertIn('data-tab="publishing" role="tab" aria-selected="true"', html)
        self.assertIn("Run order", html)
        self.assertIn('data-tab="audio"', html)
        self.assertIn('data-tab="api-keys"', html)
        self.assertIn("Add platform", html)
        self.assertIn('id="open-add-platform-dialog"', html)
        self.assertIn('id="add-platform-dialog"', html)
        self.assertIn("data-disable-provider", html)
        # Verify no inline event handlers violating CSP
        self.assertNotIn("onclick=", html)
        self.assertNotIn("onchange=", html)

        status, html = self.request("GET", "/providers?tab=audio")
        self.assertEqual(status, 200)
        self.assertIn('data-tab="audio" role="tab" aria-selected="true"', html)
        self.assertIn("Run order", html)

    def test_providers_add_and_remove_platform(self):
        # 1. Initially GEMINI_API_KEY is in Add platform dropdown
        status, html = self.request("GET", "/providers?tab=api-keys")
        self.assertEqual(status, 200)
        self.assertIn('value="GEMINI_API_KEY"', html)
        self.assertNotIn('id="key-card-gemini_api_key"', html)
        # Verify no inline event handlers violating CSP on api-keys tab
        self.assertNotIn("onclick=", html)
        self.assertNotIn("onchange=", html)

        # 2. Add GEMINI_API_KEY platform
        self.assertEqual(self.post("/settings/platforms/add", platform_key="GEMINI_API_KEY"), 303)

        # 3. Check GEMINI_API_KEY is now an active card
        status, html = self.request("GET", "/providers?tab=api-keys")
        self.assertEqual(status, 200)
        self.assertIn('id="key-card-gemini_api_key"', html)
        self.assertNotIn('value="GEMINI_API_KEY"', html)
        self.assertIn('data-remove-platform="GEMINI_API_KEY"', html)
        self.assertIn("confirm-remove-dialog", html)

        # 4. Save a key for GEMINI_API_KEY and check it becomes configured
        self.assertEqual(self.post("/settings/keys/GEMINI_API_KEY", action="save", value="gem-key-123"), 303)
        status, html = self.request("GET", "/providers?tab=api-keys")
        self.assertEqual(status, 200)
        self.assertIn('data-remove-key="GEMINI_API_KEY"', html)

        # 5. Remove key for GEMINI_API_KEY
        self.assertEqual(self.post("/settings/keys/GEMINI_API_KEY", action="remove"), 303)
        status, html = self.request("GET", "/providers?tab=api-keys")
        self.assertEqual(status, 200)
        self.assertNotIn('data-remove-key="GEMINI_API_KEY"', html)

        # 6. Remove GEMINI_API_KEY platform
        self.assertEqual(self.post("/settings/platforms/remove", key_name="GEMINI_API_KEY"), 303)

        # 7. Check GEMINI_API_KEY is back in the Add dropdown
        status, html = self.request("GET", "/providers?tab=api-keys")
        self.assertEqual(status, 200)
        self.assertNotIn('id="key-card-gemini_api_key"', html)
        self.assertIn('value="GEMINI_API_KEY"', html)

    def test_providers_platform_category_toggle_and_persistence(self):
        # 1. Check initial Anthropic categories: scripting is active, character is not supported
        status, html = self.request("GET", "/providers?tab=api-keys")
        self.assertEqual(status, 200)
        self.assertIn('data-platform="ANTHROPIC_API_KEY"', html)
        self.assertIn('data-toggle-cat="scripting" data-cat-title="Scripting" data-platform="ANTHROPIC_API_KEY"', html)
        self.assertNotIn('data-toggle-cat="character" data-cat-title="Character" data-platform="ANTHROPIC_API_KEY"', html)

        # Attempting to assign unsupported category 'character' to Anthropic is filtered out
        status, body = self.request(
            "POST", "/settings/platforms/categories",
            fields={"csrf_token": self.csrf, "platform_key": "ANTHROPIC_API_KEY", "categories": "scripting,character"},
            headers={"Accept": "application/json"}
        )
        self.assertEqual(status, 200)
        self.assertIn('"categories": ["scripting"]', body)

        # 2. Check Fal.ai categories (character, animation, audio supported)
        self.assertIn('data-toggle-cat="audio" data-cat-title="Audio" data-platform="FAL_KEY"', html)

        # Toggle audio to inactive for Fal.ai
        status, body = self.request(
            "POST", "/settings/platforms/categories",
            fields={"csrf_token": self.csrf, "platform_key": "FAL_KEY", "categories": "character,animation"},
            headers={"Accept": "application/json"}
        )
        self.assertEqual(status, 200)
        self.assertIn('"status": "ok"', body)

        # 3. Verify HTML reflects the updated categories for Fal.ai
        status, html = self.request("GET", "/providers?tab=api-keys")
        self.assertEqual(status, 200)
        self.assertIn('data-toggle-cat="audio" data-cat-title="Audio" data-platform="FAL_KEY" aria-pressed="false"', html)
        self.assertIn('data-toggle-cat="character" data-cat-title="Character" data-platform="FAL_KEY" aria-pressed="true"', html)

        # 4. Re-enable audio for Fal.ai
        status = self.post("/settings/platforms/categories", platform_key="FAL_KEY", categories="character,animation,audio", tab="api-keys")
        self.assertEqual(status, 303)

        # 5. Verify Audio is active again
        status, html = self.request("GET", "/providers?tab=api-keys")
        self.assertEqual(status, 200)
        self.assertIn('data-toggle-cat="audio" data-cat-title="Audio" data-platform="FAL_KEY" aria-pressed="true"', html)

        # 6. Verify ElevenLabs has audio category and no scripting
        self.assertIn('data-toggle-cat="audio" data-cat-title="Audio" data-platform="ELEVENLABS_API_KEY"', html)
        self.assertNotIn('data-toggle-cat="scripting" data-cat-title="Scripting" data-platform="ELEVENLABS_API_KEY"', html)

    def test_providers_audio_tab_and_models(self):
        # Verify Audio tab exists and displays fal-elevenlabs
        status, html = self.request("GET", "/providers?tab=audio")
        self.assertEqual(status, 200)
        self.assertIn('data-tab="audio" role="tabpanel"', html)
        self.assertIn("fal-elevenlabs", html)

        # Add OpenAI platform and verify its Character, Scripting, and Animation models
        self.assertEqual(self.post("/settings/platforms/add", platform_key="OPENAI_API_KEY"), 303)
        status, html = self.request("GET", "/providers?tab=api-keys")
        self.assertEqual(status, 200)
        self.assertIn("dall-e-3", html)
        self.assertIn("gpt-4o", html)
        self.assertIn("sora-1.0", html)

        # Add Google Gemini and verify Imagen 3
        self.assertEqual(self.post("/settings/platforms/add", platform_key="GEMINI_API_KEY"), 303)
        status, html = self.request("GET", "/providers?tab=api-keys")
        self.assertEqual(status, 200)
        self.assertIn("imagen-3.0-generate-002", html)
        self.assertIn("gemini-2.0-flash", html)

        # Verify ElevenLabs key card has audio model
        self.assertIn("eleven_multilingual_v2", html)

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

    def test_scene_editor_loads_the_clip_and_saves_a_legacy_scene(self):
        idea = self.store.add_idea("A scene editor test")
        self.store.decide_idea(idea, "accept")
        job_id = self.store.list_jobs()[0]["id"]
        scene = load_profile()["few_shot"][0]["scenes"][0]
        scene_id = scene["scene_id"]
        clip = f"{job_id}/{scene_id}-video.mp4"
        path = self.store.path.parent / "media" / clip
        path.parent.mkdir(parents=True)
        path.write_bytes(b"example")
        with self.store._connection(write=True) as db:
            db.execute(
                "UPDATE jobs SET status = 'preview_ready', plan_json = ?, preview_path = ? WHERE id = ?",
                (json.dumps([scene]), f"{job_id}/preview.mp4", job_id),
            )
            db.executemany(
                "INSERT INTO media_tasks (job_id, scene_id, kind, status, local_path) VALUES (?, ?, ?, 'completed', ?)",
                [(job_id, scene_id, kind, f"{job_id}/{scene_id}-{kind}.{extension}")
                 for kind, extension in (("keyframe", "png"), ("video", "mp4"), ("voice", "mp3"))],
            )

        editor = f"/workspace/{job_id}/scenes/{scene_id}"
        self.assertIn(f'href="{editor}"', self.request("GET", f"/workspace?job={job_id}")[1])
        status, html = self.request("GET", editor)
        self.assertEqual(status, 200)
        self.assertIn(scene["action"], html)
        self.assertIn(f'src="/media/{clip}"', html)
        self.assertEqual(self.request("GET", f"/media/{clip}", headers={"Range": "bytes=1-3"}), (206, "xam"))
        self.assertEqual(self.request("GET", f"/workspace/{job_id}/scenes/missing")[0], 404)

        revision = re.search(r'name="revision" value="([^"]+)"', html).group(1)
        fields = {"action": "Tavi waves.", "camera": scene["camera"], "dialogue": scene["dialogue"], "revision": revision}
        self.assertEqual(self.request("POST", editor, fields)[0], 403)
        self.assertEqual(self.post(editor, **dict(fields, action="")), 400)
        self.assertEqual(self.post(editor, **dict(fields, revision="stale")), 400)
        self.assertEqual(self.post(editor, **fields), 303)
        self.assertEqual(self.request("GET", editor)[0], 200)
        updated = self.store.get_job(job_id)
        self.assertEqual(updated["status"], "making_keyframes")
        self.assertEqual(json.loads(updated["plan_json"]), [dict(scene, action="Tavi waves.")])

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
    def test_character_profile_upload_and_per_video_options_through_http(self):
        self.assertEqual(self.request("GET", "/characters/new/profile")[0], 200)
        self.assertEqual(self.post("/characters", asset_id="mother", name="Mother",
                                   description="Clay character with blue glasses", dress_color="#7c3aed"), 303)
        base = "/characters/mother"
        self.assertIn("Needs a reference image", self.request("GET", base)[1])
        self.assertIn('href="/characters/mother"', self.request("GET", "/characters?q=MOTH")[1])
        self.assertNotIn('href="/characters/mother"', self.request("GET", "/characters?q=Tavi")[1])
        self.assertEqual(self.request("GET", "/settings?asset=mother")[0], 303)
        ref_path = f"{base}/references"
        fields = {"angle": "front", "variant_id": "", "primary": "yes"}
        self.assertEqual(self.upload(ref_path, {**fields, "csrf_token": "wrong"}, {"image": ("mother.png", png())}), 403)
        self.assertEqual(self.upload(ref_path, fields, {"image": ("../../untrusted.png", png())}), 303)
        side_fields = {**fields, "angle": "side", "primary": "", "upload_id": "ref_" + "e" * 32}
        for _ in range(2):
            self.assertEqual(self.upload(
                ref_path, side_fields, {"image": ("side.png", png(70))}, accept="application/json",
            ), 201)
        self.assertEqual(self.upload(
            ref_path, fields, {"image": ("bad.png", b"invalid")}, accept="application/json",
        ), 400)
        status, html = self.request("GET", base)
        self.assertEqual(status, 200)
        self.assertIn('href="/characters" aria-current="page"', html)
        self.assertIn("character-gallery", html)
        self.assertIn("#7C3AED", html)
        self.assertNotIn('name="image"', html)
        references = re.findall(r'src="(/asset-reference/mother/[^" ]+)"', html)
        self.assertEqual(len(references), 2)
        self.assertEqual(self.request("GET", references[0], binary=True), (200, png()))
        self.assertEqual(self.post(f"{base}/variants", variant_id="blue", name="Blue outfit",
                                   description="Blue coat", dress_color="#1122ee"), 303)
        reference_id = references[1].rsplit("/", 1)[1]
        self.assertEqual(self.post(f"{ref_path}/{reference_id}", angle="side", variant_id="blue", primary="yes"), 303)
        self.assertEqual(self.post(f"{base}/outfit-color", variant_id="blue", dress_color="#0000ff"), 303)
        self.assertEqual(self.post(f"{base}/edit", name="Mother (updated)", description="Clay character",
                                   dress_color="#FFCCAA"), 303)
        self.assertIn("Mother (updated)", self.request("GET", base)[1])
        self.assertEqual(self.post(f"{base}/voices", style_id="warm", name="Warm", voice="Rachel",
                                   speed="0.95", expressiveness="0.2", is_default="yes"), 303)
        self.assertEqual(self.upload(f"{base}/audio", {}, {"audio": ("mother.wav", wav())}), 303)
        self.assertIn('src="/character-audio/mother"', self.request("GET", base)[1])
        self.assertEqual(self.request("GET", "/character-audio/mother", binary=True), (200, wav()))
        self.assertEqual(
            self.request("GET", "/character-audio/mother", headers={"Range": "bytes=0-3"}, binary=True), (206, b"RIFF"),
        )
        self.assertEqual(self.post(f"{base}/audio-link", url="https://example.com/mother.mp3"), 303)
        self.assertIn('href="https://example.com/mother.mp3"', self.request("GET", base)[1])
        self.assertEqual(self.request("GET", "/character-audio/mother")[0], 404)
        self.assertEqual(self.post(f"{base}/audio-remove"), 303)
        self.assertIn("No voice reference added", self.request("GET", base)[1])
        self.assertEqual(self.upload("/settings/presets/audio", {"preset_id": "opening", "name": "Opening"},
                                     {"sfx": ("opening.wav", wav())}), 303)
        self.assertEqual(self.post("/settings/presets/audio/opening/default"), 303)
        idea = self.store.add_idea("A garden story")
        self.store.decide_idea(idea, "accept")
        job = self.store.list_jobs()[0]["id"]
        status, invalid_options = self.request("POST", f"/workspace/{job}/options", {
            "csrf_token": self.csrf, "characters": "mother", "variant.mother": "missing", "voice.mother": "warm",
        })
        self.assertEqual(status, 400)
        self.assertIn('class="record production-options" open', invalid_options)
        self.assertIn('name="characters" value="mother" checked', invalid_options)
        self.assertIn('value="warm" selected', invalid_options)
        self.assertEqual(self.post(f"/workspace/{job}/options", **{
            "characters": "mother", "variant.mother": "blue", "voice.mother": "warm", "preset.audio": "opening",
        }), 303)
        workspace = self.request("GET", f"/workspace?job={job}")[1]
        self.assertIn('name="characters" value="mother" checked', workspace)
        self.assertIn('value="warm" selected', workspace)
        self.assertEqual(self.post(f"/workspace/{job}/options", **{"variant.mother": "blue"}), 303)
        self.assertNotIn(
            'name="characters" value="mother" checked', self.request("GET", f"/workspace?job={job}")[1],
        )

    def test_assets_library_screens_crud_and_navigation(self):
        status, html = self.request("GET", "/assets")
        self.assertEqual(status, 200)
        self.assertIn("Asset Library", html)
        self.assertIn("+ Add Asset", html)
        self.assertIn('href="/assets?kind=place"', html)
        self.assertIn('href="/assets?kind=item"', html)
        self.assertIn('href="/assets" aria-current="page"', html)
        self.assertIn('href="/presets"', html)

        self.assertEqual(self.request("GET", "/assets/new/profile")[0], 200)
        self.assertEqual(self.post("/assets", asset_id="crystal_cave", kind="place",
                                   name="Crystal Cave", description="A glittering subterranean cave"), 303)
        base = "/assets/crystal_cave"
        status, html = self.request("GET", base)
        self.assertEqual(status, 200)
        self.assertIn("Crystal Cave", html)
        self.assertIn("Place", html)
        self.assertIn("Needs a reference image", html)
        self.assertIn('href="/assets/crystal_cave/edit"', html)

        # Search filter
        search_html = self.request("GET", "/assets?q=CRYSTAL")[1]
        self.assertIn('href="/assets/crystal_cave"', search_html)
        self.assertIn('<div class="asset-badges">', search_html)
        self.assertNotIn('href="/assets/crystal_cave"', self.request("GET", "/assets?q=DESERT")[1])

        # Add variant
        self.assertEqual(self.post(f"{base}/variants", variant_id="glowing", name="Glowing Crystals",
                                   description="Crystals glowing blue and violet"), 303)
        self.assertIn("Glowing Crystals", self.request("GET", base)[1])

        # Edit asset
        self.assertEqual(self.request("GET", f"{base}/edit")[0], 200)
        self.assertEqual(self.post(f"{base}/edit", name="Enchanted Crystal Cave",
                                   description="Glittering and humming with magic"), 303)
        self.assertIn("Enchanted Crystal Cave", self.request("GET", base)[1])

        # Test backward-compatible redirect from /settings?asset=crystal_cave to /assets/crystal_cave
        status, _ = self.request("GET", "/settings?asset=crystal_cave")
        self.assertEqual(status, 303)

        # Static script
        status, js = self.request("GET", "/assets.js")
        self.assertEqual(status, 200)
        self.assertIn("data-preview-dialog", js)

    def test_presets_screens_crud_and_audio(self):
        status, html = self.request("GET", "/presets")
        self.assertEqual(status, 200)
        self.assertIn("Production Presets", html)
        self.assertIn('data-tab-btn="visual"', html)
        self.assertIn('data-tab-btn="script"', html)
        self.assertIn('data-tab-btn="subtitle"', html)
        self.assertIn('data-tab-btn="audio"', html)
        self.assertIn('href="/presets" aria-current="page"', html)
        self.assertIn('id="open-add-preset-dialog"', html)
        self.assertIn('id="add-preset-dialog-visual"', html)

        # Create visual preset
        self.assertEqual(self.post("/presets/visual", preset_id="neon_noir", name="Neon Noir",
                                   style="High contrast dark city with neon glows",
                                   palette="Electric cyan, magenta, jet black", is_default="yes"), 303)
        status, html = self.request("GET", "/presets?tab=visual")
        self.assertEqual(status, 200)
        self.assertIn("Neon Noir", html)
        self.assertIn("Default", html)
        self.assertIn("High contrast dark city with neon glows", html)

        # Create script preset
        self.assertEqual(self.post("/presets/script", preset_id="noir_tone", name="Noir Tone",
                                   tone="Hardboiled cynical voiceover", is_default="no"), 303)
        status, html = self.request("GET", "/presets?tab=script")
        self.assertEqual(status, 200)
        self.assertIn("Noir Tone", html)
        self.assertIn("Hardboiled cynical voiceover", html)

        # Set default for script preset
        self.assertEqual(self.post("/presets/script/noir_tone/default"), 303)
        self.assertIn("Default", self.request("GET", "/presets?tab=script")[1])

        # Create subtitle preset
        self.assertEqual(self.post("/presets/subtitle", preset_id="neon_subs", name="Neon Captions",
                                   font_name="DejaVu Sans", font_size="16", text_color="#00FFFF",
                                   outline_color="#000000", position="bottom", is_default="no"), 303)
        status, html = self.request("GET", "/presets?tab=subtitle")
        self.assertEqual(status, 200)
        self.assertIn("Neon Captions", html)
        self.assertIn("#00FFFF", html)

        # Static script and audio endpoint
        status, js = self.request("GET", "/presets.js")
        self.assertEqual(status, 200)
        self.assertIn("data-tab-btn", js)
        self.assertIn("open-add-preset-dialog", js)
        self.assertEqual(self.request("GET", "/preset-audio/nonexistent.mp3")[0], 404)

        # Test backward-compatible redirect from /settings?presets=1 to /presets
        status, _ = self.request("GET", "/settings?presets=1")
        self.assertEqual(status, 303)

    def test_settings_page_does_not_contain_asset_library_or_presets(self):
        status, html = self.request("GET", "/settings")
        self.assertEqual(status, 200)
        self.assertIn("Budget", html)
        self.assertIn("Idea generation schedule", html)
        self.assertNotIn("settings-nav-cards", html)
        self.assertNotIn("<summary>Asset Library</summary>", html)
        self.assertNotIn("<summary>Production Presets</summary>", html)
        self.assertNotIn("Manage places, environments, and prop references", html)
        self.assertNotIn("Manage visual styles, script tone, subtitles, and audio", html)

    def test_brand_rules_page_and_crud(self):
        status, html = self.request("GET", "/brand-rules")
        self.assertEqual(status, 200)
        self.assertIn("Brand Rules", html)
        self.assertIn('href="/brand-rules" aria-current="page"', html)
        self.assertIn('id="open-add-brand-dialog"', html)
        self.assertIn('id="add-brand-dialog"', html)
        self.assertIn('class="brand-list"', html)
        self.assertIn('data-open-brand="kids_3d_default"', html)
        self.assertIn('id="brand-dialog-kids_3d_default"', html)
        self.assertIn("Mother Prompt / Style Rhythm Directive", html)
        self.assertIn("Default", html)

        # JS script endpoint
        js_status, js = self.request("GET", "/brand_rules.js")
        self.assertEqual(js_status, 200)
        self.assertIn("open-add-brand-dialog", js)

        # Create new brand profile
        self.assertEqual(self.post(
            "/brand-rules",
            brand_id="bedtime_tales",
            name="Bedtime Tales",
            mother_prompt="Warm soothing lighting, slow camera motion, peaceful 3D clay characters.",
            brand_rules="- No bright flashes\n- Calming color palette",
            story_rules="- Gentle resolution before sleep",
            is_default="yes",
        ), 303)

        status, html = self.request("GET", "/brand-rules")
        self.assertEqual(status, 200)
        self.assertIn("Bedtime Tales", html)
        self.assertIn("Warm soothing lighting, slow camera motion", html)
        self.assertIn("No bright flashes", html)

        # Switch default back
        self.assertEqual(self.post("/brand-rules/kids_3d_default/default"), 303)

        # Delete new brand profile
        self.assertEqual(self.post("/brand-rules/bedtime_tales/delete"), 303)
        status, html = self.request("GET", "/brand-rules")
        self.assertEqual(status, 200)
        self.assertNotIn("Bedtime Tales", html)

    def test_inbox_create_brand_dropdown_and_submission(self):
        status, html = self.request("GET", "/inbox/create")
        self.assertEqual(status, 200)
        self.assertIn('name="brand_id"', html)
        self.assertIn("kids_3d_default", html)
        self.assertIn("(Default)", html)

        # Submit manual idea with brand_id
        self.assertEqual(self.post(
            "/ideas",
            title="Tiny Turtle",
            prompt="A tiny turtle explores a crystal pond.",
            brand_id="kids_3d_default",
        ), 303)

        idea = self.store.list_ideas()[0]
        self.assertEqual(idea["title"], "Tiny Turtle")
        self.assertEqual(idea["brand_id"], "kids_3d_default")

        # Accept idea into production job
        self.assertEqual(self.post(f"/ideas/{idea['id']}/decision", decision="accept"), 303)
        job = self.store.list_jobs()[-1]
        options = json.loads(job["production_options_json"])
        self.assertEqual(options.get("presets", {}).get("brand"), "kids_3d_default")

    def test_idea_edit_page_and_update(self):
        self.assertEqual(self.post("/ideas", title="Draft Idea", prompt="Initial prompt"), 303)
        status, html = self.request("GET", "/ideas/1/edit")
        self.assertEqual(status, 200)
        self.assertIn("Edit Idea", html)
        self.assertIn("Initial prompt", html)

        self.assertEqual(self.post("/ideas/1/edit", title="Updated Idea", prompt="New prompt"), 303)
        status, html = self.request("GET", "/ideas/1")
        self.assertEqual(status, 200)
        self.assertIn("Updated Idea", html)
        self.assertIn("New prompt", html)

    def test_approved_idea_pause_resume_and_delete(self):
        self.assertEqual(self.post("/ideas", title="Queued Idea", prompt="Queued prompt"), 303)
        self.assertEqual(self.post("/ideas/1/decision", decision="accept"), 303)

        status, html = self.request("GET", "/ideas/1")
        self.assertEqual(status, 200)
        self.assertIn("Pause job", html)
        self.assertIn("Edit idea", html)
        self.assertIn("Delete", html)

        # Pause job via idea detail
        self.assertEqual(self.post("/ideas/1/job-control", action="pause"), 303)
        status, html = self.request("GET", "/ideas/1")
        self.assertEqual(status, 200)
        self.assertIn("Resume job", html)

        status, html = self.request("GET", "/workspace?job=1")
        self.assertEqual(status, 200)
        self.assertIn("Paused", html)

        # Resume job via idea detail
        self.assertEqual(self.post("/ideas/1/job-control", action="resume"), 303)
        status, html = self.request("GET", "/ideas/1")
        self.assertEqual(status, 200)
        self.assertIn("Pause job", html)

        # Delete approved idea
        self.assertEqual(self.post("/ideas/1/archive", action="delete"), 303)
        self.assertEqual(len(self.store.list_jobs()), 0)
        idea = self.store.get_idea(1)
        self.assertIsNotNone(idea["deleted_at"])

    def test_workspace_pause_resume_edit_delete(self):
        self.assertEqual(self.post("/ideas", title="Workspace Idea", prompt="Prompt to edit"), 303)
        self.assertEqual(self.post("/ideas/1/decision", decision="accept"), 303)

        status, html = self.request("GET", "/workspace?job=1")
        self.assertEqual(status, 200)
        self.assertIn("Pause job", html)
        self.assertIn("Edit title &amp; prompt", html)
        self.assertIn("Delete job", html)

        # Pause in workspace
        self.assertEqual(self.post("/workspace/1/pause"), 303)
        status, html = self.request("GET", "/workspace?job=1")
        self.assertEqual(status, 200)
        self.assertIn("Resume job", html)
        self.assertIn("Paused", html)

        # Resume in workspace
        self.assertEqual(self.post("/workspace/1/resume"), 303)
        status, html = self.request("GET", "/workspace?job=1")
        self.assertEqual(status, 200)
        self.assertIn("Pause job", html)

        # Edit title and prompt
        self.assertEqual(self.post("/workspace/1/edit", title="Modified Job", prompt="Modified prompt"), 303)
        status, html = self.request("GET", "/workspace?job=1")
        self.assertEqual(status, 200)
        self.assertIn("Modified Job", html)
        self.assertIn("Modified prompt", html)

        # Delete job
        self.assertEqual(self.post("/workspace/1/delete"), 303)
        status, _ = self.request("GET", "/workspace?job=1")
        self.assertEqual(status, 404)

    def test_schedule_edit_page_and_update(self):
        self.assertEqual(self.post(
            "/schedules",
            title="Batch Schedule",
            prompts="Prompt 1\nPrompt 2",
            start_date="2026-11-10",
            run_at="09:00",
            timezone="Asia/Dhaka",
            daily_limit="1",
        ), 303)

        status, html = self.request("GET", "/schedules/1/edit")
        self.assertEqual(status, 200)
        self.assertIn("Edit Schedule", html)
        self.assertIn("Batch Schedule", html)

        self.assertEqual(self.post(
            "/schedules/1/edit",
            title="Updated Batch",
            start_date="2026-11-12",
            run_at="10:00",
            timezone="Asia/Dhaka",
            daily_limit="2",
        ), 303)

        status, html = self.request("GET", "/schedules/1")
        self.assertEqual(status, 200)
        self.assertIn("Updated Batch", html)
        self.assertIn("10:00", html)


