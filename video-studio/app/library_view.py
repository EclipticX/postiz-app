"""Asset library and per-video production option forms."""

from urllib.parse import quote

from app.web import _csrf, _label, _text


_ID_PATTERN = '[a-z][a-z0-9_]{0,63}'
_IMAGE_TYPES = 'image/png,image/jpeg'
_AUDIO_TYPES = 'audio/mpeg,audio/wav,.mp3,.wav'
_ANGLES = ('front', 'three-quarter', 'side', 'back', 'other')
_PRESET_LABELS = {'visual': 'Visual', 'script': 'Script', 'subtitle': 'Subtitle', 'audio': 'Audio'}


def _identifier_input(name, label):
    return f'''<label>{label}
      <input name="{name}" maxlength="64" pattern="{_ID_PATTERN}" placeholder="example_name" required>
    </label>'''


def _choices(name, choices, selected='', default=''):
    options = f'<option value="">{_text(default)}</option>' if default else ''
    for value, label in choices:
        current = ' selected' if value == selected else ''
        options += f'<option value="{_text(value)}"{current}>{_text(label)}</option>'
    return f'<select name="{_text(name)}">{options}</select>'


def _asset_form(csrf_token):
    return f'''
    <details class="record">
      <summary>Add asset</summary>
      <form class="stack library-body" method="post" action="/settings/assets">
        {_csrf(csrf_token)}
        <div class="fields">
          {_identifier_input('asset_id', 'Stable asset ID')}
          <label>Kind{_choices('kind', (('place', 'Place'), ('item', 'Item')))}</label>
        </div>
        <small>Use lowercase letters, numbers and underscores for IDs. Start with a letter.</small>
        <label>Name<input name="name" maxlength="100" required></label>
        <label>Description<textarea name="description" rows="3" maxlength="2000" required></textarea></label>
        <button type="submit">Create asset</button>
      </form>
    </details>'''


def _reference_gallery(asset):
    asset_id = quote(str(asset['id']), safe='')
    figures = []
    variants = {variant['id']: variant['name'] for variant in asset['variants']}
    for reference in asset['references']:
        reference_id = quote(str(reference['id']), safe='')
        details = [reference['angle'].replace('-', ' ').capitalize()]
        if reference.get('variant_id'):
            details.append(variants.get(reference['variant_id'], reference['variant_id']))
        if reference.get('primary'):
            details.append('Primary')
        figures.append(f'''
        <figure>
          <img loading="lazy" src="/asset-reference/{_text(asset_id)}/{_text(reference_id)}"
               alt="{_text(asset['name'])} · {_text(details[0])} reference">
          <figcaption>{_text(' · '.join(details))}</figcaption>
        </figure>''')
    return '<div class="keyframes asset-references">' + ''.join(figures) + '</div>' if figures else ''


def _reference_form(asset, base, csrf_token):
    variants = [(variant['id'], variant['name']) for variant in asset['variants']]
    return f'''
    <details class="record">
      <summary>Add reference image</summary>
      <form class="stack library-body" method="post" enctype="multipart/form-data" action="{base}/references">
        {_csrf(csrf_token)}
        <label>Reference image<input name="image" type="file" accept="{_IMAGE_TYPES}" required></label>
        <small>PNG or JPEG, up to 10 MiB.</small>
        <div class="fields">
          <label>Angle{_choices('angle', ((angle, angle.replace('-', ' ').capitalize()) for angle in _ANGLES))}</label>
          <label>Variant{_choices('variant_id', variants, default='Base appearance')}</label>
        </div>
        <label><input type="checkbox" name="primary" value="yes"> Primary reference for this appearance</label>
        <button type="submit">Upload reference</button>
      </form>
    </details>'''


def _variant_section(asset, base, csrf_token):
    variants = ''.join(
        f'<li><strong>{_text(variant["name"])}</strong> · {_text(variant["description"])}</li>'
        for variant in asset['variants']
    )
    return f'''
    <details class="record">
      <summary>Variants · {len(asset['variants'])}</summary>
      {'<ul>' + variants + '</ul>' if variants else ''}
      <form class="stack library-body" method="post" action="{base}/variants">
        {_csrf(csrf_token)}
        <div class="fields">
          {_identifier_input('variant_id', 'Stable variant ID')}
          <label>Name<input name="name" maxlength="100" required></label>
        </div>
        <label>Description<textarea name="description" rows="2" maxlength="2000" required></textarea></label>
        <button type="submit">Add variant</button>
      </form>
    </details>'''


