"""Asset library view, profile, and edit screens for places and items."""

from urllib.parse import quote

from app.asset_library import REFERENCE_ANGLES
from app.library_view import _ID_PATTERN, _IMAGE_TYPES, _choices, _identifier_input
from app.web import _csrf, _label, _page, _text


def _path_segment(value):
    return _text(quote(str(value), safe=''))


def _reference_url(asset, reference):
    return f'/asset-reference/{_path_segment(asset["id"])}/{_path_segment(reference["id"])}'


def _readiness(asset):
    references = asset.get('references', [])
    if any(not reference.get('variant_id') for reference in references):
        return 'Ready for keyframes'
    if references:
        return 'Ready with variant'
    return 'Needs a reference image'


def _asset_thumbnail(asset):
    references = asset.get('references', [])
    primary = next((ref for ref in references if ref.get('primary') and not ref.get('variant_id')), None)
    primary = primary or next((ref for ref in references if ref.get('primary')), None)
    if primary is None and references:
        primary = references[0]
    if primary:
        return f'<img class="character-thumbnail" src="{_reference_url(asset, primary)}" alt="" loading="lazy">'
    kind = asset.get('kind', 'place')
    icon = '📍' if kind == 'place' else '📦'
    return f'<span class="character-thumbnail asset-placeholder" aria-hidden="true">{icon}</span>'


def assets_page(assets, query='', active_kind='all'):
    filtered = []
    place_count = 0
    item_count = 0
    all_count = 0

    for asset in assets:
        kind = asset.get('kind')
        if kind not in ('place', 'item'):
            continue
        all_count += 1
        if kind == 'place':
            place_count += 1
        elif kind == 'item':
            item_count += 1

        if active_kind in ('place', 'item') and kind != active_kind:
            continue
        if query and query.casefold() not in asset['name'].casefold() and query.casefold() not in asset['id'].casefold():
            continue
        filtered.append(asset)

    items = []
    for asset in filtered:
        thumbnail = _asset_thumbnail(asset)
        kind = asset.get('kind', 'place')
        kind_label = 'Place' if kind == 'place' else 'Item'
        readiness = _readiness(asset)
        status_class = 'status-ready' if 'Ready' in readiness else ''
        variant_count = len(asset.get('variants', []))
        variant_hint = f'<small class="muted"> · {variant_count} variant{"s" if variant_count != 1 else ""}</small>' if variant_count else ''

        items.append(f'''
        <li>
          <a class="character-list-link asset-list-link" href="/assets/{_path_segment(asset['id'])}">
            {thumbnail}
            <span>
              <strong>{_text(asset['name'])}</strong>{variant_hint}<br>
              <small class="muted">{_text(asset['id'])}</small>
            </span>
            <div class="asset-badges">
              <span class="status {status_class}">{readiness}</span>
              <span class="badge-kind badge-{_text(kind)}">{kind_label}</span>
            </div>
          </a>
        </li>''')

    empty_message = 'No assets match that filter or name.' if (query or active_kind != 'all') else 'Add places and items to organize environments and props for video scenes.'
    clear_search = f'<a href="/assets?kind={_text(active_kind)}">Clear</a>' if query else ''
    results = '<ul class="job-list character-list asset-list">' + ''.join(items) + '</ul>' if items else f'<p class="empty-state">{empty_message}</p>'

    tabs = (
        ('all', f'All ({all_count})'),
        ('place', f'Places ({place_count})'),
        ('item', f'Items ({item_count})'),
    )
    tab_links = []
    for key, label in tabs:
        current = ' class="tab-link active"' if active_kind == key else ' class="tab-link"'
        q_param = f'&amp;q={_text(quote(query))}' if query else ''
        tab_links.append(f'<a{current} href="/assets?kind={key}{q_param}">{label}</a>')

    return _page('Asset Library', '/assets', f'''
    <section class="panel stack" aria-label="Asset library">
      <div class="asset-toolbar-header">
        <div class="asset-tabs-nav" role="tablist">
          {''.join(tab_links)}
        </div>
        <a class="button-link primary" href="/assets/new/profile">+ Add Asset</a>
      </div>
      <div class="character-toolbar asset-toolbar">
        <form class="character-search asset-search" method="get" action="/assets" role="search">
          <input type="hidden" name="kind" value="{_text(active_kind)}">
          <label>Search by name or ID
            <input name="q" type="search" value="{_text(query)}" maxlength="100" placeholder="e.g. forest, enchanted sword">
          </label>
          <button type="submit">Search</button>
          {clear_search}
        </form>
      </div>
      {results}
    </section>''', '')


