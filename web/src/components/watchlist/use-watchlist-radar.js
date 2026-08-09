import { useEffect, useMemo, useState } from 'react';
import { t } from '../../i18n/index.js';
import { defaultAnalysisModules } from '../../config.js';

const defaultSymbols = 'SPY, QQQ, NVDA, TSLA, AAPL, MSFT, META';
const defaultInstanceForm = {
  name: t('watchlist2.defaultInstanceName'),
  symbols: '',
  interval_minutes: 30,
  rule_template: 'breakout',
  prompt_template: '扫描{symbol}最近日K、今天分时、新闻和期权链，寻找高赔率但风险可控的期权方案。',
  prefilter_logic: 'and',
  alert_logic: 'and',
  min_rvol: 1.0,
  alert_min_rvol: 1.5,
  price_rule_enabled: false,
  price_rule_field: 'last',
  price_rule_operator: '>=',
  price_rule_value: '',
  technical_rule_enabled: true,
  technical_rule_field: 'underlying_vs_vwap_pct',
  technical_rule_operator: '>=',
  technical_rule_value: '0',
  option_rule_enabled: false,
  option_rule_field: 'bid_ask_spread_pct',
  option_rule_operator: '<=',
  option_rule_value: '12',
  ai_provider: 'deepseek',
  market_data_source: 'yfinance',
  option_data_source: 'thetadata',
  use_ai: true,
  council: true,
  alert_mode: 'best_per_run',
  max_alerts_per_day: 5,
  max_ai_scans_per_day: 10,
  ai_scan_policy: 'prefilter_matched',
  ai_scan_top_n: 3,
  eod_review_enabled: true,
  eod_run_time_et: '16:20',
  weekend_review_enabled: false,
  strategy_family: 'spread',
  gex_filter_enabled: false,
  gex_regime: 'negative_gamma',
  gex_wall_type: 'any',
  gex_max_wall_distance_pct: '',
  gex_structural_risk: 'trend_acceleration',
  gex_alert_rule_enabled: false,
  notification_channel_ids: [],
};

