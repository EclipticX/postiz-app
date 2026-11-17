"""Provider registry, local credentials, and spend limits."""

import json
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo


BUDGET_TIMEZONE = "Asia/Dhaka"
DEFAULT_PROVIDERS = (
    ("claude-sonnet", "llm", "ANTHROPIC_API_KEY", 10, "request", {"vision": True}),
    (
        "fal-nano-banana-pro", "image", "FAL_KEY", 10, "image",
        {"reference_image": True, "aspect_ratios": ["9:16"]},
    ),
    (
        "fal-veo", "video", "FAL_KEY", 10, "second",
        {
            "image_to_video": True,
            "reference_image": True,
            "audio": True,
            "durations": [4, 6, 8],
            "aspect_ratios": ["9:16", "16:9"],
            "resolutions": ["720p", "1080p", "4k"],
            "max_resolution": "4k",
        },
    ),
    (
        "evolink-seedance", "video", "EVOLINK_API_KEY", 20, "second",
        {
            "image_to_video": True,
            "reference_image": True,
            "audio": True,
            "durations": list(range(4, 16)),
            "aspect_ratios": ["9:16", "16:9"],
            "resolutions": ["480p", "720p"],
            "max_resolution": "720p",
        },
    ),
    (
        "fal-kling", "video", "FAL_KEY", 30, "second",
        {
            "image_to_video": True,
            "reference_image": True,
            "audio": False,
            "durations": list(range(3, 21)),
            "aspect_ratios": ["9:16", "16:9"],
            "resolutions": ["720p"],
            "max_resolution": "720p",
        },
    ),
    (
        "fal-seedance-25-flash", "video", "FAL_KEY", 40, "second",
        {"direct": True, "audio": True, "reference_audio": True, "reference_image": True,
         "durations": list(range(4, 31)), "resolutions": ["480p", "720p"],
         "aspect_ratios": ["9:16", "16:9", "1:1", "4:3", "3:4", "21:9"]},
    ),
    ("fal-elevenlabs", "tts", "FAL_KEY", 10, "1000_characters", {"audio": True}),
    ("postiz", "publishing", "POSTIZ_API_KEY", 10, "request", {}),
)

DEFAULT_ACTIVE_PLATFORMS = ("FAL_KEY", "ANTHROPIC_API_KEY", "EVOLINK_API_KEY", "POSTIZ_API_KEY", "ELEVENLABS_API_KEY")

