"""Idea Inbox list, create forms, and record details."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.library_view import _choices
from app.web import _csrf, _label, _page, _text


_TYPES = (('manual', 'Manual idea'), ('claude', 'AI idea'), ('schedule', 'Prompt schedule'))
_STATUSES = tuple((status, status.capitalize()) for status in ('pending', 'approved', 'later', 'rejected', 'deleted'))
_TYPE_LABELS = dict(_TYPES)
_DATE_ZONE = ZoneInfo('Asia/Dhaka')


def _record_url(record):
    collection = 'schedules' if record['kind'] == 'schedule' else 'ideas'
    return f"/{collection}/{int(record['id'])}"


def _date(value, *, with_time=False):
    if not value:
        return '—'
    date = datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(_DATE_ZONE)
    label = f'{date.day} {date:%b %Y}'
    if with_time:
        label += f' · {date:%H:%M} Asia/Dhaka'
    return f'<time datetime="{_text(value)}">{label}</time>'


def _status(status):
    return f'<span class="status inbox-status" data-status="{_text(status)}">{_label(status)}</span>'


def _inbox_row(record):
    return f'''
    <tr>
      <td><a href="{_record_url(record)}" aria-label="Open {_text(record['index'])} {_text(record['title'])}">{_text(record['index'])}</a></td>
      <td><strong>{_text(record['title'])}</strong></td>
      <td><span class="inbox-excerpt muted">{_text(record['details'])}</span></td>
      <td>{_TYPE_LABELS.get(record['source'], 'Idea')}</td>
      <td>{_date(record['created_at'])}</td>
      <td>{_date(record['publish_at'])}</td>
      <td>{_status(record['status'])}</td>
    </tr>'''


def inbox_page(records, *, query='', kind='', status='', notice='', idea_requests=()):
    rows = ''.join(_inbox_row(record) for record in records)
    if not rows:
        message = 'No matching records.' if query or kind or status else 'No ideas or prompt schedules yet.'
        rows = f'<tr><td colspan="7" class="muted">{message}</td></tr>'
    requests = ''
    if idea_requests:
        entries = ''.join(f'<li>{_label(run["status"])} {_text(run["error"] or "")}</li>' for run in idea_requests)
        requests = f'''
        <details class="panel records">
          <summary>Recent idea generation</summary>
          <ul>{entries}</ul>
        </details>'''
    content = f'''
    <section class="panel" aria-label="Inbox records">
      <form class="inbox-filters" method="get" action="/" role="search">
        <label>Search<input name="q" type="search" value="{_text(query)}" maxlength="100" placeholder="Title or index"></label>
        <label>Type{_choices('type', _TYPES, selected=kind, default='All types')}</label>
        <label>Status{_choices('status', _STATUSES, selected=status, default='All inbox records')}</label>
        <button type="submit">Search</button>
        {'<a href="/">Clear</a>' if query or kind or status else ''}
      </form>
      <div class="inbox-table-wrap" role="region" aria-label="Ideas and prompt schedules" tabindex="0">
        <table class="inbox-table" aria-label="Ideas and prompt schedules">
          <thead>
            <tr>
              <th scope="col">Index</th><th scope="col">Title</th><th scope="col">Details</th>
              <th scope="col">Type</th><th scope="col">Created date</th><th scope="col">Publish date</th><th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </section>
    {requests}'''
    return _page(
        'Idea Inbox', '/', content, notice,
        heading_action='<a class="button-link primary" href="/inbox/create">+ Create</a>',
    )


def create_page(csrf_token, *, kind='manual', values=None, notice=''):
    values = values or {}
    labels = (('manual', 'Manual idea'), ('claude', 'Generate with Claude'), ('schedule', 'Prompt schedule'))
    links = []
    for key, label in labels:
        current = ' aria-current="page"' if kind == key else ''
        links.append(f'<a href="/inbox/create?type={key}"{current}>{label}</a>')

    title = _text(values.get('title', ''))
    if kind == 'schedule':
        route, button = '/schedules', 'Save schedule draft'
        fields = f'''
        <label>Title<input name="title" value="{title}" maxlength="100" required></label>
        <label>Batch prompts<textarea name="prompts" rows="5" aria-describedby="inbox-prompts-help" required>{_text(values.get('prompts', ''))}</textarea></label>
        <small id="inbox-prompts-help">Paste up to 100 prompts, one per line.</small>
        <div class="fields">
          <label>Start date<input name="start_date" type="date" value="{_text(values.get('start_date', ''))}" required></label>
          <label>Daily time<input name="run_at" type="time" value="{_text(values.get('run_at', '09:00'))}" required></label>
          <label>Time zone<input name="timezone" value="{_text(values.get('timezone', 'Asia/Dhaka'))}" required></label>
          <label>Videos per day<input name="daily_limit" type="number" min="1" max="100" value="{_text(values.get('daily_limit', '1'))}" required></label>
        </div>'''
    elif kind == 'claude':
        route, button = '/ideas/generate', 'Generate ideas with Claude'
        fields = f'''
        <label>Trend or theme
          <textarea name="trends" rows="4" maxlength="4000" required>{_text(values.get('trends', ''))}</textarea>
        </label>'''
    else:
        route, button = '/ideas', 'Add idea'
        fields = f'''
        <label>Title <span class="muted">(optional)</span><input name="title" value="{title}"></label>
        <label>Prompt<textarea name="prompt" rows="4" required>{_text(values.get('prompt', ''))}</textarea></label>'''
    return _page('Create', '/', f'''
    <p class="intro"><a href="/">Back to inbox</a></p>
    <section class="panel">
      <nav class="inbox-create-types" aria-label="Create type">{''.join(links)}</nav>
      <form class="stack" method="post" action="{route}">
        {_csrf(csrf_token)}
        {fields}
        <div class="actions"><button type="submit" class="primary">{button}</button><a href="/">Cancel</a></div>
      </form>
    </section>''', notice)


def _schedule_details(record):
    progress = record['progress']
    counts = ''.join(f'<div><dt>{label}</dt><dd>{int(progress[key])}</dd></div>' for key, label in (
        ('pending', 'Pending'), ('queued', 'Queued'), ('waiting', 'Waiting for review'),
        ('completed', 'Completed'), ('failed', 'Failed'),
    ))
    prompts = []
    for entry in record['entries']:
        job = entry['job_id']
        job_link = f' · <a href="/workspace?job={int(job)}">Open job</a>' if job is not None else ''
        prompts.append(f'<li><span class="prompt">{_text(entry["prompt"])}</span><small>{_label(entry["status"])}{job_link}</small></li>')
    return f'''
    <section class="record" aria-labelledby="inbox-batch-heading">
      <h2 id="inbox-batch-heading">Batch prompts</h2>
      <dl class="counts">{counts}</dl>
      <ol class="scheduled-prompts">{''.join(prompts)}</ol>
    </section>'''


def _record_actions(record, csrf_token):
    status = record['status']
    url = _record_url(record)
    buttons = []
    if record['kind'] == 'schedule':
        action, field = 'status', 'status'
        if status == 'pending':
            buttons = [('active', 'Approve and activate', True), ('rejected', 'Reject', False)]
        elif status == 'approved':
            buttons = [('active', 'Resume', False)] if record['schedule_status'] == 'paused' else [('paused', 'Pause', False)]
    else:
        action, field = 'decision', 'decision'
        if status in ('pending', 'later'):
            buttons = [('accept', 'Approve', True)]
            if status == 'pending':
                buttons.append(('later', 'Later', False))
            buttons.append(('reject', 'Reject', False))
    controls = ''
    if buttons:
        controls = ''.join(
            f'<button class="{"primary" if primary else ""}" type="submit" name="{field}" value="{value}">{label}</button>'
            for value, label, primary in buttons
        )
        controls = f'''
        <form class="actions" method="post" action="{url}/{action}">
          {_csrf(csrf_token)}<input type="hidden" name="from_detail" value="1">
          {controls}
        </form>'''
    if status == 'deleted':
        controls += f'''
        <form class="actions" method="post" action="{url}/archive">
          {_csrf(csrf_token)}<button type="submit" name="action" value="restore">Restore</button>
        </form>'''
    elif status != 'approved':
        controls += f'''
        <details class="inbox-delete">
          <summary class="button-link">Delete</summary>
          <form class="actions" method="post" action="{url}/archive">
            {_csrf(csrf_token)}
            <p class="muted">You can restore this record from the Deleted filter.</p>
            <button type="submit" name="action" value="delete">Move to Deleted</button>
          </form>
        </details>'''
    return controls


def record_page(record, csrf_token, *, notice=''):
    metadata = [
        ('Index', _text(record['index'])),
        ('Type', _TYPE_LABELS.get(record['source'], 'Idea')),
        ('Status', _status(record['status'])),
        ('Created date', _date(record['created_at'])),
        ('Publish date', _date(record['publish_at'], with_time=True)),
    ]
    job_link = ''
    if record['kind'] == 'schedule':
        start = f'{_text(record["start_date"])} · {_text(record["run_at"])} {_text(record["timezone"])}'
        metadata.extend((('Production start', start), ('Videos per day', int(record['daily_limit']))))
        if record['status'] == 'approved':
            metadata.append(('Schedule state', _label(record['schedule_status'])))
        details = _schedule_details(record)
    else:
        details = f'<section class="record"><h2>Prompt</h2><p class="prompt">{_text(record["details"])}</p></section>'
        if record['job_id'] is not None:
            job_link = f'<p><a href="/workspace?job={int(record["job_id"])}">Open video job</a></p>'
    fields = ''.join(f'<div><dt class="muted">{label}</dt><dd>{value}</dd></div>' for label, value in metadata)
    return _page(record['title'], '/', f'''
    <p class="intro"><a href="/">Back to inbox</a></p>
    <article class="panel">
      <dl class="inbox-record-fields">{fields}</dl>
      {details}
      {job_link}
      {_record_actions(record, csrf_token)}
    </article>''', notice)