export const scanLoopRuleTemplates = [
  {
    id: 'ai_spy_intraday_general',
    title: t('watchlist2.tpl.spyIntradayTitle'),
    desc: t('watchlist2.tpl.spyIntradayDesc'),
    params: t('watchlist2.tpl.spyIntradayParams'),
    watchlistPatch: {
      name: t('watchlist2.tpl.spyIntradayWatchlistName'),
      symbols: 'SPY',
    },
    patch: {
      name: t('watchlist2.tpl.spyIntradayInstanceName'),
      symbols: 'SPY',
      interval_minutes: 5,
      rule_template: 'ai_spy_intraday_general',
      prompt_template: [
        '你是 SPY 盘中实时交易台的结构分析师，每拍输出 120-200 字中文简报。',
        '硬性要求：',
        '1) 必须把 previous_report.text（如有）当作上一拍剧本，新输出要明确写「沿用 / 局部修订 / 推翻重写」之一，并说明触发的具体结构变化（VWAP、Call/Put Wall、Gamma Flip、POC/VAH/VAL 中的哪一个移动了或被穿越）。',
        '2) 重点叙述 GEX 结构：现价相对 Call Wall / Put Wall / Gamma Flip 的位置与距离百分比，gamma 区间属于 positive / negative / neutral，对盘中波动的含义。',
        '3) 重点叙述筹码分布：POC / VAH / VAL / VWAP 之间是否形成 acceptance 或 rejection，量价是否扩张。',
        '4) 给出基准 / 次情形 / 偏强 / 真弱 四档概率，并标明每档的触发线与 invalidation 价位。',
        '5) 严禁照抄上一拍措辞或编造未接入的 HIRO、IV、25Δ Skew 数据；缺数据必须明写「未接入」。',
        '输出格式：纯文本叙述 + 最末一行 `本拍要点：XXX`。',
      ].join('\n'),
      prefilter_logic: 'and',
      alert_logic: 'or',
      min_rvol: 0.05,
      alert_min_rvol: 1.1,
      price_rule_enabled: false,
      technical_rule_enabled: false,
      option_rule_enabled: false,
      alert_mode: 'best_per_run',
      max_alerts_per_day: 12,
      // 5min × 6.5h ≈ 78 calls/day，再留 20% buffer
      max_ai_scans_per_day: 100,
      ai_scan_policy: 'always',
      ai_scan_top_n: 1,
      eod_review_enabled: true,
      weekend_review_enabled: false,
      strategy_family: 'spread',
      gex_filter_enabled: false,
      gex_alert_rule_enabled: false,
      gex_regime: 'any',
      gex_wall_type: 'any',
      gex_max_wall_distance_pct: '',
      gex_structural_risk: 'any',
      use_ai: true,
      council: true,
    },
  },
  {
    id: 'ai_opening_drive',
    title: t('watchlist2.tpl.openingDriveTitle'),
    desc: t('watchlist2.tpl.openingDriveDesc'),
    params: '10m · RVOL 1.3/2.0 · VWAP +0.25% · Spread <=10%',
    patch: {
      name: t('watchlist2.tpl.openingDriveInstanceName'),
      interval_minutes: 10,
      rule_template: 'ai_opening_drive',
      prefilter_logic: 'and',
      alert_logic: 'and',
      min_rvol: 1.3,
      alert_min_rvol: 2,
      technical_rule_enabled: true,
      technical_rule_field: 'underlying_vs_vwap_pct',
      technical_rule_operator: '>=',
      technical_rule_value: '0.25',
      option_rule_enabled: true,
      option_rule_field: 'bid_ask_spread_pct',
      option_rule_operator: '<=',
      option_rule_value: '10',
      alert_mode: 'best_per_run',
      max_alerts_per_day: 4,
      max_ai_scans_per_day: 8,
      ai_scan_policy: 'top_n_per_run',
      ai_scan_top_n: 3,
      eod_review_enabled: true,
      weekend_review_enabled: false,
      strategy_family: 'spread',
      gex_filter_enabled: true,
      gex_alert_rule_enabled: false,
      gex_regime: 'negative_gamma',
      gex_wall_type: 'any',
      gex_max_wall_distance_pct: '2',
      gex_structural_risk: 'trend_acceleration',
      use_ai: true,
      council: true,
    },
  },
  {
    id: 'ai_vwap_reclaim_liquidity',
    title: t('watchlist2.tpl.vwapReclaimTitle'),
    desc: t('watchlist2.tpl.vwapReclaimDesc'),
    params: '15m · RVOL 0.9/1.25 · VWAP -0.15% · Spread <=8%',
    patch: {
      name: t('watchlist2.tpl.vwapReclaimInstanceName'),
      interval_minutes: 15,
      rule_template: 'ai_vwap_reclaim_liquidity',
      prefilter_logic: 'and',
      alert_logic: 'and',
      min_rvol: 0.9,
      alert_min_rvol: 1.25,
      technical_rule_enabled: true,
      technical_rule_field: 'underlying_vs_vwap_pct',
      technical_rule_operator: '>=',
      technical_rule_value: '-0.15',
      option_rule_enabled: true,
      option_rule_field: 'bid_ask_spread_pct',
      option_rule_operator: '<=',
      option_rule_value: '8',
      alert_mode: 'best_per_run',
      max_alerts_per_day: 3,
      max_ai_scans_per_day: 6,
      ai_scan_policy: 'alert_matched',
      ai_scan_top_n: 2,
      eod_review_enabled: true,
      weekend_review_enabled: false,
      strategy_family: 'spread',
      gex_filter_enabled: true,
      gex_alert_rule_enabled: false,
      gex_regime: 'any',
      gex_wall_type: 'any',
      gex_max_wall_distance_pct: '1.5',
      gex_structural_risk: 'pinning_high',
      use_ai: true,
      council: true,
    },
  },
  {
    id: 'credit_spread_income',
    title: t('watchlist2.tpl.creditSpreadTitle'),
    desc: t('watchlist2.tpl.creditSpreadDesc'),
    params: t('watchlist2.tpl.creditSpreadParams'),
    patch: {
      name: t('watchlist2.tpl.creditSpreadInstanceName'),
      interval_minutes: 20,
      rule_template: 'credit_spread_income',
      prompt_template: '扫描{symbol}是否适合定义风险信用价差收租。必须先判断方向：若价格在VWAP上方、靠近Put Wall/支撑、正Gamma或Pinning环境更适合Bull Put Credit Spread；若价格在VWAP下方、靠近Call Wall/阻力、上方压制更明确则适合Bear Call Credit Spread；若方向、波动率或流动性不足则观望。最终方案必须是credit spread，不得给单腿期权。',
      prefilter_logic: 'and',
      alert_logic: 'and',
      min_rvol: 0.8,
      alert_min_rvol: 1.2,
      technical_rule_enabled: false,
      option_rule_enabled: true,
      option_rule_field: 'bid_ask_spread_pct',
      option_rule_operator: '<=',
      option_rule_value: '12',
      alert_mode: 'best_per_run',
      max_alerts_per_day: 3,
      max_ai_scans_per_day: 6,
      ai_scan_policy: 'alert_matched',
      ai_scan_top_n: 2,
      eod_review_enabled: true,
      weekend_review_enabled: false,
      strategy_family: 'credit_spread',
      gex_filter_enabled: true,
      gex_alert_rule_enabled: true,
      gex_regime: 'positive_gamma',
      gex_wall_type: 'any',
      gex_max_wall_distance_pct: '2',
      gex_structural_risk: 'pinning_high',
      use_ai: true,
      council: true,
    },
  },
  {
    id: 'breakout',
    title: t('watchlist2.tpl.breakoutTitle'),
    desc: t('watchlist2.tpl.breakoutDesc'),
    patch: {
      name: t('watchlist2.tpl.breakoutInstanceName'),
      rule_template: 'breakout',
      prefilter_logic: 'and',
      alert_logic: 'and',
      min_rvol: 1.0,
      alert_min_rvol: 1.5,
      technical_rule_enabled: true,
      technical_rule_field: 'underlying_vs_vwap_pct',
      technical_rule_operator: '>=',
      technical_rule_value: '0',
      gex_structural_risk: 'trend_acceleration',
      strategy_family: 'spread',
      ai_scan_policy: 'prefilter_matched',
      ai_scan_top_n: 3,
    },
  },
  {
    id: 'vwap_reclaim',
    title: t('watchlist2.tpl.vwapReclaimBasicTitle'),
    desc: t('watchlist2.tpl.vwapReclaimBasicDesc'),
    patch: {
      name: t('watchlist2.tpl.vwapReclaimBasicInstanceName'),
      rule_template: 'vwap_reclaim',
      min_rvol: 0.8,
      alert_min_rvol: 1.1,
      technical_rule_enabled: true,
      technical_rule_field: 'underlying_vs_vwap_pct',
      technical_rule_operator: '>=',
      technical_rule_value: '-0.2',
      strategy_family: 'spread',
      ai_scan_policy: 'prefilter_matched',
      ai_scan_top_n: 2,
    },
  },
  {
    id: 'rvol_momentum',
    title: t('watchlist2.tpl.rvolMomentumTitle'),
    desc: t('watchlist2.tpl.rvolMomentumDesc'),
    patch: {
      name: t('watchlist2.tpl.rvolMomentumInstanceName'),
      rule_template: 'rvol_momentum',
      min_rvol: 1.2,
      alert_min_rvol: 2,
      technical_rule_enabled: false,
      option_rule_enabled: false,
      strategy_family: 'single_leg',
      ai_scan_policy: 'top_n_per_run',
      ai_scan_top_n: 4,
    },
  },
  {
    id: 'option_liquidity',
    title: t('watchlist2.tpl.optionLiquidityTitle'),
    desc: t('watchlist2.tpl.optionLiquidityDesc'),
    patch: {
      name: t('watchlist2.tpl.optionLiquidityInstanceName'),
      rule_template: 'option_liquidity',
      min_rvol: 0.8,
      alert_min_rvol: 1.0,
      option_rule_enabled: true,
      option_rule_field: 'bid_ask_spread_pct',
      option_rule_operator: '<=',
      option_rule_value: '12',
      strategy_family: 'spread',
      ai_scan_policy: 'alert_matched',
      ai_scan_top_n: 2,
    },
  },
  {
    id: 'gex_structure',
    title: t('watchlist2.tpl.gexStructureTitle'),
    desc: t('watchlist2.tpl.gexStructureDesc'),
    patch: {
      name: t('watchlist2.tpl.gexStructureInstanceName'),
      rule_template: 'gex_structure',
      min_rvol: 0.8,
      alert_min_rvol: 1.2,
      gex_filter_enabled: true,
      gex_alert_rule_enabled: true,
      gex_regime: 'any',
      gex_max_wall_distance_pct: '1.5',
      strategy_family: 'spread',
      ai_scan_policy: 'smart_budget',
      ai_scan_top_n: 3,
    },
  },
  {
    id: 'weekend_plan',
    title: t('watchlist2.tpl.weekendPlanTitle'),
    desc: t('watchlist2.tpl.weekendPlanDesc'),
    patch: {
      name: t('watchlist2.tpl.weekendPlanInstanceName'),
      rule_template: 'weekend_plan',
      min_rvol: 0.7,
      alert_min_rvol: 1.2,
      eod_review_enabled: true,
      weekend_review_enabled: true,
      alert_mode: 'daily_digest',
      use_ai: true,
      council: true,
      ai_scan_policy: 'smart_budget',
      ai_scan_top_n: 2,
    },
  },
];