def _reference_options(asset, reference=None):
    reference = reference or {}
    angles = [(angle, angle.replace('-', ' ').capitalize()) for angle in REFERENCE_ANGLES]
    variants = [(variant['id'], variant['name']) for variant in asset.get('variants', [])]
    angle_choice = _choices('angle', angles, selected=reference.get('angle', 'front'))
    variant_choice = _choices('variant_id', variants, selected=reference.get('variant_id'), default='Base appearance')
    checked = ' checked' if reference.get('primary') else ''
    return f'''
    <div class="fields">
      <label>Angle or view{angle_choice}</label>
      <label>Variant appearance{variant_choice}</label>
    </div>
    <label><input type="checkbox" name="primary" value="yes"{checked}> Primary reference for this appearance</label>'''


def _gallery(asset, base, csrf_token=None):
    figures = []
    variants = {variant['id']: variant['name'] for variant in asset.get('variants', [])}
    for reference in asset.get('references', []):
        url = _reference_url(asset, reference)
        angle = reference['angle'].replace('-', ' ').capitalize()
        label = f'{asset["name"]} · {angle} reference'
        variant = variants.get(reference.get('variant_id'))
        primary = '<span class="status status-ready">Primary</span>' if reference.get('primary') else ''
        settings = ''
        if csrf_token is not None:
            settings = f'''
            <details class="reference-edit-details">
              <summary>Edit image labels</summary>
              <form class="stack library-body" method="post" action="{base}/references/{_path_segment(reference['id'])}">
                {_csrf(csrf_token)}
                {_reference_options(asset, reference)}
                <button type="submit">Save image details</button>
              </form>
            </details>'''
        figures.append(f'''
        <figure class="asset-figure">
          <a href="{url}" data-image-preview>
            <img src="{url}" alt="{_text(label)}" loading="lazy">
          </a>
          <figcaption>{_text(angle)}{f' · {_text(variant)}' if variant else ''} {primary}</figcaption>
          {settings}
        </figure>''')
    gallery = '<p class="muted">No reference images yet. Upload images below to guide generation.</p>'
    if figures:
        gallery = '<div class="keyframes asset-references character-gallery">' + ''.join(figures) + '</div>'
    return f'<div data-reference-gallery>{gallery}</div>'


