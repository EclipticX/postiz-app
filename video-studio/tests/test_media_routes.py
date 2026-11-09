import base64
import io
import socket
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from app.media_routes import (
    _CDNConnection, adapter_for, download_artifact, eligible_routes, estimated_micros, local_image,
)
from app.providers import ClaudeAdapter, EvoLinkAdapter, FalAdapter, ProviderError, ProviderRequest
from app.settings import DEFAULT_PROVIDERS


_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aVt0AAAAASUVORK5CYII='
)
_JPEG = b'\xff\xd8\xff\xe0' + b'image-bytes'
_MP4 = b'\x00\x00\x00\x18ftypisom' + b'video-bytes'
_CDN_URL = 'https://v3b.fal.media/output.jpg?signature=test-signature'


def _settings():
    providers = [
        {
            'name': name, 'type': kind, 'priority': priority, 'billing_unit': unit,
            'rate_micros': 100, 'enabled': True, 'configured': True, 'capabilities': capabilities,
        }
        for name, kind, _, priority, unit, capabilities in DEFAULT_PROVIDERS
    ]
    settings = Mock()
    settings.list_providers.return_value = providers
    return settings, {provider['name']: provider for provider in providers}


class RoutingTests(unittest.TestCase):
    def test_video_routes_match_capabilities_and_priority(self):
        settings, providers = _settings()
        routes = eligible_routes(settings, 'video', ProviderRequest('Scene', reference_image='frame', duration=4))
        self.assertEqual([route['name'] for route in routes], ['fal-veo', 'evolink-seedance', 'fal-kling'])
        request = ProviderRequest('Scene', reference_image='frame', duration=5)
        routes = eligible_routes(settings, 'video', request, preferred='fal-kling')
        self.assertEqual([route['name'] for route in routes], ['fal-kling', 'evolink-seedance'])
        providers['fal-kling']['enabled'] = False
        providers['evolink-seedance']['configured'] = False
        self.assertEqual(eligible_routes(settings, 'video', request), [])

    def test_video_respects_references_resolution_and_aspect_ratio(self):
        settings, _ = _settings()
        request = ProviderRequest('Scene', reference_image='frame', reference_images=('character',))
        routes = eligible_routes(settings, 'video', request)
        self.assertEqual([route['name'] for route in routes], ['fal-kling'])
        for request in (
            ProviderRequest('Scene'),
            ProviderRequest('Scene', reference_image='frame', aspect_ratio='1:1'),
            ProviderRequest('Scene', reference_image='frame', resolution='16k'),
            ProviderRequest('Scene', reference_image='frame', duration=4.0),
        ):
            with self.subTest(request=request):
                self.assertEqual(eligible_routes(settings, 'video', request), [])

    def test_keyframe_and_voice_require_configured_rate_and_capability(self):
        settings, providers = _settings()
        keyframe = providers['fal-nano-banana-pro']
        voice = providers['fal-elevenlabs']
        self.assertEqual(eligible_routes(settings, 'keyframe', ProviderRequest('Image')), [keyframe])
        self.assertEqual(eligible_routes(settings, 'voice', ProviderRequest('Voice')), [voice])
        self.assertEqual(eligible_routes(settings, 'voice', ProviderRequest('  ')), [])
        self.assertEqual(eligible_routes(settings, 'keyframe', ProviderRequest('Image', aspect_ratio='16:9')), [])
        keyframe['rate_micros'] = 0
        voice['billing_unit'] = 'request'
        self.assertEqual(eligible_routes(settings, 'keyframe', ProviderRequest('Image')), [])
        self.assertEqual(eligible_routes(settings, 'voice', ProviderRequest('Voice')), [])

    def test_adapters_and_estimates(self):
        _, providers = _settings()
        self.assertIsInstance(adapter_for(providers['claude-sonnet'], 'test-key'), ClaudeAdapter)
        self.assertIsInstance(adapter_for(providers['evolink-seedance'], 'test-key'), EvoLinkAdapter)
        self.assertIsInstance(adapter_for(providers['fal-veo'], 'test-key'), FalAdapter)
        with self.assertRaises(ProviderError):
            adapter_for(providers['postiz'], 'test-key')
        self.assertEqual(estimated_micros(providers['fal-nano-banana-pro'], ProviderRequest('Image')), 100)
        self.assertEqual(estimated_micros(providers['fal-veo'], ProviderRequest('Scene', duration=4)), 400)
        voice = providers['fal-elevenlabs']
        self.assertEqual(estimated_micros(voice, ProviderRequest('a' * 1001)), 101)
        self.assertEqual(estimated_micros(voice, ProviderRequest('a')), 1)
        with self.assertRaises(ValueError):
            estimated_micros(voice, ProviderRequest(' '))


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'artifact.png'

    def test_local_image_detects_raster_type_by_content(self):
        self.path.write_bytes(_JPEG)
        self.assertEqual(local_image(self.path), 'data:image/jpeg;base64,' + base64.b64encode(_JPEG).decode())
        self.path.write_bytes(b'<script>bad payload</script>')
        with self.assertRaises(ProviderError):
            local_image(self.path)

    def test_local_svg_uses_renderer_stdin_and_preserves_the_source(self):
        svg = self.path.with_suffix('.svg')
        svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>')
        with patch('app.media_routes.subprocess.run', return_value=Mock(stdout=_PNG)) as run:
            self.assertTrue(local_image(svg).startswith('data:image/png;base64,'))
        self.assertEqual(run.call_args.kwargs['input'], svg.read_bytes())
        self.assertEqual(run.call_args.args[0][0], 'rsvg-convert')
        self.assertNotIn(str(svg), run.call_args.args[0])
        with patch('app.media_routes.subprocess.run', side_effect=FileNotFoundError):
            with self.assertRaisesRegex(ProviderError, 'Install rsvg-convert'):
                local_image(svg)

    def test_download_uses_cdn_get_without_auth_and_replaces_artifact(self):
        for url, host, body, kind in (
            (_CDN_URL, 'v3b.fal.media', _JPEG, 'keyframe'),
            ('https://cdn.evolink.ai/output.jpg?signature=test-signature', 'cdn.evolink.ai', _MP4, 'video'),
        ):
            with self.subTest(host=host):
                self.path.write_bytes(b'previous file')
                with patch('app.media_routes._CDNConnection') as connection_type:
                    connection = connection_type.return_value
                    connection.getresponse.return_value = self.response(body)
                    self.assertEqual(download_artifact(url, self.path, kind), self.path)
                    self.assertEqual(self.path.read_bytes(), body)
                    connection_type.assert_called_once_with(host, timeout=20)
                    connection.request.assert_called_once_with('GET', '/output.jpg?signature=test-signature')
                    connection.close.assert_called_once()
                    self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_invalid_urls_never_open_a_connection(self):
        with patch('app.media_routes._CDNConnection') as connect:
            for url in (
                'https://fal.media.evil.test/file.png', 'https://127.0.0.1/file.png',
                'https://user:password@v3b.fal.media/file.png', 'http://v3b.fal.media/file.png',
                'https://v3b.fal.media:8443/file.png', 'https://v3b.fal.media/path\r\nAuthorization:x',
            ):
                with self.subTest(url=url), self.assertRaises(ProviderError):
                    download_artifact(url, self.path, 'keyframe')
            connect.assert_not_called()

    def test_redirect_mismatch_incomplete_and_oversize_preserve_existing_file(self):
        for response, limit in (
            (self.response(_PNG, status=302), 1024),
            (self.response(b'<html>not an image</html>'), 1024),
            (self.response(_PNG, declared_size=len(_PNG) + 1), 1024),
            (self.response(_PNG), 20),
            (self.response(_PNG, declared_size=''), 20),
        ):
            with self.subTest(response=response, limit=limit):
                self.path.write_bytes(b'previous file')
                with patch('app.media_routes._CDNConnection') as connection, patch.dict(
                    'app.media_routes._LIMITS', {'keyframe': limit},
                ):
                    connection.return_value.getresponse.return_value = response
                    with self.assertRaises(ProviderError):
                        download_artifact(_CDN_URL, self.path, 'keyframe')
                    connection.return_value.request.assert_called_once()
                self.assertEqual(self.path.read_bytes(), b'previous file')
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_private_and_mapped_dns_answers_are_rejected_before_socket_open(self):
        connection = _CDNConnection('v3b.fal.media', timeout=20)
        for family, address in (
            (socket.AF_INET, ('169.254.169.254', 443)),
            (socket.AF_INET, ('10.0.0.5', 443)),
            (socket.AF_INET6, ('::ffff:127.0.0.1', 443, 0, 0)),
        ):
            with self.subTest(address=address), patch('app.media_routes.socket.getaddrinfo', return_value=[
                (family, socket.SOCK_STREAM, 6, '', address),
            ]), patch('app.media_routes.socket.socket') as open_socket:
                with self.assertRaisesRegex(ProviderError, 'non-public'):
                    connection.connect()
                open_socket.assert_not_called()

    def test_public_dns_answer_is_pinned_and_tls_uses_cdn_hostname(self):
        host = 'v3b.fal.media'
        connection = _CDNConnection(host, timeout=20)
        address = ('1.1.1.1', 443)
        with patch('app.media_routes.socket.getaddrinfo', return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', address),
        ]) as resolve, patch('app.media_routes.socket.socket') as open_socket, patch(
            'app.media_routes.ssl.create_default_context',
        ) as tls:
            connection.connect()
        resolve.assert_called_once_with(host, 443, type=socket.SOCK_STREAM)
        open_socket.return_value.connect.assert_called_once_with(address)
        tls.return_value.wrap_socket.assert_called_once_with(open_socket.return_value, server_hostname=host)

    @staticmethod
    def response(body, status=200, declared_size=None):
        data = io.BytesIO(body)
        response = Mock(status=status, read=data.read, read1=data.read)
        if declared_size != '':
            response.getheader.return_value = str(len(body) if declared_size is None else declared_size)
        else:
            response.getheader.return_value = None
        return response


if __name__ == '__main__':
    unittest.main()