function appendGexConditions(conditions, form, { includeRisk = true } = {}) {
  if (form.gex_regime && form.gex_regime !== 'any') {
    conditions.push({ field: 'gex.regime', operator: '==', value: form.gex_regime });
  } else {
    conditions.push({ field: 'gex.regime', operator: '!=', value: 'unknown' });
  }
  if (form.gex_wall_type && form.gex_wall_type !== 'any') {
    conditions.push({ field: 'gex.nearest_wall', operator: '==', value: form.gex_wall_type });
  }
  const wallDistance = Number(form.gex_max_wall_distance_pct);
  if (Number.isFinite(wallDistance) && wallDistance > 0) {
    conditions.push({ field: 'gex.nearest_wall_distance_pct', operator: '<=', value: wallDistance });
  }
  if (includeRisk && form.gex_structural_risk && form.gex_structural_risk !== 'any') {
    const field = form.gex_structural_risk === 'pinning_high'
      ? 'gex.pinning_risk'
      : 'gex.trend_acceleration_risk';
    conditions.push({ field, operator: 'in', value: ['medium', 'high'] });
  }
}

function conditionValue(rules, field, operator = null) {
  const condition = (rules?.conditions || []).find((item) => item?.field === field && (!operator || item.operator === operator));
  return condition?.value;
}

function conditionFor(rules, fields) {
  const names = Array.isArray(fields) ? fields : [fields];
  return (rules?.conditions || []).find((item) => names.includes(item?.field));
}

function hasGexCondition(rules) {
  return (rules?.conditions || []).some((item) => String(item?.field || '').startsWith('gex.'));
}

function strategyFamilyFromModes(modes = []) {
  if (modes.includes('single_leg')) return 'single_leg';
  if (modes.includes('iron_condor')) return 'income';
  if (modes.includes('credit_spread')) return 'credit_spread';
  return 'spread';
}

function strategyModesFromFamily(family) {
  if (family === 'credit_spread') return ['credit_spread'];
  if (family === 'single_leg') return ['single_leg'];
  if (family === 'income') return ['iron_condor', 'credit_spread'];
  return ['spread'];
}

export const aiScanPolicyLabels = {
  always: t('watchlist2.aiPolicy.always'),
  prefilter_matched: t('watchlist2.aiPolicy.prefilter_matched'),
  alert_matched: t('watchlist2.aiPolicy.alert_matched'),
  top_n_per_run: t('watchlist2.aiPolicy.top_n_per_run'),
  smart_budget: t('watchlist2.aiPolicy.smart_budget'),
};

