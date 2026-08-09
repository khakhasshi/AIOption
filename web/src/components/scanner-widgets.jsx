import React, { memo, useMemo } from 'react';
import { Metric, SectionTitle, Pair } from './common.jsx';
import { Term } from './term.jsx';
import { Skeleton } from './skeleton.jsx';
import { t } from '../i18n/index.js';
import {
  compact,
  decisionConsistencyLabel,
  decisionConsistencyTone,
  decisionGateLabel,
  decisionGateSubLabel,
  decisionGateTone,
  fmt,
  gexAlignmentLabel,
  gexRegimeLabel,
  gexWallLabel,
  linePath,
  marketDataSourceLabel,
  money,
  pct,
  previewText,
  pricingSourceLabel,
  quoteWarningLabel,
  shortContract,
  sideLabel,
  stageLabel,
  strategyBlockReasonLabel,
  strategyBreakevensLabel,
  strategyComboLabel,
  strategyDirectionLabel,
  strategyFamilyLabel,
  strategyFlagLabel,
  strategyOrderStatusLabel,
} from '../utils/display.js';

export function BlockedStrategyPanel({ items, compact = false }) {
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) return null;
  return (
    <div className="status-box danger-box blocked-strategy-panel">
      <strong>{t('scanner2.blockedStrategies')}</strong>
      <div className="blocked-strategy-grid">
        {rows.map((item, index) => (
          <article className="detail-card wide" key={`${item.tracking_id || item.symbol || 'blocked'}-${index}`}>
            <div className="detail-card-head">
              <strong>{item.label || item.strategy_type || t('scanner2.strategyCombo')} · {item.symbol || '--'}</strong>
              <span>{strategyOrderStatusLabel(item.status)} · {item.quantity ?? item.units ?? '--'} {t('scanner2.units')}</span>
            </div>
            <dl className="intent compact-dl">
              <Pair k={t('scanner2.combo')} v={strategyComboLabel(item)} />
              <Pair k={t('scanner2.orderStatus')} v={strategyOrderStatusLabel(item.status)} />
              <Pair k={t('scanner2.expectedNetDebit')} v={fmt(item.expected_debit ?? item.expected_net)} />
              <Pair k={t('scanner2.liveNetDebit')} v={fmt(item.actual_debit ?? item.actual_net)} />
              <Pair k={t('scanner2.tolerance')} v={item.tolerance_pct == null ? '--' : `${fmt(item.tolerance_pct)}%`} />
              <Pair k={t('scanner2.executionMode')} v={item.strategy_execution_mode || '--'} />
            </dl>
            <div className="mini-list blocked-reason-list">
              {(item.issues.length ? item.issues : [item.reason]).filter(Boolean).map((issue, issueIndex) => (
                <span key={`${item.tracking_id || index}-issue-${issueIndex}`}>{strategyBlockReasonLabel(issue)}</span>
              ))}
            </div>
            {!compact && (
              <div className="strategy-legs blocked-leg-list">
                {item.legs.map((leg, legIndex) => (
                  <span key={`${item.tracking_id || index}-leg-${leg.contract_symbol || legIndex}`}>
                    {strategyLegActionLabel(leg.action)} {leg.qty || 1} · {shortContract(leg.contract_symbol)}
                    {' '}· K {fmt(leg.strike)} · fresh {fmt(leg.price)} · bid {fmt(leg.bid)} / ask {fmt(leg.ask)}
                  </span>
                ))}
                {!item.legs.length && <small>{t('scanner2.noLegQuotes')}</small>}
              </div>
            )}
          </article>
        ))}
      </div>
    </div>
  );
}



export function EmptyState({ scan }) {
  if (scan && ['queued', 'running'].includes(scan.status)) {
    return (
      <div className="empty">
        <h3>{scan.status === 'queued' ? t('scanner2.scanQueued') : stageLabel(scan.stage)}</h3>
        <p>{scan.query}</p>
      </div>
    );
  }
  if (scan?.status === 'failed') {
    return (
      <div className="empty">
        <h3>{t('scanner2.scanFailed')}</h3>
        <p>{scan.error || t('scanner2.scanFailedDesc')}</p>
      </div>
    );
  }
  return (
    <div className="empty">
      <h3>{t('scanner2.awaitingFirstScan')}</h3>
      <p>{t('scanner2.awaitingFirstScanDesc')}</p>
    </div>
  );
}

