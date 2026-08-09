import React, { useEffect, useMemo, useState } from 'react';
import { Archive, BellPlus, BookmarkCheck, BookmarkPlus, PauseCircle, Play, RefreshCw, Save, X } from 'lucide-react';
import { t } from '../../i18n/index.js';
import { formatTime } from '../../utils/display.js';

function fmt(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(2) : '--';
}

function eventLabel(type) {
  return {
    created: t('watchlist2.event.created'),
    updated: t('watchlist2.event.updated'),
    review: t('watchlist2.event.review'),
    gex_change: 'GEX',
    eod_review: t('watchlist2.event.eod_review_full'),
    weekend_plan: t('watchlist2.event.weekend_plan_full'),
    take_profit: t('watchlist2.event.take_profit'),
    stop_loss: t('watchlist2.event.stop_loss'),
    invalidated: t('watchlist2.event.invalidated'),
    expired: t('watchlist2.event.expired'),
    trigger_matched: 'Trigger',
  }[type] || type || t('watchlist2.event.default');
}

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
  return lifecycleLabels[item?.lifecycle_phase] || t('watchlist2.lifecycle.watching');
}

function priorityTone(priority) {
  const score = Number(priority?.score);
  if (Number.isFinite(score) && score >= 88) return 'negative';
  if (Number.isFinite(score) && score >= 70) return 'warning';
  return '';
}

function gexSummary(gex) {
  if (!gex || Object.keys(gex).length === 0) return 'unknown';
  const distance = Number(gex.nearest_wall_distance_pct);
  return `${gex.regime || 'unknown'} · ${gex.nearest_wall || '--'}${Number.isFinite(distance) ? ` · ${distance.toFixed(1)}%` : ''}`;
}

function latestValue(event, key) {
  return event?.payload?.[key] ?? null;
}

function firstTakeProfit(opportunity) {
  const levels = opportunity?.risk_plan?.take_profit?.levels;
  return Array.isArray(levels) ? levels.find((item) => Number.isFinite(Number(item.underlying_reference))) : null;
}

