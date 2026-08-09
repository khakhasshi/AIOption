import { ONBOARDING_GUIDE_VERSION } from './config.js';

export function normalizeRouteMode(value) {
  const cleaned = String(value || 'auto').trim().toLowerCase();
  return ['auto', 'primary', 'secondary'].includes(cleaned) ? cleaned : 'auto';
}

export function setRouteCookie(routeMode) {
  const normalized = normalizeRouteMode(routeMode);
  document.cookie = `ai_option_route=${encodeURIComponent(normalized)}; path=/; max-age=31536000; samesite=lax`;
}

export function clearRouteCookie() {
  document.cookie = 'ai_option_route=; path=/; max-age=0; samesite=lax';
}

export function getRouteMode() {
  const cookie = document.cookie.split('; ').find((item) => item.startsWith('ai_option_route='));
  if (!cookie) return 'auto';
  try {
    return normalizeRouteMode(decodeURIComponent(cookie.split('=')[1] || 'auto'));
  } catch {
    return 'auto';
  }
}

export function onboardingGuideKey(username) {
  return `ai_option_onboarding_guide_seen:${ONBOARDING_GUIDE_VERSION}:${String(username || 'anonymous').trim().toLowerCase()}`;
}

export function hasSeenOnboardingGuide(username) {
  try {
    return window.localStorage.getItem(onboardingGuideKey(username)) === '1';
  } catch {
    return true;
  }
}

export function markOnboardingGuideSeen(username) {
  try {
    window.localStorage.setItem(onboardingGuideKey(username), '1');
  } catch {
    // Ignore private-mode storage failures; the guide remains accessible from the header.
  }
}

export function getClientUserId() {
  const key = 'ai_option_scanner_user_id';
  let value = window.localStorage.getItem(key);
  if (!value) {
    value = `browser-${createBrowserId()}`;
    window.localStorage.setItem(key, value);
  }
  return value;
}

function createBrowserId() {
  const browserCrypto = globalThis.crypto;
  if (typeof browserCrypto?.randomUUID === 'function') {
    return browserCrypto.randomUUID();
  }
  if (typeof browserCrypto?.getRandomValues === 'function') {
    try {
      const bytes = new Uint8Array(16);
      browserCrypto.getRandomValues(bytes);
      bytes[6] = (bytes[6] & 0x0f) | 0x40;
      bytes[8] = (bytes[8] & 0x3f) | 0x80;
      const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0'));
      return `${hex.slice(0, 4).join('')}-${hex.slice(4, 6).join('')}-${hex.slice(6, 8).join('')}-${hex.slice(8, 10).join('')}-${hex.slice(10).join('')}`;
    } catch {
      // Fall through to the non-crypto fallback below.
    }
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

// --- Session cache (stale-while-revalidate for auth) ---
const SESSION_CACHE_KEY = 'ai_option_session_v1';
const SESSION_CACHE_TTL_MS = 24 * 60 * 60 * 1000; // 24h

export function saveSessionCache(session) {
  try {
    const { loading: _loading, ...rest } = session;
    window.localStorage.setItem(SESSION_CACHE_KEY, JSON.stringify({ ...rest, _ts: Date.now() }));
  } catch {
    // ignore private-mode failures
  }
}

export function loadSessionCache() {
  try {
    const raw = window.localStorage.getItem(SESSION_CACHE_KEY);
    if (!raw) return null;
    const cached = JSON.parse(raw);
    if (!cached?.authenticated) return null;
    if (Date.now() - (cached._ts || 0) > SESSION_CACHE_TTL_MS) return null;
    const { _ts: _ignored, ...session } = cached;
    return { ...session, loading: false };
  } catch {
    return null;
  }
}

export function clearSessionCache() {
  try {
    window.localStorage.removeItem(SESSION_CACHE_KEY);
  } catch {
    // ignore
  }
}

// --- Generic page-scoped stale-while-revalidate cache ---
export function savePageCache(key, value) {
  try {
    window.localStorage.setItem(key, JSON.stringify({ value, _ts: Date.now() }));
  } catch {
    // ignore
  }
}

export function loadPageCache(key, ttlMs = 5 * 60 * 1000) {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    const cached = JSON.parse(raw);
    if (Date.now() - (cached._ts || 0) > ttlMs) return null;
    return cached.value;
  } catch {
    return null;
  }
}
