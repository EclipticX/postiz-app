"""Published video metrics and per-channel post details."""

from app.inbox_view import _date
from app.library_view import _choices
from app.web import _page, _text


_PLATFORMS = (
    ('youtube', 'YouTube'),
    ('instagram', 'Instagram'),
    ('facebook', 'Facebook'),
    ('tiktok', 'TikTok'),
)
_METRICS = (
    ('views', 'Views'),
    ('engagement', 'Engagement'),
    ('likes', 'Likes'),
    ('comments', 'Comments'),
    ('follows', 'Follows gained'),
)
_ACTIVE = '/published-posts'


def _metric(value, *, partial=False):
    if value is None:
        return '—'
    mark = '<sup title="Partial total">*</sup>' if partial else ''
    return f'{value:,}{mark}'


def _metric_headers():
    return ''.join(f'<th scope="col" class="published-number">{label}</th>' for _, label in _METRICS)


def _metric_cells(metrics, partial=()):
    return ''.join(
        f'<td class="published-number" data-label="{label}">{_metric(metrics[key], partial=key in partial)}</td>'
        for key, label in _METRICS
    )


def _published_row(record):
    platforms = dict.fromkeys(post['platform_label'] for post in record['platforms'])
    names = ', '.join(platforms)
    return f'''
    <tr>
      <td><a href="{_ACTIVE}/{int(record['id'])}" aria-label="Open {_text(record['index'])} {_text(record['title'])}">{_text(record['index'])}</a></td>
      <td><strong>{_text(record['title'])}</strong></td>
      <td>Short</td>
      <td>{_text(names)}</td>
      <td>{_date(record['release_at'])}</td>
      {_metric_cells(record['metrics'], record['partial_metrics'])}
    </tr>'''


def published_posts_page(records, *, query='', platform=''):
    rows = ''.join(_published_row(record) for record in records)
    if not rows:
        message = 'No matching published videos.' if query or platform else 'Published videos will appear after Studio confirms publication.'
        rows = f'<tr><td colspan="10" class="muted">{message}</td></tr>'
    count = len(records)
    return _page('Published Posts', _ACTIVE, f'''
    <p class="intro">Performance by video across platforms.</p>
    <section class="panel" aria-label="Published videos">
      <form class="published-filters" method="get" action="{_ACTIVE}" role="search">
        <label>Search<input name="q" type="search" value="{_text(query)}" maxlength="100" placeholder="Title or index"></label>
        <label>Platform{_choices('platform', _PLATFORMS, selected=platform, default='All platforms')}</label>
        <button type="submit">Search</button>
        {f'<a href="{_ACTIVE}">Clear</a>' if query or platform else ''}
      </form>
      <div class="published-table-wrap" role="region" aria-label="Published video performance" tabindex="0">
        <table class="published-table published-list-table" aria-label="Published video performance">
          <thead><tr>
            <th scope="col">Index</th><th scope="col">Title</th><th scope="col">Type</th>
            <th scope="col">Platforms</th><th scope="col">Release date</th>{_metric_headers()}
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      <footer class="published-foot muted">
        <span>{count} published {'video' if count == 1 else 'videos'}</span>
        <span>Release dates use the scheduled publication time.</span>
        <span>— Unavailable · * Partial total</span>
      </footer>
    </section>''', '')


def _post_overview(record, selected):
    metrics = ''.join(
        f'<div><dt class="muted">{label}</dt><dd>{_metric(record["metrics"][key], partial=key in record["partial_metrics"])}</dd></div>'
        for key, label in _METRICS
    )
    tags = ' '.join(f'#{tag.lstrip("#")}' for tag in selected['hashtags'])
    caption = '\n\n'.join(part for part in (selected['caption'], tags) if part)
    return f'''
    <section class="panel" aria-labelledby="published-overview">
      <h2 id="published-overview">Post overview</h2>
      <dl class="published-kpis" aria-label="Combined post totals">{metrics}</dl>
      <small>— Unavailable · * Partial total</small>
      <div class="published-caption">
        <h3>Caption <span class="muted">· {_text(selected['channel_name'])}</span></h3>
        <p class="prompt">{_text(caption) if caption else 'No caption.'}</p>
      </div>
    </section>'''


