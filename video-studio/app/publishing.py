"""Send explicitly approved preview files to the local Postiz instance."""

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.insights import normalize_metrics, update_memory
from app.postiz_client import META_CHANNELS, PostizClient, PostizError
from app.publishing_store import POSTIZ_HANDOFF_EXPIRED, PublishingStore
from app.settings import provider_blockers


SCHEDULABLE_CHANNELS = {"youtube", *META_CHANNELS}
TIKTOK_SCHEDULE_NOTE = "TikTok requires an approved Direct Post integration and account-specific approval. Create a draft in Postiz."


def _publication_time(local_time, zone, now):
    try:
        wall_time = datetime.fromisoformat(local_time)
    except (TypeError, ValueError):
        raise ValueError("Enter a valid publication time and time zone.") from None
    if wall_time.tzinfo is not None:
        raise ValueError("Choose a local date and time.")
    scheduled = wall_time.replace(tzinfo=zone).astimezone(timezone.utc)
    if scheduled <= now:
        raise ValueError("Choose a future publication time.")
    return scheduled


class StudioPublishing:
    def __init__(self, store, settings, media_root, postiz_url):
        self.store = store
        self.settings = settings
        self.posts = PublishingStore(store)
        self.media_root = media_root
        self.postiz_url = postiz_url

    def _client(self):
        route = next((route for route in self.settings.list_providers() if route["name"] == "postiz"), None)
        key = self.settings.key(route["key_env"]) if route and not provider_blockers(route) else None
        if not key:
            raise ValueError("Enable Postiz and add its API key in Settings.")
        return PostizClient(self.postiz_url, key)

    def channels(self):
        channels = self._client().channels()
        return [
            {
                **channel,
                "can_schedule": channel["identifier"] in SCHEDULABLE_CHANNELS,
                "schedule_note": (
                    TIKTOK_SCHEDULE_NOTE if channel["identifier"] == "tiktok"
                    else "Requires an HTTPS video URL that Meta can access." if channel["identifier"] in META_CHANNELS
                    else ""
                ),
            }
            for channel in channels
        ]

    def original_post_url(self, post):
        if post["status"] != "published" or post["draft"]:
            return None
        return post.get("release_url") or self._client().original_post_url(post["post_id"], post["scheduled_at"])

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
        request_id, = self.approve_channels(
            job_id, [channel_id], title, caption, hashtags, local_time, timezone_name, draft,
            expected_preview_path=expected_preview_path,
        )
        return request_id

    def approve_channels(self, job_id, channel_ids, title, caption, hashtags, local_time, timezone_name, draft, *,
                         expected_preview_path=None, channel_times=None):
        if (
            not isinstance(channel_ids, (list, tuple)) or not channel_ids
            or any(not isinstance(channel_id, str) or not channel_id for channel_id in channel_ids)
            or len(set(channel_ids)) != len(channel_ids)
        ):
            raise ValueError("Select one or more distinct connected Postiz channels.")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100:
            raise ValueError("Enter a post title between 1 and 100 characters.")
        connected = {channel["id"]: channel for channel in self.channels()}
        if any(channel_id not in connected for channel_id in channel_ids):
            raise ValueError("Select connected Postiz channels.")
        channels = [connected[channel_id] for channel_id in channel_ids]
        if any(channel["identifier"] == "youtube" for channel in channels) and len(title.strip()) < 2:
            raise ValueError("YouTube requires a title between 2 and 100 characters.")
        channel_times = channel_times or {}
        deliveries = [(channel, None) for channel in channels]
        if not draft:
            if any(channel["identifier"] not in SCHEDULABLE_CHANNELS for channel in channels):
                raise ValueError(TIKTOK_SCHEDULE_NOTE)
            try:
                zone = ZoneInfo(timezone_name)
            except (TypeError, ValueError, ZoneInfoNotFoundError):
                raise ValueError("Enter a valid publication time and time zone.") from None
            now = datetime.now(timezone.utc)
            deliveries = [
                (channel, _publication_time(channel_times.get(channel["id"]) or local_time, zone, now))
                for channel in channels
            ]

        tags = [tag.strip().lstrip("#") for tag in hashtags.replace(",", " ").split()]
        return self.posts.queue_many(
            job_id, deliveries, title, caption, [tag for tag in tags if tag], draft,
            expected_preview_path=expected_preview_path,
        )

    def process(self, now):
        self.posts.expire_queued_handoffs(now)
        try:
            client = self._client()
        except (ValueError, PostizError):
            return
        request = self.posts.next_request()
        if request:
            self._send(client, request, now)
        for request in self.posts.due_postiz_status(now):
            try:
                state, url = client.post_status(request["post_id"], request["scheduled_at"])
            except PostizError:
                state, url = "UNAVAILABLE", None
            self.posts.record_postiz_state(request["id"], state, url, now)
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
        if deadline is not None and deadline <= max(now, datetime.now(timezone.utc)):
            self.posts.fail(request["id"], POSTIZ_HANDOFF_EXPIRED)
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
                self.posts.fail(request["id"], POSTIZ_HANDOFF_EXPIRED)
                return
            post_id = client.create_post(
                channel, media, request["title"], request["caption"], request["hashtags"],
                request["scheduled_at"], draft=bool(request["draft"]),
            )
        except PostizError as error:
            self.posts.fail(request["id"], str(error), error.uncertain)
            return
        self.posts.finish(request["id"], post_id)
