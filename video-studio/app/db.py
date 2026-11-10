"""SQLite storage for the Studio inbox and prompt schedule."""

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA = """
CREATE TABLE IF NOT EXISTS niches (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS assets (
    id TEXT NOT NULL,
    niche_id TEXT NOT NULL REFERENCES niches(id),
    type TEXT NOT NULL CHECK (type IN ('character', 'place', 'item')),
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    reference_images_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (niche_id, id)
);

CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY,
    niche_id TEXT REFERENCES niches(id),
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected', 'later')),
    source TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS prompt_schedules (
    id INTEGER PRIMARY KEY,
    niche_id TEXT REFERENCES niches(id),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'paused')),
    start_date TEXT NOT NULL,
    run_at TEXT NOT NULL,
    timezone TEXT NOT NULL,
    daily_limit INTEGER NOT NULL CHECK (daily_limit > 0)
);

CREATE TABLE IF NOT EXISTS schedule_entries (
    id INTEGER PRIMARY KEY,
    schedule_id INTEGER NOT NULL REFERENCES prompt_schedules(id),
    prompt TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS schedule_entries_schedule ON schedule_entries(schedule_id);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    niche_id TEXT REFERENCES niches(id),
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    idea_id INTEGER UNIQUE REFERENCES ideas(id),
    schedule_entry_id INTEGER UNIQUE REFERENCES schedule_entries(id),
    scheduled_for TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK ((idea_id IS NULL) <> (schedule_entry_id IS NULL)),
    CHECK ((schedule_entry_id IS NULL) = (scheduled_for IS NULL))
);

CREATE TABLE IF NOT EXISTS providers (
    name TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    key_env TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    priority INTEGER NOT NULL DEFAULT 100,
    billing_unit TEXT NOT NULL,
    rate_micros INTEGER NOT NULL CHECK (rate_micros >= 0),
    capabilities_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY,
    niche_id TEXT NOT NULL REFERENCES niches(id),
    pattern_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS analytics (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    platform TEXT NOT NULL,
    post_id TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    collected_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

PRAGMA user_version = 1;
"""


SETTINGS_AND_PLANNING_SCHEMA = """
ALTER TABLE providers ADD COLUMN auto_enable_pending INTEGER NOT NULL DEFAULT 1
    CHECK (auto_enable_pending IN (0, 1));

CREATE TABLE budget_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    daily_micros INTEGER NOT NULL CHECK (daily_micros >= 0),
    video_micros INTEGER NOT NULL CHECK (video_micros >= 0)
);

INSERT INTO budget_settings (id, daily_micros, video_micros) VALUES (1, 0, 0);

CREATE TABLE spend_reservations (
    id INTEGER PRIMARY KEY,
    job_id INTEGER REFERENCES jobs(id),
    provider_name TEXT NOT NULL REFERENCES providers(name),
    budget_day TEXT NOT NULL,
    amount_micros INTEGER NOT NULL CHECK (amount_micros >= 0),
    status TEXT NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved', 'settled', 'released'))
);

CREATE INDEX spend_reservations_day ON spend_reservations (budget_day);
CREATE INDEX spend_reservations_job ON spend_reservations (job_id);

ALTER TABLE jobs ADD COLUMN plan_json TEXT;
ALTER TABLE jobs ADD COLUMN stage_error TEXT;
ALTER TABLE jobs ADD COLUMN plan_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN plan_feedback TEXT;

CREATE TABLE idea_generation_requests (
    id INTEGER PRIMARY KEY,
    trends TEXT NOT NULL,
    schedule_slot TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT
);

CREATE TABLE idea_schedule (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL,
    cadence TEXT NOT NULL,
    run_at TEXT NOT NULL,
    timezone TEXT NOT NULL,
    weekday INTEGER NOT NULL,
    trends TEXT NOT NULL,
    starts_on TEXT NOT NULL
);

PRAGMA user_version = 2;
"""


