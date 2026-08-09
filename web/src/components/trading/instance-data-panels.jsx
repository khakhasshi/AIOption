import React from 'react';
import { SectionTitle } from '../common.jsx';
import { fmt, runStatusLabel, strategyOrderStatusLabel } from '../../utils/display.js';
import { t } from '../../i18n/index.js';

export function InstanceDataPanels({ executionSnapshot, scans, strategyExecutionOrders }) {
  const executionSource = executionSnapshot.source === 'unavailable_for_broker' ? t('trading2.execUnavailable') : t('trading2.execDetail');
  return (
    <>
<div className="panel history-panel">
  <SectionTitle title={t('trading2.candidatePool')} />
  {scans.length > 0 && (
    <div className="history-list scan-list">
      {scans.map((scan) => (
        <div className="history-item" key={scan.symbol}>
          <span>{runStatusLabel(scan.status)}</span>
          <strong>{scan.symbol} · {scan.candidate?.contract_symbol || '--'}</strong>
          <small>{t('trading2.scoreShort')} {fmt(scan.candidate?.analysis_score)} · {t('trading2.askShort')} {fmt(scan.candidate?.ask)}</small>
        </div>
      ))}
    </div>
  )}
  {!scans.length && <p className="muted">{t('trading2.noCandidatePool')}</p>}
</div>

{strategyExecutionOrders.length > 0 && (
  <div className="panel executions-panel">
    <div className="answer-head">
      <SectionTitle title={t('trading2.strategyRiskRecords')} />
      <span className="muted">{t('trading2.strategyTrackingTasks')}</span>
    </div>
    <div className="execution-list">
      {strategyExecutionOrders.map((item, index) => (
        <div className="execution-row" key={`${item.tracking_id || item.symbol || index}`}>
          <strong>{item.symbol || '--'}</strong>
          <span>{item.strategy_type || '--'} · {strategyOrderStatusLabel(item.status || item.strategy_exit_status)}</span>
          <small>{t('trading2.trackingShort')} {item.risk_tracking_active ? t('trading2.on') : t('trading2.off')} · {t('trading2.exitShort')} {strategyOrderStatusLabel(item.strategy_exit_status)} · {item.tracking_id || '--'}</small>
        </div>
      ))}
    </div>
  </div>
)}

<div className="panel executions-panel">
  <div className="answer-head">
    <SectionTitle title={t('trading2.filledOrdersSnapshot')} />
    <span className="muted">{t('trading2.filledShort')} {executionSnapshot.count ?? 0} · {t('trading2.notional')} ${fmt(executionSnapshot.notional)} · {executionSource}</span>
  </div>
  <div className="execution-list">
    {(executionSnapshot.rows ?? []).slice(0, 8).map((execution, index) => (
      <div className="execution-row" key={`${execution.trade_id || execution.order_id || index}`}>
        <strong>{execution.symbol || '--'}</strong>
        <span>{execution.quantity || '--'} @ {execution.price || '--'}</span>
        <small>{execution.trade_done_at || execution.order_id || '--'}</small>
      </div>
    ))}
    {!(executionSnapshot.rows ?? []).length && <p className="muted">{t('trading2.noFills')}</p>}
  </div>
</div>
    </>
  );
}
