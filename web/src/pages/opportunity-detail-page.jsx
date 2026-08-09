import React, { useEffect, useMemo, useState } from 'react';
import { BellPlus, Radar } from 'lucide-react';
import { t } from '../i18n/index.js';
import { MarketClock } from '../components/market-clock.jsx';
import { SessionAndRouteBar } from '../components/session-route-bar.jsx';
import { OpportunityDetailWorkspace } from '../components/watchlist/opportunity-detail-modal.jsx';

export function OpportunityDetailPage({
  api,
  opportunityId,
  session,
  routeMode,
  onRouteModeChange,
  onLogout,
  onBack,
  marketClock,
  clockTick,
}) {
  const [detail, setDetail] = useState(null);
  const [notificationChannels, setNotificationChannels] = useState([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [createdTrigger, setCreatedTrigger] = useState(null);
  const decodedId = useMemo(() => decodeURIComponent(opportunityId || ''), [opportunityId]);

  useEffect(() => {
    refreshDetail();
  }, [decodedId]);

  async function refreshDetail() {
    if (!decodedId) return;
    setError('');
    try {
      const [rowResult, channelsResult] = await Promise.allSettled([
        api(`/api/opportunities/${encodeURIComponent(decodedId)}`),
        api('/api/notification-channels'),
      ]);
      if (rowResult.status !== 'fulfilled') {
        throw rowResult.reason;
      }
      const row = rowResult.value;
      const channels = channelsResult.status === 'fulfilled' ? channelsResult.value : [];
      setDetail(row);
      setNotificationChannels(Array.isArray(channels) ? channels : []);
    } catch (loadError) {
      setError(loadError.message);
    }
  }

  async function patchOpportunity(opportunity, body) {
    if (!opportunity?.id) return;
    setBusy(true);
    setError('');
    try {
      await api(`/api/opportunities/${encodeURIComponent(opportunity.id)}`, {
        method: 'PATCH',
        body: JSON.stringify(body),
      });
      await refreshDetail();
    } catch (actionError) {
      setError(actionError.message);
    } finally {
      setBusy(false);
    }
  }

  async function postOpportunityAction(opportunityIdValue, path) {
    setBusy(true);
    setError('');
    try {
      await api(`/api/opportunities/${encodeURIComponent(opportunityIdValue)}${path}`, { method: 'POST' });
      await refreshDetail();
    } catch (actionError) {
      setError(actionError.message);
    } finally {
      setBusy(false);
    }
  }

  async function createTrigger(payload) {
    if (!payload?.symbol) {
      setError(t('watchlist2.missingInfoForTrigger'));
      return;
    }
    setBusy(true);
    setError('');
    setCreatedTrigger(null);
    try {
      const trigger = await api('/api/scan-triggers', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      setCreatedTrigger(trigger);
      await refreshDetail();
    } catch (triggerError) {
      setError(triggerError.message);
    } finally {
      setBusy(false);
    }
  }

  async function reviewOpportunity(opportunity) {
    const fallback = opportunity?.entry_reference?.underlying_reference ?? opportunity?.trigger_snapshot?.last ?? '';
    const value = window.prompt(t('watchlist2.promptCurrentRef'), fallback === '' ? '' : String(fallback));
    if (value === null) return;
    const last = Number(value);
    if (!Number.isFinite(last) || last <= 0) {
      setError(t('watchlist2.invalidCurrentRef'));
      return;
    }
    setBusy(true);
    setError('');
    try {
      await api(`/api/opportunities/${encodeURIComponent(opportunity.id)}/check`, {
        method: 'POST',
        body: JSON.stringify({
          quote_snapshot: {
            symbol: opportunity.symbol,
            last,
            market_state: 'regular_open',
            data_timestamp: new Date().toISOString(),
            source: 'manual_review',
          },
        }),
      });
      await refreshDetail();
    } catch (reviewError) {
      setError(reviewError.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="shell">
      <section className="hero app-hero compact-hero">
        <div className="hero-brand">
          <div className="radar-logo"><Radar size={26} /></div>
          <div>
            <div className="eyebrow">Opportunity Research Workspace</div>
            <h1>{t('watchlist2.detailTitle')}</h1>
            <p>{t('watchlist2.detailSubtitle')}</p>
          </div>
        </div>
        <div className="hero-controls">
          <MarketClock clock={marketClock} tick={clockTick} />
          <SessionAndRouteBar session={session} routeMode={routeMode} onRouteModeChange={onRouteModeChange} onLogout={onLogout} />
          <div className="hero-actions">
            <button className="ghost nav-action" type="button" onClick={onBack}>{t('watchlist2.backToRadar')}</button>
            <button className="ghost nav-action" type="button" onClick={refreshDetail}>{t('watchlist2.refreshDetail')}</button>
          </div>
        </div>
      </section>

      {error && <div className="error-banner">{error}</div>}
      {createdTrigger && (
        <div className="notification-result-line">
          <BellPlus size={14} /> {t('watchlist2.triggerCreated')}{createdTrigger.name} · {t('watchlist2.nextCheck')} {createdTrigger.next_check_at || '--'}
        </div>
      )}
      {!detail && !error && <section className="panel"><p className="muted">{t('watchlist2.loadingDetail')}</p></section>}
      {detail && (
        <OpportunityDetailWorkspace
          opportunity={detail}
          detail={detail}
          busy={busy}
          standalone
          onBack={onBack}
          onMarkWatching={(id) => patchOpportunity({ id }, { status: 'watching_entry' })}
          onMarkActive={(id) => patchOpportunity({ id }, { status: 'active_reference' })}
          onSaveRiskPlan={(opportunity, riskPlan) => patchOpportunity(opportunity, { risk_plan: riskPlan })}
          onPause={(id) => postOpportunityAction(id, '/pause')}
          onResume={(id) => postOpportunityAction(id, '/resume')}
          onReview={reviewOpportunity}
          onArchive={(id) => postOpportunityAction(id, '/archive')}
          onCreateTrigger={createTrigger}
          notificationChannels={notificationChannels}
        />
      )}
    </main>
  );
}
