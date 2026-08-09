import React from 'react';
import { SectionTitle } from '../common.jsx';
import { effectiveOrderStatus, fmt, orderDisplayTitle, orderExecutionSummary, orderStatusLabel, previewText, renderOrderDetail, riskPlanSourceLabel } from '../../utils/display.js';
import { t } from '../../i18n/index.js';

export function ExecutionRecordsPanel({
  monitorResult,
  orders,
  runMonitorOnce,
  strategyPositions,
}) {
  return (
<div className="panel">
  <SectionTitle title={t('trading2.executionRecords')} />
  <div className="answer-head">
    <p className="muted">{t('trading2.executionRecordsNote')}</p>
    <div className="action-row">
      <button className="ghost" type="button" onClick={runMonitorOnce}>{t('trading2.checkProtection')}</button>
    </div>
  </div>
  {monitorResult && (
    <div className={`status-box ${monitorResult.software_stop_failed ? 'danger-box' : 'warning-box'}`}>
      <strong>{t('trading2.protectionCheckResult')}</strong>
      <p>
        {t('trading2.checkedInstances')} {monitorResult.runs_checked || 0} · {t('trading2.updated')} {monitorResult.orders_changed || 0} ·
        {t('trading2.softwareStopTriggered')} {monitorResult.software_stop_triggered || 0} ·
        {t('trading2.softwareTakeProfitTriggered')} {monitorResult.software_take_profit_triggered || 0} ·
        {t('trading2.strategyStopAlerted')} {monitorResult.strategy_stop_alerted || 0} ·
        {t('trading2.strategyTakeProfitAlerted')} {monitorResult.strategy_take_profit_alerted || 0} ·
        {t('trading2.strategyAutoExit')} {monitorResult.strategy_auto_exit_submitted || 0} {t('trading2.legsUnit')} ·
        {t('trading2.failedCount')} {(monitorResult.software_stop_failed || 0) + (monitorResult.software_take_profit_failed || 0)}
        {monitorResult.strategy_auto_exit_failed ? ` · ${t('trading2.strategyExitFailed')} ${monitorResult.strategy_auto_exit_failed} ${t('trading2.legsUnit')}` : ''}
      </p>
    </div>
  )}
  <div className="trade-grid">
    {orders.map((order, index) => (
      <article className="trade-card" key={`${order.contract_symbol}-${order.quantity}-${index}`}>
        <strong>{orderStatusLabel(effectiveOrderStatus(order))} · {orderDisplayTitle(order)}</strong>
        <span>{orderExecutionSummary(order)}</span>
        <small>{renderOrderDetail(order)}</small>
      </article>
    ))}
    {!orders.length && <p className="muted">{t('trading2.noExecutionRecords')}</p>}
  </div>
  {strategyPositions.length > 0 && (
    <div className="strategy-tracking-list">
      <div className="answer-head">
        <SectionTitle title={t('trading2.strategyRiskTracking')} />
        <span className="muted">{t('trading2.strategyPnlMonitorNote')}</span>
      </div>
      <div className="trade-grid">
        {strategyPositions.map((position) => (
          <article className="trade-card" key={position.tracking_id}>
            <strong>{position.label || position.strategy_type} · {position.symbol}</strong>
            <span className={`source-chip ${position.risk_plan_source || 'system_default'}`}>{riskPlanSourceLabel(position.risk_plan_source)}</span>
            <span>{position.expiration || '--'} · {t('trading2.statusShort')} {position.tracking_status || '--'} · {t('trading2.actualShort')} {fmt(position.realized_pnl ?? position.actual_exit_pnl)} · {t('trading2.estimatedShort')} {fmt(position.last_pnl)}</span>
            <small>{position.tiered_take_profit_enabled ? `TP1 ${fmt(position.take_profit_1_pnl)} · TP2 ${fmt(position.take_profit_2_pnl)}` : `${t('trading2.takeProfitShort')} ${fmt(position.take_profit_1_pnl)}`} · {t('trading2.triggerShort')} {fmt(position.strategy_exit_trigger_mark_pnl)} · {t('trading2.markShort')} {fmt(position.last_mark)}</small>
            {(position.ai_risk_plan?.reason || position.invalidation || position.latest_exit) && (
              <small>{previewText(position.ai_risk_plan?.reason || position.invalidation || position.latest_exit, 160)}</small>
            )}
          </article>
        ))}
      </div>
    </div>
  )}
</div>
  );
}
