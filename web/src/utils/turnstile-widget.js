// Browser-side helper to render a Cloudflare Turnstile widget and surface its
// single-use token to the login form.  The backend re-verifies that token at
// /api/auth/login and /api/auth/oauth/login before issuing a session.
//
// The Cloudflare script is loaded lazily on first use so the login page does not
// pay for it when Turnstile is unconfigured (no site key from the config API).

const TURNSTILE_SRC = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';

let scriptPromise = null;

// Load the Turnstile script once and resolve when window.turnstile is ready.
function loadTurnstileScript() {
  if (scriptPromise) return scriptPromise;
  scriptPromise = new Promise((resolve, reject) => {
    if (window.turnstile) {
      resolve(window.turnstile);
      return;
    }
    const finish = () => {
      if (window.turnstile) resolve(window.turnstile);
      else reject(new Error('turnstile script loaded but window.turnstile is missing'));
    };
    const existing = document.querySelector(`script[src="${TURNSTILE_SRC}"]`);
    if (existing) {
      if (existing.dataset.loaded === '1') finish();
      else {
        existing.addEventListener('load', finish);
        existing.addEventListener('error', () => reject(new Error('failed to load turnstile')));
      }
      return;
    }
    const script = document.createElement('script');
    script.src = TURNSTILE_SRC;
    script.async = true;
    script.defer = true;
    script.addEventListener('load', () => { script.dataset.loaded = '1'; finish(); });
    script.addEventListener('error', () => reject(new Error('failed to load turnstile')));
    document.head.appendChild(script);
  });
  return scriptPromise;
}

// Render an explicit Turnstile widget into `container`.  Returns a handle with
// reset()/remove() and forwards token lifecycle to the callbacks.  Tokens are
// single-use and expire (~5 min), so onExpire clears the cached token and the
// form must wait for a fresh one (Turnstile auto-refreshes via callback).
export async function renderTurnstile(container, siteKey, { onToken, onExpire, onError } = {}) {
  const turnstile = await loadTurnstileScript();
  const widgetId = turnstile.render(container, {
    sitekey: siteKey,
    callback: (token) => { onToken?.(token); },
    'expired-callback': () => { onExpire?.(); },
    'error-callback': () => { onError?.(); },
    'refresh-expired': 'auto',
  });
  return {
    reset() { try { turnstile.reset(widgetId); } catch { /* widget already gone */ } },
    remove() { try { turnstile.remove(widgetId); } catch { /* widget already gone */ } },
  };
}
