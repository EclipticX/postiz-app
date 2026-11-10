import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.db import StudioStore
from app.media_store import scene_revision
from app.niche import load_profile
from app.planning import StudioPlanner
from app.production import StudioProduction
from app.providers import ProviderError, ProviderResult
from app.scenes import spoken_lines
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

    def prepare_scene_edit(self, *, structured_dialogue=False):
        scenes = load_profile()["few_shot"][0]["scenes"]
        if structured_dialogue:
            scenes[0]["dialogue"] = ""
            scenes[0]["dialogue_lines"] = [
                {"speaker_id": "tavi_seed_sprite", "text": "Hello!"},
                {"speaker_id": "tavi_seed_sprite", "text": "You look thirsty!"},
            ]
        preview = f"{self.job}/preview.mp4"
        self.write_fixture(self.production.root / preview)
        with self.store._connection(write=True) as db:
            db.execute(
                "UPDATE jobs SET status = 'preview_ready', plan_json = ?, preview_path = ?, qa_json = ? WHERE id = ?",
                (json.dumps(scenes), preview, json.dumps({"decision": "pass", "score": 95, "issues": []}), self.job),
            )
            for scene in scenes:
                media = [("keyframe", 0, "png"), ("video", 0, "mp4")]
                media.extend(("voice", index, "mp3") for index, _ in enumerate(spoken_lines(scene)))
                for kind, index, extension in media:
                    path = f"{self.job}/{scene['scene_id']}-{kind}-{index}.{extension}"
                    self.write_fixture(self.production.root / path)
                    db.execute(
                        """INSERT INTO media_tasks (job_id, scene_id, kind, line_index, status, attempts, local_path)
                           VALUES (?, ?, ?, ?, 'completed', 1, ?)""",
                        (self.job, scene["scene_id"], kind, index, path),
                    )
        return scenes

    def scene_revision(self):
        return scene_revision(self.store.get_job(self.job), self.production.media.job_tasks(self.job))

    def complete_scene_keyframe(self, scene_id):
        media = self.production.media
        frame, = media.tasks()
        self.assertEqual((frame["scene_id"], frame["kind"]), (scene_id, "keyframe"))
        reservation = self.settings.reserve(self.job, "fal-nano-banana-pro", 100, NOW)
        self.assertTrue(media.begin(frame["id"], "fal-nano-banana-pro", reservation))
        self.settings.settle(reservation)
        path = f"{self.job}/{scene_id}-keyframe-0.png"
        self.write_fixture(self.production.root / path)
        media.complete(frame["id"], path)
        self.assertEqual(self.store.get_job(self.job)["status"], "review_keyframes")
        self.assertEqual(media.tasks(), [])

    def test_scene_visual_edit_preserves_other_media_and_requires_fresh_keyframe_approval(self):
        scene, second_scene = self.prepare_scene_edit()
        scene_id = scene["scene_id"]
        media = self.production.media
        initial = media.job_tasks(self.job)
        untouched = {task["id"]: task for task in initial if task["scene_id"] != scene_id or task["kind"] == "voice"}
        original_revision = self.scene_revision()
        original_preview = self.store.get_job(self.job)["preview_path"]
        posts = self.production.posts
        request = posts.queue(self.job, {"id": "channel", "identifier": "youtube"}, "Title", "", [], NOW)
        posts.next_request()
        posts.finish(request, "published-post")

        self.production.edit_scene(
            self.job, scene_id, "Tavi waves.", scene["camera"], [scene["dialogue"]],
            regenerate_keyframe=False, expected_revision=original_revision,
        )
        job = self.store.get_job(self.job)
        self.assertEqual(job["status"], "making_keyframes")
        self.assertIsNone(job["preview_path"])
        self.assertNotIn("score", json.loads(job["qa_json"]))
        self.assertEqual(json.loads(job["plan_json"]), [dict(scene, action="Tavi waves."), second_scene])
        self.assertEqual({task["id"]: task for task in media.job_tasks(self.job) if task["id"] in untouched}, untouched)

        self.complete_scene_keyframe(scene_id)
        reviewed_revision = self.scene_revision()
        self.production.edit_scene(
            self.job, scene_id, "Tavi waves.", scene["camera"], [scene["dialogue"]],
            regenerate_keyframe=True, expected_revision=reviewed_revision,
        )
        self.complete_scene_keyframe(scene_id)
        with self.assertRaises(ValueError):
            media.approve_keyframes(self.job, expected_revision=reviewed_revision)
        media.approve_keyframes(self.job, expected_revision=self.scene_revision())
        clip, = media.tasks()
        self.assertEqual((clip["scene_id"], clip["kind"]), (scene_id, "video"))
        self.assertEqual({task["id"]: task for task in media.job_tasks(self.job) if task["id"] in untouched}, untouched)
        self.assertEqual(posts.job_posts(self.job)[0]["preview_path"], original_preview)
        self.assertTrue((self.production.root / original_preview).is_file())

    def test_scene_dialogue_edit_requeues_only_the_changed_structured_line(self):
        scene, other_scene = self.prepare_scene_edit(structured_dialogue=True)
        initial = self.production.media.job_tasks(self.job)
        original_revision = self.scene_revision()
        self.production.edit_scene(
            self.job, scene["scene_id"], scene["action"], scene["camera"], ["Hello!", "Let me help."],
            regenerate_keyframe=False, expected_revision=original_revision,
        )
        job = self.store.get_job(self.job)
        self.assertEqual(job["status"], "making_media")
        self.assertEqual(json.loads(job["plan_json"])[0]["dialogue_lines"], [
            scene["dialogue_lines"][0], dict(scene["dialogue_lines"][1], text="Let me help."),
        ])
        voice, = self.production.media.tasks()
        self.assertEqual((voice["scene_id"], voice["kind"], voice["line_index"]), (scene["scene_id"], "voice", 1))
        untouched = {
            task["id"]: task for task in initial
            if (task["scene_id"], task["kind"], task["line_index"]) != (scene["scene_id"], "voice", 1)
        }
        self.assertEqual(
            {task["id"]: task for task in self.production.media.job_tasks(self.job) if task["id"] in untouched},
            untouched,
        )

        qa = {"decision": "retry", "score": 60, "issues": ["Review the second scene."],
              "failed_scene_ids": [other_scene["scene_id"]]}
        with patch("app.production.adapter_for", self.adapter), \
                patch("app.production.local_image", return_value=FRAME), \
                patch("app.production.download_artifact", side_effect=lambda url, path, kind: self.write_fixture(path)), \
                patch("app.production.render_video", side_effect=lambda scenes, clips, voices, path, **kw: self.write_fixture(path)), \
                patch("app.production.sample_frames", return_value=[Path("fixture.jpg")]), \
                patch.object(self, "submit", return_value=ProviderResult("completed", data=qa)):
            self.production.advance(NOW)

        self.assertEqual(self.store.get_job(self.job)["status"], "needs_review")
        self.assertEqual(self.production.media.tasks(), [])
        self.assertEqual(
            {task["id"]: task for task in self.production.media.job_tasks(self.job) if task["id"] in untouched},
            untouched,
        )

    def test_new_render_replaces_the_preview_version(self):
        original = Path(str(self.job)) / "preview.mp4"
        original_file = self.write_fixture(self.production.root / original)
        original_subtitles = self.write_fixture(original_file.with_suffix(".srt"))
        backup = self.write_fixture(original_file.with_name("preview.backup.mp4"))
        published = self.write_fixture(original_file.with_name(f"preview.{'0' * 32}.mp4"))
        with self.store._connection(write=True) as db:
            db.execute(
                "UPDATE jobs SET status = 'preview_ready', preview_path = ? WHERE id = ?",
                (published.relative_to(self.production.root).as_posix(), self.job),
            )
        posts = self.production.posts
        request = posts.queue(self.job, {"id": "channel", "identifier": "youtube"}, "Title", "", [], NOW)
        posts.next_request()
        posts.finish(request, "published-post")
        with self.store._connection(write=True) as db:
            db.execute(
                "UPDATE jobs SET status = 'render_ready', preview_path = ? WHERE id = ?",
                (original.as_posix(), self.job),
            )
        with patch("app.production.render_video", side_effect=lambda scenes, clips, voices, path, **kw: self.write_fixture(path)):
            self.production._render()
            first = self.store.get_job(self.job)["preview_path"]
            with self.store._connection(write=True) as db:
                db.execute("UPDATE jobs SET status = 'render_ready' WHERE id = ?", (self.job,))
            self.production._render()

        second = self.store.get_job(self.job)["preview_path"]
        self.assertNotEqual(first, second)
        self.assertFalse(original_file.exists())
        self.assertFalse(original_subtitles.exists())
        self.assertFalse((self.production.root / first).exists())
        self.assertTrue((self.production.root / second).is_file())
        self.assertTrue(backup.is_file())
        self.assertTrue(published.is_file())

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

    def test_qa_rotates_past_budget_blocked_jobs_and_retries_them(self):
        for prompt in ("Another garden scene", "A garden scene with room in its budget"):
            self.store.decide_idea(self.store.add_idea(prompt), "accept")
        job_ids = sorted(job["id"] for job in self.store.list_jobs())
        scene_plan = self.store.get_job(self.job)["plan_json"]
        with self.store._connection(write=True) as db:
            db.executemany(
                "UPDATE jobs SET status = 'qa_pending', preview_path = ?, plan_json = ? WHERE id = ?",
                [(f"{job_id}/preview.mp4", scene_plan, job_id) for job_id in job_ids],
            )
        self.settings.set_budgets(100_000, 100)
        for job_id in job_ids[:2]:
            reservation = self.settings.reserve(job_id, "claude-sonnet", 100, NOW)
            self.settings.settle(reservation)

        with patch("app.production.sample_frames", return_value=[Path("fixture.jpg")]), \
                patch("app.production.local_image", return_value=FRAME):
            for _ in job_ids:
                self.production.advance(NOW)

            self.assertEqual(self.store.get_job(job_ids[2])["status"], "preview_ready")
            for job_id in job_ids[:2]:
                job = self.store.get_job(job_id)
                self.assertEqual(job["status"], "qa_pending")
                self.assertIn("Per-video budget", job["stage_error"])

            self.settings.set_budgets(100_000, 200)
            for _ in job_ids[:2]:
                self.production.advance(NOW)

        for job_id in job_ids[:2]:
            self.assertEqual(self.store.get_job(job_id)["status"], "preview_ready")
