import { t } from '../i18n/index.js';

export function linePath(data, field, width, height) {
  if (!field || !data?.length) return '';
  const values = data.map((item) => Number(item[field])).filter((value) => Number.isFinite(value) && value > 0);
  if (values.length < 2) return '';
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  return values.map((value, index) => {
    const x = (index / (values.length - 1)) * width;
    const y = height - ((value - min) / range) * (height - 18) - 9;
    return `${index === 0 ? 'M' : 'L'} ${x.toFixed(2)} ${y.toFixed(2)}`;
  }).join(' ');
}

export function fmt(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '--';
  return number.toLocaleString('en-US', { maximumFractionDigits: 2 });
}

export function pct(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '--';
  return `${number.toFixed(2)}%`;
}

export function money(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '--';
  return `$${number.toLocaleString('en-US', { maximumFractionDigits: 2 })}`;
}

export function sideLabel(value) {
  if (value === 'call') return 'CALL';
  if (value === 'put') return 'PUT';
  return String(value || '--').toUpperCase();
}

export function marketDataSourceLabel(value) {
  const source = String(value || 'auto').toLowerCase();
  if (source === 'longbridge') return 'Longbridge';
  if (source === 'thetadata') return 'ThetaData';
  if (source === 'yfinance') return 'yfinance';
  return t('source.autoTheta');
}

export function optionDataSourceLabel(value) {
  const source = String(value || 'thetadata').toLowerCase();
  if (source === 'longbridge') return 'Longbridge';
  if (source === 'yfinance') return 'yfinance';
  return 'ThetaData';
}

export function strategyFamilyLabel(value) {
  const key = String(value || '');
  return key ? t(`strategy.family.${key}`) : (String(value || '--'));
}

export function strategyModesLabel(values) {
  const list = Array.isArray(values) ? values : [];
  if (!list.length) return '--';
  return list.map((mode) => strategyFamilyLabel(mode)).join(' / ');
}

export function strategyDirectionLabel(value) {
  const key = String(value || '');
  const known = ['bullish', 'bearish', 'neutral', 'neutral_to_bullish', 'neutral_to_bearish'];
  return known.includes(key) ? t(`strategy.direction.${key}`) : String(value || '--');
}

export function strategyBreakevensLabel(values) {
  const list = Array.isArray(values) ? values : [];
  if (!list.length) return '--';
  return list.map((value) => fmt(value)).join(' / ');
}

export function strategyFlagLabel(value) {
  const known = [
    'requires_stock_position', 'requires_cash_secured', 'term_structure_risk',
    'diagonal_assignment_risk', 'unlimited_upside', 'needs_stock_backing',
    'needs_cash_secured', 'invalid_ask', 'wide_spread', 'thin_liquidity',
    'quote_unavailable', 'no_bid', 'trigger_not_met', 'observe_trigger_not_met',
    'blocked_execution', 'time_value_risk_high', 'bad_long_ask',
    'short_leg_bid_unavailable', 'net_price_inconsistent',
  ];
  const key = String(value || '');
  return known.includes(key) ? t(`strategy.flag.${key}`) : String(value || '--');
}

export function candidateSortValue(candidate, field) {
  const value = candidate?.[field];
  const number = Number(value);
  if (Number.isFinite(number)) return number;
  return 0;
}

export function buildScanBlockers({ loading, query, marketDataSource, longbridgeAccount, useAi, aiProvider, providers, canTrade = true, strategyModes = [] }) {
  const blockers = [];
  if (loading) blockers.push(t('scanBlocker.running'));
  if (!String(query || '').trim()) blockers.push(t('scanBlocker.queryRequired'));
  const source = String(marketDataSource || '').toLowerCase();
  if (source === 'longbridge' && !canTrade) {
    blockers.push(t('scanBlocker.noLongbridgePermission'));
  }
  if (source === 'longbridge' && canTrade && !longbridgeAccount) {
    blockers.push(t('scanBlocker.longbridgeAccount'));
  }
  if (useAi && !aiProvider) blockers.push(t('scanBlocker.aiModelRequired'));
  if (useAi && Array.isArray(providers) && providers.length === 0) blockers.push(t('scanBlocker.noAiModel'));
  if (useAi && Array.isArray(providers) && providers.length > 0) {
    const selected = providers.find((provider) => provider.name === aiProvider);
    if (selected && selected.configured === false) blockers.push(t('scanBlocker.aiKeyMissing').replace('{model}', selected.label || selected.name));
  }
  if (!Array.isArray(strategyModes) || !strategyModes.length) blockers.push(t('scanBlocker.strategyMode'));
  return blockers;
}

export function findAiSelectedCandidate(answer, candidates) {
  if (!answer || !Array.isArray(candidates) || candidates.length === 0) return null;
  const bySymbol = new Map(candidates.map((candidate) => [candidate.contract_symbol, candidate]));
  // NOTE: these are content markers matched against the AI's Chinese answer text,
  // not UI strings. They must stay in Chinese to parse the model output correctly.
  const markers = ['最终单腿', '最终选择', '合约代码', '最终方案', '最终合约'];
  for (const marker of markers) {
    const index = String(answer).indexOf(marker);
    if (index < 0) continue;
    const segment = String(answer).slice(index, index + 1600);
    const symbol = contractSymbolsInText(segment).find((item) => bySymbol.has(item));
    if (symbol) return bySymbol.get(symbol);
  }
  const unique = [];
  for (const symbol of contractSymbolsInText(String(answer))) {
    if (bySymbol.has(symbol) && !unique.includes(symbol)) unique.push(symbol);
  }
  return unique.length === 1 ? bySymbol.get(unique[0]) : null;
}

export function contractSymbolsInText(text) {
  const matches = [];
  const regex = /\b([A-Z]{1,6})\s*(\d{6}[CP]\d{8})\b/g;
  for (const item of String(text || '').matchAll(regex)) {
    matches.push(`${item[1]}${item[2]}`);
  }
  return matches;
}