def _voice_section(asset, base, csrf_token):
    if asset['kind'] != 'character':
        return ''
    styles = []
    for style in asset['voice_styles']:
        details = [style['voice'], f'{style["speed"]}× speed']
        if style.get('expressiveness') is not None:
            details.append(f'{style["expressiveness"]} expressiveness')
        if style['is_default']:
            details.append('Default')
        styles.append(f'<li><strong>{_text(style["name"])}</strong> · {_text(" · ".join(details))}</li>')
    return f'''
    <details class="record">
      <summary>Voice styles · {len(styles)}</summary>
      {'<ul>' + ''.join(styles) + '</ul>' if styles else ''}
      <form class="stack library-body" method="post" action="{base}/voices">
        {_csrf(csrf_token)}
        <div class="fields">
          {_identifier_input('style_id', 'Stable voice style ID')}
          <label>Name<input name="name" maxlength="100" required></label>
        </div>
        <label>ElevenLabs voice name or ID<input name="voice" maxlength="100" required></label>
        <div class="fields">
          <label>Speed<input type="number" name="speed" min="0.7" max="1.2" step="0.01" value="1.0" required></label>
          <label>Expressiveness (optional)<input type="number" name="expressiveness" min="0" max="1" step="0.05"></label>
        </div>
        <label><input type="checkbox" name="is_default" value="yes"> Default voice style for this character</label>
        <button type="submit">Add voice style</button>
      </form>
    </details>'''


def _asset_card(asset, csrf_token, expanded=False):
    asset_id = _text(quote(str(asset['id']), safe=''))
    base = '/settings/assets/' + asset_id
    return f'''
    <details class="record" id="asset-{asset_id}"{' open' if expanded else ''}>
      <summary><strong>{_text(asset['name'])}</strong> · {_label(asset['kind'])}</summary>
      <div class="library-body">
        <p class="muted">{_text(asset['id'])}</p>
        <p class="prompt">{_text(asset['description'])}</p>
        {_reference_gallery(asset)}
        {_reference_form(asset, base, csrf_token)}
        {_variant_section(asset, base, csrf_token)}
        {_voice_section(asset, base, csrf_token)}
      </div>
    </details>'''


def asset_library_section(assets, csrf_token, open_asset=None):
    cards = ''.join(_asset_card(asset, csrf_token, asset['id'] == open_asset) for asset in assets)
    return f'''
    <details class="panel records library-panel" id="asset-library"{' open' if open_asset else ''}>
      <summary>Asset Library</summary>
      <div class="library-body">
        <p class="muted">Manage places, items and their reference images. For cast profiles, open <a href="/characters">Characters</a>.</p>
        {_asset_form(csrf_token)}
        {cards}
      </div>
    </details>'''


def _preset_fields(kind):
    if kind == 'visual':
        return '''<label>Style guide<textarea name="style" rows="3" required></textarea></label>
        <label>Palette<input name="palette" placeholder="Soft peach, forest green" required></label>'''
    if kind == 'script':
        return '<label>Tone<textarea name="tone" rows="3" required></textarea></label>'
    if kind == 'subtitle':
        fonts = ('DejaVu Sans', 'DejaVu Serif', 'DejaVu Sans Mono')
        return f'''<div class="fields">
          <label>Font{_choices('font_name', ((font, font) for font in fonts))}</label>
          <label>Font size<input type="number" name="font_size" min="8" max="30" value="12" required></label>
          <label>Text color<input type="color" name="text_color" value="#FFFFFF"></label>
          <label>Outline color<input type="color" name="outline_color" value="#000000"></label>
        </div>
        <label>Position{_choices('position', ((position, position.capitalize()) for position in ('top', 'middle', 'bottom')), selected='bottom')}</label>'''
    return f'''<label>Music<input name="music" type="file" accept="{_AUDIO_TYPES}"></label>
    <label>Opening sound effect<input name="sfx" type="file" accept="{_AUDIO_TYPES}"></label>
    <small>Add at least one MP3 or WAV. Each file can be up to 20 MiB.</small>'''


def _preset_description(preset):
    config = preset['config']
    kind = preset['kind']
    if kind == 'visual':
        details = (config.get('style'), config.get('palette'))
    elif kind == 'script':
        details = (config.get('tone'),)
    elif kind == 'subtitle':
        details = (config.get('font_name'), config.get('font_size'), config.get('position'))
    else:
        details = ('Music' if config.get('music') else None, 'Opening sound effect' if config.get('sfx') else None)
    return _text(' · '.join(str(detail) for detail in details if detail))


def _preset_card(preset, csrf_token):
    button = '<span class="status">Default</span>'
    if not preset['is_default']:
        preset_id = _text(quote(str(preset['id']), safe=''))
        button = f'''
        <form method="post" action="/settings/presets/{_text(preset['kind'])}/{preset_id}/default">
          {_csrf(csrf_token)}
          <button type="submit">Set as default</button>
        </form>'''
    return f'''
    <article class="record">
      <div class="record-title"><strong>{_text(preset['name'])}</strong>{button}</div>
      <p class="muted prompt">{_preset_description(preset)}</p>
    </article>'''