def _post_history(record, selected, metric):
    name = dict(_METRICS)[metric]
    history = selected['history']
    values = [item['value'] for item in history if item['value'] is not None]
    chart = f'<p class="muted">No {_text(name.lower())} data reported for this channel.</p>'
    if values:
        maximum = max(1, max(values))
        points = []
        for item in history:
            value = item['value']
            day = _date(item['collected_at'])
            count = _metric(value)
            bar = (
                f'<progress value="{value}" max="{maximum}" aria-hidden="true"></progress>'
                if value is not None else '<span></span>'
            )
            points.append(f'<li>{day}{bar}<span class="published-number">{count}</span></li>')
        chart = f'''
        <figure class="published-chart">
          <figcaption class="muted">{_text(name)} totals by snapshot date</figcaption>
          <ol>{''.join(points)}</ol>
        </figure>'''
    channels = tuple(
        (post['id'], f"{post['platform_label']} · {post['channel_name']}") for post in record['platforms']
    )
    return f'''
    <section class="panel" aria-labelledby="published-performance">
      <h2 id="published-performance">Performance over time</h2>
      <form class="published-filters" method="get" action="{_ACTIVE}/{int(record['id'])}">
        <label>Platform and channel{_choices('channel', channels, selected=selected['id'])}</label>
        <label>Metric{_choices('metric', _METRICS, selected=metric)}</label>
        <button type="submit">Show</button>
      </form>
      {chart}
    </section>'''


def _platform_breakdown(record, selected):
    job_id = int(record['id'])
    rows = ''.join(f'''
    <tr>
      <th scope="row">{_text(post['platform_label'])}<br><small>{_text(post['channel_name'])}</small></th>
      <td data-label="Release date">{_date(post['scheduled_at'])}</td>
      {_metric_cells(post['metrics'])}
      <td data-label="Original post">
        <a href="{_ACTIVE}/{job_id}/original/{int(post['id'])}" aria-label="Open original post on {_text(post['channel_name'])}">Open post</a>
      </td>
    </tr>''' for post in record['platforms'])
    synced = f'Last synced · {_date(selected["collected_at"], with_time=True)}' if selected['collected_at'] else 'Waiting for analytics.'
    error = f'<p class="notice" role="status">{_text(selected["analytics_error"])}</p>' if selected['analytics_error'] else ''
    return f'''
    <section class="panel" aria-labelledby="published-platforms">
      <h2 id="published-platforms">Platform breakdown</h2>
      <div class="published-table-wrap" role="region" aria-labelledby="published-platforms" tabindex="0">
        <table class="published-table published-platform-table" aria-labelledby="published-platforms">
          <thead><tr>
            <th scope="col">Platform</th><th scope="col">Release date</th>{_metric_headers()}<th scope="col">Original post</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      <footer class="published-foot muted">
        <span>{_text(selected['channel_name'])} · {synced}</span>
        <span>Release dates use the scheduled publication time.</span>
        <span>Engagement = likes + comments</span>
      </footer>
      {error}
    </section>'''


def published_post_page(record, selected, *, metric='engagement', notice=''):
    preview = selected['preview_path']
    player = (
        f'<video controls playsinline preload="metadata" src="/media/{_text(preview)}" aria-label="Studio video preview"></video>'
        if preview else '<p class="muted">The local video is unavailable.</p>'
    )
    return _page(record['title'], _ACTIVE, f'''
    <p class="intro"><a href="{_ACTIVE}">Back to Published Posts</a></p>
    <p class="muted">{_text(record['index'])} · Short · First release {_date(record['release_at'])}</p>
    <div class="published-detail">
      <div class="published-media-grid">
        <section class="panel published-video" aria-labelledby="published-video">
          <h2 id="published-video">Video preview</h2>
          {player}
        </section>
        <div class="published-analysis">
          {_post_overview(record, selected)}
          {_post_history(record, selected, metric)}
        </div>
      </div>
      {_platform_breakdown(record, selected)}
    </div>''', notice)