export function compact(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '--';
  return Intl.NumberFormat('en-US', { notation: 'compact' }).format(number);
}

export function previewText(value, limit = 160) {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  if (!text) return '--';
  return text.length > limit ? `${text.slice(0, limit)}...` : text;
}

export function enabledToolNames(toolPlan = {}) {
  const keys = [
    'longbridge_quote', 'longbridge_daily_kline', 'longbridge_intraday',
    'longbridge_news', 'longbridge_option_chain', 'thetadata_market_data',
    'thetadata_option_chain', 'yfinance_option_chain', 'iv_structure',
    'scenario_pricing', 'risk_plan',
  ];
  const enabled = keys.filter((key) => toolPlan[key] !== false).map((key) => t(`tool.${key}`));
  return enabled.length ? enabled.join(' / ') : '--';
}

export function formatTime(value) {
  if (!value) return '--';
  if (/^\d{4}-\d{2}-\d{2}$/.test(String(value))) return value;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', {
    timeZone: 'America/New_York',
    hour12: false,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    timeZoneName: 'short',
  });
}

export function stageLabel(stage) {
  const known = [
    'queued', 'starting', 'plan_intent', 'parse_intent', 'longbridge_market_data',
    'thetadata_market_data', 'yfinance_market_data', 'technical_summary',
    'option_chain', 'ai_analysis', 'scan_universe', 'council_ranking',
    'decision_gate_blocked', 'submit_orders', 'risk_circuit_breaker',
    'build_response', 'completed', 'interrupted', 'failed',
  ];
  return known.includes(stage) ? t(`stage.${stage}`) : (stage || '--');
}

export function runStatusLabel(status) {
  const known = ['queued', 'running', 'succeeded', 'partial_failed', 'reviewed', 'failed', 'skipped'];
  return known.includes(status) ? t(`runStatus.${status}`) : (status || '--');
}

export function runTaskStatusLabel(status) {
  const known = ['queued', 'running', 'succeeded', 'failed'];
  return known.includes(status) ? t(`runTaskStatus.${status}`) : (status || '--');
}

export function orderStatusLabel(status) {
  const known = [
    'submitted', 'failed', 'skipped_insufficient_allocation',
    'entry_submitted_stop_pending_unfilled', 'entry_partially_filled_stop_partial',
    'stop_submitted_after_fill', 'entry_filled_stop_unsupported_paper',
    'software_stop_submitted', 'software_stop_failed',
    'software_take_profit_partial_submitted', 'software_take_profit_submitted',
    'software_take_profit_failed', 'instance_flatten_submitted',
    'strategy_auto_exit_submitted', 'strategy_auto_exit_failed',
    'strategy_residual_tracking', 'strategy_manual_exit_detected',
    'residual_exit_failed', 'broker_combo_close_required', 'entry_terminal_no_stop',
  ];
  return known.includes(status) ? t(`orderStatus.${status}`) : (status || '--');
}

export function effectiveOrderStatus(order) {
  if (isPaperStopUnsupported(order)) return 'entry_filled_stop_unsupported_paper';
  return order?.status;
}

export function selectionSourceLabel(source) {
  const known = ['ai_initial', 'ai_top_up', 'system_fallback', 'post_validator_fill'];
  return known.includes(source) ? t(`selectionSource.${source}`) : t('selectionSource.unknown');
}

export function riskPlanSourceLabel(source) {
  const known = ['ai', 'system_default', 'none'];
  return known.includes(source) ? t(`riskPlanSource.${source}`) : t('riskPlanSource.system_default');
}

export function triggerSourceLabel(value) {
  const known = ['manual', 'scheduler', 'api', 'backfill'];
  return known.includes(value) ? t(`triggerSource.${value}`) : (value || '--');
}

export function strategyModeLabel(value) {
  const known = ['single_leg_option', 'strategy_structure_analysis', 'live_option_scan', 'day_trade_options', 'swing_options'];
  return known.includes(value) ? t(`strategyMode.${value}`) : (value || '--');
}

export function environmentLabel(value) {
  const key = String(value || '').toLowerCase();
  if (key === 'paper') return t('environment.paper');
  if (key === 'live') return t('environment.live');
  return value || '--';
}

export function councilModeLabel(value) {
  const known = [
    'council', 'single', 'disabled', 'ai_top_up', 'three_advisors', 'single_ai',
    'single_ai_invalid_json', 'strategy_three_advisors', 'strategy_single_ai',
    'strategy_single_ai_invalid_json', 'strategy_disabled',
  ];
  return known.includes(value) ? t(`councilMode.${value}`) : (value || '--');
}

export function instanceActionResultTitle(action) {
  const known = ['flatten', 'cancel', 'reset-risk', 'delete', 'bulk-delete'];
  return known.includes(action) ? t(`instanceActionResult.${action}`) : t('instanceActionResult.default');
}

export function advisorStatusLabel(value) {
  const map = { succeeded: 'succeeded', success: 'succeeded', failed: 'failed', timeout: 'timeout', skipped: 'skipped', fallback: 'fallback' };
  return map[value] ? t(`advisorStatus.${map[value]}`) : (value || '--');
}

export function candidateStatusLabel(value) {
  const map = { succeeded: 'succeeded', success: 'succeeded', failed: 'failed', no_candidate: 'no_candidate', blocked_by_decision_gate: 'blocked_by_decision_gate', skipped: 'skipped' };
  return map[value] ? t(`candidateStatus.${map[value]}`) : (value || '--');
}

export function topUpStatusLabel(value) {
  const known = ['filled', 'partial', 'skipped', 'not_needed'];
  return known.includes(value) ? t(`topUpStatus.${value}`) : (value || '--');
}

export function pnlBasisLabel(value) {
  const known = ['broker_confirmed', 'broker_and_estimate'];
  return known.includes(value) ? t(`pnlBasis.${value}`) : t('pnlBasis.local_estimate');
}

export function pnlWarningLabel(value) {
  const known = ['entry_price_estimated', 'entry_price_unavailable', 'exit_order_pending_broker_fill', 'exit_price_estimated', 'open_positions_use_mark'];
  return known.includes(value) ? t(`pnlWarning.${value}`) : (value || '--');
}

