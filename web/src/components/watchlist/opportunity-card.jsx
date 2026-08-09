import React from 'react';
import { Archive, BookmarkCheck, BookmarkPlus, ChevronDown, ChevronUp, Maximize2, PauseCircle, Play, RefreshCw, SlidersHorizontal, Star } from 'lucide-react';
import { t } from '../../i18n/index.js';
import { CopyableId } from '../copyable-id.jsx';
import { formatTime } from '../../utils/display.js';

const directionLabels = {
  bullish: t('watchlist2.direction.bullish'),
  bearish: t('watchlist2.direction.bearish'),
  neutral: t('watchlist2.direction.neutral'),
  volatility_long: t('watchlist2.direction.volatility_long'),
  volatility_short: t('watchlist2.direction.volatility_short'),
  income: t('watchlist2.direction.income'),
  hedge: t('watchlist2.direction.hedge'),
  unknown: t('watchlist2.direction.unknown'),
};

const structureLabels = {
  single_leg_call: t('watchlist2.structure.single_leg_call'),
  single_leg_put: t('watchlist2.structure.single_leg_put'),
  single_leg: t('watchlist2.structure.single_leg'),
  debit_call_spread: 'Call Debit Spread',
  debit_put_spread: 'Put Debit Spread',
  credit_call_spread: 'Call Credit Spread',
  credit_put_spread: 'Put Credit Spread',
  long_straddle: 'Long Straddle',
  long_strangle: 'Long Strangle',
  short_straddle: 'Short Straddle',
  short_strangle: 'Short Strangle',
  iron_condor: 'Iron Condor',
  butterfly: 'Butterfly',
  calendar: 'Calendar',
  diagonal: 'Diagonal',
  covered_call: 'Covered Call',
  cash_secured_put: 'Cash Secured Put',
  collar: 'Collar',
  custom_multi_leg: t('watchlist2.structure.custom_multi_leg'),
};

const statusLabels = {
  created: t('watchlist2.status.created'),
  watching_entry: t('watchlist2.status.watching_entry'),
  triggered: t('watchlist2.status.triggered'),
  active_reference: t('watchlist2.status.active_reference'),
  tracking_reference: t('watchlist2.status.tracking_reference'),
  take_profit_zone: t('watchlist2.status.take_profit_zone'),
  stop_loss_zone: t('watchlist2.status.stop_loss_zone'),
  invalidated: t('watchlist2.status.invalidated'),
  expired: t('watchlist2.status.expired'),
  archived: t('watchlist2.status.archived'),
};

const lifecycleLabels = {
  watching: t('watchlist2.lifecycle.watching'),
  triggered: t('watchlist2.lifecycle.triggered'),
  tracking: t('watchlist2.lifecycle.tracking'),
  exited: t('watchlist2.lifecycle.exited'),
};

function statusLabel(item) {
  return item?.status_label || statusLabels[item?.status] || item?.status || '--';
}

function lifecycleLabel(item) {
  return lifecycleLabels[item?.lifecycle_phase] || lifecycleLabels[item?.status] || t('watchlist2.lifecycle.watching');
}

function fmtRef(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(2) : '--';
}

function opportunityRiskSummary(riskPlan) {
  const takeProfitLevels = Array.isArray(riskPlan?.take_profit?.levels) ? riskPlan.take_profit.levels : [];
  const takeProfit = takeProfitLevels.map((level) => `${level.label || 'TP'} ${fmtRef(level.underlying_reference)}`).join(' / ');
  const stopLoss = riskPlan?.stop_loss?.underlying_reference ? fmtRef(riskPlan.stop_loss.underlying_reference) : '--';
  return `${t('watchlist2.refTakeProfit')} ${takeProfit || '--'} · ${t('watchlist2.refStopLoss')} ${stopLoss} · ${riskPlan?.latest_exit || '--'}`;
}