def _images(asset, base, csrf_token=None):
    upload = ''
    if csrf_token is not None:
        upload = f'''
        <form class="stack library-body asset-upload-form" method="post" enctype="multipart/form-data"
              action="{base}/references" data-reference-upload>
          {_csrf(csrf_token)}
          <h3>Upload reference image</h3>
          <label>Reference images
            <input name="image" type="file" accept="{_IMAGE_TYPES}" required>
          </label>
          <small class="muted">PNG or JPEG, up to 10 MiB each. The first uploaded image is marked primary when checked.</small>
          {_reference_options(asset)}
          <button type="submit" class="primary">Upload image</button>
          <ul class="upload-queue" data-upload-queue aria-label="Image upload status" hidden></ul>
          <p data-upload-note class="muted" role="status" hidden></p>
        </form>'''
    return f'''
    <section class="panel" aria-labelledby="asset-images">
      <h2 id="asset-images">Reference Images</h2>
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


def _variants(asset, base, csrf_token=None):
    items = []
    for variant in asset.get('variants', []):
        items.append(f'''
        <div class="record">
          <h3>{_text(variant['name'])}</h3>
          <p class="muted"><code>{_text(variant['id'])}</code></p>
          <p class="prompt">{_text(variant['description'])}</p>
        </div>''')
    if csrf_token is not None:
        items.append(f'''
        <details class="record">
          <summary>+ Add variant appearance</summary>
          <form class="stack library-body" method="post" action="{base}/variants">
            {_csrf(csrf_token)}
            <div class="fields">
              {_identifier_input('variant_id', 'Variant ID')}
              <label>Name<input name="name" maxlength="100" required></label>
            </div>
            <label>Description<textarea name="description" rows="2" maxlength="2000" required></textarea></label>
            <button type="submit">Add variant</button>
          </form>
        </details>''')
    return '<div><h3>Variants</h3>' + ''.join(items) + '</div>' if items else (
        '<div><h3>Variants</h3>' + items[0] + '</div>' if (csrf_token is not None and items) else ''
    )


def _details_form(asset, csrf_token, values):
    creating = asset is None
    base = '/assets' if creating else f'/assets/{_path_segment(asset["id"])}'
    initial = asset or {}
    values = values or {}
    identifier = f'<p><strong>Asset ID</strong> <code>{_text(initial.get("id", ""))}</code></p>'
    kind_field = f'<p><strong>Kind</strong> {_label(initial.get("kind", "place"))}</p>'
    if creating:
        identifier = f'''
        <label>Asset ID
          <input name="asset_id" value="{_text(values.get('asset_id', ''))}" maxlength="64"
                 pattern="{_ID_PATTERN}" placeholder="e.g. enchanted_forest" required>
        </label>
        <small class="muted">Use lowercase letters, numbers, and underscores. Start with a letter.
          The ID stays fixed after creation.</small>'''
        kind_field = f'''
        <label>Kind
          {_choices('kind', (('place', 'Place / Environment'), ('item', 'Item / Prop')), selected=values.get('kind', 'place'))}
        </label>'''
    return f'''
    <section class="panel" aria-labelledby="asset-overview">
      <h2 id="asset-overview">{'Add New Asset' if creating else 'Overview'}</h2>
      <form class="stack" method="post" action="{base if creating else base + '/edit'}">
        {_csrf(csrf_token)}
        {identifier}
        {kind_field}
        <label>Asset name
          <input name="name" value="{_text(values.get('name', initial.get('name', '')))}" maxlength="100" placeholder="e.g. Enchanted Forest" required>
        </label>
        <label>Description
          <textarea name="description" rows="4" maxlength="2000" placeholder="Describe the visual style, atmosphere, materials, or features..." required>{_text(values.get('description', initial.get('description', '')))}</textarea>
        </label>
        <div class="actions">
          <button class="primary" type="submit">{'Create asset' if creating else 'Save overview'}</button>
          <a href="{base}">Cancel</a>
        </div>
      </form>
    </section>'''


def asset_page(asset, csrf_token, *, edit=False, notice='', values=None):
    back = '<a href="/assets">← All assets</a>'
    if asset is None:
        return _page('Add Asset', '/assets', f'''
        <div class="character-page asset-page stack">
          {back}
          {_details_form(None, csrf_token, values)}
        </div>''', notice, script='/assets.js')

    base = f'/assets/{_path_segment(asset["id"])}'
    title = asset['name']
    kind_label = 'Place' if asset.get('kind') == 'place' else 'Item'

    if edit:
        title = f'Edit {title}'
        toolbar = f'<div class="character-toolbar asset-toolbar">{back}<a href="{base}">View asset profile</a></div>'
        overview = _details_form(asset, csrf_token, values)
    else:
        toolbar = f'''
        <div class="character-toolbar asset-toolbar">
          {back}
          <a class="button-link primary" href="{base}/edit">Edit</a>
        </div>'''
        readiness = _readiness(asset)
        status_class = 'status-ready' if 'Ready' in readiness else ''
        overview = f'''
        <section class="panel" aria-labelledby="asset-overview">
          <div class="record-title">
            <h2 id="asset-overview">{_text(asset['name'])}</h2>
            <div class="asset-badges">
              <span class="badge-kind badge-{_text(asset.get('kind', 'place'))}">{kind_label}</span>
              <span class="status {status_class}">{readiness}</span>
            </div>
          </div>
          <p><strong>Asset ID</strong> <code>{_text(asset['id'])}</code></p>
          <p><strong>Description</strong></p>
          <p class="prompt">{_text(asset['description'])}</p>
          {_variants(asset, base, csrf_token)}
        </section>'''

    media_token = csrf_token
    return _page(title, '/assets', f'''
    <div class="character-page asset-page stack">
      {toolbar}
      {overview}
      {_images(asset, base, media_token)}
    </div>''', notice, script='/assets.js')
