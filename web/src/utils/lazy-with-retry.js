import { lazy } from 'react';

// After a deployment, Vite emits new content-hashed chunk filenames and removes
// the old ones. A browser tab still running the previous index.html references
// the old chunk names, so lazy-loading a not-yet-visited route requests a file
// that returns 404 ("Failed to fetch dynamically imported module"). This helper
// makes route loading resilient:
//   1. Retry the dynamic import a couple of times (handles transient network
//      blips / flaky mobile connections).
//   2. If it still fails, force a single hard reload so the browser fetches the
//      fresh index.html + current chunk hashes. A sessionStorage guard prevents
//      an infinite reload loop when the failure is genuine (e.g. offline).

const RELOAD_GUARD_KEY = 'ai_option_chunk_reload_at';
const RELOAD_COOLDOWN_MS = 15000;

function isChunkLoadError(error) {
  const message = String(error?.message || error || '');
  return (
    /Failed to fetch dynamically imported module/i.test(message) ||
    /error loading dynamically imported module/i.test(message) ||
    /Importing a module script failed/i.test(message) ||
    /dynamically imported module/i.test(message) ||
    error?.name === 'ChunkLoadError'
  );
}

function reloadOnce() {
  let last = 0;
  try {
    last = Number(window.sessionStorage.getItem(RELOAD_GUARD_KEY) || '0');
  } catch {
    last = 0;
  }
  const now = Date.now();
  if (now - last < RELOAD_COOLDOWN_MS) return false;
  try {
    window.sessionStorage.setItem(RELOAD_GUARD_KEY, String(now));
  } catch {
    /* ignore storage failures */
  }
  // Drop any service-worker-cached stale shell so the reload pulls fresh assets.
  try {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.getRegistrations?.().then((regs) => {
        regs.forEach((reg) => reg.update?.());
      });
    }
  } catch {
    /* best effort */
  }
  window.location.reload();
  return true;
}

async function importWithRetry(factory, retries = 2, delayMs = 400) {
  let lastError;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      return await factory();
    } catch (error) {
      lastError = error;
      if (!isChunkLoadError(error) || attempt === retries) break;
      await new Promise((resolve) => setTimeout(resolve, delayMs * (attempt + 1)));
    }
  }
  if (isChunkLoadError(lastError) && reloadOnce()) {
    // Reload is in flight; return a never-resolving promise so React keeps the
    // Suspense fallback up instead of surfacing the error before navigation.
    return new Promise(() => {});
  }
  throw lastError;
}

export function lazyWithRetry(factory) {
  return lazy(() => importWithRetry(factory));
}

// Install global guards so a stale-chunk failure that escapes React Suspense
// (e.g. Vite's preload error event, or an unhandled dynamic-import rejection)
// still triggers a single recovery reload instead of a dead page.
export function installChunkErrorRecovery() {
  if (typeof window === 'undefined' || window.__aiOptionChunkGuard) return;
  window.__aiOptionChunkGuard = true;
  window.addEventListener('vite:preloadError', (event) => {
    event.preventDefault?.();
    reloadOnce();
  });
  window.addEventListener('unhandledrejection', (event) => {
    if (isChunkLoadError(event?.reason)) reloadOnce();
  });
  window.addEventListener('error', (event) => {
    if (isChunkLoadError(event?.error || event?.message)) reloadOnce();
  });
}

export { isChunkLoadError, reloadOnce };