export function ChartCard({ title, data, field, secondField }) {
  const path = useMemo(() => linePath(data, field, 420, 150), [data, field]);
  const secondPath = useMemo(() => linePath(data, secondField, 420, 150), [data, secondField]);
  const endPoint = useMemo(() => {
    if (!path) return null;
    const m = path.match(/L\s+([\d.]+)\s+([\d.]+)(?!.*L)/s);
    if (!m) {
      const m0 = path.match(/M\s+([\d.]+)\s+([\d.]+)/);
      return m0 ? { x: Number(m0[1]), y: Number(m0[2]) } : null;
    }
    return { x: Number(m[1]), y: Number(m[2]) };
  }, [path]);
  const gradId = `area-${field}-${secondField || 'x'}`;
  return (
    <div className="panel chart-card">
      <SectionTitle title={title} />
      <svg viewBox="0 0 420 150" preserveAspectRatio="none" role="img">
        <defs>
          <linearGradient id={gradId} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="currentColor" stopOpacity="0.32" />
            <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
          </linearGradient>
        </defs>
        <line className="grid-line" x1="0" y1="75" x2="420" y2="75" />
        <line className="grid-line" x1="0" y1="37.5" x2="420" y2="37.5" />
        <line className="grid-line" x1="0" y1="112.5" x2="420" y2="112.5" />
        {path && <path className="area" style={{ color: 'var(--chart-primary)', fill: `url(#${gradId})` }} d={`${path} L 420 150 L 0 150 Z`} />}
        {path && <path className="line" d={path} />}
        {secondPath && <path className="line second" d={secondPath} />}
        {endPoint && <circle className="end-dot" cx={endPoint.x} cy={endPoint.y} r="3" />}
      </svg>
      <div className="chart-caption">
        <span>{data[0]?.time_label || data[0]?.time || '--'}</span>
        <span>{data.at(-1)?.time_label || data.at(-1)?.time || '--'}</span>
      </div>
    </div>
  );
}

export function DecisionConsistencyNotice({ gate, consistency }) {
  if (!gate && !consistency) return null;
  const gateTone = decisionGateTone(gate);
  const consistencyTone = decisionConsistencyTone(consistency);
  return (
    <div className="decision-strip">
      <div className={`decision-chip ${gateTone}`}>
        <strong>{t('scanner2.regimeGate')}</strong>
        <span>{decisionGateLabel(gate)} · {decisionGateSubLabel(gate)}</span>
      </div>
      <div className={`decision-chip ${consistencyTone}`}>
        <strong>{t('scanner2.consistency')}</strong>
        <span>{decisionConsistencyLabel(consistency)} · {consistency?.message || '--'}</span>
      </div>
    </div>
  );
}

export function DecisionNotes({ gate }) {
  const blockers = Array.isArray(gate?.blockers) ? gate.blockers : [];
  const warnings = Array.isArray(gate?.warnings) ? gate.warnings : [];
  const notes = [...blockers.map((text) => ['danger', text]), ...warnings.map((text) => ['warning', text])];
  if (!notes.length) return <p className="planner-note">{t('scanner2.noHardBlockers')}</p>;
  return (
    <div className="mini-list decision-notes">
      {notes.map(([tone, text]) => <span className={tone === 'danger' ? 'danger-scope' : 'warning-scope'} key={`${tone}-${text}`}>{text}</span>)}
    </div>
  );
}

