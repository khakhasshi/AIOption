import React, { useEffect, useState } from 'react';
import { RotateCw } from 'lucide-react';

const t = (p) => window._t(p);

export function QuotaPage() {

  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const fetchUsage = async () => {
    setLoading(true);
    setError('');
    try {
      const res = await fetch('/api/auth/me/usage', { credentials: 'same-origin' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
    } catch (ex) {
      setError(ex.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchUsage(); }, []);

  return (
    <div>
      <div className="quota-page-head">
          <div>
            <h1>{t("quota.title")}</h1>
            <p>{window._t('quota.desc')}</p>
          </div>
          <button className="ghost compact" type="button" onClick={fetchUsage} title={t("quota.refresh")}>
            <RotateCw size={13} /> {window._t('quota.refresh')}
          </button>
        </div>

        {loading && <p className="muted">{window._t('quota.loading')}</p>}
        {error && <p className="muted">{window._t('quota.error')}：{error} <button className="ghost compact" type="button" onClick={fetchUsage}>{window._t('quota.retry')}</button></p>}

        {data?.resources?.length > 0 && (
          <div className="quota-usage-grid">
            {data.resources.map((r) => {
              const pct = r.limit > 0 ? Math.min(100, Math.round((r.usage / r.limit) * 100)) : 0;
              const tone = r.limit >= 0 && r.usage >= r.limit ? 'is-exhausted' : pct >= 80 ? 'is-near' : 'is-ok';
              return (
                <div key={r.key} className={`quota-usage-card ${tone}`}>
                  <span className="quota-usage-card-label">{r.label}</span>
                  <strong className="quota-usage-card-value">
                    {r.usage}
                    <small>/{r.limit >= 0 ? r.limit : window._t('quota.unlimited')}</small>
                  </strong>
                  {r.limit > 0 && (
                    <div className="quota-usage-card-bar">
                      <i style={{ width: `${pct}%` }} />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
    </div>
  );
}
