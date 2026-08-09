import { useEffect, useRef } from 'react';

// Drop-in replacement for setInterval that:
//   1) Skips ticks while the tab is hidden (saves battery and backend load).
//   2) Fires immediately on tab refocus if at least one interval was missed.
//   3) Always reads the latest callback (no stale closures across re-renders).
//
// Pass `enabled = false` to fully disable the timer.
export function useVisibilityInterval(callback, delayMs, { enabled = true, fireOnFocus = true } = {}) {
  const cbRef = useRef(callback);
  useEffect(() => {
    cbRef.current = callback;
  }, [callback]);

  useEffect(() => {
    if (!enabled || !delayMs || delayMs <= 0) return undefined;
    let lastFire = Date.now();
    let timer = null;
    const tick = () => {
      if (typeof document !== 'undefined' && document.hidden) return;
      lastFire = Date.now();
      try {
        cbRef.current?.();
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error('[useVisibilityInterval] callback error:', err);
      }
    };
    timer = window.setInterval(tick, delayMs);
    const onVisibility = () => {
      if (!fireOnFocus) return;
      if (document.hidden) return;
      const elapsed = Date.now() - lastFire;
      if (elapsed >= delayMs) tick();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [delayMs, enabled, fireOnFocus]);
}
