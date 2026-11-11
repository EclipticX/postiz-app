function switchTab(tabId, focusId) {
  const tabs = document.querySelectorAll('.providers-tab-btn[data-tab]');
  const panes = document.querySelectorAll('.provider-tab-pane');

  let targetPane = null;
  panes.forEach(pane => {
    if (pane.getAttribute('data-tab') === tabId) {
      pane.classList.add('active');
      targetPane = pane;
    } else {
      pane.classList.remove('active');
    }
  });

  tabs.forEach(tab => {
    if (tab.getAttribute('data-tab') === tabId) {
      tab.classList.add('active');
      tab.setAttribute('aria-selected', 'true');
    } else {
      tab.classList.remove('active');
      tab.setAttribute('aria-selected', 'false');
    }
  });

  const url = new URL(window.location.href);
  url.searchParams.set('tab', tabId);
  window.history.replaceState({}, '', url.toString());

  if (focusId) {
    setTimeout(() => {
      const el = document.getElementById(focusId);
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        el.focus();
      }
    }, 100);
  }
}

function openRemoveDialog(keyName, platformTitle) {
  const dialog = document.getElementById('confirm-remove-dialog');
  const input = document.getElementById('confirm-dialog-key');
  const msg = document.getElementById('confirm-dialog-message');
  if (dialog && input && msg) {
    input.value = keyName;
    msg.textContent = `Are you sure you want to remove ${platformTitle || keyName}? Any saved API key will also be cleared.`;
    if (typeof dialog.showModal === 'function') {
      dialog.showModal();
    }
  }
}

function openRemoveKeyDialog(keyName) {
  const dialog = document.getElementById('confirm-remove-key-dialog');
  const form = document.getElementById('confirm-remove-key-form');
  const msg = document.getElementById('confirm-remove-key-message');
  if (dialog && form && msg) {
    form.action = `/settings/keys/${encodeURIComponent(keyName)}`;
    msg.textContent = `Are you sure you want to remove the API key for ${keyName}?`;
    if (typeof dialog.showModal === 'function') {
      dialog.showModal();
    }
  }
}

function blurActiveElement() {
  if (document.activeElement && typeof document.activeElement.blur === 'function' && document.activeElement !== document.body) {
    document.activeElement.blur();
  }
}

function closeAllModelPickers() {
  document.querySelectorAll('[data-model-picker]').forEach(picker => {
    picker.classList.remove('is-open');
    const popover = picker.querySelector('[data-picker-popover]');
    const trigger = picker.querySelector('[data-picker-trigger]');
    if (popover) popover.style.display = 'none';
    if (trigger) {
      trigger.setAttribute('aria-expanded', 'false');
      if (typeof trigger.blur === 'function') {
        trigger.blur();
      }
    }
  });
}

function toggleModelPicker(picker) {
  const popover = picker.querySelector('[data-picker-popover]');
  const trigger = picker.querySelector('[data-picker-trigger]');
  const searchInput = picker.querySelector('[data-picker-search]');
  if (!popover) return;

  const isOpen = picker.classList.contains('is-open');
  closeAllModelPickers();

  if (!isOpen) {
    picker.classList.add('is-open');
    popover.style.display = 'block';
    if (trigger) trigger.setAttribute('aria-expanded', 'true');
    if (searchInput) {
      searchInput.value = '';
      filterModelPicker(picker, '');
      setTimeout(() => searchInput.focus(), 50);
    }
  }
}

function filterModelPicker(picker, query) {
  const q = (query || '').trim().toLowerCase();
  const items = picker.querySelectorAll('.model-picker-item');
  const empty = picker.querySelector('[data-picker-empty]');
  let visibleCount = 0;

  items.forEach(item => {
    const val = (item.getAttribute('data-value') || '').toLowerCase();
    const title = (item.getAttribute('data-title') || '').toLowerCase();
    const desc = (item.getAttribute('data-desc') || '').toLowerCase();
    const matches = !q || val.includes(q) || title.includes(q) || desc.includes(q);
    if (matches) {
      item.style.display = 'flex';
      visibleCount++;
    } else {
      item.style.display = 'none';
    }
  });

  if (empty) {
    empty.style.display = visibleCount === 0 ? 'block' : 'none';
  }
}