function triggerPayloadForOpportunity(opportunity, kind) {
  const symbol = String(opportunity?.symbol || '').toUpperCase();
  const notificationIds = Array.isArray(opportunity?.notification_channel_ids) ? opportunity.notification_channel_ids : [];
  const stop = Number(opportunity?.risk_plan?.stop_loss?.underlying_reference);
  const tp1 = firstTakeProfit(opportunity);
  const tpValue = Number(tp1?.underlying_reference);
  const entry = Number(opportunity?.entry_reference?.underlying_reference || opportunity?.trigger_snapshot?.last);
  const shared = {
    symbol,
    opportunity_id: opportunity?.id,
    notification_channel_ids: notificationIds,
    check_interval_seconds: 300,
    cooldown_seconds: 1800,
    max_trigger_count: 3,
    market_policy: 'regular_only',
  };
  if (kind === 'tp1' && Number.isFinite(tpValue) && tpValue > 0) {
    return {
      ...shared,
      name: `${symbol} ${t('watchlist2.trig.tp1')}`,
      condition: { type: 'underlying_price', symbol, operator: '>=', value: tpValue, label: 'TP1 reference' },
      max_trigger_count: 2,
    };
  }
  if (kind === 'stop_loss' && Number.isFinite(stop) && stop > 0) {
    return {
      ...shared,
      name: `${symbol} ${t('watchlist2.trig.stop')}`,
      condition: { type: 'underlying_price', symbol, operator: '<=', value: stop, label: 'Stop reference' },
      check_interval_seconds: 180,
      max_trigger_count: 2,
    };
  }
  if (kind === 'option_spread') {
    const spread = Number(opportunity?.trigger_snapshot?.bid_ask_spread_pct);
    const contractSymbol = opportunity?.contract_symbol || opportunity?.trigger_snapshot?.contract_symbol || '';
    if (!contractSymbol) return null;
    return {
      ...shared,
      name: `${symbol} ${t('watchlist2.trig.optionSpread')}`,
      condition: {
        type: 'option_quote',
        symbol,
        contract_symbol: contractSymbol,
        field: 'bid_ask_spread_pct',
        operator: '>=',
        value: Number.isFinite(spread) && spread > 0 ? Math.max(spread * 1.5, 15) : 15,
        label: 'Bid/Ask Spread %',
      },
      check_interval_seconds: 180,
    };
  }
  if (['option_iv_crush', 'option_theta_worse', 'option_delta_decay'].includes(kind)) {
    const contractSymbol = opportunity?.contract_symbol || opportunity?.trigger_snapshot?.contract_symbol || '';
    if (!contractSymbol) return null;
    const delta = Number(opportunity?.trigger_snapshot?.delta);
    const theta = Number(opportunity?.trigger_snapshot?.theta);
    const iv = Number(opportunity?.trigger_snapshot?.iv ?? opportunity?.trigger_snapshot?.implied_volatility);
    if (kind === 'option_iv_crush') {
      return {
        ...shared,
        name: `${symbol} ${t('watchlist2.trig.ivCrush')}`,
        condition: {
          type: 'option_quote',
          symbol,
          contract_symbol: contractSymbol,
          field: 'iv',
          operator: '<=',
          value: Number.isFinite(iv) && iv > 0 ? Number((iv * 0.8).toFixed(4)) : 0.35,
          label: 'IV compression',
        },
        check_interval_seconds: 180,
      };
    }
    if (kind === 'option_theta_worse') {
      return {
        ...shared,
        name: `${symbol} ${t('watchlist2.trig.thetaWorse')}`,
        condition: {
          type: 'option_quote',
          symbol,
          contract_symbol: contractSymbol,
          field: 'theta',
          operator: '<=',
          value: Number.isFinite(theta) && theta < 0 ? Number((theta * 1.4).toFixed(4)) : -0.08,
          label: 'Theta deterioration',
        },
        check_interval_seconds: 180,
      };
    }
    return {
      ...shared,
      name: `${symbol} ${t('watchlist2.trig.deltaDecay')}`,
      condition: {
        type: 'option_quote',
        symbol,
        contract_symbol: contractSymbol,
        field: 'delta',
        operator: Number.isFinite(delta) && delta < 0 ? '>=' : '<=',
        value: Number.isFinite(delta) ? Number((delta * 0.6).toFixed(4)) : 0.25,
        label: 'Delta exposure decay',
      },
      check_interval_seconds: 180,
    };
  }
  if (kind === 'rv_rank_high') {
    const rvRank = Number(opportunity?.trigger_snapshot?.rv_rank);
    return {
      ...shared,
      name: `${symbol} ${t('watchlist2.trig.rvRankHigh')}`,
      condition: { type: 'technical_indicator', symbol, field: 'rv_rank', operator: '>=', value: Number.isFinite(rvRank) ? Math.max(rvRank, 0.75) : 0.75, label: 'RV Rank' },
      check_interval_seconds: 300,
    };
  }
  if (kind === 'volume_profile_poc') {
    const poc = Number(opportunity?.trigger_snapshot?.volume_profile_poc);
    return {
      ...shared,
      name: `${symbol} ${t('watchlist2.trig.poc')}`,
      condition: { type: 'technical_indicator', symbol, field: 'last', operator: '<=', value: Number.isFinite(poc) && poc > 0 ? poc : entry, label: 'Volume Profile POC' },
      check_interval_seconds: 180,
    };
  }
  const wallDistance = Number(opportunity?.gex_snapshot?.nearest_wall_distance_pct);
  const nearestWall = opportunity?.gex_snapshot?.nearest_wall;
  const wallPrice = Number(nearestWall === 'put_wall' ? opportunity?.gex_snapshot?.put_wall : opportunity?.gex_snapshot?.call_wall);
  const gexValue = Number.isFinite(wallPrice) && wallPrice > 0 ? wallPrice : entry;
  return {
    ...shared,
    name: `${symbol} ${t('watchlist2.trig.gexWall')}`,
    condition: {
      type: 'underlying_price',
      symbol,
      operator: nearestWall === 'put_wall' ? '<=' : '>=',
      value: Number.isFinite(gexValue) && gexValue > 0 ? gexValue : 1,
      label: Number.isFinite(wallDistance) ? `GEX ${nearestWall || 'wall'} ${wallDistance.toFixed(1)}% follow-up` : 'GEX wall follow-up',
    },
  };
}

