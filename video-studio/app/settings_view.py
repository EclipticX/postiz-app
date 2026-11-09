from decimal import Decimal

from app.library_view import asset_library_section, production_presets_section
from app.web import _csrf, _page, _text


def _usd(micros):
    return format(Decimal(micros) / 1_000_000, 'f')


def settings_page(providers, budgets, schedule, csrf_token, notice='', *, assets=(), presets=(),
                  open_asset=None, open_presets=False):
    credentials = {route['key_env']: route['configured'] for route in providers}
    keys = ''.join(f'''
        <form class="record stack" method="post" action="/settings/keys/{_text(name)}">
          {_csrf(csrf_token)}
          <label for="key-{_text(name)}">{_text(name)}</label>
          <small>{'Configured · ••••••••' if configured else 'Key required'}</small>
          <input id="key-{_text(name)}" name="value" type="password" autocomplete="new-password">
          <div class="actions">
            <button type="submit" name="action" value="save">Save key</button>
            {'<button type="submit" name="action" value="remove">Remove key</button>' if configured else ''}
          </div>
        </form>''' for name, configured in credentials.items())
    routes = ''.join(f'''
        <form class="record stack" method="post" action="/settings/providers/{_text(route['name'])}">
          {_csrf(csrf_token)}
          <h3>{_text(route['name'])}</h3>
          <label><input type="checkbox" name="enabled" value="yes"{' checked' if route['enabled'] else ''}> Enabled</label>
          <label>Priority (lower runs first)
            <input type="number" name="priority" min="0" max="9999" value="{int(route['priority'])}" required>
          </label>
          <label>Estimated USD per {_text(route['billing_unit'].replace('_', ' '))}
            <input type="number" name="rate" min="0" step="0.000001" value="{_usd(route['rate_micros'])}" required>
          </label>
          <button type="submit">Save provider</button>
        </form>''' for route in providers)

    cadence = schedule['cadence']
    weekdays = ''.join(
        f'<option value="{number}"{" selected" if number == schedule["weekday"] else ""}>{day}</option>'
        for number, day in enumerate(('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'))
    )
    content = f'''
    <div class="columns">
      <section class="panel">
        <h2>API keys</h2>
        <p class="muted">Studio stores keys in its local data folder.</p>
        {keys}
      </section>
      <div class="stack">
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
      </div>
    </div>
    <section class="panel records">
      <h2>Providers</h2>
      {routes}
    </section>
    {asset_library_section(assets, csrf_token, open_asset)}
    {production_presets_section(presets, csrf_token, open_presets)}'''
    return _page('Settings', '/settings', content, notice)