function legSummary(legs) {
  if (!Array.isArray(legs) || !legs.length) return t('watchlist2.legsPending');
  return legs.slice(0, 4).map((leg) => {
    if (leg.asset_type === 'underlying') {
      return `${leg.action || '--'} Stock`;
    }
    const action = leg.action === 'sell' ? 'Sell' : 'Buy';
    const right = leg.right === 'put' ? 'P' : leg.right === 'call' ? 'C' : '';
    const strike = leg.strike ? fmtRef(leg.strike) : '--';
    return `${action} ${strike}${right}`;
  }).join(' / ');
}

function payoffSummary(payoff) {
  const breakeven = Array.isArray(payoff?.breakeven_points) && payoff.breakeven_points.length ? fmtRef(payoff.breakeven_points[0]) : '--';
  const ratio = Number(payoff?.risk_reward_ratio);
  const ratioText = Number.isFinite(ratio) ? ratio.toFixed(2) : '--';
  const definedRisk = payoff?.defined_risk_estimate || {};
  const mode = definedRisk.mode && definedRisk.mode !== 'reference_only' ? definedRisk.mode : payoff?.valuation_mode || 'reference';
  const maxLoss = Number(definedRisk.max_loss_per_contract);
  const maxProfit = Number(definedRisk.max_profit_per_contract);
  const riskText = Number.isFinite(maxLoss) ? ` · MaxL $${maxLoss.toFixed(0)}` : '';
  const profitText = Number.isFinite(maxProfit) ? ` · MaxP $${maxProfit.toFixed(0)}` : '';
  return `${t('watchlist2.refBE')} ${breakeven} · RR ${ratioText} · ${mode}${riskText}${profitText}`;
}

function payoffZoneClass(zone) {
  if (zone === 'profit') return 'positive';
  if (zone === 'loss') return 'negative';
  return '';
}

function PayoffCurve({ scenarios }) {
  const rows = Array.isArray(scenarios) ? scenarios.filter((item) => Number.isFinite(Number(item.underlying)) && Number.isFinite(Number(item.pnl_per_contract))) : [];
  if (rows.length < 2) return null;
  const width = 320;
  const height = 120;
  const padding = 14;
  const minX = Math.min(...rows.map((item) => Number(item.underlying)));
  const maxX = Math.max(...rows.map((item) => Number(item.underlying)));
  const minY = Math.min(...rows.map((item) => Number(item.pnl_per_contract)), 0);
  const maxY = Math.max(...rows.map((item) => Number(item.pnl_per_contract)), 0);
  const xScale = (value) => padding + ((Number(value) - minX) / Math.max(maxX - minX, 1)) * (width - padding * 2);
  const yScale = (value) => height - padding - ((Number(value) - minY) / Math.max(maxY - minY, 1)) * (height - padding * 2);
  const path = rows.map((item, index) => `${index === 0 ? 'M' : 'L'} ${xScale(item.underlying).toFixed(1)} ${yScale(item.pnl_per_contract).toFixed(1)}`).join(' ');
  const zeroY = yScale(0);
  return (
    <div className="payoff-curve">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={t('watchlist2.payoffCurveAria')}>
        <line x1={padding} x2={width - padding} y1={zeroY} y2={zeroY} />
        <path d={path} />
        {rows.map((item) => (
          <circle key={`${item.label}-${item.underlying}`} cx={xScale(item.underlying)} cy={yScale(item.pnl_per_contract)} r="2.5" className={item.zone === 'profit' ? 'positive' : item.zone === 'loss' ? 'negative' : ''} />
        ))}
      </svg>
      <small className="muted">{t('watchlist2.payoffLeft')} {fmtRef(minX)} · {t('watchlist2.payoffMid')} $0 · {t('watchlist2.payoffRight')} {fmtRef(maxX)}</small>
    </div>
  );
}

function eventPlanSummary(event) {
  const plan = event?.payload?.weekend_plan || event?.payload?.next_session_plan;
  const checklist = Array.isArray(plan?.checklist) ? plan.checklist : [];
  if (!checklist.length) return '';
  return checklist.slice(0, 2).join('；');
}

