import json
from html import escape


def _text(value):
    return escape(str(value), quote=True)


def _label(value):
    return _text(value.replace('_', ' ').capitalize())


def _page(title, active, content, notice, *, script=None, heading_action=''):
    links = (
        ('/', 'Idea Inbox'), ('/workspace', 'Video Workspace'), ('/published-posts', 'Published Posts'),
        ('/characters', 'Characters'),
        ('/providers', 'Providers'), ('/watch-dog', 'Watch Dog'), ('/settings', 'Settings'),
    )
    navigation_links = []
    for path, label in links:
        current = ' aria-current="page"' if active == path else ''
        navigation_links.append(f'<a href="{path}"{current}>{label}</a>')
    navigation = ''.join(navigation_links)
    title = _text(title)
    heading = f'<h1>{title}</h1>'
    if heading_action:
        heading = f'<div class="inbox-heading">{heading}{heading_action}</div>'
    notice_html = f'<p class="notice" role="status">{_text(notice)}</p>' if notice else ''
    script_html = f'<script src="{_text(script)}" defer></script>' if script else ''
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} · Video Studio</title>
  <link rel="stylesheet" href="/studio.css">
  {script_html}
</head>
<body>
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
    if job.get('preview_path') and job['status'] in ('preview_ready', 'needs_review'):
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
    assets=(), presets=(), production_options=None, production_options_open=False,
) -> str:
    if job is not None:
        from app.library_view import production_options_section

        error = job.get('stage_error')
        error_html = f'<p class="notice">{_text(error)}</p>' if error else ''
        scenes = json.loads(job.get('plan_json') or '[]')
        asset_names = {asset['id']: asset['name'] for asset in assets}
        scenes_html = ''.join(
            f'''<li><h4>{_text(scene['scene_id'])} · {int(scene['duration'])} seconds</h4>
              <p>{_text(scene['action'])}</p>
              <p class="muted">Camera: {_text(scene['camera'])}</p>
              {_scene_dialogue(scene, asset_names)}</li>'''
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
        if job['status'] == 'review_keyframes':
            actions = f'''
            <form method="post" action="/workspace/{int(job['id'])}/approve-keyframes" class="actions">
              {_csrf(csrf_token)}<button class="primary" type="submit">Approve keyframes</button>
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
        qa_html = f'<h3>Visual QA · {int(qa["score"])}/100</h3><ul>{issues}</ul>' if qa else ''
        content = f'''
        <p class="intro"><a href="/workspace">All video jobs</a></p>
        <section class="panel" aria-labelledby="job-heading">
          <div class="record-title">
            <h2 id="job-heading">{_text(job['title'])}</h2>
            <span class="status">{_label(job['status'])}</span>
          </div>
          <p class="muted">Job #{int(job['id'])} · {_label(job['source'])}</p>
          <h3>Production prompt</h3>
          <p class="prompt">{_text(job['prompt'])}</p>
          {error_html}
          {production_options_section(job, assets, presets, production_options, csrf_token, expanded=production_options_open)}
          {'<h3>Scene plan</h3><ol>' + scenes_html + '</ol>' if scenes_html else ''}
          {'<h3>Keyframes</h3><div class="keyframes">' + frames + '</div>' if frames else ''}
          {'<ul class="notice">' + errors + '</ul>' if errors else ''}
          {preview_html}
          {qa_html}
          {actions}
        </section>
        {_publishing_section(job, posts, channels, metrics, csrf_token)}'''
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
    return _page('Video Workspace', '/workspace', content, notice)
