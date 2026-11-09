"""Postiz's public API for Studio publishing and post-level analytics."""

import json
import os
import re
import stat
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


_CHANNELS = ('youtube', 'facebook', 'instagram', 'instagram-standalone', 'tiktok')
_MAX_MP4_BYTES = 1024**3
_MAX_RESPONSE_BYTES = 4 * 1024**2


class PostizError(RuntimeError):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class PostizClient:
    def __init__(self, base_url, key):
        self._origin, self._prefix = _api_address(base_url)
        if not isinstance(key, str) or not key or any(not 33 <= ord(char) <= 126 for char in key):
            raise PostizError('Configure a valid Postiz API key in Settings.')
        self._key = key

    def channels(self):
        result = self._request('GET', '/integrations')
        if not isinstance(result, list):
            raise PostizError('Postiz did not return a channel list.')
        channels = []
        for item in result:
            if not isinstance(item, dict) or item.get('disabled'):
                continue
            identifier = item.get('identifier')
            if identifier not in _CHANNELS or not _valid_id(item.get('id')):
                continue
            name = item.get('name')
            channels.append({
                'id': item['id'], 'identifier': identifier,
                'name': name if isinstance(name, str) else identifier,
            })
        return channels

    def upload(self, path):
        path = Path(path)
        boundary = f'studio-{uuid4().hex}'
        first = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            'filename="studio-video.mp4"\r\nContent-Type: video/mp4\r\n\r\n'
        ).encode('ascii')
        last = f'\r\n--{boundary}--\r\n'.encode('ascii')
        try:
            if path.suffix.lower() != '.mp4' or not path.is_file():
                raise PostizError('Choose a local MP4 video to upload to Postiz.')
            with path.open('rb') as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _MAX_MP4_BYTES:
                    raise PostizError('Postiz requires a non-empty MP4 no larger than 1 GB.')
                header = source.read(12)
                if len(header) < 12 or header[4:8] != b'ftyp':
                    raise PostizError('The local upload does not have a valid MP4 header.')
                source.seek(0)
                result = self._request(
                    'POST', '/upload', body=_multipart_stream(source, info.st_size, first, last),
                    headers={
                        'Content-Type': f'multipart/form-data; boundary={boundary}',
                        'Content-Length': str(info.st_size + len(first) + len(last)),
                    },
                )
        except OSError:
            raise PostizError('The local MP4 could not be read for upload.') from None
        return _media(result)

    def create_post(self, channel, media, title, caption, hashtags, scheduled_at, draft=False):
        if (
            not isinstance(channel, dict) or channel.get('disabled')
            or channel.get('identifier') not in _CHANNELS or not _valid_id(channel.get('id'))
        ):
            raise PostizError('Choose an active supported Postiz channel.')
        if not isinstance(title, str) or not isinstance(caption, str) or type(draft) is not bool:
            raise PostizError('Provide valid post text and a publishing mode.')
        identifier = channel['identifier']
        if identifier == 'tiktok' and not draft:
            raise PostizError('Create a TikTok draft and select publishing options in Postiz.')
        tags = _hashtags(hashtags)
        title = title.strip()
        description = caption.strip()
        if not description and identifier == 'tiktok':
            description = title
        tagged = ' '.join(f'#{tag}' for tag in tags)
        content = '\n\n'.join(part for part in (description, tagged) if part)
        if draft:
            mode = 'draft'
        else:
            mode = 'now' if scheduled_at is None else 'schedule'
        payload = {
            'type': mode,
            'date': _post_date(scheduled_at),
            'shortLink': False,
            'tags': [],
            'posts': [{
                'integration': {'id': channel['id']},
                'settings': _post_settings(identifier, title, tags),
                'value': [{'content': content, 'image': [_media(media)]}],
            }],
        }
        result = self._request(
            'POST', '/posts', body=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json'}, creating=True,
        )
        if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
            raise PostizError('Postiz did not confirm the post ID. Check Postiz before retrying.', uncertain=True)
        item = result[0]
        if item.get('integration') != channel['id'] or not _valid_id(item.get('postId')):
            raise PostizError('Postiz did not confirm the post ID. Check Postiz before retrying.', uncertain=True)
        return item['postId']

    def analytics(self, post_id):
        if not _valid_id(post_id):
            raise PostizError('A valid Postiz post ID is required.')
        result = self._request('GET', f'/analytics/post/{post_id}?date=7')
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and result.get('missing') is True:
            return []
        raise PostizError('Postiz did not return post analytics.')

    def _request(self, method, path, *, body=None, headers=None, creating=False):
        scheme, host, port = self._origin
        connection_type = HTTPSConnection if scheme == 'https' else HTTPConnection
        connection = connection_type(host, port, timeout=30)
        try:
            connection.request(method, self._prefix + path, body=body, headers={
                'Accept': 'application/json', 'Authorization': self._key, **(headers or {}),
            })
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                _reject_response(response.status, creating)
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise _response_error(creating)
            return json.loads(raw)
        except (OSError, HTTPException, ValueError):
            raise _response_error(creating) from None
        finally:
            connection.close()