MEDIA_SCHEMA = """
ALTER TABLE jobs ADD COLUMN preview_path TEXT;
ALTER TABLE jobs ADD COLUMN qa_json TEXT;
ALTER TABLE jobs ADD COLUMN revision_feedback TEXT;

CREATE TABLE media_tasks (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    scene_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('keyframe', 'video', 'voice')),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'submitting', 'pending', 'completed', 'failed', 'needs_review')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
    provider_name TEXT REFERENCES providers(name),
    provider_task_id TEXT,
    reservation_id INTEGER REFERENCES spend_reservations(id),
    local_path TEXT,
    error TEXT,
    UNIQUE (job_id, scene_id, kind)
);

CREATE INDEX media_tasks_status ON media_tasks (status);

PRAGMA user_version = 3;
"""


PUBLISHING_SCHEMA = """
ALTER TABLE jobs ADD COLUMN planning_started_for TEXT;

CREATE TABLE publishing_requests (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    channel_id TEXT NOT NULL,
    identifier TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    preview_path TEXT NOT NULL,
    title TEXT NOT NULL,
    caption TEXT NOT NULL,
    hashtags_json TEXT NOT NULL,
    scheduled_at TEXT,
    draft INTEGER NOT NULL DEFAULT 0 CHECK (draft IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'sending', 'scheduled', 'published', 'draft', 'failed', 'needs_review')),
    post_id TEXT,
    error TEXT,
    analytics_after_at TEXT,
    analytics_error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (draft = 1 OR scheduled_at IS NOT NULL),
    UNIQUE (job_id, channel_id)
);

CREATE INDEX publishing_requests_status ON publishing_requests (status);
CREATE INDEX analytics_job_post ON analytics (job_id, platform, post_id, collected_at);

PRAGMA user_version = 4;
"""


LIBRARY_SCHEMA = """
ALTER TABLE assets ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE jobs ADD COLUMN production_options_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE jobs ADD COLUMN production_context_json TEXT;

CREATE TABLE production_presets (
    niche_id TEXT NOT NULL REFERENCES niches(id),
    kind TEXT NOT NULL CHECK (kind IN ('visual', 'script', 'subtitle', 'audio')),
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    config_json TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    PRIMARY KEY (niche_id, kind, id)
);
CREATE UNIQUE INDEX production_presets_default
    ON production_presets (niche_id, kind) WHERE is_default = 1;

ALTER TABLE media_tasks RENAME TO media_tasks_v4;
CREATE TABLE media_tasks (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    scene_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('keyframe', 'video', 'voice')),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'submitting', 'pending', 'completed', 'failed', 'needs_review')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
    provider_name TEXT REFERENCES providers(name),
    provider_task_id TEXT,
    reservation_id INTEGER REFERENCES spend_reservations(id),
    local_path TEXT,
    error TEXT,
    line_index INTEGER NOT NULL DEFAULT 0 CHECK (line_index >= 0 AND (kind = 'voice' OR line_index = 0)),
    UNIQUE (job_id, scene_id, kind, line_index)
);
INSERT INTO media_tasks (id, job_id, scene_id, kind, status, attempts, provider_name, provider_task_id,
    reservation_id, local_path, error)
SELECT id, job_id, scene_id, kind, status, attempts, provider_name, provider_task_id,
    reservation_id, local_path, error FROM media_tasks_v4;
DROP TABLE media_tasks_v4;
CREATE INDEX media_tasks_status ON media_tasks (status);

PRAGMA user_version = 5;
"""


INBOX_SCHEMA = """
ALTER TABLE ideas ADD COLUMN deleted_at TEXT
    CHECK (deleted_at IS NULL OR status <> 'accepted');

ALTER TABLE prompt_schedules ADD COLUMN title TEXT;
ALTER TABLE prompt_schedules ADD COLUMN created_at TEXT;
ALTER TABLE prompt_schedules ADD COLUMN rejected_at TEXT
    CHECK (rejected_at IS NULL OR status = 'draft');
ALTER TABLE prompt_schedules ADD COLUMN deleted_at TEXT
    CHECK (deleted_at IS NULL OR status = 'draft');

PRAGMA user_version = 6;
"""


