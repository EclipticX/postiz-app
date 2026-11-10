"""Detail view for editing and previewing one scene from Video Workspace."""

from app.web import _csrf, _label, _page, _scene_dialogue, _text


def _edit_form(scene, path, csrf_token, revision, asset_names, values):
    values = values or {}
    lines = scene.get('dialogue_lines') or []
    dialogue = [
        (f'Dialogue · {asset_names.get(line["speaker_id"], line["speaker_id"])}', line['text'])
        for line in lines
    ] or [('Narration', scene['dialogue'])]
    submitted = values.get('dialogue', [])
    required = ' required' if lines else ''
    dialogue_fields = ''.join(
        f'<label>{_text(label)}<textarea name="dialogue" rows="2" maxlength="4000"{required}>'
        f'{_text(submitted[index] if index < len(submitted) else text)}</textarea></label>'
        for index, (label, text) in enumerate(dialogue)
    )
    checked = ' checked' if values.get('regenerate_keyframe') else ''
    return f'''
      <form class="stack" method="post" action="{path}">
        {_csrf(csrf_token)}
        <input type="hidden" name="revision" value="{_text(revision)}">
        <label>Scene prompt
          <textarea name="action" rows="4" maxlength="4000" required>{_text(values.get('action', scene['action']))}</textarea>
        </label>
        <label>Camera
          <textarea name="camera" rows="2" maxlength="4000" required>{_text(values.get('camera', scene['camera']))}</textarea>
        </label>
        {dialogue_fields}
        <label><input type="checkbox" name="regenerate_keyframe" value="1"{checked}> Regenerate keyframe</label>
        <small>Prompt or camera edits regenerate the keyframe. Approve it in Video Workspace before the clip is generated.
          Text edits regenerate the affected audio.</small>
        <div class="actions"><button class="primary" type="submit">Save scene changes</button></div>
      </form>'''


def scene_editor_page(job, scene, tasks, csrf_token, revision, asset_names, *, editable, notice='', values=None):
    job_id = int(job['id'])
    scene_id = _text(scene['scene_id'])
    path = f'/workspace/{job_id}/scenes/{scene_id}'
    media = {task['kind']: _text(task['local_path']) for task in tasks
             if task['status'] == 'completed' and task['local_path']}
    keyframe = ''
    clip = ''
    if 'keyframe' in media:
        keyframe = f'<figure><img src="/media/{media["keyframe"]}" alt="Keyframe for {scene_id}"><figcaption>Keyframe</figcaption></figure>'
    if 'video' in media:
        clip = f'''
        <figure>
          <video controls playsinline preload="metadata" src="/media/{media['video']}" aria-label="Scene clip"></video>
          <figcaption>Scene clip</figcaption>
        </figure>'''
    previews = (f'<div class="keyframes scene-previews">{keyframe}{clip}</div>' if keyframe or clip
                else '<p class="muted">The scene has no generated preview yet.</p>')
    errors = ''.join(
        f'<li>{_label(task["kind"])} · {_text(task["error"])}</li>' for task in tasks if task['error']
    )
    fields = _edit_form(scene, path, csrf_token, revision, asset_names, values) if editable else f'''
      <p class="muted">Editing is available once scene media is ready for review.</p>
      <p class="prompt">{_text(scene['action'])}</p>
      <p>Camera · {_text(scene['camera'])}</p>
      {_scene_dialogue(scene, asset_names)}'''
    return _page('Scene Editor', '/workspace', f'''
    <p class="intro"><a href="/workspace?job={job_id}">Back to Video Workspace</a></p>
    <section class="panel stack" aria-labelledby="scene-heading">
      <div class="record-title">
        <h2 id="scene-heading">{scene_id} · {int(scene['duration'])} seconds</h2>
        <span class="status">{_label(job['status'])}</span>
      </div>
      <p class="muted">{_text(job['title'])}</p>
      {previews}
      {f'<ul class="notice">{errors}</ul>' if errors else ''}
      {fields}
    </section>''', notice)
