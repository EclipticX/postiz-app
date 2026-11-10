"""Run approved scene plans through keyframes, clips, and local rendering."""

import json
import logging
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from itertools import zip_longest
from pathlib import Path
from uuid import uuid4

from app.media_routes import adapter_for, download_artifact, eligible_routes, estimated_micros, local_image
from app.media_store import MediaStore, can_edit_scene, queue_scene_media, scene_revision
from app.niche import load_profile
from app.providers import ProviderError, ProviderRequest
from app.publishing_store import PublishingStore
from app.render import render_video, sample_frames
from app.scenes import qa_prompt, spoken_lines
from app.studio_library import select_references


MAX_ATTEMPTS = 3
DEFAULT_VOICE = "Rachel"
MAX_ASSET_REFERENCES = 12
EXTENSIONS = {"keyframe": "png", "video": "mp4", "voice": "mp3"}
QA_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "score", "issues", "failed_scene_ids"],
    "properties": {
        "decision": {"type": "string", "enum": ["pass", "review", "retry"]},
        "score": {"type": "integer"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "failed_scene_ids": {"type": "array", "items": {"type": "string"}},
    },
}


class StudioProduction:
    def __init__(self, store, settings, planner):
        self.store = store
        self.media = MediaStore(store)
        self.posts = PublishingStore(store)
        self.settings = settings
        self.planner = planner
        self.library = planner.library
        self.root = store.path.parent / "media"
        self._qa_round = deque()

    def recover_interrupted(self):
        self.media.recover_interrupted()
        with self.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET status = 'render_ready' WHERE status = 'rendering'")
            db.execute(
                "UPDATE jobs SET status = 'needs_review', stage_error = ? WHERE status = 'qa_running'",
                ("Studio restarted during visual QA. Review the saved preview.",),
            )

    def advance(self, now):
        self.media.queue_keyframes()
        tasks = self.media.tasks()[:4]
        if tasks:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda task: self._process(task, now), tasks))
        self._render()
        self._review(now)

    def revise(self, job_id, feedback):
        feedback = feedback.strip()
        if not feedback or len(feedback) > 4000:
            raise ValueError("Enter revision feedback in 1–4,000 characters.")
        self._reset_plan(job_id, feedback=feedback)

    def edit_scene(self, job_id, scene_id, action, camera, dialogue, *, regenerate_keyframe, expected_revision):
        action, camera = action.strip(), camera.strip()
        dialogue = [line.strip() for line in dialogue]
        if not 1 <= len(action) <= 4000 or not 1 <= len(camera) <= 4000:
            raise ValueError("Enter 1–4,000 characters for both the action and camera directions.")
        if any(len(line) > 4000 for line in dialogue):
            raise ValueError("Use at most 4,000 characters per dialogue line.")

        with self.store._connection(write=True) as db:
            job = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise LookupError("Video job not found.")
            scenes = json.loads(job["plan_json"] or "[]")
            index = next((i for i, scene in enumerate(scenes) if scene["scene_id"] == scene_id), None)
            if index is None:
                raise LookupError("Scene not found.")
            tasks = db.execute("SELECT * FROM media_tasks WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
            if expected_revision != scene_revision(job, tasks):
                raise ValueError("The scene plan or media changed. Reload the editor before saving.")
            if not can_edit_scene(job, tasks):
                raise ValueError("Wait for the current production step to finish before editing this scene.")
            if db.execute(
                "SELECT 1 FROM publishing_requests WHERE job_id = ? AND status IN ('queued', 'sending')", (job_id,),
            ).fetchone():
                raise ValueError("Wait for the approved Postiz handoff to finish before editing this scene.")

            scene = scenes[index]
            edited = dict(scene, action=action, camera=camera)
            if scene.get("dialogue_lines"):
                if len(dialogue) != len(scene["dialogue_lines"]) or not all(dialogue):
                    raise ValueError("Enter text for every existing dialogue line.")
                edited["dialogue_lines"] = [
                    dict(line, text=text) for line, text in zip(scene["dialogue_lines"], dialogue)
                ]
            else:
                if len(dialogue) != 1:
                    raise ValueError("Enter a single narration field.")
                edited["dialogue"] = dialogue[0]

            redraw = regenerate_keyframe or action != scene["action"] or camera != scene["camera"]
            changed_lines = [
                (job_id, scene_id, line_index)
                for line_index, (before, after) in enumerate(zip_longest(spoken_lines(scene), spoken_lines(edited)))
                if before != after
            ]
            if not redraw and not changed_lines:
                raise ValueError("Edit a field or select Regenerate keyframe.")

            db.executemany(
                "DELETE FROM media_tasks WHERE job_id = ? AND scene_id = ? AND kind = 'voice' AND line_index = ?",
                changed_lines,
            )
            status = job["status"]
            if redraw:
                db.execute(
                    "DELETE FROM media_tasks WHERE job_id = ? AND scene_id = ? AND kind IN ('keyframe', 'video')",
                    (job_id, scene_id),
                )
                db.execute("INSERT INTO media_tasks (job_id, scene_id, kind) VALUES (?, ?, 'keyframe')", (job_id, scene_id))
                status = "making_keyframes"
            elif status != "review_keyframes":
                queue_scene_media(db, job_id, [edited])
                pending = db.execute("SELECT 1 FROM media_tasks WHERE job_id = ? AND status = 'queued'", (job_id,))
                status = "making_media" if pending.fetchone() else "render_ready"

            scenes[index] = edited
            db.execute(
                "UPDATE jobs SET plan_json = ?, status = ?, preview_path = NULL, qa_json = ?, stage_error = NULL WHERE id = ?",
                (json.dumps(scenes), status, json.dumps({"auto_retry": False}), job_id),
            )

    def configure(self, job_id, options):
        job = self.store.get_job(job_id)
        if job is None:
            raise LookupError("Video job not found.")
        self.library.validate_options(job["niche_id"] or "kids_3d", options)
        self._reset_plan(job_id, options=options)

    def _reset_plan(self, job_id, *, feedback=None, options=None):
        with self.store._connection(write=True) as db:
            job = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise LookupError("Video job not found.")
            if options is not None:
                previous = json.loads(job["production_options_json"])
                defaults = {"characters": []}
                if all(previous.get(group, defaults.get(group, {})) == value for group, value in options.items()):
                    return
            allowed = {"review_keyframes", "preview_ready", "needs_review"}
            if options is not None:
                allowed.add("queued")
            if job["status"] not in allowed:
                raise ValueError("Wait for the current production step to finish before revising.")
            if db.execute(
                "SELECT 1 FROM media_tasks WHERE job_id = ? AND status IN ('submitting', 'pending')", (job_id,),
            ).fetchone():
                raise ValueError("Wait for the submitted provider tasks to finish before revising.")
            if db.execute(
                "SELECT 1 FROM publishing_requests WHERE job_id = ? AND status IN ('queued', 'sending')", (job_id,),
            ).fetchone():
                raise ValueError("Wait for the approved Postiz handoff to finish before revising.")
            db.execute("DELETE FROM media_tasks WHERE job_id = ?", (job_id,))
            db.execute(
                """UPDATE jobs SET status = 'queued', plan_json = NULL, plan_attempts = 0,
                    plan_feedback = NULL, stage_error = NULL, preview_path = NULL, qa_json = NULL,
                    revision_feedback = ?, production_context_json = NULL, production_options_json = ?
                    WHERE id = ?""",
                (feedback if feedback is not None else job["revision_feedback"],
                 json.dumps(options) if options is not None else job["production_options_json"], job_id),
            )

    def retry_render(self, job_id):
        with self.store._connection(write=True) as db:
            job = db.execute("SELECT status, preview_path FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise LookupError("Video job not found.")
            media = db.execute(
                "SELECT kind, status FROM media_tasks WHERE job_id = ? AND kind IN ('video', 'voice')", (job_id,),
            ).fetchall()
            if (job["status"] != "needs_review" or job["preview_path"]
                    or not any(task["kind"] == "video" for task in media)
                    or any(task["status"] != "completed" for task in media)):
                raise ValueError("A local render can be retried once all scene media is ready.")
            db.execute("UPDATE jobs SET status = 'render_ready', stage_error = NULL WHERE id = ?", (job_id,))

    def _request(self, task):
        scene = next(scene for scene in json.loads(task["plan_json"]) if scene["scene_id"] == task["scene_id"])
        profile = self._job_profile(task)
        if task["kind"] == "voice":
            line = spoken_lines(scene)[task["line_index"]]
            speaker = next((asset for asset in profile["assets"] if asset["id"] == line.get("speaker_id")), {})
            voice = speaker.get("selected_voice") or {}
            if not speaker.get("voice_override") and line.get("voice_style_id"):
                voice = next((style for style in speaker.get("voice_styles", [])
                              if style["id"] == line["voice_style_id"]), voice)
            return ProviderRequest(
                prompt=line["text"], voice=voice.get("voice") or profile.get("voice", DEFAULT_VOICE),
                voice_speed=voice.get("speed"), voice_expressiveness=voice.get("expressiveness"),
            ), []
        shot = f'{scene["action"]}\nCamera: {scene["camera"]}\nStyle: {profile["style_guide"]}'
        if task["kind"] == "video":
            keyframe = next(row for row in self.media.job_tasks(task["job_id"])
                            if row["kind"] == "keyframe" and row["scene_id"] == task["scene_id"])
            return ProviderRequest(
                prompt=shot + "\nAnimate the approved first frame and keep the character design fixed.",
                reference_image=local_image(self.root / keyframe["local_path"]),
                duration=scene["duration"],
            ), []

        asset_ids = set(scene["characters"] + [scene["place"]] + scene.get("items", []))
        assets = [asset for asset in profile["assets"] if asset["id"] in asset_ids]
        descriptions = "\n".join(f'{asset["name"]} ({asset["id"]}): {asset["description"]}' for asset in assets)
        by_asset = [(asset, select_references(asset, scene["camera"])) for asset in assets]
        selected = [(asset, refs[0]) for asset, refs in by_asset]
        selected.extend((asset, refs[1]) for asset, refs in by_asset if len(refs) > 1)
        selected = selected[:MAX_ASSET_REFERENCES]
        labels = "\n".join(f"Image {index} shows {asset['name']} ({asset['id']}) from angle {ref['angle']}."
                           for index, (asset, ref) in enumerate(selected, start=1))
        prompt = f"Create the opening frame. Match each reference to its labeled asset.\n{shot}\n{descriptions}\n{labels}"
        return ProviderRequest(prompt=prompt), [reference for _, reference in selected]

    @staticmethod
    def _job_profile(job):
        if job["production_context_json"]:
            return json.loads(job["production_context_json"])
        return load_profile(job["niche_id"] or "kids_3d")

    def _process(self, task, now):
        try:
            if task["status"] == "pending":
                self._poll(task)
                return
            request, references = self._request(task)
            routes = eligible_routes(self.settings, task["kind"], request)
            if task["provider_name"] and len(routes) > 1:
                routes.sort(key=lambda route: route["name"] == task["provider_name"])
            if not routes:
                self.media.blocked(task["id"], f"Configure an eligible {task['kind']} route and rate in Settings.")
                return
            if references:
                request = replace(request, reference_images=tuple(
                    local_image(self.library.assets.reference_path(ref)) for ref in references
                ))
            self._submit(task, request, routes, now)
        except (OSError, ValueError, ProviderError) as error:
            self.media.blocked(task["id"], str(error))

    def _submit(self, task, request, routes, now):
        wait_reason = "Configure an eligible provider and budget in Settings."
        for route in routes:
            key = self.settings.key(route["key_env"])
            if not key:
                continue
            try:
                adapter = adapter_for(route, key)
                reservation = self.settings.reserve(
                    task["job_id"], route["name"], estimated_micros(route, request), now,
                )
            except (ValueError, ProviderError) as error:
                wait_reason = str(error)
                continue
            if not self.media.begin(task["id"], route["name"], reservation):
                self.settings.release(reservation)
                return

            task = dict(task, provider_name=route["name"], reservation_id=reservation, attempts=task["attempts"] + 1)
            try:
                result = adapter.submit(request)
            except ProviderError as error:
                self._provider_failure(task, error)
                return
            self._result(task, result)
            return
        self.media.blocked(task["id"], wait_reason)

    def _poll(self, task):
        route = next((row for row in self.settings.list_providers() if row["name"] == task["provider_name"]), None)
        key = self.settings.key(route["key_env"]) if route else None
        if not key:
            self.media.blocked(task["id"], "Restore the key for the submitted provider to resume polling.")
            return
        try:
            result = adapter_for(route, key).poll(task["provider_task_id"])
        except ProviderError as error:
            if error.terminal:
                self._provider_failure(task, error)
            else:
                self.media.blocked(task["id"], str(error))
            return
        self._result(task, result)

    def _provider_failure(self, task, error):
        self.settings.settle(task["reservation_id"])
        if error.uncertain:
            status = "needs_review"
        elif error.retryable and task["attempts"] < MAX_ATTEMPTS:
            status = "queued"
        else:
            status = "failed"
        self.media.problem(task["id"], str(error), status)

    def _result(self, task, result):
        if result.task_id and not task["provider_task_id"]:
            self.media.pending(task["id"], result.task_id)
            task = dict(task, provider_task_id=result.task_id)
        if result.status == "pending":
            return

        suffix = f"-{task['line_index'] + 1}" if task["line_index"] else ""
        path = Path(str(task["job_id"])) / f"{task['scene_id']}-{task['kind']}{suffix}.{EXTENSIONS[task['kind']]}"
        try:
            path = download_artifact(result.url, self.root / path, task["kind"]).relative_to(self.root)
        except (OSError, ValueError, ProviderError) as error:
            if task["provider_task_id"]:
                self.media.blocked(task["id"], str(error))
            else:
                self.settings.settle(task["reservation_id"])
                self.media.problem(task["id"], str(error), "needs_review")
            return
        self.settings.settle(task["reservation_id"])
        self.media.complete(task["id"], path.as_posix())

    def _take_job(self, status, running, *, job_id=None):
        with self.store._connection(write=True) as db:
            job = db.execute(
                "SELECT * FROM jobs WHERE status = ? AND (? IS NULL OR id = ?) ORDER BY id LIMIT 1",
                (status, job_id, job_id),
            ).fetchone()
            if job:
                db.execute("UPDATE jobs SET status = ? WHERE id = ?", (running, job["id"]))
            return job

    def _render(self):
        job = self._take_job("render_ready", "rendering")
        if job is None:
            return
        tasks = self.media.job_tasks(job["id"])
        scenes = json.loads(job["plan_json"])
        clips = {task["scene_id"]: self.root / task["local_path"] for task in tasks if task["kind"] == "video"}
        voices = {}
        for scene in scenes:
            lines = sorted((task for task in tasks if task["kind"] == "voice" and task["scene_id"] == scene["scene_id"]),
                           key=lambda task: task["line_index"])
            if lines:
                paths = [self.root / line["local_path"] for line in lines]
                voices[scene["scene_id"]] = paths if scene.get("dialogue_lines") else paths[0]
        preview = Path(str(job["id"])) / f"preview.{uuid4().hex}.mp4"
        music = self.store.path.parent / "music.mp3"
        try:
            presets = self._job_profile(job).get("render_presets", {})
            kwargs = {"music_path": music if music.is_file() else None}
            if "audio" in presets:
                niche = job["niche_id"] or "kids_3d"
                audio = presets["audio"]
                kwargs = {f"{kind}_path": self.library.audio_path(niche, audio[kind]) if kind in audio else None
                          for kind in ("music", "sfx")}
            if "subtitle" in presets:
                kwargs["subtitle_style"] = presets["subtitle"]
            render_video(scenes, clips, voices, self.root / preview, **kwargs)
        except (OSError, RuntimeError, ValueError) as error:
            self._update_job(job["id"], "needs_review", str(error))
            return
        with self.store._connection(write=True) as db:
            db.execute(
                "UPDATE jobs SET status = 'qa_pending', preview_path = ?, stage_error = NULL WHERE id = ?",
                (preview.as_posix(), job["id"]),
            )
        delivered = self.posts.delivered_preview_paths(job["id"])
        for previous in (self.root / preview.parent).glob("preview.*"):
            if previous.relative_to(self.root).as_posix() in delivered:
                continue
            if previous.stem != preview.stem and re.fullmatch(r"preview(?:\.[0-9a-f]{32})?\.(?:mp4|srt)", previous.name):
                try:
                    previous.unlink(missing_ok=True)
                except OSError:
                    logging.warning("Could not remove superseded preview file %s", previous)

    def _review(self, now):
        # Give each pending job one turn before retrying jobs blocked by budget or settings.
        if not self._qa_round:
            with self.store._connection() as db:
                self._qa_round.extend(
                    row["id"] for row in db.execute("SELECT id FROM jobs WHERE status = 'qa_pending' ORDER BY id")
                )
        if not self._qa_round:
            return

        job = self._take_job("qa_pending", "qa_running", job_id=self._qa_round.popleft())
        if job is None:
            return
        try:
            adapter, reservation = self.planner._claude(job["id"], now)
        except ValueError as error:
            self._update_job(job["id"], "qa_pending", str(error))
            return
        submitted = False
        try:
            profile = self._job_profile(job)
            scenes = json.loads(job["plan_json"])
            path = self.root / job["preview_path"]
            frames = sample_frames(path, path.parent / "qa", scenes)
            used_assets = {asset_id for scene in scenes for asset_id in scene["characters"] + [scene["place"]] + scene.get("items", [])}
            cast = [asset for asset in profile["assets"] if asset["id"] in used_assets][:MAX_ASSET_REFERENCES]
            references = [select_references(asset, "front")[0] for asset in cast]
            reference_order = ", ".join(asset["id"] for asset in cast)
            request = ProviderRequest(
                prompt=qa_prompt(profile, scenes) + f"\nThe initial references match these asset IDs in order: {reference_order}."
                " The remaining images are scene samples in timeline order. Include failed_scene_ids for visual retries.",
                reference_images=tuple(local_image(self.library.assets.reference_path(ref)) for ref in references)
                + tuple(local_image(frame) for frame in frames), output_schema=QA_SCHEMA,
            )
            submitted = True
            result = adapter.submit(request)
            qa = result.data
            if (not isinstance(qa, dict) or qa.get("decision") not in {"pass", "review", "retry"}
                    or type(qa.get("score")) is not int or not 0 <= qa["score"] <= 100
                    or not isinstance(qa.get("issues"), list) or not isinstance(qa.get("failed_scene_ids"), list)):
                raise ValueError("Claude returned invalid visual QA results.")
        except (OSError, RuntimeError, ValueError) as error:
            self._update_job(job["id"], "needs_review", str(error))
            return
        finally:
            if submitted:
                self.settings.settle(reservation)
            else:
                self.settings.release(reservation)

        with self.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET qa_json = ? WHERE id = ?", (json.dumps(qa), job["id"]))
            tasks = db.execute("SELECT * FROM media_tasks WHERE job_id = ? AND kind = 'video'", (job["id"],)).fetchall()
            failed_ids = {value for value in qa["failed_scene_ids"] if isinstance(value, str)}
            retries = [task for task in tasks if task["scene_id"] in failed_ids and task["attempts"] < MAX_ATTEMPTS]
            # A scene edit authorizes only the requested regeneration. Let the user review further QA findings.
            auto_retry = json.loads(job["qa_json"] or "{}").get("auto_retry", True)
            if auto_retry and qa["decision"] == "retry" and retries and len(retries) == len(failed_ids):
                db.executemany(
                    """UPDATE media_tasks SET status = 'queued', provider_task_id = NULL,
                        reservation_id = NULL, local_path = NULL WHERE id = ?""",
                    [(task["id"],) for task in retries],
                )
                status = "making_media"
            else:
                status = "preview_ready" if qa["decision"] == "pass" and qa["score"] >= 85 else "needs_review"
            db.execute("UPDATE jobs SET status = ?, stage_error = NULL WHERE id = ?", (status, job["id"]))

    def _update_job(self, job_id, status, error):
        with self.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET status = ?, stage_error = ? WHERE id = ?", (status, error, job_id))
