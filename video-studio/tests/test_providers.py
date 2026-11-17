import json
import unittest
from unittest.mock import Mock, patch

from app.providers import ClaudeAdapter, EvoLinkAdapter, FalAdapter, ProviderError, ProviderRequest


_KEY = 'test-secret-key'
_FRAME = 'https://v3.fal.media/files/frame.png'
_IMAGE_DATA = 'data:image/png;base64,aW1hZ2U='
_VIDEO = 'https://v3.fal.media/files/video.mp4'


def response(payload, status=200):
    result = Mock(status=status)
    result.read.return_value = json.dumps(payload).encode('utf-8')
    return result


class ProviderTests(unittest.TestCase):
    def setUp(self):
        connection_patch = patch('app.providers.common.HTTPSConnection')
        self.connection_type = connection_patch.start()
        self.addCleanup(connection_patch.stop)
        self.connection = self.connection_type.return_value

    def payload(self, index=0):
        request = self.connection.request.call_args_list[index]
        return json.loads(request.kwargs['body'])

    def test_claude_structured_vision_and_usage(self):
        schema = {'type': 'object', 'additionalProperties': False, 'properties': {}}
        usage = {'input_tokens': 32, 'output_tokens': 4}
        self.connection.getresponse.return_value = response({
            'content': [{'type': 'thinking'}, {'type': 'text', 'text': '{}'}],
            'stop_reason': 'end_turn', 'usage': usage,
        })
        result = ClaudeAdapter(_KEY, model='claude-test').submit(ProviderRequest(
            'Review frame', reference_image=_IMAGE_DATA, reference_images=(_FRAME,),
            output_schema=schema, system='Use the QA rubric',
        ))

        self.connection_type.assert_called_once_with('api.anthropic.com', timeout=90)
        request = self.connection.request.call_args
        self.assertEqual(request.args, ('POST', '/v1/messages'))
        self.assertEqual(request.kwargs['headers']['x-api-key'], _KEY)
        payload = self.payload()
        self.assertEqual(payload['model'], 'claude-test')
        self.assertEqual(payload['system'], 'Use the QA rubric')
        self.assertEqual(payload['output_config']['format'], {'type': 'json_schema', 'schema': schema})
        first_image, second_image, prompt = payload['messages'][0]['content']
        self.assertEqual(first_image['source'], {
            'type': 'base64', 'media_type': 'image/png', 'data': 'aW1hZ2U=',
        })
        self.assertEqual(second_image['source'], {'type': 'url', 'url': _FRAME})
        self.assertEqual(prompt['text'], 'Review frame')
        self.assertEqual((result.status, result.data, result.usage), ('completed', {}, usage))
        self.connection.close.assert_called_once()

    def test_claude_handles_refusal_and_invalid_structured_response(self):
        for stop_reason, text, message in (
            ('refusal', _KEY, 'declined'), ('max_tokens', _KEY, 'token limit'),
            ('end_turn', _KEY, 'structured JSON'),
        ):
            with self.subTest(stop_reason=stop_reason):
                self.connection.getresponse.return_value = response({
                    'stop_reason': stop_reason, 'content': [{'type': 'text', 'text': text}],
                })
                with self.assertRaisesRegex(ProviderError, message) as caught:
                    ClaudeAdapter(_KEY).submit(ProviderRequest('Prompt', output_schema={}))
                self.assertNotIn(_KEY, str(caught.exception))
                self.assertFalse(caught.exception.retryable)

    def test_fal_veo_resumes_polling_from_task_id_and_ignores_tracking_urls(self):
        self.connection.getresponse.side_effect = [
            response({'request_id': 'task-42', 'status_url': 'https://untrusted.example/status'}),
            response({'status': 'IN_QUEUE'}),
            response({'status': 'COMPLETED', 'response_url': 'https://untrusted.example/result'}),
            response({'video': {'url': _VIDEO}}),
        ]
        queued = FalAdapter(_KEY, 'fal-veo').submit(ProviderRequest(
            'Animate the approved frame', reference_image=_FRAME, duration=4,
        ))
        self.assertEqual((queued.status, queued.task_id), ('pending', 'task-42'))
        resumed = FalAdapter(_KEY, 'fal-veo')
        self.assertEqual(resumed.poll(queued.task_id).status, 'pending')
        result = resumed.poll(queued.task_id)
        self.assertEqual((result.status, result.url, result.task_id), ('completed', _VIDEO, 'task-42'))
        calls = self.connection.request.call_args_list
        self.assertEqual([call.args for call in calls], [
            ('POST', '/fal-ai/veo3.1/image-to-video'),
            ('GET', '/fal-ai/veo3.1/requests/task-42/status'),
            ('GET', '/fal-ai/veo3.1/requests/task-42/status'),
            ('GET', '/fal-ai/veo3.1/requests/task-42'),
        ])
        self.assertEqual(calls[0].kwargs['headers']['X-Fal-No-Retry'], '1')
        self.assertEqual(calls[0].kwargs['headers']['Authorization'], f'Key {_KEY}')
        self.assertEqual(self.payload(), {
            'prompt': 'Animate the approved frame', 'aspect_ratio': '9:16', 'resolution': '720p',
            'image_url': _FRAME, 'duration': '4s', 'generate_audio': False, 'auto_fix': False,
        })
        self.assertTrue(all(call.args == ('queue.fal.run',) for call in self.connection_type.call_args_list))

    def test_fal_routes_translate_inputs_and_outputs(self):
        cases = (
            ('fal-nano-banana-pro', ProviderRequest('Draw'), 'fal-ai/nano-banana-pro', {},
             {'images': [{'url': _FRAME}]}, _FRAME),
            ('fal-nano-banana-pro', ProviderRequest('Draw', reference_images=(_IMAGE_DATA,)),
             'fal-ai/nano-banana-pro/edit', {'image_urls': [_IMAGE_DATA]},
             {'images': [{'url': _FRAME}]}, _FRAME),
            ('fal-kling', ProviderRequest('Animate', reference_image=_FRAME, duration=5,
                                         reference_images=(_IMAGE_DATA,)),
             'kling/v4/flash/reference-to-video',
             {'start_image_url': _FRAME, 'duration': 5, 'image_urls': [_IMAGE_DATA]},
             {'video': {'url': _VIDEO}}, _VIDEO),
            ('fal-elevenlabs', ProviderRequest('Narrate', voice='Aria'),
             'fal-ai/elevenlabs/tts/turbo-v2.5',
             {'text': 'Narrate', 'voice': 'Aria', 'enable_safety_checker': True},
             {'audio': {'url': 'https://v3.fal.media/voice.mp3'}}, 'https://v3.fal.media/voice.mp3'),
        )
        for route, request, path, expected_payload, media, url in cases:
            with self.subTest(route=route, path=path):
                self.connection.reset_mock()
                self.connection.getresponse.side_effect = [
                    response({'request_id': 'queued-1'}), response({'status': 'COMPLETED'}), response(media),
                ]
                adapter = FalAdapter(_KEY, route)
                result = adapter.poll(adapter.submit(request).task_id)
                self.assertEqual(result.url, url)
                self.assertEqual(self.connection.request.call_args_list[0].args, ('POST', f'/{path}'))
                self.assertEqual(
                    self.connection.request.call_args_list[2].args,
                    ('GET', f'/{"/".join(path.split("/")[:2])}/requests/queued-1'),
                )
                payload = self.payload()
                for key, value in expected_payload.items():
                    self.assertEqual(payload[key], value)

    def test_video_rejects_unsupported_requests_before_submission(self):
        veo = FalAdapter(_KEY, 'fal-veo')
        kling = FalAdapter(_KEY, 'fal-kling')
        for adapter, request in (
            (veo, ProviderRequest('Animate', duration=4)),
            (veo, ProviderRequest('Animate', reference_image=_FRAME, duration=5)),
            (kling, ProviderRequest('Animate', reference_image=_FRAME, resolution='1080p')),
            (EvoLinkAdapter(_KEY), ProviderRequest('Animate', reference_image=_FRAME, duration=3)),
        ):
            with self.subTest(request=request):
                with self.assertRaises(ProviderError):
                    adapter.submit(request)
        with self.assertRaises(ProviderError):
            veo.poll('../other-task')
        self.connection_type.assert_not_called()

    def test_seedance_flash_submits_text_and_reference_video_and_resumes_by_id(self):
        voice = 'data:audio/wav;base64,UklGRg=='
        self.connection.getresponse.side_effect = [
            response({'request_id': 'text-1'}), response({'request_id': 'reference-1'}),
            response({'status': 'COMPLETED'}), response({'video': {'url': _VIDEO}}),
        ]
        adapter = FalAdapter(_KEY, 'fal-seedance-25-flash')
        text = adapter.submit(ProviderRequest('Create a film', duration=30, generate_audio=True))
        reference = adapter.submit(ProviderRequest(
            'Use @Image1 and @Audio1', duration=6, resolution='480p', aspect_ratio='16:9',
            reference_images=(_FRAME,), audio_references=(voice,), generate_audio=True,
        ))
        resumed = FalAdapter(_KEY, 'fal-seedance-25-flash').poll(reference.task_id)

        self.assertEqual((text.task_id, resumed.task_id, resumed.url), ('text-1', 'reference-1', _VIDEO))
        self.assertEqual([call.args for call in self.connection.request.call_args_list], [
            ('POST', '/bytedance/seedance-2.5/flash/text-to-video'),
            ('POST', '/bytedance/seedance-2.5/flash/reference-to-video'),
            ('GET', '/bytedance/seedance-2.5/requests/reference-1/status'),
            ('GET', '/bytedance/seedance-2.5/requests/reference-1'),
        ])
        self.assertEqual(self.payload(), {
            'prompt': 'Create a film', 'duration': 30, 'resolution': '720p', 'aspect_ratio': '9:16',
            'generate_audio': True, 'codec': 'H264',
        })
        self.assertEqual(self.payload(1)['image_urls'], [_FRAME])
        self.assertEqual(self.payload(1)['audio_urls'], [voice])

    def test_seedance_flash_rejects_invalid_audio_references_before_submission(self):
        adapter = FalAdapter(_KEY, 'fal-seedance-25-flash')
        voice = 'https://assets.example.test/sample.mp3'
        for request in (
            ProviderRequest('Create a film', duration=31),
            ProviderRequest('Voice', duration=8, audio_references=(voice,), generate_audio=True),
            ProviderRequest('Voice', duration=8, reference_images=(_FRAME,), audio_references=(voice,)),
            ProviderRequest('Voice', duration=8, reference_images=(_FRAME,), generate_audio=True,
                            audio_references=('data:audio/wav;base64,invalid data',)),
        ):
            with self.subTest(request=request), self.assertRaises(ProviderError):
                adapter.submit(request)
        self.connection_type.assert_not_called()

    def test_image_to_video_can_request_native_audio(self):
        self.connection.getresponse.side_effect = [
            response({'request_id': 'video-1'}), response({'id': 'video-2'}),
        ]
        request = ProviderRequest('Speak', reference_image=_FRAME, duration=4, generate_audio=True)
        FalAdapter(_KEY, 'fal-veo').submit(request)
        EvoLinkAdapter(_KEY).submit(request)
        self.assertTrue(self.payload()['generate_audio'])
        self.assertTrue(self.payload(1)['generate_audio'])

    def test_fal_elevenlabs_sends_supported_voice_controls_only_when_set(self):
        self.connection.getresponse.return_value = response({'request_id': 'queued-voice'})
        adapter = FalAdapter(_KEY, 'fal-elevenlabs')

        adapter.submit(ProviderRequest('Narrate', voice='Aria', voice_speed=1.1, voice_expressiveness=0.35))
        adapter.submit(ProviderRequest('Legacy narration'))

        self.assertEqual(self.payload(), {
            'text': 'Narrate', 'voice': 'Aria', 'enable_safety_checker': True, 'speed': 1.1, 'style': 0.35,
        })
        self.assertNotIn('speed', self.payload(1))
        self.assertNotIn('style', self.payload(1))

    def test_fal_elevenlabs_rejects_unsupported_voice_controls_before_submission(self):
        adapter = FalAdapter(_KEY, 'fal-elevenlabs')
        for options in ({'voice_speed': 0.6}, {'voice_speed': True}, {'voice_expressiveness': 1.1}):
            with self.subTest(options=options), self.assertRaises(ProviderError):
                adapter.submit(ProviderRequest('Narrate', **options))

        self.connection_type.assert_not_called()

    def test_fal_failed_status_classifies_transient_and_safety_errors(self):
        adapter = FalAdapter(_KEY, 'fal-kling')
        for code, retryable in [('runner_server_error', True), ('content_policy_violation', False)]:
            with self.subTest(code=code):
                self.connection.request.reset_mock()
                self.connection.getresponse.return_value = response({
                    'status': 'COMPLETED', 'error': _KEY, 'error_type': code,
                })
                with self.assertRaises(ProviderError) as caught:
                    adapter.poll('fal-1')
                self.assertTrue(caught.exception.terminal)
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertNotIn(_KEY, str(caught.exception))
                self.connection.request.assert_called_once()

    def test_evolink_uploads_a_local_frame_and_polls_to_completion(self):
        uploaded = 'https://files.evolink.ai/frame.png'
        usage = {'credits_used': 10, 'cost': {'usd': 0.148}}
        self.connection.getresponse.side_effect = [
            response({'success': True, 'data': {'file_url': uploaded}}),
            response({'id': 'task-unified-1', 'status': 'pending'}),
            response({'status': 'processing'}),
            response({'status': 'completed', 'results': [_VIDEO], 'usage': usage}),
        ]
        adapter = EvoLinkAdapter(_KEY)
        queued = adapter.submit(ProviderRequest('Animate', reference_image=_IMAGE_DATA, duration=5))
        self.assertEqual((queued.status, queued.task_id), ('pending', 'task-unified-1'))
        self.assertEqual(adapter.poll(queued.task_id).status, 'pending')
        result = adapter.poll(queued.task_id)
        self.assertEqual((result.status, result.url, result.usage), ('completed', _VIDEO, usage))
        self.assertEqual(self.payload(), {'base64_data': _IMAGE_DATA})
        self.assertEqual(self.payload(1), {
            'model': 'seedance-2.0-image-to-video', 'prompt': 'Animate', 'image_urls': [uploaded],
            'duration': 5, 'quality': '720p', 'aspect_ratio': '9:16',
            'generate_audio': False, 'content_filter': True,
        })
        self.assertEqual([call.args for call in self.connection_type.call_args_list], [
            ('files-api.evolink.ai',), ('api.evolink.ai',), ('api.evolink.ai',), ('api.evolink.ai',),
        ])
        self.assertEqual([call.args for call in self.connection.request.call_args_list], [
            ('POST', '/api/v1/files/upload/base64'), ('POST', '/v1/videos/generations'),
            ('GET', '/v1/tasks/task-unified-1'), ('GET', '/v1/tasks/task-unified-1'),
        ])

    def test_evolink_reuses_https_frame_and_classifies_confirmed_failure(self):
        self.connection.getresponse.side_effect = [
            response({'id': 'task-unified-1', 'status': 'pending'}),
            response({'status': 'failed', 'error': {'code': 'generation_timeout', 'message': _KEY}}),
        ]
        adapter = EvoLinkAdapter(_KEY)
        queued = adapter.submit(ProviderRequest('Animate', reference_image=_FRAME, duration=4))
        self.assertEqual(self.payload()['image_urls'], [_FRAME])
        with self.assertRaises(ProviderError) as caught:
            adapter.poll(queued.task_id)
        self.assertTrue(caught.exception.terminal)
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn(_KEY, str(caught.exception))

    def test_malformed_error_codes_are_terminal_without_retry(self):
        for adapter, payload in (
            (FalAdapter(_KEY, 'fal-veo'), {'status': 'COMPLETED', 'error_type': [_KEY]}),
            (EvoLinkAdapter(_KEY), {'status': 'failed', 'error': {'code': {'secret': _KEY}}}),
        ):
            with self.subTest(adapter=type(adapter).__name__):
                self.connection.getresponse.return_value = response(payload)
                with self.assertRaises(ProviderError) as caught:
                    adapter.poll('task-42')
                self.assertTrue(caught.exception.terminal)
                self.assertFalse(caught.exception.retryable)
                self.assertNotIn(_KEY, str(caught.exception))

    def test_errors_keep_secrets_and_ambiguous_submissions_out_of_retries(self):
        adapter = FalAdapter(_KEY, 'fal-elevenlabs')
        for status, code, retryable, uncertain in (
            (302, None, False, False),
            (401, 'access_denied', True, False),
            (402, 'insufficient_credit', True, False),
            (403, 'access_denied', True, False),
            (429, 'rate_limited', True, False),
            (503, None, False, True),
        ):
            with self.subTest(status=status):
                rejected = response({'error': _KEY}, status)
                self.connection.getresponse.return_value = rejected
                with self.assertRaises(ProviderError) as caught:
                    adapter.submit(ProviderRequest('Narrate'))
                error = caught.exception
                self.assertEqual((error.retryable, error.uncertain, error.terminal), (retryable, uncertain, False))
                self.assertEqual(error.warning_code, code)
                self.assertNotIn(_KEY, str(error))
                rejected.read.assert_not_called()
        self.connection.getresponse.side_effect = TimeoutError(_KEY)
        with self.assertRaises(ProviderError) as submit_error:
            adapter.submit(ProviderRequest('Narrate'))
        self.assertTrue(submit_error.exception.uncertain)
        self.assertFalse(submit_error.exception.retryable)
        with self.assertRaises(ProviderError) as poll_error:
            adapter.poll('task-42')
        self.assertFalse(poll_error.exception.terminal)
        self.assertTrue(poll_error.exception.retryable)
        self.assertNotIn(_KEY, str(poll_error.exception))

    def test_missing_submission_id_is_uncertain(self):
        self.connection.getresponse.return_value = response({'status': 'queued'})
        with self.assertRaises(ProviderError) as caught:
            FalAdapter(_KEY, 'fal-elevenlabs').submit(ProviderRequest('Narrate'))
        self.assertTrue(caught.exception.uncertain)
        self.assertFalse(caught.exception.retryable)


if __name__ == '__main__':
    unittest.main()
