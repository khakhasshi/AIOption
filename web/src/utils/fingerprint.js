// Lightweight browser fingerprint used to deter a single person from entering
// the beta lottery multiple times (incognito, cleared storage, multiple browser
// profiles) to inflate their odds. It is intentionally privacy-conservative:
// it only combines coarse, already-exposed device/browser traits and hashes
// them — no tracking cookies, no third-party libraries.

const STORAGE_KEY = 'ai_option_device_fp';

function safe(fn, fallback = '') {
  try {
    const value = fn();
    return value == null ? fallback : value;
  } catch {
    return fallback;
  }
}

function canvasSignal() {
  return safe(() => {
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    if (!ctx) return '';
    ctx.textBaseline = 'top';
    ctx.font = "14px 'Arial'";
    ctx.fillStyle = '#f60';
    ctx.fillRect(125, 1, 62, 20);
    ctx.fillStyle = '#069';
    ctx.fillText('AI-Option-Lottery-\u26a1', 2, 15);
    ctx.fillStyle = 'rgba(102, 204, 0, 0.7)';
    ctx.fillText('AI-Option-Lottery-\u26a1', 4, 17);
    return canvas.toDataURL();
  });
}

function webglSignal() {
  return safe(() => {
    const canvas = document.createElement('canvas');
    const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
    if (!gl) return '';
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    const vendor = ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : '';
    const renderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : '';
    return `${vendor}~${renderer}`;
  });
}

function collectSignals() {
  const nav = window.navigator || {};
  const scr = window.screen || {};
  return [
    safe(() => nav.userAgent),
    safe(() => (nav.languages || []).join(',')) || safe(() => nav.language),
    safe(() => nav.platform),
    safe(() => nav.hardwareConcurrency),
    safe(() => nav.deviceMemory),
    safe(() => nav.maxTouchPoints),
    safe(() => `${scr.width}x${scr.height}x${scr.colorDepth}`),
    safe(() => `${scr.availWidth}x${scr.availHeight}`),
    safe(() => window.devicePixelRatio),
    safe(() => Intl.DateTimeFormat().resolvedOptions().timeZone),
    safe(() => new Date().getTimezoneOffset()),
    canvasSignal(),
    webglSignal(),
  ].join('|');
}

async function hashString(input) {
  // Prefer SubtleCrypto (available on secure contexts) for a stable SHA-256.
  if (window.crypto?.subtle?.digest) {
    try {
      const bytes = new TextEncoder().encode(input);
      const digest = await window.crypto.subtle.digest('SHA-256', bytes);
      return Array.from(new Uint8Array(digest))
        .map((b) => b.toString(16).padStart(2, '0'))
        .join('')
        .slice(0, 32);
    } catch {
      /* fall through to non-crypto hash */
    }
  }
  // Fallback: FNV-1a style 32-bit hash, repeated for a longer hex string.
  let h1 = 0x811c9dc5;
  let h2 = 0xc2b2ae35;
  for (let i = 0; i < input.length; i += 1) {
    const c = input.charCodeAt(i);
    h1 = Math.imul(h1 ^ c, 0x01000193);
    h2 = Math.imul(h2 ^ c, 0x85ebca6b);
  }
  const hex = (n) => (n >>> 0).toString(16).padStart(8, '0');
  return (hex(h1) + hex(h2) + hex(h1 ^ h2) + hex(Math.imul(h1, 0x27d4eb2f))).slice(0, 32);
}

// Returns a stable 32-char hex fingerprint for the current device/browser.
// The result is cached in localStorage so it stays consistent across reloads;
// the cache is only a convenience — the underlying signals reproduce the same
// hash even if storage is cleared.
export async function getDeviceFingerprint() {
  try {
    const cached = window.localStorage.getItem(STORAGE_KEY);
    if (cached && /^[0-9a-f]{16,}$/i.test(cached)) return cached;
  } catch {
    /* storage may be unavailable */
  }
  const fp = await hashString(collectSignals());
  try {
    window.localStorage.setItem(STORAGE_KEY, fp);
  } catch {
    /* ignore storage write failures */
  }
  return fp;
}
