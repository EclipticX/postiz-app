"""Route media requests and persist validated provider artifacts locally."""

import base64
import socket
import ssl
import subprocess
from http.client import HTTPException, HTTPSConnection
from ipaddress import ip_address
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic
from urllib.parse import urlsplit

from .providers import ClaudeAdapter, EvoLinkAdapter, FalAdapter, ProviderError


_KINDS = {'keyframe': ('image', 'image'), 'video': ('video', 'second'), 'voice': ('tts', '1000_characters')}
_FAL_ROUTES = {'fal-nano-banana-pro', 'fal-veo', 'fal-kling', 'fal-elevenlabs'}
_VIDEO_REFERENCE_LIMITS = {'fal-veo': 1, 'evolink-seedance': 1, 'fal-kling': 10}
_LIMITS = {'keyframe': 12 * 1024**2, 'video': 80 * 1024**2, 'voice': 16 * 1024**2}
_EVOLINK_CDN_HOSTS = {'cdn.evolink.ai', 'files.evolink.ai'}


def eligible_routes(settings, kind, request, preferred=None):
    provider_type, unit = _KINDS[kind]
    providers = [
        provider for provider in settings.list_providers()
        if provider['type'] == provider_type and provider['billing_unit'] == unit
        and provider['enabled'] and provider['configured'] and provider['rate_micros'] > 0
        and _supports(provider, kind, request)
    ]
    return sorted(providers, key=lambda provider: (
        provider['name'] != preferred, provider['priority'], provider['name'],
    ))


def _supports(provider, kind, request):
    name = provider['name']
    capabilities = provider['capabilities']
    reference_count = int(bool(request.reference_image)) + len(request.reference_images)
    if kind == 'keyframe':
        return (
            name == 'fal-nano-banana-pro' and reference_count <= 14
            and (not reference_count or capabilities.get('reference_image'))
            and request.aspect_ratio in capabilities.get('aspect_ratios', ())
            and ('resolutions' not in capabilities or request.resolution in capabilities['resolutions'])
        )
    if kind == 'voice':
        return name == 'fal-elevenlabs' and bool(request.prompt.strip()) and capabilities.get('audio')
    duration = 4 if request.duration is None else request.duration
    return (
        bool(request.reference_image) and capabilities.get('image_to_video')
        and capabilities.get('reference_image')
        and reference_count <= _VIDEO_REFERENCE_LIMITS.get(name, 0)
        and type(duration) is int and duration in capabilities.get('durations', ())
        and request.aspect_ratio in capabilities.get('aspect_ratios', ())
        and request.resolution in capabilities.get('resolutions', ())
    )


def adapter_for(provider, key):
    name = provider['name']
    if name == 'claude-sonnet':
        return ClaudeAdapter(key)
    if name == 'evolink-seedance':
        return EvoLinkAdapter(key)
    if name in _FAL_ROUTES:
        return FalAdapter(key, name)
    raise ProviderError('No adapter is registered for this media provider.')


def estimated_micros(provider, request):
    rate = provider['rate_micros']
    unit = provider['billing_unit']
    if type(rate) is not int or rate <= 0:
        raise ValueError('Configure a positive provider rate before reserving spend.')
    if unit in ('image', 'request'):
        return rate
    if unit == 'second':
        duration = 4 if request.duration is None else request.duration
        if type(duration) is not int or duration <= 0:
            raise ValueError('A positive whole-second duration is required to estimate spend.')
        return rate * duration
    if unit == '1000_characters' and request.prompt.strip():
        return (rate * len(request.prompt) + 999) // 1000
    raise ValueError('Cannot estimate spend for this provider request.')


def local_image(path):
    """Encode a raster image or a checked-in SVG reference as a provider data URL."""
    path = Path(path)
    limit = _LIMITS['keyframe']
    try:
        with path.open('rb') as source:
            data = source.read(limit + 1)
    except OSError:
        raise ProviderError('The local reference image could not be read.') from None
    if len(data) > limit:
        raise ProviderError('The local reference image exceeds the size limit.')

    if not _image_mime(data) and path.suffix.lower() == '.svg':
        data = _rasterize_svg(data)
    mime = _image_mime(data)
    if not mime or len(data) > limit:
        raise ProviderError('The reference needs to be a supported PNG, JPEG, or WebP image.')
    return f'data:{mime};base64,{base64.b64encode(data).decode("ascii")}'