KNOWN_PLATFORMS = {
    "FAL_KEY": {
        "name": "Fal.ai (FAL_KEY)",
        "slug": "fal",
        "active": ["character", "animation", "audio"],
        "supported": ["character", "animation", "audio"],
        "models": [
            ("Character model", [
                ("", "Select model...", ""),
                ("fal-ai/nano-banana-pro", "fal-ai/nano-banana-pro", "Ultra-fast character & asset generation"),
                ("fal-ai/flux-pro/v1.1-ultra", "fal-ai/flux-pro/v1.1-ultra", "High fidelity photorealistic scenes"),
                ("fal-ai/recraft-v3", "fal-ai/recraft-v3", "Vector art & graphic asset design"),
            ]),
            ("Animation model", [
                ("", "Select model...", ""),
                ("fal-ai/veo3.1/image-to-video", "fal-ai/veo3.1/image-to-video", "Deep cinematic video generation"),
                ("fal-ai/kling-video/v1.5/pro", "fal-ai/kling-video/v1.5/pro", "Smooth motion video generation"),
                ("fal-ai/luma-dream-machine", "fal-ai/luma-dream-machine", "Fast realistic dynamic video"),
            ]),
            ("Audio model", [
                ("", "Select model...", ""),
                ("fal-ai/elevenlabs/tts", "fal-ai/elevenlabs/tts", "High quality character dialogue voice"),
            ]),
        ],
    },
    "ANTHROPIC_API_KEY": {
        "name": "Anthropic (Claude) (ANTHROPIC_API_KEY)",
        "slug": "anthropic",
        "active": ["scripting"],
        "supported": ["scripting"],
        "models": [
            ("Scripting model", [
                ("", "Select model...", ""),
                ("claude-fable-5-2", "claude-fable-5-2", "Next generation of intelligence for the hardest tasks"),
                ("claude-haiku-5-5", "claude-haiku-5-5", "Fastest model for daily tasks"),
                ("claude-sonnet-5-5", "claude-sonnet-5-5", "Smart, efficient model for everyday use"),
                ("claude-opus-5-5", "claude-opus-5-5", "Powerful, large model for complex challenges"),
                ("claude-fable-5-1", "claude-fable-5-1", "Claude Fable 5.1"),
                ("claude-opus-5", "claude-opus-5", "Claude Opus 5"),
                ("claude-3-7-sonnet-20250219", "Claude 3.7 Sonnet", "Hybrid reasoning flagship model"),
                ("claude-3-5-sonnet-20241022", "Claude 3.5 Sonnet", "Coding and analysis flagship"),
                ("claude-3-5-haiku-20241022", "Claude 3.5 Haiku", "Fast, lightweight daily tasks"),
            ]),
        ],
    },
    "EVOLINK_API_KEY": {
        "name": "EvoLink (Seedance) (EVOLINK_API_KEY)",
        "slug": "evolink",
        "active": ["animation"],
        "supported": ["animation"],
        "models": [
            ("Animation model", [
                ("", "Select model...", ""),
                ("seedance-2.0-image-to-video", "seedance-2.0-image-to-video", "Seedance 2.0 image to video animation"),
                ("seedance-1.5-pro", "seedance-1.5-pro", "Seedance 1.5 Pro animation"),
            ]),
        ],
    },
    "POSTIZ_API_KEY": {
        "name": "Postiz (POSTIZ_API_KEY)",
        "slug": "postiz",
        "active": ["publishing"],
        "supported": ["publishing"],
        "models": [],
    },
    "GEMINI_API_KEY": {
        "name": "Google (Gemini, Veo, Imagen) (GEMINI_API_KEY)",
        "slug": "google",
        "active": ["scripting", "character", "animation"],
        "supported": ["scripting", "character", "animation"],
        "models": [
            ("Scripting model", [
                ("", "Select model...", ""),
                ("gemini-2.0-flash", "gemini-2.0-flash", "Next-gen multimodal speed & reasoning"),
                ("gemini-1.5-pro", "gemini-1.5-pro", "Long-context reasoning & analysis"),
            ]),
            ("Character model", [
                ("", "Select model...", ""),
                ("imagen-3.0-generate-002", "Imagen 3", "Photorealistic character & asset generation"),
            ]),
            ("Animation model", [
                ("", "Select model...", ""),
                ("veo-2.0-generate-video", "veo-2.0-generate-video", "Google Veo 2.0 video generation"),
            ]),
        ],
    },
    "OPENAI_API_KEY": {
        "name": "OpenAI (GPT-4o, DALL-E, Sora) (OPENAI_API_KEY)",
        "slug": "openai",
        "active": ["scripting", "character", "animation"],
        "supported": ["scripting", "character", "animation"],
        "models": [
            ("Scripting model", [
                ("", "Select model...", ""),
                ("gpt-4o", "gpt-4o", "Flagship omni model for text & vision"),
                ("gpt-4o-mini", "gpt-4o-mini", "Fast & lightweight everyday model"),
                ("o3-mini", "o3-mini", "High intelligence reasoning model"),
            ]),
            ("Character model", [
                ("", "Select model...", ""),
                ("dall-e-3", "DALL·E 3", "High fidelity photorealistic character generation"),
                ("dall-e-2", "DALL·E 2", "Fast general concept generation"),
            ]),
            ("Animation model", [
                ("", "Select model...", ""),
                ("sora-1.0", "sora-1.0", "OpenAI Sora video generation"),
            ]),
        ],
    },
    "REPLICATE_API_KEY": {
        "name": "Replicate (REPLICATE_API_KEY)",
        "slug": "replicate",
        "active": ["character", "animation"],
        "supported": ["character", "animation"],
        "models": [
            ("Character model", [
                ("", "Select model...", ""),
                ("black-forest-labs/flux-1.1-pro", "Flux 1.1 Pro", "High-res prompt following"),
            ]),
            ("Animation model", [
                ("", "Select model...", ""),
                ("kling-ai/kling-v1", "Kling v1", "Kling AI video generation"),
            ]),
        ],
    },
    "RUNWAY_API_KEY": {
        "name": "Runway (RUNWAY_API_KEY)",
        "slug": "runway",
        "active": ["animation"],
        "supported": ["animation"],
        "models": [
            ("Animation model", [
                ("", "Select model...", ""),
                ("gen-3-alpha-turbo", "gen-3-alpha-turbo", "Runway Gen-3 Alpha Turbo"),
            ]),
        ],
    },
    "ELEVENLABS_API_KEY": {
        "name": "ElevenLabs (ELEVENLABS_API_KEY)",
        "slug": "elevenlabs",
        "active": ["audio"],
        "supported": ["audio"],
        "models": [
            ("Audio model", [
                ("", "Select model...", ""),
                ("eleven_multilingual_v2", "eleven_multilingual_v2", "Eleven Multilingual v2"),
                ("eleven_turbo_v2_5", "eleven_turbo_v2_5", "Eleven Turbo v2.5 low-latency"),
            ]),
        ],
    },
}

