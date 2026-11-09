import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.db import MEDIA_SCHEMA, PUBLISHING_SCHEMA, SCHEMA, SETTINGS_AND_PLANNING_SCHEMA, StudioStore
from app.niche import load_profile
from app.planning import StudioPlanner
from app.production import StudioProduction
from app.providers import ProviderResult
from app.scenes import SceneValidationError, validate_scene_plan
from app.settings import StudioSettings
from app.studio_library import StudioLibrary
from test_asset_library import REQUIRES_FFMPEG, png, wav


NOW = datetime(2026, 11, 9, 3, tzinfo=timezone.utc)
NICHE = "kids_3d"


class LibraryMigrationTests(unittest.TestCase):
    def test_v4_keeps_bundled_reference_and_pending_provider_job(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "studio.sqlite3"
            reference = load_profile()["assets"][0]
            with sqlite3.connect(path) as db:
                for schema in (SCHEMA, SETTINGS_AND_PLANNING_SCHEMA, MEDIA_SCHEMA, PUBLISHING_SCHEMA):
                    db.executescript(schema)
                db.execute("INSERT INTO niches (id, name) VALUES (?, 'Kids 3D')", (NICHE,))
                db.execute(
                    "INSERT INTO assets VALUES (?, ?, 'character', 'Tavi', 'Mint sprite', ?)",
                    (reference["id"], NICHE, json.dumps([reference["reference_image"]])),
                )
                db.execute("INSERT INTO ideas (id, title, prompt, source) VALUES (3, 'Story', 'Story', 'manual')")
                db.execute("INSERT INTO jobs (id, idea_id, title, prompt, source) VALUES (4, 3, 'Story', 'Story', 'manual')")
                db.execute(
                    """INSERT INTO media_tasks (id, job_id, scene_id, kind, status, attempts, provider_task_id)
                        VALUES (5, 4, 'old_scene', 'voice', 'pending', 2, 'known-task')"""
                )
            store = StudioStore(path)
            store.initialize()
            with store._connection() as db:
                task = db.execute("SELECT * FROM media_tasks WHERE id = 5").fetchone()
                self.assertEqual((task["status"], task["provider_task_id"], task["attempts"], task["line_index"]),
                                 ("pending", "known-task", 2, 0))
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            library = StudioLibrary(store)
            self.assertEqual(library.assets.list_assets()[0]["references"][0]["source"], "bundled")


@REQUIRES_FFMPEG
class LibraryPipelineTests(unittest.TestCase):
    def test_two_characters_keep_their_own_refs_voices_and_render_presets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = StudioStore(path / "studio.sqlite3")
            store.initialize()
            store.seed_niche(load_profile())
            settings = StudioSettings(store, path / ".env")
            settings.seed()
            settings.set_key("ANTHROPIC_API_KEY", "fixture-key")
            settings.update_provider("claude-sonnet", True, 10, 100)
            settings.set_budgets(1000, 1000)

            lines = [
                {"speaker_id": "mother", "text": "Ready?", "voice_style_id": "bright"},
                {"speaker_id": "grandpa", "text": "Yes."},
            ]
            first = {
                "scene_id": "opening", "characters": ["mother", "grandpa"], "place": "pebble_garden",
                "camera": "Side view", "action": "Mother and Grandpa wave", "dialogue": "",
                "dialogue_lines": lines, "duration": 5,
            }
            scenes = [first, *[dict(first, scene_id=f"scene_{number}", dialogue_lines=[]) for number in range(5)]]
            requests = []

            class PlannerFixture:
                def submit(self, request):
                    requests.append(request)
                    return ProviderResult("completed", data={"scenes": scenes})

            planner = StudioPlanner(store, settings, lambda key: PlannerFixture())
            production = StudioProduction(store, settings, planner)
            library = planner.library
            assets = library.assets
            for asset_id in ("mother", "grandpa"):
                assets.create_asset(NICHE, asset_id, "character", asset_id.title(), "Clay sprite with a round face.")
                for angle in ("front", "side"):
                    assets.add_reference(NICHE, asset_id, angle, "", angle == "front", png())
            assets.add_variant(NICHE, "mother", "blue_coat", "Blue coat", "A blue coat and peach shoes.")
            for angle in ("front", "side"):
                assets.add_reference(NICHE, "mother", angle, "blue_coat", angle == "front", png(120))
            assets.save_voice_style(NICHE, "mother", "bright", "Bright", "Aria", 1.05, True)
            assets.save_voice_style(NICHE, "mother", "calm", "Calm", "Rachel", 0.85, False, 0.2)
            assets.save_voice_style(NICHE, "grandpa", "bedtime", "Bedtime", "Brian", 0.9, True)

            library.add_preset(NICHE, "visual", "dusk", "Dusk", {"style": "Soft clay", "palette": "indigo"}, {}, True)
            library.add_preset(NICHE, "script", "gentle", "Gentle", {"tone": "Gentle bedtime story"}, {}, True)
            library.add_preset(NICHE, "subtitle", "peach", "Peach", {
                "font_name": "DejaVu Sans", "font_size": "18", "text_color": "#FFAA88",
                "outline_color": "#000000", "position": "top",
            }, {}, True)
            library.add_preset(NICHE, "audio", "opening", "Opening", {}, {"music": wav(), "sfx": wav()}, True)

            idea = store.add_idea("Mother and Grandpa visit the garden")
            store.decide_idea(idea, "accept")
            job_id = store.list_jobs()[0]["id"]
            production.configure(job_id, {"presets": {}, "variants": {"mother": "blue_coat"}, "voices": {"mother": "calm"}})
            planner.process_plan(NOW)
            self.assertIn("Gentle bedtime story", requests[0].prompt)
            self.assertIn("indigo", requests[0].prompt)
            self.assertIn("Blue coat", requests[0].prompt)

            # A default edited after planning applies to future plans. This job's context is already saved.
            library.add_preset(NICHE, "visual", "day", "Day", {"style": "Glossy", "palette": "yellow"}, {}, True)
            production.media.queue_keyframes()
            opening = next(task for task in production.media.tasks() if task["scene_id"] == "opening")
            request, refs = production._request(opening)
            self.assertIn("indigo", request.prompt)
            self.assertIn("Mother (mother) from angle side", request.prompt)
            self.assertIn("Grandpa (grandpa) from angle side", request.prompt)
            self.assertEqual([ref["variant_id"] for ref in refs if "/mother/" in ref["path"]], ["blue_coat", "blue_coat"])
            self.assertEqual([ref["angle"] for ref in refs if "/grandpa/" in ref["path"]], ["front", "side"])
            with self.assertRaises(ValueError):
                production.media.approve_keyframes(job_id)

            with store._connection(write=True) as db:
                db.execute("UPDATE media_tasks SET status = 'completed', local_path = 'keyframe.png' WHERE job_id = ?", (job_id,))
                db.execute("UPDATE jobs SET status = 'review_keyframes' WHERE id = ?", (job_id,))
            production.media.approve_keyframes(job_id)
            voices = [production._request(task)[0] for task in production.media.tasks() if task["kind"] == "voice"]
            self.assertEqual([(voice.voice, voice.prompt) for voice in voices], [("Rachel", "Ready?"), ("Brian", "Yes.")])
            self.assertEqual((voices[0].voice_speed, voices[0].voice_expressiveness), (0.85, 0.2))

            with store._connection(write=True) as db:
                db.execute("UPDATE media_tasks SET status = 'completed', local_path = kind || line_index WHERE job_id = ?", (job_id,))
                db.execute("UPDATE jobs SET status = 'render_ready' WHERE id = ?", (job_id,))
            with patch("app.production.render_video") as render:
                production._render()
            self.assertEqual(render.call_args.args[2]["opening"], [production.root / "voice0", production.root / "voice1"])
            self.assertEqual(render.call_args.kwargs["subtitle_style"]["text_color"], "#FFAA88")
            self.assertTrue(render.call_args.kwargs["sfx_path"].is_file())
            self.assertTrue(render.call_args.kwargs["music_path"].is_file())

            invalid = dict(first, dialogue_lines=[{"speaker_id": "tavi_seed_sprite", "text": "Hello"}])
            with self.assertRaisesRegex(SceneValidationError, "speaker_id must reference"):
                validate_scene_plan({"scenes": [invalid]}, library.profile(), 5)
