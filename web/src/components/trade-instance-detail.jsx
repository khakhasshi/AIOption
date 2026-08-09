import React, { useEffect, useState } from 'react';
import { Metric, Pair, SectionTitle } from './common.jsx';
import { CopyableId } from './copyable-id.jsx';
import {
  CandidateSnapshotTab,
  DecisionTab,
  EventsTab,
  RawInstanceTab,
  ReviewTab,
  RiskTab,
  TraceTab,
} from './trade-instance-detail-tabs.jsx';
import { instanceDetailTabs } from '../config.js';
import {
  buildBlockedStrategyItems,
  councilModeLabel,
  entryOrderTypeLabel,
  environmentLabel,
  eventMessageLabel,
  eventTypeLabel,
  fmt,
  formatTime,
  lifecycleDisplayLabel,
  lifecycleLabel,
  pct,
  protectionSummaryLabel,
  shortId,
  strategyModeLabel,
  triggerSourceLabel,
  winLossLabel,
} from '../utils/display.js';
import { t } from '../i18n/index.js';

export function TradeInstanceDetail({ instance, activeTab, onTabChange, startCollapsed = false }) {
  const [collapsed, setCollapsed] = useState(startCollapsed);
  const basic = instance?.basic_info ?? {};
  const intent = instance?.strategy_intent ?? {};
  const snapshot = instance?.candidate_snapshot ?? {};
  const decision = instance?.ai_decision ?? {};
  const risk = instance?.risk_plan ?? {};
  const execution = instance?.execution_plan ?? {};
  const protectionStatus = instance?.protection_status ?? {};
  const review = instance?.review_metrics ?? {};
  const events = Array.isArray(instance?.event_timeline) ? [...instance.event_timeline].reverse() : [];
  const symbols = Array.isArray(snapshot.symbols) ? snapshot.symbols : [];
  const finalTopN = Array.isArray(decision.final_top_n) ? decision.final_top_n : [];
  const advisors = Array.isArray(decision.advisor_reports) ? decision.advisor_reports : [];
  const rejected = Array.isArray(decision.rejected) ? decision.rejected : [];
  const positions = Array.isArray(risk.positions) ? risk.positions : [];
  const strategyPositions = Array.isArray(risk.strategy_positions) ? risk.strategy_positions : [];
  const executionOrders = Array.isArray(execution.orders) ? execution.orders : [];
  const strategyExecutionOrders = Array.isArray(execution.strategy_orders) ? execution.strategy_orders : [];
  const protectionContracts = Array.isArray(protectionStatus.contracts) ? protectionStatus.contracts : [];
  const blockedStrategyItems = buildBlockedStrategyItems({
    orders: strategyExecutionOrders,
    strategyPositions,
    protectionContracts,
  });
  const latestEvent = events[0];

  useEffect(() => {
    setCollapsed(startCollapsed);
  }, [instance?.instance_id, startCollapsed]);

  return (
    <div className={`panel instance-detail ${collapsed ? 'collapsed' : ''}`}>
      <div className="answer-head">
        <SectionTitle title={t('trading2.instanceDetail')} />
        <div className="action-row">
          <CopyableId value={instance?.locator_id || instance?.basic_info?.locator_id || instance?.instance_id} label={t('trading2.tradeInstance')} />
          <span className="muted">{shortId(instance?.instance_id)} · v{instance?.version || 1}</span>
          <button className="ghost" type="button" onClick={() => setCollapsed((value) => !value)}>
            {collapsed ? t('trading2.expandDetail') : t('trading2.collapseDetail')}
          </button>
        </div>
      </div>
      {collapsed && (
        <>
          <div className="instance-brief">
            <Metric label={t('trading2.stage')} value={lifecycleDisplayLabel(instance?.lifecycle_state, protectionStatus)} sub={protectionSummaryLabel(protectionStatus)} />
            <Metric label={t('trading2.selectionShort')} value={decision.selection_count ?? finalTopN.length ?? 0} sub={`${t('trading2.candidateShort')} ${snapshot.contract_candidates ?? 0}`} />
            <Metric label={t('trading2.plannedRisk')} value={`$${fmt(risk.planned_premium_at_risk)}`} sub={`${t('trading2.positionShort')} ${positions.length}`} />
            <Metric label={t('trading2.protection')} value={protectionSummaryLabel(protectionStatus)} sub={`${t('trading2.unprotectedShort')} ${protectionStatus.unprotected_quantity ?? 0}`} />
            <Metric label={t('trading2.reviewPnl')} value={fmt(review.estimated_total_pnl)} sub={winLossLabel(review.win_loss)} />
            <Metric label={t('trading2.latestEvent')} value={eventTypeLabel(latestEvent?.event_type)} sub={latestEvent ? formatTime(latestEvent.time) : '--'} />
          </div>
          {latestEvent && (
            <div className={`timeline-item ${latestEvent.status || 'info'} brief-event`}>
              <strong>{eventMessageLabel(latestEvent.message)}</strong>
              <small>{lifecycleLabel(latestEvent.lifecycle_state)} · {eventTypeLabel(latestEvent.event_type)}</small>
            </div>
          )}
        </>
      )}
      {!collapsed && (
        <>
          <div className="detail-tabs">
            {instanceDetailTabs.map(([key, label]) => (
              <button key={key} type="button" className={activeTab === key ? 'active' : ''} onClick={() => onTabChange(key)}>
                {label}
              </button>
            ))}
          </div>

      {activeTab === 'overview' && (
        <div className="detail-section">
          <div className="detail-grid">
            <div className="detail-card">
              <strong>{t('trading2.basicInfo')}</strong>
              <dl className="intent compact-dl">
                <Pair k={t('trading2.triggerMethod')} v={triggerSourceLabel(basic.trigger_source)} />
                <Pair k={t('trading2.instanceIdShort')} v={instance?.locator_id || basic.locator_id || '--'} />
                <Pair k={t('trading2.strategyModeLabel')} v={strategyModeLabel(basic.strategy_mode)} />
                <Pair k={t('trading2.account')} v={basic.account_name || '--'} />
                <Pair k={t('trading2.tradeEnvironment')} v={environmentLabel(basic.paper_or_live)} />
                <Pair k={t('trading2.selectionCount')} v={basic.top_n ?? '--'} />
                <Pair k={t('trading2.runEt')} v={basic.run_time_et || '--'} />
              </dl>
            </div>
            <div className="detail-card">
              <strong>{t('trading2.strategyIntent')}</strong>
              <dl className="intent compact-dl">
                <Pair k={t('trading2.aiModel')} v={intent.ai_provider || '--'} />
                <Pair k={t('trading2.aiDecision')} v={intent.use_ai === false ? t('trading2.off') : (intent.council === false ? t('trading2.singleAi') : t('trading2.council'))} />
                <Pair k={t('trading2.aiAdjustAllocation')} v={intent.ai_adjust_allocation ? t('trading2.on') : t('trading2.off')} />
                <Pair k={t('trading2.aiAdjustStopLoss')} v={intent.ai_adjust_stop_loss ? t('trading2.on') : t('trading2.off')} />
                <Pair k={t('trading2.aiAdjustTakeProfit')} v={intent.ai_adjust_take_profit ? t('trading2.on') : t('trading2.off')} />
                <Pair k={t('trading2.softwareStop')} v={intent.software_stop_enabled === false ? t('trading2.off') : t('trading2.on')} />
                <Pair k={t('trading2.softwareTakeProfit')} v={intent.software_take_profit_enabled === false ? t('trading2.off') : t('trading2.on')} />
                <Pair k={t('trading2.lowGate')} v={intent.low_gate_enabled ? t('trading2.on') : t('trading2.off')} />
                <Pair k={t('trading2.defaultStopLoss')} v={`${fmt(intent.default_stop_loss_pct)}%`} />
                <Pair k={t('trading2.takeProfitMode')} v={intent.tiered_take_profit_enabled ? t('trading2.tieredTakeProfit') : t('trading2.oneShotTakeProfit')} />
                <Pair k={t('trading2.defaultTakeProfit')} v={intent.tiered_take_profit_enabled ? `TP1 ${fmt(intent.default_take_profit_1_pct ?? 20)}% / TP2 ${fmt(intent.default_take_profit_2_pct ?? 35)}%` : `${fmt(intent.default_take_profit_pct ?? 30)}%`} />
                <Pair k={t('trading2.entryMethod')} v={entryOrderTypeLabel(execution.entry_order_type)} />
              </dl>
            </div>
            <div className="detail-card wide">
              <strong>{t('trading2.promptWord')}</strong>
              <p className="detail-copy">{intent.prompt_template || '--'}</p>
            </div>
          </div>
          <div className="detail-metrics">
            <Metric label={t('trading2.scannedSymbols')} value={snapshot.symbols_scanned ?? 0} sub={`${t('trading2.withCandidates')} ${snapshot.symbols_with_candidates ?? 0}`} />
            <Metric label={t('trading2.candidateContracts')} value={snapshot.contract_candidates ?? 0} sub={`Top K ${snapshot.top_k_per_symbol ?? 0}`} />
            <Metric label={t('trading2.finalSelection')} value={decision.selection_count ?? 0} sub={councilModeLabel(decision.council_mode)} />
            <Metric label={t('trading2.plannedRisk')} value={`$${fmt(risk.planned_premium_at_risk)}`} sub={`${t('trading2.limitLabel')} ${fmt(risk.max_loss_if_all_premiums_lost)}`} />
            <Metric label={t('trading2.strategyTracking')} value={strategyPositions.length} sub={risk.strategy_analysis_only ? t('trading2.manualReviewOnly') : t('trading2.dynamicTracking')} />
            <Metric label={t('trading2.strategyExecution')} value={risk.strategy_auto_execute_enabled ? t('trading2.auto') : t('trading2.manual')} sub={risk.strategy_auto_execute_enabled ? t('trading2.multiLegSplit') : t('trading2.candidatesOnly')} />
          </div>
        </div>
      )}

      {activeTab === 'candidates' && (
        <CandidateSnapshotTab symbols={symbols} />
      )}

      {activeTab === 'decision' && (
        <DecisionTab decision={decision} finalTopN={finalTopN} advisors={advisors} rejected={rejected} />
      )}

      {activeTab === 'trace' && (
        <TraceTab instance={instance} />
      )}

      {activeTab === 'risk' && (
        <RiskTab
          blockedStrategyItems={blockedStrategyItems}
          executionOrders={executionOrders}
          positions={positions}
          protectionContracts={protectionContracts}
          strategyPositions={strategyPositions}
        />
      )}

      {activeTab === 'events' && (
        <EventsTab events={events} />
      )}

      {activeTab === 'review' && (
        <ReviewTab review={review} />
      )}

      {activeTab === 'raw' && (
        <RawInstanceTab instance={instance} />
      )}
        </>
      )}
    </div>
  );
}
