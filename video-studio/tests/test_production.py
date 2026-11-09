import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.db import StudioStore
from app.niche import load_profile
from app.planning import StudioPlanner
from app.production import StudioProduction
from app.providers import ProviderError, ProviderResult
from app.settings import StudioSettings


NOW = datetime(2026, 11, 9, 3, tzinfo=timezone.utc)
FRAME = "data:image/png;base64,YWJj"


class ProductionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name)
        self.store = StudioStore(path / "studio.sqlite3")
        self.store.initialize()
        profile = load_profile()
        self.store.seed_niche(profile)
        self.settings = StudioSettings(self.store, path / ".env")
        self.settings.seed()
        for key in ("FAL_KEY", "EVOLINK_API_KEY", "ANTHROPIC_API_KEY"):
            self.settings.set_key(key, "test-fixture-key")
        for route in self.settings.list_providers():
            if route["enabled"]:
                self.settings.update_provider(route["name"], True, route["priority"], 100)
        self.settings.set_budgets(100_000, 100_000)
        idea = self.store.add_idea("An animated garden scene")
        self.store.decide_idea(idea, "accept")
        self.job = self.store.list_jobs()[0]["id"]
        scene = profile["few_shot"][0]["scenes"][0]
        with self.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET status = 'planned', plan_json = ? WHERE id = ?", (json.dumps([scene]), self.job))
        planner = StudioPlanner(self.store, self.settings, lambda key: self)
        self.production = StudioProduction(self.store, self.settings, planner)
        self.requests = []
        self.polls = []

    def submit(self, request):
        self.assertEqual(request.output_schema["properties"]["score"]["type"], "integer")
        return ProviderResult("completed", data={"decision": "pass", "score": 95, "issues": [], "failed_scene_ids": []})

    def adapter(self, route, key):
        test = self
        name = route["name"]

        class Adapter:
            def submit(self, request):
                test.requests.append((name, request))
                if name == "fal-veo":
                    raise ProviderError("Primary route is limited", retryable=True)
                if name == "evolink-seedance":
                    return ProviderResult("pending", task_id="fixture-task")
                return ProviderResult("completed", url="https://fixture.example/file")

            def poll(self, task_id):
                test.polls.append(task_id)
                return ProviderResult("completed", url="https://fixture.example/file")

        return Adapter()

    @staticmethod
    def write_fixture(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"media fixture")
        return path

    def test_manual_gate_failover_poll_and_preview(self):
        with patch("app.production.adapter_for", self.adapter), \
                patch("app.production.local_image", return_value=FRAME), \
                patch("app.production.download_artifact", side_effect=lambda url, path, kind: self.write_fixture(path)), \
                patch("app.production.render_video", side_effect=lambda scenes, clips, voices, path, **kw: self.write_fixture(path)), \
                patch("app.production.sample_frames", return_value=[Path("fixture.jpg")]):
            self.production.advance(NOW)
            self.assertEqual(self.store.get_job(self.job)["status"], "review_keyframes")
            self.production.advance(NOW)
            self.assertEqual([name for name, _ in self.requests], ["fal-nano-banana-pro"])
            self.production.media.approve_keyframes(self.job)

            self.production.advance(NOW)
            self.production.advance(NOW)
            self.production.recover_interrupted()
            self.production.advance(NOW)

        videos = [request for name, request in self.requests if name in ("fal-veo", "evolink-seedance")]
        self.assertEqual([request.reference_image for request in videos], [FRAME, FRAME])
        self.assertEqual(self.polls, ["fixture-task"])
        self.assertEqual(self.store.get_job(self.job)["status"], "preview_ready")

        self.production.revise(self.job, "Use a slower camera.")
        updated = self.store.get_job(self.job)
        self.assertEqual(updated["status"], "queued")
        self.assertEqual(updated["revision_feedback"], "Use a slower camera.")
        self.assertIsNone(updated["preview_path"])