function currentValueForTrigger(opportunity, kind) {
  if (kind === 'option_spread') return opportunity?.trigger_snapshot?.bid_ask_spread_pct;
  if (kind === 'option_iv_crush') return opportunity?.trigger_snapshot?.iv ?? opportunity?.trigger_snapshot?.implied_volatility;
  if (kind === 'option_theta_worse') return opportunity?.trigger_snapshot?.theta;
  if (kind === 'option_delta_decay') return opportunity?.trigger_snapshot?.delta;
  if (kind === 'rv_rank_high') return opportunity?.trigger_snapshot?.rv_rank;
  if (kind === 'volume_profile_poc') return opportunity?.trigger_snapshot?.last ?? opportunity?.entry_reference?.underlying_reference;
  return latestValue(opportunity?.events?.[0], 'current_price') ?? opportunity?.trigger_snapshot?.last ?? opportunity?.entry_reference?.underlying_reference;
}

function conditionExpression(condition, threshold) {
  if (!condition) return '--';
  const subject = condition.type === 'option_quote'
    ? `${condition.contract_symbol || condition.symbol} ${condition.label || condition.field}`
    : `${condition.symbol} ${condition.label || 'price'}`;
  return `${subject} ${condition.operator || '>='} ${threshold}`;
}

function PayoffCurve({ scenarios = [] }) {
  const rows = scenarios.filter((item) => Number.isFinite(Number(item.underlying)) && Number.isFinite(Number(item.pnl_per_contract)));
  if (rows.length < 2) return null;
  const width = 420;
  const height = 140;
  const padding = 16;
  const minX = Math.min(...rows.map((item) => Number(item.underlying)));
  const maxX = Math.max(...rows.map((item) => Number(item.underlying)));
  const minY = Math.min(...rows.map((item) => Number(item.pnl_per_contract)), 0);
  const maxY = Math.max(...rows.map((item) => Number(item.pnl_per_contract)), 0);
  const x = (value) => padding + ((Number(value) - minX) / Math.max(maxX - minX, 1)) * (width - padding * 2);
  const y = (value) => height - padding - ((Number(value) - minY) / Math.max(maxY - minY, 1)) * (height - padding * 2);
  const path = rows.map((item, index) => `${index ? 'L' : 'M'} ${x(item.underlying).toFixed(1)} ${y(item.pnl_per_contract).toFixed(1)}`).join(' ');
  return (
    <div className="payoff-curve">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={t('watchlist2.opportunityPayoffAria')}>
        <line x1={padding} x2={width - padding} y1={y(0)} y2={y(0)} />
        <path d={path} />
        {rows.map((item) => <circle key={`${item.label}-${item.underlying}`} cx={x(item.underlying)} cy={y(item.pnl_per_contract)} r="2.7" className={item.zone === 'profit' ? 'positive' : item.zone === 'loss' ? 'negative' : ''} />)}
      </svg>
      <small className="muted">{t('watchlist2.payoffLeft')} {fmt(minX)} · {t('watchlist2.payoffMid')} $0 · {t('watchlist2.payoffRight')} {fmt(maxX)}</small>
    </div>
  );
}

