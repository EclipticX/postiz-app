"""Durable scene tasks and the keyframe approval gate."""

import hashlib
import json

from app.scenes import spoken_lines


MAX_MEDIA_ATTEMPTS = 3
ACTIVE_JOB_STATUSES = ("making_keyframes", "making_media")
SCENE_EDIT_STATUSES = ("review_keyframes", "preview_ready", "needs_review")


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

    def queue_keyframes(self):
        queued = 0
        with self.store._connection(write=True) as db:
            jobs = db.execute("SELECT id, plan_json FROM jobs WHERE status = 'planned' ORDER BY id").fetchall()
            for job in jobs:
                try:
                    scenes = _load_scenes(job["plan_json"])
                except ValueError as error:
                    db.execute(
                        "UPDATE jobs SET status = 'needs_review', stage_error = ? WHERE id = ?",
                        (str(error), job["id"]),
                    )
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

    def approve_keyframes(self, job_id, *, expected_revision=None):
        with self.store._connection(write=True) as db:
            job = db.execute("SELECT id, status, plan_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise LookupError("Video job not found.")
            if job["status"] != "review_keyframes":
                raise ValueError("This job is not waiting for keyframe approval.")
            tasks = db.execute("SELECT * FROM media_tasks WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
            if expected_revision is not None and expected_revision != scene_revision(job, tasks):
                raise ValueError("The scene plan or keyframes changed. Review them before approving.")

            scenes = _load_scenes(job["plan_json"])
            ready = {
                task["scene_id"] for task in tasks
                if task["kind"] == "keyframe" and task["status"] == "completed" and task["local_path"]
            }
            if ready != {scene["scene_id"] for scene in scenes}:
                raise ValueError("Wait for every keyframe before approving.")

            queue_scene_media(db, job_id, scenes)
            db.execute("UPDATE jobs SET status = 'making_media', stage_error = NULL WHERE id = ?", (job_id,))

    def tasks(self):
        with self.store._connection() as db:
            return [
                dict(row) for row in db.execute(
                    """
                    SELECT media_tasks.*, jobs.plan_json, jobs.niche_id, jobs.production_context_json,
                        jobs.status AS job_status
                    FROM media_tasks JOIN jobs ON jobs.id = media_tasks.job_id
                    WHERE media_tasks.status = 'pending'
                        OR (media_tasks.status = 'queued' AND jobs.status IN (?, ?))
                    ORDER BY media_tasks.error IS NOT NULL, media_tasks.status = 'queued', media_tasks.id
                    """,
                    ACTIVE_JOB_STATUSES,
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

    def problem(self, task_id, message, status):
        if status not in ("queued", "failed", "needs_review"):
            raise ValueError("Unknown media failure status.")
        with self.store._connection(write=True) as db:
            task = db.execute(
                """
                SELECT media_tasks.job_id, media_tasks.attempts, jobs.status AS job_status
                FROM media_tasks JOIN jobs ON jobs.id = media_tasks.job_id
                WHERE media_tasks.id = ? AND media_tasks.status IN ('submitting', 'pending')
                """,
                (task_id,),
            ).fetchone()
            if task is None:
                raise ValueError("This task is not in progress.")
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

    def blocked(self, task_id, message):
        with self.store._connection(write=True) as db:
            db.execute(
                "UPDATE media_tasks SET error = ? WHERE id = ? AND status IN ('queued', 'pending')",
                (message, task_id),
            )

    def recover_interrupted(self):
        message = "Provider submission was interrupted. Check its status before starting a new attempt."
        with self.store._connection(write=True) as db:
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
