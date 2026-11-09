from .common import (
    ProviderError, ProviderRequest, ProviderResult, credential, is_https_url,
    reference_images, request_json, task_identifier,
)


_RETRYABLE_TASK_ERRORS = {
    'service_error', 'generation_timeout', 'resource_exhausted',
    'quota_exceeded', 'service_unavailable',
}


class EvoLinkAdapter:
    def __init__(self, api_key):
        self.headers = {'Authorization': f'Bearer {credential(api_key)}'}

    def submit(self, request: ProviderRequest) -> ProviderResult:
        if not request.reference_image:
            raise ProviderError('An approved first-frame image is required to generate video.')
        duration = request.duration if request.duration is not None else 4
        if type(duration) is not int or not 4 <= duration <= 15:
            raise ProviderError('Seedance image-to-video supports integer durations from 4 to 15 seconds.')
        images = reference_images(request)
        if len(images) > 1:
            raise ProviderError('The Studio Seedance route accepts one approved first-frame image.')
        result = request_json('api.evolink.ai', '/v1/videos/generations', self.headers, {
            'model': 'seedance-2.0-image-to-video', 'prompt': request.prompt,
            'image_urls': [self._image_url(images[0])], 'duration': duration,
            'quality': request.resolution, 'aspect_ratio': request.aspect_ratio,
            'generate_audio': False, 'content_filter': True,
        }, paid_submission=True)
        return ProviderResult('pending', task_id=task_identifier(result.get('id'), submitted=True))

    def poll(self, task_id: str) -> ProviderResult:
        task_id = task_identifier(task_id)
        result = request_json('api.evolink.ai', f'/v1/tasks/{task_id}', self.headers)
        status = result.get('status')
        if status in ('pending', 'processing'):
            return ProviderResult('pending', task_id=task_id)
        if status == 'failed':
            error = result.get('error')
            code = error.get('code') if isinstance(error, dict) else None
            raise ProviderError(
                'EvoLink reported that generation failed.', terminal=True,
                retryable=code in _RETRYABLE_TASK_ERRORS,
            )
        if status != 'completed':
            raise ProviderError('EvoLink returned an unknown task status.')
        results = result.get('results')
        url = results[0] if isinstance(results, list) and results else None
        if not is_https_url(url):
            raise ProviderError('EvoLink completed the task without a usable media URL.', terminal=True)
        return ProviderResult('completed', url=url, task_id=task_id, usage=result.get('usage'))

    def _image_url(self, image):
        if is_https_url(image):
            return image
        result = request_json(
            'files-api.evolink.ai', '/api/v1/files/upload/base64', self.headers,
            {'base64_data': image},
        )
        data = result.get('data')
        url = data.get('file_url') if isinstance(data, dict) else None
        if result.get('success') is not True or not is_https_url(url):
            raise ProviderError('EvoLink could not upload the reference image.')
        return url
