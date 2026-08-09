import React from 'react';
import { Metric, Pair } from './common.jsx';
import { BlockedStrategyPanel } from './scanner-widgets.jsx';
import { AnalysisTracePanel, LazyJsonPanel, VirtualList, buildTradeInstanceTrace } from './trace-panels.jsx';
import {
  advisorStatusLabel,
  advisorSummary,
  candidateEvidenceText,
  candidateStatusLabel,
  compact,
  entryOrderTypeLabel,
  eventMessageLabel,
  eventTypeLabel,
  fmt,
  formatTime,
  lifecycleLabel,
  pnlBasisLabel,
  pnlWarningLabel,
  pct,
  previewText,
  protectionContractLabel,
  rejectionReason,
  riskPlanSourceLabel,
  shortContract,
  strategyBreakevensLabel,
  strategyDirectionLabel,
  topUpStatusLabel,
  winLossLabel,
} from '../utils/display.js';
import { t } from '../i18n/index.js';

export function CandidateSnapshotTab({ symbols }) {
  return (
    <div className="detail-section">
      <VirtualList
        items={symbols}
        itemHeight={230}
        maxHeight={600}
        className="candidate-snapshot-list"
        empty={<p className="muted">{t('trading2.candidatePoolAfterScan')}</p>}
        renderItem={(symbol) => (
          <article className="detail-card virtual-card" key={symbol.symbol || symbol.top_contract}>
            <div className="detail-card-head">
              <strong>{symbol.symbol || '--'}</strong>
              <span>{candidateStatusLabel(symbol.status)} · {symbol.technical_bias || t('trading2.directionDash')}</span>
            </div>
            <small>{candidateEvidenceText(symbol.technical_evidence)}</small>
            <div className="mini-contract-grid">
              {(symbol.top_candidates || []).map((candidate) => (
                <div className="mini-contract" key={candidate.contract_symbol}>
                  <strong>{shortContract(candidate.contract_symbol)}</strong>
                  <span>{candidate.side || '--'} · {candidate.expiration || '--'} · K {fmt(candidate.strike)}</span>
                  <small>{t('trading2.askShort')} {fmt(candidate.ask)} · IV {pct(Number(candidate.implied_volatility || 0) * 100)} · Δ {fmt(candidate.delta)} · {t('trading2.volumeShort')} {compact(candidate.volume)}</small>
                  <small>{t('trading2.scoreShort')} {fmt(candidate.analysis_score)} · {candidate.strategy_tag || candidate.execution_quality_state || '--'}</small>
                </div>
              ))}
              {(symbol.top_strategy_candidates || []).map((candidate, index) => (
                <div className="mini-contract strategy-mini-contract" key={`${symbol.symbol}-strategy-${candidate.strategy_type}-${index}`}>
                  <strong>{candidate.label || candidate.strategy_type}</strong>
                  <span>{candidate.expiration || '--'} · {strategyDirectionLabel(candidate.direction)}</span>
                  <small>{t('trading2.maxLossShort')} {fmt(candidate.max_loss)} · {t('trading2.maxProfitShort')} {candidate.max_profit == null ? t('trading2.unlimited') : fmt(candidate.max_profit)} · {t('trading2.scoreShort')} {fmt(candidate.score)}</small>
                  <small>{t('trading2.legsCountShort')} {(candidate.legs || []).length} · {strategyBreakevensLabel(candidate.breakevens)}</small>
                </div>
              ))}
              {!(symbol.top_candidates || []).length && !(symbol.top_strategy_candidates || []).length && <p className="muted">{t('trading2.noCandidatesForSymbol')}</p>}
            </div>
          </article>
        )}
      />
    </div>
  );
}