export function TopRecommendationCard({ candidate, payload, label = t('scanner2.topByScore'), observeOnly = false }) {
  if (!candidate) {
    return (
      <div className="top-recommendation muted-card">
        <strong>{observeOnly ? t('scanner2.observeOnlyConclusion') : t('scanner2.noUsableCandidate')}</strong>
        <span>{observeOnly ? t('scanner2.observeOnlyDesc') : t('scanner2.emptyPoolDesc')}</span>
      </div>
    );
  }
  const risk = candidate.risk_plan || {};
  return (
    <article className="top-recommendation">
      <div className="top-recommendation-head">
        <div>
          <span>{label} · {marketDataSourceLabel(payload?.market_data_source)}</span>
          <h3>{candidate.contract_symbol}</h3>
        </div>
        <strong>{sideLabel(candidate.side)} · {candidate.strategy_tag || t('scanner2.untagged')}</strong>
      </div>
      <div className="recommendation-grid">
        <Metric label={t('scanner2.expiryStrike')} value={candidate.expiration} sub={`K ${fmt(candidate.strike)}`} />
        <Metric label={t('scanner2.bidAsk')} value={`${fmt(candidate.bid)} / ${fmt(candidate.ask)}`} sub={`${t('scanner2.spread')} ${pct(candidate.spread_pct)}`} tone={candidate.spread_pct <= 20 ? 'ok' : 'warning'} />
        <Metric label={t('scanner2.liquidity')} value={`Vol ${compact(candidate.volume)}`} sub={`OI ${compact(candidate.open_interest)}`} tone={(candidate.volume || 0) > 500 || (candidate.open_interest || 0) > 3000 ? 'ok' : 'warning'} />
        <Metric label={t('scanner2.greeks')} value={`Δ ${fmt(candidate.delta)}`} sub={`Theta ${fmt(candidate.theta_per_day)}`} />
        <Metric label={t('scanner2.volatility')} value={pct((candidate.implied_volatility ?? 0) * 100)} sub={`RV20 ${pct((candidate.rv20 ?? 0) * 100)} · IV/RV ${fmt(candidate.iv_rv_ratio)}`} tone={candidate.iv_edge_state === 'expensive_iv_crush_risk' ? 'warning' : 'ok'} />
        <Metric label={t('scanner2.volumeProfile')} value={candidate.volume_profile_position || '--'} sub={`POC ${fmt(candidate.volume_profile_poc)} · ${candidate.volume_profile_state || '--'}`} tone={candidate.volume_profile_state === 'resistance_risk' ? 'warning' : candidate.volume_profile_state === 'supportive' ? 'ok' : 'muted'} />
        <Metric label="GEX" value={gexRegimeLabel(candidate.gex_regime)} sub={`${gexAlignmentLabel(candidate.gex_alignment)} · ${gexWallLabel(candidate.gex_nearest_wall)} ${pct(candidate.gex_nearest_wall_distance_pct)}`} tone={candidate.gex_regime === 'negative_gamma' ? 'warning' : candidate.gex_regime === 'positive_gamma' ? 'ok' : 'muted'} />
        <Metric label="Alpha" value={fmt(candidate.alpha_score ?? candidate.analysis_score)} sub={`${t('scanner2.rewardRisk')} ${fmt(candidate.reward_risk_score)}`} tone="ok" />
        <Metric label={t('scanner2.execution')} value={fmt(candidate.execution_score ?? candidate.execution_quality_score)} sub={`${t('scanner2.decision')} ${fmt(candidate.decision_score ?? candidate.analysis_score)}`} tone="ok" />
        <Metric label={t('scanner2.trigger')} value={fmt(candidate.trigger_score)} sub={candidate.trigger_state || '--'} tone={(candidate.trigger_score || 0) >= 60 ? 'ok' : 'warning'} />
        <Metric label={t('scanner2.decision')} value={fmt(candidate.decision_score ?? candidate.analysis_score)} sub={`${t('scanner2.bucket')} ${candidate.decision_bucket || '--'}`} tone={candidate.decision_bucket === 'observe_trigger_not_met' || candidate.decision_bucket === 'blocked_execution' ? 'warning' : 'ok'} />
      </div>
      <div className="risk-strip">
        <span>{t('scanner2.maxLossPerContract')} ${fmt(risk.max_loss_per_contract)}</span>
        <span>{t('scanner2.stopLoss')} {fmt(risk.stop_loss_option_price)}</span>
        <span>{t('scanner2.takeProfit')} {fmt(risk.take_profit_1)} / {fmt(risk.take_profit_2)}</span>
        <span>{t('scanner2.latestExit')} {risk.latest_exit || '--'}</span>
        {risk.iv_rv_note ? <span>{risk.iv_rv_note}</span> : null}
        {risk.volume_profile_note ? <span>{risk.volume_profile_note}</span> : null}
        {candidate.time_value_risk_penalty ? <span>{t('scanner2.timeRiskPenalty')} {fmt(candidate.time_value_risk_penalty)}</span> : null}
      </div>
      {!!(candidate.execution_hard_flags || []).length && (
        <div className="mini-list">
          {(candidate.execution_hard_flags || []).map((flag) => <span key={flag} className="danger-scope">{strategyFlagLabel(flag)}</span>)}
        </div>
      )}
      {candidate.quote_warning && <p className="quote-warning">{quoteWarningLabel(candidate.quote_warning)}</p>}
    </article>
  );
}

