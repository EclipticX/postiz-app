"""Character library, profile, and edit screens."""

from urllib.parse import quote

from app.asset_library import REFERENCE_ANGLES
from app.library_view import _AUDIO_TYPES, _ID_PATTERN, _IMAGE_TYPES, _choices, _identifier_input, _voice_section
from app.web import _csrf, _page, _text


def _path_segment(value):
    return _text(quote(str(value), safe=''))


def _reference_url(asset, reference):
    return f'/asset-reference/{_path_segment(asset["id"])}/{_path_segment(reference["id"])}'


def _readiness(asset):
    references = asset['references']
    if any(not reference.get('variant_id') for reference in references):
        return 'Ready for keyframes'
    if references:
        return 'Ready with outfit'
    return 'Needs a reference image'


def characters_page(assets, query=''):
    items = []
    for asset in assets:
        if asset['kind'] != 'character' or query.casefold() not in asset['name'].casefold():
            continue
        references = asset['references']
        primary = next((ref for ref in references if ref['primary'] and not ref.get('variant_id')), None)
        primary = primary or next((ref for ref in references if ref['primary']), None)
        if primary is None and references:
            primary = references[0]
        thumbnail = '<span class="character-thumbnail" aria-hidden="true"></span>'
        if primary:
            thumbnail = f'<img class="character-thumbnail" src="{_reference_url(asset, primary)}" alt="" loading="lazy">'
        items.append(f'''
        <li>
          <a class="character-list-link" href="/characters/{_path_segment(asset['id'])}">
            {thumbnail}
            <span><strong>{_text(asset['name'])}</strong><br><small>{_text(asset['id'])}</small></span>
            <span class="status">{_readiness(asset)}</span>
          </a>
        </li>''')
    empty_message = 'No characters match that name.' if query else 'Add a character to organize their look and voice.'
    clear_search = '<a href="/characters">Clear</a>' if query else ''
    results = '<ul class="job-list character-list">' + ''.join(items) + '</ul>' if items else f'<p>{empty_message}</p>'
    return _page('Characters', '/characters', f'''
    <section class="panel stack" aria-label="Character library">
      <div class="character-toolbar">
        <form class="character-search" method="get" action="/characters" role="search">
          <label>Search by name<input name="q" type="search" value="{_text(query)}" maxlength="100"></label>
          <button type="submit">Search</button>
          {clear_search}
        </form>
        <a class="button-link primary" href="/characters/new/profile">Add Character</a>
      </div>
      {results}
    </section>''', '')


def _color_input(value, label):
    return f'''
    <div class="character-color fields" data-color-field>
      <label>{label}<input type="color" value="{_text(value or '#000000')}" data-color-picker></label>
      <label>HEX code
        <input name="dress_color" value="{_text(value)}" placeholder="#7C3AED" maxlength="7"
               pattern="#[0-9A-Fa-f]{{6}}" autocomplete="off" data-color-hex>
      </label>
    </div>'''


def _details_form(asset, csrf_token, values):
    creating = asset is None
    base = '/characters' if creating else f'/characters/{_path_segment(asset["id"])}'
    initial = asset or {}
    values = values or {}
    identifier = f'<p><strong>Character ID</strong> <code>{_text(initial.get("id", ""))}</code></p>'
    if creating:
        identifier = f'''
        <label>Character ID
          <input name="asset_id" value="{_text(values.get('asset_id', ''))}" maxlength="64"
                 pattern="{_ID_PATTERN}" placeholder="mother" required>
        </label>
        <small>Use lowercase letters, numbers, and underscores. Start with a letter.
          The ID stays fixed after creation.</small>'''
    outfits = _outfits(asset, base, csrf_token) if asset is not None else ''
    return f'''
    <section class="panel" aria-labelledby="character-overview">
      <h2 id="character-overview">Overview</h2>
      <form class="stack" method="post" action="{base if creating else base + '/edit'}">
        {_csrf(csrf_token)}
        {identifier}
        <label>Character name
          <input name="name" value="{_text(values.get('name', initial.get('name', '')))}" maxlength="100" required>
        </label>
        <label>Description
          <textarea name="description" rows="4" maxlength="2000" required>{_text(values.get('description', initial.get('description', '')))}</textarea>
        </label>
        {_color_input(values.get('dress_color', initial.get('dress_color', '')), 'Base dress color')}
        <div class="actions">
          <button class="primary" type="submit">{'Create character' if creating else 'Save overview'}</button>
          <a href="{base}">Cancel</a>
        </div>
      </form>
      {outfits}
    </section>'''


