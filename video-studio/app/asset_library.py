"""Persistent profiles, media references, variants, and voices for Studio assets."""

import json
import math
import os
import re
import subprocess
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from uuid import uuid4

from app.niche import STUDIO_ROOT


ASSET_ID = re.compile(r"[a-z][a-z0-9_]{0,63}")
ASSET_KINDS = ("character", "place", "item")
REFERENCE_ANGLES = ("front", "three-quarter", "side", "back", "full-body", "other")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_IMAGE_EDGE = 4096
MAX_AUDIO_SECONDS = 900


def _valid_id(value, label):
    if not isinstance(value, str) or ASSET_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must start with a lowercase letter and use up to 64 letters, digits, or underscores.")


def _text(value, label, limit):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError(f"Enter {label.lower()} in 1–{limit} characters.")
    return value.strip()


def _number(value, label, minimum, maximum):
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        number = math.nan
    if not minimum <= number <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    return number


def _dress_color(value):
    if value == "":
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("Enter a dress color in #RRGGBB format.")
    return value.upper()


def _audio_url(url):
    if (
        not isinstance(url, str) or not 1 <= len(url) <= 4096 or "\\" in url
        or any(not 33 <= ord(char) <= 126 for char in url)
    ):
        raise ValueError("Enter an HTTPS audio link without credentials.")
    try:
        parts = urlsplit(url)
        port = parts.port
        if (
            parts.scheme != "https" or not parts.hostname or parts.username is not None
            or not re.fullmatch(r"[a-zA-Z0-9.:-]+", parts.hostname) or port == 0
        ):
            raise ValueError
    except ValueError:
        raise ValueError("Enter an HTTPS audio link without credentials.") from None
    return url


def _validate_reference_fields(angle, variant_id, primary):
    if angle not in REFERENCE_ANGLES:
        raise ValueError("Choose a valid reference angle.")
    if variant_id:
        _valid_id(variant_id, "Variant ID")
    if type(primary) is not bool:
        raise ValueError("Choose whether the reference is primary.")


def _references(raw):
    references = []
    for index, entry in enumerate(json.loads(raw), start=1):
        if isinstance(entry, str):
            entry = {
                "id": f"bundled_{index}", "path": entry, "source": "bundled",
                "angle": "front", "variant_id": "", "primary": index == 1,
            }
        references.append(entry)
    return references


def _probe_upload(data, extension, input_format, stream_type):
    """Decode only an allowlisted media format from a temporary local file."""
    try:
        with NamedTemporaryFile(suffix=extension) as source:
            source.write(data)
            source.flush()
            ffprobe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-max_alloc", "67108864",
                    "-protocol_whitelist", "file", "-f", input_format,
                    "-select_streams", f"{stream_type}:0",
                    "-show_entries", "stream=codec_name,width,height:format=duration",
                    "-of", "json", source.name,
                ],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=10,
            )
            metadata = json.loads(ffprobe.stdout)
            stream, = metadata["streams"]
            if stream_type == "v":
                if not (
                    stream["codec_name"] in ("png", "mjpeg")
                    and 0 < stream["width"] <= MAX_IMAGE_EDGE
                    and 0 < stream["height"] <= MAX_IMAGE_EDGE
                ):
                    raise ValueError
                decode_limit = ["-frames:v", "1"]
            else:
                duration = float(metadata["format"]["duration"])
                if not 0 < duration <= MAX_AUDIO_SECONDS:
                    raise ValueError
                decode_limit = ["-t", "1"]
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-xerror",
                    "-max_alloc", "67108864", "-threads", "1",
                    "-protocol_whitelist", "file,pipe", "-f", input_format, "-i", source.name,
                    "-map", f"0:{stream_type}:0", *decode_limit, "-f", "null", "-",
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=10,
            )
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
        message = (
            "Upload a readable PNG or JPG image up to 4096 × 4096 pixels."
            if stream_type == "v"
            else "Upload a readable MP3 or WAV file up to 15 minutes long."
        )
        raise ValueError(message) from error


def _image_extension(data):
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_IMAGE_BYTES:
        raise ValueError("Upload a PNG or JPG image up to 10 MiB.")
    if data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"\x00\x00\x00\x00IEND\xaeB\x60\x82"):
        extension, input_format = ".png", "png_pipe"
    elif data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        extension, input_format = ".jpg", "jpeg_pipe"
    else:
        raise ValueError("Upload a PNG or JPG image up to 10 MiB.")
    _probe_upload(data, extension, input_format, "v")
    return extension


