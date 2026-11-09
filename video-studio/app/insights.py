"""Normalize Postiz snapshots and build niche memory from comparable posts."""

import json
import re
from collections import defaultdict
from datetime import date


_COUNT_LABELS = ("views", "likes", "comments", "plays", "reactions")
_GENERATED_BY = "studio.postiz.analytics"
_MIN_SUPPORT = 2
_STOP_WORDS = set(
    ("and are for from have into make one our out short some that the their them then this "
     "video was were what when where with you your create show clip scene").split()
)
_SCORE_METRICS = (
    ("likes_and_comments_per_100_views", "views", ("likes", "comments")),
    ("likes_per_100_views", "views", ("likes",)),
    ("comments_per_100_views", "views", ("comments",)),
    ("reactions_per_100_plays", "plays", ("reactions",)),
)


def normalize_metrics(rows) -> dict:
    """Use the latest dated total per metric and preserve unavailable values."""
    metrics = dict.fromkeys(_COUNT_LABELS)
    # The audited per-post providers expose no retention measure. A change
    # percentage, reach, or impressions cannot substitute for one.
    metrics["retention_percent"] = None
    if not isinstance(rows, list):
        return metrics

    latest = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("label"), str):
            continue
        metric = row["label"].strip().casefold()
        if metric not in _COUNT_LABELS or not isinstance(row.get("data"), list):
            continue
        for point in row["data"]:
            if not isinstance(point, dict):
                continue
            try:
                day = date.fromisoformat(point.get("date"))
            except (TypeError, ValueError):
                continue
            if metric not in latest or day >= latest[metric]:
                latest[metric] = day
                metrics[metric] = _count(point.get("total"))
    return metrics


def update_memory(store, niche_id: str) -> list[dict]:
    """Store one evidence summary per platform, keeping manual memory intact."""
    with store._connection(write=True) as db:
        niche = db.execute("SELECT config_json FROM niches WHERE id = ?", (niche_id,)).fetchone()
        if niche is None:
            raise ValueError("Niche not found.")
        profile = _json(niche["config_json"])
        style = _text(profile.get("style_guide")) if isinstance(profile, dict) else None
        if style:
            style = style.split(". ", 1)[0][:160]

        platforms = defaultdict(list)
        for row in db.execute(
            """
            WITH latest AS (
                SELECT analytics.job_id, analytics.platform, analytics.metrics_json,
                       jobs.title, jobs.plan_json,
                       ROW_NUMBER() OVER (
                           PARTITION BY analytics.job_id, analytics.platform
                           ORDER BY julianday(analytics.collected_at) DESC, analytics.id DESC
                       ) AS rank
                FROM analytics JOIN jobs ON jobs.id = analytics.job_id
                WHERE jobs.niche_id = ?
            )
            SELECT * FROM latest WHERE rank = 1 ORDER BY platform, job_id
            """,
            (niche_id,),
        ):
            platforms[row["platform"]].append(row)

        summaries = []
        for platform, observations in platforms.items():
            summary = _summarize(platform, observations, style)
            if summary is not None:
                summaries.append(summary)

        generated = []
        for row in db.execute("SELECT id, pattern_json FROM memory WHERE niche_id = ? ORDER BY id", (niche_id,)):
            pattern = _json(row["pattern_json"])
            if isinstance(pattern, dict) and pattern.get("_generated_by") == _GENERATED_BY:
                generated.append((row["id"], pattern))

        if summaries != [pattern for _, pattern in generated]:
            db.executemany("DELETE FROM memory WHERE id = ?", [(row_id,) for row_id, _ in generated])
            db.executemany(
                "INSERT INTO memory (niche_id, pattern_json) VALUES (?, ?)",
                [(niche_id, json.dumps(summary, ensure_ascii=False, sort_keys=True)) for summary in summaries],
            )
    return summaries


def _summarize(platform, observations, style):
    scores = {row["job_id"]: _json(row["metrics_json"]) for row in observations}
    metric, counts = _comparable_counts(scores)
    if len(counts) < _MIN_SUPPORT:
        return None

    groups = defaultdict(dict)
    for row in observations:
        job_id = row["job_id"]
        if job_id not in counts:
            continue
        for dimension, value, source in _patterns(row, style):
            groups[dimension].setdefault((value, source), set()).add(job_id)

    patterns = {}
    for dimension, candidates in groups.items():
        best_key = None
        for (value, source), job_ids in candidates.items():
            if len(job_ids) < _MIN_SUPPORT:
                continue
            comparison = counts.keys() - job_ids
            score = _rate(job_ids, counts)
            comparison_score = _rate(comparison, counts) if comparison else None
            difference = score - comparison_score if comparison_score is not None else 0
            above_comparison = difference > 0 and len(comparison) >= _MIN_SUPPORT
            key = (above_comparison, difference, score, len(job_ids), value)
            if best_key is None or key > best_key:
                best_key = key
                patterns[dimension] = {
                    "value": value,
                    "source": source,
                    "score": round(score, 4),
                    "support": len(job_ids),
                    "comparison_score": round(comparison_score, 4) if comparison_score is not None else None,
                    "comparison_support": len(comparison),
                    "signal": "above_comparison" if above_comparison else "observed",
                }
    if not patterns:
        return None
    return {
        "_generated_by": _GENERATED_BY,
        "platform": platform,
        "score_metric": metric,
        "platform_support": len(counts),
        "patterns": patterns,
    }


def _comparable_counts(scores):
    best_metric, best_counts = None, {}
    for metric, exposure_key, interaction_keys in _SCORE_METRICS:
        counts = {}
        for job_id, data in scores.items():
            if not isinstance(data, dict):
                continue
            exposure = _count(data.get(exposure_key))
            interactions = [_count(data.get(key)) for key in interaction_keys]
            if exposure and all(count is not None for count in interactions):
                counts[job_id] = (sum(interactions), exposure)
        # Use the widest complete comparison set. Ties keep the earlier metric.
        if len(counts) > len(best_counts):
            best_metric, best_counts = metric, counts
    return best_metric, best_counts


def _rate(job_ids, counts):
    return 100 * sum(counts[job_id][0] for job_id in job_ids) / sum(counts[job_id][1] for job_id in job_ids)


def _patterns(row, style):
    plan = _json(row["plan_json"])
    metadata = plan if isinstance(plan, dict) else {}
    scenes = metadata.get("scenes", []) if metadata else plan

    topic = _text(metadata.get("topic"))
    if topic:
        yield "topic", topic, "plan_topic"
    else:
        words = set(re.findall(r"[^\W\d_]{3,}", row["title"].casefold())) - _STOP_WORDS
        for word in sorted(words):
            yield "topic", word, "title_keyword"

    hook = _text(metadata.get("hook"))
    if hook:
        yield "hook", hook, "plan_hook"
    if isinstance(scenes, list) and scenes and all(isinstance(scene, dict) for scene in scenes):
        dialogue = scenes[0].get("dialogue")
        if not hook and isinstance(dialogue, str):
            opening = "visual opening"
            if dialogue.strip():
                opening = "opening question" if "?" in dialogue else "opening statement"
            yield "hook", opening, "first_scene_dialogue"
        durations = [scene.get("duration") for scene in scenes]
        if all(type(duration) is int and duration > 0 for duration in durations):
            yield "length", f"{sum(durations)} seconds", "scene_plan"
    if style:
        yield "style", style, "niche_style_guide"


def _count(value):
    if type(value) is int:
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value.strip()):
        return int(value)
    return None


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _json(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None