export function tradingCreateBlockers({ saving, running, config, readinessState }) {
  const blockers = [];
  if (saving) blockers.push(t('tradingBlocker.saving'));
  if (running) blockers.push(t('tradingBlocker.running'));
  if (config.single_instance_enabled === false) blockers.push(t('tradingBlocker.singleInstanceOff'));
  if (!config.live_enabled) blockers.push(t('tradingBlocker.liveDisabled'));
  const broker = config.broker || 'longbridge';
  const usesBrokerAccount = broker === 'alpaca' || broker === 'usmart';
  if (usesBrokerAccount) {
    if (!config.broker_account) blockers.push(t('tradingBlocker.alpacaAccount'));
  } else if (!config.longbridge_account) {
    blockers.push(t('tradingBlocker.tradeAccount'));
  }
  if (Number(config.total_capital) <= 0) blockers.push(t('tradingBlocker.capital'));
  if (!readinessState) blockers.push(t('tradingBlocker.readinessPending'));
  if (readinessState && !readinessState.ok) {
    (readinessState.issues || []).forEach((item) => {
      const lower = String(item || '').toLowerCase();
      if (!config.live_enabled && lower.includes('live trading is disabled')) return;
      if (!config.longbridge_account && lower.includes('longbridge_account is required')) return;
      if (usesBrokerAccount && !config.broker_account && lower.includes('broker_account is required')) return;
      blockers.push(userErrorLabel(item));
    });
  }
  return [...new Set(blockers.filter(Boolean))];
}

export function typedTradingConfirmation(expected, message) {
  const input = window.prompt(`${message}\n\n${t('confirm.typePrompt')}${expected}`);
  return String(input || '').trim() === expected;
}

export function runNeedsProtectionPolling(run) {
  if (!run) return false;
  const protection = run?.trade_instance?.protection_status || {};
  const riskPositions = run?.trade_instance?.risk_plan?.strategy_positions;
  const strategyActive = Array.isArray(riskPositions) && riskPositions.some((position) => position?.risk_tracking_active);
  return Boolean(
    protection.software_stop_active
    || protection.software_take_profit_active
    || protection.single_leg_smart_exit_active
    || protection.requires_manual_attention
    || protection.state === 'strategy_residual_tracking'
    || strategyActive
  );
}

export function statusTone(status) {
  const value = String(status || '');
  if (value === 'succeeded') return 'ok';
  if (value === 'failed') return 'danger';
  if (value === 'queued') return 'muted';
  if (value === 'running') return 'warning';
  return 'muted';
}

export function lifecycleTone(lifecycle, protection = {}) {
  const value = String(lifecycle || '');
  if (protection.requires_manual_attention || protection.unprotected_quantity > 0 || ['blocked', 'unprotected', 'stop_failed', 'manual_intervention_required'].includes(value)) return 'danger';
  if (['exiting', 'monitoring', 'partial_fill'].includes(value)) return 'warning';
  if (value === 'closed' && String(protection.state || '') === 'strategy_residual_tracking') return 'warning';
  if (['closed', 'reviewed'].includes(value)) return 'ok';
  return 'muted';
}

export function protectionTone(protection = {}) {
  const state = protection.state;
  if (protection.requires_manual_attention || protection.unprotected_quantity > 0 || state === 'unprotected' || state === 'blocked' || state === 'strategy_exit_failed' || state === 'broker_combo_close_required') return 'danger';
  if (state === 'pending' || state === 'strategy_exiting' || state === 'strategy_partial_exiting' || state === 'strategy_residual_tracking' || protection.software_stop_active || protection.software_take_profit_active || protection.single_leg_smart_exit_active) return 'warning';
  if (state === 'protected' || state === 'software_protected' || state === 'strategy_protected' || state === 'strategy_exited' || state === 'exited') return 'ok';
  return 'muted';
}

export function gexRegimeLabel(value) {
  const known = ['positive_gamma', 'negative_gamma', 'neutral', 'mixed', 'disabled', 'unknown'];
  return known.includes(value) ? t(`gexRegime.${value}`) : (value || '--');
}

export function gexAlignmentLabel(value) {
  const known = ['tailwind', 'headwind', 'neutral'];
  return known.includes(value) ? t(`gexAlignment.${value}`) : (value || '--');
}

export function gexWallLabel(value) {
  const labels = {
    call_wall: 'Call Wall',
    put_wall: 'Put Wall',
  };
  return labels[value] || value || '--';
}

export function decisionGateLabel(gate = {}) {
  if (!gate || Object.keys(gate).length === 0) return '--';
  if (gate.should_trade === false) return t('decisionGate.blocked');
  if (gate.allow_auto_trade === false) return t('decisionGate.observeLimit');
  if (Array.isArray(gate.warnings) && gate.warnings.length) return t('decisionGate.hasWarnings');
  return t('decisionGate.pass');
}

export function decisionGateSubLabel(gate = {}) {
  if (!gate || Object.keys(gate).length === 0) return '--';
  const execution = preferredExecutionLabel(gate.preferred_execution);
  return `${decisionRegimeLabel(gate.regime)} · ${execution}`;
}

export function decisionGateTone(gate = {}) {
  if (!gate || Object.keys(gate).length === 0) return 'muted';
  if (gate.should_trade === false || (Array.isArray(gate.blockers) && gate.blockers.length)) return 'danger';
  if (gate.allow_auto_trade === false || (Array.isArray(gate.warnings) && gate.warnings.length)) return 'warning';
  return 'ok';
}

export function decisionRegimeLabel(value) {
  const known = ['bull_trend', 'bear_trend', 'momentum', 'range', 'choppy', 'unclear', 'unknown'];
  const key = String(value || '');
  return known.includes(key) ? t(`decisionRegime.${key}`) : String(value || '--');
}

