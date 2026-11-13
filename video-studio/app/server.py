"""Local HTTP entry point and clock for Video Studio."""

import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from app.asset_view import asset_page, assets_page
from app.brand_view import brand_rules_page
from app.character_view import character_page, characters_page
from app.db import StudioStore
from app.inbox_view import create_page, edit_idea_page, edit_schedule_page, inbox_page, record_page
from app.media_store import can_edit_scene, scene_revision
from app.niche import load_profile
from app.planning import StudioPlanner
from app.postiz_client import PostizError
from app.preset_view import presets_page
from app.production import StudioProduction
from app.published_posts import METRICS, PLATFORMS, group_published_posts, post_metrics
from app.published_view import published_post_page, published_posts_page
from app.publishing import StudioPublishing
from app.scene_editor_view import scene_editor_page
from app.settings import StudioSettings
from app.settings_view import providers_page, settings_page
from app.watchdog import get_issue, list_issues
from app.watchdog_view import watchdog_issue_page, watchdog_page
from app.web import new_video_page, workspace_page


STUDIO_ROOT = Path(__file__).resolve().parents[1]
MAX_FORM_SIZE = 2 * 1024 * 1024
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
INBOX_RECORD = re.compile(r"/(ideas|schedules)/([0-9]{1,18})")
INBOX_RECORD_EDIT = re.compile(r"/(ideas|schedules)/([0-9]{1,18})/edit")
INBOX_ARCHIVE = re.compile(r"/(ideas|schedules)/([0-9]{1,18})/archive")
INBOX_CREATE_TYPES = {"/ideas": "manual", "/ideas/generate": "claude", "/schedules": "schedule"}
INBOX_RECORD_TYPES = {"manual", "claude", "schedule"}
INBOX_STATUSES = {"pending", "approved", "later", "rejected", "deleted"}
PUBLISHED_POST_PAGE = re.compile(r"/published-posts/([0-9]{1,18})")
PUBLISHED_ORIGINAL = re.compile(r"/published-posts/([0-9]{1,18})/original/([0-9]{1,18})")
WATCHDOG_ISSUE = re.compile(r"/watch-dog/([a-z0-9-]{1,80})")
WORKSPACE_SCENE = re.compile(r"/workspace/([0-9]{1,18})/scenes/([A-Za-z0-9][A-Za-z0-9_-]{0,63})")
JOB_MEDIA_PATH = re.compile(r"([0-9]{1,18})/([a-zA-Z0-9_.-]+)")
ASSET_ID = r"[a-z][a-z0-9_]{0,63}"
REFERENCE_ID = r"[a-zA-Z0-9_-]{1,80}"
CHARACTER_PAGE = re.compile(rf"/characters/({ASSET_ID})(/edit)?")
CHARACTER_ACTION = re.compile(
    rf"/characters/({ASSET_ID})/(edit|references(?:/{REFERENCE_ID})?|variants|outfit-color|voices|audio|audio-link|audio-remove)"
)
ASSET_LIBRARY_PAGE = re.compile(rf"/assets/({ASSET_ID})(/edit)?")
ASSET_LIBRARY_ACTION = re.compile(rf"/assets/({ASSET_ID})/(edit|references(?:/{REFERENCE_ID})?|variants)")
PRESET_ACTION = re.compile(r"/presets/(visual|script|subtitle|audio)")
PRESET_DEFAULT = re.compile(rf"/presets/(visual|script|subtitle|audio)/({ASSET_ID})/default")
BRAND_DEFAULT = re.compile(rf"/brand-rules/({ASSET_ID})/default")
BRAND_DELETE = re.compile(rf"/brand-rules/({ASSET_ID})/delete")