function RiskPlanEditor({ opportunity, busy, onSave }) {
  const risk = opportunity?.risk_plan || {};
  const tpLevels = Array.isArray(risk.take_profit?.levels) ? risk.take_profit.levels : [];
  const [draft, setDraft] = useState({
    tp1: tpLevels[0]?.underlying_reference ?? '',
    tp2: tpLevels[1]?.underlying_reference ?? '',
    stop: risk.stop_loss?.underlying_reference ?? '',
    latest_exit: risk.latest_exit || '',
    max_alerts: opportunity?.max_followup_alerts ?? 6,
    interval: opportunity?.followup_interval_seconds ?? 300,
  });

  useEffect(() => {
    setDraft({
      tp1: tpLevels[0]?.underlying_reference ?? '',
      tp2: tpLevels[1]?.underlying_reference ?? '',
      stop: risk.stop_loss?.underlying_reference ?? '',
      latest_exit: risk.latest_exit || '',
      max_alerts: opportunity?.max_followup_alerts ?? 6,
      interval: opportunity?.followup_interval_seconds ?? 300,
    });
  }, [opportunity?.id]);

  function submit(event) {
    event.preventDefault();
    const levels = [
      { ...(tpLevels[0] || {}), label: tpLevels[0]?.label || 'TP1', underlying_reference: Number(draft.tp1) },
      { ...(tpLevels[1] || {}), label: tpLevels[1]?.label || 'TP2', underlying_reference: Number(draft.tp2 || draft.tp1) },
    ].filter((item) => Number.isFinite(item.underlying_reference) && item.underlying_reference > 0);
    const stop = Number(draft.stop);
    onSave?.(opportunity, {
      ...risk,
      latest_exit: draft.latest_exit || risk.latest_exit,
      take_profit: { ...(risk.take_profit || {}), levels },
      stop_loss: {
        ...(risk.stop_loss || {}),
        type: risk.stop_loss?.type || 'underlying_reference',
        underlying_reference: Number.isFinite(stop) && stop > 0 ? stop : risk.stop_loss?.underlying_reference,
      },
    });
  }

  return (
    <form className="risk-plan-editor" onSubmit={submit}>
      <div className="two">
        <label>TP1<input type="number" step="0.01" value={draft.tp1} onChange={(event) => setDraft((current) => ({ ...current, tp1: event.target.value }))} /></label>
        <label>TP2<input type="number" step="0.01" value={draft.tp2} onChange={(event) => setDraft((current) => ({ ...current, tp2: event.target.value }))} /></label>
      </div>
      <div className="two">
        <label>{t('watchlist2.stopReference')}<input type="number" step="0.01" value={draft.stop} onChange={(event) => setDraft((current) => ({ ...current, stop: event.target.value }))} /></label>
        <label>{t('watchlist2.latestExit')}<input value={draft.latest_exit} onChange={(event) => setDraft((current) => ({ ...current, latest_exit: event.target.value }))} /></label>
      </div>
      <button className="primary compact" type="submit" disabled={busy}>
        <Save size={14} /> {t('watchlist2.saveRiskPlan')}
      </button>
    </form>
  );
}