function formFromInstance(instance) {
  const prefilter = instance?.prefilter_rules || {};
  const alert = instance?.alert_rules || {};
  const schedule = instance?.schedule || {};
  const gexRegime = conditionValue(prefilter, 'gex.regime', '==');
  const gexWall = conditionValue(prefilter, 'gex.nearest_wall', '==');
  const wallDistance = conditionValue(prefilter, 'gex.nearest_wall_distance_pct');
  const trendRisk = conditionValue(prefilter, 'gex.trend_acceleration_risk');
  const pinningRisk = conditionValue(prefilter, 'gex.pinning_risk');
  const priceRule = conditionFor(prefilter, ['last', 'price']);
  const technicalRule = conditionFor(prefilter, ['underlying_vs_vwap_pct', 'vwap', 'orb_high', 'orb_low', 'ema_20', 'ema_50', 'ema_200', 'rsi', 'atr']);
  const optionRule = conditionFor(alert, ['ask', 'bid', 'mid', 'bid_ask_spread_pct', 'volume', 'open_interest']);
  return {
    ...defaultInstanceForm,
    name: instance?.name || defaultInstanceForm.name,
    symbols: Array.isArray(instance?.symbols) ? instance.symbols.join(', ') : defaultInstanceForm.symbols,
    prompt_template: instance?.prompt_template || defaultInstanceForm.prompt_template,
    interval_minutes: schedule.interval_minutes || defaultInstanceForm.interval_minutes,
    prefilter_logic: prefilter.logic || defaultInstanceForm.prefilter_logic,
    alert_logic: alert.logic || defaultInstanceForm.alert_logic,
    min_rvol: conditionValue(prefilter, 'rvol') ?? defaultInstanceForm.min_rvol,
    alert_min_rvol: conditionValue(alert, 'rvol') ?? defaultInstanceForm.alert_min_rvol,
    price_rule_enabled: Boolean(priceRule),
    price_rule_field: priceRule?.field || defaultInstanceForm.price_rule_field,
    price_rule_operator: priceRule?.operator || defaultInstanceForm.price_rule_operator,
    price_rule_value: priceRule?.value ?? '',
    technical_rule_enabled: Boolean(technicalRule),
    technical_rule_field: technicalRule?.field || defaultInstanceForm.technical_rule_field,
    technical_rule_operator: technicalRule?.operator || defaultInstanceForm.technical_rule_operator,
    technical_rule_value: technicalRule?.value ?? defaultInstanceForm.technical_rule_value,
    option_rule_enabled: Boolean(optionRule),
    option_rule_field: optionRule?.field || defaultInstanceForm.option_rule_field,
    option_rule_operator: optionRule?.operator || defaultInstanceForm.option_rule_operator,
    option_rule_value: optionRule?.value ?? defaultInstanceForm.option_rule_value,
    ai_provider: instance?.ai_provider || defaultInstanceForm.ai_provider,
    market_data_source: instance?.market_data_source || defaultInstanceForm.market_data_source,
    option_data_source: instance?.option_data_source || defaultInstanceForm.option_data_source,
    use_ai: Boolean(instance?.use_ai),
    council: Boolean(instance?.council),
    alert_mode: instance?.alert_mode || defaultInstanceForm.alert_mode,
    max_alerts_per_day: instance?.max_alerts_per_day ?? defaultInstanceForm.max_alerts_per_day,
    max_ai_scans_per_day: instance?.max_ai_scans_per_day ?? defaultInstanceForm.max_ai_scans_per_day,
    ai_scan_policy: instance?.ai_scan_policy || defaultInstanceForm.ai_scan_policy,
    ai_scan_top_n: instance?.ai_scan_top_n ?? defaultInstanceForm.ai_scan_top_n,
    eod_review_enabled: Boolean(instance?.eod_review_enabled),
    eod_run_time_et: instance?.eod_run_time_et || defaultInstanceForm.eod_run_time_et,
    weekend_review_enabled: Boolean(instance?.weekend_review_enabled),
    strategy_family: strategyFamilyFromModes(instance?.strategy_modes || []),
    gex_filter_enabled: hasGexCondition(prefilter),
    gex_regime: gexRegime || (conditionValue(prefilter, 'gex.regime', '!=') ? 'any' : defaultInstanceForm.gex_regime),
    gex_wall_type: gexWall || 'any',
    gex_max_wall_distance_pct: wallDistance ?? '',
    gex_structural_risk: Array.isArray(pinningRisk) ? 'pinning_high' : Array.isArray(trendRisk) ? 'trend_acceleration' : 'any',
    gex_alert_rule_enabled: hasGexCondition(alert),
    notification_channel_ids: Array.isArray(instance?.notification_channel_ids) ? instance.notification_channel_ids : [],
  };
}