IDEA_RECORDS = """
SELECT ideas.*, jobs.id AS job_id, (
    SELECT MIN(scheduled_at) FROM publishing_requests
    WHERE job_id = jobs.id AND draft = 0 AND status IN ('scheduled', 'published')
) AS publish_at
FROM ideas LEFT JOIN jobs ON jobs.idea_id = ideas.id
"""


SCHEDULE_RECORDS = """
SELECT prompt_schedules.*, (
    SELECT MIN(publishing_requests.scheduled_at)
    FROM schedule_entries
    JOIN jobs ON jobs.schedule_entry_id = schedule_entries.id
    JOIN publishing_requests ON publishing_requests.job_id = jobs.id
    WHERE schedule_entries.schedule_id = prompt_schedules.id
        AND publishing_requests.draft = 0
        AND publishing_requests.status IN ('scheduled', 'published')
) AS publish_at
FROM prompt_schedules
"""


def _apply_schema(db, script):
    # executescript commits any open transaction before running the script.
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("Incomplete Studio schema migration.")


def _prompt_title(prompt):
    return prompt.splitlines()[0][:100]


def _clean_prompt(prompt):
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Enter a prompt.")
    return prompt.strip()


class StudioStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def _connection(self, write=False):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if not 0 <= version <= 6:
                raise RuntimeError(f"Unsupported Studio database version {version}.")
            if version < 1:
                _apply_schema(db, SCHEMA)
            if version < 2:
                _apply_schema(db, SETTINGS_AND_PLANNING_SCHEMA)
            if version < 3:
                _apply_schema(db, MEDIA_SCHEMA)
            if version < 4:
                _apply_schema(db, PUBLISHING_SCHEMA)
            if version < 5:
                _apply_schema(db, LIBRARY_SCHEMA)
            if version < 6:
                _apply_schema(db, INBOX_SCHEMA)

    def seed_niche(self, profile):
        with self._connection(write=True) as db:
            db.execute(
                """
                INSERT INTO niches (id, name, config_json) VALUES (?, ?, ?)
                ON CONFLICT (id) DO NOTHING
                """,
                (profile["id"], profile["name"], json.dumps(profile)),
            )
            db.executemany(
                """
                INSERT INTO assets
                    (id, niche_id, type, name, description, reference_images_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (niche_id, id) DO NOTHING
                """,
                [
                    (
                        asset["id"],
                        profile["id"],
                        asset["kind"],
                        asset.get("name", asset["id"]),
                        asset["description"],
                        json.dumps([asset["reference_image"]]),
                    )
                    for asset in profile["assets"]
                ],
            )

    def list_ideas(self, *, include_deleted=False):
        with self._connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    IDEA_RECORDS + "WHERE (? OR ideas.deleted_at IS NULL) ORDER BY ideas.id DESC",
                    (include_deleted,),
                )
            ]

    def get_idea(self, idea_id):
        with self._connection() as db:
            row = db.execute(IDEA_RECORDS + "WHERE ideas.id = ?", (idea_id,)).fetchone()
            return dict(row) if row is not None else None

    def add_idea(self, prompt, title=None, source="manual"):
        prompt = _clean_prompt(prompt)
        title = (title or "").strip() or _prompt_title(prompt)
        with self._connection(write=True) as db:
            return db.execute(
                """
                INSERT INTO ideas (niche_id, title, prompt, source)
                VALUES ((SELECT id FROM niches WHERE id = 'kids_3d'), ?, ?, ?)
                """,
                (title, prompt, source),
            ).lastrowid

    def decide_idea(self, idea_id, decision):
        statuses = {"accept": "accepted", "reject": "rejected", "later": "later"}
        if decision not in statuses:
            raise ValueError("Choose Accept, Reject, or Later.")

        status = statuses[decision]
        with self._connection(write=True) as db:
            idea = db.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
            if idea is None:
                raise LookupError("Idea not found.")
            if idea["deleted_at"]:
                raise ValueError("Restore this idea before reviewing it.")
            if idea["status"] == status:
                return
            if idea["status"] in ("accepted", "rejected"):
                raise ValueError("This idea has already been accepted or rejected.")

            db.execute("UPDATE ideas SET status = ? WHERE id = ?", (status, idea_id))
            if status == "accepted":
                db.execute(
                    """
                    INSERT INTO jobs (niche_id, title, prompt, source, idea_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (idea["niche_id"], idea["title"], idea["prompt"], idea["source"], idea_id),
                )

    def list_jobs(self):
        with self._connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY id DESC")]

    def get_job(self, job_id):
        with self._connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row is not None else None

    def create_schedule(self, prompts, start_date, run_at, timezone, daily_limit, *, title=None):
        if not isinstance(prompts, (list, tuple)) or not prompts:
            raise ValueError("Enter at least one prompt.")
        if len(prompts) > 100:
            raise ValueError("Enter at most 100 prompts per schedule.")
        prompts = [_clean_prompt(prompt) for prompt in prompts]
        if type(daily_limit) is not int or not 1 <= daily_limit <= len(prompts):
            raise ValueError("Videos per day must be between 1 and the number of prompts.")
        if title is not None:
            if not isinstance(title, str) or len(title.strip()) > 100:
                raise ValueError("Enter a schedule title of up to 100 characters.")
            title = title.strip() or None

        try:
            starts = date.fromisoformat(start_date)
            start_time = time.fromisoformat(run_at)
        except (TypeError, ValueError) as error:
            raise ValueError("Enter a valid start date and time.") from error
        if (
            starts.isoformat() != start_date
            or start_time.tzinfo is not None
            or start_time.isoformat(timespec="minutes") != run_at
        ):
            raise ValueError("Use YYYY-MM-DD for the date and HH:MM for the time.")
        try:
            ZoneInfo(timezone)
        except (TypeError, ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("Enter a valid time zone, such as Asia/Dhaka.") from error

        with self._connection(write=True) as db:
            schedule_id = db.execute(
                """
                INSERT INTO prompt_schedules (niche_id, start_date, run_at, timezone, daily_limit, title, created_at)
                VALUES ((SELECT id FROM niches WHERE id = 'kids_3d'), ?, ?, ?, ?, ?,
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (start_date, run_at, timezone, daily_limit, title),
            ).lastrowid
            db.executemany(
                "INSERT INTO schedule_entries (schedule_id, prompt) VALUES (?, ?)",
                [(schedule_id, prompt) for prompt in prompts],
            )
            return schedule_id

    def list_schedules(self, *, include_deleted=False):
        return self._read_schedules(
            SCHEDULE_RECORDS + "WHERE (? OR prompt_schedules.deleted_at IS NULL) ORDER BY prompt_schedules.id DESC",
            (include_deleted,),
        )

    def get_schedule(self, schedule_id):
        schedules = self._read_schedules(SCHEDULE_RECORDS + "WHERE prompt_schedules.id = ?", (schedule_id,))
        return schedules[0] if schedules else None

    def _read_schedules(self, query, parameters):
        with self._connection() as db:
            db.execute("BEGIN")
            schedules = [dict(row) for row in db.execute(query, parameters)]
            for schedule in schedules:
                schedule["entries"] = [
                    dict(row)
                    for row in db.execute(
                        """
                        SELECT schedule_entries.id, schedule_entries.prompt, jobs.id AS job_id,
                            COALESCE(jobs.status, 'pending') AS status, jobs.scheduled_for AS queued_on
                        FROM schedule_entries
                        LEFT JOIN jobs ON jobs.schedule_entry_id = schedule_entries.id
                        WHERE schedule_entries.schedule_id = ?
                        ORDER BY schedule_entries.id
                        """,
                        (schedule["id"],),
                    )
                ]
                progress = dict.fromkeys(("pending", "queued", "waiting", "completed", "failed"), 0)
                for entry in schedule["entries"]:
                    status = entry["status"]
                    progress[status if status in progress else "waiting"] += 1
                schedule["progress"] = progress
            return schedules

    def set_schedule_status(self, schedule_id, status):
        if status not in ("active", "paused", "rejected"):
            raise ValueError("Choose Activate, Pause, Resume, or Reject.")
        with self._connection(write=True) as db:
            schedule = db.execute(
                "SELECT status, rejected_at, deleted_at FROM prompt_schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
            if schedule is None:
                raise LookupError("Prompt schedule not found.")
            if schedule["deleted_at"]:
                raise ValueError("Restore this schedule before changing its status.")
            if schedule["rejected_at"]:
                if status == "rejected":
                    return
                raise ValueError("This schedule has already been rejected.")
            if status == "rejected":
                if schedule["status"] != "draft":
                    raise ValueError("Only a draft schedule can be rejected.")
                db.execute(
                    "UPDATE prompt_schedules SET rejected_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                    (schedule_id,),
                )
                return
            if schedule["status"] == "draft" and status == "paused":
                raise ValueError("Activate this schedule before pausing it.")
            db.execute("UPDATE prompt_schedules SET status = ? WHERE id = ?", (status, schedule_id))

    def set_inbox_deleted(self, kind, record_id, deleted):
        tables = {"idea": ("ideas", ("accepted",)), "schedule": ("prompt_schedules", ("active", "paused"))}
        if kind not in tables or type(deleted) is not bool:
            raise ValueError("Choose a valid record and a delete or restore action.")
        table, approved_states = tables[kind]
        with self._connection(write=True) as db:
            record = db.execute(f"SELECT status, deleted_at FROM {table} WHERE id = ?", (record_id,)).fetchone()
            if record is None:
                raise LookupError("Inbox record not found.")
            if (record["deleted_at"] is not None) == deleted:
                return
            if deleted and record["status"] in approved_states:
                raise ValueError("Approved records cannot be deleted.")
            db.execute(
                f"""
                UPDATE {table}
                SET deleted_at = CASE WHEN ? THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now') END
                WHERE id = ?
                """,
                (deleted, record_id),
            )

    def dispatch_due(self, now):
        """Queue today's due prompts once. A missed day never increases today's quota."""
        if now.utcoffset() is None:
            raise ValueError("Use a timezone-aware scheduler time.")
        dispatched = 0
        with self._connection(write=True) as db:
            schedules = db.execute(
                """
                SELECT * FROM prompt_schedules
                WHERE status = 'active' AND rejected_at IS NULL AND deleted_at IS NULL
                """
            ).fetchall()
            for schedule in schedules:
                local_now = now.astimezone(ZoneInfo(schedule["timezone"]))
                today = local_now.date().isoformat()
                if today < schedule["start_date"] or local_now.time() < time.fromisoformat(schedule["run_at"]):
                    continue

                queued_today = db.execute(
                    """
                    SELECT COUNT(*) FROM jobs
                    JOIN schedule_entries ON schedule_entries.id = jobs.schedule_entry_id
                    WHERE schedule_entries.schedule_id = ? AND jobs.scheduled_for = ?
                    """,
                    (schedule["id"], today),
                ).fetchone()[0]
                available = schedule["daily_limit"] - queued_today
                if available <= 0:
                    continue

                entries = db.execute(
                    """
                    SELECT schedule_entries.id, schedule_entries.prompt
                    FROM schedule_entries
                    LEFT JOIN jobs ON jobs.schedule_entry_id = schedule_entries.id
                    WHERE schedule_entries.schedule_id = ? AND jobs.id IS NULL
                    ORDER BY schedule_entries.id LIMIT ?
                    """,
                    (schedule["id"], available),
                ).fetchall()
                db.executemany(
                    """
                    INSERT INTO jobs
                        (niche_id, title, prompt, source, schedule_entry_id, scheduled_for)
                    VALUES (?, ?, ?, 'schedule', ?, ?)
                    """,
                    [
                        (
                            schedule["niche_id"], _prompt_title(entry["prompt"]),
                            entry["prompt"], entry["id"], today,
                        )
                        for entry in entries
                    ],
                )
                dispatched += len(entries)
        return dispatched
