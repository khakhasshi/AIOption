import React from 'react';
import { Metric, SectionTitle } from '../common.jsx';
import { CopyableId } from '../copyable-id.jsx';
import { BlockedStrategyPanel } from '../scanner-widgets.jsx';
import { eventMessageLabel, eventTypeLabel, fmt, formatTime, lifecycleDisplayLabel, lifecycleLabel, protectionContractLabel, protectionSummaryLabel, protectionTone, shortContract, shortId, userErrorLabel } from '../../utils/display.js';
import { t } from '../../i18n/index.js';

export function ActiveInstancePanel({ activeRun, blockedStrategyItems, instanceRisk, openInstanceDetailPage, protection, timeline, tradeInstance }) {
  return (
<div className={`panel instance-panel ${protection.requires_manual_attention ? 'attention' : ''}`}>
  <div className="answer-head">
    <SectionTitle title={t('trading2.tradeInstance')} />
    <div className="action-row">
      <CopyableId value={tradeInstance.locator_id || activeRun.locator_id || activeRun.id} label={t('trading2.tradeInstance')} />
      <span className={`instance-state ${tradeInstance.lifecycle_state || 'created'}`}>
        {lifecycleDisplayLabel(tradeInstance.lifecycle_state, protection)}
      </span>
    </div>
  </div>
  <div className="instance-grid">
    <Metric label={t('trading2.instanceId')} value={tradeInstance.locator_id || activeRun.locator_id || shortId(tradeInstance.instance_id || activeRun.id)} sub={formatTime(tradeInstance.created_at)} />
    <Metric label={t('trading2.candidateContracts')} value={tradeInstance.candidate_snapshot?.contract_candidates ?? 0} sub={`${t('trading2.symbolsShort')} ${tradeInstance.candidate_snapshot?.symbols_with_candidates ?? 0}/${tradeInstance.candidate_snapshot?.symbols_scanned ?? 0}`} />
    <Metric label={t('trading2.plannedRisk')} value={`$${fmt(instanceRisk.planned_premium_at_risk)}`} sub={`${t('trading2.contractsShort')} ${instanceRisk.planned_contracts ?? 0}`} />
    <Metric label={t('trading2.protection')} value={protectionSummaryLabel(protection)} sub={`${t('trading2.brokerShort')} ${protection.protected_quantity ?? 0} · ${t('trading2.softwareShort')} ${protection.software_protected_quantity ?? 0} · ${t('trading2.unprotectedShort')} ${protection.unprotected_quantity ?? 0}`} tone={protectionTone(protection)} />
    <Metric label={t('trading2.takeProfit')} value={protection.software_take_profit_active ? t('trading2.monitoring') : '--'} sub={`${t('trading2.softwareShort')} ${protection.software_take_profit_quantity ?? 0}`} tone={protection.software_take_profit_active ? 'warning' : 'muted'} />
  </div>
  {protection.requires_manual_attention && (
    <div className="status-box danger-box">
      <strong>{t('trading2.needsAttention')}</strong>
      <p>{userErrorLabel(protection.stop_failure_reason) || t('trading2.unprotectedPositionsNote')}</p>
    </div>
  )}
  <BlockedStrategyPanel items={blockedStrategyItems} compact />
  {protection.software_stop_active && (
    <div className="status-box warning-box">
      <strong>{t('trading2.softwareStopMonitoring')}</strong>
      <p>{t('trading2.softwareStopMonitoringNote').replace('{n}', protection.software_protected_quantity || 0)}</p>
    </div>
  )}
  {protection.software_take_profit_active && (
    <div className="status-box warning-box">
      <strong>{t('trading2.softwareTakeProfitMonitoring')}</strong>
      <p>{t('trading2.softwareTakeProfitMonitoringNote').replace('{n}', protection.software_take_profit_quantity || 0)}</p>
    </div>
  )}
  {protection.contracts?.length > 0 && (
    <div className="protection-list">
      {protection.contracts.slice(0, 8).map((item) => (
        <span key={item.contract_symbol || item.order_symbol}>
          {shortContract(item.contract_symbol || item.order_symbol)} · {protectionContractLabel(item)}
        </span>
      ))}
    </div>
  )}
  <div className="instance-timeline">
    {timeline.map((event, index) => (
      <div className={`timeline-item ${event.status || 'info'}`} key={`${event.event_type}-${event.time}-${index}`}>
        <span>{formatTime(event.time)}</span>
        <strong>{eventMessageLabel(event.message)}</strong>
        <small>{lifecycleLabel(event.lifecycle_state)} · {eventTypeLabel(event.event_type)}</small>
      </div>
    ))}
    {!timeline.length && <p className="muted">{t('trading2.eventsAfterCreate')}</p>}
  </div>
  <div className="action-row instance-actions">
    <button className="ghost" type="button" disabled={!activeRun?.id} onClick={() => openInstanceDetailPage(activeRun.id)}>{t('trading2.standaloneDetail')}</button>
  </div>
</div>
  );
}
