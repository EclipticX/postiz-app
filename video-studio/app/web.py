import json
from html import escape

from app.media_store import can_edit_scene, scene_revision


def _text(value):
    return escape(str(value), quote=True)


def _label(value):
    return _text(value.replace('_', ' ').capitalize())


def _page(title, active, content, notice, *, script=None, heading_action='', breadcrumb=''):
    links = (
        ('/', 'Idea Inbox'), ('/workspace', 'Video Workspace'), ('/published-posts', 'Published Posts'),
        ('/characters', 'Characters'), ('/assets', 'Asset Library'), ('/presets', 'Production Presets'),
        ('/brand-rules', 'Brand Rules'),
        ('/providers', 'Providers'), ('/watch-dog', 'Watch Dog'), ('/settings', 'Settings'),
    )
    navigation_links = []
    for path, label in links:
        current = ' aria-current="page"' if active == path else ''
        navigation_links.append(f'<a href="{path}"{current}>{label}</a>')
    navigation = ''.join(navigation_links)
    title_text = _text(title)
    if breadcrumb:
        heading = f'<div class="heading-group"><p class="intro-nav">{breadcrumb}</p><h1>{title_text}</h1></div>'
    else:
        heading = f'<h1>{title_text}</h1>'
    if heading_action or breadcrumb:
        heading = f'<div class="inbox-heading">{heading}{heading_action}</div>'
    notice_html = f'<p class="notice" role="status">{_text(notice)}</p>' if notice else ''
    script_html = f'<script src="{_text(script)}" defer></script>' if script else ''
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title_text} · Video Studio</title>
  <link rel="stylesheet" href="/studio.css">
  <script src="/studio.js" defer></script>
  {script_html}
</head>
<body{' class="providers-page-body"' if active == '/providers' else ''}>
  <header class="studio-sidebar">
    <a class="brand" href="/">Video Studio</a>
    <nav class="desktop-navigation" aria-label="Studio navigation">{navigation}</nav>
    <details class="mobile-navigation">
      <summary>Menu</summary>
      <nav aria-label="Studio navigation">{navigation}</nav>
    </details>
  </header>
  <main class="shell">
    {heading}
    {notice_html}
    {content}
  </main>
