"""Approved ideas and scheduled prompts enter the same scene planner."""

import json
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.providers import ClaudeAdapter, ProviderError, ProviderRequest
from app.scenes import SCENE_SCHEMA, SceneValidationError, idea_prompt, planner_prompt, validate_scene_plan
from app.studio_library import StudioLibrary


IDEA_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ideas"],
    "properties": {
        "ideas": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "prompt"],
                "properties": {"title": {"type": "string"}, "prompt": {"type": "string"}},
            },
        },
    },
}
TOTAL_DURATION = 30
MAX_PLAN_ATTEMPTS = 3
CLAUDE_ROUTE = "claude-sonnet"


class StudioPlanner:
    def __init__(self, store, settings, adapter_factory=ClaudeAdapter):
        self.store = store
        self.settings = settings
        self.adapter_factory = adapter_factory
        self.library = StudioLibrary(store)

    def request_ideas(self, trends, brand_id=None):
        trends = trends.strip()
        if not trends or len(trends) > 4000:
            raise ValueError("Enter trends in 1–4,000 characters.")
        if brand_id is not None:
            brand_id = brand_id.strip() or None
        with self.store._connection(write=True) as db:
            db.execute("INSERT INTO idea_generation_requests (trends, brand_id) VALUES (?, ?)", (trends, brand_id))

    def generate_three_ideas(self, trends, brand_id=None, now=None):
        trends = trends.strip()
        if not trends or len(trends) > 4000:
            raise ValueError("Enter trends in 1–4,000 characters.")
        if brand_id is not None:
            brand_id = brand_id.strip() or None
        now = now or datetime.now(timezone.utc)

        with self.store._connection() as db:
            memory = [json.loads(row[0]) for row in db.execute(
                "SELECT pattern_json FROM memory WHERE niche_id = 'kids_3d' ORDER BY id DESC LIMIT 10"
            )]

        options = {"presets": {"brand": brand_id}} if brand_id else {}
        profile = self.library.profile(options=options)
        prompt = idea_prompt(profile, trends, memory)

        adapter, reservation = self._claude(None, now)
        try:
            result = adapter.submit(ProviderRequest(prompt=prompt, output_schema=IDEA_SCHEMA))
            ideas = self._validated_ideas(result.data)
        except ProviderError as error:
            raise ValueError(str(error)) from None
        finally:
            self.settings.settle(reservation)

        return ideas[:3]

    def idea_requests(self):
        with self.store._connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT status, error FROM idea_generation_requests ORDER BY id DESC LIMIT 3"
            )]

    def idea_schedule(self):
        with self.store._connection() as db:
            schedule = db.execute("SELECT * FROM idea_schedule WHERE id = 1").fetchone()
        return dict(schedule) if schedule else {
            "enabled": False, "cadence": "daily", "run_at": "09:00",
            "timezone": "Asia/Dhaka", "weekday": 0, "trends": "",
        }

    def set_idea_schedule(self, enabled, cadence, run_at, timezone_name, weekday, trends, now):
        if cadence not in ("daily", "weekly") or not 0 <= weekday <= 6:
            raise ValueError("Choose a daily or weekly schedule and a valid weekday.")
        try:
            clock = time.fromisoformat(run_at)
            zone = ZoneInfo(timezone_name)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("Enter a valid time and time zone.") from error
        if clock.tzinfo or clock.isoformat(timespec="minutes") != run_at or len(trends) > 4000:
            raise ValueError("Use HH:MM and at most 4,000 characters of trends.")

        with self.store._connection(write=True) as db:
            db.execute(
                """INSERT INTO idea_schedule VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET enabled=excluded.enabled, cadence=excluded.cadence,
                    run_at=excluded.run_at, timezone=excluded.timezone, weekday=excluded.weekday,
                    trends=excluded.trends, starts_on=excluded.starts_on""",
                (enabled, cadence, run_at, timezone_name, weekday, trends, now.astimezone(zone).date().isoformat()),
            )

    def dispatch_idea_schedule(self, now):
        with self.store._connection(write=True) as db:
            schedule = db.execute("SELECT * FROM idea_schedule WHERE id = 1 AND enabled = 1").fetchone()
            if schedule is None:
                return
            local = now.astimezone(ZoneInfo(schedule["timezone"]))
            if local.date() < date.fromisoformat(schedule["starts_on"]) or local.time() < time.fromisoformat(schedule["run_at"]):
                return
            if schedule["cadence"] == "weekly" and local.weekday() != schedule["weekday"]:
                return
            db.execute(
                "INSERT OR IGNORE INTO idea_generation_requests (trends, schedule_slot) VALUES (?, ?)",
                (schedule["trends"], local.date().isoformat()),
            )

    def recover_interrupted(self):
        reason = "Studio restarted during a provider call. Review its status before resubmitting."
        with self.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET status = 'needs_review', stage_error = ? WHERE status = 'planning'", (reason,))
            db.execute("UPDATE idea_generation_requests SET status = 'needs_review', error = ? WHERE status = 'generating'", (reason,))

    def _claude(self, job_id, now):
        provider = next((row for row in self.settings.list_providers() if row["name"] == CLAUDE_ROUTE), None)
        key = self.settings.key("ANTHROPIC_API_KEY")
        if not key or not provider or not provider["enabled"]:
            raise ValueError("Enable Claude and add an API key in Settings.")
        if not provider["rate_micros"]:
            raise ValueError("Set Claude's estimated cost per request in Settings.")
        try:
            adapter = self.adapter_factory(key)
        except ProviderError as error:
            raise ValueError(str(error)) from None
        reservation = self.settings.reserve(job_id, CLAUDE_ROUTE, provider["rate_micros"], now)
        return adapter, reservation

    def process_ideas(self, now):
        with self.store._connection(write=True) as db:
            request = db.execute(
                "SELECT * FROM idea_generation_requests WHERE status = 'queued' ORDER BY error IS NOT NULL, id LIMIT 1"
            ).fetchone()
            if request is None:
                return
            db.execute("UPDATE idea_generation_requests SET status = 'generating' WHERE id = ?", (request["id"],))
            memory = [json.loads(row[0]) for row in db.execute(
                "SELECT pattern_json FROM memory WHERE niche_id = 'kids_3d' ORDER BY id DESC LIMIT 10"
            )]

        brand_id = request["brand_id"] if "brand_id" in request.keys() else None
        options = {"presets": {"brand": brand_id}} if brand_id else {}
        profile = self.library.profile(options=options)
        prompt = idea_prompt(profile, request["trends"], memory)
        try:
            adapter, reservation = self._claude(None, now)
        except ValueError as error:
            self._update_idea_request(request["id"], "queued", str(error))
            return

        try:
            result = adapter.submit(ProviderRequest(prompt=prompt, output_schema=IDEA_SCHEMA))
            ideas = self._validated_ideas(result.data)
        except ProviderError as error:
            self._update_idea_request(request["id"], "needs_review" if error.uncertain else "failed", str(error))
            return
        except ValueError as error:
            self._update_idea_request(request["id"], "failed", str(error))
            return
        finally:
            self.settings.settle(reservation)

        with self.store._connection(write=True) as db:
            db.executemany(
                "INSERT INTO ideas (niche_id, title, prompt, source, brand_id) VALUES (?, ?, ?, 'claude', ?)",
                [(profile["id"], idea["title"].strip(), idea["prompt"].strip(), brand_id) for idea in ideas],
            )
            db.execute(
                "UPDATE idea_generation_requests SET status = 'completed', error = NULL WHERE id = ?", (request["id"],)
            )

    @staticmethod
    def _validated_ideas(data):
        ideas = data.get("ideas") if isinstance(data, dict) else None
        if not isinstance(ideas, list) or not 1 <= len(ideas) <= 10:
            raise ValueError("Claude must return between one and ten ideas.")
        for idea in ideas:
            if not isinstance(idea, dict) or any(
                not isinstance(idea.get(field), str) or not idea[field].strip()
                for field in ("title", "prompt")
            ):
                raise ValueError("Claude returned an idea without a title or prompt.")
        return ideas

    def _update_idea_request(self, request_id, status, error):
        with self.store._connection(write=True) as db:
            db.execute(
                "UPDATE idea_generation_requests SET status = ?, error = ? WHERE id = ?",
                (status, error, request_id),
            )

    def process_plan(self, now):
        with self.store._connection(write=True) as db:
            job = self._claim_plan(db, now)
        if job is None:
            return

        try:
            profile = (
                json.loads(job["production_context_json"]) if job["production_context_json"] else self.library.profile(
                    job["niche_id"] or "kids_3d", json.loads(job["production_options_json"]),
                )
            )
            feedback = json.loads(job["plan_feedback"] or "[]")
            prompt_text = job["prompt"]
            if job["revision_feedback"]:
                prompt_text += "\nRequested revision:\n" + job["revision_feedback"]
            prompt = planner_prompt(profile, prompt_text, TOTAL_DURATION, feedback)
            adapter, reservation = self._claude(job["id"], now)
        except ValueError as error:
            with self.store._connection(write=True) as db:
                db.execute(
                    """UPDATE jobs SET status = 'queued', stage_error = ?,
                        planning_started_for = CASE WHEN plan_attempts = 0 THEN NULL ELSE planning_started_for END
                        WHERE id = ?""",
                    (str(error), job["id"]),
                )
            return

        attempt = job["plan_attempts"] + 1
        with self.store._connection(write=True) as db:
            db.execute(
                "UPDATE jobs SET plan_attempts = ?, production_context_json = ? WHERE id = ?",
                (attempt, json.dumps(profile), job["id"]),
            )
        try:
            result = adapter.submit(ProviderRequest(prompt=prompt, output_schema=SCENE_SCHEMA))
            scenes = validate_scene_plan(result.data, profile, TOTAL_DURATION)
        except SceneValidationError as error:
            self._update_plan(
                job["id"], "queued" if attempt < MAX_PLAN_ATTEMPTS else "needs_review",
                str(error), feedback=error.errors,
            )
            return
        except ProviderError as error:
            if error.uncertain:
                status = "needs_review"
            elif error.retryable and attempt < MAX_PLAN_ATTEMPTS:
                status = "queued"
            else:
                status = "failed"
            self._update_plan(job["id"], status, str(error), feedback=feedback)
            return
        finally:
            self.settings.settle(reservation)

        self._update_plan(job["id"], "planned", None, scenes=scenes)

    @staticmethod
    def _claim_plan(db, now):
        candidates = db.execute(
            """SELECT jobs.*, prompt_schedules.id AS batch_id, prompt_schedules.status AS batch_status,
                prompt_schedules.timezone AS batch_timezone, prompt_schedules.run_at AS batch_time,
                prompt_schedules.daily_limit AS batch_limit
                FROM jobs LEFT JOIN schedule_entries ON schedule_entries.id = jobs.schedule_entry_id
                LEFT JOIN prompt_schedules ON prompt_schedules.id = schedule_entries.schedule_id
                WHERE jobs.status = 'queued' ORDER BY jobs.stage_error IS NOT NULL, jobs.id"""
        ).fetchall()
        for job in candidates:
            started_for = job["planning_started_for"]
            if job["batch_id"] and started_for is None:
                local = now.astimezone(ZoneInfo(job["batch_timezone"]))
                if job["batch_status"] != "active" or local.time() < time.fromisoformat(job["batch_time"]):
                    continue
                started_for = local.date().isoformat()
                started_today = db.execute(
                    """SELECT COUNT(*) FROM jobs JOIN schedule_entries ON schedule_entries.id = jobs.schedule_entry_id
                        WHERE schedule_entries.schedule_id = ? AND jobs.planning_started_for = ?""",
                    (job["batch_id"], started_for),
                ).fetchone()[0]
                if started_today >= job["batch_limit"]:
                    continue
            db.execute(
                "UPDATE jobs SET status = 'planning', planning_started_for = ? WHERE id = ?",
                (started_for, job["id"]),
            )
            return job
        return None

    def _update_plan(self, job_id, status, error, *, feedback=(), scenes=None):
        with self.store._connection(write=True) as db:
            db.execute(
                "UPDATE jobs SET status = ?, stage_error = ?, plan_feedback = ?, plan_json = ? WHERE id = ?",
                (status, error, json.dumps(feedback), json.dumps(scenes) if scenes is not None else None, job_id),
            )