function gexSummary(gex) {
  if (!gex || Object.keys(gex).length === 0) return 'GEX unknown';
  const wall = gex.nearest_wall ? ` · ${gex.nearest_wall}` : '';
  const distance = Number(gex.nearest_wall_distance_pct);
  const distanceText = Number.isFinite(distance) ? ` ${distance.toFixed(1)}%` : '';
  const flip = Number(gex.gamma_flip);
  const flipText = Number.isFinite(flip) && flip > 0 ? ` · flip ${flip.toFixed(2)}` : '';
  return `${gex.regime || 'unknown'}${wall}${distanceText}${flipText}`;
}

function dataQualitySummary(snapshot) {
  const quality = snapshot?.data_quality || {};
  const freshness = snapshot?.freshness_status || snapshot?.data_status || quality.status || 'unknown';
  const source = snapshot?.source || snapshot?.pricing_source || quality.source || '--';
  const timestamp = snapshot?.data_timestamp || quality.data_timestamp;
  const stale = freshness === 'stale' || quality.stale;
  const unavailable = freshness === 'data_unavailable' || quality.status === 'data_unavailable' || snapshot?.error;
  return {
    label: unavailable ? t('watchlist2.dataUnavailable') : stale ? t('watchlist2.dataStale') : freshness === 'fresh' ? t('watchlist2.dataFresh') : freshness,
    tone: unavailable ? 'negative' : stale ? 'warning' : freshness === 'fresh' ? 'positive' : '',
    source,
    timestamp,
    explanation: quality.explanation || snapshot?.quote_warning || snapshot?.error || '',
  };
}

function priorityTone(priority) {
  const score = Number(priority?.score);
  if (Number.isFinite(score) && score >= 88) return 'negative';
  if (Number.isFinite(score) && score >= 70) return 'warning';
  return '';
}

function latestPlan(detail, latestEvent) {
  const events = Array.isArray(detail?.events) ? detail.events : [];
  const event = events.find((item) => item?.payload?.weekend_plan || item?.payload?.next_session_plan) || latestEvent;
  return event?.payload?.weekend_plan || event?.payload?.next_session_plan || null;
}

function eventLabel(eventType) {
  return {
    created: t('watchlist2.event.created'),
    updated: t('watchlist2.event.updated'),
    review: t('watchlist2.event.review'),
    gex_change: 'GEX',
    eod_review: t('watchlist2.event.eod_review'),
    weekend_plan: t('watchlist2.event.weekend_plan'),
    take_profit: t('watchlist2.event.take_profit'),
    stop_loss: t('watchlist2.event.stop_loss'),
    invalidated: t('watchlist2.event.invalidated'),
    expired: t('watchlist2.event.expired'),
  }[eventType] || eventType || t('watchlist2.event.default');
}

