"""Niche presets and the assets resolved for a production job."""

import json
import re
import sqlite3

from app.asset_library import AssetLibrary, save_audio_upload
from app.niche import load_profile


PRESET_KINDS = ("visual", "script", "subtitle", "audio")
FONTS = ("DejaVu Sans", "DejaVu Serif", "DejaVu Sans Mono")


def _required(value, label, limit=100):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError(f"Enter {label} in 1–{limit} characters.")
    return value.strip()


def _subtitle_config(fields):
    font = fields.get("font_name")
    size = fields.get("font_size", "")
    position = fields.get("position")
    colors = {name: fields.get(name, "") for name in ("text_color", "outline_color")}
    if font not in FONTS or not re.fullmatch(r"[0-9]{1,2}", size) or not 8 <= int(size) <= 30:
        raise ValueError("Choose a supported subtitle font and a size from 8 to 30.")
    if position not in ("top", "middle", "bottom") or any(
        not re.fullmatch(r"#[0-9a-fA-F]{6}", value) for value in colors.values()
    ):
        raise ValueError("Choose a subtitle position and valid text and outline colors.")
    return {"font_name": font, "font_size": int(size), "position": position, **colors}


class StudioLibrary:
    def __init__(self, store):
        self.store = store
        self.assets = AssetLibrary(store)
        self.audio_root = store.path.parent / "assets" / "_audio"

    def presets(self, niche_id="kids_3d"):
        with self.store._connection() as db:
            return [dict(row, config=json.loads(row["config_json"])) for row in db.execute(
                """SELECT kind, id, name, is_default, config_json FROM production_presets
                    WHERE niche_id = ? ORDER BY kind, is_default DESC, name, id""", (niche_id,),
            )]

    def add_preset(self, niche_id, kind, preset_id, name, fields, files, is_default):
        if kind not in PRESET_KINDS or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", preset_id):
            raise ValueError("Use a preset ID starting with a lowercase letter, with letters, digits or underscores.")
        name = _required(name, "a preset name")
        if kind == "visual":
            config = {"style": _required(fields.get("style"), "a visual style", 1000),
                      "palette": _required(fields.get("palette"), "a palette", 300)}
        elif kind == "script":
            config = {"tone": _required(fields.get("tone"), "a script tone", 1000)}
        elif kind == "subtitle":
            config = _subtitle_config(fields)
        else:
            if not any(files.get(field) for field in ("music", "sfx")):
                raise ValueError("Upload a music or sound effect file for this preset.")
            config = {}

        saved = []
        try:
            if kind == "audio":
                # The niche must exist before a directory can be created for it.
                load_profile(niche_id)
                for field in ("music", "sfx"):
                    if files.get(field):
                        filename = save_audio_upload(self.audio_root / niche_id, files[field])
                        saved.append(self.audio_root / niche_id / filename)
                        config[field] = filename
            with self.store._connection(write=True) as db:
                if is_default:
                    self._clear_default(db, niche_id, kind)
                db.execute(
                    "INSERT INTO production_presets VALUES (?, ?, ?, ?, ?, ?)",
                    (niche_id, kind, preset_id, name, json.dumps(config), int(is_default)),
                )
        except (OSError, ValueError, sqlite3.Error) as error:
            for path in saved:
                path.unlink(missing_ok=True)
            if isinstance(error, sqlite3.IntegrityError):
                raise ValueError("This preset ID is already in use for that type.") from None
            raise

    @staticmethod
    def _clear_default(db, niche_id, kind):
        db.execute("UPDATE production_presets SET is_default = 0 WHERE niche_id = ? AND kind = ?", (niche_id, kind))

    def set_default(self, niche_id, kind, preset_id):
        with self.store._connection(write=True) as db:
            self._clear_default(db, niche_id, kind)
            changed = db.execute(
                "UPDATE production_presets SET is_default = 1 WHERE niche_id = ? AND kind = ? AND id = ?",
                (niche_id, kind, preset_id),
            )
            if not changed.rowcount:
                raise LookupError("Preset not found.")

    def validate_options(self, niche_id, options):
        presets = {(preset["kind"], preset["id"]) for preset in self.presets(niche_id)}
        assets = {asset["id"]: asset for asset in self.assets.list_assets(niche_id)}
        for kind, preset_id in options["presets"].items():
            if (kind, preset_id) not in presets:
                raise ValueError("Choose a saved production preset.")
        for group, field in (("variants", "variants"), ("voices", "voice_styles")):
            for asset_id, selection in options[group].items():
                if not any(choice["id"] == selection for choice in assets.get(asset_id, {}).get(field, [])):
                    raise ValueError("Choose a saved variant or voice style for that asset.")

    def profile(self, niche_id="kids_3d", options=None):
        options = options or {}
        profile = load_profile(niche_id)
        catalog = {asset["id"]: asset for asset in profile["assets"]}
        for asset in self.assets.list_assets(niche_id):
            if not asset["references"]:
                continue
            variant_id = options.get("variants", {}).get(asset["id"])
            variant = next((item for item in asset["variants"] if item["id"] == variant_id), None)
            base = [ref for ref in asset["references"] if not ref["variant_id"]]
            custom = [ref for ref in asset["references"] if variant and ref["variant_id"] == variant_id]
            references = custom or base
            if not references:
                continue
            voice_id = options.get("voices", {}).get(asset["id"])
            voice = next((item for item in asset["voice_styles"] if item["id"] == voice_id), None)
            if voice is None:
                voice = next((item for item in asset["voice_styles"] if item["is_default"]), None)
            description = asset["description"]
            if variant:
                description += f" Selected appearance {variant['name']}. {variant['description']}"
            catalog[asset["id"]] = {
                "id": asset["id"], "kind": asset["kind"], "name": asset["name"],
                "description": description, "references": references, "voice_styles": asset["voice_styles"],
                "selected_voice": voice, "voice_override": bool(voice_id),
            }
        profile["assets"] = list(catalog.values())

        selected = {}
        for preset in self.presets(niche_id):
            preferred = options.get("presets", {}).get(preset["kind"])
            if (preferred and preferred == preset["id"]) or (not preferred and preset["is_default"]):
                selected[preset["kind"]] = preset["config"]
        if "visual" in selected:
            visual = selected["visual"]
            profile["style_guide"] += f" Visual treatment: {visual['style']}. Color palette: {visual['palette']}."
        if "script" in selected:
            profile["script_style"] = selected["script"]["tone"]
        profile["render_presets"] = {kind: selected[kind] for kind in ("subtitle", "audio") if kind in selected}
        return profile

    def audio_path(self, niche_id, filename):
        if not isinstance(filename, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+\.(mp3|wav)", filename):
            raise ValueError("The audio preset contains an invalid file name.")
        directory = self.audio_root.resolve()
        path = (directory / niche_id / filename).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            raise ValueError("An audio preset file is missing from the local data directory.")
        return path


def select_references(asset, camera):
    if "references" not in asset:
        return [{"path": asset["reference_image"], "source": "bundled", "angle": "front"}]
    references = asset["references"]
    primary = next((ref for ref in references if ref["primary"]), references[0])
    camera = camera.lower()
    angle = "front"
    for target, terms in (
        ("back", r"\b(?:behind|back view|rear view)\b"),
        ("three-quarter", r"\b(?:three[- ]quarter|3/4)\b"),
        ("side", r"\b(?:side[- ]view|side angle|side shot|side[- ]on|profile)\b"),
    ):
        if re.search(terms, camera):
            angle = target
            break
    other = next((ref for ref in references if ref["angle"] == angle and ref != primary), None)
    return [primary, other] if other is not None else [primary]
