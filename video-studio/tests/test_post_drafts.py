import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from app.db import StudioStore
from app.planning import StudioPlanner
from app.post_drafts import StudioPostDrafts
from app.providers import ProviderError, ProviderResult
from app.settings import StudioSettings


NOW = datetime(2026, 11, 9, 3, tzinfo=timezone.utc)


class StudioPostDraftTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = StudioStore(root / "studio.sqlite3")
        self.store.initialize()
        self.settings = StudioSettings(self.store, root / ".env")
        self.settings.seed()
        self.settings.set_key("ANTHROPIC_API_KEY", "claude-fixture")
        self.settings.update_provider("claude-sonnet", True, 10, 20)
        self.settings.set_budgets(100, 100)
        self.provider = Mock()
        self.provider.submit.return_value = ProviderResult(
            "completed", data={"title": "A story in the garden", "caption": "Tavi finds a seed.", "hashtags": ["tavi", "#garden"]},
        )
        planner = StudioPlanner(self.store, self.settings, adapter_factory=lambda key: self.provider)
        self.drafts = StudioPostDrafts(self.store, planner)
        self.job_id = self.store.create_direct_job("Tavi finds a seed", "Seed story")
        self.first = f"{self.job_id}/preview.first.mp4"
        self.preview(self.first)

    def preview(self, path):
        with self.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET preview_path = ?, status = 'preview_ready' WHERE id = ?", (path, self.job_id))

    def draft(self):
        return self.drafts.get(self.store.get_job(self.job_id))

    def test_generates_once_per_preview_and_rejects_outdated_manual_save(self):
        self.drafts.prepare(NOW)
        self.assertEqual(self.draft()["caption"], "Tavi finds a seed.")
        self.assertEqual(self.draft()["hashtags"], "#tavi #garden")
        self.drafts.prepare(NOW)
        self.assertEqual(self.provider.submit.call_count, 1)

        self.preview(f"{self.job_id}/preview.second.mp4")
        self.assertEqual(self.draft()["status"], "pending")
        with self.assertRaisesRegex(ValueError, "preview changed"):
            self.drafts.save(self.job_id, self.first, {"title": "An outdated title"})
        self.drafts.prepare(NOW)
        self.assertEqual(self.provider.submit.call_count, 2)

    def test_late_generation_does_not_overwrite_a_manual_edit(self):
        result = self.provider.submit.return_value

        def submit(request):
            self.drafts.save(self.job_id, self.first, {"title": "My title", "caption": "My edits"})
            return result

        self.provider.submit.side_effect = submit
        self.drafts.prepare(NOW)
        self.assertEqual(self.draft()["caption"], "My edits")
        self.drafts.prepare(NOW)
        self.assertEqual(self.provider.submit.call_count, 1)

    def test_uncertain_provider_response_stays_editable_and_does_not_retry(self):
        self.provider.submit.side_effect = ProviderError("Provider connection failed.", uncertain=True)
        self.drafts.prepare(NOW)
        self.assertEqual(self.draft()["status"], "needs_review")
        self.drafts.prepare(NOW)
        self.assertEqual(self.provider.submit.call_count, 1)
        self.drafts.save(self.job_id, self.first, {"title": "Manual fallback"})
        self.assertEqual(self.draft()["status"], "ready")
