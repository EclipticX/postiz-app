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
from app.studio_library import StudioLibrary, select_references
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


class LibraryReferenceTests(unittest.TestCase):
    def test_wide_shots_use_full_body_when_available_and_keep_legacy_front_fallback(self):
        primary = {"angle": "side", "primary": True}
        front = {"angle": "front", "primary": False}
        full_body = {"angle": "full-body", "primary": False}
        self.assertEqual(select_references({"references": [primary, front]}, "Wide shot"), [primary, front])
        self.assertEqual(
            select_references({"references": [primary, front, full_body]}, "Wide shot"), [primary, full_body],
        )


class LibraryCastTests(unittest.TestCase):
    def test_cast_requires_renderable_characters_and_accepts_bundled_references(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StudioStore(Path(directory) / "studio.sqlite3")
            store.initialize()
            store.seed_niche(load_profile())
            library = StudioLibrary(store)
            library.assets.create_asset(NICHE, "draft", "character", "Draft", "No images yet")
            options = {"presets": {}, "variants": {}, "voices": {}}
            library.validate_options(NICHE, options)
            library.validate_options(NICHE, dict(options, characters=["tavi_seed_sprite"]))
            with self.assertRaisesRegex(ValueError, "Add a reference image for Draft"):
                library.validate_options(NICHE, dict(options, characters=["draft"]))
            with self.assertRaisesRegex(ValueError, "Choose saved characters"):
                library.validate_options(NICHE, dict(options, characters=["pebble_garden"]))
            with self.assertRaisesRegex(ValueError, "Choose each character only once"):
                library.validate_options(NICHE, dict(options, characters=["tavi_seed_sprite", "tavi_seed_sprite"]))

    def test_bundled_fallback_keeps_edited_profile_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StudioStore(Path(directory) / "studio.sqlite3")
            store.initialize()
            store.seed_niche(load_profile())
            library = StudioLibrary(store)
            assets = library.assets
            asset_id = "tavi_seed_sprite"
            assets.update_character(NICHE, asset_id, "Tavi", "New description", "#4420bf")
            assets.add_variant(NICHE, asset_id, "coat", "Coat", "Rain coat")
            assets.update_reference(NICHE, asset_id, "bundled_1", "side", "coat", True)
            options = {"presets": {}, "variants": {}, "voices": {}, "characters": [asset_id]}
            library.validate_options(NICHE, options)
            character = next(asset for asset in library.profile(NICHE, options)["assets"] if asset["id"] == asset_id)
            self.assertEqual(character["name"], "Tavi")
            self.assertEqual(character["description"], "New description\nDress color: #4420BF.")
            self.assertEqual(assets.reference_path(select_references(character, "front")[0]).suffix, ".svg")


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
            assets.add_variant(NICHE, "mother", "blue_coat", "Blue coat", "A blue coat and peach shoes.", "#406cbf")
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
            options = {
                "presets": {}, "variants": {"mother": "blue_coat"}, "voices": {"mother": "calm"},
                "characters": ["mother", "grandpa"],
            }
            production.configure(job_id, options)
            planner.process_plan(NOW)
            self.assertIn("Gentle bedtime story", requests[0].prompt)
            self.assertIn("indigo", requests[0].prompt)
            self.assertIn("Blue coat", requests[0].prompt)
            self.assertIn("Dress color: #406CBF", requests[0].prompt)
            catalog = {asset["id"] for asset in library.profile(NICHE, options)["assets"]}
            self.assertEqual(catalog, {"mother", "grandpa", "pebble_garden", "blue_watering_can"})

            # A default edited after planning applies to future plans. This job's context is already saved.
            library.add_preset(NICHE, "visual", "day", "Day", {"style": "Glossy", "palette": "yellow"}, {}, True)
            assets.update_character(NICHE, "mother", "", "", "#EB5300", "blue_coat")
            production.media.queue_keyframes()
            opening = next(task for task in production.media.tasks() if task["scene_id"] == "opening")
            request, refs = production._request(opening)
            self.assertIn("indigo", request.prompt)
            self.assertIn("Mother (mother) from angle side", request.prompt)
            self.assertIn("Grandpa (grandpa) from angle side", request.prompt)
            self.assertIn("Dress color: #406CBF", request.prompt)
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