export function OpportunityCard({
  item,
  detail = null,
  expanded = false,
  busy = false,
  detailBusy = false,
  onMarkWatching,
  onMarkActive,
  onAdjustRiskPlan,
  onPause,
  onResume,
  onReview,
  onArchive,
  onToggleDetail,
  onOpenDetailPage,
  onOpenDetailModal,
}) {
  const entry = item.entry_reference || {};
  const risk = item.risk_plan || {};
  const legs = Array.isArray(item.legs) ? item.legs : [];
  const payoff = item.payoff || {};
  const payoffScenarios = Array.isArray(payoff.scenario_table) ? payoff.scenario_table : [];
  const validation = item.validation || {};
  const latestEvent = item.latest_event || null;
  const detailEvents = Array.isArray(detail?.events) ? detail.events : [];
  const plan = latestPlan(detail, latestEvent);
  const dataQuality = dataQualitySummary(item.trigger_snapshot || {});
  const latestQuality = dataQualitySummary(latestEvent?.payload?.quote_snapshot || item.trigger_snapshot || {});
  const priority = item.action_priority || {};

  return (
    <article className="run-card">
      <div className="run-card-head">
        <strong><Star size={15} /> {item.symbol}</strong>
        <span>{statusLabel(item)}</span>
      </div>
      <p>{item.title} · GEX {item.gex_snapshot?.regime || 'unknown'}</p>
      <div className="mini-list">
        <span>{t('watchlist2.stage')} {lifecycleLabel(item)} · {item.lifecycle_step || 1}/4</span>
        <span className={priorityTone(priority)}>{t('watchlist2.priority')} {priority.label || '--'} · {priority.score ?? '--'}</span>
        {priority.followup_due && <span className="warning">{t('watchlist2.reviewDue')}</span>}
        <span>{directionLabels[item.direction] || item.direction || t('watchlist2.direction.unknown')}</span>
        <span>{structureLabels[item.strategy_structure] || item.strategy_structure || t('watchlist2.structurePending')}</span>
        <span>{t('watchlist2.underlyingRef')} {fmtRef(entry.underlying_reference)}</span>
        <span>{entry.entry_side || 'reference'} {fmtRef(entry.entry_reference)}</span>
        <span>{item.followup_enabled ? t('watchlist2.trackingOn') : t('watchlist2.trackingPaused')} · {item.followup_alert_count || 0}/{item.max_followup_alerts || 0}</span>
        <span>{t('watchlist2.lastReview')} {item.last_checked_at ? formatTime(item.last_checked_at) : '--'}</span>
      </div>
      {Array.isArray(priority.reasons) && priority.reasons.length > 0 && (
        <div className="mini-list">
          {priority.reasons.map((reason) => <span key={reason}>{t('watchlist2.focus')}{reason}</span>)}
        </div>
      )}
      <div className="mini-list">
        <span className={dataQuality.tone}>{t('watchlist2.initialData')}{dataQuality.label}</span>
        <span>{t('watchlist2.source')} {dataQuality.source}</span>
        {dataQuality.timestamp && <span>{formatTime(dataQuality.timestamp)}</span>}
        {dataQuality.explanation && <span>{dataQuality.explanation}</span>}
      </div>
      <div className="mini-list">
        <span>{legs.length} {t('watchlist2.legs')}</span>
        <span>{legSummary(legs)}</span>
        <span>{payoffSummary(payoff)}</span>
        {validation.status && <span>{validation.status === 'complete' ? t('watchlist2.structureComplete') : t('watchlist2.structureReference')}</span>}
        {Array.isArray(validation.warnings) && validation.warnings.length > 0 && <span>{t('watchlist2.validationHint')} {validation.warnings.slice(0, 2).join(', ')}</span>}
      </div>
      <p>{opportunityRiskSummary(risk)}</p>
      {item.next_action && <p>{t('watchlist2.nextStep')}{item.next_action}</p>}
      {latestEvent && (
        <p>
          {t('watchlist2.latestTracking')}{latestEvent.event_type} · {latestEvent.body || latestEvent.title}
        </p>
      )}
      {eventPlanSummary(latestEvent) && <p>{t('watchlist2.plan')}{eventPlanSummary(latestEvent)}</p>}
      <div className="opportunity-actions">
        <button className="ghost compact" type="button" disabled={busy} onClick={() => onMarkWatching(item.id)}>
          <BookmarkPlus size={14} /> {t('watchlist2.imWatching')}
        </button>
        <button className="ghost compact" type="button" disabled={busy} onClick={() => onMarkActive(item.id)}>
          <BookmarkCheck size={14} /> {t('watchlist2.trackClosely')}
        </button>
        <button className="ghost compact" type="button" disabled={busy} onClick={() => onPause(item.id)}>
          <PauseCircle size={14} /> {t('watchlist2.pauseAlerts')}
        </button>
        <button className="ghost compact" type="button" disabled={busy} onClick={() => onResume(item.id)}>
          <Play size={14} /> {t('watchlist2.resumeAlerts')}
        </button>
        <button className="ghost compact" type="button" disabled={busy} onClick={() => onReview(item)}>
          <RefreshCw size={14} /> {t('watchlist2.generateReview')}
        </button>
        <button className="ghost compact" type="button" disabled={busy} onClick={() => onAdjustRiskPlan?.(item)}>
          <SlidersHorizontal size={14} /> {t('watchlist2.adjustRiskPlan')}
        </button>
        <button className="ghost compact" type="button" disabled={detailBusy} onClick={() => onToggleDetail?.(item)}>
          {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />} {detailBusy ? t('watchlist2.loadingShort') : expanded ? t('watchlist2.collapseDetail') : t('watchlist2.viewDetail')}
        </button>
        <button className="ghost compact" type="button" disabled={detailBusy} onClick={() => onOpenDetailModal?.(item)}>
          <Maximize2 size={14} /> {t('watchlist2.modal')}
        </button>
        <button className="ghost compact" type="button" disabled={detailBusy} onClick={() => onOpenDetailPage?.(item)}>
          <Maximize2 size={14} /> {t('watchlist2.standalonePage')}
        </button>
        <button className="ghost compact" type="button" disabled={busy} onClick={() => onArchive(item.id)}>
          <Archive size={14} /> {t('watchlist2.stopTracking')}
        </button>
      </div>
      {expanded && (
        <div className="opportunity-detail opportunity-detail-page">
          <div className="detail-block">
            <strong>{t('watchlist2.initialThesis')}</strong>
            <p>{item.thesis || item.title}</p>
            <div className="mini-list">
              <span>{t('watchlist2.statusLabel')} {item.status}</span>
              <span>{t('watchlist2.stage')} {lifecycleLabel(item)} · {statusLabel(item)}</span>
              <span>{t('watchlist2.directionLabel')} {directionLabels[item.direction] || item.direction || t('watchlist2.direction.unknown')}</span>
              <span>{t('watchlist2.structureLabel')} {structureLabels[item.strategy_structure] || item.strategy_structure || t('watchlist2.pending')}</span>
              <span>{t('watchlist2.refEntry')} {entry.entry_side || 'reference'} {fmtRef(entry.entry_reference)}</span>
              {item.next_action && <span>{t('watchlist2.nextStepLabel')} {item.next_action}</span>}
            </div>
          </div>
          <div className="detail-block">
            <strong>{t('watchlist2.gexChange')}</strong>
            <div className="mini-list">
              <span>{t('watchlist2.initial')} {gexSummary(item.gex_snapshot)}</span>
              {latestEvent?.payload?.gex_current && <span>{t('watchlist2.current')} {gexSummary(latestEvent.payload.gex_current)}</span>}
              {Array.isArray(latestEvent?.payload?.gex_changes) && latestEvent.payload.gex_changes.length > 0 && (
                <span>{t('watchlist2.change')} {latestEvent.payload.gex_changes.map((change) => `${change.type}: ${change.from} -> ${change.to}`).join(' / ')}</span>
              )}
              {(!latestEvent?.payload?.gex_changes || latestEvent.payload.gex_changes.length === 0) && <span>{t('watchlist2.noGexChange')}</span>}
            </div>
          </div>
          <div className="detail-block">
            <strong>{t('watchlist2.tpSlZone')}</strong>
            <div className="mini-list">
              {(risk.take_profit?.levels || []).map((level) => <span key={`${level.label}-${level.underlying_reference}`}>{level.label || 'TP'} {fmtRef(level.underlying_reference)}</span>)}
              <span>{t('watchlist2.stopLoss')} {fmtRef(risk.stop_loss?.underlying_reference)}</span>
              <span>{t('watchlist2.latestExit')} {risk.latest_exit || '--'}</span>
            </div>
          </div>
          <div className="detail-block">
            <strong>{t('watchlist2.dataQualityAndSource')}</strong>
            <div className="mini-list">
              <span className={latestQuality.tone}>{t('watchlist2.current')} {latestQuality.label}</span>
              <span>{t('watchlist2.source')} {latestQuality.source}</span>
              {latestQuality.timestamp && <span>{formatTime(latestQuality.timestamp)}</span>}
              {latestQuality.explanation && <span>{latestQuality.explanation}</span>}
              <span>GEX {item.gex_snapshot?.available || item.gex_snapshot?.regime !== 'unknown' ? t('watchlist2.available') : 'unknown'}</span>
            </div>
          </div>
          <div className="mini-table">
            {legs.map((leg, index) => (
              <span key={`${leg.role || 'leg'}-${index}`}>
                {leg.action || '--'} · {leg.asset_type || '--'} · {leg.right || '--'} · {fmtRef(leg.strike)} · {leg.expiration || '--'} · x{leg.quantity_ratio || 1}
              </span>
            ))}
          </div>
          {payoff.defined_risk_estimate && (
            <div className="mini-list">
              <span>{t('watchlist2.estimateMode')} {payoff.defined_risk_estimate.mode || '--'}</span>
              <span>{t('watchlist2.width')} {fmtRef(payoff.defined_risk_estimate.width)}</span>
              <span>{t('watchlist2.maxProfit')} ${fmtRef(payoff.defined_risk_estimate.max_profit_per_contract)}</span>
              <span>{t('watchlist2.maxLoss')} ${fmtRef(payoff.defined_risk_estimate.max_loss_per_contract)}</span>
              {payoff.defined_risk_estimate.valuation_note && <span>{payoff.defined_risk_estimate.valuation_note}</span>}
            </div>
          )}
          {payoffScenarios.length > 0 && (
            <div className="detail-block">
              <strong>{t('watchlist2.expiryPayoffScenarios')}</strong>
              <PayoffCurve scenarios={payoffScenarios} />
              <div className="payoff-scenario-grid">
                <span>{t('watchlist2.scenario')}</span>
                <span>{t('watchlist2.underlying')}</span>
                <span>{t('watchlist2.intrinsicValue')}</span>
                <span>{t('watchlist2.estimatedPL')}</span>
                {payoffScenarios.slice(0, 9).map((scenario) => (
                  <React.Fragment key={`${scenario.label}-${scenario.underlying}`}>
                    <span>{scenario.label}</span>
                    <span>{fmtRef(scenario.underlying)}</span>
                    <span>${fmtRef(scenario.intrinsic_value)}</span>
                    <span className={payoffZoneClass(scenario.zone)}>${fmtRef(scenario.pnl_per_contract)}</span>
                  </React.Fragment>
                ))}
              </div>
              {payoffScenarios.find((scenario) => scenario.note) && (
                <small className="muted">{payoffScenarios.find((scenario) => scenario.note)?.note}</small>
              )}
            </div>
          )}
          {plan && (
            <div className="detail-block">
              <strong>{plan.mode === 'weekend' ? t('watchlist2.weekendPlan') : t('watchlist2.nextSessionPlan')}</strong>
              {plan.priority && <small className="muted">{t('watchlist2.priorityColon')}{plan.priority}</small>}
              <ul>
                {(plan.checklist || []).slice(0, 5).map((text) => <li key={text}>{text}</li>)}
              </ul>
              {Array.isArray(plan.key_levels) && plan.key_levels.length > 0 && (
                <div className="mini-list">
                  {plan.key_levels.slice(0, 6).map((level) => (
                    <span key={`${level.label}-${level.value}`}>{level.label} {fmtRef(level.value)}</span>
                  ))}
                </div>
              )}
              {Array.isArray(plan.suggested_triggers) && plan.suggested_triggers.length > 0 && (
                <div className="mini-table">
                  {plan.suggested_triggers.slice(0, 4).map((trigger) => (
                    <span key={`${trigger.label}-${trigger.field || trigger.value}`}>{trigger.label} · {trigger.operator} {trigger.value}</span>
                  ))}
                </div>
              )}
            </div>
          )}
          {detailEvents.length > 0 && (
            <div className="detail-block">
              <strong>{t('watchlist2.eventTimeline')}</strong>
              <ol>
                {detailEvents.slice(0, 6).map((event) => (
                  <li key={event.id}>
                    {eventLabel(event.event_type)} · {formatTime(event.created_at)} · {event.body || event.title}
                  </li>
                ))}
              </ol>
            </div>
          )}
        </div>
      )}
      <CopyableId value={item.id} label={t('watchlist2.opportunityInstance')} compact />
    </article>
  );
}