export function preferredExecutionLabel(value) {
  const known = ['normal', 'wait_trigger', 'limit_only'];
  const key = String(value || '');
  return known.includes(key) ? t(`preferredExecution.${key}`) : String(value || '--');
}

export function decisionConsistencyLabel(value = {}) {
  const known = ['consistent', 'ai_overrode_top_rank', 'score_top_fallback', 'gate_blocked', 'blocked'];
  const key = String(value?.status || '');
  return known.includes(key) ? t(`decisionConsistency.${key}`) : String(value?.status || '--');
}

export function decisionConsistencyTone(value = {}) {
  const severity = String(value?.severity || '');
  if (severity === 'danger') return 'danger';
  if (severity === 'warning') return 'warning';
  if (severity === 'ok') return 'ok';
  if (value?.status === 'consistent') return 'ok';
  if (value?.status) return 'warning';
  return 'muted';
}

export function decisionValidationLabel(value) {
  const known = ['valid', 'valid_with_warnings', 'observe', 'invalid_json_schema'];
  const key = String(value || '');
  return known.includes(key) ? t(`decisionValidation.${key}`) : String(value || '--');
}

export function decisionValidationSubLabel(value = {}) {
  const warnings = Array.isArray(value?.warnings) ? value.warnings.length : 0;
  const errors = Array.isArray(value?.errors) ? value.errors.length : 0;
  return `${errors} ${t('decisionValidation.errors')} · ${warnings} ${t('decisionValidation.warnings')}`;
}

export function decisionValidationTone(value = {}) {
  const errors = Array.isArray(value?.errors) ? value.errors.length : 0;
  const warnings = Array.isArray(value?.warnings) ? value.warnings.length : 0;
  if (errors > 0) return 'danger';
  if (String(value?.action || '') !== 'trade') return 'warning';
  if (warnings > 0) return 'warning';
  if (value?.valid) return 'ok';
  return 'muted';
}

export function primarySourceLabel(value) {
  const known = ['ai_selected_candidate', 'validated_trade_selection', 'decision_score_top', 'decision_gate_blocked', 'no_candidates', 'observe_only'];
  const key = String(value || '');
  return known.includes(key) ? t(`primarySource.${key}`) : String(value || '--');
}

export function runLifecycle(run = {}) {
  return run.trade_instance?.lifecycle_state || run.lifecycle_state || 'created';
}

export function runProtection(run = {}) {
  return run.trade_instance?.protection_status || { state: run.protection_state || 'not_started' };
}

export function runProtectionState(run = {}) {
  return runProtection(run).state || run.protection_state || 'not_started';
}

export function protectionSummaryLabel(protection = {}) {
  const state = String(protection.state || '');
  if (state === 'strategy_partial_exiting') {
    const tracked = Number(protection.strategy_tracked_quantity || 0);
    const exiting = Number(protection.strategy_exit_submitted_quantity || 0);
    return `${t('protectionSummary.partialExiting')} · ${t('protectionSummary.tracked')} ${tracked} · ${t('protectionSummary.exiting')} ${exiting}`;
  }
  return protectionStateLabel(state);
}

export function instanceLifecycleLabel(run = {}) {
  return lifecycleDisplayLabel(runLifecycle(run), runProtection(run));
}

export function lifecycleDisplayLabel(lifecycle, protection = {}) {
  const value = String(lifecycle || '');
  if (value === 'exiting' && String(protection.state || '') === 'strategy_partial_exiting') {
    return t('lifecycle.strategy_partial_exiting');
  }
  if (value === 'closed' && String(protection.state || '') === 'strategy_residual_tracking') {
    return t('lifecycle.strategy_residual_tracking');
  }
  return lifecycleLabel(value);
}

export function normalizeRunId(value) {
  if (typeof value === 'string') return value.trim();
  if (value && typeof value === 'object' && typeof value.id === 'string') return value.id.trim();
  return '';
}

export function mergeRunSummary(current = {}, summary = {}) {
  return {
    ...current,
    id: summary.id || current.id,
    owner_id: summary.owner_id ?? current.owner_id,
    status: summary.status ?? current.status,
    created_at: summary.created_at ?? current.created_at,
    started_at: summary.started_at ?? current.started_at,
    finished_at: summary.finished_at ?? current.finished_at,
    stage: summary.stage ?? current.stage,
    progress: summary.progress ?? current.progress,
    selection_count: summary.selection_count ?? current.selection_count,
    order_count: summary.order_count ?? current.order_count,
    lifecycle_state: summary.lifecycle_state ?? current.lifecycle_state,
    protection_state: summary.protection_state ?? current.protection_state,
    error: summary.error ?? current.error,
    trade_instance: current.trade_instance || summary.trade_instance,
  };
}

export function runSelectionCount(run = {}) {
  const decisionCount = run.trade_instance?.ai_decision?.selection_count;
  if (decisionCount != null) return decisionCount;
  if (run.selection_count != null) return run.selection_count;
  return Array.isArray(run.selections) ? run.selections.length : 0;
}

export function runOrderCount(run = {}) {
  if (run.order_count != null) return run.order_count;
  return Array.isArray(run.orders) ? run.orders.length : 0;
}

export function runPlannedRisk(run = {}) {
  return run.trade_instance?.risk_plan?.planned_premium_at_risk ?? 0;
}

export function instanceAttention(run = {}) {
  const lifecycle = runLifecycle(run);
  const protection = runProtection(run);
  return Boolean(
    protection.requires_manual_attention ||
    Number(protection.unprotected_quantity || 0) > 0 ||
    ['blocked', 'unprotected', 'stop_failed', 'manual_intervention_required'].includes(lifecycle) ||
    ['blocked', 'unprotected', 'broker_combo_close_required'].includes(protection.state)
  );
}