def _write_upload(directory, filename, data):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=directory, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        destination = directory / filename
        temporary.replace(destination)
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_audio_upload(root, data):
    """Save validated audio and return its local basename."""
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_AUDIO_BYTES:
        raise ValueError("Upload an MP3 or WAV audio file up to 20 MiB.")
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        extension, input_format = ".wav", "wav"
    elif data.startswith(b"ID3") or (len(data) > 2 and data[0] == 0xff and data[1] & 0xe0 == 0xe0):
        extension, input_format = ".mp3", "mp3"
    else:
        raise ValueError("Upload an MP3 or WAV audio file up to 20 MiB.")
    _probe_upload(data, extension, input_format, "a")
    path = _write_upload(Path(root), f"audio_{uuid4().hex}{extension}", data)
    return path.name


def _local_audio_file(directory, audio):
    if audio and audio["source"] == "upload" and re.fullmatch(r"audio_[0-9a-f]{32}\.(mp3|wav)", audio["path"]):
        return directory / audio["path"]
    return None


class AssetLibrary:
    def __init__(self, store):
        self.store = store
        self.root = store.path.parent / "assets"

    def list_assets(self, niche_id="kids_3d"):
        _valid_id(niche_id, "Niche ID")
        with self.store._connection() as db:
            return [
                self._asset_record(row)
                for row in db.execute(
                    "SELECT * FROM assets WHERE niche_id = ? ORDER BY type, name COLLATE NOCASE, id",
                    (niche_id,),
                )
            ]

    @staticmethod
    def _asset_record(row):
        metadata = json.loads(row["metadata_json"])
        return {
            "id": row["id"],
            "kind": row["type"],
            "name": row["name"],
            "description": row["description"],
            "references": _references(row["reference_images_json"]),
            "variants": metadata.get("variants", []),
            "voice_styles": metadata.get("voice_styles", []),
            "dress_color": metadata.get("dress_color", ""),
            "audio": metadata.get("audio"),
        }

    @staticmethod
    def _asset_row(db, niche_id, asset_id):
        row = db.execute(
            "SELECT * FROM assets WHERE niche_id = ? AND id = ?", (niche_id, asset_id),
        ).fetchone()
        if row is None:
            raise LookupError("Asset not found.")
        return row

    def _character_row(self, db, niche_id, asset_id):
        row = self._asset_row(db, niche_id, asset_id)
        if row["type"] != "character":
            raise LookupError("Character not found.")
        return row

    def _asset_directory(self, niche_id, asset_id):
        root = self.root.resolve()
        directory = (root / niche_id / asset_id).resolve()
        if not directory.is_relative_to(root):
            raise ValueError("Asset storage is outside the Studio data directory.")
        return directory

    def get_character(self, niche_id, asset_id):
        _valid_id(niche_id, "Niche ID")
        _valid_id(asset_id, "Character ID")
        with self.store._connection() as db:
            return self._asset_record(self._character_row(db, niche_id, asset_id))

    def update_character(self, niche_id, asset_id, name, description, dress_color, variant_id=""):
        _valid_id(niche_id, "Niche ID")
        _valid_id(asset_id, "Character ID")
        dress_color = _dress_color(dress_color)
        with self.store._connection(write=True) as db:
            row = self._character_row(db, niche_id, asset_id)
            metadata = json.loads(row["metadata_json"])
            if variant_id:
                _valid_id(variant_id, "Variant ID")
                variant = next((item for item in metadata.get("variants", []) if item["id"] == variant_id), None)
                if variant is None:
                    raise LookupError("Appearance not found.")
                variant["dress_color"] = dress_color
                name, description = row["name"], row["description"]
            else:
                name = _text(name, "Character name", 100)
                description = _text(description, "Character description", 2000)
                metadata["dress_color"] = dress_color
            db.execute(
                "UPDATE assets SET name = ?, description = ?, metadata_json = ? WHERE niche_id = ? AND id = ?",
                (name, description, json.dumps(metadata), niche_id, asset_id),
            )

    def create_asset(self, niche_id, asset_id, kind, name, description, dress_color=""):
        _valid_id(niche_id, "Niche ID")
        _valid_id(asset_id, "Asset ID")
        if kind not in ASSET_KINDS:
            raise ValueError("Choose character, place, or item.")
        name = _text(name, "Asset name", 100)
        description = _text(description, "Asset description", 2000)
        dress_color = _dress_color(dress_color)
        if dress_color and kind != "character":
            raise ValueError("Dress colors belong to characters.")
        metadata = {"dress_color": dress_color} if dress_color else {}
        with self.store._connection(write=True) as db:
            if db.execute("SELECT 1 FROM niches WHERE id = ?", (niche_id,)).fetchone() is None:
                raise LookupError("Niche not found.")
            inserted = db.execute(
                """
                INSERT INTO assets (id, niche_id, type, name, description, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (niche_id, id) DO NOTHING
                """,
                (asset_id, niche_id, kind, name, description, json.dumps(metadata)),
            )
            if not inserted.rowcount:
                raise ValueError("An asset with this ID already exists in this niche.")
        return asset_id

    def add_reference(self, niche_id, asset_id, angle, variant_id, primary, image_bytes, upload_id=""):
        _valid_id(niche_id, "Niche ID")
        _valid_id(asset_id, "Asset ID")
        variant_id = variant_id or ""
        _validate_reference_fields(angle, variant_id, primary)
        if upload_id != "" and (not isinstance(upload_id, str) or not re.fullmatch(r"ref_[a-f0-9]{32}", upload_id)):
            raise ValueError("Invalid reference upload ID.")
        extension = _image_extension(image_bytes)
        reference_id = upload_id or f"ref_{uuid4().hex}"
        written = None
        try:
            with self.store._connection(write=True) as db:
                asset = self._asset_record(self._asset_row(db, niche_id, asset_id))
                references = asset["references"]
                if upload_id and any(entry["id"] == reference_id for entry in references):
                    return reference_id
                if variant_id and not any(variant["id"] == variant_id for variant in asset["variants"]):
                    raise ValueError("Add this variant before uploading its references.")
                group = [entry for entry in references if entry["variant_id"] == variant_id]
                if primary or not any(entry["primary"] for entry in group):
                    primary = True
                    for entry in group:
                        entry["primary"] = False
                directory = self._asset_directory(niche_id, asset_id)
                written = _write_upload(directory, f"{reference_id}{extension}", image_bytes)
                references.append({
                    "id": reference_id, "path": written.relative_to(self.root.resolve()).as_posix(),
                    "source": "upload", "angle": angle, "variant_id": variant_id, "primary": primary,
                })
                db.execute(
                    "UPDATE assets SET reference_images_json = ? WHERE niche_id = ? AND id = ?",
                    (json.dumps(references), niche_id, asset_id),
                )
        except BaseException:
            if written is not None:
                written.unlink(missing_ok=True)
            raise
        return reference_id

    def update_reference(self, niche_id, asset_id, reference_id, angle, variant_id, primary):
        _valid_id(niche_id, "Niche ID")
        _valid_id(asset_id, "Character ID")
        _valid_id(reference_id, "Reference ID")
        variant_id = variant_id or ""
        _validate_reference_fields(angle, variant_id, primary)
        with self.store._connection(write=True) as db:
            asset = self._asset_record(self._character_row(db, niche_id, asset_id))
            if variant_id and not any(item["id"] == variant_id for item in asset["variants"]):
                raise ValueError("Add this variant before assigning its references.")
            references = asset["references"]
            reference = next((item for item in references if item["id"] == reference_id), None)
            if reference is None:
                raise LookupError("Reference image not found.")
            previous_variant = reference["variant_id"]
            reference.update(angle=angle, variant_id=variant_id, primary=primary)
            for group_id in dict.fromkeys((previous_variant, variant_id)):
                group = [item for item in references if item["variant_id"] == group_id]
                if group_id == variant_id and primary:
                    for item in group:
                        item["primary"] = item is reference
                elif group and not any(item["primary"] for item in group):
                    replacement = next((item for item in group if item is not reference), group[0])
                    replacement["primary"] = True
            db.execute(
                "UPDATE assets SET reference_images_json = ? WHERE niche_id = ? AND id = ?",
                (json.dumps(references), niche_id, asset_id),
            )

    def save_character_audio(self, niche_id, asset_id, url="", audio_bytes=None):
        self.get_character(niche_id, asset_id)
        if url and audio_bytes is not None:
            raise ValueError("Choose either an audio file or an audio link.")
        audio = {"source": "link", "url": _audio_url(url)} if url != "" else None
        directory = self._asset_directory(niche_id, asset_id)
        written = None
        try:
            if audio_bytes is not None:
                filename = save_audio_upload(directory, audio_bytes)
                written = directory / filename
                audio = {"source": "upload", "path": filename}
            with self.store._connection(write=True) as db:
                row = self._character_row(db, niche_id, asset_id)
                metadata = json.loads(row["metadata_json"])
                previous = _local_audio_file(directory, metadata.get("audio"))
                metadata["audio"] = audio
                db.execute(
                    "UPDATE assets SET metadata_json = ? WHERE niche_id = ? AND id = ?",
                    (json.dumps(metadata), niche_id, asset_id),
                )
        except BaseException:
            if written is not None:
                with suppress(OSError):
                    written.unlink(missing_ok=True)
            raise
        if previous is not None:
            with suppress(OSError):
                previous.unlink(missing_ok=True)

    def character_audio_path(self, niche_id, asset_id):
        asset = self.get_character(niche_id, asset_id)
        directory = self._asset_directory(niche_id, asset_id)
        audio = _local_audio_file(directory, asset["audio"])
        if audio is not None:
            path = audio.resolve()
            if path.is_relative_to(directory) and path.is_file():
                return path
        raise LookupError("Character audio file not found.")

    def add_variant(self, niche_id, asset_id, variant_id, name, description, dress_color=""):
        _valid_id(niche_id, "Niche ID")
        _valid_id(asset_id, "Asset ID")
        _valid_id(variant_id, "Variant ID")
        name = _text(name, "Variant name", 100)
        description = _text(description, "Variant description", 2000)
        dress_color = _dress_color(dress_color)
        with self.store._connection(write=True) as db:
            row = self._asset_row(db, niche_id, asset_id)
            if dress_color and row["type"] != "character":
                raise ValueError("Dress colors belong to characters.")
            metadata = json.loads(row["metadata_json"])
            variants = metadata.setdefault("variants", [])
            if any(entry["id"] == variant_id for entry in variants):
                raise ValueError("A variant with this ID already exists for this asset.")
            variant = {"id": variant_id, "name": name, "description": description}
            if dress_color:
                variant["dress_color"] = dress_color
            variants.append(variant)
            db.execute(
                "UPDATE assets SET metadata_json = ? WHERE niche_id = ? AND id = ?",
                (json.dumps(metadata), niche_id, asset_id),
            )
        return variant_id

    def save_voice_style(self, niche_id, asset_id, style_id, name, voice, speed, is_default, expressiveness=None):
        _valid_id(niche_id, "Niche ID")
        _valid_id(asset_id, "Asset ID")
        _valid_id(style_id, "Style ID")
        name = _text(name, "Style name", 100)
        voice = _text(voice, "Provider voice ID or name", 100)
        speed = _number(speed, "Voice speed", 0.7, 1.2)
        if expressiveness not in (None, ""):
            expressiveness = _number(expressiveness, "Voice expressiveness", 0, 1)
        else:
            expressiveness = None
        if type(is_default) is not bool:
            raise ValueError("Choose whether the voice style is the default.")
        style = {
            "id": style_id, "name": name, "voice": voice, "speed": speed,
            "is_default": is_default, "expressiveness": expressiveness,
        }
        with self.store._connection(write=True) as db:
            row = self._asset_row(db, niche_id, asset_id)
            if row["type"] != "character":
                raise ValueError("Voice styles belong to characters.")
            metadata = json.loads(row["metadata_json"])
            styles = [entry for entry in metadata.get("voice_styles", []) if entry["id"] != style_id]
            if is_default:
                for entry in styles:
                    entry["is_default"] = False
            metadata["voice_styles"] = [*styles, style]
            db.execute(
                "UPDATE assets SET metadata_json = ? WHERE niche_id = ? AND id = ?",
                (json.dumps(metadata), niche_id, asset_id),
            )
        return style_id

    def reference_path(self, reference):
        if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
            raise ValueError("Invalid asset reference.")
        relative = Path(reference["path"])
        if reference.get("source") == "bundled":
            base = STUDIO_ROOT
            root = (STUDIO_ROOT / "niches").resolve()
            extensions = (".png", ".jpg", ".jpeg", ".svg")
        elif reference.get("source") == "upload":
            base = root = self.root.resolve()
            extensions = (".png", ".jpg")
        else:
            raise ValueError("Invalid asset reference.")
        path = (base / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root) or path.suffix.lower() not in extensions or not path.is_file():
            raise ValueError("The asset reference is missing or outside Studio storage.")
        return path
