import React, { useEffect, useMemo, useState } from 'react';
import { SessionAndRouteBar } from '../components/session-route-bar.jsx';
import { PageHeader } from '../components/app-shell/page-header.jsx';
import { useAppShell } from '../components/app-shell/app-shell-context.js';
import { t } from '../i18n/index.js';

function money(value, currency = 'cny') {
  const numberValue = Number(value || 0);
  if (currency === 'usd') return `$${numberValue.toFixed(4)}`;
  return `¥${numberValue.toFixed(4)}`;
}

function tokens(value) {
  const numberValue = Number(value || 0);
  return numberValue.toLocaleString();
}

function CostMetric({ label, value, sub }) {
  return (
    <div className="metric ai-cost-metric">
      <span>{label}</span>
      <strong>{value}</strong>
      {sub && <small>{sub}</small>}
    </div>
  );
}

export function AdminAiCostPage({ api, session, routeMode, onRouteModeChange, onLogout, onBack }) {
  const [days, setDays] = useState(30);
  const [ownerFilter, setOwnerFilter] = useState('');
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  async function refresh() {
    setLoading(true);
    setError('');
    try {
      const query = new URLSearchParams({ days: String(days), limit: '120' });
      if (ownerFilter.trim()) query.set('owner_id', ownerFilter.trim());
      const next = await api(`/api/admin/ai-usage?${query.toString()}`);
      setSummary(next);
    } catch (costError) {
      setError(costError.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
  }, [days]);

  const totals = summary?.totals || {};
  const today = totals.today || {};
  const week = totals.week || {};
  const month = totals.month || {};
  const rangeKey = `${summary?.days || days}d`;
  const range = totals[rangeKey] || {};
  const balance = summary?.balance || {};
  const sourceMix = useMemo(() => {
    const rows = summary?.recent || [];
    return rows.reduce((acc, row) => {
      const key = row.radar_scan ? t('admin2.radarScan') : row.council_mode ? t('admin2.council') : row.source_type || t('admin2.normalAi');
      acc[key] = (acc[key] || 0) + 1;
      return acc;
    }, {});
  }, [summary]);

  return (
    <main className="shell guide-shell">
      <AiCostPageHeader loading={loading} onRefresh={refresh} />
      <section className="hero app-hero">
        <div className="hero-brand">
          <img className="site-logo" src="/logo.png" alt="AI Option Scanner" />
          <div>
            <div className="eyebrow">Admin · AI Cost Center</div>
            <h1>{t('admin2.aiCostCenter')}</h1>
            <p>{t('admin2.aiCostCenterDesc')}</p>
          </div>
        </div>
        <div className="hero-controls">
          <SessionAndRouteBar session={session} routeMode={routeMode} onRouteModeChange={onRouteModeChange} onLogout={onLogout} />
          <div className="hero-actions">
            <button className="ghost nav-action" type="button" onClick={onBack}>{t('admin2.backToAdmin')}</button>
            <button className="ghost nav-action" type="button" onClick={refresh} disabled={loading}>{loading ? t('admin2.refreshing') : t('admin2.refresh')}</button>
          </div>
        </div>
      </section>
      {error && <div className="error">{error}</div>}
      <section className="panel ai-cost-toolbar">
        <label>
          {t('admin2.dateRange')}
          <select value={days} onChange={(event) => setDays(Number(event.target.value))}>
            <option value={1}>{t('admin2.range1d')}</option>
            <option value={7}>{t('admin2.range7d')}</option>
            <option value={30}>{t('admin2.range30d')}</option>
            <option value={90}>{t('admin2.range90d')}</option>
          </select>
        </label>
        <label>
          {t('admin2.userFilter')}
          <input value={ownerFilter} onChange={(event) => setOwnerFilter(event.target.value)} placeholder={t('admin2.userFilterPlaceholder')} />
        </label>
        <button className="primary" type="button" onClick={refresh} disabled={loading}>{t('admin2.applyFilter')}</button>
      </section>
      <section className="admin-summary-grid">
        <CostMetric label={t('admin2.todayCost')} value={money(today.estimated_cost_cny)} sub={`${tokens(today.total_tokens)} tokens · ${today.calls || 0} calls`} />
        <CostMetric label={t('admin2.cost7d')} value={money(week.estimated_cost_cny)} sub={`${money(week.estimated_cost_usd, 'usd')} · ${tokens(week.total_tokens)} tokens`} />
        <CostMetric label={t('admin2.cost30d')} value={money(month.estimated_cost_cny)} sub={`${money(month.estimated_cost_usd, 'usd')} · ${month.calls || 0} calls`} />
        <CostMetric label={`${summary?.days || days}${t('admin2.costNdSuffix')}`} value={money(range.estimated_cost_cny)} sub={`${tokens(range.total_tokens)} tokens`} />
        <CostMetric label={t('admin2.deepseekBalance')} value={balance.available ? t('admin2.balanceAvailable') : t('admin2.notConnected')} sub={balance.error || (balance.balance_infos?.[0]?.total_balance ? `${t('admin2.balance')} ${balance.balance_infos[0].total_balance}` : t('admin2.balanceApiReady'))} />
      </section>
      <section className="ai-cost-layout">
        <div className="panel ai-cost-panel">
          <div className="admin-panel-head">
            <div>
              <h2>{t('admin2.modelCost')}</h2>
              <p>{t('admin2.modelCostDesc')}</p>
            </div>
          </div>
          <div className="ai-cost-table">
            <div className="ai-cost-table-head"><span>{t('admin2.model')}</span><span>{t('admin2.calls')}</span><span>Tokens</span><span>{t('admin2.cost')}</span></div>
            {(summary?.by_model || []).map((row) => (
              <div className="ai-cost-row" key={`${row.provider}:${row.model}`}>
                <span><strong>{row.provider}</strong><small>{row.model}</small></span>
                <span>{row.calls}</span>
                <span>{tokens(row.total_tokens)}</span>
                <span>{money(row.estimated_cost_cny)}<small>{money(row.estimated_cost_usd, 'usd')}</small></span>
              </div>
            ))}
            {!summary?.by_model?.length && <p className="muted">{t('admin2.noAiCalls')}</p>}
          </div>
        </div>
        <div className="panel ai-cost-panel">
          <div className="admin-panel-head">
            <div>
              <h2>{t('admin2.sourceBreakdown')}</h2>
              <p>{t('admin2.sourceBreakdownDesc')}</p>
            </div>
          </div>
          <div className="ai-source-list">
            {Object.entries(sourceMix).map(([label, count]) => (
              <div className="ai-source-row" key={label}>
                <span>{label}</span>
                <strong>{count}</strong>
              </div>
            ))}
            {!Object.keys(sourceMix).length && <p className="muted">{t('admin2.noSourceStats')}</p>}
          </div>
        </div>
      </section>
      <section className="panel ai-cost-panel">
        <div className="admin-panel-head">
          <div>
            <h2>{t('admin2.recentAiCalls')}</h2>
            <p>{t('admin2.recentAiCallsDesc')}</p>
          </div>
        </div>
        <div className="ai-cost-event-list">
          {(summary?.recent || []).map((event) => (
            <div className="ai-cost-event" key={event.id}>
              <div>
                <strong>{event.owner_id || 'local'} · {event.request_role || event.source_type || 'ai'}</strong>
                <span>{event.provider}/{event.model} · {event.symbol || '--'} · {event.created_at}</span>
              </div>
              <div className="ai-cost-event-tags">
                <span>{event.radar_scan ? t('admin2.radarScan') : event.source_type || t('admin2.normalScan')}</span>
                <span>{event.council_mode ? t('admin2.council') : t('admin2.single')}</span>
                <span>{tokens(event.total_tokens)} tokens</span>
                <span>{money(event.estimated_cost_cny)}</span>
              </div>
            </div>
          ))}
          {!summary?.recent?.length && <p className="muted">{t('admin2.noCallDetails')}</p>}
        </div>
      </section>
    </main>
  );
}

function AiCostPageHeader({ loading, onRefresh }) {
  const { embedded } = useAppShell();
  if (!embedded) return null;
  return (
    <PageHeader
      eyebrow="Admin · AI Cost Center"
      title={t('admin2.aiCostCenter')}
      subtitle={t('admin2.aiCostCenterSubtitle')}
      actions={
        <button className="ghost" type="button" onClick={onRefresh} disabled={loading}>
          {loading ? t('admin2.refreshingEllipsis') : t('admin2.refresh')}
        </button>
      }
    />
  );
}
