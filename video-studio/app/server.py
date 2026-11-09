"""Local HTTP entry point and clock for Video Studio."""

import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.db import StudioStore
from app.niche import load_profile
from app.planning import StudioPlanner
from app.postiz_client import PostizError
from app.production import StudioProduction
from app.publishing import StudioPublishing
from app.settings import StudioSettings
from app.settings_view import settings_page
from app.web import inbox_page, workspace_page


STUDIO_ROOT = Path(__file__).resolve().parents[1]
MAX_FORM_SIZE = 2 * 1024 * 1024
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
ASSET_ID = r"[a-z][a-z0-9_]{0,63}"


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

    def _show_inbox(self, status=HTTPStatus.OK, notice=""):
        store = self.server.store
        self._reply(status, inbox_page(
            store.list_ideas(), store.list_schedules(), self.server.csrf_token, notice,
            self.server.planning.idea_requests(),
        ))

    def _show_settings(self, status=HTTPStatus.OK, notice="", *, open_asset=None, open_presets=False):
        self._reply(status, settings_page(
            self.server.settings.list_providers(), self.server.settings.get_budgets(),
            self.server.planning.idea_schedule(), self.server.csrf_token, notice,
            assets=self.server.library.assets.list_assets(), presets=self.server.library.presets(),
            open_asset=open_asset, open_presets=open_presets,
        ))

    def _show_workspace(self, job=None, status=HTTPStatus.OK, notice="", discover=False):
        tasks = self.server.production.media.job_tasks(job["id"]) if job else ()
        posts, metrics, channels = (), (), ()
        assets, presets, options = (), (), None
        if job:
            niche_id = job["niche_id"] or "kids_3d"
            assets = self.server.library.assets.list_assets(niche_id)
            presets = self.server.library.presets(niche_id)
            options = json.loads(job["production_options_json"])
            posts = self.server.publishing.posts.job_posts(job["id"])
            metrics = self.server.publishing.metrics(job["id"])
            if discover and job["preview_path"] and job["status"] in ("preview_ready", "needs_review"):
                try:
                    channels = self.server.publishing.channels()
                    if not channels:
                        notice = "Connect a supported channel in Postiz first."
                except (ValueError, PostizError) as error:
                    notice = str(error)
        self._reply(status, workspace_page(
            self.server.store.list_jobs(), job, notice, tasks, self.server.csrf_token,
            posts=posts, channels=channels, metrics=metrics, assets=assets, presets=presets, production_options=options,
        ))

    def _serve_reference(self, relative_path):
        match = re.fullmatch(rf"({ASSET_ID})/([a-zA-Z0-9_-]{{1,80}})", relative_path)
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

    def _serve_media(self, relative_path):
        match = re.fullmatch(r"([0-9]{1,18})/([a-zA-Z0-9_.-]+)", relative_path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        job = self.server.store.get_job(int(match[1]))
        if job is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        tasks = self.server.production.media.job_tasks(job["id"])
        paths = {task["local_path"] for task in tasks if task["kind"] == "keyframe"}
        paths.add(job["preview_path"])
        root = self.server.production.root.resolve()
        path = (root / relative_path).resolve()
        if relative_path not in paths or not path.is_relative_to(root) or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        with path.open("rb") as media:
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
            self._show_inbox()
        elif url.path == "/settings":
            params = parse_qs(url.query)
            asset = params.get("asset", [None])[0]
            self._show_settings(open_asset=asset, open_presets=params.get("presets") == ["1"])
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
        elif url.path.startswith("/media/"):
            self._serve_media(url.path.removeprefix("/media/"))
        elif url.path.startswith("/asset-reference/"):
            self._serve_reference(url.path.removeprefix("/asset-reference/"))
        elif url.path == "/studio.css":
            css = (STUDIO_ROOT / "app/static/studio.css").read_text(encoding="utf-8")
            self._reply(HTTPStatus.OK, css, "text/css; charset=utf-8")
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if not self._is_local_host():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        path = urlsplit(self.path).path
        content_type = self.headers.get_content_type()
        asset_route = re.fullmatch(rf"/settings/assets/({ASSET_ID})/(references|variants|voices)", path)
        preset_route = re.fullmatch(r"/settings/presets/(visual|script|subtitle|audio)", path)
        upload = (asset_route and asset_route[2] == "references") or (preset_route and preset_route[1] == "audio")
        expected = "multipart/form-data" if upload else "application/x-www-form-urlencoded"
        if content_type != expected:
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            maximum = ((42 if preset_route else 11) * 1024 * 1024) if upload else MAX_FORM_SIZE
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
        schedule_status = re.fullmatch(r"/schedules/(\d{1,18})/status", path)
        key_route = re.fullmatch(r"/settings/keys/([A-Z_]+)", path)
        provider_route = re.fullmatch(r"/settings/providers/([a-z0-9-]+)", path)
        preset_default = re.fullmatch(rf"/settings/presets/(visual|script|subtitle|audio)/({ASSET_ID})/default", path)
        workspace_route = re.fullmatch(r"/workspace/(\d{1,18})/(approve-keyframes|retry-render|revise|publish|options)", path)
        store = self.server.store
        settings = self.server.settings
        planning = self.server.planning
        library = self.server.library
        niche_id = "kids_3d"
        in_settings = path.startswith("/settings/")
        try:
            if path == "/ideas":
                store.add_idea(field("prompt"), field("title") or None)
            elif path == "/ideas/generate":
                planning.request_ideas(field("trends"))
            elif idea_decision:
                store.decide_idea(int(idea_decision[1]), field("decision"))
            elif path == "/schedules":
                daily_limit = field("daily_limit")
                if not re.fullmatch(r"[0-9]{1,3}", daily_limit):
                    raise ValueError("Enter the number of videos to start each day.")
                prompts = [line for line in field("prompts").splitlines() if line.strip()]
                store.create_schedule(
                    prompts, field("start_date"), field("run_at"),
                    field("timezone"), int(daily_limit),
                )
            elif schedule_status:
                store.set_schedule_status(int(schedule_status[1]), field("status"))
            elif key_route:
                action = field("action")
                if action == "remove":
                    settings.set_key(key_route[1], None)
                elif action == "save" and field("value").strip():
                    settings.set_key(key_route[1], field("value").strip())
                else:
                    raise ValueError("Enter an API key to save.")
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
            elif asset_route:
                asset_id, action = asset_route.groups()
                if action == "references":
                    library.assets.add_reference(niche_id, asset_id, field("angle"), field("variant_id"),
                                                 field("primary") == "yes", files.get("image"))
                elif action == "variants":
                    library.assets.add_variant(niche_id, asset_id, field("variant_id"), field("name"), field("description"))
                else:
                    library.assets.save_voice_style(
                        niche_id, asset_id, field("style_id"), field("name"), field("voice"), field("speed"),
                        field("is_default") == "yes", field("expressiveness") or None,
                    )
            elif preset_route:
                library.add_preset(niche_id, preset_route[1], field("preset_id"), field("name"),
                                   {name: field(name) for name in form}, files, field("is_default") == "yes")
            elif preset_default:
                library.set_default(niche_id, preset_default[1], preset_default[2])
            elif workspace_route:
                job_id = int(workspace_route[1])
                if workspace_route[2] == "approve-keyframes":
                    self.server.production.media.approve_keyframes(job_id)
                elif workspace_route[2] == "retry-render":
                    self.server.production.retry_render(job_id)
                elif workspace_route[2] == "options":
                    options = {group: {name[len(prefix):]: field(name) for name in form
                                       if name.startswith(prefix) and field(name)}
                               for group, prefix in (("presets", "preset."), ("variants", "variant."), ("voices", "voice."))}
                    self.server.production.configure(job_id, options)
                elif workspace_route[2] == "publish":
                    mode = field("mode")
                    if mode not in ("draft", "schedule"):
                        raise ValueError("Choose to create a draft or schedule a post.")
                    self.server.publishing.approve(
                        job_id, field("channel"), field("title").strip(), field("caption").strip(),
                        field("hashtags"), field("scheduled_at"), field("timezone"), mode == "draft",
                    )
                else:
                    self.server.production.revise(job_id, field("feedback"))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
        except (ValueError, PostizError) as error:
            if workspace_route:
                self._show_workspace(store.get_job(job_id), HTTPStatus.BAD_REQUEST, str(error),
                                     discover=workspace_route[2] == "publish")
            else:
                show_error = self._show_settings if in_settings else self._show_inbox
                show_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        except LookupError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        self.send_response(HTTPStatus.SEE_OTHER)
        location = f"/workspace?job={job_id}" if workspace_route else ("/settings" if in_settings else "/")
        if asset_route or path == "/settings/assets":
            asset_id = asset_route[1] if asset_route else field("asset_id")
            location = f"/settings?asset={asset_id}#asset-{asset_id}"
        elif preset_route or preset_default:
            location = "/settings?presets=1#production-presets"
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()


def run_schedule_clock(server, stopped):
    while not stopped.is_set():
        try:
            now = datetime.now(timezone.utc)
            server.store.dispatch_due(now)
            server.planning.dispatch_idea_schedule(now)
            server.planning.process_ideas(now)
            server.planning.process_plan(now)
            server.production.advance(now)
            server.publishing.process(now)
        except sqlite3.Error:
            logging.exception("Studio queue processing failed")
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
