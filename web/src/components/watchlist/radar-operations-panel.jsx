import React from 'react';
import { Activity, PlayCircle, RefreshCw } from 'lucide-react';
import { t } from '../../i18n/index.js';
import { DisclosurePanel, Metric, SectionTitle } from '../common.jsx';
import { formatTime } from '../../utils/display.js';

function loopLabel(name) {
  return {
    scan_loop_scheduler: t('watchlist2.loop.scan_loop_scheduler'),
    trigger_monitor: 'Wait Trigger',
    opportunity_followup: t('watchlist2.loop.opportunity_followup'),
  }[name] || name;
}

function statusTone(value) {
  if (value === 'ok' || value === 'running') return 'ok';
  if (value === 'error') return 'danger';
  return 'muted';
}

function previewTitle(item) {
  return item?.name || item?.title || item?.symbol || item?.id || '--';
}

function previewMeta(item, type) {
  if (type === 'trigger') {
    const field = item.field ? ` · ${item.field}` : '';
    return `${item.symbol || '--'}${field} ${item.operator || ''} ${item.value ?? ''}`;
  }
  if (type === 'opportunity') {
    return `${item.symbol || '--'} · ${item.status || '--'} · ${item.strategy_structure || '--'}`;
  }
  return `${(item.symbols || []).slice(0, 4).join(', ') || '--'} · ${item.last_market_state || 'unknown'}`;
}

function PreviewList({ title, items = [], type }) {
  return (
    <div className="ops-preview">
      <strong>{title}</strong>
      {items.slice(0, 4).map((item) => (
        <div key={`${type}-${item.id}`} className="ops-preview-row">
          <span>{previewTitle(item)}</span>
          <small>{previewMeta(item, type)} · {t('watchlist2.next')} {formatTime(item.next_run_at || item.next_check_at)}</small>
        </div>
      ))}
      {!items.length && <small className="muted">{t('watchlist2.noPendingItems')}</small>}
    </div>
  );
}

export function RadarOperationsPanel({
  health = null,
  busy = false,
  onRefresh,
  onRunDueCycle,
}) {
  const counts = health?.radar?.counts || {};
  const loops = health?.scheduler?.loops || {};
  const schedulerOnline = Boolean(health?.scheduler?.online);
  const lastCycle = health?.last_manual_cycle;
  const nextScanAt = health?.radar?.next_scan_at || health?.radar?.next?.scan_loops?.[0]?.next_run_at;
  const nextTriggerAt = health?.radar?.next_trigger_check_at || health?.radar?.next?.triggers?.[0]?.next_check_at;
  return (
    <section className="panel radar-ops-panel">
      <div className="answer-head">
        <SectionTitle title={t('watchlist2.radarConsole')} />
        <div className="action-row">
          <button className="ghost compact" type="button" disabled={busy} onClick={onRefresh}>
            <RefreshCw size={14} /> {t('watchlist2.refresh')}
          </button>
          <button className="primary compact" type="button" disabled={busy} onClick={onRunDueCycle}>
            <PlayCircle size={14} /> {t('watchlist2.runDueCycle')}
          </button>
        </div>
      </div>
      <div className="metrics admin-health-metrics">
        <Metric label="Scheduler" value={schedulerOnline ? t('watchlist2.online') : t('watchlist2.notInProcess')} sub={`${health?.process?.role || 'unknown'} · ${health?.process?.worker_enabled ? 'worker enabled' : 'worker off'}`} tone={schedulerOnline ? 'ok' : 'warning'} />
        <Metric label="Due Trigger" value={counts.due_triggers ?? 0} sub={`${counts.triggers_enabled ?? 0} ${t('watchlist2.enabledCount')}`} tone={(counts.due_triggers || 0) ? 'warning' : 'muted'} />
        <Metric label="Due Loop" value={counts.due_scan_loops ?? 0} sub={`${counts.scan_loops_active ?? 0} ${t('watchlist2.activeCount')}`} tone={(counts.due_scan_loops || 0) ? 'warning' : 'muted'} />
        <Metric label={t('watchlist2.dueOpportunities')} value={counts.due_opportunities ?? 0} sub={`${counts.opportunities_followed ?? 0} ${t('watchlist2.trackingCount')}`} tone={(counts.due_opportunities || 0) ? 'warning' : 'muted'} />
        <Metric label={t('watchlist2.nextScan')} value={nextScanAt ? formatTime(nextScanAt) : '--'} sub={nextTriggerAt ? `Trigger ${formatTime(nextTriggerAt)}` : t('watchlist2.waitingSchedule')} tone={nextScanAt ? 'ok' : 'muted'} />
      </div>
      <DisclosurePanel
        title={t('watchlist2.runtimeDiagnostics')}
        summary={`${Object.keys(loops).length} loops · ${health?.radar?.last_failures?.length || 0} failures`}
        className="embedded-disclosure"
      >
        <div className="scheduler-loop-grid">
          {Object.entries(loops).map(([name, loop]) => (
            <article key={name} className={`scheduler-loop-card ${statusTone(loop.status)}`}>
              <strong><Activity size={14} /> {loopLabel(name)}</strong>
              <span>{loop.online ? 'online' : loop.status || 'idle'} · tick {loop.last_tick_at ? formatTime(loop.last_tick_at) : '--'}</span>
              <small>{t('watchlist2.success')} {loop.last_success_at ? formatTime(loop.last_success_at) : '--'}{loop.last_error ? ` · ${loop.last_error}` : ''}</small>
            </article>
          ))}
        </div>
        {lastCycle && (
          <div className="permission-note compact-note">
            <strong>{t('watchlist2.lastManualCycle')}</strong>
            <small>
              {t('watchlist2.scanned')} {lastCycle.scan_loops?.checked_count || 0} · Trigger {lastCycle.triggers?.checked_count || 0}
              · {t('watchlist2.opportunitiesShort')} {lastCycle.opportunities?.checked_count || 0} · {lastCycle.duration_ms}ms
            </small>
          </div>
        )}
        <div className="ops-preview-grid">
          <PreviewList title={t('watchlist2.nextLoopBatch')} items={health?.radar?.next?.scan_loops || []} type="scan-loop" />
          <PreviewList title={t('watchlist2.nextTriggerBatch')} items={health?.radar?.next?.triggers || []} type="trigger" />
          <PreviewList title={t('watchlist2.nextOpportunityBatch')} items={health?.radar?.next?.opportunities || []} type="opportunity" />
        </div>
        {Array.isArray(health?.radar?.last_failures) && health.radar.last_failures.length > 0 && (
          <div className="ops-preview">
            <strong>{t('watchlist2.recentFailures')}</strong>
            {health.radar.last_failures.slice(0, 4).map((item) => (
              <div key={`${item.source_type}-${item.id}`} className="ops-preview-row">
                <span>{item.source_type} · {item.status}</span>
                <small>{item.error || t('watchlist2.unknownError')} · {formatTime(item.created_at)}</small>
              </div>
            ))}
          </div>
        )}
      </DisclosurePanel>
    </section>
  );
}
