import json
import unittest
from html.parser import HTMLParser

from app.inbox_view import create_page, edit_schedule_page
from app.library_view import asset_library_section, production_presets_section
from app.settings_view import settings_page
from app.web import workspace_page


class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = {}
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == 'form':
            self.current = {'attrs': attrs, 'fields': {}}
            self.forms[attrs['action']] = self.current
        if self.current is not None and tag in ('input', 'select', 'textarea'):
            self.current['fields'][attrs['name']] = attrs

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None


_ASSETS = [{
    'id': 'pip', 'kind': 'character', 'name': 'Pip <b>Fox</b>', 'description': 'A curious young fox',
    'references': [{
        'id': 'ref_01', 'path': '/internal/reference.png', 'source': 'upload',
        'angle': 'three-quarter', 'variant_id': 'raincoat', 'primary': True,
    }],
    'variants': [{'id': 'raincoat', 'name': 'Raincoat', 'description': 'A yellow raincoat'}],
    'voice_styles': [{
        'id': 'soft', 'name': 'Soft', 'voice': 'Rachel', 'speed': 0.95, 'expressiveness': 0.4,
        'is_default': True,
    }],
}, {
    'id': 'forest', 'kind': 'place', 'name': 'Forest <b>Path</b>', 'description': 'A shaded path',
    'references': [{
        'id': 'ref_02', 'path': '/internal/reference.png', 'source': 'upload',
        'angle': 'front', 'variant_id': '', 'primary': True,
    }],
    'variants': [], 'voice_styles': [],
}]
_PRESETS = [{
    'id': 'watercolor', 'kind': 'visual', 'name': 'Watercolor',
    'config': {'style': 'Soft watercolor', 'palette': 'Lilac and green'}, 'is_default': False,
}, {
    'id': 'large', 'kind': 'subtitle', 'name': 'Large captions',
    'config': {'font_name': 'DejaVu Sans', 'font_size': 24, 'position': 'bottom'}, 'is_default': True,
}]
_SCHEDULE = {'enabled': False, 'cadence': 'daily', 'weekday': 0, 'run_at': '09:00', 'timezone': 'Asia/Dhaka', 'trends': ''}
_BUDGET = {'daily_micros': 0, 'video_micros': 0}
_JOB = {'id': 12, 'title': 'First video', 'status': 'review_keyframes', 'source': 'manual', 'prompt': 'Make a video'}


