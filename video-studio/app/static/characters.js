const hexColor = /^#[0-9A-Fa-f]{6}$/;

for (const field of document.querySelectorAll('[data-color-field]')) {
  const picker = field.querySelector('[data-color-picker]');
  const hex = field.querySelector('[data-color-hex]');
  picker.addEventListener('input', () => {
    hex.value = picker.value.toUpperCase();
  });
  hex.addEventListener('input', () => {
    if (hexColor.test(hex.value)) picker.value = hex.value;
  });
}

const preview = document.querySelector('[data-preview-dialog]');
if (preview?.showModal) {
  const image = preview.querySelector('img');
  document.addEventListener('click', (event) => {
    const link = event.target.closest('[data-image-preview]');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    image.src = link.href;
    image.alt = link.querySelector('img').alt;
    preview.showModal();
  });
  preview.addEventListener('close', () => image.removeAttribute('src'));
}

const upload = document.querySelector('[data-reference-upload]');
if (upload && globalThis.crypto?.randomUUID) {
  const input = upload.elements.namedItem('image');
  const submit = upload.querySelector('button[type="submit"]');
  const queue = upload.querySelector('[data-upload-queue]');
  const note = upload.querySelector('[data-upload-note]');
  let uploading = false;
  input.multiple = true;

  function setUploading(value) {
    uploading = value;
    submit.disabled = value;
    input.disabled = value;
    for (const button of queue.querySelectorAll('button')) button.disabled = value;
  }

  async function sendImage(entry) {
    entry.status.textContent = 'Uploading…';
    entry.retry.hidden = true;
    try {
      const response = await fetch(upload.action, {
        method: 'POST',
        headers: { Accept: 'application/json' },
        body: entry.data,
      });
      if (response.status !== 201) {
        const json = response.headers.get('Content-Type')?.includes('application/json');
        const result = json ? await response.json() : {};
        const errors = {
          403: 'Reload this page to renew the upload session.',
          413: 'Choose an image up to 10 MiB.',
        };
        throw new Error(result.notice || errors[response.status] || 'Upload failed. Try again.');
      }
      entry.status.textContent = 'Uploaded';
      return true;
    } catch (error) {
      entry.status.textContent = error instanceof TypeError ? 'Connection failed. Try again.' : error.message;
      entry.retry.hidden = false;
      return false;
    }
  }

  async function refreshGallery() {
    try {
      const response = await fetch(window.location.pathname);
      if (!response.ok) throw new Error();
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      const gallery = page.querySelector('[data-reference-gallery]');
      if (!gallery) throw new Error();
      document.querySelector('[data-reference-gallery]').replaceWith(gallery);
      note.hidden = true;
    } catch {
      note.textContent = 'Images were saved. Reload this page to refresh the gallery.';
      note.hidden = false;
    }
  }

  function enqueue(file, index, fields) {
    const row = document.createElement('li');
    row.className = 'upload-row';
    const name = document.createElement('span');
    name.textContent = file.name;
    const status = document.createElement('span');
    status.setAttribute('role', 'status');
    status.textContent = 'Waiting';
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.textContent = 'Retry';
    retry.setAttribute('aria-label', `Retry ${file.name}`);
    retry.hidden = true;
    row.append(name, status, retry);
    queue.append(row);

    const data = new FormData();
    data.set('csrf_token', fields.get('csrf_token'));
    data.set('angle', fields.get('angle'));
    data.set('variant_id', fields.get('variant_id'));
    data.set('upload_id', 'ref_' + crypto.randomUUID().replaceAll('-', ''));
    if (index === 0 && fields.has('primary')) data.set('primary', 'yes');
    data.set('image', file);
    const entry = { data, status, retry };
    retry.addEventListener('click', async () => {
      if (uploading) return;
      setUploading(true);
      if (await sendImage(entry)) await refreshGallery();
      setUploading(false);
    });
    return entry;
  }

  upload.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (uploading || !input.files.length) return;
    const fields = new FormData(upload);
    queue.replaceChildren();
    queue.hidden = false;
    const entries = Array.from(input.files, (file, index) => enqueue(file, index, fields));
    input.value = '';
    setUploading(true);
    let saved = false;
    for (const entry of entries) {
      if (await sendImage(entry)) saved = true;
    }
    if (saved) await refreshGallery();
    setUploading(false);
  });
}
