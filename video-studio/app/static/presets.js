document.addEventListener('DOMContentLoaded', () => {
  const tabs = document.querySelectorAll('[data-tab-btn]');
  const panes = document.querySelectorAll('[data-tab-pane]');

  function activateTab(tabId) {
    tabs.forEach(tab => {
      const match = tab.getAttribute('data-tab-btn') === tabId;
      tab.classList.toggle('active', match);
      tab.setAttribute('aria-selected', match ? 'true' : 'false');
    });

    panes.forEach(pane => {
      pane.classList.toggle('active', pane.getAttribute('data-tab-pane') === tabId);
    });
  }

  tabs.forEach(tab => {
    tab.addEventListener('click', (e) => {
      const tabId = tab.getAttribute('data-tab-btn');
      if (tabId) {
        e.preventDefault();
        activateTab(tabId);
        const url = new URL(window.location);
        url.searchParams.set('tab', tabId);
        window.history.replaceState({}, '', url);
      }
    });
  });

  // Open Add Preset Dialog for currently active tab
  const openAddPresetBtn = document.getElementById('open-add-preset-dialog');
  if (openAddPresetBtn) {
    openAddPresetBtn.addEventListener('click', () => {
      const activeTab = document.querySelector('[data-tab-btn].active')?.getAttribute('data-tab-btn') || 'visual';
      const dialog = document.getElementById('add-preset-dialog-' + activeTab);
      if (dialog && typeof dialog.showModal === 'function') {
        dialog.showModal();
      }
    });
  }

  // Close dialog buttons
  document.querySelectorAll('[data-close-dialog]').forEach(btn => {
    btn.addEventListener('click', () => {
      const dialogId = btn.getAttribute('data-close-dialog');
      const dialog = document.getElementById(dialogId);
      if (dialog && typeof dialog.close === 'function') {
        dialog.close();
      }
    });
  });

  // Close when clicking on backdrop
  document.querySelectorAll('dialog.brand-dialog').forEach(dialog => {
    dialog.addEventListener('click', (e) => {
      const rect = dialog.getBoundingClientRect();
      const inDialog = (
        rect.top <= e.clientY && e.clientY <= rect.top + rect.height &&
        rect.left <= e.clientX && e.clientX <= rect.left + rect.width
      );
      if (!inDialog && typeof dialog.close === 'function') {
        dialog.close();
      }
    });
  });
});