function selectPickerItem(picker, item) {
  const val = item.getAttribute('data-value') || '';
  const title = item.getAttribute('data-title') || '';
  const select = picker.querySelector('[data-native-select]');
  const triggerLabel = picker.querySelector('.model-picker-selected-label');

  if (select) {
    select.value = val;
    select.dispatchEvent(new Event('change', { bubbles: true }));
  }

  if (triggerLabel) {
    triggerLabel.textContent = val || title || 'Select model...';
  }

  picker.querySelectorAll('.model-picker-item').forEach(it => {
    it.classList.toggle('is-selected', it === item);
  });

  const pickerId = picker.id;
  if (pickerId) {
    try {
      localStorage.setItem('postiz_model_' + pickerId, val);
    } catch (_) {}
  }

  closeAllModelPickers();
  const trigger = picker.querySelector('[data-picker-trigger]');
  if (trigger && typeof trigger.blur === 'function') {
    trigger.blur();
  }
}

function initModelPickers() {
  document.querySelectorAll('[data-model-picker]').forEach(picker => {
    const pickerId = picker.id;
    const select = picker.querySelector('[data-native-select]');
    const triggerLabel = picker.querySelector('.model-picker-selected-label');
    const items = picker.querySelectorAll('.model-picker-item');

    let savedVal = '';
    if (pickerId) {
      try {
        savedVal = localStorage.getItem('postiz_model_' + pickerId) || '';
      } catch (_) {}
    }

    if (!savedVal && select && select.value) {
      savedVal = select.value;
    }

    if (savedVal) {
      const matched = Array.from(items).find(it => it.getAttribute('data-value') === savedVal);
      if (matched) {
        if (select) select.value = savedVal;
        if (triggerLabel) triggerLabel.textContent = savedVal;
        items.forEach(it => it.classList.toggle('is-selected', it === matched));
        return;
      }
    }

    // Default to unselected placeholder
    if (select) select.value = '';
    if (triggerLabel) triggerLabel.textContent = 'Select model...';
    items.forEach(it => it.classList.toggle('is-selected', !it.getAttribute('data-value')));
  });
}