KEY_NAMES = {provider[2] for provider in DEFAULT_PROVIDERS} | set(KNOWN_PLATFORMS.keys())
PROVIDER_CATEGORIES = {
    "llm": "scripting",
    "image": "character",
    "video": "animation",
    "tts": "audio",
    "publishing": "publishing",
}
PROVIDER_WARNING_MESSAGES = {
    "access_denied": "Provider denied access. Check the API key and model permissions.",
    "invalid_credential": "Configure a valid API key for this provider.",
    "insufficient_credit": "Provider account has insufficient credit.",
    "rate_limited": "Provider rate limit reached.",
}


def provider_blockers(provider):
    """Configuration checks shared by the provider list and new requests."""
    if not provider["enabled"] or not provider.get("category_enabled", True):
        return ("disabled",)
    blockers = []
    if not provider["configured"]:
        blockers.append("needs_key")
    if provider["type"] != "publishing" and provider["rate_micros"] <= 0:
        blockers.append("needs_rate")
    return tuple(blockers)


def provider_order(provider):
    denied = provider.get("warning_code") in {"access_denied", "invalid_credential"}
    return denied, provider["priority"], provider["name"]


def active_categories(key_env, metadata, overrides):
    """A saved empty list explicitly turns every category off."""
    categories = overrides.get(key_env)
    if not isinstance(categories, list):
        categories = metadata.get("active", ())
    return set(categories) & set(metadata.get("supported", ()))


def _check_whole_number(value, label, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise ValueError(f"{label} must be a whole number of at least {minimum}.")


def _check_key_name(key_env):
    if key_env not in KEY_NAMES:
        raise ValueError("Unknown Studio credential.")


def _valid_key_value(value):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 4096
        and all(char.isprintable() and not char.isspace() for char in value)
    )


def _key_values(lines):
    keys = {}
    for line in lines:
        name, separator, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not separator or name not in KEY_NAMES:
            continue
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                value = None
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        if _valid_key_value(value):
            keys[name] = value
        else:
            keys.pop(name, None)
    return keys


