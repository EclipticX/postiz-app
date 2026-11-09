import re
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlencode

from app.db import StudioStore
from app.niche import load_profile
from app.server import StudioServer
from test_asset_library import REQUIRES_FFMPEG, png, wav


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
        self.assertEqual(self.post("/ideas/1/decision", decision="later"), 303)
        self.assertEqual(self.store.list_jobs(), [])
        self.assertEqual(self.post("/ideas/1/decision", decision="accept"), 303)
        status, html = self.request("GET", "/workspace?job=1")
        self.assertEqual(status, 200)
        self.assertIn("A forest visit", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertEqual(self.request("GET", "/studio.css")[0], 200)

    def test_prompt_schedule_requires_activation(self):
        self.assertEqual(self.post(
            "/schedules", prompts="Forest scene\nLibrary scene", start_date="2026-11-09",
            run_at="09:00", timezone="Asia/Dhaka", daily_limit="1",
        ), 303)
        due = datetime(2026, 11, 9, 3, 15, tzinfo=timezone.utc)
        self.assertEqual(self.store.dispatch_due(due), 0)
        self.assertIn("Activate Schedule", self.request("GET", "/")[1])
        self.assertEqual(self.post("/schedules/1/status", status="active"), 303)
        self.assertEqual(self.store.dispatch_due(due), 1)
        self.assertIn("Forest scene", self.request("GET", "/workspace?job=1")[1])

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
