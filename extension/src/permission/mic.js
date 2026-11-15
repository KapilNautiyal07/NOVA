const dark = matchMedia('(prefers-color-scheme: dark)').matches;
chrome.storage.local.get('settings').then(({ settings }) => {
  const t = settings?.theme || 'auto';
  document.documentElement.dataset.theme = t === 'auto' ? (dark ? 'dark' : 'light') : t;
});

const status = document.getElementById('status');
document.getElementById('grant').addEventListener('click', async () => {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((t) => t.stop()); // we only needed the permission grant
    status.textContent = '✓ Microphone allowed. You can close this tab — say “Hey NOVA” or use the mic button.';
    chrome.runtime.sendMessage({ type: 'nova:wakeSync' }).catch(() => {}); // start the listener if it's turned on
    setTimeout(() => window.close(), 2500);
  } catch (e) {
    status.textContent =
      e.name === 'NotAllowedError'
        ? 'Permission was denied. Click the 🔒 icon in the address bar → Site settings → Microphone → Allow, then try again. You can always type instead.'
        : `Couldn't access a microphone (${e.name}). You can always type instead.`;
  }
});
