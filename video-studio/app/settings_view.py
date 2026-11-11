from decimal import Decimal

from app.settings import DEFAULT_ACTIVE_PLATFORMS, KNOWN_PLATFORMS
from app.web import _csrf, _page, _text


def _usd(micros):
    return format(Decimal(micros) / 1_000_000, 'f')


_CATEGORIES = (
    ('scripting', 'Scripting', {'llm'}),
    ('character', 'Character', {'image'}),
    ('animation', 'Animation', {'video', 'tts'}),
    ('publishing', 'Publishing', {'publishing'}),
)

_PLATFORM_META = {
    'FAL_KEY': {
        'slug': 'fal',
        'active': {'character', 'animation'},
    },
    'ANTHROPIC_API_KEY': {
        'slug': 'anthropic',
        'active': {'scripting'},
    },
    'EVOLINK_API_KEY': {
        'slug': 'evolink',
        'active': {'animation'},
    },
    'POSTIZ_API_KEY': {
        'slug': 'postiz',
        'active': {'publishing'},
    },
}


def providers_page(providers, csrf_token, notice='', *, active_tab='', platforms_info=None, configured_keys=None, raw_keys=None):
    if configured_keys is None:
        configured_keys = {route['key_env'] for route in providers if route.get('configured')}
    else:
        configured_keys = set(configured_keys)
    if raw_keys is None:
        raw_keys = {}
    if platforms_info:
        active_platforms = platforms_info[0]
        custom_platforms = platforms_info[1]
        platform_categories = platforms_info[2] if len(platforms_info) > 2 else {}
    else:
        active_platforms, custom_platforms, platform_categories = list(DEFAULT_ACTIVE_PLATFORMS), {}, {}

    all_platforms_catalog = dict(KNOWN_PLATFORMS)
    for k, v in custom_platforms.items():
        all_platforms_catalog[k] = {
            "name": v.get("name", k),
            "slug": v.get("slug", k.lower()),
            "active": v.get("active", ["scripting"]),
            "models": [],
        }

    valid_tabs = [cat[0] for cat in _CATEGORIES] + ['api-keys']
    selected_tab = active_tab if active_tab in valid_tabs else 'scripting'

    # Build category tab counts
    category_counts = {}
    for cat_id, _, type_set in _CATEGORIES:
        category_counts[cat_id] = sum(
            1 for p in providers if p['type'] in type_set and p['enabled'] and p['configured']
        )

    # Build Navigation Tabs
    nav_buttons = []
    for cat_id, cat_title, _ in _CATEGORIES:
        count = category_counts[cat_id]
        active_cls = ' active' if selected_tab == cat_id else ''
        badge_cls = ' badge-active' if count > 0 else ''
        nav_buttons.append(
            f'<button type="button" class="providers-tab-btn{active_cls}" data-tab="{cat_id}" role="tab" '
            f'aria-selected="{"true" if selected_tab == cat_id else "false"}">'
            f'{cat_title} <span class="providers-tab-badge{badge_cls}">{count}</span></button>'
        )

    api_keys_active = ' active' if selected_tab == 'api-keys' else ''
    nav_buttons.append(
        f'<button type="button" class="providers-tab-btn{api_keys_active}" data-tab="api-keys" role="tab" '
        f'aria-selected="{"true" if selected_tab == "api-keys" else "false"}">API keys</button>'
    )
    nav_buttons.append(
        '<button type="button" class="providers-tab-btn providers-tab-btn-plus" data-tab-plus="api-keys" title="Add platform">+</button>'
    )
    nav_tabs_html = f'<nav class="providers-nav-tabs" role="tablist">{"".join(nav_buttons)}</nav>'

    # Build Category Panes
    panes_html = []
    for cat_id, _, type_set in _CATEGORIES:
        pane_active = ' active' if selected_tab == cat_id else ''
        cat_providers = [p for p in providers if p['type'] in type_set]
        ready_providers = sorted(
            [p for p in cat_providers if p['enabled'] and p['configured']],
            key=lambda x: x['priority']
        )

        if ready_providers:
            run_order_items = ''.join(f'<li>{i}. {_text(p["name"])}</li>' for i, p in enumerate(ready_providers, 1))
            run_order_content = f'<ol class="run-order-list">{run_order_items}</ol>'
        else:
            run_order_content = '<p class="run-order-empty">No provider is enabled for this category.</p>'

        run_order_html = f'''
        <div class="run-order-panel">
          <h3>Run order</h3>
          {run_order_content}
        </div>'''

        cards_html = []
        for route in cat_providers:
            if route['configured'] and route['enabled']:
                badge = '<span class="pill-badge pill-badge-ready">Ready</span>'
            elif not route['configured']:
                badge = '<span class="pill-badge pill-badge-needs-key">Needs key</span>'
            else:
                badge = '<span class="pill-badge pill-badge-disabled">Disabled</span>'

            missing_banner = ''
            if not route['configured']:
                missing_banner = f'''
                <div class="missing-key-bar">
                  <span class="missing-key-text">{_text(route['key_env'])} is missing.</span>
                  <button type="button" class="btn-add-key" data-switch-to-key="{_text(route['key_env'])}">Add key</button>
                </div>'''

            cards_html.append(f'''
            <div class="provider-card">
              <div class="provider-card-header">
                <h3>{_text(route['name'])}</h3>
                {badge}
              </div>
              {missing_banner}
              <form method="post" action="/settings/providers/{_text(route['name'])}">
                {_csrf(csrf_token)}
                <input type="hidden" name="tab" value="{cat_id}">
                <div class="fields-row">
                  <label>Priority (lower runs first)
                    <input type="number" name="priority" min="0" max="9999" value="{int(route['priority'])}" required>
                  </label>
                  <label>Est. USD per {_text(route['billing_unit'].replace('_', ' '))}
                    <input type="number" name="rate" min="0" step="0.000001" value="{_usd(route['rate_micros'])}" required>
                  </label>
                </div>
                <div class="card-footer-row">
                  <label class="toggle-checkbox">
                    <input type="checkbox" name="enabled" value="yes"{' checked' if route['enabled'] else ''}> Enabled
                  </label>
                  <div class="card-actions">
                    <button type="button" class="btn-danger-link" data-disable-provider>Remove</button>
                    <button type="submit" class="btn-primary-save">Save</button>
                  </div>
                </div>
              </form>
            </div>''')

        panes_html.append(f'''
        <div class="provider-tab-pane{pane_active}" data-tab="{cat_id}" role="tabpanel">
          {run_order_html}
          {''.join(cards_html)}
        </div>''')

    # Build API Keys Pane
    api_pane_active = ' active' if selected_tab == 'api-keys' else ''

    available_to_add = [
        (k, all_platforms_catalog[k]["name"])
        for k in all_platforms_catalog
        if k not in active_platforms
    ]
    add_options = ''.join(f'<option value="{_text(k)}">{_text(title)}</option>' for k, title in available_to_add)

    add_platform_html = f'''
    <form method="post" action="/settings/platforms/add" class="add-platform-card">
      {_csrf(csrf_token)}
      <input type="hidden" name="tab" value="api-keys">
      <h3>Add platform</h3>
      <label for="platform-dropdown">Platform</label>
      <div class="add-platform-control">
        <select id="platform-dropdown" name="platform_key" required data-platform-dropdown>
          <option value="" disabled selected>Choose a platform to add…</option>
          {add_options}
          <option value="custom">+ Custom platform…</option>
        </select>
        <button type="submit" class="btn-add-platform">Add platform</button>
      </div>
      <div id="custom-platform-inputs" class="custom-platform-fields" style="display:none;">
        <div class="fields-row">
          <label>Key Name (ENV)
            <input type="text" name="custom_key" placeholder="e.g. MISTRAL_API_KEY">
          </label>
          <label>Platform Display Name
            <input type="text" name="custom_name" placeholder="e.g. Mistral AI">
          </label>
        </div>
      </div>
      <p class="helper-text muted">Suggested categories are ticked for you. Change them below.</p>
    </form>'''

    key_cards = []
    for name in active_platforms:
        configured = name in configured_keys
        meta = dict(all_platforms_catalog.get(name, {
            'name': name,
            'slug': name.lower().replace('_api_key', '').replace('_key', ''),
            'active': {'scripting'},
            'models': [],
        }))
        if name in platform_categories:
            active_cats = set(platform_categories[name])
        else:
            active_cats = set(meta.get('active', []))

        badge = '<span class="pill-badge pill-badge-saved">Key saved</span>' if configured else '<span class="pill-badge pill-badge-needs-key">Key required</span>'

        pills = []
        for cat_id, cat_title, _ in _CATEGORIES:
            is_active = cat_id in active_cats
            active_cls = ' active' if is_active else ''
            check_prefix = '✓ ' if is_active else ''
            pills.append(
                f'<button type="button" class="category-pill{active_cls}" '
                f'data-toggle-cat="{cat_id}" data-cat-title="{cat_title}" '
                f'data-platform="{_text(name)}" aria-pressed="{"true" if is_active else "false"}" '
                f'title="Toggle {cat_title} for {meta["name"]}">'
                f'{check_prefix}{cat_title}</button>'
            )
        category_pills_html = f'<div class="category-pills-row" data-platform-pills="{_text(name)}">{"".join(pills)}</div>'

        model_dropdowns_html = ''
        if meta.get('models'):
            dropdowns = []
            for label, options in meta['models']:
                select_opts = []
                picker_items = []
                field_id = f"model-{_text(name.lower())}-{_text(label.lower().replace(' ', '-'))}"

                for opt in options:
                    if len(opt) >= 3:
                        val, title, desc = opt[0], opt[1], opt[2]
                    elif len(opt) == 2:
                        val, title, desc = opt[0], opt[1], ""
                    else:
                        val, title, desc = opt[0], opt[0], ""

                    opt_text = f"{title} · {desc}" if desc and title != desc else title
                    select_opts.append(f'<option value="{_text(val)}">{_text(opt_text)}</option>')

                    if not val:
                        picker_items.append(f'''
                        <div class="model-picker-item is-placeholder" data-value="" data-title="{_text(title)}" data-desc="" role="option">
                          <div class="model-picker-item-main">
                            <span class="model-item-title">{_text(title)}</span>
                          </div>
                          <span class="model-item-check">✓</span>
                        </div>''')
                    else:
                        desc_html = f'<span class="model-item-desc">{_text(desc)}</span>' if desc else ''
                        picker_items.append(f'''
                        <div class="model-picker-item" data-value="{_text(val)}" data-title="{_text(title)}" data-desc="{_text(desc)}" role="option">
                          <div class="model-picker-item-main">
                            <span class="model-item-title mono">{_text(val)}</span>
                            {desc_html}
                          </div>
                          <span class="model-item-check">✓</span>
                        </div>''')

                opts_html = "".join(select_opts)
                picker_items_html = "".join(picker_items)

                dropdowns.append(f'''
                <div class="model-select-group">
                  <span class="model-field-label">{_text(label)}</span>
                  <div class="model-searchable-picker" id="{field_id}" data-model-picker data-configured="{'true' if configured else 'false'}">
                    <button type="button" class="model-picker-trigger" aria-haspopup="listbox" aria-expanded="false" data-picker-trigger>
                      <span class="model-picker-selected-label mono">Select model...</span>
                      <span class="model-picker-chevron">▼</span>
                    </button>
                    <div class="model-picker-popover" data-picker-popover style="display:none;">
                      <div class="model-picker-search-bar">
                        <input type="text" class="model-picker-search" placeholder="Search models" autocomplete="off" spellcheck="false" data-picker-search>
                      </div>
                      <div class="model-picker-list" role="listbox" data-picker-list>
                        {picker_items_html}
                        <div class="model-picker-empty" data-picker-empty style="display:none;">No matching models found</div>
                      </div>
                    </div>
                    <select class="model-select visual-hidden" name="model_{_text(name)}_{_text(label.lower().replace(' ', '_'))}" data-native-select>{opts_html}</select>
                  </div>
                </div>''')
            warning_note = '' if configured else '<p class="orange-warning">Save the key to choose models.</p>'
            model_dropdowns_html = f'<div class="model-dropdowns-grid">{"".join(dropdowns)}</div>{warning_note}'

        placeholder_dots = '••••••••'
        remove_key_button = (
            f'<button type="button" class="btn-remove-key" data-remove-key="{_text(name)}">Remove key</button>'
            if configured else ''
        )

        key_cards.append(f'''
        <div class="provider-card" id="key-card-{_text(name.lower())}">
          <div class="provider-card-header">
            <div class="title-with-badge">
              <h3>{_text(name)}</h3>
              {badge}
            </div>
            <button type="button" class="btn-remove-platform-trigger"
                    data-remove-platform="{_text(name)}"
                    data-platform-title="{_text(meta['name'])}"
                    title="Remove platform from Studio">✕ Remove</button>
          </div>
          <p class="provider-card-subtitle">{_text(meta['slug'])} · tick every category it can do</p>
          {category_pills_html}
          {model_dropdowns_html}
          <form class="key-form" method="post" action="/settings/keys/{_text(name)}">
            {_csrf(csrf_token)}
            <input type="hidden" name="tab" value="api-keys">
            <div class="key-label-row">
              <label for="key-input-{_text(name)}">{'Replace key' if configured else 'Paste key'}</label>
              <span class="key-status-text muted">{'Configured · ••••••••' if configured else 'Key required'}</span>
            </div>
            <div class="key-input-action-group">
              <input id="key-input-{_text(name)}" class="key-input" name="value" type="password" autocomplete="new-password" placeholder="{placeholder_dots if configured else 'Paste key here…'}">
              <div class="key-submit-actions">
                <button type="submit" name="action" value="save" class="btn-save-key">Save key</button>
                {remove_key_button}
              </div>
            </div>
          </form>
        </div>''')

    dialogs_html = f'''
    <dialog class="confirm-dialog" id="confirm-remove-dialog">
      <form method="post" action="/settings/platforms/remove" class="confirm-dialog-content">
        {_csrf(csrf_token)}
        <input type="hidden" name="tab" value="api-keys">
        <input type="hidden" name="key_name" id="confirm-dialog-key" value="">
        <h3>Remove Platform</h3>
        <p id="confirm-dialog-message">Are you sure you want to remove this platform? Any saved API key will also be cleared.</p>
        <div class="confirm-dialog-actions">
          <button type="button" class="btn-cancel" data-close-dialog="confirm-remove-dialog" formmethod="dialog">Cancel</button>
          <button type="submit" class="btn-danger-confirm">Yes, Remove</button>
        </div>
      </form>
    </dialog>

    <dialog class="confirm-dialog" id="confirm-remove-key-dialog">
      <form method="post" action="" id="confirm-remove-key-form" class="confirm-dialog-content">
        {_csrf(csrf_token)}
        <input type="hidden" name="tab" value="api-keys">
        <input type="hidden" name="action" value="remove">
        <h3>Remove API Key</h3>
        <p id="confirm-remove-key-message">Are you sure you want to remove this API key from Studio?</p>
        <div class="confirm-dialog-actions">
          <button type="button" class="btn-cancel" data-close-dialog="confirm-remove-key-dialog" formmethod="dialog">Cancel</button>
          <button type="submit" class="btn-danger-confirm">Yes, Remove Key</button>
        </div>
      </form>
    </dialog>'''

    panes_html.append(f'''
    <div class="provider-tab-pane{api_pane_active}" data-tab="api-keys" role="tabpanel">
      {add_platform_html}
      {''.join(key_cards)}
    </div>''')

    content = f'''
    <p class="intro">Keys are stored in Studio's local data folder.</p>
    {nav_tabs_html}
    {''.join(panes_html)}
    {dialogs_html}'''

    return _page('Providers', '/providers', content, notice, script='/providers.js')