def _preset_group(kind, label, presets, csrf_token):
    cards = ''.join(_preset_card(preset, csrf_token) for preset in presets)
    multipart = ' enctype="multipart/form-data"' if kind == 'audio' else ''
    return f'''
    <details class="record">
      <summary>{label} presets · {len(presets)}</summary>
      <div class="library-body">
        {cards}
        <form class="stack" method="post"{multipart} action="/settings/presets/{kind}">
          {_csrf(csrf_token)}
          <div class="fields">
            {_identifier_input('preset_id', 'Stable preset ID')}
            <label>Name<input name="name" maxlength="100" required></label>
          </div>
          {_preset_fields(kind)}
          <label><input type="checkbox" name="is_default" value="yes"> Default for new videos</label>
          <button type="submit">Add {label.lower()} preset</button>
        </form>
      </div>
    </details>'''


def production_presets_section(presets, csrf_token, expanded=False):
    groups = ''.join(
        _preset_group(kind, label, [preset for preset in presets if preset['kind'] == kind], csrf_token)
        for kind, label in _PRESET_LABELS.items()
    )
    return f'''
    <details class="panel records library-panel" id="production-presets"{' open' if expanded else ''}>
      <summary>Production Presets</summary>
      <div class="library-body">
        <p class="muted">Reuse style, script, subtitle and audio settings across videos.</p>
        {groups}
      </div>
    </details>'''


def production_options_section(job, assets, presets, options, csrf_token, *, expanded=False, brand_profiles=None):
    if job['status'] not in ('queued', 'review_keyframes', 'preview_ready', 'needs_review'):
        return ''
    options = options or {}
    selected_presets = options.get('presets', {})
    preset_fields = []
    if brand_profiles:
        default_brand = next((bp['name'] for bp in brand_profiles if bp.get('is_default')), None)
        select = _choices(
            'preset.brand', ((bp['id'], bp['name']) for bp in brand_profiles),
            selected=selected_presets.get('brand'), default=f'Default ({default_brand})' if default_brand else 'Use default brand',
        )
        preset_fields.append(f'<label>Brand rules{select}</label>')
    for kind, label in _PRESET_LABELS.items():
        choices = [preset for preset in presets if preset['kind'] == kind]
        default = next((preset['name'] for preset in choices if preset['is_default']), None)
        select = _choices(
            f'preset.{kind}', ((preset['id'], preset['name']) for preset in choices),
            selected=selected_presets.get(kind), default=f'Default ({default})' if default else 'Use built-in settings',
        )
        preset_fields.append(f'<label>{label} preset{select}</label>')
    selected_characters = set(options.get('characters', []))
    character_fields = []
    asset_fields = []
    for asset in assets:
        asset_id = asset['id']
        if asset['kind'] == 'character' and asset['references']:
            checked = ' checked' if asset_id in selected_characters else ''
            hint = '' if any(not ref['variant_id'] for ref in asset['references']) else ' · Choose an outfit with images'
            character_fields.append(
                f'<label><input type="checkbox" name="characters" value="{_text(asset_id)}"{checked}> '
                f'{_text(asset["name"])}<small>{hint}</small></label>'
            )
        if asset['variants']:
            select = _choices(
                f'variant.{asset_id}', ((variant['id'], variant['name']) for variant in asset['variants']),
                selected=options.get('variants', {}).get(asset_id), default='Base appearance',
            )
            asset_fields.append(f'<label>{_text(asset["name"])} appearance{select}</label>')
        if asset['voice_styles']:
            default = next((style['name'] for style in asset['voice_styles'] if style['is_default']), None)
            select = _choices(
                f'voice.{asset_id}', ((style['id'], style['name']) for style in asset['voice_styles']),
                selected=options.get('voices', {}).get(asset_id), default=f'Default ({default})' if default else 'Use default voice',
            )
            asset_fields.append(f'<label>{_text(asset["name"])} voice{select}</label>')
    character_section = ''
    if character_fields:
        character_section = f'''
        <fieldset class="stack">
          <legend>Characters</legend>
          <small>Leave all unchecked to let Studio choose the cast.</small>
          <div class="fields">{''.join(character_fields)}</div>
        </fieldset>'''
    if job['status'] == 'queued':
        description = 'Options apply when planning starts.'
        button = 'Save options'
    else:
        description = 'Saving changes reruns planning and requires fresh keyframe approval.'
        button = 'Save options and replan'
    return f'''
    <details class="record production-options"{' open' if expanded else ''}>
      <summary>Production options</summary>
      <form class="stack library-body" method="post" action="/workspace/{int(job['id'])}/options">
        {_csrf(csrf_token)}
        <p class="muted">{description}</p>
        <fieldset class="fields"><legend>Presets</legend>{''.join(preset_fields)}</fieldset>
        {character_section}
        {'<fieldset class="fields"><legend>Asset overrides</legend>' + ''.join(asset_fields) + '</fieldset>' if asset_fields else ''}
        <button type="submit">{button}</button>
      </form>
    </details>'''