export function instanceFilterMatch(run = {}, filter = 'all') {
  const lifecycle = runLifecycle(run);
  const protection = runProtection(run);
  if (filter === 'all') return true;
  if (filter === 'attention') return instanceAttention(run);
  if (filter === 'protected') return ['protected', 'software_protected', 'strategy_protected', 'strategy_residual_tracking'].includes(protection.state);
  if (filter === 'closed') return ['closed', 'reviewed', 'blocked', 'no_position'].includes(lifecycle);
  if (filter === 'active') {
    return ['queued', 'running'].includes(run.status) || ['scanning', 'council_review', 'approved', 'submitting', 'open', 'protected', 'monitoring', 'exiting', 'partial_fill'].includes(lifecycle);
  }
  return true;
}

export function buildInstanceListStats(runs = []) {
  return runs.reduce((stats, run) => {
    stats.total += 1;
    if (instanceFilterMatch(run, 'active')) stats.active += 1;
    if (instanceFilterMatch(run, 'attention')) stats.attention += 1;
    if (instanceFilterMatch(run, 'protected')) stats.protected += 1;
    if (instanceFilterMatch(run, 'closed')) stats.closed += 1;
    return stats;
  }, { total: 0, active: 0, attention: 0, protected: 0, closed: 0 });
}

export function winLossLabel(value) {
  const known = ['win', 'loss', 'flat'];
  return known.includes(value) ? t(`winLoss.${value}`) : (value || '--');
}

export function pricingSourceLabel(value) {
  const known = ['bid_ask', 'ask_only', 'last_price_fallback', 'unavailable'];
  return known.includes(value) ? t(`pricingSource.${value}`) : (value || '--');
}

export function quoteWarningLabel(value) {
  const text = String(value || '');
  if (!text) return '';
  if (text.includes('bid is unavailable')) return t('quoteWarning.bidUnavailable');
  if (text.includes('bid/ask are unavailable')) return t('quoteWarning.bidAskUnavailable');
  if (text.includes('did not return a usable bid/ask')) return t('quoteWarning.noUsable');
  return text;
}

export function lifecycleLabel(value) {
  const known = [
    'created', 'scanning', 'council_review', 'approved', 'submitting', 'open',
    'protected', 'monitoring', 'exiting', 'closed', 'reviewed', 'blocked',
    'partial_fill', 'unprotected', 'stop_failed', 'manual_intervention_required',
    'strategy_residual_tracking', 'strategy_manual_exit_detected',
    'residual_exit_failed', 'broker_combo_close_required', 'strategy_partial_exiting',
    'strategy_exit_failed', 'strategy_exited', 'exited',
  ];
  return known.includes(value) ? t(`lifecycle.${value}`) : (value || '--');
}

export function protectionStateLabel(value) {
  const known = [
    'not_started', 'pending', 'protected', 'software_protected', 'strategy_protected',
    'strategy_residual_tracking', 'broker_combo_close_required', 'strategy_partial_exiting',
    'strategy_exiting', 'strategy_exit_failed', 'strategy_exited', 'exited',
    'no_position', 'unprotected', 'blocked',
  ];
  return known.includes(value) ? t(`protectionState.${value}`) : (value || '--');
}

export function strategyOrderStatusLabel(value) {
  const known = [
    'submitted', 'failed', 'pending', 'blocked', 'blocked_missing_backing',
    'blocked_no_option_legs', 'blocked_strategy_net_price_gate',
    'strategy_auto_exit_submitted', 'strategy_auto_exit_failed',
    'strategy_residual_tracking', 'residual_exit_failed', 'broker_combo_close_required',
    'residual_tracking', 'broker_combo_required', 'no_filled_legs',
  ];
  return known.includes(value) ? t(`strategyOrderStatus.${value}`) : (value || '--');
}

export function buildBlockedStrategyItems({ orders = [], strategyPositions = [], protectionContracts = [] } = {}) {
  const positionsByTrackingId = new Map(
    (Array.isArray(strategyPositions) ? strategyPositions : [])
      .filter((position) => position?.tracking_id)
      .map((position) => [position.tracking_id, position])
  );
  const output = [];
  const seen = new Set();
  const addItem = (raw) => {
    if (!raw || !strategyRecordBlocked(raw)) return;
    const position = positionsByTrackingId.get(raw.tracking_id) || {};
    const gate = raw.strategy_net_price_gate || {};
    const key = raw.tracking_id || `${raw.symbol || ''}:${raw.strategy_type || ''}:${raw.status || ''}:${output.length}`;
    if (seen.has(key)) return;
    seen.add(key);
    output.push({
      ...position,
      ...raw,
      status: raw.status || raw.strategy_entry_status || position.execution_status || position.tracking_status,
      reason: raw.message || gate.message || raw.stop_failure_reason || position.reason || '',
      issues: Array.isArray(gate.issues) ? gate.issues : [],
      expected_net: gate.expected_net,
      actual_net: gate.actual_net,
      expected_debit: gate.expected_debit,
      actual_debit: gate.actual_debit,
      tolerance_pct: gate.tolerance_pct,
      legs: Array.isArray(gate.legs) && gate.legs.length
        ? gate.legs
        : Array.isArray(raw.legs) && raw.legs.length
          ? raw.legs
          : Array.isArray(position.legs)
            ? position.legs
            : [],
    });
  };
  (Array.isArray(orders) ? orders : []).forEach(addItem);
  (Array.isArray(strategyPositions) ? strategyPositions : []).forEach(addItem);
  (Array.isArray(protectionContracts) ? protectionContracts : []).forEach((item) => {
    if (String(item?.status || '').startsWith('blocked')) addItem(item);
  });
  return output;
}

export function strategyRecordBlocked(item) {
  const status = String(item?.status || item?.strategy_entry_status || item?.execution_status || item?.tracking_status || '').toLowerCase();
  if (status.startsWith('blocked')) return true;
  if (item?.strategy_net_price_gate && item.strategy_net_price_gate.passed === false) return true;
  return false;
}

export function strategyComboLabel(item) {
  const type = item?.strategy_type || '--';
  const expiration = item?.expiration || item?.legs?.[0]?.expiration || '--';
  const strikes = (item?.legs || [])
    .map((leg) => fmt(leg.strike))
    .filter((value) => value !== '--')
    .join('/');
  return `${type} · ${expiration}${strikes ? ` · ${strikes}` : ''}`;
}