export function useWatchlistRadar({
  api,
  providers = [],
  refreshTriggers = () => {},
  selectedInstanceId: initialSelectedInstanceId = '',
}) {
  const [watchlists, setWatchlists] = useState([]);
  const [instances, setInstances] = useState([]);
  const [runs, setRuns] = useState([]);
  const [channels, setChannels] = useState([]);
  const [events, setEvents] = useState([]);
  const [opportunities, setOpportunities] = useState([]);
  const [opportunityDetails, setOpportunityDetails] = useState({});
  const [expandedOpportunityId, setExpandedOpportunityId] = useState('');
  const [selectedInstanceId, setSelectedInstanceId] = useState(initialSelectedInstanceId);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [followupBusy, setFollowupBusy] = useState(false);
  const [opportunityBusyId, setOpportunityBusyId] = useState('');
  const [opportunityDetailBusyId, setOpportunityDetailBusyId] = useState('');
  const [selectedOpportunityId, setSelectedOpportunityId] = useState('');
  const [observationHealth, setObservationHealth] = useState(null);
  const [observationBusy, setObservationBusy] = useState(false);
  const [ruleTestResult, setRuleTestResult] = useState(null);
  const [ruleTestBusy, setRuleTestBusy] = useState(false);
  const [notificationPreview, setNotificationPreview] = useState(null);
  const [notificationPreviewBusy, setNotificationPreviewBusy] = useState(false);
  const [notificationBusyId, setNotificationBusyId] = useState('');
  const [testingChannelId, setTestingChannelId] = useState('');
  const [selectedWatchlistId, setSelectedWatchlistId] = useState('');
  const [editingWatchlistId, setEditingWatchlistId] = useState('');
  const [editingInstanceId, setEditingInstanceId] = useState('');
  const [watchlistForm, setWatchlistForm] = useState({ name: t('watchlist2.defaultWatchlistName'), symbols: defaultSymbols });
  const [channelForm, setChannelForm] = useState({ type: 'email', email: '', url: '', secret: '' });
  const [instanceForm, setInstanceForm] = useState(defaultInstanceForm);

  const selectedWatchlist = useMemo(
    () => (selectedWatchlistId === '__all__' ? null : watchlists.find((item) => item.id === selectedWatchlistId) || watchlists[0] || null),
    [watchlists, selectedWatchlistId],
  );
  const visibleInstances = useMemo(
    () => (selectedWatchlistId === '__all__' || !selectedWatchlist ? instances : instances.filter((item) => item.watchlist_id === selectedWatchlist.id)),
    [instances, selectedWatchlist, selectedWatchlistId],
  );

  const selectedInstance = useMemo(
    () => visibleInstances.find((item) => item.id === selectedInstanceId) || visibleInstances[0] || null,
    [visibleInstances, selectedInstanceId],
  );

  useEffect(() => {
    refreshAll();
  }, []);

  async function refreshAll(preferred = {}) {
    setError('');
    const [watchlistRows, instanceRows, channelRows, eventRows, opportunityRows, healthRows] = await Promise.all([
      api('/api/watchlists'),
      api('/api/scan-loop-instances'),
      api('/api/notification-channels'),
      api('/api/notification-events?limit=20'),
      api('/api/opportunities?limit=20'),
      api('/api/observation-health'),
    ]);
    setWatchlists(watchlistRows);
    setInstances(instanceRows);
    setChannels(channelRows);
    setEvents(eventRows);
    setOpportunities(opportunityRows);
    setObservationHealth(healthRows);
    const preferredWatchlistId = preferred.watchlistId || selectedWatchlistId;
    const preferredInstanceId = preferred.instanceId || selectedInstanceId;
    const nextWatchlist = preferredWatchlistId === '__all__' ? null : watchlistRows.find((item) => item.id === preferredWatchlistId) || watchlistRows[0] || null;
    const scopedInstances = preferredWatchlistId === '__all__' || !nextWatchlist ? instanceRows : instanceRows.filter((item) => item.watchlist_id === nextWatchlist.id);
    const nextInstance = scopedInstances.find((item) => item.id === preferredInstanceId) || scopedInstances[0] || null;
    setSelectedWatchlistId(preferredWatchlistId === '__all__' ? '__all__' : nextWatchlist?.id || '');
    setSelectedInstanceId(nextInstance?.id || '');
    if (nextInstance) {
      const runRows = await api(`/api/scan-loop-instances/${encodeURIComponent(nextInstance.id)}/runs?limit=10`);
      setRuns(runRows);
    } else {
      setRuns([]);
    }
    await refreshTriggers();
  }

  async function refreshObservationHealth() {
    const health = await api('/api/observation-health');
    setObservationHealth(health);
    return health;
  }

  async function runObservationDueCycle() {
    setObservationBusy(true);
    setError('');
    try {
      const result = await api('/api/observation-health/run-due-cycle', {
        method: 'POST',
        body: JSON.stringify({ scan_limit: 5, trigger_limit: 30, opportunity_limit: 30 }),
      });
      setObservationHealth({ ...(result.health || {}), status: 'ok', last_manual_cycle: result.result });
      await refreshAll();
    } catch (healthError) {
      setError(healthError.message);
    } finally {
      setObservationBusy(false);
    }
  }

  async function fetchRunsForInstance(instanceId) {
    if (!instanceId) {
      setRuns([]);
      return;
    }
    const runRows = await api(`/api/scan-loop-instances/${encodeURIComponent(instanceId)}/runs?limit=10`);
    setRuns(runRows);
  }

  async function selectInstance(instanceId) {
    setSelectedInstanceId(instanceId || '');
    await fetchRunsForInstance(instanceId);
  }

  async function selectWatchlist(watchlistId) {
    setSelectedWatchlistId(watchlistId || '');
    const nextInstance = watchlistId === '__all__' ? instances[0] || null : instances.find((item) => item.watchlist_id === watchlistId) || null;
    setSelectedInstanceId(nextInstance?.id || '');
    await fetchRunsForInstance(nextInstance?.id || '');
  }

  async function createWatchlist(event) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      const endpoint = editingWatchlistId ? `/api/watchlists/${encodeURIComponent(editingWatchlistId)}` : '/api/watchlists';
      const method = editingWatchlistId ? 'PATCH' : 'POST';
      const row = await api(endpoint, {
        method,
        body: JSON.stringify({
          name: watchlistForm.name,
          symbols: watchlistForm.symbols,
        }),
      });
      setSelectedWatchlistId(row.id);
      setEditingWatchlistId('');
      setWatchlistForm({ name: t('watchlist2.defaultWatchlistName'), symbols: defaultSymbols });
      await refreshAll({ watchlistId: row.id });
    } catch (formError) {
      setError(formError.message);
    } finally {
      setBusy(false);
    }
  }

  function editWatchlist(watchlist) {
    setEditingWatchlistId(watchlist.id);
    setSelectedWatchlistId(watchlist.id);
    setWatchlistForm({ name: watchlist.name || '', symbols: (watchlist.symbols || []).join(', ') });
  }

  function cancelWatchlistEdit() {
    setEditingWatchlistId('');
    setWatchlistForm({ name: t('watchlist2.defaultWatchlistName'), symbols: defaultSymbols });
  }

  async function duplicateWatchlist(watchlist) {
    setBusy(true);
    setError('');
    try {
      const row = await api('/api/watchlists', {
        method: 'POST',
        body: JSON.stringify({
          name: `${watchlist.name || t('watchlist2.watchlistFallback')} Copy`,
          symbols: watchlist.symbols || [],
        }),
      });
      setSelectedWatchlistId(row.id);
      await refreshAll({ watchlistId: row.id });
    } catch (formError) {
      setError(formError.message);
    } finally {
      setBusy(false);
    }
  }

  async function deleteWatchlist(watchlist) {
    if (!watchlist?.id || !window.confirm(t('watchlist2.confirmDeleteWatchlist').replace('{name}', watchlist.name))) return;
    setBusy(true);
    setError('');
    try {
      await api(`/api/watchlists/${encodeURIComponent(watchlist.id)}`, { method: 'DELETE' });
      if (selectedWatchlistId === watchlist.id) setSelectedWatchlistId('');
      if (editingWatchlistId === watchlist.id) cancelWatchlistEdit();
      await refreshAll();
    } catch (formError) {
      setError(formError.message);
    } finally {
      setBusy(false);
    }
  }

  async function createChannel(event) {
    event.preventDefault();
    if (channelForm.type === 'email' && !channelForm.email.trim()) return;
    if (channelForm.type === 'webhook' && !channelForm.url.trim()) return;
    setBusy(true);
    setError('');
    try {
      await api('/api/notification-channels', {
        method: 'POST',
        body: JSON.stringify(
          channelForm.type === 'webhook'
            ? { type: 'webhook', label: channelForm.url.trim(), url: channelForm.url.trim(), secret: channelForm.secret.trim() }
            : { type: 'email', label: channelForm.email.trim(), email: channelForm.email.trim() },
        ),
      });
      setChannelForm({ type: channelForm.type || 'email', email: '', url: '', secret: '' });
      await refreshAll();
    } catch (formError) {
      setError(formError.message);
    } finally {
      setBusy(false);
    }
  }

  async function testChannel(channelId) {
    setTestingChannelId(channelId);
    setError('');
    try {
      const result = await api(`/api/notification-channels/${encodeURIComponent(channelId)}/test`, { method: 'POST' });
      setChannels((current) => current.map((item) => (item.id === channelId ? result.channel : item)));
      await refreshAll();
      if (result.event?.status !== 'sent') {
        setError(result.event?.last_error || t('watchlist2.channelTestFailed'));
      }
    } catch (testError) {
      setError(testError.message);
    } finally {
      setTestingChannelId('');
    }
  }

  async function sendNotificationEvent(eventId) {
    setNotificationBusyId(eventId);
    setError('');
    try {
      const result = await api(`/api/notification-events/${encodeURIComponent(eventId)}/send`, { method: 'POST' });
      await refreshAll();
      if (result.status !== 'sent') {
        setError(result.last_error || t('watchlist2.notificationSendFailed'));
      }
    } catch (notificationError) {
      setError(notificationError.message);
    } finally {
      setNotificationBusyId('');
    }
  }

  async function updateOpportunity(opportunityId, path, body = null) {
    setOpportunityBusyId(opportunityId);
    setError('');
    try {
      await api(`/api/opportunities/${encodeURIComponent(opportunityId)}${path}`, body ? {
        method: path ? 'POST' : 'PATCH',
        body: body ? JSON.stringify(body) : undefined,
      } : {
        method: path ? 'POST' : 'PATCH',
      });
      await refreshAll();
    } catch (opportunityError) {
      setError(opportunityError.message);
    } finally {
      setOpportunityBusyId('');
    }
  }

  async function patchOpportunity(opportunityId, body) {
    setOpportunityBusyId(opportunityId);
    setError('');
    try {
      await api(`/api/opportunities/${encodeURIComponent(opportunityId)}`, {
        method: 'PATCH',
        body: JSON.stringify(body),
      });
      await refreshAll();
    } catch (opportunityError) {
      setError(opportunityError.message);
    } finally {
      setOpportunityBusyId('');
    }
  }

  async function toggleOpportunityDetail(item) {
    if (!item?.id) return;
    if (expandedOpportunityId === item.id) {
      setExpandedOpportunityId('');
      return;
    }
    setExpandedOpportunityId(item.id);
    if (opportunityDetails[item.id]) return;
    setOpportunityDetailBusyId(item.id);
    setError('');
    try {
      const detail = await api(`/api/opportunities/${encodeURIComponent(item.id)}`);
      setOpportunityDetails((current) => ({ ...current, [item.id]: detail }));
    } catch (detailError) {
      setError(detailError.message);
    } finally {
      setOpportunityDetailBusyId('');
    }
  }

  async function openOpportunityModal(item) {
    if (!item?.id) return;
    setSelectedOpportunityId(item.id);
    if (opportunityDetails[item.id]) return;
    setOpportunityDetailBusyId(item.id);
    setError('');
    try {
      const detail = await api(`/api/opportunities/${encodeURIComponent(item.id)}`);
      setOpportunityDetails((current) => ({ ...current, [item.id]: detail }));
    } catch (detailError) {
      setError(detailError.message);
    } finally {
      setOpportunityDetailBusyId('');
    }
  }

  function closeOpportunityModal() {
    setSelectedOpportunityId('');
  }

  async function markOpportunityWatching(opportunityId) {
    await patchOpportunity(opportunityId, { status: 'watching_entry' });
  }

  async function markOpportunityActive(opportunityId) {
    await patchOpportunity(opportunityId, { status: 'active_reference' });
  }

  async function pauseOpportunity(opportunityId) {
    await updateOpportunity(opportunityId, '/pause');
  }

  async function resumeOpportunity(opportunityId) {
    await updateOpportunity(opportunityId, '/resume');
  }

  async function archiveOpportunity(opportunityId) {
    await updateOpportunity(opportunityId, '/archive');
  }

  async function reviewOpportunity(item) {
    const fallback = item.entry_reference?.underlying_reference ?? item.trigger_snapshot?.last ?? '';
    const value = window.prompt(t('watchlist2.promptCurrentRef'), fallback === '' ? '' : String(fallback));
    if (value === null) return;
    const last = Number(value);
    if (!Number.isFinite(last) || last <= 0) {
      setError(t('watchlist2.invalidCurrentRef'));
      return;
    }
    setOpportunityBusyId(item.id);
    setError('');
    try {
      await api(`/api/opportunities/${encodeURIComponent(item.id)}/check`, {
        method: 'POST',
        body: JSON.stringify({
          quote_snapshot: {
            symbol: item.symbol,
            last,
            market_state: 'regular_open',
            data_timestamp: new Date().toISOString(),
            source: 'manual_review',
          },
        }),
      });
      await refreshAll();
    } catch (opportunityError) {
      setError(opportunityError.message);
    } finally {
      setOpportunityBusyId('');
    }
  }

  async function adjustOpportunityRiskPlan(item) {
    if (!item?.id) return;
    const risk = item.risk_plan || {};
    const firstTakeProfit = Array.isArray(risk.take_profit?.levels) ? risk.take_profit.levels[0] : null;
    const currentTp = firstTakeProfit?.underlying_reference ?? item.entry_reference?.underlying_reference ?? '';
    const currentStop = risk.stop_loss?.underlying_reference ?? '';
    const tpInput = window.prompt(t('watchlist2.promptTp1'), currentTp === '' ? '' : String(currentTp));
    if (tpInput === null) return;
    const stopInput = window.prompt(t('watchlist2.promptStop'), currentStop === '' ? '' : String(currentStop));
    if (stopInput === null) return;
    const tp = Number(tpInput);
    const stop = Number(stopInput);
    if (!Number.isFinite(tp) || tp <= 0 || !Number.isFinite(stop) || stop <= 0) {
      setError(t('watchlist2.invalidTpStop'));
      return;
    }
    const nextRisk = {
      ...risk,
      take_profit: {
        ...(risk.take_profit || {}),
        levels: [
          { ...(firstTakeProfit || {}), label: firstTakeProfit?.label || 'TP1', underlying_reference: tp },
          ...(Array.isArray(risk.take_profit?.levels) ? risk.take_profit.levels.slice(1) : []),
        ],
      },
      stop_loss: {
        ...(risk.stop_loss || {}),
        type: risk.stop_loss?.type || 'underlying_reference',
        underlying_reference: stop,
      },
    };
    await patchOpportunity(item.id, { risk_plan: nextRisk });
  }

  async function saveOpportunityRiskPlan(item, riskPlan) {
    if (!item?.id) return;
    await patchOpportunity(item.id, { risk_plan: riskPlan });
    const detail = await api(`/api/opportunities/${encodeURIComponent(item.id)}`);
    setOpportunityDetails((current) => ({ ...current, [item.id]: detail }));
  }

  function applyInstanceTemplate(templateId) {
    const template = scanLoopRuleTemplates.find((item) => item.id === templateId);
    if (!template) return;
    setInstanceForm((current) => ({ ...current, ...template.patch }));
    if (template.watchlistPatch) {
      setWatchlistForm((current) => ({ ...current, ...template.watchlistPatch }));
    }
  }

  function appendAdvancedRules(target, form, scope) {
    if (scope === 'prefilter' && form.price_rule_enabled && form.price_rule_value !== '') {
      target.push({ field: form.price_rule_field, operator: form.price_rule_operator, value: Number(form.price_rule_value) });
    }
    if (scope === 'prefilter' && form.technical_rule_enabled && form.technical_rule_value !== '') {
      target.push({ field: form.technical_rule_field, operator: form.technical_rule_operator, value: Number(form.technical_rule_value) });
    }
    if (scope === 'alert' && form.option_rule_enabled && form.option_rule_value !== '') {
      target.push({ field: form.option_rule_field, operator: form.option_rule_operator, value: Number(form.option_rule_value) });
    }
  }


  function buildInstancePayload(form, watchlist, status = 'active', nameOverride = '') {
    const provider = providers.some((item) => item.name === form.ai_provider) ? form.ai_provider : providers[0]?.name || 'deepseek';
    const prefilterConditions = [
      { field: 'rvol', operator: '>=', value: Number(form.min_rvol) || 1 },
    ];
    appendAdvancedRules(prefilterConditions, form, 'prefilter');
    if (form.gex_filter_enabled) {
      appendGexConditions(prefilterConditions, form);
    }
    const alertConditions = [
      { field: 'rvol', operator: '>=', value: Number(form.alert_min_rvol) || Number(form.min_rvol) || 1 },
    ];
    appendAdvancedRules(alertConditions, form, 'alert');
    if (form.gex_filter_enabled && form.gex_alert_rule_enabled) {
      appendGexConditions(alertConditions, form);
    }
    return {
      watchlist_id: watchlist.id,
      ...(form.symbols ? { symbols: form.symbols } : {}),
      name: nameOverride || form.name,
      status,
      schedule: { type: 'interval_minutes', interval_minutes: Number(form.interval_minutes) || 30, skip_missed_runs: true },
      market_session: 'regular',
      eod_review_enabled: Boolean(form.eod_review_enabled),
      eod_run_time_et: form.eod_run_time_et || '16:20',
      weekend_review_enabled: Boolean(form.weekend_review_enabled),
      weekend_run_time_local: 'Sunday 18:00',
      market_data_source: form.market_data_source || 'yfinance',
      option_data_source: form.option_data_source || 'thetadata',
      ai_provider: provider,
      use_ai: form.use_ai,
      council: form.council,
      analysis_modules: defaultAnalysisModules,
      strategy_modes: strategyModesFromFamily(form.strategy_family),
      prompt_template: form.prompt_template || defaultInstanceForm.prompt_template,
      prefilter_rules: {
        logic: form.prefilter_logic || 'and',
        conditions: prefilterConditions,
      },
      alert_rules: {
        logic: form.alert_logic || 'and',
        conditions: alertConditions,
      },
      alert_mode: form.alert_mode,
      max_alerts_per_day: Number(form.max_alerts_per_day) || 5,
      max_ai_scans_per_day: Number(form.max_ai_scans_per_day) || 10,
      notification_channel_ids: Array.isArray(form.notification_channel_ids) ? form.notification_channel_ids : [],
      ai_scan_policy: form.ai_scan_policy || 'prefilter_matched',
      ai_scan_top_n: Number(form.ai_scan_top_n) || 3,
    };
  }

  async function createInstance(event) {
    event.preventDefault();
    const watchlist = selectedWatchlist;
    if (!watchlist) {
      setError(t('watchlist2.createWatchlistFirst'));
      return;
    }
    setBusy(true);
    setError('');
    try {
      const existing = editingInstanceId ? instances.find((item) => item.id === editingInstanceId) : null;
      const payload = buildInstancePayload(instanceForm, watchlist, existing?.status || 'active');
      const row = await api(editingInstanceId ? `/api/scan-loop-instances/${encodeURIComponent(editingInstanceId)}` : '/api/scan-loop-instances', {
        method: editingInstanceId ? 'PATCH' : 'POST',
        body: JSON.stringify(payload),
      });
      setEditingInstanceId('');
      setInstanceForm(defaultInstanceForm);
      setSelectedInstanceId(row.id);
      await refreshAll({ watchlistId: watchlist.id, instanceId: row.id });
    } catch (formError) {
      setError(formError.message);
    } finally {
      setBusy(false);
    }
  }

  function editInstance(instance) {
    setEditingInstanceId(instance.id);
    if (instance.watchlist_id) setSelectedWatchlistId(instance.watchlist_id);
    setSelectedInstanceId(instance.id);
    setInstanceForm(formFromInstance(instance));
    fetchRunsForInstance(instance.id);
  }

  function cancelInstanceEdit() {
    setEditingInstanceId('');
    setInstanceForm(defaultInstanceForm);
  }

  async function duplicateInstance(instance) {
    const watchlist = watchlists.find((item) => item.id === instance.watchlist_id) || selectedWatchlist;
    if (!watchlist) {
      setError(t('watchlist2.selectWatchlistBeforeDuplicate'));
      return;
    }
    setBusy(true);
    setError('');
    try {
      const form = formFromInstance(instance);
      const row = await api('/api/scan-loop-instances', {
        method: 'POST',
        body: JSON.stringify(buildInstancePayload(form, watchlist, 'paused', `${instance.name || t('watchlist2.instanceFallback')} Copy`)),
      });
      setSelectedWatchlistId(watchlist.id);
      setSelectedInstanceId(row.id);
      await refreshAll({ watchlistId: watchlist.id, instanceId: row.id });
    } catch (formError) {
      setError(formError.message);
    } finally {
      setBusy(false);
    }
  }

  async function deleteInstance(instance) {
    if (!instance?.id || !window.confirm(t('watchlist2.confirmDeleteInstance').replace('{name}', instance.name))) return;
    setBusy(true);
    setError('');
    try {
      await api(`/api/scan-loop-instances/${encodeURIComponent(instance.id)}`, { method: 'DELETE' });
      if (selectedInstanceId === instance.id) setSelectedInstanceId('');
      if (editingInstanceId === instance.id) cancelInstanceEdit();
      await refreshAll();
    } catch (formError) {
      setError(formError.message);
    } finally {
      setBusy(false);
    }
  }

  async function toggleInstanceStatus(instance) {
    const nextStatus = instance.status === 'active' ? 'paused' : 'active';
    setBusy(true);
    setError('');
    try {
      const form = formFromInstance(instance);
      const watchlist = watchlists.find((item) => item.id === instance.watchlist_id) || selectedWatchlist;
      await api(`/api/scan-loop-instances/${encodeURIComponent(instance.id)}`, {
        method: 'PATCH',
        body: JSON.stringify(buildInstancePayload(form, watchlist || { id: instance.watchlist_id }, nextStatus)),
      });
      await refreshAll();
    } catch (formError) {
      setError(formError.message);
    } finally {
      setBusy(false);
    }
  }

  async function runSelectedInstance() {
    if (!selectedInstance) return;
    setBusy(true);
    setError('');
    try {
      const row = await api(`/api/scan-loop-instances/${encodeURIComponent(selectedInstance.id)}/run-now`, {
        method: 'POST',
        body: JSON.stringify({ allow_non_regular: true, submit_scans: true, review_only: true }),
      });
      setRuns((current) => [row, ...current.filter((item) => item.id !== row.id)]);
      await refreshAll();
    } catch (runError) {
      setError(runError.message);
    } finally {
      setBusy(false);
    }
  }

  async function testSelectedInstanceRules() {
    if (!selectedInstance) return;
    setRuleTestBusy(true);
    setError('');
    try {
      const result = await api(`/api/scan-loop-instances/${encodeURIComponent(selectedInstance.id)}/test-rules`, {
        method: 'POST',
        body: JSON.stringify({ allow_non_regular: true, submit_scans: false, review_only: true }),
      });
      setRuleTestResult(result);
    } catch (testError) {
      setError(testError.message);
    } finally {
      setRuleTestBusy(false);
    }
  }

  async function testSelectedInstanceNotificationPayload() {
    if (!selectedInstance) return;
    setNotificationPreviewBusy(true);
    setError('');
    try {
      const result = await api(`/api/scan-loop-instances/${encodeURIComponent(selectedInstance.id)}/notification-preview`, {
        method: 'POST',
      });
      setNotificationPreview(result);
    } catch (testError) {
      setError(testError.message);
    } finally {
      setNotificationPreviewBusy(false);
    }
  }

  async function processOpportunityFollowups() {
    setFollowupBusy(true);
    setError('');
    try {
      await api('/api/opportunity-followups/process', {
        method: 'POST',
        body: JSON.stringify({ limit: 30 }),
      });
      await refreshAll();
    } catch (followupError) {
      setError(followupError.message);
    } finally {
      setFollowupBusy(false);
    }
  }

  return {
    busy,
    channels,
    channelForm,
    cancelInstanceEdit,
    cancelWatchlistEdit,
    createChannel,
    createInstance,
    createWatchlist,
    deleteInstance,
    deleteWatchlist,
    duplicateInstance,
    duplicateWatchlist,
    editingInstanceId,
    editingWatchlistId,
    editInstance,
    editWatchlist,
    error,
    events,
    followupBusy,
    instanceForm,
    instances,
    markOpportunityActive,
    markOpportunityWatching,
    adjustOpportunityRiskPlan,
    applyInstanceTemplate,
    notificationBusyId,
    notificationPreview,
    notificationPreviewBusy,
    opportunities,
    opportunityDetails,
    observationBusy,
    observationHealth,
    expandedOpportunityId,
    opportunityDetailBusyId,
    pauseOpportunity,
    processOpportunityFollowups,
    refreshObservationHealth,
    refreshAll,
    reviewOpportunity,
    resumeOpportunity,
    runObservationDueCycle,
    runSelectedInstance,
    runs,
    ruleTestBusy,
    ruleTestResult,
    selectedInstance,
    selectedOpportunity: selectedOpportunityId ? opportunities.find((item) => item.id === selectedOpportunityId) || opportunityDetails[selectedOpportunityId] || null : null,
    selectedOpportunityId,
    selectedInstanceId,
    selectedWatchlist,
    selectedWatchlistId,
    selectInstance,
    selectWatchlist,
    sendNotificationEvent,
    setChannelForm,
    setInstanceForm,
    setSelectedInstanceId,
    setSelectedWatchlistId,
    setWatchlistForm,
    testingChannelId,
    testChannel,
    testSelectedInstanceRules,
    testSelectedInstanceNotificationPayload,
    toggleInstanceStatus,
    toggleOpportunityDetail,
    openOpportunityModal,
    closeOpportunityModal,
    saveOpportunityRiskPlan,
    visibleInstances,
    watchlistForm,
    archiveOpportunity,
    watchlists,
    busyOpportunityId: opportunityBusyId,
  };
}