</body>
</html>'''


_ICON_EDIT = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/></svg>'
_ICON_PAUSE = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>'
_ICON_RESUME = '<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><polygon points="6 3 20 12 6 21 6 3"/></svg>'
_ICON_OPEN = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>'
_ICON_DELETE = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>'
_ICON_RESTORE = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>'


def _action_menu(items, *, aria_label='More actions'):
    if not items:
        return ''
    rendered_items = ''.join(items)
    return f'''
    <details class="action-menu">
      <summary class="action-menu-trigger" aria-label="{_text(aria_label)}" title="{_text(aria_label)}">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <circle cx="12" cy="5" r="2.2"/>
          <circle cx="12" cy="12" r="2.2"/>
          <circle cx="12" cy="19" r="2.2"/>
        </svg>
      </summary>
      <div class="action-menu-dropdown" role="menu">
        {rendered_items}
      </div>
    </details>'''


def _menu_link(label, href, *, icon=''):
    icon_html = f'<span class="menu-icon">{icon}</span>' if icon else ''
    return f'<a href="{_text(href)}" class="action-menu-item" role="menuitem">{icon_html}{_text(label)}</a>'


def _menu_button(label, action, fields, csrf_token, *, danger=False, confirm_msg='', icon='', extra_attrs=''):
    csrf = _csrf(csrf_token)
    hidden = ''.join(f'<input type="hidden" name="{_text(k)}" value="{_text(v)}">' for k, v in fields.items())
    danger_class = ' danger' if danger else ''
    confirm_attr = f' onsubmit="return confirm(\'{_text(confirm_msg)}\');"' if confirm_msg else ''
    icon_html = f'<span class="menu-icon">{icon}</span>' if icon else ''
    extra = f' {extra_attrs.strip()}' if extra_attrs else ''
    return f'''
    <form method="post" action="{action}" class="action-menu-form"{confirm_attr}>
      {csrf}{hidden}
      <button type="submit" class="action-menu-item{danger_class}" role="menuitem"{extra}>{icon_html}{_text(label)}</button>
    </form>'''


def _menu_action_button(label, *, icon='', extra_attrs='', danger=False):
    danger_class = ' danger' if danger else ''
    icon_html = f'<span class="menu-icon">{icon}</span>' if icon else ''
    extra = f' {extra_attrs.strip()}' if extra_attrs else ''
    return f'<button type="button" class="action-menu-item{danger_class}" role="menuitem"{extra}>{icon_html}{_text(label)}</button>'


def _menu_divider():
    return '<hr class="action-menu-divider" role="separator">'


def _csrf(csrf_token):
    return f'<input type="hidden" name="csrf_token" value="{_text(csrf_token)}">'


def _publishing_section(job, posts, channels, metrics, csrf_token):
    if not (posts or (job.get('preview_path') and job['status'] in ('preview_ready', 'needs_review'))):
        return ''
    job_id = int(job['id'])
    request_list = ''.join(
        f'<li>{_text(post["channel_name"])} · {_label(post["status"])} '
        f'{_text(post.get("error") or "")}</li>' for post in posts
    )
    request_html = f'<ul>{request_list}</ul>' if request_list else ''
    stats = ''.join(
        f'<li>{_text(entry["platform"])} · ' + ' · '.join(
            f'{_label(key)} {_text(value) if value is not None else "unavailable"}'
            for key, value in entry['metrics'].items()
        ) + '</li>' for entry in metrics
    )
    stats_html = f'<h4>Post analytics</h4><ul>{stats}</ul>' if stats else ''
    discover = ''
    if not channels and job.get('preview_path') and job['status'] in ('preview_ready', 'needs_review'):
        discover = f'<p><a href="/workspace?job={job_id}&amp;channels=1">Load connected Postiz channels</a></p>'
    form = ''
    if channels:
        options = ''.join(f'<option value="{_text(channel["id"])}">{_text(channel["name"])} · '
                          f'{_text(channel["identifier"])}</option>' for channel in channels)
        form = f'''
        <form class="stack" method="post" action="/workspace/{job_id}/publish">
          {_csrf(csrf_token)}
          <input type="hidden" name="preview_path" value="{_text(job['preview_path'])}">
          <label>Channel<select name="channel" required>{options}</select></label>
          <label>Title<input name="title" value="{_text(job['title'])}" maxlength="100" required></label>
          <label>Caption<textarea name="caption" rows="3"></textarea></label>
          <label>Hashtags<input name="hashtags" placeholder="#animation #story"></label>
          <div class="fields">
            <label>Schedule date and time<input name="scheduled_at" type="datetime-local"></label>
            <label>Time zone<input name="timezone" value="Asia/Dhaka"></label>
          </div>
          <small>Drafts are available for all four platforms. Studio can schedule a public YouTube post.</small>
          <div class="actions">
            <button name="mode" value="draft" type="submit">Approve draft</button>
            <button class="primary" name="mode" value="schedule" type="submit">Approve public YouTube schedule</button>
          </div>
        </form>'''
    return f'<section class="panel records"><h2>Publishing</h2>{request_html}{stats_html}{discover}{form}</section>'


def _scene_dialogue(scene, asset_names):
    lines = scene.get('dialogue_lines')
    if not isinstance(lines, list):
        return f'<p>Dialogue: {_text(scene.get("dialogue", ""))}</p>'
    if not lines:
        return '<p class="muted">No dialogue</p>'
    speakers = []
    for line in lines:
        speaker = line['speaker_id']
        label = asset_names.get(speaker) or speaker.replace('_', ' ').capitalize()
        speakers.append(f'<li><strong>{_text(label)}</strong> · {_text(line["text"])}</li>')
    return '<ul class="dialogue-lines">' + ''.join(speakers) + '</ul>'


def workspace_page(
    jobs, job=None, notice='', tasks=(), csrf_token='', *, posts=(), channels=(), metrics=(),
    assets=(), presets=(), production_options=None, production_options_open=False, brand_profiles=None,
) -> str:
    if job is not None:
        from app.library_view import production_options_section

        error = job.get('stage_error')
        error_html = f'<p class="notice">{_text(error)}</p>' if error else ''
        scenes = json.loads(job.get('plan_json') or '[]')
        asset_names = {asset['id']: asset['name'] for asset in assets}
        scene_link = 'Edit scene' if can_edit_scene(job, tasks) else 'View scene'
        scenes_html = ''.join(
            f'''<li><h4>{_text(scene['scene_id'])} · {int(scene['duration'])} seconds</h4>
              <p>{_text(scene['action'])}</p>
              <p class="muted">Camera: {_text(scene['camera'])}</p>
              {_scene_dialogue(scene, asset_names)}
              <p><a href="/workspace/{int(job['id'])}/scenes/{_text(scene['scene_id'])}"
                    aria-label="{scene_link} {_text(scene['scene_id'])}">{scene_link}</a></p></li>'''
            for scene in scenes
        )
        frames = ''.join(
            f'<figure><img loading="lazy" src="/media/{_text(task["local_path"])}" '
            f'alt="Keyframe for {_text(task["scene_id"])}"><figcaption>{_text(task["scene_id"])}</figcaption></figure>'
            for task in tasks if task['kind'] == 'keyframe' and task['local_path']
        )
        errors = ''.join(
            f'<li>{_text(task["scene_id"])} {_text(task["kind"])} · {_text(task["error"])}</li>'
            for task in tasks if task['error']
        )
        actions = ''
        if job['status'] in ('queued', 'paused', 'needs_review') and not job.get('preview_path'):
            actions += f'''
            <form method="post" action="/workspace/{int(job['id'])}/generate" class="actions">
              {_csrf(csrf_token)}
              <button class="primary" type="submit">Generate video</button>
            </form>'''
        elif job['status'] == 'review_keyframes':
            actions = f'''
            <form method="post" action="/workspace/{int(job['id'])}/generate" class="actions">
              {_csrf(csrf_token)}
              <input type="hidden" name="revision" value="{_text(scene_revision(job, tasks))}">
              <button class="primary" type="submit">Generate video</button>
            </form>'''
        if job['status'] in ('review_keyframes', 'preview_ready', 'needs_review'):
            actions += f'''
            <form method="post" action="/workspace/{int(job['id'])}/revise" class="stack records">
              {_csrf(csrf_token)}<label for="revision">Revision feedback</label>
              <textarea id="revision" name="feedback" rows="3" maxlength="4000" required></textarea>
              <button type="submit">Revise</button>
            </form>'''
        media_tasks = [task for task in tasks if task['kind'] in ('video', 'voice')]
        if (job['status'] == 'needs_review' and not job.get('preview_path') and media_tasks
                and all(task['status'] == 'completed' for task in media_tasks)):
            actions += f'''
            <form method="post" action="/workspace/{int(job['id'])}/retry-render" class="actions">
              {_csrf(csrf_token)}<button type="submit">Retry local render</button>
            </form>'''
        preview = job.get('preview_path')
        preview_html = (
            f'<h3>Preview</h3><video controls playsinline preload="metadata" src="/media/{_text(preview)}"></video>'
            if preview else ''
        )
        qa = json.loads(job.get('qa_json') or '{}')
        issues = ''.join(f'<li>{_text(issue)}</li>' for issue in qa.get('issues', []))
        qa_html = f'<h3>Visual QA · {int(qa["score"])}/100</h3><ul>{issues}</ul>' if 'score' in qa else ''

        menu_items = [
            _menu_action_button('Edit title & prompt', icon=_ICON_EDIT, extra_attrs='data-open-dialog="edit-job-dialog"'),
        ]
        if job['status'] == 'queued':
            menu_items.append(
                _menu_button('Pause job', f'/workspace/{int(job["id"])}/pause', {}, csrf_token, icon=_ICON_PAUSE)
            )
        elif job['status'] == 'paused':
            menu_items.append(
                _menu_button('Resume job', f'/workspace/{int(job["id"])}/resume', {}, csrf_token, icon=_ICON_RESUME)
            )
        menu_items.append(_menu_divider())
        menu_items.append(
            _menu_button('Delete job', f'/workspace/{int(job["id"])}/delete', {}, csrf_token, danger=True, confirm_msg='Are you sure you want to delete this job? This will cancel pending tasks and release budget reservations.', icon=_ICON_DELETE)
        )
        action_menu_html = _action_menu(menu_items)
        heading_action = f'<div class="header-actions"><span class="status">{_label(job["status"])}</span>{action_menu_html}</div>'

        plan_warning = (
            '<p class="muted"><small>Note: Updating prompt will reset generated scenes &amp; keyframes to replan from the new prompt.</small></p>'
            if job.get('plan_json') else ''
        )
        edit_dialog = f'''
        <dialog class="brand-dialog" id="edit-job-dialog">
          <div class="brand-dialog-header">
            <h2>Edit title &amp; prompt</h2>
            <button type="button" class="brand-dialog-close" data-close-dialog="edit-job-dialog" aria-label="Close dialog">&times;</button>
          </div>
          <form method="post" action="/workspace/{int(job['id'])}/edit" class="stack">
            {_csrf(csrf_token)}
            <label>Title<input name="title" value="{_text(job['title'])}" maxlength="100" required></label>
            <label>Production prompt<textarea name="prompt" rows="5" required>{_text(job['prompt'])}</textarea></label>
            {plan_warning}
            <div class="actions">
              <button type="submit" class="primary">Save changes</button>
              <button type="button" class="button-link" data-close-dialog="edit-job-dialog">Cancel</button>
            </div>
          </form>
        </dialog>'''

        content = f'''
        <section class="panel" aria-labelledby="job-heading">
          <div class="record-title">
            <h2 id="job-heading">{_text(job['title'])}</h2>
            <p class="muted" style="margin: 0;">Job #{int(job['id'])} · {_label(job['source'])}</p>
          </div>
          <h3>Production prompt</h3>
          <p class="prompt">{_text(job['prompt'])}</p>
          {error_html}
          {production_options_section(job, assets, presets, production_options, csrf_token, expanded=production_options_open, brand_profiles=brand_profiles)}
          {'<h3>Scene plan</h3><ol>' + scenes_html + '</ol>' if scenes_html else ''}
          {'<h3>Keyframes</h3><div class="keyframes">' + frames + '</div>' if frames else ''}
          {'<ul class="notice">' + errors + '</ul>' if errors else ''}
          {preview_html}
          {qa_html}
          {actions}
        </section>
        {edit_dialog}
        {_publishing_section(job, posts, channels, metrics, csrf_token)}'''
        return _page(
            job['title'], '/workspace', content, notice,
            breadcrumb='<a href="/workspace">← All video jobs</a>',
            heading_action=heading_action,
        )
    else:
        jobs_html = ''.join(
            f'''<li class="record-title">
              <a href="/workspace?job={int(item['id'])}">{_text(item['title'])}</a>
              <span class="status">{_label(item['status'])}</span>
            </li>'''
            for item in jobs
        )
        job_list = (
            f'<ul class="job-list">{jobs_html}</ul>' if jobs_html
            else '<p class="muted">Accepted ideas and prompts from active schedules will appear here.</p>'
        )
        content = f'''
        <p class="intro">Track approved prompts as they move through production.</p>
        <section class="panel" aria-labelledby="jobs-heading">
          <h2 id="jobs-heading">Video jobs</h2>
          {job_list}
        </section>'''
    return _page(
        'Video Workspace', '/workspace', content, notice,
        heading_action='<a class="button-link primary" href="/workspace/create">+ New Video</a>',
    )


def new_video_page(csrf_token, *, values=None, notice='', brand_profiles=None) -> str:
    values = values or {}
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
    prompt = _text(values.get('prompt', ''))
    content = f'''
    <p class="intro"><a href="/workspace">← Back to video workspace</a></p>
    <section class="panel">
      <form class="stack" method="post" action="/workspace/create">
        {_csrf(csrf_token)}
        {brand_select}
        <label>Title <span class="muted">(optional)</span><input name="title" value="{title}" maxlength="100" placeholder="e.g. Tavi and the Garden Seed"></label>
        <label>Production prompt<textarea name="prompt" rows="5" required placeholder="Describe the scene, actions, characters, and mood...">{prompt}</textarea></label>
        <div class="actions">
          <button type="submit" class="primary">Start Production</button>
          <a href="/workspace">Cancel</a>
        </div>
      </form>
    </section>'''
    return _page('New Video', '/workspace', content, notice)
