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

    def _write_lines(self, lines):
        self.env_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp_path = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.env_path.parent, delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                handle.writelines(f"{line}\n" for line in lines)
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.replace(self.env_path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    @staticmethod
    def _enable_defaults(db, key_env):
        db.execute(
            """
            UPDATE providers
            SET enabled = CASE WHEN name = 'fal-kling' THEN 0 ELSE 1 END,
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
                    SELECT name, type, key_env, enabled, priority, billing_unit,
                        rate_micros, capabilities_json
                    FROM providers ORDER BY type, priority, name
                    """
                )
            ]
        keys = _key_values(self._read_lines())
        for provider in providers:
            provider["enabled"] = bool(provider["enabled"])
            provider["capabilities"] = json.loads(provider.pop("capabilities_json"))
            provider["configured"] = provider["key_env"] in keys
        return providers

    @property
    def platforms_path(self):
        return self.env_path.with_name("platforms.json")

    def list_platforms(self):
        saved = None
        custom = {}
        categories = {}
        if self.platforms_path.exists():
            try:
                data = json.loads(self.platforms_path.read_text(encoding="utf-8"))
                saved = data.get("active")
                custom = data.get("custom", {})
                categories = data.get("categories", {})
            except Exception:
                saved = None
        if saved is None:
            saved = list(DEFAULT_ACTIVE_PLATFORMS)

        configured_keys = _key_values(self._read_lines())
        for k in configured_keys:
            if k not in saved:
                saved.append(k)

        for k in custom:
            KEY_NAMES.add(k)

        return saved, custom, categories

    def add_platform(self, key_name, name=None, slug=None):
        key_name = key_name.strip().upper()
        if not re.fullmatch(r"[A-Z0-9_]{3,64}", key_name):
            raise ValueError("Platform key must be 3-64 uppercase letters, numbers, or underscores.")
        active, custom, categories = self.list_platforms()
        if key_name not in active:
            active.append(key_name)
        if key_name not in KNOWN_PLATFORMS and key_name not in custom:
            custom[key_name] = {
                "name": name or key_name,
                "slug": slug or key_name.lower().replace("_api_key", "").replace("_key", ""),
                "active": ["scripting"],
                "supported": ["scripting", "character", "animation", "audio", "publishing"],
            }
        KEY_NAMES.add(key_name)
        self.platforms_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.platforms_path.write_text(json.dumps({"active": active, "custom": custom, "categories": categories}, indent=2), encoding="utf-8")
        return key_name

    def remove_platform(self, key_name):
        key_name = key_name.strip().upper()
        active, custom, categories = self.list_platforms()
        if key_name in active:
            active.remove(key_name)
        if key_name in categories:
            del categories[key_name]
        self.platforms_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.platforms_path.write_text(json.dumps({"active": active, "custom": custom, "categories": categories}, indent=2), encoding="utf-8")
        if key_name in KEY_NAMES:
            self.set_key(key_name, None)

    def update_platform_categories(self, key_name, categories):
        key_name = key_name.strip().upper()
        active, custom, current_categories = self.list_platforms()
        valid_cats = {"scripting", "character", "animation", "audio", "publishing"}
        platform_meta = KNOWN_PLATFORMS.get(key_name) or custom.get(key_name, {})
        supported = set(platform_meta.get("supported", valid_cats))
        if isinstance(categories, str):
            raw = [c.strip().lower() for c in categories.split(",")]
        else:
            raw = [c.strip().lower() for c in categories]
        cleaned = [c for c in raw if c in valid_cats and c in supported]
        current_categories[key_name] = cleaned
        self.platforms_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.platforms_path.write_text(json.dumps({
            "active": active,
            "custom": custom,
            "categories": current_categories
        }, indent=2), encoding="utf-8")
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
            lines = [
                line for line in self._read_lines()
                if line.partition("=")[0].strip() != key_env
            ]
            if value:
                lines.append(f"{key_env}={json.dumps(value)}")
                self._enable_defaults(db, key_env)
            self._write_lines(lines)

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

    def set_budgets(self, daily_micros, video_micros):
        _check_whole_number(daily_micros, "Daily budget")
        _check_whole_number(video_micros, "Per-video budget")
        with self.store._connection(write=True) as db:
            db.execute(
                "UPDATE budget_settings SET daily_micros = ?, video_micros = ? WHERE id = 1",
                (daily_micros, video_micros),
            )

    def reserve(self, job_id, provider_name, amount_micros, now):
        """Reserve estimated micros of USD before sending a paid request."""
        _check_whole_number(amount_micros, "Reservation", minimum=1)
        if now.utcoffset() is None:
            raise ValueError("Use a timezone-aware budget time.")
        day = now.astimezone(ZoneInfo(BUDGET_TIMEZONE)).date().isoformat()
        with self.store._connection(write=True) as db:
            if db.execute("SELECT 1 FROM providers WHERE name = ?", (provider_name,)).fetchone() is None:
                raise LookupError("Provider not found.")
            if job_id is not None and db.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
            ).fetchone() is None:
                raise LookupError("Video job not found.")
            budgets = db.execute("SELECT daily_micros, video_micros FROM budget_settings").fetchone()
            daily_used = sum(
                row[0] for row in db.execute(
                    """
                    SELECT amount_micros FROM spend_reservations
                    WHERE budget_day = ? AND status <> 'released'
                    """,
                    (day,),
                )
            )
            if daily_used + amount_micros > budgets["daily_micros"]:
                raise ValueError("Daily budget is unset or has insufficient funds.")
            if job_id is not None:
                video_used = sum(
                    row[0] for row in db.execute(
                        """
                        SELECT amount_micros FROM spend_reservations
                        WHERE job_id = ? AND status <> 'released'
                        """,
                        (job_id,),
                    )
                )
                if video_used + amount_micros > budgets["video_micros"]:
                    raise ValueError("Per-video budget is unset or has insufficient funds.")
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