export function strategyLegActionLabel(value) {
  const key = String(value || '').toLowerCase();
  if (key === 'buy') return t('legAction.buy');
  if (key === 'sell') return t('legAction.sell');
  return value || '--';
}

export function strategyBlockReasonLabel(value) {
  const text = String(value || '').trim();
  if (!text) return '--';
  const actual = text.match(/actual=([0-9.]+)/)?.[1];
  const expected = text.match(/expected=([0-9.]+)/)?.[1];
  const maximum = text.match(/maximum=([0-9.]+)/)?.[1];
  if (text.startsWith('net_debit_worse_than_tolerance')) {
    return t('strategyBlockReason.debitWorseThanTolerance')
      .replace('{actual}', actual || '--')
      .replace('{expected}', expected || '--');
  }
  if (text.startsWith('net_debit_above_maximum')) {
    return t('strategyBlockReason.debitAboveMaximum')
      .replace('{actual}', actual || '--')
      .replace('{maximum}', maximum || '--');
  }
  if (text.includes('fresh strategy leg quote unavailable')) return t('strategyBlockReason.freshQuoteUnavailable');
  if (text.includes('net_price_flipped_from_debit_to_credit_or_zero')) return t('strategyBlockReason.flippedDebitToCredit');
  if (text.includes('net_price_flipped_from_credit_to_debit_or_zero')) return t('strategyBlockReason.flippedCreditToDebit');
  return userErrorLabel(text);
}

export function eventTypeLabel(value) {
  const known = [
    'created', 'scanning_started', 'candidate_snapshot', 'ai_decision', 'risk_plan',
    'decision_gate_blocked_execution', 'orders_submitted', 'protection_status_changed',
    'risk_state_reinitialized', 'instance_orders_cancel_requested', 'manual_flatten_instance',
    'strategy_analysis_only', 'strategy_stop_alerted', 'strategy_take_profit_alerted',
    'strategy_residual_tracking_started', 'strategy_residual_exit_submitted',
    'strategy_residual_exit_filled', 'strategy_residual_exit_failed',
    'strategy_residual_manual_flat_detected', 'broker_combo_close_required',
    'software_stop_triggered', 'software_stop_failed', 'take_profit_hit',
    'software_take_profit_failed', 'risk_circuit_breaker_blocked', 'interrupted', 'failed',
  ];
  return known.includes(value) ? t(`eventType.${value}`) : (value || '--');
}

export function eventMessageLabel(value) {
  const text = String(value || '');
  if (!text) return '--';
  const orderMatch = text.match(/^订单执行完成：(\d+) 条结果，保护状态 ([^.。]+)[.。]?$/);
  if (orderMatch) return t('eventMessage.orderComplete').replace('{count}', orderMatch[1]).replace('{state}', protectionStateLabel(orderMatch[2]));
  const protectionMatch = text.match(/^保护状态从 (.+) 更新为 (.+)[.。]?$/);
  if (protectionMatch) return t('eventMessage.protectionChanged').replace('{from}', protectionStateLabel(protectionMatch[1])).replace('{to}', protectionStateLabel(protectionMatch[2]));
  return text;
}

export function monitorStatusLabel(value) {
  const known = [
    'waiting_entry_fill', 'waiting_additional_fill', 'completed', 'software_stop_submitted',
    'software_stop_failed', 'software_take_profit_submitted', 'instance_flatten_submitted',
  ];
  return known.includes(value) ? t(`monitorStatus.${value}`) : (value || '--');
}

export function userMessageLabel(value) {
  const text = String(value || '');
  if (!text) return '';
  if (text.includes('without any fill')) return t('userMessage.withoutFill');
  if (text.includes('paper accounts do not support automatic stop')) return t('userMessage.paperNoStop');
  if (text.includes('partially filled')) return t('userMessage.partiallyFilled');
  if (text.includes('entry order was not confirmed filled')) return t('userMessage.entryNotConfirmed');
  return text;
}

export function userErrorLabel(value) {
  const text = String(value || '').trim();
  if (!text) return '';
  const lower = text.toLowerCase();
  if (lower.includes('live trading is disabled')) return t('userError.liveDisabled');
  if (lower.includes('terms acceptance required')) return t('userError.termsRequired');
  if (lower.includes('longbridge_account is required')) return t('userError.accountRequired');
  if (lower.includes('missing env var')) return t('userError.missingEnvVar');
  if (lower.includes('longbridge account profile') && lower.includes('does not exist')) return t('userError.accountMissing');
  if (lower.includes('too many requests') || lower.includes('rate limited')) return t('userError.rateLimited');
  if (lower.includes("no such file or directory: 'longbridge'")) return t('userError.longbridgeCliMissing');
  if (lower.includes('paper account') && (lower.includes('not supported') || lower.includes('604050'))) return t('userError.paperUnsupported');
  if (lower.includes('requires the longbridge python sdk backend')) return t('userError.requiresSdkBackend');
  if (lower.includes('entry order ended') && lower.includes('without any fill')) return t('userError.entryEndedNoFill');
  if (lower.includes('entry order was not confirmed filled')) return t('userError.entryNotConfirmed');
  if (lower.includes('software stop quote unavailable')) return t('userError.stopQuoteUnavailable');
  if (lower.includes('software take profit quote unavailable')) return t('userError.takeProfitQuoteUnavailable');
  if (lower.includes('invalid software stop quantity')) return t('userError.invalidStopQuantity');
  if (lower.includes('stop order id missing')) return t('userError.stopOrderIdMissing');
  if (lower.includes('order quantity must be greater than zero') || lower.includes('invalid order quantity')) return t('userError.invalidOrderQuantity');
  if (lower.includes('unsupported order side')) return t('userError.unsupportedSide');
  if (lower.includes('timed out')) return t('userError.timedOut');
  if (lower.includes('api error')) return text.replace(/^Error:\s*/i, t('userError.brokerApiPrefix'));
  return text;
}