class LibraryViewTests(unittest.TestCase):
    def test_asset_forms_use_expected_fields_and_same_origin_thumbnails(self):
        place_and_item_assets = [asset for asset in _ASSETS if asset['kind'] in ('place', 'item')]
        html = asset_library_section(place_and_item_assets, 'token')
        forms = Forms(html).forms
        expected_fields = {
            '/settings/assets': {'asset_id', 'kind', 'name', 'description'},
            '/settings/assets/forest/references': {'image', 'angle', 'variant_id', 'primary'},
            '/settings/assets/forest/variants': {'variant_id', 'name', 'description'},
        }
        for action, fields in expected_fields.items():
            with self.subTest(action=action):
                self.assertEqual(set(forms[action]['fields']), fields | {'csrf_token'})
                self.assertEqual(forms[action]['attrs']['method'], 'post')
                self.assertEqual(forms[action]['fields']['csrf_token']['value'], 'token')
        self.assertEqual(forms['/settings/assets/forest/references']['attrs']['enctype'], 'multipart/form-data')
        self.assertNotIn('/settings/assets/forest/voices', forms)
        self.assertNotIn('/settings/assets/pip/', html)
        self.assertNotIn('<option value="character">', html)
        self.assertIn('href="/characters"', html)
        self.assertIn('src="/asset-reference/forest/ref_02"', html)
        self.assertNotIn('/internal/reference.png', html)
        self.assertIn('Forest &lt;b&gt;Path&lt;/b&gt;', html)

    def test_preset_forms_include_correct_settings_upload_and_default_actions(self):
        forms = Forms(production_presets_section(_PRESETS, 'token')).forms
        per_kind = {
            'visual': {'style', 'palette'}, 'script': {'tone'},
            'subtitle': {'font_name', 'font_size', 'text_color', 'outline_color', 'position'},
            'audio': {'music', 'sfx'},
        }
        for kind, fields in per_kind.items():
            with self.subTest(kind=kind):
                form = forms[f'/settings/presets/{kind}']
                self.assertEqual(set(form['fields']), fields | {'preset_id', 'name', 'is_default', 'csrf_token'})
                self.assertEqual(form['fields']['csrf_token']['value'], 'token')
                self.assertEqual(form['attrs'].get('enctype'), 'multipart/form-data' if kind == 'audio' else None)
        self.assertIn('/settings/presets/visual/watercolor/default', forms)
        self.assertNotIn('/settings/presets/subtitle/large/default', forms)

    def test_settings_page_does_not_contain_asset_library_or_presets(self):
        html = settings_page(_BUDGET, _SCHEDULE, 'token', assets=_ASSETS, presets=_PRESETS)
        self.assertNotIn("settings-nav-cards", html)
        self.assertNotIn("<summary>Asset Library</summary>", html)
        self.assertNotIn("<summary>Production Presets</summary>", html)
        self.assertNotIn('/settings/assets', html)
        self.assertNotIn('/settings/presets', html)
        self.assertNotIn("Manage places, environments, and prop references", html)
        self.assertNotIn("Manage visual styles, script tone, subtitles, and audio", html)
        self.assertIn("Daily limit (USD)", html)
        self.assertIn("Idea generation schedule", html)

    def test_workspace_applies_selected_overrides_and_escapes_structured_dialogue(self):
        scene = {
            'scene_id': 'scene_01', 'duration': 4, 'action': 'Pip smiles', 'camera': 'Wide shot',
            'dialogue_lines': [{'speaker_id': 'pip', 'text': '<script>alert(1)</script>'}],
        }
        job = {**_JOB, 'plan_json': json.dumps([scene])}
        html = workspace_page([], job, csrf_token='token', assets=_ASSETS, presets=_PRESETS, production_options={
            'presets': {'visual': 'watercolor'}, 'variants': {'pip': 'raincoat'}, 'voices': {'pip': 'soft'},
            'characters': ['pip'],
        })
        form = Forms(html).forms['/workspace/12/options']
        self.assertEqual(set(form['fields']), {
            'preset.visual', 'preset.script', 'preset.subtitle', 'preset.audio',
            'characters', 'variant.pip', 'voice.pip', 'csrf_token',
        })
        self.assertEqual(form['fields']['characters']['value'], 'pip')
        self.assertIn('checked', form['fields']['characters'])
        self.assertIn('Choose an outfit with images', html)
        self.assertNotIn('name="characters" value="forest"', html)
        self.assertEqual(form['fields']['csrf_token']['value'], 'token')
        for identifier in ('watercolor', 'raincoat', 'soft'):
            self.assertIn(f'<option value="{identifier}" selected>', html)
        self.assertIn('fresh keyframe approval', html)
        self.assertIn('<strong>Pip &lt;b&gt;Fox&lt;/b&gt;</strong> · &lt;script&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)

    def test_workspace_gates_option_edits_and_preserves_legacy_dialogue(self):
        scene = {'scene_id': 'old', 'duration': 5, 'action': 'Walk', 'camera': 'Wide shot', 'dialogue': 'An old line'}
        html = workspace_page([], {**_JOB, 'status': 'producing', 'plan_json': json.dumps([scene])})
        self.assertNotIn('/workspace/12/options', html)
        self.assertIn('Dialogue: An old line', html)
        queued = workspace_page([], {**_JOB, 'status': 'queued'})
        self.assertIn('Options apply when planning starts.', queued)

    def test_ready_for_review_filter_requires_a_finished_preview(self):
        jobs = [
            {**_JOB, 'id': 12, 'title': 'Finished video', 'status': 'preview_ready', 'preview_path': '12/preview.mp4'},
            {**_JOB, 'id': 13, 'title': 'Missing preview', 'status': 'needs_review'},
            {**_JOB, 'id': 14, 'title': 'QA review video', 'status': 'needs_review', 'preview_path': '14/preview.mp4'},
            {**_JOB, 'id': 15, 'title': 'Queued job', 'status': 'queued'},
        ]
        html = workspace_page(jobs, status_filter='ready-for-review')
        self.assertIn('status=ready-for-review" aria-current="page"', html)
        self.assertIn('Finished video', html)
        self.assertIn('QA review video', html)
        self.assertNotIn('Missing preview', html)
        self.assertNotIn('Queued job', html)

    def test_post_review_uses_saved_draft_and_keeps_draft_only_channel_visible(self):
        job = {**_JOB, 'status': 'preview_ready', 'preview_path': '12/version.mp4', 'plan_json': '[]',
               'qa_json': json.dumps({'score': 90, 'issues': ['Check framing']})}
        post_draft = {
            'title': 'Garden & forest', 'caption': '<Preview>', 'hashtags': 'story nature',
            'channels': ['yt'], 'scheduled_at': '2099-01-01T13:00', 'timezone': 'Asia/Dhaka',
            'channel_times': {'yt': '2099-01-01T14:00'}, 'status': 'ready',
        }
        channels = [
            {'id': 'yt', 'name': 'Video channel', 'identifier': 'youtube', 'can_schedule': True},
            {'id': 'tt', 'name': 'Shorts channel', 'identifier': 'tiktok', 'can_schedule': False,
             'schedule_note': 'Postiz drafts only'},
        ]
        html = workspace_page([], job, csrf_token='token', channels=channels, post_draft=post_draft)
        fields = Forms(html).forms['/workspace/12/publish']['fields']
        self.assertEqual(fields['preview_path']['value'], '12/version.mp4')
        self.assertEqual(fields['title']['value'], 'Garden & forest')
        self.assertEqual(fields['hashtags']['value'], 'story nature')
        self.assertEqual(fields['scheduled_at']['value'], '2099-01-01T13:00')
        self.assertEqual(fields['scheduled_at.yt']['value'], '2099-01-01T14:00')
        self.assertNotIn('scheduled_at.tt', fields)
        self.assertIn('value="yt" checked data-schedule-channel', html)
        self.assertIn('name="channel" value="tt"', html)
        self.assertIn('Postiz drafts only', html)
        self.assertIn('Approve &amp; schedule', html)
        self.assertIn('Request video revision', html)
        self.assertIn('&lt;Preview&gt;', html)
        self.assertIn('src="/media/12/version.mp4"', html)
        self.assertIn('Check framing', html)

    def test_scheduled_keyframe_approval_is_opt_in_and_editable(self):
        create = Forms(create_page('token', kind='schedule')).forms['/schedules']['fields']
        self.assertEqual(create['auto_approve_keyframes_present']['value'], '1')
        self.assertNotIn('checked', create['auto_approve_keyframes'])
        schedule = {'id': 3, 'title': 'Studio batch', 'auto_approve_keyframes': True}
        edit = Forms(edit_schedule_page(schedule, 'token')).forms['/schedules/3/edit']['fields']
        self.assertIn('checked', edit['auto_approve_keyframes'])


if __name__ == '__main__':
    unittest.main()