export function StrategyRecommendationCard({ candidate, label = t('scanner2.structureCandidateExp') }) {
  if (!candidate) return null;
  const naturalExit = candidate.natural_exit || {};
  return (
    <article className="strategy-recommendation">
      <div className="top-recommendation-head">
        <div>
          <span>{label}</span>
          <h3>{candidate.label}</h3>
        </div>
        <strong>{strategyFamilyLabel(candidate.family)} · {strategyDirectionLabel(candidate.direction)}</strong>
      </div>
      <div className="recommendation-grid strategy-grid">
        <Metric label={t('scanner2.strategyType')} value={candidate.strategy_type || '--'} sub={candidate.expiration || '--'} />
        <Metric
          label={t('scanner2.netDebitCredit')}
          value={candidate.net_debit && candidate.net_credit ? `${money(candidate.net_debit)} / ${money(candidate.net_credit)}` : (candidate.net_credit ? money(candidate.net_credit) : money(candidate.net_debit))}
          sub={candidate.net_credit ? `${t('scanner2.netCredit')} ${money(candidate.net_credit)}` : `${t('scanner2.netDebit')} ${money(candidate.net_debit)}`}
        />
        <Metric label={t('scanner2.maxLossLabel')} value={candidate.max_loss == null ? t('scanner2.undefined') : `$${fmt(candidate.max_loss)}`} sub={`${t('scanner2.capital')} ${candidate.capital_required ? `$${fmt(candidate.capital_required)}` : '--'}`} />
        <Metric label={t('scanner2.maxProfitLabel')} value={candidate.max_profit == null ? t('scanner2.upsideUnlimited') : `$${fmt(candidate.max_profit)}`} sub={`${t('scanner2.width')} ${fmt(candidate.width)}`} />
        <Metric label={t('scanner2.breakeven')} value={strategyBreakevensLabel(candidate.breakevens)} sub={`${t('scanner2.probHint')} ${fmt(candidate.probability_hint)}%`} />
        <Metric label={t('scanner2.score')} value={fmt(candidate.score)} sub={candidate.summary || '--'} tone="ok" />
      </div>
      <div className="recommendation-grid strategy-grid strategy-score-grid">
        <Metric label={t('scanner2.structureFit')} value={fmt(candidate.structure_fit_score)} sub={t('scanner2.structureFitSub')} />
        <Metric label={t('scanner2.payoffQuality')} value={fmt(candidate.payoff_quality_score)} sub={t('scanner2.payoffQualitySub')} />
        <Metric label={t('scanner2.executionComplexity')} value={fmt(candidate.execution_complexity_score)} sub={t('scanner2.executionComplexitySub')} />
        <Metric label={t('scanner2.capitalEfficiency')} value={fmt(candidate.capital_efficiency_score)} sub={t('scanner2.capitalEfficiencySub')} />
        <Metric label={t('scanner2.riskDefined')} value={fmt(candidate.risk_defined_score)} sub={t('scanner2.riskDefinedSub')} />
        <Metric label={t('scanner2.quoteConsistency')} value={fmt(candidate.quote_consistency_score)} sub={candidate.quote_consistency_state || '--'} />
      </div>
      <div className="strategy-legs">
        {candidate.legs?.map((leg, index) => (
          <span key={`${candidate.strategy_type}-${index}`}>
            {leg.action} {leg.qty || 1} · {String(leg.side || '--').toUpperCase()} {shortContract(leg.contract_symbol)} · K {fmt(leg.strike)} · {fmt(leg.price)}
          </span>
        ))}
      </div>
      <div className="mini-list">
        {(candidate.fit_notes || []).map((note) => <span key={note}>{note}</span>)}
        {(candidate.hard_flags || []).map((flag) => <span key={flag} className="danger-scope">{strategyFlagLabel(flag)}</span>)}
        {naturalExit.take_profit && <span>{t('scanner2.takeProfitColon')}{naturalExit.take_profit}</span>}
        {naturalExit.stop_loss && <span>{t('scanner2.stopLossColon')}{naturalExit.stop_loss}</span>}
      </div>
    </article>
  );
}