export function userRemainingLabel(user = {}) {
  if (user.expired) return t('userRemaining.expired');
  const seconds = Number(user.remaining_seconds);
  if (!Number.isFinite(seconds)) return t('userRemaining.permanent');
  if (seconds <= 0) return t('userRemaining.expired');
  const days = seconds / 86400;
  if (days >= 1) return t('userRemaining.days').replace('{n}', days.toFixed(days >= 10 ? 0 : 1));
  const hours = seconds / 3600;
  if (hours >= 1) return t('userRemaining.hours').replace('{n}', hours.toFixed(hours >= 10 ? 0 : 1));
  const minutes = Math.max(1, Math.floor(seconds / 60));
  return t('userRemaining.minutes').replace('{n}', minutes);
}

export function formatBjDisplay(value) {
  if (!value) return '--';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date).replaceAll('/', '-');
}

// Convert a stored ISO datetime (any tz) into a `datetime-local` input value
// expressed in Beijing time, e.g. "2026-05-11T19:30". Returns '' when invalid.
export function bjIsoToLocalInput(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(date).reduce((acc, p) => {
    if (p.type !== 'literal') acc[p.type] = p.value;
    return acc;
  }, {});
  const hour = parts.hour === '24' ? '00' : parts.hour;
  return `${parts.year}-${parts.month}-${parts.day}T${hour}:${parts.minute}`;
}

// Convert a `datetime-local` value (interpreted as Beijing time) into an ISO-8601
// string with the +08:00 offset, e.g. "2026-05-11T19:30:00+08:00". Returns null
// when empty/invalid so the caller can omit it from the request.
export function localInputToBjIso(value) {
  if (!value) return null;
  const m = String(value).match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!m) return null;
  return `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:00+08:00`;
}

