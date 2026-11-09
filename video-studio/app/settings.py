"""Provider registry, local credentials, and spend limits."""

import json
import os
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
KEY_NAMES = {provider[2] for provider in DEFAULT_PROVIDERS}


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
