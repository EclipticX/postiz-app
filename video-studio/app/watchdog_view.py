"""Watch Dog issue report and details."""

from app.inbox_view import _date
from app.web import _label, _page, _text


_ACTIVE = '/watch-dog'


def _issue_row(issue):
    reason = issue['reason']
    summary = reason[:159] + '…' if len(reason) > 160 else reason
    return f'''
    <tr>
      <td>{_date(issue['occurred_at'], with_time=True)}</td>
      <td>{_text(issue['source'])}</td>
      <td>{_label(issue['status'])}</td>
      <td>{_text(issue['item'])}</td>
      <td>
        <div>{_text(summary)}</div>
        <a href="{_ACTIVE}/{_text(issue['id'])}" aria-label="View {_text(issue['source'])} issue for {_text(issue['item'])}">View details</a>
      </td>
    </tr>'''


def watchdog_page(issues):
    rows = ''.join(_issue_row(issue) for issue in issues)
    if not rows:
        rows = '<tr><td colspan="5" class="muted">No issues found in the monitored Studio features.</td></tr>'
    return _page('Watch Dog', _ACTIVE, f'''
    <p class="intro">Review recorded Studio workflow errors and provider configuration blockers.</p>
    <section class="panel">
      <div class="published-table-wrap" role="region" aria-label="Watch Dog issues" tabindex="0">
        <table class="published-table published-list-table watchdog-table" aria-label="Watch Dog issues">
          <thead><tr>
            <th scope="col">Time</th><th scope="col">Feature / stage</th><th scope="col">Status</th>
            <th scope="col">Affected item</th><th scope="col">Issue</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </section>''', '')


def watchdog_issue_page(issue):
    fields = [
        ('Time', _date(issue['occurred_at'], with_time=True)),
        ('Feature / stage', _text(issue['source'])),
        ('Status', _label(issue['status'])),
        ('Affected item', _text(issue['item'])),
    ]
    if issue['attempts'] is not None:
        fields.append(('Attempts', _text(issue['attempts'])))
    metadata = ''.join(f'<div><dt class="muted">{label}</dt><dd>{value}</dd></div>' for label, value in fields)
    related = ''
    if issue['origin_url']:
        label = issue['origin_label'] or 'Open related record'
        related = f'<p><a href="{_text(issue["origin_url"])}">{_text(label)}</a></p>'
    return _page('Issue details', _ACTIVE, f'''
    <p class="intro"><a href="{_ACTIVE}">Back to Watch Dog</a></p>
    <article class="panel">
      <dl class="inbox-record-fields">{metadata}</dl>
      <section class="record" aria-labelledby="watchdog-reason">
        <h2 id="watchdog-reason">Reason</h2>
        <p class="prompt">{_text(issue['reason'])}</p>
      </section>
      {related}
    </article>''', '')
