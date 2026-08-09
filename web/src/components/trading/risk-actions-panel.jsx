import React from 'react';
import { SectionTitle } from '../common.jsx';
import { flattenSideLabel, instanceActionResultTitle, userErrorLabel } from '../../utils/display.js';
import { t } from '../../i18n/index.js';

export function RiskActionsPanel({
  activeRun,
  cancelInstanceOrders,
  config,
  flattenAllPositions,
  flattenCurrentInstance,
  flattenResult,
  flattening,
  instanceActionResult,
  instanceActionRunning,
}) {
  const instanceSubmitted = (instanceActionResult?.submitted_count || 0) + (instanceActionResult?.strategy_submitted_count || 0);
  const instanceFailed = (instanceActionResult?.failed_count || 0) + (instanceActionResult?.strategy_failed_count || 0) + (instanceActionResult?.cancel_failed_count || 0) + (instanceActionResult?.failed?.length || 0);
  const flattenSubmitted = (flattenResult?.submitted_count || 0) + (flattenResult?.strategy_submitted_count || 0);
  const flattenFailed = (flattenResult?.failed_count || 0) + (flattenResult?.strategy_failed_count || 0) + (flattenResult?.cancel_failed_count || 0);
  return (
<div className="panel danger-zone">
  <div className="answer-head">
    <SectionTitle title={t('trading2.liveTradeActions')} />
    <span className="muted">{t('trading2.liveTradeActionsNote')}</span>
  </div>
  <div className="danger-actions">
    <div className="danger-action-card">
      <span className="scope-chip danger-scope">{t('trading2.scopeAllAccount')}</span>
      <strong>{t('trading2.flattenAll')}</strong>
      <p>{t('trading2.flattenAllDesc')}</p>
      <button className="danger compact-danger" type="button" disabled={flattening || ((config.broker === 'alpaca' || config.broker === 'usmart') ? !config.broker_account : !config.longbridge_account)} onClick={flattenAllPositions}>
        {flattening ? t('trading2.flatteningAll') : t('trading2.flattenAllBtn')}
      </button>
    </div>
    <div className="danger-action-card">
      <span className="scope-chip warning-scope">{t('trading2.scopeCurrentInstance')}</span>
      <strong>{t('trading2.flattenInstance')}</strong>
      <p>{t('trading2.flattenInstanceDesc')}</p>
      <button className="danger compact-danger" type="button" disabled={instanceActionRunning || !activeRun?.id} onClick={flattenCurrentInstance}>
        {instanceActionRunning ? t('trading2.processing') : t('trading2.flattenInstanceBtn')}
      </button>
    </div>
    <div className="danger-action-card">
      <span className="scope-chip neutral-scope">{t('trading2.scopeCancelOnly')}</span>
      <strong>{t('trading2.cancelOrders')}</strong>
      <p>{t('trading2.cancelOrdersDesc')}</p>
      <button className="ghost" type="button" disabled={instanceActionRunning || !activeRun?.id} onClick={cancelInstanceOrders}>{t('trading2.cancelOrders')}</button>
    </div>
  </div>
  {instanceActionResult && !['reset-risk', 'delete', 'bulk-delete'].includes(instanceActionResult.action) && (
    <div className={`status-box ${instanceFailed ? 'danger-box' : 'warning-box'}`}>
      <strong>{instanceActionResultTitle(instanceActionResult.action)}</strong>
      <p>
        {t('trading2.canceledLabel')} {instanceActionResult.canceled_order_count ?? instanceActionResult.canceled_count ?? 0} ·
        {t('trading2.flattenSubmitted')} {instanceSubmitted} ·
        {t('trading2.failedCount')} {instanceFailed} ·
        {t('trading2.skippedCount')} {instanceActionResult.skipped_count || 0}
      </p>
    </div>
  )}
  {flattenResult && (
    <div className={`status-box ${flattenFailed ? 'danger-box' : 'warning-box'}`}>
      <strong>{t('trading2.flattenAllResult')}</strong>
      <p>
        {t('trading2.canceledLabel')} {flattenResult.canceled_order_count || 0} · {t('trading2.flattenSubmitted')} {flattenSubmitted} ·
        {t('trading2.failedCount')} {flattenFailed} · {t('trading2.skippedCount')} {flattenResult.skipped_count || 0}
      </p>
      {flattenResult.submitted?.length > 0 && (
        <div className="flatten-list">
          {flattenResult.submitted.slice(0, 6).map((item) => (
            <span key={`${item.symbol}-${item.side}-${item.quantity}`}>
              {item.symbol} · {flattenSideLabel(item.side)} {item.quantity}
            </span>
          ))}
        </div>
      )}
      {flattenResult.failed?.length > 0 && (
        <small>{flattenResult.failed.slice(0, 2).map((item) => `${item.symbol}: ${userErrorLabel(item.error)}`).join('；')}</small>
      )}
    </div>
  )}
</div>
  );
}
