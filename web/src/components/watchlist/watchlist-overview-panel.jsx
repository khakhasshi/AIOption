import React from 'react';
import { CopyableId } from '../copyable-id.jsx';
import { Copy, Edit3, PauseCircle, PlayCircle, Trash2 } from 'lucide-react';
import { t } from '../../i18n/index.js';
import { SectionTitle } from '../common.jsx';
import { formatTime, marketDataSourceLabel } from '../../utils/display.js';

export function WatchlistOverviewPanel({
  busy = false,
  watchlists = [],
  instances = [],
  allInstances = [],
  channels = [],
  selectedWatchlistId = '',
  selectedInstanceId = '',
  onRefresh,
  onSelectWatchlist,
  onEditWatchlist,
  onDuplicateWatchlist,
  onDeleteWatchlist,
  onSelectInstance,
  onEditInstance,
  onDuplicateInstance,
  onDeleteInstance,
  onToggleInstanceStatus,
}) {
  const channelById = new Map(channels.map((channel) => [channel.id, channel]));

  return (
    <section className="panel">
      <div className="answer-head">
        <SectionTitle title={t('watchlist2.poolsAndInstances')} />
        <button className="ghost compact" type="button" onClick={onRefresh} disabled={busy}>{t('watchlist2.refresh')}</button>
      </div>
      <div className="watchlist-stack">
        <article className={`watchlist-card ${selectedWatchlistId === '__all__' ? 'active' : ''}`}>
          <button className="watchlist-card-main" type="button" onClick={() => onSelectWatchlist('__all__')}>
            <strong>{t('watchlist2.allPools')}</strong>
            <p>{t('watchlist2.allPoolsDesc')}</p>
            <small>{watchlists.length} {t('watchlist2.poolsCount')} · {allInstances.length} {t('watchlist2.instancesCount')}</small>
          </button>
        </article>
        {watchlists.map((watchlist) => {
          const instanceCount = allInstances.filter((item) => item.watchlist_id === watchlist.id).length;
          const active = selectedWatchlistId === watchlist.id;
          return (
            <article key={watchlist.id} className={`watchlist-card ${active ? 'active' : ''}`}>
              <button className="watchlist-card-main" type="button" onClick={() => onSelectWatchlist(watchlist.id)}>
                <strong>{watchlist.name}</strong>
                <p>{watchlist.symbols.join(', ')}</p>
                <small>{watchlist.symbols.length} {t('watchlist2.tickers')} · {instanceCount} {t('watchlist2.instancesCount')}</small>
              </button>
              <div className="watchlist-actions">
                <button className="ghost icon" type="button" title={t('watchlist2.editPool')} disabled={busy} onClick={() => onEditWatchlist(watchlist)}><Edit3 size={14} /></button>
                <button className="ghost icon" type="button" title={t('watchlist2.duplicatePool')} disabled={busy} onClick={() => onDuplicateWatchlist(watchlist)}><Copy size={14} /></button>
                <button className="ghost icon" type="button" title={t('watchlist2.deletePool')} disabled={busy} onClick={() => onDeleteWatchlist(watchlist)}><Trash2 size={14} /></button>
                <CopyableId value={watchlist.id} label={t('watchlist2.pool')} compact />
              </div>
            </article>
          );
        })}
        {!watchlists.length && <p className="muted">{t('watchlist2.noPoolsYet')}</p>}
      </div>

      <div className="instance-list">
        {instances.map((instance) => (
          <article key={instance.id} className={`instance-row ${selectedInstanceId === instance.id ? 'active' : ''}`}>
            <button type="button" className="instance-row-main-action" onClick={() => onSelectInstance(instance.id)}>
              <span>{instance.status} · {marketDataSourceLabel(instance.market_data_source)}</span>
              <strong>{instance.name}</strong>
              <small>
                {instance.symbols.join(', ')} · {instance.alert_mode} · {t('watchlist2.alertsLabel')} {instance.max_alerts_per_day}/{t('watchlist2.perDay')} · AI {instance.max_ai_scans_per_day}/{t('watchlist2.perDay')}
                {instance.ai_scan_policy ? ` · ${t('watchlist2.aiPolicyAbbr')} ${instance.ai_scan_policy}${instance.ai_scan_top_n ? ` / Top${instance.ai_scan_top_n}` : ''}` : ''}
                {instance.eod_review_enabled ? ` · EOD ${instance.eod_run_time_et || '16:20'}` : ''}
                {instance.weekend_review_enabled ? ` · ${t('watchlist2.weekendPlanShort')}` : ''}
                {' · '}{t('watchlist2.last')} {instance.last_run_at ? formatTime(instance.last_run_at) : t('watchlist2.notRun')}
              </small>
              <small>
                {t('watchlist2.next')} {instance.next_run_at ? formatTime(instance.next_run_at) : t('watchlist2.pendingCalc')}
                {instance.last_market_state ? ` · ${instance.last_market_state}` : ''}
              </small>
              <small>
                {t('watchlist2.notify')}{(instance.notification_channel_ids || []).length
                  ? (instance.notification_channel_ids || []).map((id) => channelById.get(id)?.label || id.slice(0, 8)).join(' / ')
                  : t('watchlist2.notBound')}
              </small>
            </button>
            <div className="instance-actions">
              <button className="ghost compact" type="button" disabled={busy} onClick={() => onToggleInstanceStatus(instance)}>
                {instance.status === 'active' ? <PauseCircle size={14} /> : <PlayCircle size={14} />}
                {instance.status === 'active' ? t('watchlist2.pause') : t('watchlist2.resume')}
              </button>
              <button className="ghost icon" type="button" title={t('watchlist2.editInstance')} disabled={busy} onClick={() => onEditInstance(instance)}><Edit3 size={14} /></button>
              <button className="ghost icon" type="button" title={t('watchlist2.duplicateInstance')} disabled={busy} onClick={() => onDuplicateInstance(instance)}><Copy size={14} /></button>
              <button className="ghost icon" type="button" title={t('watchlist2.deleteInstance')} disabled={busy} onClick={() => onDeleteInstance(instance)}><Trash2 size={14} /></button>
            </div>
          </article>
        ))}
        {!instances.length && <p className="muted">{t('watchlist2.noInstancesInPool')}</p>}
      </div>
    </section>
  );
}
