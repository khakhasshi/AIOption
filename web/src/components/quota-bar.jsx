import React, { useEffect, useState } from 'react';
import { t } from '../i18n/index.js';

/** Localized label for a quota resource: prefer the dictionary keyed by the
    backend's stable `key`, fall back to the server-provided label. */
function quotaLabel(r) {
  const path = `quota.byKey.${r.key}`;
  const translated = t(path);
  return translated === path ? (r.label || r.key) : translated;
}

/** Compact quota-indicator bar for scanner / chat pages.
    Fetches daily counters and shows a pill per resource that matters. */
export function QuotaBar() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch('/api/auth/me/usage', { credentials: 'same-origin' });
        if (!res.ok) return;
        const data = await res.json();
        if (cancelled) return;
        setItems((data.resources || []).filter((r) => r.limit > 0));
      } catch {
        /* silent — quota bar is optional */
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  if (loading || !items.length) return null;

  return (
    <div className="quotabar">
      {items.map((r) => {
        const pct = Math.min(100, Math.round((r.usage / r.limit) * 100));
        const tone = r.usage >= r.limit ? 'quotabar-exhausted' : pct >= 80 ? 'quotabar-near' : '';
        const label = quotaLabel(r);
        return (
          <span key={r.key} className={`quotabar-item ${tone}`} title={`${label}: ${r.usage}/${r.limit}`}>
            <span className="quotabar-label">{label}</span>
            <span className="quotabar-num">{r.usage}/{r.limit}</span>
          </span>
        );
      })}
    </div>
  );
}
