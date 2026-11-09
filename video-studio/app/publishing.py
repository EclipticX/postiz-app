"""Send explicitly approved preview files to the local Postiz instance."""

import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.insights import normalize_metrics, update_memory
from app.postiz_client import PostizClient, PostizError
from app.publishing_store import PublishingStore


SCHEDULABLE_CHANNELS = {"youtube"}


class StudioPublishing:
    def __init__(self, store, settings, media_root, postiz_url):
        self.store = store
        self.settings = settings
        self.posts = PublishingStore(store)
        self.media_root = media_root
        self.postiz_url = postiz_url

    def _client(self):
        key = self.settings.key("POSTIZ_API_KEY")
        enabled = any(route["name"] == "postiz" and route["enabled"] for route in self.settings.list_providers())
        if not key or not enabled:
            raise ValueError("Enable Postiz and add its API key in Settings.")
        return PostizClient(self.postiz_url, key)

    def channels(self):
        return self._client().channels()

    def metrics(self, job_id):
        with self.store._connection() as db:
            rows = db.execute(
                """SELECT platform, post_id, metrics_json FROM analytics
                    WHERE id IN (SELECT max(id) FROM analytics WHERE job_id = ? GROUP BY post_id)
                    ORDER BY platform""", (job_id,),
            )
            return [dict(row, metrics=json.loads(row["metrics_json"])) for row in rows]

    def approve(self, job_id, channel_id, title, caption, hashtags, local_time, timezone_name, draft, *,
                expected_preview_path=None):
        channel = next((channel for channel in self.channels() if channel["id"] == channel_id), None)
        if channel is None:
            raise ValueError("Select a connected Postiz channel.")
        if not draft and channel["identifier"] not in SCHEDULABLE_CHANNELS:
            raise ValueError("Create a Postiz draft for this channel. Scheduling is available for YouTube here.")

        scheduled = None
        if not draft:
            try:
                wall_time = datetime.fromisoformat(local_time)
                zone = ZoneInfo(timezone_name)
            except (ValueError, ZoneInfoNotFoundError) as error:
                raise ValueError("Enter a valid publication time and time zone.") from error
            if wall_time.tzinfo is not None:
                raise ValueError("Choose a local date and time.")
            scheduled = wall_time.replace(tzinfo=zone).astimezone(timezone.utc)
            if scheduled <= datetime.now(timezone.utc):
                raise ValueError("Choose a future publication time.")

        tags = [tag.strip().lstrip("#") for tag in hashtags.replace(",", " ").split()]
        return self.posts.queue(
            job_id, channel, title, caption, [tag for tag in tags if tag], scheduled, draft,
            expected_preview_path=expected_preview_path,
        )

    def process(self, now):
        try:
            client = self._client()
        except (ValueError, PostizError):
            return
        request = self.posts.next_request()
        if request:
            self._send(client, request, now)
        for request in self.posts.due_analytics(now, limit=3):
            try:
                metrics = normalize_metrics(client.analytics(request["post_id"]))
                self.posts.record_analytics(request["id"], metrics, now)
                if any(value is not None for value in metrics.values()):
                    update_memory(self.store, request.get("niche_id") or "kids_3d")
            except (PostizError, ValueError):
                logging.warning("Postiz analytics are unavailable for request %s", request["id"])
                self.posts.defer_analytics(request["id"], now)

    def _send(self, client, request, now):
        deadline = None if request["draft"] else datetime.fromisoformat(request["scheduled_at"])
        expired_message = "The approved publication time passed before Postiz handoff."
        if deadline is not None and deadline <= max(now, datetime.now(timezone.utc)):
            self.posts.fail(request["id"], expired_message)
            return
        root = self.media_root.resolve()
        preview = (root / request["preview_path"]).resolve()
        if not preview.is_relative_to(root) or not preview.is_file():
            self.posts.fail(request["id"], "The approved preview file is missing.", False)
            return
        channel = {"id": request["channel_id"], "identifier": request["identifier"], "name": request["channel_name"]}
        try:
            media = client.upload(preview)
            if deadline is not None and deadline <= max(now, datetime.now(timezone.utc)):
                self.posts.fail(request["id"], expired_message)
                return
            post_id = client.create_post(
                channel, media, request["title"], request["caption"], request["hashtags"],
                request["scheduled_at"], draft=bool(request["draft"]),
            )
        except PostizError as error:
            self.posts.fail(request["id"], str(error), error.uncertain)
            return
        self.posts.finish(request["id"], post_id)
