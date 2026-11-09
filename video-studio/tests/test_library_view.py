import json
import unittest
from html.parser import HTMLParser

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
    'id': 'forest', 'kind': 'place', 'name': 'Forest', 'description': 'A shaded path',
    'references': [], 'variants': [], 'voice_styles': [],
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
        html = settings_page([], _BUDGET, _SCHEDULE, 'token', assets=_ASSETS, presets=_PRESETS)
        forms = Forms(html).forms
        expected_fields = {
            '/settings/assets': {'asset_id', 'kind', 'name', 'description'},
            '/settings/assets/pip/references': {'image', 'angle', 'variant_id', 'primary'},
            '/settings/assets/pip/variants': {'variant_id', 'name', 'description'},
            '/settings/assets/pip/voices': {'style_id', 'name', 'voice', 'speed', 'expressiveness', 'is_default'},
        }
        for action, fields in expected_fields.items():
            with self.subTest(action=action):
                self.assertEqual(set(forms[action]['fields']), fields | {'csrf_token'})
                self.assertEqual(forms[action]['attrs']['method'], 'post')
                self.assertEqual(forms[action]['fields']['csrf_token']['value'], 'token')
        self.assertEqual(forms['/settings/assets/pip/references']['attrs']['enctype'], 'multipart/form-data')
        self.assertNotIn('/settings/assets/forest/voices', forms)
        self.assertIn('src="/asset-reference/pip/ref_01"', html)
        self.assertNotIn('/internal/reference.png', html)
        self.assertIn('Pip &lt;b&gt;Fox&lt;/b&gt;', html)
        self.assertIn('0.4 expressiveness', html)

    def test_preset_forms_include_correct_settings_upload_and_default_actions(self):
        forms = Forms(settings_page([], _BUDGET, _SCHEDULE, 'token', presets=_PRESETS)).forms
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

    def test_workspace_applies_selected_overrides_and_escapes_structured_dialogue(self):
        scene = {
            'scene_id': 'scene_01', 'duration': 4, 'action': 'Pip smiles', 'camera': 'Wide shot',
            'dialogue_lines': [{'speaker_id': 'pip', 'text': '<script>alert(1)</script>'}],
        }
        job = {**_JOB, 'plan_json': json.dumps([scene])}
        html = workspace_page([], job, csrf_token='token', assets=_ASSETS, presets=_PRESETS, production_options={
            'presets': {'visual': 'watercolor'}, 'variants': {'pip': 'raincoat'}, 'voices': {'pip': 'soft'},
        })
        form = Forms(html).forms['/workspace/12/options']
        self.assertEqual(set(form['fields']), {
            'preset.visual', 'preset.script', 'preset.subtitle', 'preset.audio',
            'variant.pip', 'voice.pip', 'csrf_token',
        })
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


if __name__ == '__main__':
    unittest.main()
