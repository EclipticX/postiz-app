"""Durable scene tasks and the keyframe approval gate."""

import hashlib
import json

from app.scenes import spoken_lines


MAX_MEDIA_ATTEMPTS = 3
ACTIVE_JOB_STATUSES = ("making_keyframes", "making_media")
SCENE_EDIT_STATUSES = ("review_keyframes", "preview_ready", "needs_review")
_AUTO_KEYFRAME_JOBS = """
    SELECT jobs.* FROM jobs
    JOIN schedule_entries ON schedule_entries.id = jobs.schedule_entry_id
    JOIN prompt_schedules ON prompt_schedules.id = schedule_entries.schedule_id
    JOIN schedule_generation_options ON schedule_generation_options.schedule_id = prompt_schedules.id
    WHERE jobs.status = 'review_keyframes' AND jobs.revision_feedback IS NULL
        AND prompt_schedules.status = 'active' AND prompt_schedules.deleted_at IS NULL
        AND schedule_generation_options.auto_approve_keyframes = 1
        AND (? IS NULL OR jobs.id = ?) ORDER BY jobs.id
"""


def can_edit_scene(job, tasks):
    return (job["status"] in SCENE_EDIT_STATUSES and bool(job["plan_json"]) and bool(tasks)
            and all(task["status"] == "completed" and task["local_path"] for task in tasks))


def scene_revision(job, tasks):
    # A new provider reservation distinguishes regenerated frames even when paths are reused.
    snapshot = [job["id"], job["status"], job["plan_json"], [
        (task["id"], task["status"], task["local_path"], task["reservation_id"]) for task in tasks
    ]]
    return hashlib.sha256(json.dumps(snapshot).encode("utf-8")).hexdigest()


def queue_scene_media(db, job_id, scenes):
    tasks = []
    for scene in scenes:
        tasks.append((job_id, scene["scene_id"], "video", 0))
        tasks.extend(
            (job_id, scene["scene_id"], "voice", index)
            for index, _ in enumerate(spoken_lines(scene))
        )
    db.executemany(
        """INSERT INTO media_tasks (job_id, scene_id, kind, line_index) VALUES (?, ?, ?, ?)
        ON CONFLICT (job_id, scene_id, kind, line_index) DO NOTHING""",
        tasks,
    )


def ready_keyframes(scenes, tasks):
    ready = {
        task["scene_id"]: task["local_path"] for task in tasks
        if task["kind"] == "keyframe" and task["status"] == "completed" and task["local_path"]
    }
    if set(ready) != {scene["scene_id"] for scene in scenes}:
        raise ValueError("Wait for every keyframe before approving.")
    return [ready[scene["scene_id"]] for scene in scenes]


def _load_scenes(plan_json):
    try:
        scenes = json.loads(plan_json or "null")
    except (TypeError, ValueError) as error:
        raise ValueError("The saved scene plan is invalid.") from error
    if (
        not isinstance(scenes, list)
        or not scenes
        or any(
            not isinstance(scene, dict)
            or not isinstance(scene.get("scene_id"), str)
            or not scene["scene_id"]
            or not isinstance(scene.get("dialogue"), str)
            for scene in scenes
        )
        or len({scene["scene_id"] for scene in scenes}) != len(scenes)
    ):
        raise ValueError("The saved scene plan is invalid.")
    return scenes