def _rasterize_svg(data):
    try:
        # Use stdin, which gives librsvg no source directory for resolving external files.
        return subprocess.run(
            ['rsvg-convert', '--format=png', '--width=768', '--height=768', '--keep-aspect-ratio'],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=15,
        ).stdout
    except FileNotFoundError:
        raise ProviderError('Install rsvg-convert to render the SVG niche reference images.') from None
    except (OSError, subprocess.SubprocessError):
        raise ProviderError('The SVG niche reference image could not be rendered.') from None


def _image_mime(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def _matches_artifact(kind, head):
    if kind == 'keyframe':
        return bool(_image_mime(head))
    if kind == 'video':
        return len(head) >= 12 and head[4:8] == b'ftyp'
    return len(head) >= 4 and (head.startswith(b'ID3') or (head[0] == 0xff and head[1] & 0xe0 == 0xe0))


def _artifact_source(url):
    if not isinstance(url, str) or len(url) > 8192 or any(not 33 <= ord(char) <= 126 for char in url):
        raise ProviderError('Provider media URL is invalid.')
    try:
        parts = urlsplit(url)
        host = parts.hostname or ''
        allowed_host = host == 'fal.media' or host.endswith('.fal.media') or host in _EVOLINK_CDN_HOSTS
        if (
            parts.scheme != 'https' or not allowed_host or parts.port not in (443, None)
            or parts.username is not None or parts.fragment or not parts.path.startswith('/')
        ):
            raise ValueError
    except ValueError:
        raise ProviderError('Provider media must use an approved HTTPS CDN address.') from None
    target = parts.path
    if parts.query:
        target += f'?{parts.query}'
    return host, target


class _CDNConnection(HTTPSConnection):
    def connect(self):
        addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        for _, _, _, _, address in addresses:
            ip = ip_address(address[0])
            ip = getattr(ip, 'ipv4_mapped', None) or ip
            if not ip.is_global or ip.is_multicast or ip.is_reserved:
                raise ProviderError('Provider CDN resolved to a non-public address.')

        # Connect to the addresses checked above and verify TLS for the original hostname.
        tls = ssl.create_default_context()
        for family, socktype, proto, _, address in addresses:
            raw_socket = socket.socket(family, socktype, proto)
            try:
                raw_socket.settimeout(self.timeout)
                raw_socket.connect(address)
                self.sock = tls.wrap_socket(raw_socket, server_hostname=self.host)
                return
            except OSError:
                raw_socket.close()
        raise OSError('Could not connect to provider CDN.')


def download_artifact(url, path, kind):
    """Save a bounded download atomically without forwarding API credentials."""
    host, target = _artifact_source(url)
    limit = _LIMITS[kind]
    path = Path(path)
    temporary_path = None
    connection = _CDNConnection(host, timeout=20)
    try:
        connection.request('GET', target)
        response = connection.getresponse()
        if response.status != 200:
            raise ProviderError(
                f'Provider media download returned HTTP {response.status}.',
                retryable=response.status in (408, 429) or response.status >= 500,
            )
        expected_size = _content_size(response.getheader('Content-Length'), limit)
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=path.parent, delete=False) as destination:
            temporary_path = Path(destination.name)
            _copy_artifact(response, destination, kind, limit, expected_size)
        temporary_path.replace(path)
        return path
    except (OSError, HTTPException):
        raise ProviderError('Provider media could not be downloaded to disk.', retryable=True) from None
    finally:
        connection.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _content_size(header, limit):
    if header is None:
        return None
    if len(header) > 12 or not header.isascii() or not header.isdigit() or not 0 < int(header) <= limit:
        raise ProviderError('Provider media exceeds the download size limit or has an invalid length.')
    return int(header)


def _copy_artifact(response, destination, kind, limit, expected_size):
    deadline = monotonic() + 120
    head = response.read(12)
    if not _matches_artifact(kind, head):
        raise ProviderError('Provider output does not match the expected media format.')
    destination.write(head)
    received = len(head)
    while True:
        if monotonic() >= deadline:
            raise ProviderError('Provider media download timed out.', retryable=True)
        chunk = response.read1(64 * 1024)
        if not chunk:
            break
        received += len(chunk)
        if received > limit:
            raise ProviderError('Provider media exceeds the download size limit.')
        destination.write(chunk)
    if expected_size is not None and received != expected_size:
        raise ProviderError('Provider media download ended early.', retryable=True)