export function CandidateTable({ candidates, topPick, loading = false }) {
  if (!candidates.length) {
    if (loading) {
      return (
        <div className="candidate-table-wrap">
          <Skeleton variant="row" count={5} />
        </div>
      );
    }
    return <p className="muted">{t('scanner2.waitingCandidates')}</p>;
  }
  const topSymbol = topPick?.contract_symbol;
  return (
    <div className="candidate-table-wrap">
      <table className="candidate-table">
        <thead>
          <tr>
            <th>{t('scanner2.contract')}</th>
            <th>{t('scanner2.side')}</th>
            <th>{t('scanner2.expiry')}</th>
            <th>K</th>
            <th>{t('scanner2.ask')}</th>
            <th>{t('scanner2.spread')}</th>
            <th>Vol / <Term name="OI">OI</Term></th>
            <th><Term name="IV">IV</Term></th>
            <th><Term name="Delta">Delta</Term></th>
            <th><Term name="Theta">Theta</Term></th>
            <th><Term name="GEX">GEX</Term></th>
            <th>Alpha</th>
            <th>{t('scanner2.execution')}</th>
            <th>{t('scanner2.decision')}</th>
            <th>{t('scanner2.tag')}</th>
          </tr>
        </thead>
        <tbody>
          {candidates.map((candidate) => (
            <CandidateRow
              key={candidate.contract_symbol}
              candidate={candidate}
              active={candidate.contract_symbol === topSymbol}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

const CandidateRow = memo(function CandidateRow({ candidate, active }) {
  return (
    <tr className={active ? 'active' : ''}>
      <td><strong>{candidate.contract_symbol}</strong></td>
      <td>{sideLabel(candidate.side)}</td>
      <td>{candidate.expiration}</td>
      <td>{fmt(candidate.strike)}</td>
      <td>{fmt(candidate.ask)}</td>
      <td>{pct(candidate.spread_pct)}</td>
      <td>{compact(candidate.volume)} / {compact(candidate.open_interest)}</td>
      <td>{pct((candidate.implied_volatility ?? 0) * 100)}</td>
      <td>{fmt(candidate.delta)}</td>
      <td>{fmt(candidate.theta_per_day)}</td>
      <td>{gexRegimeLabel(candidate.gex_regime)} · {gexAlignmentLabel(candidate.gex_alignment)}</td>
      <td>{fmt(candidate.alpha_score ?? candidate.analysis_score)}</td>
      <td>{fmt(candidate.execution_score ?? candidate.execution_quality_score)}</td>
      <td>{fmt(candidate.decision_score ?? candidate.analysis_score)}</td>
      <td>{candidate.strategy_tag || '--'}</td>
    </tr>
  );
});

export function StrategyCandidateTable({ candidates }) {
  if (!candidates.length) {
    return <p className="muted">{t('scanner2.noStructureCandidates')}</p>;
  }
  return (
    <div className="candidate-table-wrap">
      <table className="candidate-table strategy-table">
        <thead>
          <tr>
            <th>{t('scanner2.structure')}</th>
            <th>{t('scanner2.family')}</th>
            <th>{t('scanner2.expiry')}</th>
            <th>{t('scanner2.legs')}</th>
            <th>{t('scanner2.netDebitCreditShort')}</th>
            <th>{t('scanner2.maxLossLabel')}</th>
            <th>{t('scanner2.maxProfitLabel')}</th>
            <th>{t('scanner2.breakeven')}</th>
            <th>{t('scanner2.score')}</th>
            <th>{t('scanner2.tag')}</th>
          </tr>
        </thead>
        <tbody>
          {candidates.map((candidate) => (
            <StrategyCandidateRow
              key={`${candidate.strategy_type}-${candidate.expiration}-${candidate.summary || candidate.label}`}
              candidate={candidate}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

const StrategyCandidateRow = memo(function StrategyCandidateRow({ candidate }) {
  return (
    <tr>
      <td>
        <strong>{candidate.label}</strong>
        <small>{candidate.strategy_type}</small>
      </td>
      <td>{strategyFamilyLabel(candidate.family)}</td>
      <td>{candidate.expiration}</td>
      <td>
        <div className="strategy-leg-list">
          {(candidate.legs || []).map((leg, index) => (
            <span key={`${candidate.strategy_type}-leg-${index}`}>{leg.action} {shortContract(leg.contract_symbol)} · K {fmt(leg.strike)}</span>
          ))}
        </div>
      </td>
      <td>{candidate.net_debit && candidate.net_credit ? `${money(candidate.net_debit)} / ${money(candidate.net_credit)}` : (candidate.net_credit ? money(candidate.net_credit) : money(candidate.net_debit))}</td>
      <td>{candidate.max_loss == null ? '--' : `$${fmt(candidate.max_loss)}`}</td>
      <td>{candidate.max_profit == null ? t('scanner2.unlimited') : `$${fmt(candidate.max_profit)}`}</td>
      <td>{strategyBreakevensLabel(candidate.breakevens)}</td>
      <td>{fmt(candidate.score)}</td>
      <td>{(candidate.fit_notes || []).slice(0, 2).join(' / ') || '--'}</td>
    </tr>
  );
});

export function CandidateCard({ candidate, rank, active }) {
  return (
    <article className={`candidate ${active ? 'active' : ''}`}>
      <div className="candidate-top">
        <span>#{rank}</span>
        <strong>{candidate.strategy_tag || candidate.side?.toUpperCase()}</strong>
      </div>
      <h3>{candidate.contract_symbol}</h3>
      <div className="candidate-meta">
        <span>{candidate.expiration}</span>
        <span>{t('scanner2.strikePrice')} {fmt(candidate.strike)}</span>
      </div>
      <div className="quote-line">
        <b>{t('scanner2.bidShort')} {fmt(candidate.bid)} / {t('scanner2.askShort')} {fmt(candidate.ask)}</b>
        <small>{t('scanner2.spread')} {pct(candidate.spread_pct)}</small>
      </div>
      <div className="mini-stats">
        <span>{t('scanner2.volume')} {compact(candidate.volume)}</span>
        <span>OI {compact(candidate.open_interest)}</span>
        <span>IV {pct((candidate.implied_volatility ?? 0) * 100)}</span>
        <span>IVP {pct(candidate.iv_percentile)}</span>
        <span>{t('scanner2.termSlope')} {pct(candidate.term_structure_slope_pct)}</span>
        <span>Delta {fmt(candidate.delta)}</span>
        <span>Theta {fmt(candidate.theta_per_day)}</span>
        <span>{t('scanner2.breakevenShort')} {fmt(candidate.breakeven)}</span>
        <span>{t('scanner2.probBreakeven')} {pct(candidate.probability_breakeven)}</span>
        <span>{t('scanner2.rewardRisk')} {fmt(candidate.reward_risk_score)}</span>
        <span>Alpha {fmt(candidate.alpha_score ?? candidate.analysis_score)}</span>
        <span>{t('scanner2.execution')} {fmt(candidate.execution_score ?? candidate.execution_quality_score)}</span>
        <span>{t('scanner2.decision')} {fmt(candidate.decision_score ?? candidate.analysis_score)}</span>
        <span>GEX {gexRegimeLabel(candidate.gex_regime)} / {gexAlignmentLabel(candidate.gex_alignment)}</span>
        {candidate.pricing_source && candidate.pricing_source !== 'bid_ask' && <span>{t('scanner2.pricing')} {pricingSourceLabel(candidate.pricing_source)}</span>}
      </div>
      {candidate.quote_warning && <p className="quote-warning">{quoteWarningLabel(candidate.quote_warning)}</p>}
    </article>
  );
}