class StudioSettings:
    def __init__(self, store, env_path):
        self.store = store
        self.env_path = Path(env_path)
        self.list_platforms()

    def _read_lines(self):
        try:
            return self.env_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []

    def _write_lines(self, path, lines):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp_path = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                handle.writelines(f"{line}\n" for line in lines)
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.replace(path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    @staticmethod
    def _enable_defaults(db, key_env):
        db.execute(
            """
            UPDATE providers
            SET enabled = CASE WHEN name IN ('fal-kling', 'fal-seedance-25-flash') THEN 0 ELSE 1 END,
                auto_enable_pending = 0
            WHERE key_env = ? AND auto_enable_pending = 1
            """,
            (key_env,),
        )

    def seed(self):
        with self.store._connection(write=True) as db:
            db.executemany(
                """
                INSERT INTO providers
                    (name, type, key_env, priority, billing_unit, rate_micros, capabilities_json)
                VALUES (?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT (name) DO NOTHING
                """,
                [
                    (name, kind, key_env, priority, billing_unit, json.dumps(capabilities))
                    for name, kind, key_env, priority, billing_unit, capabilities in DEFAULT_PROVIDERS
                ],
            )
            for key_env in _key_values(self._read_lines()):
                self._enable_defaults(db, key_env)

    def list_providers(self):
        with self.store._connection() as db:
            providers = [
                dict(row)
                for row in db.execute(
                    """
                    SELECT p.name, p.type, p.key_env, p.enabled, p.priority, p.billing_unit,
                        p.rate_micros, p.capabilities_json, w.code AS warning_code,
                        w.occurred_at AS warning_at
                    FROM providers AS p LEFT JOIN provider_warnings AS w ON w.provider_name = p.name
                    ORDER BY p.type, p.priority, p.name
                    """
                )
            ]
        _, custom, overrides = self.list_platforms()
        keys = self.configured_keys()
        for provider in providers:
            key_env = provider["key_env"]
            metadata = KNOWN_PLATFORMS.get(key_env) or custom.get(key_env)
            category = PROVIDER_CATEGORIES.get(provider["type"])
            provider["enabled"] = bool(provider["enabled"])
            provider["capabilities"] = json.loads(provider.pop("capabilities_json"))
            provider["configured"] = key_env in keys
            provider["category_enabled"] = metadata is None or category in active_categories(key_env, metadata, overrides)
            if not provider["configured"]:
                provider["warning_code"] = None
                provider["warning_at"] = None
        return providers

    @property
    def platforms_path(self):
        return self.env_path.with_name("platforms.json")

    def list_platforms(self):
        saved = None
        custom = {}
        categories = {}
        try:
            data = json.loads(self.platforms_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            saved = data.get("active")
            if isinstance(data.get("custom"), dict):
                custom = data["custom"]
            if isinstance(data.get("categories"), dict):
                categories = data["categories"]
        if not isinstance(saved, list):
            saved = list(DEFAULT_ACTIVE_PLATFORMS)

        KEY_NAMES.update(custom)
        configured_keys = _key_values(self._read_lines())
        for k in configured_keys:
            if k not in saved:
                saved.append(k)

        return saved, custom, categories

    def _write_platforms(self, active, custom, categories):
        contents = json.dumps({"active": active, "custom": custom, "categories": categories}, indent=2)
        self._write_lines(self.platforms_path, [contents])

    def add_platform(self, key_name, name=None, slug=None):
        key_name = key_name.strip().upper()
        if not re.fullmatch(r"[A-Z0-9_]{3,64}", key_name):
            raise ValueError("Platform key must be 3-64 uppercase letters, numbers, or underscores.")
        with self.store._connection(write=True):
            active, custom, categories = self.list_platforms()
            if key_name not in active:
                active.append(key_name)
            if key_name not in KNOWN_PLATFORMS and key_name not in custom:
                custom[key_name] = {
                    "name": name or key_name,
                    "slug": slug or key_name.lower().replace("_api_key", "").replace("_key", ""),
                    "active": ["scripting"],
                    "supported": list(PROVIDER_CATEGORIES.values()),
                }
            self._write_platforms(active, custom, categories)
            KEY_NAMES.add(key_name)
        return key_name

    def remove_platform(self, key_name):
        key_name = key_name.strip().upper()
        with self.store._connection(write=True) as db:
            active, custom, categories = self.list_platforms()
            if key_name in KEY_NAMES:
                self._set_key(db, key_name, "")
            if key_name in active:
                active.remove(key_name)
            categories.pop(key_name, None)
            self._write_platforms(active, custom, categories)

    def update_platform_categories(self, key_name, categories):
        key_name = key_name.strip().upper()
        with self.store._connection(write=True):
            active, custom, current_categories = self.list_platforms()
            _check_key_name(key_name)
            valid_cats = set(PROVIDER_CATEGORIES.values())
            platform_meta = KNOWN_PLATFORMS.get(key_name) or custom.get(key_name, {})
            supported = set(platform_meta.get("supported", valid_cats)) & valid_cats
            raw = categories.split(",") if isinstance(categories, str) else categories
            normalized = (category.strip().lower() for category in raw)
            cleaned = list(dict.fromkeys(category for category in normalized if category in supported))
            current_categories[key_name] = cleaned
            self._write_platforms(active, custom, current_categories)
        return cleaned

    def configured_keys(self):
        return set(_key_values(self._read_lines()).keys())

    def key(self, key_env):
        _check_key_name(key_env)
        return _key_values(self._read_lines()).get(key_env)

    def set_key(self, key_env, value):
        _check_key_name(key_env)
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError("Enter a single-line API key.")
        value = value.strip()
        if value and not _valid_key_value(value):
            raise ValueError("Enter a single-line API key without spaces.")
        # Serialize filesystem edits across Settings instances with the same database.
        with self.store._connection(write=True) as db:
            self._set_key(db, key_env, value)

    def _set_key(self, db, key_env, value):
        lines = [line for line in self._read_lines() if line.partition("=")[0].strip() != key_env]
        if value:
            lines.append(f"{key_env}={json.dumps(value)}")
            self._enable_defaults(db, key_env)
        self._write_lines(self.env_path, lines)
        db.execute(
            "DELETE FROM provider_warnings WHERE provider_name IN (SELECT name FROM providers WHERE key_env = ?)",
            (key_env,),
        )

    def _is_current_key(self, db, provider_name, key):
        route = db.execute("SELECT key_env FROM providers WHERE name = ?", (provider_name,)).fetchone()
        return bool(route and key and self.key(route["key_env"]) == key)

    def record_provider_warning(self, provider_name, key, code, now):
        if code not in PROVIDER_WARNING_MESSAGES:
            raise ValueError("Unknown provider warning.")
        with self.store._connection(write=True) as db:
            if not self._is_current_key(db, provider_name, key):
                return
            db.execute(
                """INSERT INTO provider_warnings (provider_name, code, occurred_at) VALUES (?, ?, ?)
                ON CONFLICT (provider_name) DO UPDATE SET code = excluded.code, occurred_at = excluded.occurred_at""",
                (provider_name, code, now.isoformat()),
            )

    def clear_provider_warning(self, provider_name, key):
        with self.store._connection(write=True) as db:
            if self._is_current_key(db, provider_name, key):
                db.execute("DELETE FROM provider_warnings WHERE provider_name = ?", (provider_name,))

    def update_provider(self, name, enabled, priority, rate_micros):
        if type(enabled) is not bool:
            raise ValueError("Choose whether the provider is enabled.")
        _check_whole_number(priority, "Priority")
        _check_whole_number(rate_micros, "Rate")
        with self.store._connection(write=True) as db:
            updated = db.execute(
                """
                UPDATE providers
                SET enabled = ?, priority = ?, rate_micros = ?, auto_enable_pending = 0
                WHERE name = ?
                """,
                (enabled, priority, rate_micros, name),
            )
            if not updated.rowcount:
                raise LookupError("Provider not found.")

    def get_budgets(self):
        with self.store._connection() as db:
            return dict(db.execute("SELECT daily_micros, video_micros FROM budget_settings").fetchone())

    def get_generation(self):
        with self.store._connection() as db:
            row = db.execute("SELECT scene_plan, separate_voice FROM video_generation_settings WHERE id = 1").fetchone()
        return {name: bool(row[name]) for name in row.keys()}

    def set_generation(self, scene_plan, separate_voice):
        if type(scene_plan) is not bool or type(separate_voice) is not bool:
            raise ValueError("Choose valid video generation settings.")
        with self.store._connection(write=True) as db:
            db.execute("UPDATE video_generation_settings SET scene_plan = ?, separate_voice = ? WHERE id = 1",
                       (scene_plan, separate_voice))

    def set_budgets(self, daily_micros, video_micros):
        _check_whole_number(daily_micros, "Daily budget")
        _check_whole_number(video_micros, "Per-video budget")
        with self.store._connection(write=True) as db:
            db.execute(
                "UPDATE budget_settings SET daily_micros = ?, video_micros = ? WHERE id = 1",
                (daily_micros, video_micros),
            )

    @staticmethod
    def _budget_day(amount_micros, now):
        _check_whole_number(amount_micros, "Reservation", minimum=1)
        if now.utcoffset() is None:
            raise ValueError("Use a timezone-aware budget time.")
        return now.astimezone(ZoneInfo(BUDGET_TIMEZONE)).date().isoformat()

    @staticmethod
    def _require_budget(db, job_id, amount_micros, day, *, daily_amount_micros=None):
        budgets = db.execute("SELECT daily_micros, video_micros FROM budget_settings").fetchone()
        daily_used = db.execute(
            """SELECT COALESCE(SUM(amount_micros), 0) FROM spend_reservations
                WHERE budget_day = ? AND status <> 'released'""", (day,),
        ).fetchone()[0]
        if daily_used + (daily_amount_micros or amount_micros) > budgets["daily_micros"]:
            raise ValueError("Daily budget is unset or has insufficient funds.")
        if job_id is not None:
            video_used = db.execute(
                """SELECT COALESCE(SUM(amount_micros), 0) FROM spend_reservations
                    WHERE job_id = ? AND status <> 'released'""", (job_id,),
            ).fetchone()[0]
            if video_used + amount_micros > budgets["video_micros"]:
                raise ValueError("Per-video budget is unset or has insufficient funds.")

    def check_budget(self, job_id, amount_micros, now, *, daily_amount_micros):
        """Check an estimate without creating a reservation or promising future funds."""
        day = self._budget_day(amount_micros, now)
        _check_whole_number(daily_amount_micros, "Daily preflight amount", minimum=1)
        with self.store._connection() as db:
            db.execute("BEGIN")
            self._require_budget(db, job_id, amount_micros, day, daily_amount_micros=daily_amount_micros)

    def reserve(self, job_id, provider_name, amount_micros, now):
        """Reserve estimated micros of USD before sending a paid request."""
        day = self._budget_day(amount_micros, now)
        with self.store._connection(write=True) as db:
            if db.execute("SELECT 1 FROM providers WHERE name = ?", (provider_name,)).fetchone() is None:
                raise LookupError("Provider not found.")
            if job_id is not None and db.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
            ).fetchone() is None:
                raise LookupError("Video job not found.")
            self._require_budget(db, job_id, amount_micros, day)
            return db.execute(
                """
                INSERT INTO spend_reservations (job_id, provider_name, budget_day, amount_micros)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, provider_name, day, amount_micros),
            ).lastrowid

    def settle(self, reservation_id, billed_micros=None):
        if billed_micros is not None:
            _check_whole_number(billed_micros, "Billed amount")
        with self.store._connection(write=True) as db:
            updated = db.execute(
                """
                UPDATE spend_reservations
                SET status = 'settled', amount_micros = COALESCE(?, amount_micros)
                WHERE id = ? AND status <> 'released'
                """,
                (billed_micros, reservation_id),
            )
            if not updated.rowcount:
                raise ValueError("This reservation is missing or has already been released.")

    def release(self, reservation_id):
        """Release a reservation only when the caller knows a request was never sent."""
        with self.store._connection(write=True) as db:
            updated = db.execute(
                """
                UPDATE spend_reservations SET status = 'released'
                WHERE id = ? AND status <> 'settled'
                """,
                (reservation_id,),
            )
            if not updated.rowcount:
                raise ValueError("This reservation is missing or has already been settled.")
