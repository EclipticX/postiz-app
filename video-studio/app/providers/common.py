import json
import re
from dataclasses import dataclass, field
from http.client import HTTPException, HTTPSConnection
from typing import Optional, Protocol
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ProviderRequest:
    prompt: str = field(repr=False)
    reference_image: Optional[str] = field(default=None, repr=False)
    duration: Optional[int] = None
    resolution: str = '720p'
    output_schema: Optional[dict] = None
    system: Optional[str] = field(default=None, repr=False)
    reference_images: tuple[str, ...] = field(default=(), repr=False)
    voice: Optional[str] = None
    aspect_ratio: str = '9:16'
    voice_speed: Optional[float] = None
    voice_expressiveness: Optional[float] = None


@dataclass(frozen=True)
class ProviderResult:
    status: str
    text: Optional[str] = field(default=None, repr=False)
    data: Optional[dict] = field(default=None, repr=False)
    url: Optional[str] = field(default=None, repr=False)
    task_id: Optional[str] = None
    usage: Optional[dict] = None


class ProviderError(RuntimeError):
    def __init__(self, message, *, retryable=False, uncertain=False, terminal=False):
        super().__init__(message)
        self.retryable = retryable
        self.uncertain = uncertain
        self.terminal = terminal


class ProviderAdapter(Protocol):
    def submit(self, request: ProviderRequest) -> ProviderResult: ...

    def poll(self, task_id: str) -> ProviderResult: ...


def credential(api_key):
    if not isinstance(api_key, str) or not api_key or not all(33 <= ord(char) <= 126 for char in api_key):
        raise ProviderError('Configure a valid API key for this provider.')
    return api_key


def task_identifier(value, *, submitted=False):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,200}', value):
        raise ProviderError('Provider task ID is missing or invalid.', uncertain=submitted)
    return value


def is_https_url(value):
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        return url.scheme == 'https' and bool(url.hostname) and url.username is None
    except ValueError:
        return False


def image_data_url(value):
    if not isinstance(value, str):
        raise ProviderError('Image references need a supported image data URL or HTTPS URL.')
    metadata, _, data = value.partition(',')
    if metadata not in (
        'data:image/png;base64', 'data:image/jpeg;base64',
        'data:image/webp;base64', 'data:image/gif;base64',
    ) or not data:
        raise ProviderError('Image references need a supported image data URL or HTTPS URL.')
    return metadata.removeprefix('data:').removesuffix(';base64'), data


def reference_images(request):
    images = list(request.reference_images)
    if request.reference_image:
        images.insert(0, request.reference_image)
    for image in images:
        if not is_https_url(image):
            image_data_url(image)
    return images


def request_json(host, path, headers, payload=None, *, paid_submission=False, timeout=30):
    method = 'GET'
    body = None
    headers = {'Accept': 'application/json', **headers}
    if payload is not None:
        method = 'POST'
        headers['Content-Type'] = 'application/json'
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')

    # Call fixed API hosts directly. Do not follow redirects with API credentials.
    connection = HTTPSConnection(host, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        if response.status >= 300:
            _http_error(response.status, paid_submission)
        limit = 4 * 1024 * 1024
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ProviderError('Provider response exceeds the size limit.', uncertain=paid_submission)
        result = json.loads(body)
    except (OSError, HTTPException):
        raise ProviderError(
            'Provider connection failed.',
            retryable=not paid_submission, uncertain=paid_submission,
        ) from None
    except ValueError:
        raise ProviderError('Provider returned invalid JSON.', uncertain=paid_submission) from None
    finally:
        connection.close()

    if not isinstance(result, dict):
        raise ProviderError('Provider returned an unexpected response.', uncertain=paid_submission)
    return result


def _http_error(status, paid_submission):
    if status in (401, 403):
        raise ProviderError('Provider denied access. Check the API key and model permissions.', retryable=True)
    if status == 402:
        raise ProviderError('Provider account has insufficient credit.', retryable=True)
    if status == 429:
        raise ProviderError('Provider rate limit reached.', retryable=True)
    if status == 408 or status >= 500:
        raise ProviderError(
            f'Provider service unavailable (HTTP {status}).',
            retryable=not paid_submission, uncertain=paid_submission,
        )
    raise ProviderError(f'Provider rejected the request (HTTP {status}).')
