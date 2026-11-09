import json
import tempfile
import unittest
from pathlib import Path

from app.db import StudioStore
from app.insights import normalize_metrics, update_memory
from app.niche import load_profile


class NormalizeMetricsTests(unittest.TestCase):
    def test_uses_latest_totals_and_preserves_zero_and_unavailable(self):
        rows = [
            {"label": "Views", "percentageChange": 85, "data": [
                {"date": "2026-11-08", "total": "100"},
                {"date": "2026-11-07", "total": "90"},
                {"date": "2026-11-09", "total": "120"},
            ]},
            {"label": "Likes", "data": [{"date": "2026-11-09", "total": "0"}]},
            {"label": "Comments", "data": [{"date": "2026-11-09", "total": None}]},
            {"label": "Impressions", "data": [{"date": "2026-11-09", "total": "800"}]},
            {"label": "Average View Percentage", "data": [{"date": "2026-11-09", "total": "63"}]},
        ]

        self.assertEqual(normalize_metrics(rows), {
            "views": 120, "likes": 0, "comments": None, "retention_percent": None,
            "plays": None, "reactions": None,
        })

    def test_facebook_plays_and_reactions_remain_distinct_from_views_and_likes(self):
        rows = [
            {"label": "Plays", "data": [{"date": "2026-11-09", "total": "900"}]},
            {"label": "Reactions", "data": [{"date": "2026-11-09", "total": "41"}]},
            {"label": "Engagement", "data": [{"date": "2026-11-09", "total": "10"}]},
        ]

        result = normalize_metrics(rows)

        self.assertEqual((result["plays"], result["reactions"]), (900, 41))
        for key in ("views", "likes", "comments", "retention_percent"):
            self.assertIsNone(result[key])


class MemoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = StudioStore(Path(directory.name) / "studio.db")
        self.store.initialize()
        self.profile = load_profile()
        self.store.seed_niche(self.profile)

    def test_scores_latest_snapshots_per_platform_and_preserves_manual_memory(self):
        first = self.add_job("Garden rescue", "Can we help?", [4, 4])
        second = self.add_job("Garden adventure", "What grew here?", [4, 4])
        third = self.add_job("Rocket launch", "Ready to go.", [5, 5])
        fourth = self.add_job("Drumbeat", "Time to dance.", [4, 5])
        self.add_snapshot(first, "youtube", {"views": 100, "likes": 12, "comments": 0}, "2026-11-08")
        self.add_snapshot(first, "youtube", {"views": 100, "likes": 1, "comments": 0}, "2026-11-06")
        self.add_snapshot(second, "youtube", {"views": 100, "likes": 8, "comments": None}, "2026-11-08")
        self.add_snapshot(third, "youtube", {"views": 100, "likes": 1, "comments": 0}, "2026-11-08")
        self.add_snapshot(fourth, "youtube", {"views": 200, "likes": 2, "comments": 0}, "2026-11-08")
        self.add_snapshot(first, "tiktok", {"views": 1000, "likes": 50, "comments": 10}, "2026-11-08")
        self.add_snapshot(second, "tiktok", {"views": 1000, "likes": 50, "comments": 10}, "2026-11-08")
        with self.store._connection(write=True) as db:
            manual_id = db.execute(
                "INSERT INTO memory (niche_id, pattern_json) VALUES (?, ?)",
                (self.profile["id"], json.dumps({"note": "Use gentle motion"})),
            ).lastrowid

        summaries = {summary["platform"]: summary for summary in update_memory(self.store, self.profile["id"])}

        youtube = summaries["youtube"]
        self.assertEqual(youtube["score_metric"], "likes_per_100_views")
        self.assertEqual(youtube["platform_support"], 4)
        self.assertEqual(youtube["patterns"]["topic"], {
            "value": "garden", "source": "title_keyword", "score": 10.0, "support": 2,
            "comparison_score": 1.0, "comparison_support": 2, "signal": "above_comparison",
        })
        self.assertEqual(youtube["patterns"]["hook"]["value"], "opening question")
        self.assertEqual(youtube["patterns"]["length"]["value"], "8 seconds")
        self.assertEqual(youtube["patterns"]["style"]["signal"], "observed")
        self.assertIsNone(youtube["patterns"]["style"]["comparison_score"])

        tiktok = summaries["tiktok"]
        self.assertEqual(tiktok["score_metric"], "likes_and_comments_per_100_views")
        self.assertEqual(tiktok["patterns"]["topic"]["score"], 6.0)
        self.assertEqual(tiktok["patterns"]["topic"]["signal"], "observed")

        previous = self.memory_rows()
        update_memory(self.store, self.profile["id"])
        self.assertEqual(self.memory_rows(), previous)

        self.add_snapshot(first, "youtube", {"views": 100, "likes": 20, "comments": 0}, "2026-11-09")
        updated = {summary["platform"]: summary for summary in update_memory(self.store, self.profile["id"])}
        self.assertEqual(updated["youtube"]["patterns"]["topic"]["score"], 14.0)
        self.assertIn((manual_id, {"note": "Use gentle motion"}), self.memory_rows())
        self.assertEqual(len(self.memory_rows()), 3)

    def test_one_job_or_missing_interactions_do_not_establish_a_pattern(self):
        first = self.add_job("Garden rescue", "Can we help?", [4, 4])
        second = self.add_job("Garden adventure", "What grew here?", [4, 4])
        self.add_snapshot(first, "youtube", {"views": 100, "likes": 0, "comments": 0}, "2026-11-08")
        self.add_snapshot(second, "youtube", {"views": 100, "likes": None, "comments": None}, "2026-11-08")

        self.assertEqual(update_memory(self.store, self.profile["id"]), [])
        self.assertEqual(self.memory_rows(), [])

    def add_job(self, title, dialogue, durations):
        idea_id = self.store.add_idea(f"A video about {title}", title)
        self.store.decide_idea(idea_id, "accept")
        scenes = [{"dialogue": dialogue, "duration": duration} for duration in durations]
        with self.store._connection(write=True) as db:
            job_id = db.execute("SELECT id FROM jobs WHERE idea_id = ?", (idea_id,)).fetchone()["id"]
            db.execute("UPDATE jobs SET plan_json = ? WHERE id = ?", (json.dumps(scenes), job_id))
        return job_id

    def add_snapshot(self, job_id, platform, metrics, collected_at):
        with self.store._connection(write=True) as db:
            db.execute(
                "INSERT INTO analytics (job_id, platform, post_id, metrics_json, collected_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, platform, str(job_id), json.dumps(metrics), collected_at),
            )

    def memory_rows(self):
        with self.store._connection() as db:
            return [(row["id"], json.loads(row["pattern_json"])) for row in db.execute(
                "SELECT id, pattern_json FROM memory ORDER BY id"
            )]
