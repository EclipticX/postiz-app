// studio.js - Shared UI interactions for Video Studio

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-generation-estimate]').forEach((form) => {
    const duration = form.querySelector('[name="duration"]');
    const narration = form.querySelector('[name="narration"]');
    const cost = form.querySelector('[data-estimated-cost]');
    const videoRate = Number(form.dataset.videoRate);
    const voiceRate = Number(form.dataset.voiceRate);
    const currency = new Intl.NumberFormat('en-US', {
      style: 'currency', currency: 'USD', maximumFractionDigits: 6,
    });
    const updateEstimate = () => {
      const seconds = Number(duration.value);
      if (!Number.isInteger(seconds) || seconds < 4 || seconds > 30) {
        cost.textContent = 'Enter a duration from 4 to 30 seconds to estimate cost.';
        return;
      }
      if (!videoRate) {
        cost.textContent = 'Set the video rate in Providers to estimate cost.';
        return;
      }
      const characters = narration ? Array.from(narration.value.trim()).length : 0;
      const total = seconds * videoRate + Math.ceil(characters * voiceRate / 1000);
      const amount = currency.format(total / 1_000_000);
      cost.textContent = narration && !voiceRate
        ? `Estimated video cost · ${amount}. Set the voice rate in Providers to estimate the total.`
        : `Estimated cost · ${amount} per video`;
    };
    duration.addEventListener('input', updateEstimate);
    if (narration) narration.addEventListener('input', updateEstimate);

    const voice = form.querySelector('[name="voice_reference"]');
    if (voice) {
      const cast = form.querySelectorAll('[name="character"]');
      const updateVoice = () => {
        const selected = new Set(Array.from(cast).filter((character) => character.checked).map((character) => character.value));
        Array.from(voice.options).forEach((option) => {
          option.disabled = Boolean(option.value) && !selected.has(option.value);
        });
        if (voice.value && !selected.has(voice.value)) voice.value = '';
      };
      cast.forEach((character) => character.addEventListener('change', updateVoice));
      updateVoice();
    }
  });

  const postReview = document.querySelector('[data-post-review]');
  if (postReview) {
    const channels = postReview.querySelectorAll('input[name="channel"]');
    const draft = postReview.querySelector('button[value="draft"]');
    const schedule = postReview.querySelector('button[value="schedule"]');
    const notice = postReview.querySelector('[data-post-review-notice]');
    const updateActions = () => {
      const selected = Array.from(channels).filter((channel) => channel.checked);
      const draftOnly = selected.some((channel) => !channel.hasAttribute('data-schedule-channel'));
      if (draft) draft.disabled = selected.length === 0;
      if (schedule) schedule.disabled = selected.length === 0 || draftOnly;
      notice.hidden = !draftOnly || !schedule;
      notice.textContent = notice.hidden ? '' : 'Select channels that support scheduling, or send a Postiz draft.';
    };
    channels.forEach((channel) => channel.addEventListener('change', updateActions));
    updateActions();
  }

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