export function DecisionTab({ decision, finalTopN, advisors, rejected }) {
  return (
    <div className="detail-section">
      <div className="detail-grid">
        <div className="detail-card wide">
          <strong>{t('trading2.moderatorSummary')}</strong>
          <p className="detail-copy">{decision.summary || '--'}</p>
          {decision.top_up?.needed && <small>{t('trading2.topUp')}{topUpStatusLabel(decision.top_up.status)} · {t('trading2.added')} {decision.top_up.added_count ?? 0}/{decision.top_up.missing_count ?? 0}</small>}
        </div>
        <div className="detail-card">
          <strong>{t('trading2.finalSelected')}</strong>
          <div className="mini-list">
            {finalTopN.map((item) => (
              <span key={item.contract_symbol || item.strategy_key || item.tracking_id}>
                {item.contract_symbol
                  ? `${shortContract(item.contract_symbol)}`
                  : `${item.label || item.strategy_type || item.strategy_key || t('trading2.strategyWord')}${item.strategy_key ? ` · ${item.strategy_key}` : ''}`}
                · {pct(Number(item.allocation_pct || 0) * 100)}
                {item.stop_loss_pct != null ? ` · ${t('trading2.stopLossShort')} ${pct(item.stop_loss_pct)}` : ''}
              </span>
            ))}
            {!finalTopN.length && <small>{t('trading2.noFinalSelection')}</small>}
          </div>
        </div>
        <div className="detail-card">
          <strong>{t('trading2.rejectedRemoved')}</strong>
          <div className="mini-list">
            {rejected.slice(0, 8).map((item, index) => (
              <span key={`${item.contract_symbol || item.source || 'reject'}-${index}`}>{shortContract(item.contract_symbol)} · {previewText(rejectionReason(item), 90)}</span>
            ))}
            {!rejected.length && <small>{t('trading2.noRejections')}</small>}
          </div>
        </div>
      </div>
      <div className="advisor-detail-grid">
        {advisors.map((advisor) => (
          <article className="advisor-note detail-advisor" key={advisor.key || advisor.advisor}>
            <strong>{advisor.advisor || advisor.key}</strong>
            <span>{advisorStatusLabel(advisor.status)}</span>
            <small>{advisorSummary(advisor)}</small>
          </article>
        ))}
        {!advisors.length && <p className="muted">{t('trading2.advisorRecordsNote')}</p>}
      </div>
    </div>
  );
}

export function TraceTab({ instance }) {
  return (
    <div className="detail-section">
      <AnalysisTracePanel trace={instance?.analysis_trace || buildTradeInstanceTrace(instance)} fallbackTitle={t('trading2.instanceDecisionTrace')} />
    </div>
  );
}

