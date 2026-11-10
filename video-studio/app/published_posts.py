"""Group delivered Studio videos and their saved analytics for the published list."""


PLATFORMS = {
    "youtube": "YouTube",
    "facebook": "Facebook",
    "instagram": "Instagram",
    "tiktok": "TikTok",
}
METRICS = ("views", "engagement", "likes", "comments", "follows")


def post_metrics(values):
    values = values or {}
    metrics = {}
    for key in ("views", "likes", "comments"):
        value = values.get(key)
        metrics[key] = value if type(value) is int and value >= 0 else None
    likes, comments = metrics["likes"], metrics["comments"]
    metrics["engagement"] = likes + comments if likes is not None and comments is not None else None
    metrics["follows"] = None
    return metrics


def group_published_posts(posts):
    groups = {}
    for post in posts:
        job_id = post["job_id"]
        platform = post["identifier"].split("-", 1)[0]
        release = dict(
            post,
            platform=platform,
            platform_label=PLATFORMS[platform],
            metrics=post_metrics(post["metrics"]),
        )
        if job_id not in groups:
            groups[job_id] = {
                "id": job_id,
                "index": f"P-{job_id:03d}",
                "title": post["job_title"],
                "platforms": [],
            }
        groups[job_id]["platforms"].append(release)

    for record in groups.values():
        platforms = record["platforms"]
        record["release_at"] = min(post["scheduled_at"] for post in platforms)
        record["metrics"] = {}
        record["partial_metrics"] = set()
        for key in METRICS:
            available = [post["metrics"][key] for post in platforms if post["metrics"][key] is not None]
            record["metrics"][key] = sum(available) if available else None
            if available and len(available) < len(platforms):
                record["partial_metrics"].add(key)
    return sorted(groups.values(), key=lambda record: (record["release_at"], record["id"]), reverse=True)
