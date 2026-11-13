import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import StudioStore
from app.niche import load_profile
from app.planning import StudioPlanner
from app.production import StudioProduction
from app.providers import ProviderError, ProviderResult
from app.settings import StudioSettings
from test_asset_library import REQUIRES_FFMPEG, png


NOW = datetime(2026, 11, 9, 3, 30, tzinfo=timezone.utc)


class PlanningTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name)
        self.store = StudioStore(path / "studio.sqlite3")
        self.store.initialize()
        self.profile = load_profile()
        self.store.seed_niche(self.profile)
        self.settings = StudioSettings(self.store, path / ".env")
        self.settings.seed()
        self.settings.set_key("ANTHROPIC_API_KEY", "test-fixture-key")
        self.settings.update_provider("claude-sonnet", True, 10, 1000)
        self.settings.set_budgets(10_000, 5000)
        self.planner = StudioPlanner(self.store, self.settings, lambda key: self)
        self.requests = []
        self.responses = []

    def submit(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return ProviderResult("completed", data=response)

    def valid_plan(self):
        example = self.profile["few_shot"][0]["scenes"][0]
        return {"scenes": [
            dict(example, scene_id=f"scene_{index}", duration=duration)
            for index, duration in enumerate((4, 4, 4, 4, 4, 5, 5))
        ]}

    def queue_job(self):
        idea = self.store.add_idea("Tavi finds a lost seed.")
        self.store.decide_idea(idea, "accept")
        return self.store.list_jobs()[0]["id"]

    def test_manual_trend_uses_memory_and_creates_pending_ideas(self):
        with self.store._connection(write=True) as db:
            db.execute("INSERT INTO memory (niche_id, pattern_json) VALUES ('kids_3d', ?)", ('{"hook":"seed reveal"}',))
        self.planner.request_ideas("Gentle garden stories")
        self.responses = [{"ideas": [{"title": "A small discovery", "prompt": "Tavi finds a seed."}]}]
        self.planner.process_ideas(NOW)
        self.assertIn("Gentle garden stories", self.requests[0].prompt)
        self.assertIn("seed reveal", self.requests[0].prompt)
        self.assertEqual(self.store.list_ideas()[0]["status"], "pending")
        self.assertEqual(self.store.list_jobs(), [])

    def test_generate_three_ideas_returns_top_three(self):
        self.responses = [{"ideas": [
            {"title": "Idea 1", "prompt": "Prompt 1"},
            {"title": "Idea 2", "prompt": "Prompt 2"},
            {"title": "Idea 3", "prompt": "Prompt 3"},
            {"title": "Idea 4", "prompt": "Prompt 4"},
        ]}]
        ideas = self.planner.generate_three_ideas("Gentle garden stories", now=NOW)
        self.assertEqual(len(ideas), 3)
        self.assertEqual(ideas[0]["title"], "Idea 1")
        self.assertEqual(ideas[1]["title"], "Idea 2")
        self.assertEqual(ideas[2]["title"], "Idea 3")

    def test_weekly_schedule_dispatches_only_once_in_local_time(self):
        self.planner.set_idea_schedule(True, "weekly", "09:00", "Asia/Dhaka", 0, "Garden", NOW)
        self.planner.dispatch_idea_schedule(NOW)
        StudioPlanner(self.store, self.settings).dispatch_idea_schedule(NOW)
        self.assertEqual(len(self.planner.idea_requests()), 1)

    def test_invalid_scene_is_sent_back_to_planner_then_persisted(self):
        job_id = self.queue_job()
        bad = self.valid_plan()
        bad["scenes"][0]["characters"] = ["unknown_character"]
        self.responses = [bad, self.valid_plan()]
        self.planner.process_plan(NOW)
        self.assertEqual(self.store.get_job(job_id)["status"], "queued")
        self.planner.process_plan(NOW)
        self.assertIn("unknown_character", self.requests[1].prompt)
        self.assertIn('"target_duration_seconds": 30', self.requests[1].prompt)
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "planned")
        self.assertEqual(sum(scene["duration"] for scene in json.loads(job["plan_json"])), 30)
        self.assertEqual(job["plan_attempts"], 2)

    def test_budget_blocks_paid_requests_and_uncertain_call_is_held(self):
        job_id = self.queue_job()
        self.settings.set_budgets(500, 5000)
        self.planner.process_plan(NOW)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.store.get_job(job_id)["plan_attempts"], 0)
        self.settings.set_budgets(10_000, 5000)
        self.responses = [ProviderError("Provider timeout", uncertain=True)]
        self.planner.process_plan(NOW)
        self.planner.process_plan(NOW)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.store.get_job(job_id)["status"], "needs_review")

    @REQUIRES_FFMPEG
    def test_cast_missing_a_reference_stays_queued_until_reference_is_restored(self):
        job_id = self.queue_job()
        production = StudioProduction(self.store, self.settings, self.planner)
        assets = self.planner.library.assets
        niche_id = "kids_3d"
        assets.create_asset(niche_id, "mother", "character", "Mother", "A clay figure.")
        assets.add_variant(niche_id, "mother", "coat", "Coat", "Rain coat")
        reference = assets.add_reference(niche_id, "mother", "front", "", True, png())
        production.configure(job_id, {"presets": {}, "variants": {}, "voices": {}, "characters": ["mother"]})

        assets.update_reference(niche_id, "mother", reference, "front", "coat", True)
        self.planner.process_plan(NOW)
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "queued")
        self.assertIn("Add a reference image for Mother", job["stage_error"])
        self.assertEqual(job["plan_attempts"], 0)
        self.assertIsNone(job["production_context_json"])
        self.assertEqual(self.requests, [])

        assets.update_reference(niche_id, "mother", reference, "front", "", True)
        plan = self.valid_plan()
        for scene in plan["scenes"]:
            scene["characters"] = ["mother"]
        self.responses = [plan]
        self.planner.process_plan(NOW)
        self.assertEqual(self.store.get_job(job_id)["status"], "planned")
        self.assertEqual(len(self.requests), 1)

    def test_planner_stops_after_two_correction_retries(self):
        job_id = self.queue_job()
        self.responses = [{"scenes": []}] * 3
        for _ in range(4):
            self.planner.process_plan(NOW)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.store.get_job(job_id)["status"], "needs_review")

    def test_scheduled_backlog_keeps_its_daily_start_limit(self):
        schedule = self.store.create_schedule(["First", "Second"], "2026-11-09", "09:00", "Asia/Dhaka", 1)
        self.store.set_schedule_status(schedule, "active")
        self.store.dispatch_due(NOW)
        self.store.dispatch_due(NOW + timedelta(days=1))
        resumed = NOW + timedelta(days=2)
        self.responses = [self.valid_plan(), self.valid_plan()]
        self.planner.process_plan(resumed)
        self.planner.process_plan(resumed)
        self.assertEqual(len(self.requests), 1)
        self.planner.process_plan(resumed + timedelta(days=1))
        self.assertEqual(len(self.requests), 2)
