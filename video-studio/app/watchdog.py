"""Read the current recorded Studio issues without polling external services."""

import re

from app.settings import PROVIDER_WARNING_MESSAGES, provider_blockers


_LIMIT_PER_SOURCE = 25
_ISSUE_QUERIES = {
    "idea": """
        SELECT id, 'Idea generation' AS source, status, error, NULL AS attempts,
            'Idea request #' || id AS item, NULL AS job_id
        FROM idea_generation_requests
    """,
    "job": """
        SELECT id, CASE WHEN plan_json IS NULL THEN 'Scene planning' ELSE 'Video production' END AS source,
            status, stage_error AS error, CASE WHEN plan_json IS NULL THEN plan_attempts END AS attempts,
            'Video job #' || id AS item, id AS job_id
        FROM jobs
    """,
    "media": """
        SELECT id, 'Media production' AS source, status, error, attempts,
            kind || ' task #' || id || ' · video job #' || job_id AS item, job_id, scene_id
        FROM media_tasks
    """,
    "publish": """
        SELECT id, 'Publishing' AS source, status, error, NULL AS attempts,
            identifier || ' request #' || id || ' · video job #' || job_id AS item, job_id
        FROM publishing_requests
    """,
    "analytics": """
        SELECT id, 'Publishing analytics' AS source, 'error' AS status, analytics_error AS error,
            NULL AS attempts, identifier || ' request #' || id || ' · video job #' || job_id AS item, job_id
        FROM publishing_requests
    """,
}

# Stored errors can include provider output, local paths, or subprocess stderr.
# Only known application messages and constrained HTTP codes are safe to repeat here.
_BLOCK_REASONS = {
    "Configure an eligible provider and budget in Settings.",
    "Daily budget is unset or has insufficient funds.",
    "Enable a Scripting provider with vision for visual QA.",
    "Enable Claude and add an API key in Settings.",
    "Enable Postiz and add its API key in Settings.",
    "Per-video budget is unset or has insufficient funds.",
    "Restore the key for the submitted provider to resume polling.",
    "Set Claude's estimated cost per request in Settings.",
    PROVIDER_WARNING_MESSAGES["access_denied"],
    PROVIDER_WARNING_MESSAGES["invalid_credential"],
}
_SAFE_REASONS = _BLOCK_REASONS | set(PROVIDER_WARNING_MESSAGES.values()) | {
    "Claude returned invalid visual QA results.",
    "Claude returned an idea without a title or prompt.",
    "Claude must return between one and ten ideas.",
    "Install ffmpeg and make it available on PATH",
    "Install ffprobe and make it available on PATH",
    "Install faster-whisper to generate local narration subtitles",
    "Local Whisper could not align the narration. Check the model and audio file",
    "Postiz analytics are temporarily unavailable.",
    "Postiz denied access. Check the API key and channel permissions.",
    "Postiz did not confirm creation. Check Postiz before retrying.",
    "Postiz did not confirm the post ID. Check Postiz before retrying.",
    "Postiz did not return a usable response.",
    "Postiz rate limit reached. Try again later.",
    "Provider connection failed.",
    "Provider returned an unexpected response.",
    "Provider returned invalid JSON.",
    "Provider submission was interrupted. Check its status before starting a new attempt.",
    "Publishing was interrupted. Check Postiz for the post before trying again.",
    "Set STUDIO_WHISPER_MODEL_PATH to an existing local faster-whisper model directory",
    "Studio restarted during a provider call. Review its status before resubmitting.",
    "Studio restarted during visual QA. Review the saved preview.",
    "The approved preview changed before publishing.",
    "The approved preview file is missing.",
    "The approved publication time passed before Postiz handoff.",
}
_HTTP_REASON = re.compile(
    r"(?:Provider service unavailable|Provider rejected the request|Postiz rejected the request|"
    r"Provider media download returned) (?:\(HTTP [1-5][0-9]{2}\)|HTTP [1-5][0-9]{2})\."
)
_ROUTE_REASON = re.compile(
    r"(?:Configure an eligible (?:keyframe|video|voice) route and rate in Settings\.|"
    r"No provider accepted this (?:keyframe|video|voice) request\. "
    r"Check provider access, account credit, or rate limits in Providers\.)"
)
_FALLBACK_REASONS = {
    "Idea generation": "The idea generation request recorded an error.",
    "Scene planning": "The scene planner recorded an error. Review the video job.",
    "Video production": "Video production recorded an error. Review the video job.",
    "Media production": "The media task recorded an error. Review the affected video job.",
    "Publishing": "The publishing request recorded an error. Review the affected video job.",
    "Publishing analytics": "The analytics check recorded an error.",
}


