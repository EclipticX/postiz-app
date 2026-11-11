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
});