def _outfits(asset, base, csrf_token=None):
    items = []
    for variant in asset['variants']:
        color = variant.get('dress_color') or ''
        content = f'<p>Dress color <code>{_text(color)}</code></p>' if color else ''
        if csrf_token is not None:
            content = f'''
            <form class="stack" method="post" action="{base}/outfit-color">
              {_csrf(csrf_token)}
              <input type="hidden" name="variant_id" value="{_text(variant['id'])}">
              {_color_input(color, 'Outfit dress color')}
              <button type="submit">Save outfit color</button>
            </form>'''
        items.append(f'''
        <div class="record">
          <h3>{_text(variant['name'])}</h3>
          <p class="prompt">{_text(variant['description'])}</p>
          {content}
        </div>''')
    if csrf_token is not None:
        items.append(f'''
        <details class="record">
          <summary>Add outfit</summary>
          <form class="stack library-body" method="post" action="{base}/variants">
            {_csrf(csrf_token)}
            <div class="fields">
              {_identifier_input('variant_id', 'Outfit ID')}
              <label>Name<input name="name" maxlength="100" required></label>
            </div>
            <label>Description<textarea name="description" rows="2" maxlength="2000" required></textarea></label>
            {_color_input('', 'Outfit dress color')}
            <button type="submit">Add outfit</button>
          </form>
        </details>''')
    return '<div><h3>Outfits</h3>' + ''.join(items) + '</div>' if items else ''


def _reference_options(asset, reference=None):
    reference = reference or {}
    angles = [(angle, angle.replace('-', ' ').capitalize()) for angle in REFERENCE_ANGLES]
    variants = [(variant['id'], variant['name']) for variant in asset['variants']]
    angle_choice = _choices('angle', angles, selected=reference.get('angle', 'front'))
    outfit_choice = _choices('variant_id', variants, selected=reference.get('variant_id'), default='Base appearance')
    checked = ' checked' if reference.get('primary') else ''
    return f'''
    <div class="fields">
      <label>Angle or view{angle_choice}</label>
      <label>Outfit{outfit_choice}</label>
    </div>
    <label><input type="checkbox" name="primary" value="yes"{checked}> Primary for this appearance</label>'''


def _gallery(asset, base, csrf_token=None):
    figures = []
    variants = {variant['id']: variant['name'] for variant in asset['variants']}
    for reference in asset['references']:
        url = _reference_url(asset, reference)
        angle = reference['angle'].replace('-', ' ').capitalize()
        label = f'{asset["name"]} · {angle} reference'
        outfit = variants.get(reference.get('variant_id'))
        primary = '<span class="status">Primary</span>' if reference['primary'] else ''
        settings = ''
        if csrf_token is not None:
            settings = f'''
            <details>
              <summary>Edit image labels</summary>
              <form class="stack library-body" method="post" action="{base}/references/{_path_segment(reference['id'])}">
                {_csrf(csrf_token)}
                {_reference_options(asset, reference)}
                <button type="submit">Save image details</button>
              </form>
            </details>'''
        figures.append(f'''
        <figure>
          <a href="{url}" data-image-preview>
            <img src="{url}" alt="{_text(label)}" loading="lazy">
          </a>
          <figcaption>{_text(angle)}{f' · {_text(outfit)}' if outfit else ''} {primary}</figcaption>
          {settings}
        </figure>''')
    gallery = '<p class="muted">No reference images yet.</p>'
    if figures:
        gallery = '<div class="keyframes asset-references character-gallery">' + ''.join(figures) + '</div>'
    return f'<div data-reference-gallery>{gallery}</div>'


def _images(asset, base, csrf_token=None):
    upload = ''
    if csrf_token is not None:
        upload = f'''
        <form class="stack library-body" method="post" enctype="multipart/form-data"
              action="{base}/references" data-reference-upload>
          {_csrf(csrf_token)}
          <label>Reference images
            <input name="image" type="file" accept="{_IMAGE_TYPES}" required>
          </label>
          <small>PNG or JPEG, up to 10 MiB each. The first selected image is marked primary when checked.
            You can update labels individually after upload.</small>
          {_reference_options(asset)}
          <button type="submit">Upload images</button>
          <ul class="upload-queue" data-upload-queue aria-label="Image upload status" hidden></ul>
          <p data-upload-note class="muted" role="status" hidden></p>
        </form>'''
    return f'''
    <section class="panel" aria-labelledby="character-images">
      <h2 id="character-images">Reference Images</h2>
      {_gallery(asset, base, csrf_token)}
      {upload}
      <dialog class="image-preview" aria-labelledby="image-preview-title" data-preview-dialog>
        <div class="record-title">
          <h2 id="image-preview-title">Image preview</h2>
          <form method="dialog"><button type="submit" autofocus>Close</button></form>
        </div>
        <img alt="">
      </dialog>
    </section>'''


