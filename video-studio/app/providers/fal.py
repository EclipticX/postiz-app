import base64
from urllib.parse import urlsplit

from .common import (
    ProviderError, ProviderRequest, ProviderResult, credential, is_https_url,
    reference_images, request_json, task_identifier,
)


SEEDANCE_FLASH_ROUTE = 'fal-seedance-25-flash'
SEEDANCE_FLASH_RESOLUTIONS = ('480p', '720p')
SEEDANCE_FLASH_ASPECT_RATIOS = ('auto', '21:9', '16:9', '4:3', '1:1', '3:4', '9:16')
SEEDANCE_FLASH_MAX_AUDIO_BYTES = 15_000_000
_ROUTES = {
    'fal-nano-banana-pro': ('fal-ai/nano-banana-pro', 'images'),
    'fal-veo': ('fal-ai/veo3.1/image-to-video', 'video'),
    'fal-kling': ('kling/v4/flash/reference-to-video', 'video'),
    'fal-elevenlabs': ('fal-ai/elevenlabs/tts/turbo-v2.5', 'audio'),
    SEEDANCE_FLASH_ROUTE: ('bytedance/seedance-2.5/flash', 'video'),
}
_RETRYABLE_TASK_ERRORS = {
    'request_timeout', 'startup_timeout', 'runner_scheduling_failure',
    'runner_connection_timeout', 'runner_disconnected', 'runner_connection_refused',
    'runner_connection_error', 'runner_incomplete_response', 'runner_server_error',
    'internal_error',
}


def supports_seedance_flash(request):
    image_count = int(bool(request.reference_image)) + len(request.reference_images)
    audio_count = len(request.audio_references)
    return (
        bool(request.prompt.strip()) and type(request.duration) is int and 4 <= request.duration <= 30
        and request.resolution in SEEDANCE_FLASH_RESOLUTIONS
        and request.aspect_ratio in SEEDANCE_FLASH_ASPECT_RATIOS
        and type(request.generate_audio) is bool and image_count <= 10
        and (not audio_count or (image_count > 0 and audio_count <= 5 and request.generate_audio))
    )


def _validate_audio_reference(reference):
    message = 'Audio references need a supported MP3 or WAV data URL or HTTPS URL.'
    if is_https_url(reference):
        if len(reference) <= 4096 and all(33 <= ord(char) <= 126 for char in reference) and '\\' not in reference:
            try:
                parts = urlsplit(reference)
                if parts.port in (None, 443) and not parts.fragment:
                    return
            except ValueError:
                pass
        raise ProviderError(message)
    if not isinstance(reference, str):
        raise ProviderError(message)
    metadata, _, data = reference.partition(',')
    if metadata not in ('data:audio/mpeg;base64', 'data:audio/wav;base64') or not data:
        raise ProviderError(message)
    if len(data) > (SEEDANCE_FLASH_MAX_AUDIO_BYTES + 2) // 3 * 4:
        raise ProviderError('Audio references must be at most 15 MB.')
    try:
        decoded = base64.b64decode(data, validate=True)
    except ValueError:
        raise ProviderError(message) from None
    if not decoded or len(decoded) > SEEDANCE_FLASH_MAX_AUDIO_BYTES:
        raise ProviderError(message)