def settings_page(budgets, schedule, csrf_token, notice='', *, assets=(), presets=(),
                  open_asset=None, open_presets=False):
    cadence = schedule['cadence']
    weekdays = ''.join(
        f'<option value="{number}"{" selected" if number == schedule["weekday"] else ""}>{day}</option>'
        for number, day in enumerate(('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'))
    )
    content = f'''
    <div class="columns">
      <section class="panel">
        <h2>Budget</h2>
        <form class="stack" method="post" action="/settings/budgets">
          {_csrf(csrf_token)}
          <label>Daily limit (USD)
            <input type="number" name="daily" min="0" step="0.000001" value="{_usd(budgets['daily_micros'])}" required>
          </label>
          <label>Per-video limit (USD)
            <input type="number" name="video" min="0" step="0.000001" value="{_usd(budgets['video_micros'])}" required>
          </label>
          <small class="muted">A zero limit pauses paid requests.</small>
          <button type="submit">Save budget</button>
        </form>
      </section>
      <section class="panel">
        <h2>Idea generation schedule</h2>
        <form class="stack" method="post" action="/settings/idea-schedule">
          {_csrf(csrf_token)}
          <label><input type="checkbox" name="enabled" value="yes"{' checked' if schedule['enabled'] else ''}> Enable idea generation</label>
          <label>Repeat
            <select name="cadence">
              <option value="daily"{' selected' if cadence == 'daily' else ''}>Daily</option>
              <option value="weekly"{' selected' if cadence == 'weekly' else ''}>Weekly</option>
            </select>
          </label>
          <label>Day (weekly)<select name="weekday">{weekdays}</select></label>
          <label>Time <input type="time" name="run_at" value="{_text(schedule['run_at'])}" required></label>
          <label>Time zone <input name="timezone" value="{_text(schedule['timezone'])}" required></label>
          <label>Trends or themes <textarea name="trends" rows="3" maxlength="4000">{_text(schedule['trends'])}</textarea></label>
          <button type="submit">Save idea schedule</button>
        </form>
      </section>
    </div>'''
    return _page('Settings', '/settings', content, notice)
