"""Idea Inbox list, create forms, and record details."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.library_view import _choices
from app.web import (
    _action_menu, _csrf, _label, _menu_button, _menu_divider, _menu_link, _page, _text,
    _ICON_DELETE, _ICON_EDIT, _ICON_PAUSE, _ICON_RESTORE, _ICON_RESUME,
)


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


def create_page(csrf_token, *, kind='manual', values=None, notice='', brand_profiles=None, generated_ideas=None):
    values = values or {}
    labels = (('manual', 'Manual idea'), ('claude', 'Generate with Claude'), ('schedule', 'Prompt schedule'))
    links = []
    for key, label in labels:
        current = ' aria-current="page"' if kind == key else ''
        links.append(f'<a href="/inbox/create?type={key}"{current}>{label}</a>')

    if kind == 'claude' and generated_ideas:
        trends = values.get('trends', '')
        brand_id = values.get('brand_id', '')
        brand_name = ''
        if brand_profiles and brand_id:
            match = next((bp['name'] for bp in brand_profiles if bp['id'] == brand_id), None)
            if match:
                brand_name = f' · {match}'

        cards = []
        for i, idea in enumerate(generated_ideas, 1):
            ititle = _text(idea['title'])
            iprompt = _text(idea['prompt'])
            cards.append(f'''
            <article class="panel generated-idea-card" style="margin-bottom: 1rem; border: 1px solid var(--border); padding: 1.25rem;">
              <div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 0.5rem;">
                <h3 style="margin: 0; font-size: 1.15rem;">Option {i}: {ititle}</h3>
                <span class="muted" style="font-size: 0.85rem;">Claude generated</span>
              </div>
              <p class="prompt" style="margin: 0.75rem 0 1rem; background: var(--bg); padding: 0.75rem; border-radius: 0.5rem; line-height: 1.5;">{iprompt}</p>
              <form method="post" action="/ideas/accept" class="actions" style="display: flex; gap: 0.75rem; align-items: center;">
                {_csrf(csrf_token)}
                <input type="hidden" name="title" value="{ititle}">
                <input type="hidden" name="prompt" value="{iprompt}">
                <input type="hidden" name="brand_id" value="{_text(brand_id)}">
                <button type="submit" name="action" value="produce" class="primary">✓ Accept</button>
                <button type="submit" name="action" value="save" class="button-link">Save to inbox</button>
              </form>
            </article>''')

        return _page('Create', '/', f'''
        <p class="intro"><a href="/">Back to inbox</a></p>
        <section class="panel">
          <nav class="inbox-create-types" aria-label="Create type">{''.join(links)}</nav>
          <div style="margin: 1.25rem 0 1rem;">
            <h2 style="margin: 0 0 0.25rem; font-size: 1.3rem;">3 Generated Ideas</h2>
            <p class="muted" style="margin: 0;">Theme: <em>&ldquo;{_text(trends)}&rdquo;</em>{brand_name}</p>
          </div>
          {''.join(cards)}
          <div class="panel" style="display: flex; justify-content: space-between; align-items: center; margin-top: 1rem; background: var(--bg);">
            <form method="post" action="/ideas/generate" style="margin: 0;">
              {_csrf(csrf_token)}
              <input type="hidden" name="trends" value="{_text(trends)}">
              <input type="hidden" name="brand_id" value="{_text(brand_id)}">
              <button type="submit" class="primary">🔄 Regenerate</button>
            </form>
            <a href="/inbox/create?type=claude" class="button-link">← Change theme</a>
          </div>
        </section>''', notice)

    brand_select = ''
    if brand_profiles:
        default_brand_id = next((bp['id'] for bp in brand_profiles if bp.get('is_default')), '')
        selected_brand = values.get('brand_id') or default_brand_id
        options = ''.join(
            f'<option value="{_text(bp["id"])}"{ " selected" if bp["id"] == selected_brand else ""}>{_text(bp["name"])}{ " (Default)" if bp.get("is_default") else ""}</option>'
            for bp in brand_profiles
        )
        brand_select = f'<label>Brand / Rules<select name="brand_id">{options}</select></label>'

    title = _text(values.get('title', ''))
    if kind == 'schedule':
        route, button = '/schedules', 'Save schedule draft'
        fields = f'''
        {brand_select}
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
        route, button = '/ideas/generate', 'Generate with Claude'
        fields = f'''
        {brand_select}
        <label>Trend or theme
          <textarea name="trends" rows="4" maxlength="4000" required>{_text(values.get('trends', ''))}</textarea>
        </label>'''
    else:
        route, button = '/ideas', 'Add idea'
        fields = f'''
        {brand_select}
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


def record_page(record, csrf_token, *, notice=''):
    url = _record_url(record)
    status = record['status']
    is_schedule = (record['kind'] == 'schedule')

    menu_items = []

    if is_schedule:
        if status != 'deleted':
            menu_items.append(_menu_link('Edit schedule', f'{url}/edit', icon=_ICON_EDIT))
        if status == 'approved':
            if record.get('schedule_status') == 'paused':
                menu_items.append(_menu_button('Resume', f'{url}/status', {'status': 'active', 'from_detail': '1'}, csrf_token, icon=_ICON_RESUME))
            else:
                menu_items.append(_menu_button('Pause', f'{url}/status', {'status': 'paused', 'from_detail': '1'}, csrf_token, icon=_ICON_PAUSE))
        if status == 'deleted':
            menu_items.append(_menu_button('Restore', f'{url}/archive', {'action': 'restore'}, csrf_token, icon=_ICON_RESTORE))
        else:
            menu_items.append(_menu_divider())
            menu_items.append(_menu_button('Delete', f'{url}/archive', {'action': 'delete'}, csrf_token, danger=True, confirm_msg='Are you sure you want to move this schedule to Deleted?', icon=_ICON_DELETE))
    else:
        # Idea
        if status != 'deleted':
            menu_items.append(_menu_link('Edit idea', f'{url}/edit', icon=_ICON_EDIT))
        if status == 'approved':
            job_status = record.get('job_status')
            if job_status == 'paused':
                menu_items.append(_menu_button('Resume job', f'{url}/job-control', {'action': 'resume', 'from_detail': '1'}, csrf_token, icon=_ICON_RESUME))
            elif job_status == 'queued':
                menu_items.append(_menu_button('Pause job', f'{url}/job-control', {'action': 'pause', 'from_detail': '1'}, csrf_token, icon=_ICON_PAUSE))
        if status == 'deleted':
            menu_items.append(_menu_button('Restore', f'{url}/archive', {'action': 'restore'}, csrf_token, icon=_ICON_RESTORE))
        else:
            menu_items.append(_menu_divider())
            menu_items.append(_menu_button('Delete', f'{url}/archive', {'action': 'delete'}, csrf_token, danger=True, confirm_msg='Are you sure you want to move this idea to Deleted?', icon=_ICON_DELETE))

    action_menu_html = _action_menu(menu_items)
    heading_action = f'<div class="header-actions">{action_menu_html}</div>'

    metadata = [
        ('Index', _text(record['index'])),
        ('Type', _TYPE_LABELS.get(record['source'], 'Idea')),
        ('Status', _status(record['status'])),
        ('Created date', _date(record['created_at'])),
        ('Publish date', _date(record['publish_at'], with_time=True)),
    ]
    if is_schedule:
        start = f'{_text(record["start_date"])} · {_text(record["run_at"])} {_text(record["timezone"])}'
        metadata.extend((('Production start', start), ('Videos per day', int(record['daily_limit']))))
        if record['status'] == 'approved':
            metadata.append(('Schedule state', _label(record['schedule_status'])))
        details = _schedule_details(record)
    else:
        if record.get('job_status'):
            metadata.append(('Job state', _label(record['job_status'])))
        details = f'<section class="record"><h2>Prompt</h2><p class="prompt">{_text(record["details"])}</p></section>'

    card_actions = ''
    if is_schedule:
        if status == 'pending':
            card_actions = f'''
            <form class="actions" method="post" action="{url}/status">
              {_csrf(csrf_token)}<input type="hidden" name="from_detail" value="1">
              <button class="primary" type="submit" name="status" value="active">Approve and activate</button>
              <button type="submit" name="status" value="rejected">Reject</button>
            </form>'''
    else:
        if status in ('pending', 'later'):
            later_button = '<button type="submit" name="decision" value="later">Later</button>' if status == 'pending' else ''
            card_actions = f'''
            <form class="actions" method="post" action="{url}/decision">
              {_csrf(csrf_token)}<input type="hidden" name="from_detail" value="1">
              <button class="primary" type="submit" name="decision" value="accept">Generate video</button>
              {later_button}
              <button type="submit" name="decision" value="reject">Reject</button>
            </form>'''

    fields = ''.join(f'<div><dt class="muted">{label}</dt><dd>{value}</dd></div>' for label, value in metadata)
    return _page(
        record['title'], '/', f'''
        <article class="panel">
          <dl class="inbox-record-fields">{fields}</dl>
          {details}
          {card_actions}
        </article>''',
        notice,
        breadcrumb='<a href="/">← Back to inbox</a>',
        heading_action=heading_action,
    )


def edit_idea_page(record, csrf_token, *, notice='', brand_profiles=None, values=None):
    values = values or {}
    title = _text(values.get('title', record.get('title', '')))
    prompt = _text(values.get('prompt', record.get('details', record.get('prompt', ''))))
    brand_select = ''
    if brand_profiles:
        selected_brand = values.get('brand_id', record.get('brand_id') or '')
        options = '<option value="">None (Niche default)</option>' + ''.join(
            f'<option value="{_text(bp["id"])}"{ " selected" if bp["id"] == selected_brand else ""}>{_text(bp["name"])}{ " (Default)" if bp.get("is_default") else ""}</option>'
            for bp in brand_profiles
        )
        brand_select = f'<label>Brand / Rules<select name="brand_id">{options}</select></label>'

    job_warning = ''
    if record.get('job_id'):
        job_warning = '<p class="muted"><small>Note: Updating this idea will keep the active video job in sync (or replan if scenes were already generated).</small></p>'

    content = f'''
    <p class="intro"><a href="/ideas/{int(record['id'])}">Back to idea</a></p>
    <section class="panel">
      <h2>Edit Idea {_text(record.get('index', ''))}</h2>
      <form class="stack" method="post" action="/ideas/{int(record['id'])}/edit">
        {_csrf(csrf_token)}
        {brand_select}
        <label>Title<input name="title" value="{title}" maxlength="100"></label>
        <label>Prompt<textarea name="prompt" rows="5" required>{prompt}</textarea></label>
        {job_warning}
        <div class="actions">
          <button type="submit" class="primary">Save changes</button>
          <a class="button-link" href="/ideas/{int(record['id'])}">Cancel</a>
        </div>
      </form>
    </section>'''
    return _page(f'Edit {record["title"]}', '/', content, notice)


def edit_schedule_page(record, csrf_token, *, notice='', brand_profiles=None, values=None):
    values = values or {}
    title = _text(values.get('title', record.get('title', '')))
    start_date = _text(values.get('start_date', record.get('start_date', '')))
    run_at = _text(values.get('run_at', record.get('run_at', '09:00')))
    timezone = _text(values.get('timezone', record.get('timezone', 'Asia/Dhaka')))
    daily_limit = _text(values.get('daily_limit', str(record.get('daily_limit', '1'))))
    brand_select = ''
    if brand_profiles:
        selected_brand = values.get('brand_id', record.get('brand_id') or '')
        options = '<option value="">None (Niche default)</option>' + ''.join(
            f'<option value="{_text(bp["id"])}"{ " selected" if bp["id"] == selected_brand else ""}>{_text(bp["name"])}{ " (Default)" if bp.get("is_default") else ""}</option>'
            for bp in brand_profiles
        )
        brand_select = f'<label>Brand / Rules<select name="brand_id">{options}</select></label>'

    content = f'''
    <p class="intro"><a href="/schedules/{int(record['id'])}">Back to schedule</a></p>
    <section class="panel">
      <h2>Edit Schedule {_text(record.get('index', ''))}</h2>
      <form class="stack" method="post" action="/schedules/{int(record['id'])}/edit">
        {_csrf(csrf_token)}
        {brand_select}
        <label>Title<input name="title" value="{title}" maxlength="100" required></label>
        <div class="fields">
          <label>Start date<input name="start_date" type="date" value="{start_date}" required></label>
          <label>Daily time<input name="run_at" type="time" value="{run_at}" required></label>
          <label>Time zone<input name="timezone" value="{timezone}" required></label>
          <label>Videos per day<input name="daily_limit" type="number" min="1" max="100" value="{daily_limit}" required></label>
        </div>
        <div class="actions">
          <button type="submit" class="primary">Save changes</button>
          <a class="button-link" href="/schedules/{int(record['id'])}">Cancel</a>
        </div>
      </form>
    </section>'''
    return _page(f'Edit {record["title"]}', '/', content, notice)
