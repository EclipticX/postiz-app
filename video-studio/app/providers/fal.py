from .common import (
    ProviderError, ProviderRequest, ProviderResult, credential, is_https_url,
    reference_images, request_json, task_identifier,
)


_ROUTES = {
    'fal-nano-banana-pro': ('fal-ai/nano-banana-pro', 'images'),
    'fal-veo': ('fal-ai/veo3.1/image-to-video', 'video'),
    'fal-kling': ('kling/v4/flash/reference-to-video', 'video'),
    'fal-elevenlabs': ('fal-ai/elevenlabs/tts/turbo-v2.5', 'audio'),
}
_RETRYABLE_TASK_ERRORS = {
    'request_timeout', 'startup_timeout', 'runner_scheduling_failure',
    'runner_connection_timeout', 'runner_disconnected', 'runner_connection_refused',
    'runner_connection_error', 'runner_incomplete_response', 'runner_server_error',
    'internal_error',
}


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
        if current.get('error') or current.get('error_type'):
            raise ProviderError(
                'fal.ai reported that generation failed.', terminal=True,
                retryable=current.get('error_type') in _RETRYABLE_TASK_ERRORS,
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
                'generate_audio': False, 'auto_fix': False,
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