def _reason(error, source):
    message = error.strip()
    if message in _SAFE_REASONS or _HTTP_REASON.fullmatch(message) or _ROUTE_REASON.fullmatch(message):
        return message
    if source == "Video production":
        if message.startswith("ffmpeg failed."):
            return "FFmpeg could not render the video."
        if message.startswith("ffprobe failed."):
            return "FFprobe could not inspect the media."
    return _FALLBACK_REASONS[source]


def _issue_from_row(prefix, row):
    job_id = row["job_id"]
    item = row["item"]
    if prefix == "media" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", row["scene_id"]):
        item += f" · scene {row['scene_id']}"
    reason = _reason(row["error"], row["source"])
    status = row["status"]
    if status != "needs_review" and (reason in _BLOCK_REASONS or _ROUTE_REASON.fullmatch(reason)):
        status = "blocked"
    return {
        "id": f"{prefix}-{row['id']}",
        "source": row["source"],
        "item": item,
        "status": status,
        "reason": reason,
        "attempts": row["attempts"],
        "occurred_at": None,
        "origin_url": f"/workspace?job={job_id}" if job_id is not None else "/",
        "origin_label": "Open Video Workspace" if job_id is not None else "Open Idea Inbox",
    }


def _provider_issues(settings):
    for provider in settings.list_providers():
        blockers = provider_blockers(provider)
        if "disabled" in blockers:
            continue
        reasons = []
        if "needs_key" in blockers:
            reasons.append("An API key is required.")
        if "needs_rate" in blockers:
            reasons.append("An estimated rate greater than zero is required.")
        warning = PROVIDER_WARNING_MESSAGES.get(provider.get("warning_code"))
        if warning:
            reasons.append(warning)
        if reasons:
            yield {
                "id": f"provider-{provider['name']}",
                "source": "Provider runtime" if warning else "Provider configuration",
                "item": provider["name"],
                "status": "warning",
                "reason": " ".join(reasons),
                "attempts": None,
                "occurred_at": provider.get("warning_at") if warning else None,
                "origin_url": "/providers",
                "origin_label": "Open Providers",
            }


def _stored_issues(db, query, *, record_id=None):
    sql = f"SELECT * FROM ({query}) WHERE trim(error) <> ''"
    if record_id is not None:
        return db.execute(sql + " AND id = ?", (record_id,))
    return db.execute(sql + " ORDER BY id DESC LIMIT ?", (_LIMIT_PER_SOURCE,))


def list_issues(store, settings):
    issues = list(_provider_issues(settings))
    with store._connection() as db:
        for prefix, query in _ISSUE_QUERIES.items():
            issues.extend(_issue_from_row(prefix, row) for row in _stored_issues(db, query))
    return issues


def get_issue(store, settings, issue_id):
    prefix, _, record_id = issue_id.partition("-")
    if prefix == "provider":
        return next((issue for issue in _provider_issues(settings) if issue["id"] == issue_id), None)
    query = _ISSUE_QUERIES.get(prefix)
    if query is None or not re.fullmatch(r"[1-9][0-9]{0,18}", record_id):
        return None
    record_id = int(record_id)
    if record_id > 2**63 - 1:
        return None
    with store._connection() as db:
        row = _stored_issues(db, query, record_id=record_id).fetchone()
    return _issue_from_row(prefix, row) if row is not None else None
