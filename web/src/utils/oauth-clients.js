// Browser-side helpers to obtain a provider id_token (the "credential") that the
// backend verifies at /api/auth/oauth/login or /api/auth/oauth/links.
//
// Both providers' JS SDKs are loaded lazily on first use so the login page does
// not pay for them when OAuth is unconfigured.  Each helper resolves with
// { credential, nonce }: the raw id_token plus the nonce we asked the provider
// to embed, which the backend re-checks to block token replay.

const SCRIPT_CACHE = new Map();

function loadScript(src) {
  if (SCRIPT_CACHE.has(src)) return SCRIPT_CACHE.get(src);
  const promise = new Promise((resolve, reject) => {
    const existing = document.querySelector(`script[src="${src}"]`);
    if (existing) {
      if (existing.dataset.loaded === '1') resolve();
      else {
        existing.addEventListener('load', () => resolve());
        existing.addEventListener('error', () => reject(new Error(`failed to load ${src}`)));
      }
      return;
    }
    const script = document.createElement('script');
    script.src = src;
    script.async = true;
    script.defer = true;
    script.addEventListener('load', () => { script.dataset.loaded = '1'; resolve(); });
    script.addEventListener('error', () => reject(new Error(`failed to load ${src}`)));
    document.head.appendChild(script);
  });
  SCRIPT_CACHE.set(src, promise);
  return promise;
}

function randomNonce() {
  const bytes = new Uint8Array(16);
  (globalThis.crypto || window.crypto).getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

// --- Google Identity Services -------------------------------------------------
// We use the GIS "prompt" flow: initialize with our client_id + nonce, register
// a one-shot callback, then call prompt().  The callback receives the credential
// (a signed id_token) which we hand to the backend.
export async function requestGoogleCredential(clientId) {
  if (!clientId) throw new Error('Google login is not configured');
  await loadScript('https://accounts.google.com/gsi/client');
  const google = window.google;
  if (!google?.accounts?.id) throw new Error('Google Identity Services unavailable');
  const nonce = randomNonce();
  return new Promise((resolve, reject) => {
    let settled = false;
    google.accounts.id.initialize({
      client_id: clientId,
      nonce,
      callback: (response) => {
        settled = true;
        if (response?.credential) resolve({ credential: response.credential, nonce });
        else reject(new Error('Google did not return a credential'));
      },
    });
    google.accounts.id.prompt((notification) => {
      // If the One Tap prompt cannot be shown (dismissed, opted out, blocked),
      // surface a clear error instead of hanging forever.
      if (settled) return;
      if (notification?.isNotDisplayed?.() || notification?.isSkippedMoment?.() || notification?.isDismissedMoment?.()) {
        reject(new Error('Google sign-in was dismissed'));
      }
    });
  });
}

// --- Sign in with Apple JS ----------------------------------------------------
// AppleID.auth.signIn() with usePopup returns authorization.id_token, which is
// the equivalent credential.  Apple only returns the user's email on the first
// authorization, but the backend keys on the stable `sub`, so repeat logins work.
export async function requestAppleCredential(clientId) {
  if (!clientId) throw new Error('Apple login is not configured');
  await loadScript('https://appleid.cdn-apple.com/appleauth/static/jsapi/appleid/1/en_US/appleid.auth.js');
  const AppleID = window.AppleID;
  if (!AppleID?.auth) throw new Error('Sign in with Apple unavailable');
  const nonce = randomNonce();
  AppleID.auth.init({
    clientId,
    scope: 'name email',
    redirectURI: `${window.location.origin}`,
    usePopup: true,
    nonce,
  });
  const data = await AppleID.auth.signIn();
  const credential = data?.authorization?.id_token;
  if (!credential) throw new Error('Apple did not return a credential');
  return { credential, nonce };
}

export async function requestOAuthCredential(provider, clientId) {
  if (provider === 'google') return requestGoogleCredential(clientId);
  if (provider === 'apple') return requestAppleCredential(clientId);
  throw new Error(`unsupported provider: ${provider}`);
}
