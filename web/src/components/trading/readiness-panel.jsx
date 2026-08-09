import React from 'react';
import { Metric, SectionTitle } from '../common.jsx';
import { formatTime, userErrorLabel } from '../../utils/display.js';
import { t } from '../../i18n/index.js';

export function ReadinessPanel({
  config,
  nextRunLabel,
  nextRunSub,
  readiness,
  readinessIssues,
  readinessState,
  readinessWarnings,
  refreshTradingReadiness,
}) {
  const brokerLabel = config.broker === 'alpaca' ? 'Alpaca' : (config.broker === 'usmart' ? 'uSMART' : 'Longbridge');
  return (
<div className="panel readiness-panel">
  <div className="answer-head">
    <SectionTitle title={t('trading2.readinessCheck')} />
    <button className="ghost" type="button" onClick={refreshTradingReadiness}>{t('trading2.refreshStatus')}</button>
  </div>
  <div className="readiness-grid">
    <Metric label={nextRunLabel} value={formatTime(readiness?.next_run_at_et)} sub={nextRunSub} />
    <Metric label={t('trading2.currentEtTime')} value={formatTime(readiness?.now_et)} sub="America/New_York" />
    <Metric label={t('trading2.brokerAccount')} value={readinessState?.account_name || '--'} sub={`${brokerLabel} · ${readinessState?.session?.token || '--'}`} tone={readinessState?.account_name ? 'ok' : 'danger'} />
    <Metric label={t('trading2.protectionData')} value="ThetaData" sub={t('trading2.protectionDataSub')} />
    <Metric label={t('trading2.lastCreateDate')} value={config.last_run_date_et || '--'} sub="ET date" />
  </div>
  {readinessState?.risk_breakers && (
    <div className="readiness-grid risk-breaker-grid">
      <Metric label={t('trading2.todayInstances')} value={readinessState.risk_breakers.today_run_count ?? 0} sub={`${t('trading2.limitLabel')} ${readinessState.risk_breakers.max_daily_runs ?? '--'}`} />
      <Metric label={t('trading2.consecutiveFailures')} value={readinessState.risk_breakers.consecutive_failures ?? 0} sub={`${t('trading2.limitLabel')} ${readinessState.risk_breakers.max_consecutive_failures ?? '--'}`} />
      <Metric label={t('trading2.unprotectedQty')} value={readinessState.risk_breakers.active_unprotected_quantity ?? 0} sub={`${t('trading2.limitLabel')} ${readinessState.risk_breakers.max_unprotected_quantity ?? '--'}`} />
      <Metric label={t('trading2.marketProtection')} value={config.risk_require_protection_for_market_order ? t('trading2.on') : t('trading2.off')} sub={t('trading2.riskBreakers')} />
    </div>
  )}
  {readiness?.schedule_preview?.enabled && (
    <div className="schedule-fire-preview">
      {(readiness.schedule_preview.slots || []).map((slot) => (
        <span key={slot.slot_id}>
          {slot.label || slot.slot_id} · {slot.time_et} · {(slot.strategy_modes || []).join('/')}
        </span>
      ))}
    </div>
  )}
  {readinessIssues.length > 0 && (
    <div className="status-box danger-box">
      <strong>{t('trading2.blockingIssues')}</strong>
      <ul>{readinessIssues.map((item) => <li key={item}>{userErrorLabel(item)}</li>)}</ul>
    </div>
  )}
  {readinessWarnings.length > 0 && (
    <div className="status-box warning-box">
      <strong>{t('trading2.warnings')}</strong>
      <ul>{readinessWarnings.map((item) => <li key={item}>{userErrorLabel(item)}</li>)}</ul>
    </div>
  )}
  {!readinessIssues.length && !readinessWarnings.length && (
    <p className="muted">{t('trading2.readinessAllPass')}</p>
  )}
</div>
  );
}
