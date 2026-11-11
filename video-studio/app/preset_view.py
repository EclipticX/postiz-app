"""Production presets screen for Visual, Script, Subtitle, and Audio settings."""

from urllib.parse import quote

from app.library_view import _AUDIO_TYPES, _ID_PATTERN, _choices, _identifier_input, _preset_fields
from app.web import _csrf, _page, _text

_PRESET_KINDS = ('visual', 'script', 'subtitle', 'audio')
_PRESET_TITLES = {
    'visual': 'Visual',
    'script': 'Script',
    'subtitle': 'Subtitle',
    'audio': 'Audio',
}
_PRESET_INTROS = {
    'visual': 'Visual style guides and color palettes applied to prompt generation and image rendering.',
    'script': 'Script tone, narrative pacing, and storytelling directives for video planning.',
    'subtitle': 'Caption typography, font sizing, text and outline colors, and screen positioning.',
    'audio': 'Background music tracks and opening sound effects blended into scene audio.',
}


def _preset_body(preset):
    kind = preset.get('kind')
    config = preset.get('config', {})

    if kind == 'visual':
        style = config.get('style', '')
        palette = config.get('palette', '')
        palette_pills = ''.join(
            f'<span class="preset-pill">{_text(color.strip())}</span>'
            for color in palette.split(',') if color.strip()
        )
        return f'''
        <div class="preset-details stack">
          <div>
            <strong>Style Guide</strong>
            <p class="prompt">{_text(style)}</p>
          </div>
          <div>
            <strong>Palette</strong>
            <div class="preset-pill-list">{palette_pills or _text(palette)}</div>
          </div>
        </div>'''

    if kind == 'script':
        tone = config.get('tone', '')
        return f'''
        <div class="preset-details stack">
          <div>
            <strong>Tone &amp; Story Directives</strong>
            <p class="prompt">{_text(tone)}</p>
          </div>
        </div>'''

    if kind == 'subtitle':
        font_name = config.get('font_name', 'DejaVu Sans')
        font_size = config.get('font_size', 12)
        text_color = config.get('text_color', '#FFFFFF')
        outline_color = config.get('outline_color', '#000000')
        position = config.get('position', 'bottom')
        return f'''
        <div class="preset-details stack">
          <div class="preset-specs-row">
            <span><strong>Font</strong> {_text(font_name)}</span>
            <span><strong>Size</strong> {_text(font_size)}px</span>
            <span><strong>Position</strong> {_text(str(position).capitalize())}</span>
            <span><strong>Colors</strong> <span class="color-dot" style="background:{_text(text_color)}"></span> {_text(text_color)} / <span class="color-dot" style="background:{_text(outline_color)}"></span> {_text(outline_color)}</span>
          </div>
          <div class="subtitle-preview-box">
            <div class="subtitle-screen-mock align-{_text(position)}">
              <span class="subtitle-mock-text" style="font-family:'{_text(font_name)}',system-ui,sans-serif;font-size:{_text(font_size * 1.5)}px;color:{_text(text_color)};text-shadow:-1px -1px 0 {_text(outline_color)}, 1px -1px 0 {_text(outline_color)}, -1px 1px 0 {_text(outline_color)}, 1px 1px 0 {_text(outline_color)};">
                Captions appear here in videos
              </span>
            </div>
          </div>
        </div>'''

    if kind == 'audio':
        music = config.get('music')
        sfx = config.get('sfx')
        music_html = f'''
        <div class="audio-track-item">
          <span class="audio-track-label"><strong>Music Track:</strong> <code>{_text(music)}</code></span>
          <audio controls preload="none" src="/preset-audio/{_text(quote(music, safe=''))}"></audio>
        </div>''' if music else '<p class="muted">No music track uploaded.</p>'

        sfx_html = f'''
        <div class="audio-track-item">
          <span class="audio-track-label"><strong>Opening Sound Effect:</strong> <code>{_text(sfx)}</code></span>
          <audio controls preload="none" src="/preset-audio/{_text(quote(sfx, safe=''))}"></audio>
        </div>''' if sfx else '<p class="muted">No sound effect uploaded.</p>'

        return f'''
        <div class="preset-details stack">
          {music_html}
          {sfx_html}
        </div>'''

    return ''


def _preset_card(preset, csrf_token):
    kind = preset.get('kind', 'visual')
    preset_id = _text(quote(str(preset['id']), safe=''))
    status_action = '<span class="status status-ready">Default</span>'
    if not preset.get('is_default'):
        status_action = f'''
        <form method="post" action="/presets/{kind}/{preset_id}/default">
          {_csrf(csrf_token)}
          <button type="submit" class="button-secondary">Set as default</button>
        </form>'''

    return f'''
    <article class="record preset-card">
      <div class="record-title preset-card-header">
        <div>
          <h3>{_text(preset['name'])}</h3>
          <small class="muted"><code>{_text(preset['id'])}</code></small>
        </div>
        {status_action}
      </div>
      {_preset_body(preset)}
    </article>'''


def presets_page(presets, csrf_token, active_tab='visual', notice=''):
    if active_tab not in _PRESET_KINDS:
        active_tab = 'visual'

    groups = {kind: [] for kind in _PRESET_KINDS}
    for preset in presets:
        kind = preset.get('kind')
        if kind in groups:
            groups[kind].append(preset)

    tab_links = []
    panes = []

    for kind in _PRESET_KINDS:
        count = len(groups[kind])
        label = _PRESET_TITLES[kind]
        is_active = (kind == active_tab)
        active_class = ' active' if is_active else ''

        tab_links.append(f'''
        <a class="nav-tab{active_class}" href="/presets?tab={kind}" data-tab-btn="{kind}" role="tab" aria-selected="{'true' if is_active else 'false'}">
          {label} <span class="tab-badge">{count}</span>
        </a>''')

        cards = ''.join(_preset_card(preset, csrf_token) for preset in groups[kind])
        if not cards:
            cards = f'<p class="empty-state">No {label.lower()} presets created yet. Add one below.</p>'

        multipart = ' enctype="multipart/form-data"' if kind == 'audio' else ''
        add_form = f'''
        <details class="record preset-add-accordion">
          <summary>+ Add {label} Preset</summary>
          <form class="stack library-body" method="post"{multipart} action="/presets/{kind}">
            {_csrf(csrf_token)}
            <div class="fields">
              {_identifier_input('preset_id', 'Preset ID')}
              <label>Preset name<input name="name" maxlength="100" placeholder="e.g. Modern Minimalist" required></label>
            </div>
            {_preset_fields(kind)}
            <label><input type="checkbox" name="is_default" value="yes"> Set as default for new videos</label>
            <button type="submit" class="primary">Save {label.lower()} preset</button>
          </form>
        </details>'''

        pane_display = ' active' if is_active else ''
        panes.append(f'''
        <div class="preset-tab-pane{pane_display}" data-tab-pane="{kind}" role="tabpanel">
          <div class="category-header-banner">
            <p>{_PRESET_INTROS[kind]}</p>
          </div>
          <div class="presets-list stack">
            {cards}
          </div>
          {add_form}
        </div>''')

    content = f'''
    <p class="intro">Configure reusable style, script, subtitle and audio directives for automated video planning.</p>
    <nav class="providers-nav-tabs presets-nav-tabs" role="tablist">
      {''.join(tab_links)}
    </nav>
    {''.join(panes)}'''

    return _page('Production Presets', '/presets', content, notice, script='/presets.js')
