// studio.js - Shared UI interactions for Video Studio

document.addEventListener('DOMContentLoaded', () => {
  // Close any open action menu when clicking outside
  document.addEventListener('click', (event) => {
    document.querySelectorAll('.action-menu[open]').forEach((menu) => {
      if (!menu.contains(event.target)) {
        menu.removeAttribute('open');
      }
    });

    // Handle data-close-dialog buttons
    const closeBtn = event.target.closest('[data-close-dialog]');
    if (closeBtn) {
      const dialogId = closeBtn.getAttribute('data-close-dialog');
      const dialog = document.getElementById(dialogId);
      if (dialog && typeof dialog.close === 'function') {
        dialog.close();
      }
    }

    // Handle data-open-dialog buttons
    const openBtn = event.target.closest('[data-open-dialog]');
    if (openBtn) {
      const dialogId = openBtn.getAttribute('data-open-dialog');
      const dialog = document.getElementById(dialogId);
      if (dialog && typeof dialog.showModal === 'function') {
        const parentMenu = openBtn.closest('.action-menu[open]');
        if (parentMenu) {
          parentMenu.removeAttribute('open');
        }
        dialog.showModal();
      }
    }
  });

  // Close brand-dialog when clicking on backdrop
  document.querySelectorAll('dialog.brand-dialog').forEach((dialog) => {
    dialog.addEventListener('click', (e) => {
      const rect = dialog.getBoundingClientRect();
      const inDialog = (
        rect.top <= e.clientY && e.clientY <= rect.top + rect.height &&
        rect.left <= e.clientX && e.clientX <= rect.left + rect.width
      );
      if (!inDialog) {
        dialog.close();
      }
    });
  });
});
