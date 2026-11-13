document.addEventListener('DOMContentLoaded', () => {
  // Open Add Brand Dialog
  const openAddBtn = document.getElementById('open-add-brand-dialog');
  const addDialog = document.getElementById('add-brand-dialog');
  if (openAddBtn && addDialog) {
    openAddBtn.addEventListener('click', () => {
      addDialog.showModal();
    });
  }

  // Open Detail Dialog for Brand Row
  document.querySelectorAll('[data-open-brand]').forEach(trigger => {
    trigger.addEventListener('click', (e) => {
      // Ignore clicks on inner action buttons or forms
      if (e.target.closest('form, button, a') && !e.target.closest('[data-open-brand]')) {
        return;
      }
      const brandId = trigger.getAttribute('data-open-brand');
      const dialog = document.getElementById('brand-dialog-' + brandId);
      if (dialog) {
        dialog.showModal();
      }
    });
  });

  // Close Dialogs via button with data-close-dialog
  document.querySelectorAll('[data-close-dialog]').forEach(btn => {
    btn.addEventListener('click', () => {
      const dialogId = btn.getAttribute('data-close-dialog');
      const dialog = document.getElementById(dialogId);
      if (dialog) {
        dialog.close();
      }
    });
  });

  // Close when clicking on the backdrop
  document.querySelectorAll('dialog.brand-dialog').forEach(dialog => {
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

  // Auto-open Add Dialog if server returned validation errors
  if (addDialog && addDialog.dataset.autoOpen === 'true') {
    addDialog.showModal();
  }
});