def parse_upload(content_type, body):
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + body,
    )
    if not message.is_multipart() or message.defects:
        raise ValueError("Invalid upload form.")
    form, files = {}, {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if (part.get_content_disposition() != "form-data" or part.is_multipart() or part.defects
                or not isinstance(name, str) or not re.fullmatch(r"[a-z_]{1,32}", name)
                or name in form or name in files or len(form) + len(files) >= 12):
            raise ValueError("Invalid upload field.")
        data = part.get_payload(decode=True)
        if not isinstance(data, bytes):
            raise ValueError("Invalid upload field data.")
        if part.get_filename() is not None:
            if part.get_filename() and data:
                files[name] = data
        else:
            if len(data) > 8192:
                raise ValueError("Upload field is too long.")
            form[name] = [data.decode("utf-8")]
    return form, files


def usd_micros(value):
    if not re.fullmatch(r"[0-9]{1,6}(?:\.[0-9]{1,6})?", value):
        raise ValueError("Enter a nonnegative USD amount with up to six decimal places.")
    return int(Decimal(value) * 1_000_000)


def inbox_record(kind, row):
    record_id = row["id"]
    status = row["status"]
    if kind == "schedule":
        title = row["title"] or f"Schedule #{record_id}"
        details = "\n".join(entry["prompt"] for entry in row["entries"])
        source = "schedule"
        status = "rejected" if row["rejected_at"] else ("pending" if status == "draft" else "approved")
    else:
        title = row["title"]
        details = row["prompt"]
        source = row["source"]
        if status == "accepted":
            status = "approved"
    return dict(
        row,
        kind=kind,
        index=f"{'S' if kind == 'schedule' else 'I'}-{record_id:03d}",
        title=title,
        details=details,
        source=source,
        status="deleted" if row["deleted_at"] else status,
        schedule_status=row["status"] if kind == "schedule" else None,
        job_status=row.get("job_status"),
    )


class StudioServer(ThreadingHTTPServer):
    def __init__(self, address, store):
        self.store = store
        self.csrf_token = secrets.token_urlsafe(32)
        super().__init__(address, StudioHandler)
        self.settings = StudioSettings(store, store.path.parent / ".env")
        self.settings.seed()
        self.planning = StudioPlanner(store, self.settings)
        self.library = self.planning.library
        self.production = StudioProduction(store, self.settings, self.planning)
        self.publishing = StudioPublishing(
            store, self.settings, self.production.root,
            os.environ.get("STUDIO_POSTIZ_URL", "http://127.0.0.1:4007"),
        )


class StudioHandler(BaseHTTPRequestHandler):
    timeout = 10

    def _is_local_host(self):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1:
            return False
        try:
            host = urlsplit("//" + hosts[0])
        except ValueError:
            return False
        return (
            host.hostname in LOCAL_HOSTS
            and not host.username
            and not host.path
            and not host.query
            and not host.fragment
        )

    def _reply(self, status, body, content_type="text/html; charset=utf-8"):
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(payload)

    def _show_inbox(self, status=HTTPStatus.OK, notice="", *, params=None):
        params = params or {}
        query = params.get("q", [""])[0].strip()[:100]
        kind = params.get("type", [""])[0]
        record_status = params.get("status", [""])[0]
        kind = kind if kind in INBOX_RECORD_TYPES else ""
        record_status = record_status if record_status in INBOX_STATUSES else ""
        include_deleted = record_status == "deleted"
        store = self.server.store
        records = []
        if kind != "schedule":
            records.extend(inbox_record("idea", row) for row in store.list_ideas(include_deleted=include_deleted))
        if kind in ("", "schedule"):
            records.extend(inbox_record("schedule", row) for row in store.list_schedules(include_deleted=include_deleted))
        search = query.casefold()
        records = [
            row for row in records
            if (not kind or row["source"] == kind)
            and (not record_status or row["status"] == record_status)
            and (search in row["title"].casefold() or search in row["index"].casefold())
        ]
        records.sort(key=lambda row: (row["created_at"] or "", row["id"], row["kind"]), reverse=True)
        self._reply(status, inbox_page(
            records, query=query, kind=kind, status=record_status,
            notice=notice, idea_requests=self.server.planning.idea_requests(),
        ))

    def _show_inbox_record(self, collection, record_id, status=HTTPStatus.OK, notice=""):
        store = self.server.store
        if collection == "ideas":
            kind, row = "idea", store.get_idea(record_id)
        else:
            kind, row = "schedule", store.get_schedule(record_id)
        if row is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._reply(status, record_page(inbox_record(kind, row), self.server.csrf_token, notice=notice))

    def _show_inbox_edit(self, collection, record_id, status=HTTPStatus.OK, notice="", values=None):
        store = self.server.store
        if collection == "ideas":
            row = store.get_idea(record_id)
            if row is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            html = edit_idea_page(
                inbox_record("idea", row), self.server.csrf_token, notice=notice,
                brand_profiles=store.list_brand_profiles(), values=values,
            )
        else:
            row = store.get_schedule(record_id)
            if row is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            html = edit_schedule_page(
                inbox_record("schedule", row), self.server.csrf_token, notice=notice,
                brand_profiles=store.list_brand_profiles(), values=values,
            )
        self._reply(status, html)

    def _show_published_posts(self, params):
        query = params.get("q", [""])[0].strip()[:100]
        platform = params.get("platform", [""])[0]
        platform = platform if platform in PLATFORMS else ""
        search = query.casefold()
        records = group_published_posts(self.server.publishing.posts.published_posts())
        records = [
            record for record in records
            if (search in record["title"].casefold() or search in record["index"].casefold())
            and (not platform or any(post["platform"] == platform for post in record["platforms"]))
        ]
        self._reply(HTTPStatus.OK, published_posts_page(records, query=query, platform=platform))

    def _show_published_post(self, job_id, params, notice=""):
        posts = self.server.publishing.posts
        records = group_published_posts(posts.published_posts(job_id))
        if not records:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        record = records[0]
        channel = params.get("channel", [""])[0]
        metric = params.get("metric", ["engagement"])[0]
        if metric not in METRICS:
            metric = "engagement"
        selected = next((post for post in record["platforms"] if str(post["id"]) == channel), record["platforms"][0])
        selected["history"] = [
            {"collected_at": snapshot["collected_at"], "value": post_metrics(snapshot["metrics"])[metric]}
            for snapshot in posts.published_history(selected)
        ]
        preview = selected["preview_path"]
        match = JOB_MEDIA_PATH.fullmatch(preview or "")
        if not match or int(match[1]) != job_id or not self._local_media_file(preview):
            selected["preview_path"] = None
        self._reply(HTTPStatus.OK, published_post_page(record, selected, metric=metric, notice=notice))

    def _open_published_original(self, job_id, request_id):
        publishing = self.server.publishing
        post = next((post for post in publishing.posts.published_posts(job_id) if post["id"] == request_id), None)
        if post is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            location = publishing.original_post_url(post)
        except (PostizError, ValueError) as error:
            notice = str(error)
        else:
            if location:
                self._redirect(location)
                return
            notice = "Postiz has no original link for this post."
        self._show_published_post(job_id, {"channel": [str(request_id)]}, notice)

    def _show_settings(self, status=HTTPStatus.OK, notice="", *, open_asset=None, open_presets=False):
        self._reply(status, settings_page(
            self.server.settings.get_budgets(),
            self.server.planning.idea_schedule(), self.server.csrf_token, notice,
        ))

    def _show_providers(self, status=HTTPStatus.OK, notice="", tab=""):
        platforms_info = self.server.settings.list_platforms()
        active_platforms = platforms_info[0]
        raw_keys = {name: (self.server.settings.key(name) or "") for name in active_platforms}
        self._reply(status, providers_page(
            self.server.settings.list_providers(), self.server.csrf_token, notice, active_tab=tab,
            platforms_info=platforms_info,
            configured_keys=self.server.settings.configured_keys(),
            raw_keys=raw_keys,
        ))

    def _show_character(self, asset_id=None, *, edit=False, status=HTTPStatus.OK, notice="", values=None):
        try:
            asset = self.server.library.assets.get_character("kids_3d", asset_id) if asset_id else None
        except LookupError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._reply(status, character_page(
            asset, self.server.csrf_token, edit=edit, notice=notice, values=values,
        ))

    def _show_asset(self, asset_id=None, *, edit=False, status=HTTPStatus.OK, notice="", values=None):
        try:
            asset = self.server.library.assets.get_asset("kids_3d", asset_id) if asset_id else None
        except LookupError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if asset and asset.get("kind") == "character":
            self._redirect(f"/characters/{asset_id}")
            return
        self._reply(status, asset_page(
            asset, self.server.csrf_token, edit=edit, notice=notice, values=values,
        ))

    def _redirect(self, location):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _show_workspace(self, job=None, status=HTTPStatus.OK, notice="", discover=False, *, options_override=None):
        tasks = self.server.production.media.job_tasks(job["id"]) if job else ()
        posts, metrics, channels = (), (), ()
        assets, presets, options = (), (), None
        if job:
            niche_id = job["niche_id"] or "kids_3d"
            assets = self.server.library.assets.list_assets(niche_id)
            presets = self.server.library.presets(niche_id)
            options = options_override if options_override is not None else json.loads(job["production_options_json"])
            posts = self.server.publishing.posts.job_posts(job["id"])
            metrics = self.server.publishing.metrics(job["id"])
            auto_discover = bool(job["preview_path"] and job["status"] in ("preview_ready", "needs_review"))
            if (discover or auto_discover):
                try:
                    channels = self.server.publishing.channels()
                    if not channels and discover:
                        notice = "Connect a supported channel in Postiz first."
                except (ValueError, PostizError) as error:
                    if discover:
                        notice = str(error)
        self._reply(status, workspace_page(
            self.server.store.list_jobs(), job, notice, tasks, self.server.csrf_token,
            posts=posts, channels=channels, metrics=metrics, assets=assets, presets=presets, production_options=options,
            production_options_open=options_override is not None,
            brand_profiles=self.server.store.list_brand_profiles(),
        ))

    def _show_scene_editor(self, job_id, scene_id, status=HTTPStatus.OK, notice="", *, values=None):
        job = self.server.store.get_job(job_id)
        if job is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        scenes = json.loads(job["plan_json"] or "[]")
        scene = next((item for item in scenes if item["scene_id"] == scene_id), None)
        if scene is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        tasks = self.server.production.media.job_tasks(job_id)
        scene_tasks = [task for task in tasks if task["scene_id"] == scene_id]
        assets = self.server.library.assets.list_assets(job["niche_id"] or "kids_3d")
        asset_names = {asset["id"]: asset["name"] for asset in assets}
        revision = scene_revision(job, tasks) if values is None else values["expected_revision"]
        self._reply(status, scene_editor_page(
            job, scene, scene_tasks, self.server.csrf_token, revision, asset_names,
            editable=can_edit_scene(job, tasks), notice=notice, values=values,
        ))

    def _serve_reference(self, relative_path):
        match = re.fullmatch(rf"({ASSET_ID})/({REFERENCE_ID})", relative_path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        library = self.server.library.assets
        asset = next((item for item in library.list_assets() if item["id"] == match[1]), {})
        reference = next((item for item in asset.get("references", []) if item["id"] == match[2]), None)
        try:
            path = library.reference_path(reference)
            image = path.read_bytes()
        except (OSError, ValueError):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._reply(HTTPStatus.OK, image, mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    def _serve_character_audio(self, asset_id):
        if not re.fullmatch(ASSET_ID, asset_id):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            path = self.server.library.assets.character_audio_path("kids_3d", asset_id)
        except (LookupError, OSError, ValueError):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._stream_media(path)

    def _serve_preset_audio(self, filename):
        filename = unquote(filename)
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", filename):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        root = (self.server.library.audio_root / "kids_3d").resolve()
        path = (root / filename).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._stream_media(path)

    def _local_media_file(self, relative_path):
        root = self.server.production.root.resolve()
        path = (root / relative_path).resolve()
        return path if path.is_relative_to(root) and path.is_file() else None

    def _serve_media(self, relative_path):
        match = JOB_MEDIA_PATH.fullmatch(relative_path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        job = self.server.store.get_job(int(match[1]))
        if job is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        tasks = self.server.production.media.job_tasks(job["id"])
        paths = {
            task["local_path"] for task in tasks
            if task["kind"] == "keyframe" or (task["kind"] == "video" and task["status"] == "completed")
        }
        paths.add(job["preview_path"])
        path = self._local_media_file(relative_path)
        if path is None or (
            relative_path not in paths and relative_path not in self.server.publishing.posts.delivered_preview_paths(job["id"])
        ):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        self._stream_media(path)

    def _stream_media(self, path):
        try:
            media_file = path.open("rb")
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        with media_file as media:
            size = os.fstat(media.fileno()).st_size
            header = media.read(12)
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if header.startswith(b"\xff\xd8\xff"):
                content_type = "image/jpeg"
            elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
                content_type = "image/webp"
            start, end = 0, size - 1
            requested = self.headers.get("Range")
            status = HTTPStatus.OK
            if requested:
                byte_range = re.fullmatch(r"bytes=(\d{0,18})-(\d{0,18})", requested)
                if not byte_range or not (byte_range[1] or byte_range[2]):
                    self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    return
                if byte_range[1]:
                    start = int(byte_range[1])
                    if byte_range[2]:
                        end = min(end, int(byte_range[2]))
                else:
                    start = max(0, size - int(byte_range[2]))
                if not 0 <= start <= end:
                    self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    return
                status = HTTPStatus.PARTIAL_CONTENT
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            media.seek(start)
            while length > 0:
                chunk = media.read(min(length, 64 * 1024))
                if not chunk:
                    break
                self.wfile.write(chunk)
                length -= len(chunk)

    def do_GET(self):
        if not self._is_local_host():
            self.send_error(HTTPStatus.FORBIDDEN)
            return

        url = urlsplit(self.path)
        if url.path == "/":
            params = parse_qs(url.query)
            notice = params.get("notice", [""])[0]
            self._show_inbox(params=params, notice=notice)
        elif url.path == "/inbox/create":
            kind = parse_qs(url.query).get("type", ["manual"])[0]
            if kind not in INBOX_RECORD_TYPES:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._reply(HTTPStatus.OK, create_page(
                self.server.csrf_token, kind=kind,
                brand_profiles=self.server.store.list_brand_profiles(),
            ))
        elif edit_record := INBOX_RECORD_EDIT.fullmatch(url.path):
            self._show_inbox_edit(edit_record[1], int(edit_record[2]))
        elif record := INBOX_RECORD.fullmatch(url.path):
            self._show_inbox_record(record[1], int(record[2]))
        elif url.path == "/published-posts":
            self._show_published_posts(parse_qs(url.query))
        elif original := PUBLISHED_ORIGINAL.fullmatch(url.path):
            self._open_published_original(int(original[1]), int(original[2]))
        elif post := PUBLISHED_POST_PAGE.fullmatch(url.path):
            self._show_published_post(int(post[1]), parse_qs(url.query))
        elif url.path == "/settings":
            params = parse_qs(url.query)
            if params.get("presets") == ["1"]:
                self._redirect("/presets")
                return
            asset = params.get("asset", [None])[0]
            if asset and re.fullmatch(ASSET_ID, asset):
                try:
                    self.server.library.assets.get_character("kids_3d", asset)
                except LookupError:
                    try:
                        self.server.library.assets.get_asset("kids_3d", asset)
                    except LookupError:
                        pass
                    else:
                        self._redirect(f"/assets/{asset}")
                        return
                else:
                    self._redirect(f"/characters/{asset}")
                    return
            self._show_settings()
        elif url.path == "/providers":
            tab = parse_qs(url.query).get("tab", [""])[0]
            self._show_providers(tab=tab)
        elif url.path == "/watch-dog":
            issues = list_issues(self.server.store, self.server.settings)
            self._reply(HTTPStatus.OK, watchdog_page(issues))
        elif issue_path := WATCHDOG_ISSUE.fullmatch(url.path):
            issue = get_issue(self.server.store, self.server.settings, issue_path[1])
            if issue is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._reply(HTTPStatus.OK, watchdog_issue_page(issue))
        elif url.path == "/characters":
            query = parse_qs(url.query).get("q", [""])[0].strip()[:100]
            self._reply(HTTPStatus.OK, characters_page(self.server.library.assets.list_assets(), query))
        elif url.path == "/characters/new/profile":
            self._show_character(edit=True)
        elif character := CHARACTER_PAGE.fullmatch(url.path):
            self._show_character(character[1], edit=bool(character[2]))
        elif url.path == "/assets":
            params = parse_qs(url.query)
            query = params.get("q", [""])[0].strip()[:100]
            kind = params.get("kind", ["all"])[0].strip()
            self._reply(HTTPStatus.OK, assets_page(self.server.library.assets.list_assets(), query, kind))
        elif url.path == "/assets/new/profile":
            self._show_asset(edit=True)
        elif asset_item := ASSET_LIBRARY_PAGE.fullmatch(url.path):
            self._show_asset(asset_item[1], edit=bool(asset_item[2]))
        elif url.path == "/presets":
            tab = parse_qs(url.query).get("tab", ["visual"])[0]
            self._reply(HTTPStatus.OK, presets_page(self.server.library.presets(), self.server.csrf_token, active_tab=tab))
        elif url.path == "/brand-rules":
            notice = parse_qs(url.query).get("notice", [""])[0]
            self._reply(HTTPStatus.OK, brand_rules_page(
                self.server.store.list_brand_profiles(), self.server.csrf_token, notice=notice,
            ))
        elif url.path == "/workspace":
            params = parse_qs(url.query)
            job_id = params.get("job", [None])[0]
            job = None
            if job_id is not None:
                try:
                    job = self.server.store.get_job(int(job_id))
                except (ValueError, LookupError, OverflowError):
                    pass
                if job is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
            self._show_workspace(job, discover=params.get("channels") == ["1"])
        elif url.path == "/workspace/create":
            self._reply(HTTPStatus.OK, new_video_page(
                self.server.csrf_token,
                brand_profiles=self.server.store.list_brand_profiles(),
            ))
        elif scene := WORKSPACE_SCENE.fullmatch(url.path):
            self._show_scene_editor(int(scene[1]), scene[2])
        elif url.path.startswith("/media/"):
            self._serve_media(url.path.removeprefix("/media/"))
        elif url.path.startswith("/asset-reference/"):
            self._serve_reference(url.path.removeprefix("/asset-reference/"))
        elif url.path.startswith("/character-audio/"):
            self._serve_character_audio(url.path.removeprefix("/character-audio/"))
        elif url.path == "/studio.css":
            css = (STUDIO_ROOT / "app/static/studio.css").read_text(encoding="utf-8")
            self._reply(HTTPStatus.OK, css, "text/css; charset=utf-8")
        elif url.path == "/studio.js":
            script = (STUDIO_ROOT / "app/static/studio.js").read_text(encoding="utf-8")
            self._reply(HTTPStatus.OK, script, "text/javascript; charset=utf-8")
        elif url.path == "/characters.js":
            script = (STUDIO_ROOT / "app/static/characters.js").read_text(encoding="utf-8")
            self._reply(HTTPStatus.OK, script, "text/javascript; charset=utf-8")
        elif url.path == "/assets.js":
            script = (STUDIO_ROOT / "app/static/assets.js").read_text(encoding="utf-8")
            self._reply(HTTPStatus.OK, script, "text/javascript; charset=utf-8")
        elif url.path == "/providers.js":
            script = (STUDIO_ROOT / "app/static/providers.js").read_text(encoding="utf-8")
            self._reply(HTTPStatus.OK, script, "text/javascript; charset=utf-8")
        elif url.path == "/presets.js":
            script = (STUDIO_ROOT / "app/static/presets.js").read_text(encoding="utf-8")
            self._reply(HTTPStatus.OK, script, "text/javascript; charset=utf-8")
        elif url.path in ("/brand_rules.js", "/brand-rules.js"):
            script = (STUDIO_ROOT / "app/static/brand_rules.js").read_text(encoding="utf-8")
            self._reply(HTTPStatus.OK, script, "text/javascript; charset=utf-8")
        elif url.path.startswith("/preset-audio/"):
            self._serve_preset_audio(url.path.removeprefix("/preset-audio/"))
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _save_asset_details(self, niche_id, asset_id, action, field, files):
        assets = self.server.library.assets
        if action == "references":
            return assets.add_reference(
                niche_id, asset_id, field("angle"), field("variant_id"), field("primary") == "yes", files.get("image"),
                upload_id=field("upload_id"),
            )
        if action == "variants":
            assets.add_variant(niche_id, asset_id, field("variant_id"), field("name"), field("description"),
                               field("dress_color"))
        elif action == "voices":
            assets.save_voice_style(
                niche_id, asset_id, field("style_id"), field("name"), field("voice"), field("speed"),
                field("is_default") == "yes", field("expressiveness") or None,
            )

    def _save_character(self, niche_id, asset_id, action, field, files):
        assets = self.server.library.assets
        if action == "edit":
            assets.update_character(niche_id, asset_id, field("name"), field("description"), field("dress_color"))
        elif action == "outfit-color":
            if not field("variant_id"):
                raise ValueError("Choose an outfit.")
            assets.update_character(niche_id, asset_id, "", "", field("dress_color"), field("variant_id"))
        elif action.startswith("references/"):
            assets.update_reference(
                niche_id, asset_id, action.removeprefix("references/"), field("angle"), field("variant_id"),
                field("primary") == "yes",
            )
        elif action == "audio":
            if "audio" not in files:
                raise ValueError("Choose an MP3 or WAV audio file.")
            assets.save_character_audio(niche_id, asset_id, audio_bytes=files["audio"])
        elif action == "audio-link":
            if not field("url").strip():
                raise ValueError("Enter a direct HTTPS audio link.")
            assets.save_character_audio(niche_id, asset_id, url=field("url"))
        elif action == "audio-remove":
            assets.save_character_audio(niche_id, asset_id)
        else:
            assets.get_character(niche_id, asset_id)
            return self._save_asset_details(niche_id, asset_id, action, field, files)

    def _save_asset_action(self, niche_id, asset_id, action, field, files):
        assets = self.server.library.assets
        if action == "edit":
            assets.update_asset(niche_id, asset_id, field("name"), field("description"))
        elif action == "variants":
            assets.add_variant(niche_id, asset_id, field("variant_id"), field("name"), field("description"))
        elif action == "references":
            return assets.add_reference(
                niche_id, asset_id, field("angle"), field("variant_id"), field("primary") == "yes", files.get("image"),
                upload_id=field("upload_id"),
            )
        elif action.startswith("references/"):
            assets.update_reference(
                niche_id, asset_id, action.removeprefix("references/"), field("angle"), field("variant_id"),
                field("primary") == "yes",
            )

    def do_POST(self):
        if not self._is_local_host():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        path = urlsplit(self.path).path
        content_type = self.headers.get_content_type()
        asset_route = re.fullmatch(rf"/settings/assets/({ASSET_ID})/(references|variants|voices)", path)
        preset_route = re.fullmatch(r"/settings/presets/(visual|script|subtitle|audio)", path)
        character_route = CHARACTER_ACTION.fullmatch(path)
        asset_action = ASSET_LIBRARY_ACTION.fullmatch(path)
        preset_action = PRESET_ACTION.fullmatch(path)
        preset_action_default = PRESET_DEFAULT.fullmatch(path)
        character_reference = character_route and character_route[2] == "references"
        asset_reference = asset_action and asset_action[2] == "references"
        character_audio = character_route and character_route[2] == "audio"
        upload_json = (character_reference or asset_reference) and self.headers.get("Accept") == "application/json"
        upload = (character_reference or asset_reference or character_audio
                  or (asset_route and asset_route[2] == "references")
                  or (preset_route and preset_route[1] == "audio")
                  or (preset_action and preset_action[1] == "audio"))
        expected = "multipart/form-data" if upload else "application/x-www-form-urlencoded"
        if content_type != expected:
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return

        maximum = MAX_FORM_SIZE
        if character_audio:
            maximum = 21 * 1024 * 1024
        elif upload:
            maximum = (42 if (preset_route or preset_action) else 11) * 1024 * 1024
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= maximum:
                self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete form.")
            if upload:
                form, files = parse_upload(self.headers["Content-Type"], body)
            else:
                files = {}
                form = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=512, errors="strict")
        except (ValueError, UnicodeError):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return

        def field(name):
            return form.get(name, [""])[0]

        if not hmac.compare_digest(field("csrf_token").encode(), self.server.csrf_token.encode()):
            self.send_error(HTTPStatus.FORBIDDEN)
            return

        idea_decision = re.fullmatch(r"/ideas/(\d{1,18})/decision", path)
        idea_control = re.fullmatch(r"/ideas/(\d{1,18})/job-control", path)
        idea_edit = re.fullmatch(r"/ideas/(\d{1,18})/edit", path)
        schedule_status = re.fullmatch(r"/schedules/(\d{1,18})/status", path)
        schedule_edit = re.fullmatch(r"/schedules/(\d{1,18})/edit", path)
        inbox_archive = INBOX_ARCHIVE.fullmatch(path)
        create_kind = INBOX_CREATE_TYPES.get(path)
        inbox_detail = None
        if idea_decision:
            inbox_detail = ("ideas", int(idea_decision[1]))
        elif idea_control:
            inbox_detail = ("ideas", int(idea_control[1]))
        elif idea_edit:
            inbox_detail = ("ideas", int(idea_edit[1]))
        elif schedule_status:
            inbox_detail = ("schedules", int(schedule_status[1]))
        elif schedule_edit:
            inbox_detail = ("schedules", int(schedule_edit[1]))
        elif inbox_archive:
            inbox_detail = (inbox_archive[1], int(inbox_archive[2]))
        key_route = re.fullmatch(r"/settings/keys/([A-Z_]+)", path)
        provider_route = re.fullmatch(r"/settings/providers/([a-z0-9-]+)", path)
        preset_default = re.fullmatch(rf"/settings/presets/(visual|script|subtitle|audio)/({ASSET_ID})/default", path)
        brand_default = BRAND_DEFAULT.fullmatch(path)
        brand_delete = BRAND_DELETE.fullmatch(path)
        workspace_route = re.fullmatch(r"/workspace/(\d{1,18})/(generate|approve-keyframes|retry-render|revise|publish|options|pause|resume|edit|delete)", path)
        scene_route = WORKSPACE_SCENE.fullmatch(path)
        store = self.server.store
        settings = self.server.settings
        planning = self.server.planning
        library = self.server.library
        niche_id = "kids_3d"
        in_settings = path.startswith("/settings/")
        options = None
        try:
            if path == "/ideas":
                if field("action") == "generate":
                    job_id = store.create_direct_job(field("prompt"), field("title") or None, brand_id=field("brand_id") or None, source="manual")
                    self._redirect(f"/workspace?job={job_id}")
                    return
                store.add_idea(field("prompt"), field("title") or None, brand_id=field("brand_id") or None)
            elif path == "/workspace/create":
                job_id = store.create_direct_job(field("prompt"), field("title") or None, brand_id=field("brand_id") or None, source="direct")
                self._redirect(f"/workspace?job={job_id}")
                return
            elif path == "/ideas/generate":
                trends = field("trends")
                brand_id = field("brand_id") or None
                ideas = planning.generate_three_ideas(trends, brand_id=brand_id)
                self._reply(HTTPStatus.OK, create_page(
                    self.server.csrf_token, kind="claude",
                    values={"trends": trends, "brand_id": brand_id},
                    brand_profiles=self.server.store.list_brand_profiles(),
                    generated_ideas=ideas,
                ))
                return
            elif path == "/ideas/accept":
                prompt = field("prompt")
                title = field("title") or None
                brand_id = field("brand_id") or None
                action = field("action")
                if action == "produce":
                    job_id = store.create_direct_job(prompt, title, brand_id=brand_id, source="claude")
                    self._redirect(f"/workspace?job={job_id}")
                    return
                else:
                    store.add_idea(prompt, title, source="claude", brand_id=brand_id)
                    self._redirect("/?notice=" + quote("Idea saved to inbox."))
                    return
            elif idea_decision:
                store.decide_idea(int(idea_decision[1]), field("decision"))
                if field("decision") == "accept":
                    idea_row = store.get_idea(int(idea_decision[1]))
                    if idea_row and idea_row.get("job_id"):
                        self._redirect(f"/workspace?job={idea_row['job_id']}")
                        return
            elif idea_control:
                action = field("action")
                if action == "pause":
                    store.pause_idea_job(int(idea_control[1]))
                elif action == "resume":
                    store.resume_idea_job(int(idea_control[1]))
                else:
                    raise ValueError("Choose Pause or Resume.")
            elif idea_edit:
                store.update_idea(
                    int(idea_edit[1]), field("prompt"), field("title") or None, brand_id=field("brand_id") or None,
                )
            elif path == "/schedules":
                daily_limit = field("daily_limit")
                if not re.fullmatch(r"[0-9]{1,3}", daily_limit):
                    raise ValueError("Enter the number of videos to start each day.")
                title = field("title").strip()
                if "title" in form and not title:
                    raise ValueError("Enter a schedule title.")
                prompts = [line for line in field("prompts").splitlines() if line.strip()]
                store.create_schedule(
                    prompts, field("start_date"), field("run_at"),
                    field("timezone"), int(daily_limit), title=title or None,
                    brand_id=field("brand_id") or None,
                )
            elif schedule_status:
                store.set_schedule_status(int(schedule_status[1]), field("status"))
            elif schedule_edit:
                daily_limit = field("daily_limit")
                if not re.fullmatch(r"[0-9]{1,3}", daily_limit):
                    raise ValueError("Enter the number of videos to start each day.")
                store.update_schedule(
                    int(schedule_edit[1]), field("title"), field("start_date"), field("run_at"),
                    field("timezone"), int(daily_limit), brand_id=field("brand_id") or None,
                )
            elif inbox_archive:
                action = field("action")
                if action not in ("delete", "restore"):
                    raise ValueError("Choose Delete or Restore.")
                kind = "idea" if inbox_archive[1] == "ideas" else "schedule"
                store.set_inbox_deleted(kind, int(inbox_archive[2]), action == "delete")
            elif key_route:
                action = field("action")
                if action == "remove":
                    settings.set_key(key_route[1], None)
                elif action == "save" and field("value").strip():
                    settings.set_key(key_route[1], field("value").strip())
                else:
                    raise ValueError("Enter an API key to save.")
            elif path == "/settings/platforms/add":
                platform_key = field("platform_key").strip()
                if platform_key == "custom":
                    custom_key = field("custom_key").strip().upper()
                    custom_name = field("custom_name").strip() or custom_key
                    settings.add_platform(custom_key, name=custom_name)
                elif platform_key:
                    settings.add_platform(platform_key)
                else:
                    raise ValueError("Select a platform to add.")
            elif path == "/settings/platforms/remove":
                key_name = field("key_name").strip().upper()
                if key_name:
                    settings.remove_platform(key_name)
                else:
                    raise ValueError("Specify a platform to remove.")
            elif path == "/settings/platforms/categories":
                key_name = field("platform_key").strip().upper()
                categories_str = field("categories").strip()
                if not key_name:
                    raise ValueError("Specify a platform key.")
                cats_list = [c.strip() for c in categories_str.split(",") if c.strip()]
                updated = settings.update_platform_categories(key_name, cats_list)
                if "application/json" in self.headers.get("Accept", ""):
                    body = json.dumps({"status": "ok", "platform": key_name, "categories": updated}).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self._redirect(f"/providers?tab={field('tab') or 'api-keys'}")
                return
            elif provider_route:
                settings.update_provider(
                    provider_route[1], field("enabled") == "yes", int(field("priority")), usd_micros(field("rate")),
                )
            elif path == "/settings/budgets":
                settings.set_budgets(usd_micros(field("daily")), usd_micros(field("video")))
            elif path == "/settings/idea-schedule":
                planning.set_idea_schedule(
                    field("enabled") == "yes", field("cadence"), field("run_at"), field("timezone"),
                    int(field("weekday")), field("trends").strip(), datetime.now(timezone.utc),
                )
            elif path == "/settings/assets":
                library.assets.create_asset(niche_id, field("asset_id"), field("kind"), field("name"), field("description"))
            elif path == "/assets":
                library.assets.create_asset(niche_id, field("asset_id"), field("kind"), field("name"), field("description"))
            elif asset_action:
                reference_id = self._save_asset_action(niche_id, *asset_action.groups(), field, files)
            elif asset_route:
                self._save_asset_details(niche_id, *asset_route.groups(), field, files)
            elif path == "/characters":
                library.assets.create_asset(
                    niche_id, field("asset_id"), "character", field("name"), field("description"), field("dress_color"),
                )
            elif character_route:
                reference_id = self._save_character(niche_id, *character_route.groups(), field, files)
            elif preset_route:
                library.add_preset(niche_id, preset_route[1], field("preset_id"), field("name"),
                                   {name: field(name) for name in form}, files, field("is_default") == "yes")
            elif preset_action:
                library.add_preset(niche_id, preset_action[1], field("preset_id"), field("name"),
                                   {name: field(name) for name in form}, files, field("is_default") == "yes")
            elif preset_default:
                library.set_default(niche_id, preset_default[1], preset_default[2])
            elif preset_action_default:
                library.set_default(niche_id, preset_action_default[1], preset_action_default[2])
            elif path == "/brand-rules":
                is_def = field("is_default") in ("1", "yes", "true", "on")
                store.create_brand_profile(
                    niche_id,
                    field("brand_id").strip(),
                    field("name").strip(),
                    field("mother_prompt").strip(),
                    brand_rules=field("brand_rules").strip(),
                    story_rules=field("story_rules").strip(),
                    is_default=is_def,
                )
            elif brand_default:
                store.set_default_brand_profile(niche_id, brand_default[1])
            elif brand_delete:
                store.delete_brand_profile(niche_id, brand_delete[1])
            elif scene_route:
                scene_values = {
                    "action": field("action"),
                    "camera": field("camera"),
                    "dialogue": form.get("dialogue", []),
                    "regenerate_keyframe": field("regenerate_keyframe") == "1",
                    "expected_revision": field("revision"),
                }
                self.server.production.edit_scene(int(scene_route[1]), scene_route[2], **scene_values)
            elif workspace_route:
                job_id = int(workspace_route[1])
                action = workspace_route[2]
                if action == "generate":
                    job = store.get_job(job_id)
                    if job is None:
                        raise LookupError("Video job not found.")
                    if job["status"] == "paused":
                        store.resume_job(job_id)
                    if job["status"] == "review_keyframes":
                        self.server.production.media.approve_keyframes(job_id, expected_revision=field("revision") or None)
                        self.server.production.advance(datetime.now(timezone.utc))
                    elif job["status"] in ("queued", "needs_review", "paused"):
                        with store._connection(write=True) as db:
                            db.execute("UPDATE jobs SET stage_error = NULL WHERE id = ?", (job_id,))
                        now = datetime.now(timezone.utc)
                        self.server.planning.process_plan(now)
                        self.server.production.advance(now)
                        updated_job = store.get_job(job_id)
                        if updated_job and updated_job.get("stage_error"):
                            raise ValueError(updated_job["stage_error"])
                elif action == "approve-keyframes":
                    self.server.production.media.approve_keyframes(job_id, expected_revision=field("revision"))
                elif action == "retry-render":
                    self.server.production.retry_render(job_id)
                elif action == "pause":
                    store.pause_job(job_id)
                elif action == "resume":
                    store.resume_job(job_id)
                elif action == "edit":
                    store.update_job_content(job_id, field("prompt"), field("title") or None)
                elif action == "delete":
                    store.delete_job(job_id)
                    self._redirect("/workspace")
                    return
                elif action == "options":
                    options = {group: {name[len(prefix):]: field(name) for name in form
                                       if name.startswith(prefix) and field(name)}
                               for group, prefix in (("presets", "preset."), ("variants", "variant."), ("voices", "voice."))}
                    options["characters"] = sorted(set(form.get("characters", [])))
                    self.server.production.configure(job_id, options)
                elif action == "publish":
                    mode = field("mode")
                    if mode not in ("draft", "schedule"):
                        raise ValueError("Choose to create a draft or schedule a post.")
                    self.server.publishing.approve(
                        job_id, field("channel"), field("title").strip(), field("caption").strip(),
                        field("hashtags"), field("scheduled_at"), field("timezone"), mode == "draft",
                        expected_preview_path=field("preview_path"),
                    )
                else:
                    self.server.production.revise(job_id, field("feedback"))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
        except (ValueError, PostizError) as error:
            if upload_json:
                self._reply(HTTPStatus.BAD_REQUEST, json.dumps({"notice": str(error)}), "application/json")
            elif character_route or path == "/characters":
                asset_id = character_route[1] if character_route else None
                values = None
                if path == "/characters" or character_route[2] == "edit":
                    values = {name: field(name) for name in ("asset_id", "name", "description", "dress_color")}
                self._show_character(asset_id, edit=True, status=HTTPStatus.BAD_REQUEST, notice=str(error), values=values)
            elif asset_action or path == "/assets":
                asset_id = asset_action[1] if asset_action else None
                values = None
                if path == "/assets" or asset_action[2] == "edit":
                    values = {name: field(name) for name in ("asset_id", "kind", "name", "description")}
                self._show_asset(asset_id, edit=True, status=HTTPStatus.BAD_REQUEST, notice=str(error), values=values)
            elif key_route or provider_route:
                self._show_providers(HTTPStatus.BAD_REQUEST, str(error))
            elif scene_route:
                self._show_scene_editor(int(scene_route[1]), scene_route[2], HTTPStatus.BAD_REQUEST, str(error),
                                        values=scene_values)
            elif workspace_route:
                self._show_workspace(store.get_job(job_id), HTTPStatus.BAD_REQUEST, str(error),
                                     discover=workspace_route[2] == "publish", options_override=options)
            elif path == "/workspace/create":
                self._reply(HTTPStatus.BAD_REQUEST, new_video_page(
                    self.server.csrf_token,
                    values={name: field(name) for name in ("title", "prompt", "brand_id")},
                    notice=str(error),
                    brand_profiles=self.server.store.list_brand_profiles(),
                ))
            elif create_kind:
                fields = ("title", "prompt", "trends", "prompts", "start_date", "run_at", "timezone", "daily_limit", "brand_id")
                self._reply(HTTPStatus.BAD_REQUEST, create_page(
                    self.server.csrf_token, kind=create_kind, values={name: field(name) for name in fields},
                    notice=str(error), brand_profiles=self.server.store.list_brand_profiles(),
                ))
            elif path == "/brand-rules" or brand_default or brand_delete:
                self._reply(HTTPStatus.BAD_REQUEST, brand_rules_page(
                    self.server.store.list_brand_profiles(), self.server.csrf_token, notice=str(error),
                    values={name: field(name) for name in ("brand_id", "name", "mother_prompt", "brand_rules", "story_rules")},
                ))
            elif idea_edit or schedule_edit:
                self._show_inbox_edit(*inbox_detail, HTTPStatus.BAD_REQUEST, str(error), values={name: field(name) for name in form})
            elif inbox_detail:
                self._show_inbox_record(*inbox_detail, HTTPStatus.BAD_REQUEST, str(error))
            else:
                show_error = self._show_settings if in_settings else self._show_inbox
                show_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        except LookupError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        location = f"/workspace?job={job_id}" if workspace_route else ("/settings" if in_settings else "/")
        if upload_json:
            self._reply(HTTPStatus.CREATED, json.dumps({"id": reference_id, "notice": "Image uploaded."}), "application/json")
            return
        if scene_route:
            location = path
        elif key_route or provider_route or path.startswith("/settings/platforms"):
            location = "/providers"
            if form.get("tab"):
                location += f"?tab={form['tab'][0]}"
        elif path == "/characters":
            location = f"/characters/{field('asset_id')}/edit"
        elif character_route:
            location = f"/characters/{character_route[1]}"
            action = character_route[2]
            if action != "edit":
                location += "/edit"
                if action.startswith("references"):
                    location += "#character-images"
                elif action.startswith("audio") or action == "voices":
                    location += "#character-voice"
                else:
                    location += "#character-overview"
        elif path == "/assets":
            location = f"/assets/{field('asset_id')}"
        elif asset_action:
            location = f"/assets/{asset_action[1]}"
            action = asset_action[2]
            if action != "edit":
                if action.startswith("references"):
                    location += "#asset-images"
                elif action == "variants":
                    location += "#asset-overview"
        elif preset_action:
            location = f"/presets?tab={preset_action[1]}"
        elif preset_action_default:
            location = f"/presets?tab={preset_action_default[1]}"
        elif path == "/brand-rules" or brand_default or brand_delete:
            location = "/brand-rules"
        elif asset_route or path == "/settings/assets":
            asset_id = asset_route[1] if asset_route else field("asset_id")
            location = f"/settings?asset={asset_id}#asset-{asset_id}"
        elif preset_route or preset_default:
            location = "/settings?presets=1#production-presets"
        elif inbox_detail and not inbox_archive and (field("from_detail") == "1" or idea_edit or schedule_edit or idea_control):
            location = f"/{inbox_detail[0]}/{inbox_detail[1]}"
        self._redirect(location)


def run_schedule_clock(server, stopped):
    stages = (
        ("prompt schedule", server.store.dispatch_due),
        ("idea schedule", server.planning.dispatch_idea_schedule),
        ("idea generation", server.planning.process_ideas),
        ("scene planning", server.planning.process_plan),
        ("media production", server.production.advance),
        ("publishing", server.publishing.process),
    )
    while not stopped.is_set():
        for name, process in stages:
            try:
                process(datetime.now(timezone.utc))
            except Exception:
                logging.exception("Studio queue stage failed: %s", name)
        stopped.wait(15)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    data_dir = Path(os.environ.get("STUDIO_DATA_DIR", STUDIO_ROOT / "data"))
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = StudioStore(data_dir / "studio.sqlite3")
    store.initialize()
    store.seed_niche(load_profile())

    host = os.environ.get("STUDIO_HOST", "127.0.0.1")
    port = int(os.environ.get("STUDIO_PORT", "8090"))
    stopped = threading.Event()
    with StudioServer((host, port), store) as server:
        server.planning.recover_interrupted()
        server.production.recover_interrupted()
        server.publishing.posts.recover_interrupted()
        clock = threading.Thread(target=run_schedule_clock, args=(server, stopped), daemon=True)
        clock.start()
        logging.info("Studio is listening on port %s", server.server_port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stopped.set()
            clock.join()


if __name__ == "__main__":
    main()