export function countdownText(targetIso, nowMs) {
  if (!targetIso) return '--';
  const target = new Date(targetIso).getTime();
  if (Number.isNaN(target)) return '--';
  const diff = target - nowMs;
  if (diff <= 0) return t('countdown.soon');
  const totalSeconds = Math.floor(diff / 1000);
  const days = Math.floor(totalSeconds / 86400);
  const hours = Math.floor((totalSeconds % 86400) / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (days > 0) return t('countdown.daysHours').replace('{d}', days).replace('{h}', hours);
  if (hours > 0) return t('countdown.hoursMinutes').replace('{h}', hours).replace('{m}', minutes);
  if (minutes > 0) return t('countdown.minutesSeconds').replace('{m}', minutes).replace('{s}', seconds);
  return t('countdown.seconds').replace('{s}', seconds);
}

export function userRemainingInputValue(user = {}) {
  const days = Number(user.remaining_days);
  if (Number.isFinite(days) && days >= 0) return days;
  return 7;
}

export function protectionContractLabel(item = {}) {
  if (item.unprotected_quantity > 0) return `${t('protectionContract.unprotected')} ${item.unprotected_quantity}`;
  if (item.broker_stop_submitted) return `${t('protectionContract.broker')} ${item.covered_quantity || 0}`;
  if (item.broker_combo_close_required) return t('protectionContract.comboClose');
  if (item.residual_leg_tracking_active) return `${t('protectionContract.residual')} ${item.residual_leg_quantity || item.quantity || 0}`;
  if (item.software_stop_active) return `${t('protectionContract.software')} ${item.software_stop_quantity || 0}`;
  if (item.software_take_profit_active) return `${t('protectionContract.takeProfit')} ${item.software_take_profit_quantity || 0}`;
  if (item.software_stop_closed_quantity > 0) return `${t('protectionContract.closed')} ${item.software_stop_closed_quantity}`;
  if (item.software_take_profit_closed_quantity > 0) return `${t('protectionContract.takeProfitClosed')} ${item.software_take_profit_closed_quantity}`;
  if (item.status === 'entry_terminal_no_stop') return t('protectionContract.noFill');
  return item.status || '--';
}

export function candidateEvidenceText(evidence = {}) {
  const daily = evidence.daily_summary || {};
  const intraday = evidence.intraday_summary || {};
  return `${t('evidence.daily')} ${pct(daily.change_pct)} · ${t('evidence.intraday')} ${pct(intraday.change_pct)} · VWAP ${pct(intraday.vs_vwap_pct)} · ${intraday.trend || intraday.state || t('evidence.trendNa')}`;
}

export function advisorSummary(advisor = {}) {
  const report = advisor.structured_report || {};
  const pieces = [
    report.recommendation && `${t('advisor.recommendation')} ${report.recommendation}`,
    report.confidence != null && `${t('advisor.confidence')} ${pct(Number(report.confidence) * 100)}`,
    report.reasoning || report.summary || advisor.report,
  ].filter(Boolean);
  if (pieces.length) return previewText(pieces.join(' · '), 360);
  return '--';
}

export function rejectionReason(item = {}) {
  if (item.reason) return item.reason;
  if (Array.isArray(item.issues)) return item.issues.join(', ');
  if (item.issues) return String(item.issues);
  return '--';
}

export function shortContract(value) {
  const text = String(value || '');
  if (!text) return '--';
  return text.replace('.US', '').replace(/^([A-Z]+)(\d{6})([CP])0*/, '$1$2$3');
}

export function shortId(value) {
  const text = String(value || '');
  if (!text) return '--';
  return text.length > 10 ? `${text.slice(0, 6)}...${text.slice(-4)}` : text;
}

export function shortLocator(value) {
  const text = String(value || '');
  if (!text) return '--';
  const [prefix, tail] = text.split('-', 2);
  if (prefix && tail && tail.length > 8) return `${prefix}-${tail.slice(0, 4)}...${tail.slice(-4)}`;
  return shortId(text);
}

export function renderOrderDetail(order) {
  const parts = [];
  if (order.strategy_net_price_gate) {
    parts.push(t('orderDetail.strategyRequote'));
  } else if (order.entry_order_type === 'market') {
    parts.push(t('orderDetail.marketNoRequote'));
  }
  if (order.entry_order_type === 'limit') parts.push(t('orderDetail.limitRequoted'));
  if (order.strategy_net_price_gate?.passed === false) {
    parts.push(strategyBlockReasonLabel(order.strategy_net_price_gate.message || order.message));
  }
  if (isPaperStopUnsupported(order)) {
    parts.push(t('orderDetail.paperManualExit'));
  } else if (order.message) {
    parts.push(userMessageLabel(order.message));
  }
  if (order.error) parts.push(userErrorLabel(order.error));
  if (order.stop_error) parts.push(userErrorLabel(order.stop_error));
  if (order.monitor_error && order.status !== 'entry_terminal_no_stop') parts.push(userErrorLabel(order.monitor_error));
  if (order.monitor_status) parts.push(`${t('orderDetail.execStatus')}${monitorStatusLabel(order.monitor_status)}`);
  if (order.software_stop_active) parts.push(`${t('orderDetail.softwareStopMonitor')} ${order.software_stop_quantity || 0} ${t('orderDetail.contracts')}`);
  if (order.software_stop_status && !order.software_stop_active) parts.push(`${t('orderDetail.softwareStop')}${monitorStatusLabel(order.software_stop_status)}`);
  if (order.software_stop_last_quote?.exit_price) parts.push(`${t('orderDetail.checkPrice')} ${fmt(order.software_stop_last_quote.exit_price)}`);
  if (order.software_stop_error) parts.push(userErrorLabel(order.software_stop_error));
  if (order.software_stop_order) parts.push(t('orderDetail.softwareStopOrderSubmitted'));
  if (order.software_take_profit_active) parts.push(`${t('orderDetail.softwareTpMonitor')} ${order.software_take_profit_quantity || 0} ${t('orderDetail.contracts')}`);
  if (order.software_take_profit_status) parts.push(`${t('orderDetail.softwareTp')}${monitorStatusLabel(order.software_take_profit_status)}`);
  if (order.software_take_profit_last_quote?.exit_price) parts.push(`${t('orderDetail.tpCheckPrice')} ${fmt(order.software_take_profit_last_quote.exit_price)}`);
  if (order.software_take_profit_error) parts.push(userErrorLabel(order.software_take_profit_error));
  if (order.software_take_profit_order) parts.push(t('orderDetail.softwareTpOrderSubmitted'));
  if (order.residual_leg_tracking_active) {
    parts.push(`${t('orderDetail.residualTracking')} ${shortContract(order.residual_leg_contract_symbol || order.contract_symbol)} ${order.residual_leg_quantity || order.quantity || 0} ${t('orderDetail.contracts')}`);
  }
  if (order.broker_combo_close_required) {
    parts.push(`${t('orderDetail.brokerComboClose')}${userErrorLabel(order.broker_combo_close_reason || order.strategy_exit_error || '')}`);
  } else if (order.strategy_exit_error && !order.residual_leg_tracking_active) {
    parts.push(userErrorLabel(order.strategy_exit_error));
  }
  if (order.instance_flatten_order) parts.push(`${t('orderDetail.instanceFlattenSubmitted')} ${order.instance_flatten_submitted_quantity || order.instance_flatten_closed_quantity || ''}`.trim());
  if (order.entry_detail?.status) parts.push(`${t('orderDetail.entryStatus')}${order.entry_detail.status}`);
  if (order.stop_orders?.length) parts.push(`${t('orderDetail.protectionOrders')} ${order.stop_orders.length} ${t('orderDetail.count')}`);
  else if (order.stop_order) parts.push(t('orderDetail.protectionReady'));
  return parts.filter(Boolean).join(' · ') || t('orderDetail.none');
}

export function orderDisplayTitle(order) {
  if (order.order_symbol || order.contract_symbol) return order.order_symbol || shortContract(order.contract_symbol);
  if (order.strategy_type || order.label) return `${order.symbol || '--'} ${order.label || order.strategy_type}`;
  return order.tracking_id || '--';
}

export function orderExecutionSummary(order) {
  if (order.strategy_type || order.strategy_net_price_gate) {
    const gate = order.strategy_net_price_gate || {};
    const parts = [
      entryOrderTypeLabel(order.entry_order_type),
      `${t('orderExec.combo')} ${order.strategy_type || order.label || '--'}`,
      `${t('orderExec.quantity')} ${order.quantity ?? order.units ?? '--'} ${t('orderExec.groups')}`,
    ];
    if (gate.expected_debit != null || gate.actual_debit != null) {
      parts.push(`${t('orderExec.netDebit')} ${fmt(gate.actual_debit ?? gate.actual_net)} / ${t('orderExec.expected')} ${fmt(gate.expected_debit ?? gate.expected_net)}`);
    }
    if (Array.isArray(gate.legs) && gate.legs.length) parts.push(`${t('orderExec.legs')} ${gate.legs.length}`);
    return parts.join(' · ');
  }
  return `${entryOrderTypeLabel(order.entry_order_type)} · ${t('orderExec.quantity')} ${order.quantity} · ${t('orderExec.protected')} ${order.covered_quantity || 0} · ${t('orderExec.softwareStop')} ${order.software_stop_quantity || 0} · ${t('orderExec.takeProfit')} ${order.software_take_profit_quantity || 0} · ${t('orderExec.entry')} ${fmt(order.entry_price)} · ${t('orderExec.stop')} ${fmt(order.stop_trigger_price)}`;
}

export function entryOrderTypeLabel(value) {
  return value === 'limit' ? t('orderType.limit') : t('orderType.market');
}

export function flattenSideLabel(value) {
  return value === 'buy' ? t('flattenSide.buyToCover') : t('flattenSide.sellToClose');
}

export function isPaperStopUnsupported(order) {
  const combined = [order?.error, order?.monitor_error, order?.stop_error, order?.message].filter(Boolean).join(' ').toLowerCase();
  const entryStatus = String(order?.entry_detail?.status || order?.entry_detail?.order_status || '').toLowerCase();
  return combined.includes('604050') && combined.includes('paper account') && entryStatus.includes('filled');
}
