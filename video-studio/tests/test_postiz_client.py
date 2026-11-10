import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from app.postiz_client import PostizClient, PostizError


_KEY = 'test-postiz-key'
_MEDIA = {'id': 'media-1', 'path': 'http://localhost:4007/uploads/clip.mp4'}
_MP4 = b'\x00\x00\x00\x18ftypisom' + b'video-bytes'


def _response(payload, status=200):
    response = Mock(status=status)
    response.read.return_value = json.dumps(payload).encode('utf-8')
    return response


class PostizClientTests(unittest.TestCase):
    def setUp(self):
        for scheme in ('HTTP', 'HTTPS'):
            connection_patch = patch(f'app.postiz_client.{scheme}Connection')
            setattr(self, scheme.lower(), connection_patch.start())
            self.addCleanup(connection_patch.stop)
        self.client = PostizClient('http://localhost:4007', _KEY)
        self.connection = self.http.return_value

    def test_channels_uses_public_api_prefix_raw_key_and_supported_enabled_channels(self):
        enabled = [
            {'id': f'channel-{index}', 'identifier': identifier, 'name': f'Channel {index}', 'disabled': False}
            for index, identifier in enumerate(('youtube', 'facebook', 'instagram', 'instagram-standalone', 'tiktok'))
        ]
        self.connection.getresponse.return_value = _response(enabled + [
            {'id': 'no', 'identifier': 'tiktok-business'},
            {'id': 'disabled', 'identifier': 'facebook', 'disabled': True},
            {'id': 'invalid/path', 'identifier': 'youtube'},
            None,
        ])
        self.assertEqual(self.client.channels(), [
            {field: channel[field] for field in ('id', 'identifier', 'name')} for channel in enabled
        ])
        self.http.assert_called_once_with('localhost', 4007, timeout=30)
        self.connection.request.assert_called_once_with(
            'GET', '/api/public/v1/integrations', body=None,
            headers={'Accept': 'application/json', 'Authorization': _KEY},
        )
        self.connection.close.assert_called_once()

    def test_uses_configured_origin_and_prefix_only(self):
        self.https.return_value.getresponse.return_value = _response([])
        for url, path in (
            ('https://postiz.example/api/', '/api/public/v1/integrations'),
            ('https://postiz.example/public/v1', '/public/v1/integrations'),
            ('https://postiz.example/preview/api/public/v1/', '/preview/api/public/v1/integrations'),
        ):
            with self.subTest(url=url):
                self.assertEqual(PostizClient(url, _KEY).channels(), [])
                self.https.assert_called_with('postiz.example', None, timeout=30)
                self.assertEqual(self.https.return_value.request.call_args.args, ('GET', path))
        for url in ('//postiz.example', 'ftp://postiz.example', 'https://key@postiz.example', 'https://postiz.example?x=1'):
            with self.subTest(url=url), self.assertRaises(PostizError):
                PostizClient(url, _KEY)
        with self.assertRaises(PostizError):
            PostizClient('http://localhost:4007', 'key\r\nother-header')

    def test_upload_streams_multipart_mp4_and_returns_media_id_and_path(self):
        sent = {}

        def request(method, path, *, body, headers):
            sent.update(method=method, path=path, headers=headers, body=b''.join(body))

        self.connection.request.side_effect = request
        self.connection.getresponse.return_value = _response({**_MEDIA, 'thumbnail': None}, status=201)
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'local clip.mp4'
            path.write_bytes(_MP4)
            self.assertEqual(self.client.upload(path), _MEDIA)
        self.assertEqual((sent['method'], sent['path']), ('POST', '/api/public/v1/upload'))
        self.assertEqual(sent['headers']['Authorization'], _KEY)
        boundary = sent['headers']['Content-Type'].split('boundary=')[1].encode('ascii')
        self.assertTrue(sent['body'].startswith(b'--' + boundary + b'\r\n'))
        self.assertTrue(sent['body'].endswith(b'\r\n--' + boundary + b'--\r\n'))
        self.assertIn(b'name="file"; filename="studio-video.mp4"', sent['body'])
        self.assertIn(b'Content-Type: video/mp4\r\n\r\n' + _MP4, sent['body'])
        self.assertEqual(int(sent['headers']['Content-Length']), len(sent['body']))

    def test_upload_refuses_invalid_local_media_before_sending(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'not-a-video.mp4'
            path.write_bytes(b'<html>example</html>')
            with self.assertRaisesRegex(PostizError, 'MP4 header'):
                self.client.upload(path)
        self.http.assert_not_called()

    def test_scheduled_youtube_post_maps_metadata_and_uses_utc(self):
        self.connection.getresponse.return_value = _response([{'postId': 'post-1', 'integration': 'channel-1'}], status=201)
        post_id = self.client.create_post(
            {'id': 'channel-1', 'identifier': 'youtube'}, _MEDIA, 'Forest adventure', 'Visit the forest.',
            '#Forest, #Kids forest', datetime(2026, 11, 10, 9, 30, tzinfo=timezone(timedelta(hours=6))),
        )
        self.assertEqual(post_id, 'post-1')
        sent = self.connection.request.call_args
        self.assertEqual(sent.args, ('POST', '/api/public/v1/posts'))
        self.assertEqual(sent.kwargs['headers']['Content-Type'], 'application/json')
        self.assertEqual(json.loads(sent.kwargs['body']), {
            'type': 'schedule', 'date': '2026-11-10T03:30:00Z', 'shortLink': False, 'tags': [],
            'posts': [{
                'integration': {'id': 'channel-1'},
                'settings': {
                    '__type': 'youtube', 'title': 'Forest adventure', 'type': 'public', 'selfDeclaredMadeForKids': 'yes',
                    'tags': [{'label': 'Forest', 'value': 'Forest'}, {'label': 'Kids', 'value': 'Kids'}],
                },
                'value': [{'content': 'Visit the forest.\n\n#Forest #Kids', 'image': [_MEDIA]}],
            }],
        })

    def test_meta_reels_match_local_provider_settings(self):
        self.connection.getresponse.return_value = _response([{'postId': 'post-1', 'integration': 'channel-1'}])
        for identifier in ('facebook', 'instagram', 'instagram-standalone'):
            with self.subTest(identifier=identifier):
                self.client.create_post({'id': 'channel-1', 'identifier': identifier}, _MEDIA, 'Studio clip', '', [], None)
                sent = json.loads(self.connection.request.call_args.kwargs['body'])
                self.assertEqual(sent['type'], 'now')
                self.assertTrue(sent['date'].endswith('Z'))
                post = sent['posts'][0]
                self.assertEqual(post['value'][0]['image'], [_MEDIA])
                self.assertEqual(post['settings']['__type'], identifier)
                self.assertEqual(post['settings']['post_type'], 'post')
                if identifier == 'facebook':
                    self.assertEqual(post['settings']['title'], 'Studio clip')

    def test_tiktok_rejects_direct_scheduling_and_only_creates_a_postiz_draft(self):
        channel = {'id': 'channel-1', 'identifier': 'tiktok'}
        for date in (None, '2026-11-10T09:30:00Z'):
            with self.subTest(date=date), self.assertRaisesRegex(PostizError, 'draft'):
                self.client.create_post(channel, _MEDIA, 'Studio clip', '', [], date)
        self.connection.request.assert_not_called()

        self.connection.getresponse.return_value = _response([{'postId': 'post-1', 'integration': 'channel-1'}])
        self.assertEqual(self.client.create_post(channel, _MEDIA, 'Studio clip', '', [], None, draft=True), 'post-1')
        sent = json.loads(self.connection.request.call_args.kwargs['body'])
        self.assertEqual(sent['type'], 'draft')
        post = sent['posts'][0]
        self.assertEqual(post['settings']['content_posting_method'], 'UPLOAD')
        self.assertEqual(post['settings']['privacy_level'], 'SELF_ONLY')
        self.assertTrue(post['settings']['video_made_with_ai'])
        self.assertFalse(post['settings']['duet'])
        self.assertFalse(post['settings']['stitch'])
        self.assertFalse(post['settings']['comment'])
        self.assertEqual(post['value'][0]['content'], 'Studio clip')

    def test_draft_mode_and_validation_before_sending(self):
        self.connection.getresponse.return_value = _response([{'postId': 'draft-1', 'integration': 'channel-1'}])
        channel = {'id': 'channel-1', 'identifier': 'instagram'}
        self.assertEqual(self.client.create_post(channel, _MEDIA, '', 'Caption', [], None, draft=True), 'draft-1')
        self.assertEqual(json.loads(self.connection.request.call_args.kwargs['body'])['type'], 'draft')
        self.connection.request.reset_mock()
        with self.assertRaisesRegex(PostizError, 'timezone'):
            self.client.create_post(channel, _MEDIA, '', 'Caption', [], '2026-11-10T09:30:00')
        with self.assertRaisesRegex(PostizError, 'MP4'):
            self.client.create_post(channel, {'id': 'media-1', 'path': 'file:/private/video.mp4'}, '', 'Caption', [], None)
        with self.assertRaisesRegex(PostizError, 'supported'):
            self.client.create_post({**channel, 'identifier': 'tiktok-business'}, _MEDIA, '', 'Caption', [], None)
        self.connection.request.assert_not_called()

    def test_create_errors_are_sanitized_and_ambiguous_outcomes_are_flagged(self):
        channel = {'id': 'channel-1', 'identifier': 'instagram'}
        for status, uncertain in ((302, True), (400, False), (401, False), (429, False), (499, True), (503, True)):
            with self.subTest(status=status):
                self.connection.request.reset_mock()
                response = _response({'detail': _KEY, 'location': 'https://other.example'}, status=status)
                self.connection.getresponse.return_value = response
                with self.assertRaises(PostizError) as raised:
                    self.client.create_post(channel, _MEDIA, '', 'Caption', [], None)
                self.assertEqual(raised.exception.uncertain, uncertain)
                self.assertNotIn(_KEY, str(raised.exception))
                self.connection.request.assert_called_once()
                response.read.assert_not_called()

        for payload in ([], {'postId': 'post-1'}, [{'postId': 'post-1', 'integration': 'different'}]):
            self.connection.getresponse.return_value = _response(payload)
            with self.assertRaises(PostizError) as raised:
                self.client.create_post(channel, _MEDIA, '', 'Caption', [], None)
            self.assertTrue(raised.exception.uncertain)

        self.connection.getresponse.side_effect = TimeoutError(_KEY)
        with self.assertRaises(PostizError) as raised:
            self.client.create_post(channel, _MEDIA, '', 'Caption', [], None)
        self.assertTrue(raised.exception.uncertain)
        self.assertNotIn(_KEY, str(raised.exception))

    def test_analytics_returns_raw_rows_and_preserves_absence(self):
        rows = [{'label': 'Views', 'data': [{'date': '2026-11-09', 'total': 0}], 'percentageChange': 0}]
        self.connection.getresponse.side_effect = [_response(rows), _response({'missing': True})]
        self.assertEqual(self.client.analytics('post-1'), rows)
        self.assertEqual(self.client.analytics('post-2'), [])
        self.assertEqual([call.args for call in self.connection.request.call_args_list], [
            ('GET', '/api/public/v1/analytics/post/post-1?date=7'),
            ('GET', '/api/public/v1/analytics/post/post-2?date=7'),
        ])
        with self.assertRaises(PostizError):
            self.client.analytics('../unsafe-path')

    def test_original_post_lookup_matches_published_id_within_utc_date_window(self):
        release = 'https://www.youtube.com/watch?v=real-video'
        self.connection.getresponse.return_value = _response({'posts': [
            {'id': 'another-post', 'state': 'PUBLISHED', 'releaseURL': 'https://www.youtube.com/watch?v=other'},
            {'id': 'post-1', 'state': 'QUEUE', 'releaseURL': 'https://www.youtube.com/watch?v=pending'},
            {'id': 'post-1', 'state': 'PUBLISHED', 'releaseURL': release},
        ]})

        self.assertEqual(self.client.original_post_url('post-1', '2026-11-10T02:10:00+06:00'), release)
        self.connection.request.assert_called_once_with(
            'GET', '/api/public/v1/posts?startDate=2026-11-08&endDate=2026-11-11', body=None,
            headers={'Accept': 'application/json', 'Authorization': _KEY},
        )

    def test_original_post_lookup_ignores_unpublished_and_unsafe_links(self):
        scheduled = '2026-11-09T09:00:00Z'
        for url in (None, '', '/watch', 'http://example.com/post', 'javascript:alert(1)',
                    'https://account@example.com/post', 'https://example.com/post\r\nheader: injected',
                    'https://example.com:broken/post', 'https://example.com\\bad/post'):
            with self.subTest(url=url):
                self.connection.getresponse.return_value = _response({'posts': [
                    {'id': 'post-1', 'state': 'PUBLISHED', 'releaseURL': url},
                    {'id': 'other-post', 'state': 'PUBLISHED', 'releaseURL': 'https://example.com/other'},
                ]})
                self.assertIsNone(self.client.original_post_url('post-1', scheduled))
        self.connection.getresponse.return_value = _response({'posts': [
            {'id': 'post-1', 'state': 'DRAFT', 'releaseURL': 'https://example.com/draft'},
        ]})
        self.assertIsNone(self.client.original_post_url('post-1', scheduled))
        self.connection.request.reset_mock()
        with self.assertRaises(PostizError):
            self.client.original_post_url('../unsafe-path', scheduled)
        self.assertIsNone(self.client.original_post_url('post-1', None))
        self.connection.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