export function RiskTab({
  blockedStrategyItems,
  executionOrders,
  positions,
  protectionContracts,
  strategyPositions,
}) {
  return (
    <div className="detail-section">
      <BlockedStrategyPanel items={blockedStrategyItems} />
      <div className="risk-table">
        {positions.map((position) => (
          <article className="detail-card" key={position.contract_symbol}>
            <div className="detail-card-head">
              <strong>{shortContract(position.contract_symbol)}</strong>
              <span>{position.symbol} · {position.side || '--'}</span>
            </div>
            <dl className="intent compact-dl">
              <Pair k={t('trading2.plannedQuantity')} v={position.estimated_quantity ?? '--'} />
              <Pair k={t('trading2.plannedCapital')} v={`$${fmt(position.allocation_amount)}`} />
              <Pair k={t('trading2.maxLoss')} v={`$${fmt(position.max_loss)}`} />
              <Pair k={t('trading2.stopPrice')} v={fmt(position.stop_trigger_price)} />
              <Pair k={t('trading2.takeProfit')} v={position.tiered_take_profit_enabled ? `${fmt(position.take_profit_1)} / ${fmt(position.take_profit_2)}` : fmt(position.take_profit_1)} />
              <Pair k={t('trading2.latestExit')} v={position.latest_exit || '--'} />
              <Pair k={t('trading2.overnight')} v={position.allow_overnight ? t('trading2.allowed') : t('trading2.notAllowed')} />
              <Pair k={t('trading2.invalidationCondition')} v={position.underlying_invalidation || '--'} />
            </dl>
          </article>
        ))}
        {strategyPositions.map((position) => (
          <article className="detail-card" key={position.tracking_id}>
            <div className="detail-card-head">
              <strong>{position.label || position.strategy_type}</strong>
              <span>{position.symbol} · {position.tracking_status || '--'}</span>
            </div>
            <dl className="intent compact-dl">
              <Pair k={t('trading2.strategyType')} v={position.strategy_type || '--'} />
              <Pair k={t('trading2.expiration')} v={position.expiration || '--'} />
              <Pair k={t('trading2.riskSource')} v={riskPlanSourceLabel(position.risk_plan_source)} />
              <Pair k={t('trading2.aiConfidence')} v={position.ai_risk_plan ? pct(Number(position.ai_risk_plan.confidence || 0) * 100) : '--'} />
              <Pair k={t('trading2.executionStatus')} v={position.execution_status || '--'} />
              <Pair k={t('trading2.executionUnits')} v={position.strategy_units ?? '--'} />
              <Pair k={t('trading2.combinedMark')} v={fmt(position.last_mark)} />
              <Pair k={t('trading2.estimatedPnl')} v={fmt(position.last_pnl)} />
              <Pair k={t('trading2.actualRealized')} v={fmt(position.realized_pnl ?? position.actual_exit_pnl)} />
              <Pair k={t('trading2.triggerEstimate')} v={fmt(position.strategy_exit_trigger_mark_pnl)} />
              <Pair k={t('trading2.stopLossPnl')} v={fmt(position.stop_loss_pnl)} />
              <Pair k={t('trading2.takeProfit')} v={position.tiered_take_profit_enabled ? `${fmt(position.take_profit_1_pnl)} / ${fmt(position.take_profit_2_pnl)}` : fmt(position.take_profit_1_pnl)} />
              <Pair k={t('trading2.latestExit')} v={position.latest_exit || '--'} />
              <Pair k={t('trading2.overnight')} v={position.allow_overnight ? t('trading2.allowed') : t('trading2.notAllowed')} />
              <Pair k={t('trading2.invalidationCondition')} v={position.invalidation || '--'} />
              <Pair k={t('trading2.trackingState')} v={position.risk_tracking_active ? t('trading2.on') : t('trading2.off')} />
              <Pair k={t('trading2.manualReview')} v={position.manual_review_required ? t('trading2.required') : t('trading2.notRequired')} />
            </dl>
            {position.ai_risk_plan?.reason && <p className="detail-copy">{position.ai_risk_plan.reason}</p>}
          </article>
        ))}
        {!positions.length && !strategyPositions.length && <p className="muted">{t('trading2.riskPlanAfterAi')}</p>}
      </div>
      <div className="detail-grid">
        <div className="detail-card">
          <strong>{t('trading2.executionPlan')}</strong>
          <div className="mini-list">
            {executionOrders.map((order) => (
              <span key={order.contract_symbol}>{shortContract(order.contract_symbol)} · {entryOrderTypeLabel(order.entry_order_type)} · {t('trading2.quantityShort')} {order.estimated_quantity ?? 0} · {t('trading2.waitShort')} {order.wait_for_fill_seconds ?? 0}s</span>
            ))}
            {!executionOrders.length && <small>{t('trading2.noExecutionPlan')}</small>}
          </div>
        </div>
        <div className="detail-card">
          <strong>{t('trading2.protectionStatus')}</strong>
          <div className="mini-list">
            {protectionContracts.map((item) => (
              <span key={item.contract_symbol || item.order_symbol}>{shortContract(item.contract_symbol || item.order_symbol)} · {protectionContractLabel(item)}</span>
            ))}
            {!protectionContracts.length && <small>{t('trading2.noProtectionRecords')}</small>}
          </div>
        </div>
      </div>
    </div>
  );
}

export function EventsTab({ events }) {
  return (
    <div className="detail-section">
      <VirtualList
        items={events}
        itemHeight={86}
        maxHeight={520}
        className="event-stream"
        empty={<p className="muted">{t('trading2.eventsTimelineNote')}</p>}
        renderItem={(event, index) => (
          <div className={`timeline-item ${event.status || 'info'}`} key={`${event.event_type}-${event.time}-${index}`}>
            <span>{formatTime(event.time)}</span>
            <strong>{eventMessageLabel(event.message)}</strong>
            <small>{lifecycleLabel(event.lifecycle_state)} · {eventTypeLabel(event.event_type)}</small>
          </div>
        )}
      />
    </div>
  );
}

