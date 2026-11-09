"""Final-approval requests, per-channel delivery state, and analytics snapshots."""

import json
from datetime import datetime, timedelta, timezone


PUBLISHABLE = ("preview_ready", "needs_review")
CHANNEL_IDENTIFIERS = {
    "youtube", "facebook", "instagram", "instagram-standalone", "tiktok", "tiktok-business",
}


def _utc_time(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Enter a valid date and time with a timezone.") from error
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Enter a valid date and time with a timezone.")
    return value.astimezone(timezone.utc)


def _timestamp(value):
    return _utc_time(value).isoformat(timespec="microseconds")


def _post_record(row):
    post = dict(row)
    post["hashtags"] = json.loads(post.pop("hashtags_json"))
    post["draft"] = bool(post["draft"])
    if "metrics_json" in post:
        metrics = post.pop("metrics_json")
        post["metrics"] = json.loads(metrics) if metrics is not None else None
    return post


class PublishingStore:
    def __init__(self, store):
        self.store = store

    def queue(self, job_id, channel, title, caption, hashtags, scheduled_at, draft=False, *,
              expected_preview_path=None):
        if not isinstance(channel, dict):
            raise ValueError("Choose a connected Postiz channel.")
        channel_id, identifier = channel.get("id"), channel.get("identifier")
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise ValueError("Choose a connected Postiz channel.")
        if not isinstance(identifier, str) or identifier not in CHANNEL_IDENTIFIERS:
            raise ValueError("This channel type is unsupported for Studio publishing.")
        channel_name = channel.get("name") or channel_id
        if not isinstance(channel_name, str) or not isinstance(title, str) or not isinstance(caption, str):
            raise ValueError("Enter a valid channel name, title, and caption.")
        if isinstance(hashtags, str):
            hashtags = hashtags.replace(",", " ").split()
        if not isinstance(hashtags, (list, tuple)) or any(not isinstance(tag, str) for tag in hashtags):
            raise ValueError("Enter hashtags separated by spaces or commas.")
        hashtags_json = json.dumps([tag.strip() for tag in hashtags if tag.strip()])
        if type(draft) is not bool:
            raise ValueError("Choose draft or scheduled publishing.")
        scheduled_at = _timestamp(scheduled_at) if scheduled_at or not draft else None
        title, caption = title.strip(), caption.strip()

        with self.store._connection(write=True) as db:
            job = db.execute("SELECT status, preview_path FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise LookupError("Video job not found.")
            preview_path = job["preview_path"]
            if expected_preview_path is not None and (
                not expected_preview_path or expected_preview_path != preview_path
            ):
                raise ValueError("The video preview changed. Reload the workspace and review it before approving.")
            existing = db.execute(
                "SELECT * FROM publishing_requests WHERE job_id = ? AND channel_id = ?",
                (job_id, channel_id),
            ).fetchone()
            if existing is not None and (existing["status"] != "failed" or existing["post_id"] is not None):
                if (
                    existing["title"] != title or existing["caption"] != caption
                    or existing["hashtags_json"] != hashtags_json
                    or existing["scheduled_at"] != scheduled_at or bool(existing["draft"]) != draft
                ):
                    raise ValueError("This video already has a publishing request for this channel.")
                return existing["id"]
            if job["status"] not in PUBLISHABLE or not preview_path:
                raise ValueError("Wait for the final video preview before approving publishing.")
            if existing is not None:
                db.execute(
                    """
                    UPDATE publishing_requests
                    SET identifier = ?, channel_name = ?, preview_path = ?, title = ?, caption = ?,
                        hashtags_json = ?, scheduled_at = ?, draft = ?, status = 'queued', error = NULL
                    WHERE id = ?
                    """,
                    (
                        identifier, channel_name, preview_path, title, caption, hashtags_json,
                        scheduled_at, draft, existing["id"],
                    ),
                )
                return existing["id"]
            return db.execute(
                """
                INSERT INTO publishing_requests (
                    job_id, channel_id, identifier, channel_name, preview_path, title,
                    caption, hashtags_json, scheduled_at, draft
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, channel_id, identifier, channel_name, preview_path, title,
                    caption, hashtags_json, scheduled_at, draft,
                ),
            ).lastrowid

    def next_request(self):
        """Claim one approved request and prevent delivery of a stale preview."""
        with self.store._connection(write=True) as db:
            db.execute(
                """
                UPDATE publishing_requests SET status = 'failed', error = ?
                WHERE status = 'queued' AND NOT EXISTS (
                    SELECT 1 FROM jobs WHERE jobs.id = publishing_requests.job_id
                    AND jobs.status IN (?, ?) AND jobs.preview_path = publishing_requests.preview_path
                )
                """,
                ("The approved preview changed before publishing.", *PUBLISHABLE),
            )
            request = db.execute(
                "SELECT * FROM publishing_requests WHERE status = 'queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if request is None:
                return None
            db.execute("UPDATE publishing_requests SET status = 'sending' WHERE id = ?", (request["id"],))
            return dict(_post_record(request), status="sending")

    def finish(self, request_id, post_id):
        if not isinstance(post_id, str) or not post_id.strip():
            raise ValueError("The provider response needs a post ID.")
        with self.store._connection(write=True) as db:
            updated = db.execute(
                """
                UPDATE publishing_requests
                SET post_id = ?, status = CASE WHEN draft = 1 THEN 'draft' ELSE 'scheduled' END,
                    analytics_after_at = CASE WHEN draft = 0 THEN scheduled_at END, error = NULL
                WHERE id = ? AND status = 'sending'
                """,
                (post_id, request_id),
            )
            if not updated.rowcount:
                raise ValueError("This publishing request is not being sent.")

    def fail(self, request_id, message, uncertain=False):
        with self.store._connection(write=True) as db:
            updated = db.execute(
                "UPDATE publishing_requests SET status = ?, error = ? WHERE id = ? AND status = 'sending'",
                ("needs_review" if uncertain else "failed", message, request_id),
            )
            if not updated.rowcount:
                raise ValueError("This publishing request is not being sent.")

    def recover_interrupted(self):
        with self.store._connection(write=True) as db:
            return db.execute(
                "UPDATE publishing_requests SET status = 'needs_review', error = ? WHERE status = 'sending'",
                ("Publishing was interrupted. Check Postiz for the post before trying again.",),
            ).rowcount

    def job_posts(self, job_id):
        with self.store._connection() as db:
            return [
                _post_record(row)
                for row in db.execute(
                    """
                    SELECT publishing_requests.*, analytics.metrics_json, analytics.collected_at
                    FROM publishing_requests
                    LEFT JOIN analytics ON analytics.id = (
                        SELECT id FROM analytics
                        WHERE job_id = publishing_requests.job_id
                            AND platform = publishing_requests.identifier AND post_id = publishing_requests.post_id
                        ORDER BY collected_at DESC, id DESC LIMIT 1
                    )
                    WHERE publishing_requests.job_id = ?
                    ORDER BY publishing_requests.id
                    """,
                    (job_id,),
                )
            ]

    def due_analytics(self, now, limit=10):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Analytics batch limit must be between 1 and 100.")
        with self.store._connection() as db:
            return [
                _post_record(row)
                for row in db.execute(
                    """
                    SELECT publishing_requests.*, jobs.niche_id
                    FROM publishing_requests JOIN jobs ON jobs.id = publishing_requests.job_id
                    WHERE publishing_requests.status IN ('scheduled', 'published')
                        AND post_id IS NOT NULL AND analytics_after_at <= ?
                    ORDER BY analytics_after_at, publishing_requests.id LIMIT ?
                    """,
                    (_timestamp(now), limit),
                )
            ]

    def record_analytics(self, request_id, metrics, now):
        if not isinstance(metrics, dict):
            raise ValueError("Analytics metrics must be an object.")
        metrics_json = json.dumps(metrics, allow_nan=False)
        observed = _utc_time(now)
        with self.store._connection(write=True) as db:
            request = db.execute(
                """
                SELECT job_id, identifier, post_id FROM publishing_requests
                WHERE id = ? AND status IN ('scheduled', 'published') AND post_id IS NOT NULL
                """,
                (request_id,),
            ).fetchone()
            if request is None:
                raise LookupError("Scheduled Postiz post not found.")
            db.execute(
                """
                INSERT INTO analytics (job_id, platform, post_id, metrics_json, collected_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    request["job_id"], request["identifier"], request["post_id"],
                    metrics_json, _timestamp(observed),
                ),
            )
            db.execute(
                """
                UPDATE publishing_requests SET analytics_after_at = ?, analytics_error = NULL,
                    status = CASE WHEN ? THEN 'published' ELSE status END
                WHERE id = ?
                """,
                (_timestamp(observed + timedelta(days=1)), any(value is not None for value in metrics.values()), request_id),
            )

    def defer_analytics(self, request_id, now, message="Postiz analytics are temporarily unavailable."):
        with self.store._connection(write=True) as db:
            db.execute(
                """
                UPDATE publishing_requests SET analytics_after_at = ?, analytics_error = ?
                WHERE id = ? AND status IN ('scheduled', 'published')
                """,
                (_timestamp(_utc_time(now) + timedelta(hours=1)), message, request_id),
            )
