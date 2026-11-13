"""Brand Rules and Mother Prompt configuration screen."""

from urllib.parse import quote

from app.library_view import _identifier_input
from app.web import _csrf, _page, _text


def _brand_item(brand, total_count=1):
    brand_id = brand["id"]
    name = _text(brand["name"])
    is_default = bool(brand.get("is_default"))
    mother_prompt = brand.get("mother_prompt", "").strip()
    excerpt = _text(mother_prompt.replace("\n", " ")[:130] + ("..." if len(mother_prompt) > 130 else ""))
    status_badge = '<span class="status status-ready">Default</span>' if is_default else ""

    return f"""
    <li class="brand-list-item" data-open-brand="{_text(brand_id)}">
      <div class="brand-item-main">
        <div class="brand-item-title-row">
          <h3>{name}</h3>
          <small class="muted"><code>{_text(brand_id)}</code></small>
          {status_badge}
        </div>
        <p class="brand-item-excerpt muted">{excerpt or "No mother prompt configured."}</p>
      </div>
      <div class="brand-item-actions">
        <button type="button" class="button-secondary" data-open-brand="{_text(brand_id)}">View Details</button>
      </div>
    </li>"""


def _brand_detail_dialog(brand, csrf_token, total_count=1):
    brand_id = brand["id"]
    brand_id_escaped = _text(quote(str(brand_id), safe=""))
    name = _text(brand["name"])
    is_default = bool(brand.get("is_default"))
    dialog_id = f"brand-dialog-{_text(brand_id)}"

    actions = []
    if is_default:
        actions.append('<span class="status status-ready">Default</span>')
    else:
        actions.append(f"""
        <form method="post" action="/brand-rules/{brand_id_escaped}/default" style="display:inline;">
          {_csrf(csrf_token)}
          <button type="submit" class="button-secondary">Set as default</button>
        </form>""")
        if total_count > 1:
            actions.append(f"""
            <form method="post" action="/brand-rules/{brand_id_escaped}/delete" style="display:inline;" onsubmit="return confirm('Are you sure you want to delete this brand rule?');">
              {_csrf(csrf_token)}
              <button type="submit" class="button-secondary" style="color:var(--danger,#b91c1c);">Delete</button>
            </form>""")

    actions.append(f'<button type="button" class="button-secondary" data-close-dialog="{dialog_id}">Close</button>')
    actions_html = f'<div class="confirm-dialog-actions" style="margin-top:1.25rem;">{"".join(actions)}</div>'

    mother_prompt = brand.get("mother_prompt", "").strip()
    brand_rules = brand.get("brand_rules", "").strip()
    story_rules = brand.get("story_rules", "").strip()

    brand_rules_list = [r.strip("-*• \t") for r in brand_rules.splitlines() if r.strip()]
    story_rules_list = [r.strip("-*• \t") for r in story_rules.splitlines() if r.strip()]

    brand_rules_html = (
        "<ul>" + "".join(f"<li>{_text(r)}</li>" for r in brand_rules_list) + "</ul>"
        if brand_rules_list
        else '<p class="muted">No brand continuity rules defined.</p>'
    )
    story_rules_html = (
        "<ul>" + "".join(f"<li>{_text(r)}</li>" for r in story_rules_list) + "</ul>"
        if story_rules_list
        else '<p class="muted">No story rhythm guidelines defined.</p>'
    )

    return f"""
    <dialog class="brand-dialog" id="{dialog_id}">
      <div class="brand-dialog-header">
        <div>
          <h3>{name}</h3>
          <small class="muted"><code>{_text(brand_id)}</code></small>
        </div>
        <button type="button" class="brand-dialog-close" data-close-dialog="{dialog_id}" aria-label="Close dialog">&times;</button>
      </div>
      <div class="preset-details stack">
        <div>
          <strong>Mother Prompt / Style Rhythm Directive</strong>
          <p class="prompt">{_text(mother_prompt)}</p>
        </div>
        <div>
          <strong>Brand &amp; Character Continuity Rules</strong>
          {brand_rules_html}
        </div>
        <div>
          <strong>Story Rhythm &amp; Narrative Guidelines</strong>
          {story_rules_html}
        </div>
      </div>
      {actions_html}
    </dialog>"""


def _add_brand_dialog(csrf_token, values=None, auto_open=False):
    values = values or {}
    dialog_id = "add-brand-dialog"
    auto_open_attr = ' data-auto-open="true"' if auto_open else ""

    return f"""
    <dialog class="brand-dialog" id="{dialog_id}"{auto_open_attr}>
      <div class="brand-dialog-header">
        <h2>Add Brand Rule &amp; Mother Prompt</h2>
        <button type="button" class="brand-dialog-close" data-close-dialog="{dialog_id}" aria-label="Close dialog">&times;</button>
      </div>
      <form class="stack library-body" method="post" action="/brand-rules">
        {_csrf(csrf_token)}
        <div class="fields">
          {_identifier_input('brand_id', 'Brand ID')}
          <label>Brand name<input name="name" maxlength="100" placeholder="e.g. Bedtime Rhymes" value="{_text(values.get('name', ''))}" required></label>
        </div>
        <label>Mother Prompt (Visual &amp; Style Rhythm Directive)
          <textarea name="mother_prompt" rows="5" placeholder="Base instructions, visual style, rendering directives applied across all videos..." required>{_text(values.get('mother_prompt', ''))}</textarea>
        </label>
        <label>Brand &amp; Character Continuity Rules (one per line)
          <textarea name="brand_rules" rows="4" placeholder="- Characters must maintain exact proportions&#10;- Keep color palette bright and cheerful&#10;- No scary or violent themes">{_text(values.get('brand_rules', ''))}</textarea>
        </label>
        <label>Story Rhythm &amp; Narrative Guidelines (one per line)
          <textarea name="story_rules" rows="4" placeholder="- Scene 1 introduces the problem playfully&#10;- Scene 2 builds rhythmic repetition&#10;- Scene 3 celebrates the gentle resolution">{_text(values.get('story_rules', ''))}</textarea>
        </label>
        <label><input type="checkbox" name="is_default" value="yes"{' checked' if values.get('is_default') else ''}> Set as default brand rule</label>
        <div class="confirm-dialog-actions" style="margin-top:1rem;">
          <button type="button" class="btn-cancel" data-close-dialog="{dialog_id}">Cancel</button>
          <button type="submit" class="primary">Save brand profile</button>
        </div>
      </form>
    </dialog>"""


def brand_rules_page(brand_profiles, csrf_token, notice="", values=None):
    values = values or {}
    total_count = len(brand_profiles)
    items = "".join(_brand_item(b, total_count=total_count) for b in brand_profiles)
    if not items:
        items = '<li class="muted" style="padding:1.5rem;">No brand profiles found. Add one above.</li>'

    dialogs = "".join(_brand_detail_dialog(b, csrf_token, total_count=total_count) for b in brand_profiles)
    auto_open_add = bool(notice and values)
    add_dialog = _add_brand_dialog(csrf_token, values=values, auto_open=auto_open_add)

    heading_action = '<button type="button" class="primary" id="open-add-brand-dialog">+ Add Brand Rule</button>'

    content = f"""
    <p class="intro">Manage brand identity, Mother Prompts, character rhythm, and storytelling constraints across videos.</p>
    <section class="panel" aria-label="Brand profiles">
      <ul class="brand-list">
        {items}
      </ul>
    </section>
    {dialogs}
    {add_dialog}"""

    return _page(
        "Brand Rules",
        "/brand-rules",
        content,
        notice,
        script="/brand_rules.js",
        heading_action=heading_action,
    )