class MediaStore:
    def __init__(self, store):
        self.store = store

    def queue_keyframes(self, *, job_id=None, preflight=None):
        queued = 0
        with self.store._connection(write=True) as db:
            jobs = db.execute(
                "SELECT id, plan_json FROM jobs WHERE status = 'planned' AND (? IS NULL OR id = ?) ORDER BY id",
                (job_id, job_id),
            ).fetchall()
            for job in jobs:
                try:
                    scenes = _load_scenes(job["plan_json"])
                except ValueError as error:
                    db.execute(
                        "UPDATE jobs SET status = 'needs_review', stage_error = ? WHERE id = ?",
                        (str(error), job["id"]),
                    )
                    continue
                if preflight:
                    try:
                        preflight(job["id"], scenes)
                    except ValueError as error:
                        db.execute("UPDATE jobs SET stage_error = ? WHERE id = ?", (str(error), job["id"]))
                        continue
                db.executemany(
                    "INSERT INTO media_tasks (job_id, scene_id, kind) VALUES (?, ?, 'keyframe')",
                    [(job["id"], scene["scene_id"]) for scene in scenes],
                )
                db.execute(
                    "UPDATE jobs SET status = 'making_keyframes', stage_error = NULL WHERE id = ?",
                    (job["id"],),
                )
                queued += 1
        return queued

    def claim_automatic_keyframe_review(self, *, job_id=None):
        with self.store._connection(write=True) as db:
            for job in db.execute(_AUTO_KEYFRAME_JOBS, (job_id, job_id)).fetchall():
                qa = json.loads(job["qa_json"] or "{}")
                if qa.get("auto_retry") is False:
                    continue
                tasks = db.execute("SELECT * FROM media_tasks WHERE job_id = ? ORDER BY id", (job["id"],)).fetchall()
                revision = scene_revision(job, tasks)
                if qa.get("keyframe_review_revision") == revision:
                    continue
                qa["keyframe_review_revision"] = revision
                qa["keyframe_review_running"] = True
                db.execute("UPDATE jobs SET qa_json = ? WHERE id = ?", (json.dumps(qa), job["id"]))
                return job, tasks, revision
        return None

    def hold_automatic_keyframe_review(self, job_id, revision, reason):
        with self.store._connection(write=True) as db:
            job = db.execute("SELECT * FROM jobs WHERE id = ? AND status = 'review_keyframes'", (job_id,)).fetchone()
            if job is None:
                return
            tasks = db.execute("SELECT * FROM media_tasks WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
            if scene_revision(job, tasks) == revision:
                self._finish_keyframe_review(db, job, reason)

    @staticmethod
    def _finish_keyframe_review(db, job, reason):
        qa = json.loads(job["qa_json"] or "{}")
        qa.pop("keyframe_review_running", None)
        db.execute("UPDATE jobs SET qa_json = ?, stage_error = ? WHERE id = ?",
                   (json.dumps(qa), reason, job["id"]))

    def approve_keyframes(self, job_id, *, expected_revision=None, automatic=False):
        with self.store._connection(write=True) as db:
            job = db.execute("SELECT id, status, plan_json, qa_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise LookupError("Video job not found.")
            if job["status"] != "review_keyframes":
                raise ValueError("This job is not waiting for keyframe approval.")
            if automatic and db.execute(_AUTO_KEYFRAME_JOBS, (job_id, job_id)).fetchone() is None:
                raise ValueError("Automatic keyframe approval is off. Review the keyframes manually.")
            tasks = db.execute("SELECT * FROM media_tasks WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
            if expected_revision is not None and expected_revision != scene_revision(job, tasks):
                raise ValueError("The scene plan or keyframes changed. Review them before approving.")

            scenes = _load_scenes(job["plan_json"])
            ready_keyframes(scenes, tasks)

            queue_scene_media(db, job_id, scenes)
            self._finish_keyframe_review(db, job, None)
            db.execute("UPDATE jobs SET status = 'making_media' WHERE id = ?", (job_id,))

    def tasks(self, *, job_id=None):
        with self.store._connection() as db:
            return [
                dict(row) for row in db.execute(
                    """
                    SELECT media_tasks.*, jobs.plan_json, jobs.niche_id, jobs.production_context_json,
                        jobs.status AS job_status
                    FROM media_tasks JOIN jobs ON jobs.id = media_tasks.job_id
                    WHERE (media_tasks.status = 'pending'
                        OR (media_tasks.status = 'queued' AND jobs.status IN (?, ?)))
                        AND (? IS NULL OR media_tasks.job_id = ?)
                    ORDER BY media_tasks.error IS NOT NULL, media_tasks.status = 'queued', media_tasks.id
                    """,
                    (*ACTIVE_JOB_STATUSES, job_id, job_id),
                )
            ]

    def begin(self, task_id, provider_name, reservation_id):
        with self.store._connection(write=True) as db:
            updated = db.execute(
                """
                UPDATE media_tasks
                SET status = 'submitting', attempts = attempts + 1, provider_name = ?,
                    reservation_id = ?, provider_task_id = NULL, error = NULL
                WHERE id = ? AND status = 'queued' AND attempts < ?
                    AND EXISTS (
                        SELECT 1 FROM jobs WHERE jobs.id = media_tasks.job_id AND jobs.status IN (?, ?)
                    )
                    AND EXISTS (
                        SELECT 1 FROM spend_reservations
                        WHERE id = ? AND provider_name = ? AND job_id = media_tasks.job_id
                            AND status = 'reserved'
                    )
                """,
                (
                    provider_name, reservation_id, task_id, MAX_MEDIA_ATTEMPTS, *ACTIVE_JOB_STATUSES,
                    reservation_id, provider_name,
                ),
            )
            return bool(updated.rowcount)

    def pending(self, task_id, provider_task_id):
        if not isinstance(provider_task_id, str) or not provider_task_id.strip():
            raise ValueError("A pending provider task needs its task ID.")
        with self.store._connection(write=True) as db:
            updated = db.execute(
                """
                UPDATE media_tasks SET status = 'pending', provider_task_id = ?, error = NULL
                WHERE id = ? AND status = 'submitting'
                """,
                (provider_task_id, task_id),
            )
            if not updated.rowcount:
                raise ValueError("This task is not submitting.")

    def complete(self, task_id, local_path):
        if not local_path:
            raise ValueError("A completed media task needs a local path.")
        with self.store._connection(write=True) as db:
            task = db.execute(
                "SELECT job_id, kind FROM media_tasks WHERE id = ? AND status IN ('submitting', 'pending')",
                (task_id,),
            ).fetchone()
            if task is None:
                raise ValueError("This task is not in progress.")
            db.execute(
                "UPDATE media_tasks SET status = 'completed', local_path = ?, error = NULL WHERE id = ?",
                (str(local_path), task_id),
            )
            if task["kind"] == "keyframe":
                active, ready = "making_keyframes", "review_keyframes"
            else:
                active, ready = "making_media", "render_ready"
            db.execute(
                """
                UPDATE jobs SET status = ?, stage_error = NULL
                WHERE id = ? AND status = ? AND NOT EXISTS (
                    SELECT 1 FROM media_tasks WHERE job_id = jobs.id AND status <> 'completed'
                )
                """,
                (ready, task["job_id"], active),
            )

    def problem(self, task_id, message, status, *, release_reservation=False):
        if status not in ("queued", "failed", "needs_review"):
            raise ValueError("Unknown media failure status.")
        with self.store._connection(write=True) as db:
            task = db.execute(
                """
                SELECT media_tasks.job_id, media_tasks.attempts, media_tasks.reservation_id,
                    media_tasks.status, jobs.status AS job_status
                FROM media_tasks JOIN jobs ON jobs.id = media_tasks.job_id
                WHERE media_tasks.id = ? AND media_tasks.status IN ('submitting', 'pending')
                """,
                (task_id,),
            ).fetchone()
            if task is None:
                raise ValueError("This task is not in progress.")
            if release_reservation:
                released = db.execute(
                    """UPDATE spend_reservations SET status = 'released'
                        WHERE id = ? AND status = 'reserved' AND ? = 'submitting'""",
                    (task["reservation_id"], task["status"]),
                )
                if not released.rowcount:
                    raise ValueError("This submission has no pending reservation to release.")
            if status == "queued" and (
                task["attempts"] >= MAX_MEDIA_ATTEMPTS or task["job_status"] not in ACTIVE_JOB_STATUSES
            ):
                status = "failed"
            if status == "queued":
                db.execute(
                    """
                    UPDATE media_tasks
                    SET status = 'queued', error = ?, provider_task_id = NULL, reservation_id = NULL
                    WHERE id = ?
                    """,
                    (message, task_id),
                )
            else:
                db.execute("UPDATE media_tasks SET status = ?, error = ? WHERE id = ?", (status, message, task_id))
                db.execute(
                    "UPDATE jobs SET status = ?, stage_error = ? WHERE id = ? AND status IN (?, ?)",
                    (status, message, task["job_id"], *ACTIVE_JOB_STATUSES),
                )
            return status

    def blocked(self, task_id, message):
        with self.store._connection(write=True) as db:
            db.execute(
                "UPDATE media_tasks SET error = ? WHERE id = ? AND status IN ('queued', 'pending')",
                (message, task_id),
            )

    def recover_interrupted(self):
        message = "Provider submission was interrupted. Check its status before starting a new attempt."
        with self.store._connection(write=True) as db:
            keyframe_jobs = db.execute(
                "SELECT id, qa_json FROM jobs WHERE status = 'review_keyframes' AND qa_json IS NOT NULL",
            ).fetchall()
            for job in keyframe_jobs:
                if json.loads(job["qa_json"]).get("keyframe_review_running"):
                    self._finish_keyframe_review(db, job,
                                                 "Studio restarted during automatic keyframe review. Review the keyframes manually.")
            db.execute(
                """
                UPDATE jobs SET status = 'needs_review', stage_error = ?
                WHERE status IN (?, ?) AND EXISTS (
                    SELECT 1 FROM media_tasks WHERE job_id = jobs.id AND status = 'submitting'
                )
                """,
                (message, *ACTIVE_JOB_STATUSES),
            )
            return db.execute(
                "UPDATE media_tasks SET status = 'needs_review', error = ? WHERE status = 'submitting'",
                (message,),
            ).rowcount

    def job_tasks(self, job_id):
        with self.store._connection() as db:
            return [
                dict(row) for row in db.execute(
                    "SELECT * FROM media_tasks WHERE job_id = ? ORDER BY id", (job_id,)
                )
            ]
