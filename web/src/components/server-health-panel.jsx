import React from 'react';
import { Metric } from './common.jsx';
import { fmt, formatTime, userErrorLabel } from '../utils/display.js';
import { t } from '../i18n/index.js';

export function ServerHealthPanel({ snapshot, onRefresh }) {
  const db = snapshot?.database || {};
  const redis = snapshot?.redis || {};
  const app = snapshot?.app || {};
  const trading = snapshot?.trading || {};
  const schedule = snapshot?.schedule || {};
  const scheduler = snapshot?.scheduler || {};
  const runtime = snapshot?.runtime || {};
  const monitor = snapshot?.order_monitor || {};
  const failureReasons = runtime?.recent_failure_reasons || [];
  const scheduleLatency = schedule?.claimed_to_fired_latency || {};
  const remainingCurve = schedule?.remaining_capital_curve || [];
  const scheduleStatusText = Object.entries(schedule?.session_status_counts || {}).map(([key, value]) => `${key}:${value}`).join(' · ') || '--';
  const statusTone = snapshot?.status === 'ok' ? 'ok' : snapshot ? 'warning' : 'muted';
  return (
    <section className="panel server-health-panel">
      <div className="panel-title-row">
        <div>
          <span className="guide-step">{t('admin2.serverLabel')}</span>
          <h2>{t('admin2.healthSnapshot')}</h2>
        </div>
        <button className="ghost compact" type="button" onClick={onRefresh}>{t('admin2.refresh')}</button>
      </div>
      <div className="metrics admin-health-metrics">
        <Metric label={t('admin2.overallStatus')} value={snapshot?.status || '--'} sub={snapshot?.generated_at_et ? formatTime(snapshot.generated_at_et) : t('admin2.awaitingRefresh')} tone={statusTone} />
        <Metric label={t('admin2.database')} value={db.ok ? 'OK' : t('admin2.abnormal')} sub={`${db.backend || '--'} · ${db.latency_ms ?? '--'}ms`} tone={db.ok ? 'ok' : 'danger'} />
        <Metric label="Redis" value={redis.ok ? 'OK' : redis.enabled ? t('admin2.abnormal') : t('admin2.disabled')} sub={`${redis.enabled ? t('admin2.queueLockAvail') : t('admin2.localMode')} · ${redis.latency_ms ?? '--'}ms`} tone={redis.ok ? 'ok' : redis.enabled ? 'danger' : 'muted'} />
        <Metric label={t('admin2.processRole')} value={app.process_role || '--'} sub={`PID ${app.pid || '--'} · ${Math.round(Number(app.uptime_seconds || 0))}s`} />
        <Metric label={t('admin2.tradingInstances')} value={trading.active ?? '--'} sub={`${t('admin2.total')} ${trading.total ?? '--'} · ${t('admin2.needAttention')} ${trading.attention ?? '--'}`} tone={Number(trading.attention || 0) > 0 ? 'warning' : 'ok'} />
        <Metric label={t('admin2.queueBacklog')} value={runtime.scan_queue_backlog ?? '--'} sub={`${t('admin2.tradingPending')} ${runtime.trading_queued ?? '--'}`} tone={Number(runtime.scan_queue_backlog || 0) > 0 || Number(runtime.trading_queued || 0) > 0 ? 'warning' : 'ok'} />
        <Metric label={t('admin2.notificationQueue')} value={runtime.notification_queued ?? '--'} sub={`${t('admin2.failed')} ${runtime.notification_failed ?? '--'}`} tone={Number(runtime.notification_failed || 0) > 0 ? 'warning' : 'ok'} />
        <Metric label={t('admin2.monitorLag')} value={monitor.lag_seconds != null ? `${monitor.lag_seconds}s` : '--'} sub={monitor.last_run_at ? `${t('admin2.latest')} ${formatTime(monitor.last_run_at)}` : t('admin2.notRunYet')} tone={monitor.running ? 'warning' : 'ok'} />
        <Metric label={t('admin2.scheduler')} value={scheduler.started ? t('admin2.running') : t('admin2.notStarted')} sub={scheduler.last_tick_at_et ? formatTime(scheduler.last_tick_at_et) : '--'} tone={scheduler.last_error ? 'danger' : scheduler.started ? 'ok' : 'muted'} />
        <Metric label={t('admin2.sessionSlots')} value={scheduleStatusText} sub={`${t('admin2.replay')} ${schedule.retrying_slots ?? 0} · ${t('admin2.stuck')} ${schedule.stale_claimed_slots ?? 0}`} tone={Number(schedule.stale_claimed_slots || 0) > 0 ? 'warning' : 'ok'} />
        <Metric label="Claim→Fired" value={scheduleLatency.avg_seconds != null ? `${scheduleLatency.avg_seconds}s` : '--'} sub={`${t('admin2.sample')} ${scheduleLatency.sample_size ?? 0} · p95 ${scheduleLatency.p95_seconds ?? '--'}s`} tone={Number(scheduleLatency.p95_seconds || 0) > 120 ? 'warning' : 'ok'} />
      </div>
      <div className="runtime-health-grid">
        <div className="status-box muted-box">
          <h4>{t('admin2.recentFailures')}</h4>
          {failureReasons.length > 0 ? (
            <ul className="runtime-failure-list">
              {failureReasons.map((item) => (
                <li key={item.reason}>
                  <span>{userErrorLabel(item.reason)}</span>
                  <strong>{item.count}</strong>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted compact-note">{t('admin2.noFailureAgg')}</p>
          )}
        </div>
        <div className="status-box muted-box">
          <h4>{t('admin2.sessionCapitalCurve')}</h4>
          {remainingCurve.length > 0 ? (
            <ul className="runtime-failure-list">
              {remainingCurve.slice(0, 6).map((item) => (
                <li key={item.session_id}>
                  <span>{item.trade_date_et} · {item.profile_id} · {item.status}</span>
                  <strong>${fmt(item.remaining_capital)}</strong>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted compact-note">{t('admin2.noSessionSlots')}</p>
          )}
        </div>
      </div>
      {db.error && <p className="error compact-error">{userErrorLabel(db.error)}</p>}
      {scheduler.last_error && <p className="error compact-error">{userErrorLabel(scheduler.last_error)}</p>}
    </section>
  );
}