document.addEventListener('DOMContentLoaded', () => {
  // Tab buttons
  const tabs = document.querySelectorAll('.providers-tab-btn[data-tab]');
  tabs.forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      const tabId = btn.getAttribute('data-tab');
      switchTab(tabId);
    });
  });

  // Tab plus button
  const plusBtn = document.querySelector('[data-tab-plus]');
  if (plusBtn) {
    plusBtn.addEventListener('click', (e) => {
      e.preventDefault();
      const tabId = plusBtn.getAttribute('data-tab-plus') || 'api-keys';
      switchTab(tabId);
    });
  }

  // Shortcut from missing key banner to specific API key input
  const addKeyButtons = document.querySelectorAll('[data-switch-to-key]');
  addKeyButtons.forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      const keyName = btn.getAttribute('data-switch-to-key');
      switchTab('api-keys', 'key-input-' + keyName);
    });
  });

  // Custom platform fields toggle in Add Platform form
  const platformDropdown = document.querySelector('[data-platform-dropdown]');
  if (platformDropdown) {
    platformDropdown.addEventListener('change', () => {
      const customFields = document.getElementById('custom-platform-inputs');
      if (customFields) {
        if (platformDropdown.value === 'custom') {
          customFields.style.display = 'block';
          const input = customFields.querySelector('input[name="custom_key"]');
          if (input) input.focus();
        } else {
          customFields.style.display = 'none';
        }
      }
    });
  }

  // Delegated click handler for remove platform, remove key, disable provider, and close dialog
  document.addEventListener('click', (e) => {
    // 1. Remove platform trigger
    const removePlatformBtn = e.target.closest('[data-remove-platform]');
    if (removePlatformBtn) {
      e.preventDefault();
      const keyName = removePlatformBtn.getAttribute('data-remove-platform');
      const platformTitle = removePlatformBtn.getAttribute('data-platform-title');
      openRemoveDialog(keyName, platformTitle);
      return;
    }

    // 2. Remove key trigger
    const removeKeyBtn = e.target.closest('[data-remove-key]');
    if (removeKeyBtn) {
      e.preventDefault();
      const keyName = removeKeyBtn.getAttribute('data-remove-key');
      openRemoveKeyDialog(keyName);
      return;
    }

    // 3. Disable provider (Remove from category run order)
    const disableBtn = e.target.closest('[data-disable-provider]');
    if (disableBtn) {
      e.preventDefault();
      const form = disableBtn.closest('form');
      if (form) {
        const checkbox = form.querySelector('input[type="checkbox"][name="enabled"]');
        if (checkbox) {
          checkbox.checked = false;
        }
        form.submit();
      }
      return;
    }

    // 4. Close dialog button
    const closeDialogBtn = e.target.closest('[data-close-dialog]');
    if (closeDialogBtn) {
      e.preventDefault();
      const dialogId = closeDialogBtn.getAttribute('data-close-dialog');
      const dialog = closeDialogBtn.closest('dialog') || (dialogId ? document.getElementById(dialogId) : null);
      if (dialog && typeof dialog.close === 'function') {
        dialog.close();
      }
      return;
    }

    // 5. Category pill toggle
    const catPill = e.target.closest('[data-toggle-cat]');
    if (catPill) {
      e.preventDefault();
      const platform = catPill.getAttribute('data-platform');
      const catId = catPill.getAttribute('data-toggle-cat');
      const catTitle = catPill.getAttribute('data-cat-title') || catId;
      const pillsRow = catPill.closest('[data-platform-pills]');

      const wasActive = catPill.classList.contains('active');
      const nowActive = !wasActive;
      catPill.classList.toggle('active', nowActive);
      catPill.setAttribute('aria-pressed', nowActive ? 'true' : 'false');
      catPill.textContent = (nowActive ? '✓ ' : '') + catTitle;

      if (pillsRow && platform) {
        const activeButtons = pillsRow.querySelectorAll('.category-pill.active');
        const activeCats = Array.from(activeButtons).map(b => b.getAttribute('data-toggle-cat'));

        const csrfInput = document.querySelector('input[name="csrf_token"]');
        const csrfToken = csrfInput ? csrfInput.value : '';

        fetch('/settings/platforms/categories', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json',
          },
          body: new URLSearchParams({
            csrf_token: csrfToken,
            platform_key: platform,
            categories: activeCats.join(','),
            tab: 'api-keys'
          })
        }).catch(err => {
          console.error('Failed to save category preference', err);
        });
      }
      return;
    }

    // 6. Searchable Model Picker Trigger
    const pickerTrigger = e.target.closest('[data-picker-trigger]');
    if (pickerTrigger) {
      e.preventDefault();
      const picker = pickerTrigger.closest('[data-model-picker]');
      if (picker) {
        toggleModelPicker(picker);
      }
      return;
    }

    // 6. Model Picker Item Selection
    const pickerItem = e.target.closest('.model-picker-item');
    if (pickerItem) {
      e.preventDefault();
      const picker = pickerItem.closest('[data-model-picker]');
      if (picker) {
        selectPickerItem(picker, pickerItem);
      }
      return;
    }

    // Close any open model picker when clicking outside
    if (!e.target.closest('[data-model-picker]')) {
      closeAllModelPickers();
    }
  });

  // Search input typing filter
  document.addEventListener('input', (e) => {
    const searchInput = e.target.closest('[data-picker-search]');
    if (searchInput) {
      const picker = searchInput.closest('[data-model-picker]');
      if (picker) {
        filterModelPicker(picker, searchInput.value);
      }
    }
  });

  // Escape key closes pickers
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeAllModelPickers();
    }
  });

  // Initialize model pickers from saved preferences or defaults
  initModelPickers();
  closeAllModelPickers();
  blurActiveElement();
  setTimeout(blurActiveElement, 50);

  // Close dialogs when clicking on the backdrop
  const dialogs = document.querySelectorAll('dialog.confirm-dialog');
  dialogs.forEach(dialog => {
    dialog.addEventListener('click', (e) => {
      const rect = dialog.getBoundingClientRect();
      const clickedInDialog = (
        rect.top <= e.clientY && e.clientY <= rect.top + rect.height &&
        rect.left <= e.clientX && e.clientX <= rect.left + rect.width
      );
      if (!clickedInDialog && typeof dialog.close === 'function') {
        dialog.close();
      }
    });
  });
});

window.addEventListener('pageshow', () => {
  closeAllModelPickers();
  blurActiveElement();
});
