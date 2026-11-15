// Must run before vendor/vosk/vosk.js: tells the patched library where its worker file is
// (extension CSP forbids the blob: worker that vosk-browser would otherwise create).
globalThis.VOSK_WORKER_URL = chrome.runtime.getURL('vendor/vosk/vosk-worker.js');