def _voice(asset, base, csrf_token=None):
    audio = asset.get('audio')
    sample = '<p class="muted">No voice reference added.</p>'
    if audio and audio['source'] == 'upload':
        sample = f'''
        <audio controls preload="metadata" aria-label="Voice reference audio"
               src="/character-audio/{_path_segment(asset["id"])}"></audio>'''
    elif audio and audio['source'] == 'link':
        sample = f'<p><a href="{_text(audio["url"])}" target="_blank" rel="noopener noreferrer">Open linked audio</a></p>'
    if csrf_token is not None:
        upload = 'Replace audio file' if audio else 'Upload audio file'
        remove = f'''
        <form class="actions" method="post" action="{base}/audio-remove">
          {_csrf(csrf_token)}
          <button type="submit">Remove voice reference</button>
        </form>''' if audio else ''
        sample += f'''
        <div class="fields library-body">
          <form class="stack" method="post" enctype="multipart/form-data" action="{base}/audio">
            {_csrf(csrf_token)}
            <label>MP3 or WAV <input name="audio" type="file" accept="{_AUDIO_TYPES}" required></label>
            <small>Up to 20 MiB.</small>
            <button type="submit">{upload}</button>
          </form>
          <form class="stack" method="post" action="{base}/audio-link">
            {_csrf(csrf_token)}
            <label>Direct audio link
              <input name="url" type="url" maxlength="4096" placeholder="https://"
                     value="{_text(audio.get('url', '') if audio else '')}" required>
            </label>
            <small>Use an HTTPS link to the audio file.</small>
            <button type="submit">Save audio link</button>
          </form>
        </div>
        {remove}'''
        styles = _voice_section(asset, base, csrf_token)
    else:
        styles = ''.join(
            f'<li>{_text(style["name"])} · {_text(style["voice"])}{" · Default" if style["is_default"] else ""}</li>'
            for style in asset['voice_styles']
        )
        styles = '<ul>' + styles + '</ul>' if styles else '<p class="muted">No provider voice style set.</p>'
    return f'''
    <section class="panel" aria-labelledby="character-voice">
      <h2 id="character-voice">Voice</h2>
      <h3>Voice reference</h3>
      {sample}
      <div class="library-body">
        <h3>Generated dialogue</h3>
        {styles}
      </div>
    </section>'''


def character_page(asset, csrf_token, *, edit=False, notice='', values=None):
    back = '<a href="/characters">All characters</a>'
    if asset is None:
        return _page('Add Character', '/characters', f'''
        <div class="character-page stack">
          {back}
          {_details_form(None, csrf_token, values)}
        </div>''', notice, script='/characters.js')
    base = f'/characters/{_path_segment(asset["id"])}'
    title = asset['name']
    if edit:
        title = f'Edit {title}'
        toolbar = f'<div class="character-toolbar">{back}<a href="{base}">View profile</a></div>'
        overview = _details_form(asset, csrf_token, values)
    else:
        toolbar = f'''
        <div class="character-toolbar">
          {back}
          <a class="button-link primary" href="{base}/edit">Edit</a>
        </div>'''
        color = asset.get('dress_color') or ''
        overview = f'''
        <section class="panel" aria-labelledby="character-overview">
          <div class="record-title">
            <h2 id="character-overview">Overview</h2>
            <span class="status">{_readiness(asset)}</span>
          </div>
          <p><strong>Character ID</strong> <code>{_text(asset['id'])}</code></p>
          <p><strong>Description</strong></p>
          <p class="prompt">{_text(asset['description'])}</p>
          <p><strong>Base dress color</strong> {'<code>' + _text(color) + '</code>' if color else 'Not set'}</p>
          {_outfits(asset, base)}
        </section>'''
    media_token = csrf_token if edit else None
    return _page(title, '/characters', f'''
    <div class="character-page stack">
      {toolbar}
      {overview}
      {_images(asset, base, media_token)}
      {_voice(asset, base, media_token)}
    </div>''', notice, script='/characters.js')