class FalAdapter:
    def __init__(self, api_key, route):
        if route not in _ROUTES:
            raise ProviderError('Unsupported fal.ai route.')
        self.headers = {'Authorization': f'Key {credential(api_key)}'}
        self.route = route
        self.endpoint, self.output = _ROUTES[route]

    def submit(self, request: ProviderRequest) -> ProviderResult:
        endpoint, payload = self._payload(request)
        result = request_json(
            'queue.fal.run', f'/{endpoint}',
            {**self.headers, 'X-Fal-No-Retry': '1'}, payload, paid_submission=True,
        )
        return ProviderResult('pending', task_id=task_identifier(result.get('request_id'), submitted=True))

    def poll(self, task_id: str) -> ProviderResult:
        task_id = task_identifier(task_id)
        owner, app = self.endpoint.split('/')[:2]
        path = f'/{owner}/{app}/requests/{task_id}'
        current = request_json('queue.fal.run', f'{path}/status', self.headers)
        status = current.get('status')
        if status in ('IN_QUEUE', 'IN_PROGRESS'):
            return ProviderResult('pending', task_id=task_id)
        if status != 'COMPLETED':
            raise ProviderError('fal.ai returned an unknown task status.')
        code = current.get('error_type')
        if current.get('error') or code:
            raise ProviderError(
                'fal.ai reported that generation failed.', terminal=True,
                retryable=isinstance(code, str) and code in _RETRYABLE_TASK_ERRORS,
            )
        result = request_json('queue.fal.run', path, self.headers)
        media = result.get(self.output)
        if self.output == 'images':
            media = media[0] if isinstance(media, list) and media else None
        url = media.get('url') if isinstance(media, dict) else None
        if not is_https_url(url):
            raise ProviderError('fal.ai completed the task without a usable media URL.', terminal=True)
        return ProviderResult('completed', url=url, task_id=task_id)

    def _payload(self, request):
        if self.route == 'fal-elevenlabs':
            payload = {
                'text': request.prompt, 'voice': request.voice or 'Rachel',
                'enable_safety_checker': True,
            }
            for field, value, minimum, maximum in (
                ('speed', request.voice_speed, 0.7, 1.2),
                ('style', request.voice_expressiveness, 0, 1),
            ):
                if value is not None:
                    if type(value) not in (int, float) or not minimum <= value <= maximum:
                        raise ProviderError(f'Voice {field} must be between {minimum} and {maximum}.')
                    payload[field] = value
            return self.endpoint, payload

        images = reference_images(request)
        payload = {'prompt': request.prompt, 'aspect_ratio': request.aspect_ratio}
        if self.route == SEEDANCE_FLASH_ROUTE:
            if not supports_seedance_flash(request):
                raise ProviderError('Seedance 2.5 Flash requires 4–30 seconds, 480p or 720p, and supported references.')
            payload.update({
                'resolution': request.resolution, 'duration': request.duration,
                'generate_audio': request.generate_audio, 'codec': 'H264',
            })
            if images:
                payload['image_urls'] = images
                if request.audio_references:
                    for audio in request.audio_references:
                        _validate_audio_reference(audio)
                    payload['audio_urls'] = list(request.audio_references)
                return f'{self.endpoint}/reference-to-video', payload
            return f'{self.endpoint}/text-to-video', payload
        if self.route == 'fal-nano-banana-pro':
            if images:
                payload['image_urls'] = images
                return f'{self.endpoint}/edit', payload
            return self.endpoint, payload

        if not request.reference_image:
            raise ProviderError('An approved first-frame image is required to generate video.')
        payload['resolution'] = request.resolution
        duration = request.duration if request.duration is not None else 4
        if self.route == 'fal-veo':
            if type(duration) is not int or duration not in (4, 6, 8):
                raise ProviderError('Veo image-to-video supports 4, 6, or 8 seconds.')
            if len(images) > 1:
                raise ProviderError('This Veo route accepts one first-frame image.')
            payload.update({
                'image_url': images[0], 'duration': f'{duration}s',
                'generate_audio': request.generate_audio, 'auto_fix': False,
            })
        else:
            if request.resolution != '720p' or type(duration) is not int or not 3 <= duration <= 20:
                raise ProviderError('Kling Flash requires 720p and an integer duration from 3 to 20 seconds.')
            if len(images) > 10:
                raise ProviderError('Kling Flash accepts up to 10 reference images including the first frame.')
            payload.update({'start_image_url': images[0], 'duration': duration})
            if len(images) > 1:
                payload['image_urls'] = images[1:]
        return self.endpoint, payload