def _api_address(value):
    if not isinstance(value, str) or any(not 33 <= ord(char) <= 126 for char in value):
        raise PostizError('Configure a valid Postiz HTTP or HTTPS base URL.')
    try:
        url = urlsplit(value)
        if (
            url.scheme not in ('http', 'https') or not url.hostname or url.username is not None
            or url.query or url.fragment
        ):
            raise ValueError
        origin = (url.scheme, url.hostname, url.port)
    except ValueError:
        raise PostizError('Configure a valid Postiz HTTP or HTTPS base URL.') from None

    path = url.path.rstrip('/')
    if path.endswith('/public/v1'):
        return origin, path
    if path.endswith('/api'):
        return origin, path + '/public/v1'
    return origin, path + '/api/public/v1'


def _reject_response(status, creating):
    if status in (401, 403):
        raise PostizError('Postiz denied access. Check the API key and channel permissions.')
    if status == 429:
        raise PostizError('Postiz rate limit reached. Try again later.')
    if creating and status not in (400, 404, 405, 413, 415, 422):
        raise _response_error(True)
    raise PostizError(f'Postiz rejected the request (HTTP {status}).')


def _response_error(creating):
    if creating:
        return PostizError('Postiz did not confirm creation. Check Postiz before retrying.', uncertain=True)
    return PostizError('Postiz did not return a usable response.')


def _valid_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[a-zA-Z0-9_-]{1,200}', value))


def _media(value):
    if isinstance(value, dict) and _valid_id(value.get('id')) and isinstance(value.get('path'), str):
        path = value['path']
        if all(33 <= ord(char) <= 126 for char in path):
            try:
                url = urlsplit(path)
                absolute = url.scheme in ('http', 'https') and url.hostname and url.username is None
                relative = not url.scheme and not url.netloc and '..' not in url.path.split('/')
                if (absolute or relative) and url.path.endswith('.mp4') and not url.fragment:
                    return {'id': value['id'], 'path': path}
            except ValueError:
                pass
    raise PostizError('Postiz did not provide a valid MP4 media ID and path.')


def _multipart_stream(source, size, first, last):
    yield first
    while size:
        chunk = source.read(min(size, 64 * 1024))
        if not chunk:
            raise OSError('Local upload file ended early.')
        size -= len(chunk)
        yield chunk
    yield last


def _hashtags(values):
    if isinstance(values, str):
        values = re.split(r'[,\s]+', values)
    if not isinstance(values, (list, tuple)) or any(not isinstance(value, str) for value in values):
        raise PostizError('Provide hashtags as text or a list of hashtag strings.')
    tags = {}
    for value in values:
        for word in re.split(r'[,\s]+', value):
            tag = word.lstrip('#')
            if tag:
                tags.setdefault(tag.casefold(), tag)
    return list(tags.values())


def _post_date(scheduled_at):
    if scheduled_at is None:
        scheduled_at = datetime.now(timezone.utc)
    elif isinstance(scheduled_at, str):
        date = scheduled_at[:-1] + '+00:00' if scheduled_at.endswith('Z') else scheduled_at
        try:
            scheduled_at = datetime.fromisoformat(date)
        except ValueError:
            raise PostizError('Provide a valid scheduled date and time with a timezone.') from None
    if not isinstance(scheduled_at, datetime) or scheduled_at.utcoffset() is None:
        raise PostizError('Provide a valid scheduled date and time with a timezone.')
    return scheduled_at.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _post_settings(identifier, title, tags):
    settings = {'__type': identifier}
    if identifier == 'youtube':
        if not 2 <= len(title) <= 100:
            raise PostizError('YouTube requires a title between 2 and 100 characters.')
        settings.update({
            'title': title, 'type': 'public', 'selfDeclaredMadeForKids': 'yes',
            'tags': [{'label': tag, 'value': tag} for tag in tags],
        })
    elif identifier == 'tiktok':
        # The public create route validates the DTO even for drafts. TikTok
        # ignores privacy and interaction settings in its UPLOAD inbox mode.
        settings.update({
            'content_posting_method': 'UPLOAD', 'privacy_level': 'SELF_ONLY',
            'duet': False, 'stitch': False, 'comment': False, 'autoAddMusic': 'no',
            'brand_content_toggle': False, 'brand_organic_toggle': False, 'video_made_with_ai': True,
        })
    else:
        # The Meta adapters select Reels from one MP4 in a non-Story post.
        settings['post_type'] = 'post'
        if identifier == 'facebook' and title:
            settings['title'] = title
    return settings