function TriggerCreationPreview({ opportunity, notificationChannels = [], busy, onCreateTrigger }) {
  const [kind, setKind] = useState('tp1');
  const [name, setName] = useState('');
  const [threshold, setThreshold] = useState('');
  const [operator, setOperator] = useState('>=');
  const [marketPolicy, setMarketPolicy] = useState('regular_only');
  const [channelIds, setChannelIds] = useState([]);
  const basePayload = useMemo(() => triggerPayloadForOpportunity(opportunity, kind), [opportunity, kind]);
  const currentValue = currentValueForTrigger(opportunity, kind);

  useEffect(() => {
    if (!basePayload) return;
    setName(basePayload.name || '');
    setThreshold(basePayload.condition?.value ?? '');
    setOperator(basePayload.condition?.operator || '>=');
    setMarketPolicy(basePayload.market_policy || 'regular_only');
    setChannelIds(Array.isArray(basePayload.notification_channel_ids) ? basePayload.notification_channel_ids : []);
  }, [opportunity?.id, kind, basePayload?.name, basePayload?.condition?.value]);

  function toggleChannel(channelId) {
    setChannelIds((current) => (
      current.includes(channelId)
        ? current.filter((item) => item !== channelId)
        : [...current, channelId]
    ));
  }

  function submit(event) {
    event.preventDefault();
    if (!basePayload) return;
    onCreateTrigger?.({
      ...basePayload,
      name,
      market_policy: marketPolicy,
      notification_channel_ids: channelIds,
      condition: {
        ...basePayload.condition,
        operator,
        value: Number(threshold),
      },
    });
  }

  const selectedChannelNames = notificationChannels
    .filter((channel) => channelIds.includes(channel.id))
    .map((channel) => channel.label || channel.email || channel.type);

  return (
    <form className="trigger-preview-builder" onSubmit={submit}>
      <div className="trigger-preview-head">
        <div>
          <strong>{t('watchlist2.createTriggerFromOpp')}</strong>
          <small>{t('watchlist2.createTriggerHint')}</small>
        </div>
        <select value={kind} onChange={(event) => setKind(event.target.value)}>
          <option value="tp1">{t('watchlist2.trig.tp1')}</option>
          <option value="stop_loss">{t('watchlist2.trig.stop')}</option>
          <option value="gex_regime">{t('watchlist2.trig.gexWall')}</option>
          <option value="option_spread">{t('watchlist2.trig.optionSpreadShort')}</option>
          <option value="option_iv_crush">{t('watchlist2.trig.ivCrush')}</option>
          <option value="option_theta_worse">{t('watchlist2.trig.thetaWorse')}</option>
          <option value="option_delta_decay">{t('watchlist2.trig.deltaDecayShort')}</option>
          <option value="rv_rank_high">{t('watchlist2.trig.rvRankHigh')}</option>
          <option value="volume_profile_poc">{t('watchlist2.trig.pocShort')}</option>
        </select>
      </div>
      {!basePayload && <div className="warning-banner">{t('watchlist2.cannotCreateTriggerType')}</div>}
      {basePayload && (
        <>
          <div className="trigger-preview-grid">
            <label>{t('watchlist2.name')}<input value={name} onChange={(event) => setName(event.target.value)} /></label>
            <label>{t('watchlist2.operator')}
              <select value={operator} onChange={(event) => setOperator(event.target.value)}>
                <option value=">=">&gt;=</option>
                <option value="<=">&lt;=</option>
                <option value=">">&gt;</option>
                <option value="<">&lt;</option>
                <option value="==">==</option>
              </select>
            </label>
            <label>{t('watchlist2.threshold')}<input type="number" step="0.01" value={threshold} onChange={(event) => setThreshold(event.target.value)} /></label>
            <label>{t('watchlist2.marketSession')}
              <select value={marketPolicy} onChange={(event) => setMarketPolicy(event.target.value)}>
                <option value="regular_only">{t('watchlist2.session.regular')}</option>
                <option value="include_extended">{t('watchlist2.session.extended')}</option>
                <option value="next_open">{t('watchlist2.session.nextOpen')}</option>
                <option value="eod_review">{t('watchlist2.session.eodReview')}</option>
                <option value="always_calendar">{t('watchlist2.session.alwaysCalendar')}</option>
              </select>
            </label>
          </div>
          <div className="trigger-preview-summary">
            <span>{t('watchlist2.condition')}{conditionExpression({ ...basePayload.condition, operator }, threshold)}</span>
            <span>{t('watchlist2.currentValue')}{fmt(currentValue)}</span>
            <span>{t('watchlist2.notify')}{selectedChannelNames.length ? selectedChannelNames.join(' / ') : channelIds.length ? t('watchlist2.nChannels').replace('{n}', channelIds.length) : t('watchlist2.defaultEventQueue')}</span>
            <span>{t('watchlist2.cooldown')}{Math.round((basePayload.cooldown_seconds || 0) / 60)} {t('watchlist2.minutes')} · {t('watchlist2.atMost')} {basePayload.max_trigger_count || 3} {t('watchlist2.times')}</span>
          </div>
          {notificationChannels.length > 0 && (
            <div className="trigger-channel-picker">
              {notificationChannels.map((channel) => (
                <label key={channel.id} className={channelIds.includes(channel.id) ? 'active' : ''}>
                  <input type="checkbox" checked={channelIds.includes(channel.id)} onChange={() => toggleChannel(channel.id)} />
                  <span>{channel.label || channel.type}</span>
                  <small>{channel.type}{channel.enabled === false ? ' · disabled' : ''}</small>
                </label>
              ))}
            </div>
          )}
          <button className="primary compact" type="submit" disabled={busy || !Number.isFinite(Number(threshold))}>
            <BellPlus size={14} /> {t('watchlist2.createAndBind')}
          </button>
        </>
      )}
    </form>
  );
}