export function ReviewTab({ review }) {
  const pnlWarnings = Array.isArray(review.pnl_warnings) ? review.pnl_warnings : [];
  return (
    <div className="detail-section">
      <div className="detail-metrics">
        <Metric label={t('trading2.maxFavorableExcursion')} value={fmt(review.mfe)} />
        <Metric label={t('trading2.maxAdverseExcursion')} value={fmt(review.mae)} />
        <Metric label={t('trading2.peakUnrealized')} value={fmt(review.max_unrealized_profit)} />
        <Metric label={t('trading2.maxDrawdown')} value={fmt(review.max_drawdown)} />
        <Metric label={t('trading2.holdingMinutes')} value={fmt(review.holding_minutes)} />
        <Metric label={t('trading2.unrealizedPnl')} value={fmt(review.current_unrealized_pnl)} sub={`${t('trading2.openShort')} ${review.open_quantity ?? 0}`} />
        <Metric label={t('trading2.actualRealized')} value={fmt(review.realized_pnl)} sub={`${t('trading2.strategyWord')} ${fmt(review.strategy_realized_pnl)}`} />
        <Metric label={t('trading2.triggerEstimate')} value={fmt(review.strategy_trigger_mark_pnl)} sub={t('trading2.strategyTriggerMark')} />
        <Metric label={t('trading2.totalPnl')} value={fmt(review.estimated_total_pnl)} sub={`${pct(review.return_pct)} · ${winLossLabel(review.win_loss)}`} />
        <Metric label={t('trading2.aiConfidence')} value={pct(review.ai_confidence_avg)} sub={`${t('trading2.deviation')} ${pct(review.ai_confidence_vs_return)}`} />
      </div>
      <div className={`status-box ${pnlWarnings.length ? 'warning-box' : 'neutral-box'}`}>
        <strong>{t('trading2.pnlBasis')}{pnlBasisLabel(review.pnl_basis)}</strong>
        <p>{pnlWarnings.length ? pnlWarnings.map(pnlWarningLabel).join('；') : t('trading2.pnlBasisNote')}</p>
      </div>
      <div className="detail-grid">
        <div className="detail-card">
          <strong>{t('trading2.executionStats')}</strong>
          <dl className="intent compact-dl">
            <Pair k={t('trading2.submittedOrders')} v={review.submitted_orders ?? '--'} />
            <Pair k={t('trading2.filledEntries')} v={review.filled_entries ?? '--'} />
            <Pair k={t('trading2.entryCost')} v={`$${fmt(review.entry_cost)}`} />
            <Pair k={t('trading2.closedOpen')} v={`${review.closed_quantity ?? 0} / ${review.open_quantity ?? 0}`} />
            <Pair k={t('trading2.failedOrders')} v={review.failed_orders ?? '--'} />
            <Pair k={t('trading2.skippedOrders')} v={review.skipped_orders ?? '--'} />
            <Pair k={t('trading2.softwareStopArmed')} v={review.software_stop_armed ?? '--'} />
            <Pair k={t('trading2.softwareStopSubmitted')} v={review.software_stop_submitted ?? '--'} />
            <Pair k={t('trading2.softwareTpArmed')} v={review.software_take_profit_armed ?? '--'} />
            <Pair k={t('trading2.softwareTpSubmitted')} v={review.software_take_profit_submitted ?? '--'} />
          </dl>
        </div>
        <div className="detail-card">
          <strong>{t('trading2.reviewTriggers')}</strong>
          <dl className="intent compact-dl">
            <Pair k={t('trading2.firstExitTrigger')} v={review.first_exit_trigger || '--'} />
            <Pair k={t('trading2.unprotectedContracts')} v={review.unprotected_contracts ?? '--'} />
            <Pair k={t('trading2.softwareStopFailed')} v={review.software_stop_failed ?? '--'} />
            <Pair k={t('trading2.softwareTpFailed')} v={review.software_take_profit_failed ?? '--'} />
          </dl>
        </div>
      </div>
    </div>
  );
}

export function RawInstanceTab({ instance }) {
  return (
    <div className="detail-section">
      <LazyJsonPanel title={t('trading2.instanceRawData')} data={instance} />
    </div>
  );
}
