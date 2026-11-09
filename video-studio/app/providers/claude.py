import json

from .common import (
    ProviderError, ProviderRequest, ProviderResult, credential, image_data_url,
    is_https_url, reference_images, request_json,
)


class ClaudeAdapter:
    def __init__(self, api_key, model='claude-sonnet-5-5', max_tokens=4096):
        self.headers = {'x-api-key': credential(api_key), 'anthropic-version': '2023-06-01'}
        self.model = model
        self.max_tokens = max_tokens

    def submit(self, request: ProviderRequest) -> ProviderResult:
        content = [_image_block(image) for image in reference_images(request)]
        content.append({'type': 'text', 'text': request.prompt})
        payload = {
            'model': self.model, 'max_tokens': self.max_tokens,
            'messages': [{'role': 'user', 'content': content}],
        }
        if request.system:
            payload['system'] = request.system
        if request.output_schema is not None:
            payload['output_config'] = {
                'format': {'type': 'json_schema', 'schema': request.output_schema},
            }
        result = request_json(
            'api.anthropic.com', '/v1/messages', self.headers, payload,
            paid_submission=True, timeout=90,
        )
        if result.get('stop_reason') == 'refusal':
            raise ProviderError('Claude declined this request.')
        if result.get('stop_reason') == 'max_tokens':
            raise ProviderError('Claude reached its output token limit.')
        blocks = result.get('content')
        if not isinstance(blocks, list):
            raise ProviderError('Claude did not return message content.')
        text = ''.join(
            block['text'] for block in blocks
            if isinstance(block, dict) and block.get('type') == 'text'
            and isinstance(block.get('text'), str)
        ).strip()
        if not text:
            raise ProviderError('Claude did not return text.')
        data = None
        if request.output_schema is not None:
            try:
                data = json.loads(text)
            except ValueError:
                raise ProviderError('Claude did not return valid structured JSON.') from None
            if not isinstance(data, dict):
                raise ProviderError('Claude did not return a JSON object.')
        return ProviderResult('completed', text=text, data=data, usage=result.get('usage'))

    def poll(self, task_id: str) -> ProviderResult:
        raise ProviderError('Claude messages complete in the submission response.')


def _image_block(image):
    if is_https_url(image):
        return {'type': 'image', 'source': {'type': 'url', 'url': image}}
    media_type, data = image_data_url(image)
    return {
        'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': data},
    }
