import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

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

    def prepare_scheduled_job(self, *, auto_approve_keyframes):
        plan_json = self.store.get_job(self.job)["plan_json"]
        schedule = self.store.create_schedule(
            ["Scheduled garden video"], "2026-11-09", "09:00", "Asia/Dhaka", 1,
            auto_approve_keyframes=auto_approve_keyframes,
        )
        self.store.set_schedule_status(schedule, "active")
        self.store.dispatch_due(NOW)
        job = self.store.list_jobs()[0]["id"]
        with self.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET status = 'planned', plan_json = ? WHERE id = ?", (plan_json, job))
        return job, schedule

    def test_targeted_production_runs_only_the_selected_job(self):
        selected, _ = self.prepare_scheduled_job(auto_approve_keyframes=False)
        with patch("app.production.adapter_for", self.adapter), \
                patch("app.production.local_image", return_value=FRAME), \
                patch("app.production.download_artifact", side_effect=lambda url, path, kind: self.write_fixture(path)):
            self.production.advance(NOW, job_id=selected)

        self.assertEqual(self.store.get_job(self.job)["status"], "planned")
        self.assertEqual(self.production.media.job_tasks(self.job), [])
        self.assertEqual(self.store.get_job(selected)["status"], "review_keyframes")
        self.assertEqual([name for name, _ in self.requests], ["fal-nano-banana-pro"])

    def test_generation_preflight_blocks_incompatible_animation_and_insufficient_budget(self):
        plan = json.loads(self.store.get_job(self.job)["plan_json"])
        plan[0]["duration"] = 5
        with self.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET plan_json = ? WHERE id = ?", (json.dumps(plan), self.job))
        self.settings.set_key("EVOLINK_API_KEY", None)
        with patch("app.production.adapter_for") as provider:
            self.production.advance(NOW, job_id=self.job)
            self.assertIn("5-second scenes", self.store.get_job(self.job)["stage_error"])
            self.assertEqual(self.store.get_job(self.job)["status"], "planned")

            self.settings.set_key("EVOLINK_API_KEY", "replacement-key")
            self.settings.set_budgets(100_000, 120)
            self.production.advance(NOW, job_id=self.job)
            self.assertIn("Per-video budget", self.store.get_job(self.job)["stage_error"])
            provider.assert_not_called()
        self.assertEqual(self.production.media.job_tasks(self.job), [])

    def configure_generation(self, job, *, scene_plan=False, separate_voice=False, **fields):
        generation = {
            "model": "fal-seedance-25-flash", "scene_plan": scene_plan, "separate_voice": separate_voice,
            "duration": 30, "resolution": "720p", "aspect_ratio": "9:16", "elements": [],
            **fields,
        }
        options = {"generation": generation, "characters": ["tavi_seed_sprite"]}
        with self.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET production_options_json = ? WHERE id = ?", (json.dumps(options), job))

    def test_direct_native_uses_saved_images_and_voice_and_resumes_the_accepted_task(self):
        job = self.store.create_direct_job("Tavi speaks in the garden")
        self.configure_generation(job, voice_reference="tavi_seed_sprite", elements=["pebble_garden"])
        self.production.library.assets.save_character_audio(
            "kids_3d", "tavi_seed_sprite", url="https://audio.example.com/tavi.wav",
        )
        self.settings.set_key("ANTHROPIC_API_KEY", None)
        self.settings.update_provider("fal-elevenlabs", False, 10, 100)
        self.settings.update_provider("fal-seedance-25-flash", True, 40, 100)
        self.settings.set_budgets(3000, 3000)
        video = Mock()
        video.submit.return_value = ProviderResult("pending", task_id="direct-task")
        video.poll.return_value = ProviderResult("completed", url="https://fixture.example/video.mp4")

        with patch("app.production.adapter_for", return_value=video) as adapter, \
                patch("app.production.local_image", return_value=FRAME), \
                patch("app.production.download_artifact", side_effect=lambda url, path, kind: self.write_fixture(path)), \
                patch("app.production.render_direct_video", side_effect=lambda clip, voice, path, duration: self.write_fixture(path)) as render:
            self.production.planner.process_plan(NOW, job_id=job)
            self.assertEqual(self.store.get_job(job)["status"], "queued")
            self.production.advance(NOW, job_id=job)
            self.assertEqual(self.production.media.job_tasks(job)[0]["provider_task_id"], "direct-task")
            self.production.recover_interrupted()
            self.production.advance(NOW, job_id=job)

        self.assertTrue(all(call.args[0]["name"] == "fal-seedance-25-flash" for call in adapter.call_args_list))
        video.submit.assert_called_once()
        request = video.submit.call_args.args[0]
        self.assertIn("@Audio1", request.prompt)
        self.assertIn("@Image2", request.prompt)
        self.assertEqual(request.reference_images, (FRAME, FRAME))
        self.assertEqual(request.audio_references, ("https://audio.example.com/tavi.wav",))
        self.assertTrue(request.generate_audio)
        video.poll.assert_called_once_with("direct-task")
        self.assertIsNone(render.call_args.args[1])
        result = self.store.get_job(job)
        self.assertEqual(result["status"], "preview_ready")
        self.assertIsNone(result["plan_json"])
        self.assertTrue((self.production.root / result["preview_path"]).is_file())

    def test_direct_separate_voice_is_explicit_and_preflights_total_cost(self):
        job = self.store.create_direct_job("Animate Tavi")
        self.configure_generation(job, separate_voice=True, narration="Look at that seed!")
        self.settings.update_provider("fal-seedance-25-flash", True, 40, 100)
        self.settings.set_budgets(3000, 3000)
        video = Mock()
        audio = Mock()
        video.submit.return_value = ProviderResult("completed", url="https://fixture.example/video.mp4")
        audio.submit.return_value = ProviderResult("completed", url="https://fixture.example/audio.mp3")

        with patch("app.production.adapter_for", side_effect=lambda route, key: {
            "fal-seedance-25-flash": video, "fal-elevenlabs": audio,
        }[route["name"]]) as adapter, patch("app.production.local_image", return_value=FRAME), \
                patch("app.production.download_artifact", side_effect=lambda url, path, kind: self.write_fixture(path)), \
                patch("app.production.render_direct_video", side_effect=lambda clip, voice, path, duration: self.write_fixture(path)) as render:
            self.production.advance(NOW, job_id=job)
            self.assertIn("Daily budget", self.store.get_job(job)["stage_error"])
            adapter.assert_not_called()
            self.settings.set_budgets(3100, 3100)
            self.production.advance(NOW, job_id=job)

        video.submit.assert_called_once()
        self.assertFalse(video.submit.call_args.args[0].generate_audio)
        audio.submit.assert_called_once()
        self.assertEqual(audio.submit.call_args.args[0].prompt, "Look at that seed!")
        self.assertEqual(render.call_args.args[1].name, "direct-voice.mp3")
        self.assertEqual(self.store.get_job(job)["status"], "preview_ready")

    def test_storyboard_can_request_native_dialogue_without_any_voice_task(self):
        self.configure_generation(self.job, scene_plan=True)
        self.settings.update_provider("fal-elevenlabs", False, 10, 100)
        self.settings.update_provider("fal-veo", False, 10, 100)
        scenes = json.loads(self.store.get_job(self.job)["plan_json"])
        scenes[0]["dialogue"] = "Look, a seed!"
        with self.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET plan_json = ? WHERE id = ?", (json.dumps(scenes), self.job))
        with patch("app.production.adapter_for", self.adapter), \
                patch("app.production.local_image", return_value=FRAME), \
                patch("app.production.download_artifact", side_effect=lambda url, path, kind: self.write_fixture(path)), \
                patch("app.production.sample_frames", return_value=[Path("fixture.jpg")]), \
                patch("app.production.render_video", side_effect=lambda scenes, clips, voices, path, **kw: self.write_fixture(path)) as render:
            self.production.advance(NOW, job_id=self.job)
            self.production.media.approve_keyframes(self.job)
            self.production.advance(NOW, job_id=self.job)
            self.production.advance(NOW, job_id=self.job)
        self.assertEqual([name for name, _ in self.requests], ["fal-nano-banana-pro", "evolink-seedance"])
        request = self.requests[-1][1]
        self.assertTrue(request.generate_audio)
        self.assertIn("Look, a seed!", request.prompt)
        self.assertTrue(render.call_args.kwargs["native_audio"])
        self.assertEqual(self.store.get_job(self.job)["status"], "preview_ready")

    def test_opted_in_schedule_uses_visual_qa_before_creating_clip_tasks(self):
        job, _ = self.prepare_scheduled_job(auto_approve_keyframes=True)
        with patch("app.production.adapter_for", self.adapter), \
                patch("app.production.local_image", return_value=FRAME), \
                patch("app.production.download_artifact", side_effect=lambda url, path, kind: self.write_fixture(path)), \
                patch.object(self, "submit", wraps=self.submit) as review:
            self.production.advance(NOW, job_id=job)

        review.assert_called_once()
        self.assertIn("Review only the submitted opening keyframe", review.call_args.args[0].prompt)
        self.assertEqual(self.store.get_job(job)["status"], "making_media")
        self.assertTrue(any(task["kind"] == "video" for task in self.production.media.job_tasks(job)))

    def test_uncertain_keyframe_review_keeps_manual_gate_and_does_not_repeat(self):
        job, _ = self.prepare_scheduled_job(auto_approve_keyframes=True)
        with patch("app.production.adapter_for", self.adapter), \
                patch("app.production.local_image", return_value=FRAME), \
                patch("app.production.download_artifact", side_effect=lambda url, path, kind: self.write_fixture(path)), \
                patch.object(self, "submit", side_effect=ProviderError("Unknown visual QA outcome", uncertain=True)) as review:
            self.production.advance(NOW, job_id=job)
            self.production.recover_interrupted()
            self.production.advance(NOW, job_id=job)

        review.assert_called_once()
        self.assertEqual(self.store.get_job(job)["status"], "review_keyframes")
        self.assertIn("manual attention", self.store.get_job(job)["stage_error"])
        self.assertTrue(all(task["kind"] == "keyframe" for task in self.production.media.job_tasks(job)))
        self.production.media.approve_keyframes(job)
        self.assertEqual(self.store.get_job(job)["status"], "making_media")

    def test_interrupted_automatic_keyframe_claim_waits_for_manual_review(self):
        job, _ = self.prepare_scheduled_job(auto_approve_keyframes=True)
        with patch("app.production.adapter_for", self.adapter), \
                patch("app.production.local_image", return_value=FRAME), \
                patch("app.production.download_artifact", side_effect=lambda url, path, kind: self.write_fixture(path)), \
                patch.object(self.production, "_automatic_keyframe_review"):
            self.production.advance(NOW, job_id=job)

        self.assertIsNotNone(self.production.media.claim_automatic_keyframe_review(job_id=job))
        self.assertIsNone(self.store.get_job(job)["stage_error"])
        self.production.recover_interrupted()

        self.assertIn("restarted during automatic keyframe review", self.store.get_job(job)["stage_error"])
        self.assertIsNone(self.production.media.claim_automatic_keyframe_review(job_id=job))

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

    def prepare_animation_task(self):
        media = self.production.media
        media.queue_keyframes()
        frame, = media.tasks()
        self.complete_scene_keyframe(frame["scene_id"])
        media.approve_keyframes(self.job)
        return next(task for task in media.tasks() if task["kind"] == "video")

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

    def test_access_rejection_releases_spend_and_falls_back_without_losing_warning(self):
        task = self.prepare_animation_task()
        self.settings.set_budgets(500, 500)
        primary = Mock()
        primary.submit.side_effect = ProviderError("Access denied", retryable=True, warning_code="access_denied")
        fallback = Mock()
        fallback.submit.return_value = ProviderResult("pending", task_id="fallback-task")
        fallback.poll.return_value = ProviderResult("completed", url="https://fixture.example/file")
        adapters = {"fal-veo": primary, "evolink-seedance": fallback}

        with patch("app.production.adapter_for", side_effect=lambda route, key: adapters[route["name"]]) as adapter_for, \
                patch("app.production.local_image", return_value=FRAME), \
                patch("app.production.download_artifact", side_effect=lambda url, path, kind: self.write_fixture(path)):
            self.production._process(task, NOW)
            pending = next(row for row in self.production.media.tasks() if row["id"] == task["id"])
            self.assertEqual((pending["status"], pending["provider_name"], pending["attempts"]),
                             ("pending", "evolink-seedance", 2))
            self.settings.update_platform_categories("EVOLINK_API_KEY", [])
            self.production._process(pending, NOW)

        self.assertEqual([call.args[0]["name"] for call in adapter_for.call_args_list],
                         ["fal-veo", "evolink-seedance", "evolink-seedance"])
        fallback.poll.assert_called_once_with("fallback-task")
        self.assertEqual(
            next(route for route in self.settings.list_providers() if route["name"] == "fal-veo")["warning_code"],
            "access_denied",
        )
        with self.store._connection() as db:
            reservations = db.execute(
                "SELECT provider_name, status FROM spend_reservations WHERE job_id = ? ORDER BY id", (self.job,),
            ).fetchall()
        self.assertEqual([tuple(row) for row in reservations], [
            ("fal-nano-banana-pro", "settled"), ("fal-veo", "released"), ("evolink-seedance", "settled"),
        ])

    def test_uncertain_video_submission_does_not_fall_back(self):
        task = self.prepare_animation_task()
        primary = Mock()
        primary.submit.side_effect = ProviderError("Provider connection failed.", uncertain=True)
        with patch("app.production.adapter_for", return_value=primary) as adapter_for, \
                patch("app.production.local_image", return_value=FRAME):
            self.production._process(task, NOW)

        adapter_for.assert_called_once()
        self.assertEqual(adapter_for.call_args.args[0]["name"], "fal-veo")
        failed = next(row for row in self.production.media.job_tasks(self.job) if row["id"] == task["id"])
        self.assertEqual((failed["status"], failed["attempts"]), ("needs_review", 1))
        self.assertEqual(self.store.get_job(self.job)["status"], "needs_review")

    def test_rejected_animation_routes_report_blocking_reason_at_attempt_limit(self):
        task = self.prepare_animation_task()
        adapter = Mock()
        adapter.submit.side_effect = ProviderError("Access denied", retryable=True, warning_code="access_denied")
        reason = "No provider accepted this video request. Check provider access, account credit, or rate limits in Providers."
        with patch("app.production.adapter_for", return_value=adapter), patch("app.production.local_image", return_value=FRAME):
            self.production._process(task, NOW)
            queued = next(row for row in self.production.media.tasks() if row["id"] == task["id"])
            self.assertEqual((queued["attempts"], queued["error"]), (2, reason))
            self.production._process(queued, NOW)

        self.assertEqual(adapter.submit.call_count, 3)
        failed = next(row for row in self.production.media.job_tasks(self.job) if row["id"] == task["id"])
        self.assertEqual((failed["status"], failed["attempts"], failed["error"]), ("failed", 3, reason))
        self.assertEqual(self.store.get_job(self.job)["stage_error"], reason)

    def test_missing_animation_routes_block_without_using_a_submission_attempt(self):
        task = self.prepare_animation_task()
        self.settings.update_platform_categories("FAL_KEY", ["character", "audio"])
        self.settings.set_key("EVOLINK_API_KEY", None)
        with patch("app.production.adapter_for") as adapter_for, patch("app.production.local_image", return_value=FRAME):
            self.production._process(task, NOW)

        adapter_for.assert_not_called()
        blocked = next(row for row in self.production.media.tasks() if row["id"] == task["id"])
        self.assertEqual(blocked["attempts"], 0)
        self.assertEqual(blocked["error"], "Configure an eligible video route and rate in Settings.")

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