export function OpportunityDetailWorkspace({
  opportunity,
  detail,
  busy = false,
  onClose,
  onBack,
  onMarkWatching,
  onMarkActive,
  onSaveRiskPlan,
  onPause,
  onResume,
  onReview,
  onArchive,
  onCreateTrigger,
  notificationChannels = [],
  standalone = false,
}) {
  if (!opportunity) return null;
  const item = detail || opportunity;
  const events = Array.isArray(item.events) ? item.events : [];
  const notifications = Array.isArray(item.notification_events) ? item.notification_events : [];
  const triggers = Array.isArray(item.linked_triggers) ? item.linked_triggers : [];
  const payoff = item.payoff || {};
  const scenarios = Array.isArray(payoff.scenario_table) ? payoff.scenario_table : [];
  const latestEvent = events[0] || item.latest_event;
  const latestGex = latestEvent?.payload?.gex_current || item.gex_snapshot || {};
  const priority = item.action_priority || {};
  const statusTrail = events
    .filter((event) => event?.payload?.status_after || event?.payload?.current_price || event?.payload?.gex_current)
    .slice(0, 6);

  return (
      <section className={`opportunity-modal ${standalone ? 'opportunity-workbench' : ''}`} role={standalone ? 'region' : 'dialog'} aria-modal={standalone ? undefined : 'true'}>
        <div className="opportunity-modal-head">
          <div>
            <span>{statusLabel(item)} · {lifecycleLabel(item)} · {item.symbol}</span>
            <h2>{item.title}</h2>
            <small>{item.direction || 'unknown'} · {item.strategy_structure || 'unknown'} · {t('watchlist2.created')} {formatTime(item.created_at)}</small>
          </div>
          <button className="icon-button" type="button" aria-label={standalone ? t('watchlist2.backToPrev') : t('watchlist2.closeDetail')} title={standalone ? t('watchlist2.back') : t('watchlist2.close')} onClick={standalone ? onBack : onClose}><X size={16} /></button>
        </div>

        <div className="opportunity-modal-actions">
          <button className="ghost compact" type="button" disabled={busy} onClick={() => onMarkWatching?.(item.id)}><BookmarkPlus size={14} /> {t('watchlist2.imWatching')}</button>
          <button className="ghost compact" type="button" disabled={busy} onClick={() => onMarkActive?.(item.id)}><BookmarkCheck size={14} /> {t('watchlist2.trackClosely')}</button>
          <button className="ghost compact" type="button" disabled={busy} onClick={() => onPause?.(item.id)}><PauseCircle size={14} /> {t('watchlist2.pause')}</button>
          <button className="ghost compact" type="button" disabled={busy} onClick={() => onResume?.(item.id)}><Play size={14} /> {t('watchlist2.resume')}</button>
          <button className="ghost compact" type="button" disabled={busy} onClick={() => onReview?.(item)}><RefreshCw size={14} /> {t('watchlist2.generateReview')}</button>
          <button className="ghost compact" type="button" disabled={busy} onClick={() => onArchive?.(item.id)}><Archive size={14} /> {t('watchlist2.archive')}</button>
        </div>

        <div className="opportunity-modal-grid">
          <div className="detail-block">
            <strong>{t('watchlist2.initialThesis')}</strong>
            <p>{item.thesis || item.title}</p>
            <div className="mini-list">
              <span>{t('watchlist2.entryRef')} {fmt(item.entry_reference?.underlying_reference)}</span>
              <span>{item.entry_reference?.entry_side || 'reference'} {fmt(item.entry_reference?.entry_reference)}</span>
              <span>{t('watchlist2.tracking')} {item.followup_enabled ? t('watchlist2.on') : t('watchlist2.paused')}</span>
              <span>{t('watchlist2.alerts')} {item.followup_alert_count || 0}/{item.max_followup_alerts || 0}</span>
            </div>
          </div>

          <div className="detail-block">
            <strong>{t('watchlist2.currentStatus')}</strong>
            <div className="mini-list">
              <span>{t('watchlist2.statusLabel')} {statusLabel(item)}</span>
              <span>{t('watchlist2.lifecycleLabel')} {lifecycleLabel(item)} · {item.lifecycle_step || 1}/4</span>
              <span className={priorityTone(priority)}>{t('watchlist2.livePriority')} {priority.label || '--'} · {priority.score ?? '--'}</span>
              {priority.followup_due && <span className="warning">{t('watchlist2.reviewDue')}</span>}
              {item.next_action && <span>{t('watchlist2.nextStepLabel')} {item.next_action}</span>}
              <span>{t('watchlist2.currentPrice')} {fmt(latestValue(latestEvent, 'current_price') ?? item.trigger_snapshot?.last ?? item.entry_reference?.underlying_reference)}</span>
              <span>{t('watchlist2.latestGex')} {gexSummary(latestGex)}</span>
              <span>{t('watchlist2.nextReview')} {formatTime(item.next_check_at)}</span>
              <span>{t('watchlist2.lastReviewLabel')} {formatTime(item.last_checked_at)}</span>
              <span>{t('watchlist2.lastAlert')} {formatTime(item.last_alert_at)}</span>
              {Array.isArray(priority.reasons) && priority.reasons.map((reason) => <span key={reason}>{t('watchlist2.focusShort')} {reason}</span>)}
            </div>
          </div>

          <div className="detail-block">
            <strong>{t('watchlist2.gexMarketStructure')}</strong>
            <div className="mini-list">
              <span>{t('watchlist2.initial')} {gexSummary(item.gex_snapshot)}</span>
              <span>{t('watchlist2.current')} {gexSummary(latestGex)}</span>
              {Array.isArray(latestEvent?.payload?.gex_changes) && latestEvent.payload.gex_changes.length > 0
                ? latestEvent.payload.gex_changes.map((change) => <span key={`${change.type}-${change.to}`}>{change.type}: {change.from} {'->'} {change.to}</span>)
                : <span>{t('watchlist2.noRegimeWallShift')}</span>}
            </div>
          </div>

          <div className="detail-block wide">
            <strong>{t('watchlist2.riskPlan')}</strong>
            <RiskPlanEditor opportunity={item} busy={busy} onSave={onSaveRiskPlan} />
          </div>

          {onCreateTrigger && (
            <div className="detail-block wide">
              <TriggerCreationPreview
                opportunity={item}
                notificationChannels={notificationChannels}
                busy={busy}
                onCreateTrigger={onCreateTrigger}
              />
            </div>
          )}

          <div className="detail-block">
            <strong>{t('watchlist2.legStructure')}</strong>
            <div className="mini-table">
              {(item.legs || []).map((leg, index) => (
                <span key={`${leg.role || 'leg'}-${index}`}>{leg.action || '--'} · {leg.asset_type || '--'} · {leg.right || '--'} · {fmt(leg.strike)} · {leg.expiration || '--'} · x{leg.quantity_ratio || 1}</span>
              ))}
            </div>
          </div>

          <div className="detail-block">
            <strong>{t('watchlist2.scanSource')}</strong>
            <div className="mini-list">
              <span>{t('watchlist2.instance')} {item.scan_loop_instance?.name || item.scan_loop_instance_id || '--'}</span>
              <span>Run {item.source_run?.status || item.source_id || '--'}</span>
              <span>Scan {item.scan_id || '--'}</span>
              <span>{t('watchlist2.marketSource')} {item.trigger_snapshot?.source || item.trigger_snapshot?.pricing_source || '--'}</span>
            </div>
            <div className="mini-table">
              {item.source_run && <span>{t('watchlist2.sourceRun')} {item.source_run.name || item.source_run.id || item.source_id}</span>}
              {item.source_run?.summary?.ai_scan_policy && <span>{t('watchlist2.aiPolicyShort')} {item.source_run.summary.ai_scan_policy} · Top {item.source_run.summary.ai_scan_top_n ?? '--'}</span>}
              {item.source_run?.summary?.market_clock?.date_et && <span>{t('watchlist2.marketClock')} {item.source_run.summary.market_clock.date_et} {item.source_run.summary.market_clock.time_et || ''}</span>}
              {!item.source_run && <span>{t('watchlist2.noSourceRunDetail')}</span>}
            </div>
          </div>

          {scenarios.length > 0 && (
            <div className="detail-block wide">
              <strong>{t('watchlist2.payoffChartScenarios')}</strong>
              <PayoffCurve scenarios={scenarios} />
              <div className="payoff-scenario-grid">
                <span>{t('watchlist2.scenario')}</span><span>{t('watchlist2.underlying')}</span><span>{t('watchlist2.intrinsicValue')}</span><span>{t('watchlist2.estimatedPL')}</span>
                {scenarios.slice(0, 9).map((scenario) => (
                  <React.Fragment key={`${scenario.label}-${scenario.underlying}`}>
                    <span>{scenario.label}</span>
                    <span>{fmt(scenario.underlying)}</span>
                    <span>${fmt(scenario.intrinsic_value)}</span>
                    <span className={scenario.zone === 'profit' ? 'positive' : scenario.zone === 'loss' ? 'negative' : ''}>${fmt(scenario.pnl_per_contract)}</span>
                  </React.Fragment>
                ))}
              </div>
            </div>
          )}

          <div className="detail-block">
            <strong>{t('watchlist2.linkedTriggers')}</strong>
            <div className="mini-table">
              {triggers.slice(0, 6).map((trigger) => <span key={trigger.id}>{trigger.name} · {trigger.opportunity_id === item.id ? t('watchlist2.opportunityBound') : t('watchlist2.compatLinked')} · {trigger.status} · {t('watchlist2.next')} {formatTime(trigger.next_check_at)}</span>)}
              {!triggers.length && <span>{t('watchlist2.noLinkedTriggers')}</span>}
            </div>
          </div>

          <div className="detail-block">
            <strong>{t('watchlist2.notificationEvents')}</strong>
            <div className="mini-table">
              {notifications.slice(0, 6).map((event) => <span key={event.id}>{event.status} · {event.title} · {formatTime(event.sent_at || event.created_at)}</span>)}
              {!notifications.length && <span>{t('watchlist2.noNotificationEventsShort')}</span>}
            </div>
          </div>

          <div className="detail-block wide">
            <strong>{t('watchlist2.lifecycleTimeline')}</strong>
            <ol className="opportunity-timeline">
              {events.slice(0, 12).map((event) => (
                <li key={event.id}>
                  <span>{eventLabel(event.event_type)}</span>
                  <strong>{event.body || event.title}</strong>
                  <small>{formatTime(event.created_at)}{event.payload?.status_after ? ` · ${event.payload.status_after}` : ''}</small>
                </li>
              ))}
              {!events.length && <li><span>{t('watchlist2.waiting')}</span><strong>{t('watchlist2.noLifecycleEvents')}</strong><small>--</small></li>}
            </ol>
          </div>

          <div className="detail-block wide">
            <strong>{t('watchlist2.gexStatusLog')}</strong>
            <div className="mini-table">
              {statusTrail.map((event) => (
                <span key={event.id}>
                  {eventLabel(event.event_type)} · {formatTime(event.created_at)}
                  {event.payload?.status_before || event.payload?.status_after ? ` · ${event.payload?.status_before || '--'} → ${event.payload?.status_after || '--'}` : ''}
                  {event.payload?.gex_current ? ` · ${gexSummary(event.payload.gex_current)}` : ''}
                </span>
              ))}
              {!statusTrail.length && <span>{t('watchlist2.noStatusGexLog')}</span>}
            </div>
          </div>
        </div>
      </section>
  );
}

export function OpportunityDetailModal(props) {
  if (!props.opportunity) return null;
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={props.onClose}>
      <div onMouseDown={(event) => event.stopPropagation()}>
        <OpportunityDetailWorkspace {...props} />
      </div>
    </div>
  );
}
