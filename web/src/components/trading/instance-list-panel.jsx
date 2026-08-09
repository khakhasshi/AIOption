import React from 'react';
import { Metric, SectionTitle } from '../common.jsx';
import { CopyableId } from '../copyable-id.jsx';
import { instanceFilterItems } from '../../config.js';
import {
  fmt,
  formatTime,
  instanceActionResultTitle,
  instanceAttention,
  instanceLifecycleLabel,
  protectionStateLabel,
  protectionSummaryLabel,
  protectionTone,
  runLifecycle,
  runOrderCount,
  runPlannedRisk,
  runProtection,
  runSelectionCount,
  runTaskStatusLabel,
  shortId,
  stageLabel,
} from '../../utils/display.js';
import { t } from '../../i18n/index.js';

export function InstanceListPanel({
  activeRun,
  allVisibleRunsSelected,
  bulkDeleteInstances,
  deleteCurrentInstance,
  filteredRuns,
  instanceActionResult,
  instanceActionRunning,
  instanceListExpanded,
  instanceListFilter,
  instanceStats,
  openInstanceDetailPage,
  refreshTradingRun,
  refreshTradingRuns,
  resetCurrentInstanceRisk,
  selectedRunCount,
  selectedRunIds,
  setInstanceListExpanded,
  setInstanceListFilter,
  setSelectedRunIds,
  visibleRunIds,
  visibleRuns,
}) {
  return (
<div className="panel instance-list-panel">
  <div className="answer-head">
    <SectionTitle title={t('trading2.instanceList')} />
    <div className="action-row">
      <button className="ghost" type="button" disabled={instanceActionRunning || !activeRun?.id} onClick={resetCurrentInstanceRisk}>
        {instanceActionRunning ? t('trading2.processing') : t('trading2.resetRisk')}
      </button>
      <button className="danger compact-danger" type="button" disabled={instanceActionRunning || selectedRunCount === 0} onClick={bulkDeleteInstances}>
        {t('trading2.bulkDelete')}{selectedRunCount ? ` ${selectedRunCount}` : ''}
      </button>
      <button className="danger compact-danger" type="button" disabled={instanceActionRunning || !activeRun?.id} onClick={deleteCurrentInstance}>
        {t('trading2.deleteInstance')}
      </button>
      <button className="ghost" type="button" onClick={refreshTradingRuns}>{t('trading2.refreshList')}</button>
    </div>
  </div>
  <div className="instance-list-summary">
    <Metric label={t('trading2.allInstances')} value={instanceStats.total} tone={instanceStats.total ? 'ok' : 'muted'} />
    <Metric label={t('trading2.activeInstances')} value={instanceStats.active} tone={instanceStats.active ? 'warning' : 'muted'} />
    <Metric label={t('trading2.needsHandling')} value={instanceStats.attention} tone={instanceStats.attention ? 'danger' : 'muted'} />
    <Metric label={t('trading2.protectedCount')} value={instanceStats.protected} tone={instanceStats.protected ? 'ok' : 'muted'} />
    <Metric label={t('trading2.closedCount')} value={instanceStats.closed} tone={instanceStats.closed ? 'muted' : 'muted'} />
  </div>
  <div className="instance-filter-row">
    {instanceFilterItems.map(([key, label]) => (
      <button key={key} type="button" className={instanceListFilter === key ? 'active' : ''} onClick={() => setInstanceListFilter(key)}>
        {label}
      </button>
    ))}
    <button
      type="button"
      disabled={!visibleRunIds.length}
      onClick={() => setSelectedRunIds((current) => {
        if (allVisibleRunsSelected) return current.filter((runId) => !visibleRunIds.includes(runId));
        return [...new Set([...current, ...visibleRunIds])];
      })}
    >
      {allVisibleRunsSelected ? t('trading2.deselectCurrent') : t('trading2.selectCurrent')}
    </button>
    {selectedRunCount > 0 && (
      <button type="button" onClick={() => setSelectedRunIds([])}>{t('trading2.clearSelection')}</button>
    )}
  </div>
  <div className="instance-list">
    {visibleRuns.map((run) => (
      <article className={`instance-row ${activeRun?.id === run.id ? 'active' : ''} ${instanceAttention(run) ? 'attention' : ''}`} key={run.id}>
        <label className="instance-row-check" title={t('trading2.selectInstance')}>
          <input
            type="checkbox"
            checked={selectedRunIds.includes(run.id)}
            onChange={(event) => {
              setSelectedRunIds((current) => event.target.checked
                ? [...new Set([...current, run.id])]
                : current.filter((runId) => runId !== run.id));
            }}
          />
        </label>
        <button type="button" className="instance-row-main" onClick={() => refreshTradingRun(run.id)}>
          <div>
            <span className={`instance-state ${runLifecycle(run) === 'closed' && runProtection(run).state === 'strategy_residual_tracking' ? 'monitoring' : runLifecycle(run)}`}>{instanceLifecycleLabel(run)}</span>
            <strong>{formatTime(run.created_at)}</strong>
            <small>{shortId(run.id)} · {runTaskStatusLabel(run.status)} · {stageLabel(run.stage)}</small>
          </div>
          <div className="instance-row-metrics">
            <span>{t('trading2.selectionShort')} {runSelectionCount(run)}</span>
            <span>{t('trading2.orderShort')} {runOrderCount(run)}</span>
            <span>{t('trading2.riskShort')} ${fmt(runPlannedRisk(run))}</span>
            <span className={`protection-pill ${protectionTone(runProtection(run))}`}>{protectionSummaryLabel(runProtection(run))}</span>
          </div>
        </button>
        <div className="instance-row-actions">
          <CopyableId value={run.locator_id || run.id} label={t('trading2.tradeInstance')} compact />
          <button className="ghost" type="button" onClick={() => refreshTradingRun(run.id)}>{t('trading2.view')}</button>
          <button className="ghost" type="button" onClick={() => openInstanceDetailPage(run.id)}>{t('trading2.detailPage')}</button>
        </div>
      </article>
    ))}
    {!filteredRuns.length && (
      <p className="muted">{t('trading2.noInstancesFilter')}</p>
    )}
  </div>
  {filteredRuns.length > 3 && (
    <div className="list-footer-actions">
      <button className="ghost compact" type="button" onClick={() => setInstanceListExpanded(!instanceListExpanded)}>
        {instanceListExpanded ? t('trading2.collapseInstances') : t('trading2.expandAll').replace('{n}', filteredRuns.length)}
      </button>
    </div>
  )}
  {instanceActionResult && ['reset-risk', 'delete', 'bulk-delete'].includes(instanceActionResult.action) && (
    <div className={`status-box ${['delete', 'bulk-delete'].includes(instanceActionResult.action) ? 'danger-box' : 'warning-box'}`}>
      <strong>{instanceActionResultTitle(instanceActionResult.action)}</strong>
      {instanceActionResult.action === 'reset-risk' ? (
        <p>{t('trading2.orderShort')} {instanceActionResult.order_count || 0} · {t('trading2.protection')} {protectionStateLabel(instanceActionResult.protection_status?.state)} · {t('trading2.unprotectedShort')} {instanceActionResult.protection_status?.unprotected_quantity ?? 0}</p>
      ) : instanceActionResult.action === 'bulk-delete' ? (
        <p>{t('trading2.bulkDeleteResult').replace('{deleted}', instanceActionResult.deleted_count || 0).replace('{failed}', instanceActionResult.failed_count || 0).replace('{requested}', instanceActionResult.requested_count || 0)}</p>
      ) : (
        <p>{t('trading2.deleteResult').replace('{id}', shortId(instanceActionResult.run_id))}</p>
      )}
    </div>
  )}
</div>
  );
}
