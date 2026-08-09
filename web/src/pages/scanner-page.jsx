import React, { useMemo, useState } from 'react';
import { Bell, Edit3, FlaskConical, Loader2, Star, Tag, Trash2, X } from 'lucide-react';
import { DisclosurePanel, Metric, Pair, ProgressBar, SectionTitle, Toggle } from '../components/common.jsx';
import { CopyableId } from '../components/copyable-id.jsx';
import { Markdownish } from '../components/markdownish.jsx';
import { MarketClock } from '../components/market-clock.jsx';
import { SessionAndRouteBar } from '../components/session-route-bar.jsx';
import { PageHeader } from '../components/app-shell/page-header.jsx';
import { useAppShell } from '../components/app-shell/app-shell-context.js';

import { Term } from '../components/term.jsx';
import { QuotaBar } from '../components/quota-bar.jsx';
import {
  CandidateTable,
  ChartCard,
  DecisionConsistencyNotice,
  DecisionNotes,
  EmptyState,
  StrategyCandidateTable,
  StrategyRecommendationCard,
  TopRecommendationCard,
} from '../components/scanner-widgets.jsx';
import { AnalysisTracePanel, LazyJsonPanel } from '../components/trace-panels.jsx';
import {
  PROMPT_PREVIEW_LIMIT,
  analysisModuleItems,
  candidateSortItems,
  marketDataSourceItems,
  optionDataSourceItems,
  quickPrompts,
  scannerResultTabs,
  strategyModeItems,
} from '../config.js';
import {
  buildScanBlockers,
  candidateSortValue,
  decisionConsistencyLabel,
  decisionConsistencyTone,
  decisionGateLabel,
  decisionGateSubLabel,
  decisionGateTone,
  decisionRegimeLabel,
  decisionValidationLabel,
  decisionValidationSubLabel,
  decisionValidationTone,
  enabledToolNames,
  findAiSelectedCandidate,
  fmt,
  formatTime,
  gexRegimeLabel,
  gexWallLabel,
  marketDataSourceLabel,
  optionDataSourceLabel,
  pct,
  preferredExecutionLabel,
  primarySourceLabel,
  runStatusLabel,
  stageLabel,
  strategyModesLabel,
} from '../utils/display.js';

const t = (p) => window._t(p);

function parseScanMarkTags(raw) {
  return String(raw || '')
    .split(',')
    .map((tag) => tag.trim())
    .filter(Boolean);
}

const technicalTriggerFields = [
  ['last', 'Last'],
  ['underlying_vs_vwap_pct', t('scanner2.field.vwapDistance')],
  ['vwap', 'VWAP'],
  ['rvol', 'RVOL'],
  ['orb_high', 'ORB High'],
  ['orb_low', 'ORB Low'],
  ['ema_20', 'EMA20'],
  ['ema_50', 'EMA50'],
  ['ema_200', 'EMA200'],
  ['rsi', 'RSI'],
  ['atr', 'ATR'],
];

const optionQuoteTriggerFields = [
  ['bid_ask_spread_pct', 'Bid/Ask Spread%'],
  ['ask', 'Ask'],
  ['bid', 'Bid'],
  ['mid', 'Mid'],
  ['last', 'Last'],
  ['volume', 'Volume'],
  ['open_interest', 'Open Interest'],
  ['delta', 'Delta'],
  ['gamma', 'Gamma'],
  ['theta', 'Theta'],
  ['vega', 'Vega'],
];

function firstNumber(...values) {
  for (const value of values) {
    const number = Number(value);
    if (Number.isFinite(number)) return number;
  }
  return null;
}

function shortNumber(value, digits = 2) {
  const number = Number(value);
  return Number.isFinite(number) ? Number(number.toFixed(digits)) : '';
}

function primaryContract(candidate) {
  return candidate?.contract_symbol || candidate?.symbol || candidate?.contract || '';
}

function triggerTypeLabel(type) {
  return {
    underlying_price: t('scanner2.triggerType.underlyingPrice'),
    technical_indicator: t('scanner2.triggerType.technicalIndicator'),
    option_quote: t('scanner2.triggerType.optionQuote'),
    rescan_score: t('scanner2.triggerType.rescanScore'),
  }[type] || t('scanner2.triggerType.underlyingPrice');
}

function triggerFieldLabel(type, field) {
  const source = type === 'option_quote' ? optionQuoteTriggerFields : technicalTriggerFields;
  return source.find(([key]) => key === field)?.[1] || field || '';
}

function triggerConditionLabel(trigger) {
  const condition = trigger?.condition || {};
  const field = condition.field ? ` · ${condition.label || triggerFieldLabel(condition.type, condition.field)}` : '';
  const contract = condition.contract_symbol ? ` · ${condition.contract_symbol}` : '';
  return `${triggerTypeLabel(condition.type)}${field}${contract} · ${trigger?.symbol || condition.symbol || '--'} ${condition.operator || '>='} ${condition.value ?? '--'}`;
}

function triggerTestLabel(result) {
  if (!result) return '';
  const quality = triggerDataQualityLabel(result);
  const suffix = quality ? ` · ${quality}` : '';
  if (result.matched) return `${t('scanner2.testMatched')} · ${t('scanner2.currentValue')} ${result.current_value ?? '--'}${suffix}`;
  return `${t('scanner2.testNotMatched')} · ${result.reason || t('scanner2.currentValue')} ${result.current_value ?? '--'}${suffix}`;
}

function triggerMarketPolicyLabel(policy) {
  return {
    regular_only: t('scanner2.marketPolicy.regularOnly'),
    include_extended: t('scanner2.marketPolicy.includeExtended'),
    next_open: t('scanner2.marketPolicy.nextOpen'),
    eod_review: t('scanner2.marketPolicy.eodReview'),
    always_calendar: t('scanner2.marketPolicy.alwaysCalendar'),
  }[policy] || t('scanner2.marketPolicy.regularOnly');
}

function triggerDataQualityLabel(result) {
  const quality = result?.data_quality || result?.snapshot?.data_quality;
  if (!quality) return '';
  const parts = [quality.label || quality.status, quality.source, quality.explanation].filter(Boolean);
  return parts.join(' · ');
}

export function ScannerPage({
  accountForm,
  accountPanelOpen,
  accounts,
  activeScan,
  addLongbridgeAccount,
  addProvider,
  addUserProvider,
  aiProvider,
  analysisModules,
  analysisPresets,
  applyAnalysisPreset,
  auth,
  authLoading,
  candidateSort,
  changeRouteMode,
  clockTick,
  council,
  createWaitTriggerFromScan,
  deleteLongbridgeAccount,
  deleteProvider,
  deleteTrigger,
  deleteUserProvider,
  error,
  forceRefreshAuth,
  loading,
  longbridgeAccount,
  logoutApp,
  markHistory,
  marketClock,
  marketDataSource,
  optionDataSource,
  onOpenBetaLottery,
  onOpenGuide,
  onOpenPresetGuide,
  onOpenTrading,
  onOpenWatchlists,
  openHistory,
  providerForm,
  providerPanelOpen,
  providers,
  query,
  quickPromptsExpanded,
  refreshHistory,
  refreshTriggers,
  result,
  routeMode,
  runScan,
  scanHistory,
  scanHistoryHasNext,
  scanHistoryPage,
  scanHistoryQuery,
  scanHistoryStarredOnly,
  scanHistoryTag,
  scanTriggers,
  scannerResultTab,
  session,
  setAccountForm,
  setAccountPanelOpen,
  setAiProvider,
  setAnalysisModules,
  setCandidateSort,
  setCouncil,
  setDefaultLongbridgeAccount,
  setLongbridgeAccount,
  setMarketDataSource,
  setOptionDataSource,
  setProviderForm,
  setProviderPanelOpen,
  setError,
  setQuery,
  setQuickPromptsExpanded,
  setScannerResultTab,
  setScanHistoryQuery,
  setScanHistoryStarredOnly,
  setScanHistoryTag,
  setStrategyModes,
  setSymbol,
  setUseAi,
  setUserProviderForm,
  setUserProviderPanelOpen,
  strategyModes,
  symbol,
  toggleStrategyMode,
  toggleTrigger,
  testTrigger,
  testingTriggerId,
  triggerTestResults,
  useAi,
  userProviderForm,
  userProviderPanelOpen,
}) {

  const [triggerDraft, setTriggerDraft] = useState({
    type: 'underlying_price',
    field: '',
    operator: '>=',
    value: '',
    interval: 300,
    contractSymbol: '',
    marketPolicy: 'regular_only',
  });
  const [markEditor, setMarkEditor] = useState(null);
  const payload = result?.payload;
  const candidates = payload?.option_candidates ?? [];
  const strategyCandidates = payload?.strategy_candidates ?? [];
  const aiSelectedCandidate = payload?.ai_selected_candidate || findAiSelectedCandidate(result?.answer, candidates);
  const hasPrimaryCandidate = payload && Object.prototype.hasOwnProperty.call(payload, 'primary_candidate');
  const primaryStrategySelection = payload?.primary_strategy || null;
  const hasValidatedSelection = payload?.primary_source === 'validated_trade_selection' && (payload?.primary_candidate || primaryStrategySelection);
  const observeOnly = payload?.primary_source === 'observe_only' || payload?.decision_validation_status === 'observe' || payload?.decision_consistency?.should_trade === false;
  const topPick = hasPrimaryCandidate ? payload.primary_candidate : (aiSelectedCandidate || candidates[0]);
  const topPickLabel = observeOnly && topPick
    ? `${primarySourceLabel(payload?.primary_source || (aiSelectedCandidate ? 'ai_selected_candidate' : 'decision_score_top'))} · ${t('scanner2.observeOnly')}`
    : primarySourceLabel(payload?.primary_source || (aiSelectedCandidate ? 'ai_selected_candidate' : 'decision_score_top'));
  const sortedCandidates = useMemo(
    () => [...candidates].sort((left, right) => candidateSortValue(right, candidateSort) - candidateSortValue(left, candidateSort)),
    [candidates, candidateSort],
  );
  const daily = payload?.daily_summary ?? {};
  const intraday = payload?.intraday_summary ?? {};
  const intent = payload?.intent ?? {};
  const planner = payload?.llm_intent_plan ?? {};
  const toolPlan = payload?.tool_plan ?? planner.tool_plan ?? {};
  const topStrategyCandidate = strategyCandidates[0];
  const intradayTools = payload?.intraday_option_tools ?? {};
  const gexContext = payload?.gex_context ?? {};
  const decisionGate = payload?.decision_gate ?? {};
  const decisionConsistency = payload?.decision_consistency ?? {};
  const actualMarketDataSource = result?.market_data_source || payload?.market_data_source || activeScan?.market_data_source || marketDataSource;
  const actualOptionDataSource = result?.option_data_source || payload?.option_data_source || activeScan?.option_data_source || optionDataSource;
  const canTrade = Boolean(session.can_trade);
  const canAdmin = Boolean(session.is_admin);
  const selectedProvider = providers.find((provider) => provider.name === aiProvider);
  const userProviders = providers.filter((provider) => provider.server_managed === false);
  const scanBlockers = buildScanBlockers({ loading, query, marketDataSource, longbridgeAccount, useAi, aiProvider, providers, canTrade, strategyModes });
  const visibleAnalysisPresets = analysisPresets.slice(0, PROMPT_PREVIEW_LIMIT);
  const visibleQuickPrompts = quickPromptsExpanded ? quickPrompts : quickPrompts.slice(0, PROMPT_PREVIEW_LIMIT);
  const activeTriggerSymbol = (activeScan?.symbol || payload?.symbol || symbol || '').trim().toUpperCase();
  const canCreateWaitTrigger = Boolean(activeScan?.id && activeTriggerSymbol);
  const triggerPlaybooks = useMemo(() => {
    if (!activeTriggerSymbol) return [];
    const tools = payload?.intraday_option_tools || {};
    const orb15 = (tools.opening_ranges || {})['15m'] || {};
    const vwap = tools.vwap_structure || {};
    const rvol = tools.relative_volume || {};
    const contract = primaryContract(topPick);
    const last = firstNumber(payload?.last_price, payload?.underlying_price, daily.close, intraday.last, topPick?.underlying_price);
    const orbHigh = firstNumber(orb15.high, intraday.orb_high, payload?.orb_high);
    const vwapDistance = firstNumber(vwap.vs_vwap_pct, intraday.vs_vwap_pct, payload?.underlying_vs_vwap_pct, 0);
    return [
      {
        key: 'orb_breakout',
        title: t('scanner2.playbook.orbBreakout'),
        desc: orbHigh ? t('scanner2.playbook.orbBreakoutDesc').replace('{v}', shortNumber(orbHigh)) : t('scanner2.playbook.orbBreakoutDescEmpty'),
        disabled: !orbHigh,
        draft: { type: 'technical_indicator', field: 'last', operator: '>=', value: shortNumber(orbHigh), interval: 120, marketPolicy: 'regular_only', contractSymbol: '' },
      },
      {
        key: 'vwap_reclaim',
        title: t('scanner2.playbook.vwapReclaim'),
        desc: t('scanner2.playbook.vwapReclaimDesc').replace('{v}', shortNumber(vwapDistance)),
        draft: { type: 'technical_indicator', field: 'underlying_vs_vwap_pct', operator: '>=', value: 0, interval: 180, marketPolicy: 'regular_only', contractSymbol: '' },
      },
      {
        key: 'rvol_expansion',
        title: t('scanner2.playbook.rvolExpansion'),
        desc: t('scanner2.playbook.rvolExpansionDesc').replace('{v}', shortNumber(firstNumber(rvol.rvol_time_adjusted, payload?.rvol, intraday.rvol), 2) || '--'),
        draft: { type: 'technical_indicator', field: 'rvol', operator: '>=', value: 1.5, interval: 180, marketPolicy: 'regular_only', contractSymbol: '' },
      },
      {
        key: 'option_spread_tighten',
        title: t('scanner2.playbook.spreadTighten'),
        desc: contract ? `${contract} bid/ask spread <= 8%` : t('scanner2.playbook.spreadTightenDescEmpty'),
        disabled: !contract,
        draft: { type: 'option_quote', field: 'bid_ask_spread_pct', operator: '<=', value: 8, interval: 180, marketPolicy: 'regular_only', contractSymbol: contract },
      },
      {
        key: 'oi_volume',
        title: t('scanner2.playbook.oiVolume'),
        desc: contract ? `${contract} volume >= 100` : t('scanner2.playbook.oiVolumeDescEmpty'),
        disabled: !contract,
        draft: { type: 'option_quote', field: 'volume', operator: '>=', value: 100, interval: 300, marketPolicy: 'regular_only', contractSymbol: contract },
      },
      {
        key: 'price_reclaim',
        title: t('scanner2.playbook.priceReclaim'),
        desc: last ? `${activeTriggerSymbol} >= $${shortNumber(last)}` : t('scanner2.playbook.priceReclaimDescEmpty'),
        disabled: !last,
        draft: { type: 'underlying_price', field: '', operator: '>=', value: shortNumber(last), interval: 300, marketPolicy: 'regular_only', contractSymbol: '' },
      },
    ];
  }, [activeTriggerSymbol, daily.close, intraday, payload, topPick]);

  function openMarkEditor(scan) {
    setMarkEditor({
      scanId: scan.id,
      locatorId: scan.locator_id || scan.id,
      symbol: scan.payload?.symbol || scan.symbol || '',
      starred: Boolean(scan.mark?.starred),
      note: scan.mark?.note || '',
      tags: (scan.mark?.tags || []).join(', '),
    });
  }

  async function submitMarkEditor(event) {
    event.preventDefault();
    if (!markEditor) return;
    try {
      await markHistory(markEditor.scanId, {
        starred: markEditor.starred,
        note: markEditor.note.trim(),
        tags: parseScanMarkTags(markEditor.tags),
      });
      setMarkEditor(null);
      setError('');
    } catch (markError) {
      setError(markError.message || t('scanner2.saveMarkFailed'));
    }
  }

  async function submitHistorySearch(event) {
    event.preventDefault();
    try {
      await refreshHistory(0, scanHistoryStarredOnly, scanHistoryQuery, scanHistoryTag);
      setError('');
    } catch (searchError) {
      setError(searchError.message || t('scanner2.searchHistoryFailed'));
    }
  }

  async function clearHistorySearch() {
    setScanHistoryQuery('');
    setScanHistoryTag('');
    try {
      await refreshHistory(0, scanHistoryStarredOnly, '', '');
      setError('');
    } catch (searchError) {
      setError(searchError.message || t('scanner2.refreshHistoryFailed'));
    }
  }

  async function submitWaitTrigger(event) {
    event.preventDefault();
    const value = Number(triggerDraft.value);
    if (!Number.isFinite(value)) {
      setError(t('scanner2.enterValidTriggerValue'));
      return;
    }
    if (triggerDraft.type === 'underlying_price' && value <= 0) {
      setError(t('scanner2.enterValidPriceValue'));
      return;
    }
    try {
      const fieldLabel = triggerDraft.type === 'technical_indicator' || triggerDraft.type === 'option_quote'
        ? triggerFieldLabel(triggerDraft.type, triggerDraft.field)
        : '';
      const triggerLabel = fieldLabel || triggerTypeLabel(triggerDraft.type);
      await createWaitTriggerFromScan(activeScan, {
        type: triggerDraft.type,
        symbol: activeTriggerSymbol,
        field: triggerDraft.field,
        label: fieldLabel,
        contract_symbol: triggerDraft.contractSymbol,
        market_policy: triggerDraft.marketPolicy,
        opening_grace_minutes: 10,
        operator: triggerDraft.operator,
        value,
        check_interval_seconds: Number(triggerDraft.interval) || 300,
        name: `${activeTriggerSymbol} ${triggerLabel} ${triggerDraft.operator} ${value}`,
      });
      setTriggerDraft((current) => ({ ...current, value: '' }));
      await refreshTriggers();
      setError('');
    } catch (triggerError) {
      setError(triggerError.message || t('scanner2.createWaitTriggerFailed'));
    }
  }

  function applyTriggerPlaybook(playbook) {
    if (!playbook || playbook.disabled) return;
    setTriggerDraft((current) => ({
      ...current,
      ...playbook.draft,
      value: String(playbook.draft.value ?? ''),
      interval: playbook.draft.interval || current.interval,
    }));
  }

  async function createSuggestedTriggersFromScan(scan, sourceLabel = t('scanner2.starredAnalysis')) {
    const historySymbol = (scan?.payload?.symbol || scan?.symbol || activeTriggerSymbol || '').trim().toUpperCase();
    if (!scan?.id || !historySymbol) {
      setError(t('scanner2.noSymbolForSuggested'));
      return;
    }
    const scanPayload = scan.payload || {};
    const scanTools = scanPayload.intraday_option_tools || {};
    const scanVwap = scanTools.vwap_structure || {};
    const scanRvol = scanTools.relative_volume || {};
    const scanLast = firstNumber(scanPayload.last_price, scanPayload.underlying_price, scanPayload.last, scanPayload.close, scanPayload.daily_summary?.close);
    const suggestions = [
      {
        type: 'technical_indicator',
        field: 'underlying_vs_vwap_pct',
        label: t('scanner2.reclaimVwap'),
        operator: '>=',
        value: 0,
        name: `${historySymbol} ${sourceLabel} · ${t('scanner2.reclaimVwap')}`,
      },
      {
        type: 'technical_indicator',
        field: 'rvol',
        label: t('scanner2.rvolExpansionLabel'),
        operator: '>=',
        value: Math.max(1.3, shortNumber(firstNumber(scanRvol.rvol_time_adjusted, scanPayload.rvol, 1.3), 2) || 1.3),
        name: `${historySymbol} ${sourceLabel} · ${t('scanner2.rvolExpansionLabel')}`,
      },
      {
        type: 'rescan_score',
        operator: '>=',
        value: 80,
        name: `${historySymbol} ${sourceLabel} · ${t('scanner2.rescanScoreGte80')}`,
      },
    ];
    if (scanLast) {
      suggestions[0].reference_price = scanLast;
    }
    if (Number.isFinite(Number(scanVwap.vs_vwap_pct))) {
      suggestions[0].note = t('scanner2.vwapDistanceAtCreate').replace('{v}', shortNumber(scanVwap.vs_vwap_pct));
    }
    try {
      await Promise.all(suggestions.map((item) => createWaitTriggerFromScan(scan, {
        ...item,
        symbol: historySymbol,
        market_policy: item.type === 'rescan_score' ? 'next_open' : 'regular_only',
        opening_grace_minutes: 10,
        check_interval_seconds: item.type === 'rescan_score' ? 600 : 300,
        max_trigger_count: 3,
      })));
      await refreshTriggers();
      setError('');
    } catch (triggerError) {
      setError(triggerError.message || t('scanner2.createSuggestedFailed'));
    }
  }

  async function createTriggerFromHistory(scan) {
    await createSuggestedTriggersFromScan(scan);
  }

  const heroSymbol = payload?.symbol || window._t('scanner.ready');

  return (
    <main className="shell">
      <ScannerPageHeader
        payload={payload}
        canTrade={canTrade}
        onOpenGuide={onOpenGuide}
        onOpenPresetGuide={onOpenPresetGuide}
        onOpenBetaLottery={onOpenBetaLottery}
        onOpenTrading={onOpenTrading}
      />
      <section className="hero app-hero">
        <div className="hero-brand">
          <img className="site-logo" src="/logo.png" alt="AI Option Scanner" />
          <div>
            <div className="eyebrow">Longbridge Options · AI Council</div>
            <h1>{t('scanner2.appTitle')}</h1>
            <p>{t('scanner2.appIntro')}</p>
          </div>
        </div>
        <div className="hero-controls">
          <MarketClock clock={marketClock} tick={clockTick} />
          <div className="hero-card">
            <span><i className="pulse" /> {t('scanner2.liveAnalyzer')}</span>
            <strong>{heroSymbol}</strong>
          </div>
          <SessionAndRouteBar session={session} routeMode={routeMode} onRouteModeChange={changeRouteMode} onLogout={logoutApp} />
          <div className="hero-actions">
            <button className="ghost nav-action" type="button" onClick={onOpenGuide}>{t("scanner.guide")}</button>
            <button className="ghost nav-action" type="button" onClick={onOpenPresetGuide}>{t("scanner.presets")}</button>
            <button className="ghost nav-action" type="button" onClick={onOpenWatchlists}>{t('scanner2.opportunityRadar')}</button>
            <button className="ghost nav-action" type="button" onClick={onOpenBetaLottery}>{t("scanner.lottery")}</button>
            <button className="primary nav-action" type="button" disabled={!canTrade} title={canTrade ? t('scanner2.enterLiveTrading') : t('scanner2.noLivePermission')} onClick={onOpenTrading}>{t("scanner.trading")}</button>
          </div>
        </div>
      </section>

      <div className="grid">
        <aside className="panel control-panel">
          <SectionTitle title={t("scanner.hint")} />
          <QuotaBar />
          <form onSubmit={runScan} className="form">
            <label>
              {t('scanner2.naturalLanguageRequest')}
              <textarea value={query} onChange={(event) => setQuery(event.target.value)} rows={5} />
            </label>
            {analysisPresets.length > 0 && (
              <div className="strategy-selector compact">
                <div className="strategy-selector-head">
                  <strong>{t("scanner.presets")}</strong>
                  <div>
                    <span className="muted">{t('scanner2.presetsHint')}</span>
                    {analysisPresets.length > PROMPT_PREVIEW_LIMIT && (
                      <button className="ghost compact" type="button" onClick={onOpenPresetGuide}>{t('scanner2.viewAll')}</button>
                    )}
                  </div>
                </div>
                <div className="strategy-pills">
                  {visibleAnalysisPresets.map((preset) => (
                    <button
                      key={preset.key}
                      type="button"
                      className="route-pill compact"
                      title={preset.description}
                      onClick={() => applyAnalysisPreset(preset)}
                    >
                      {preset.label}
                    </button>
                  ))}
                </div>
              </div>
            )}
            <div className="single-field-row">
              <label>
                {t('scanner2.tickerOverride')}
                <input value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} placeholder={t('scanner2.tickerPlaceholder')} />
              </label>
            </div>

            <DisclosurePanel
              title={t('scanner2.advancedFilters')}
              summary={`${marketDataSourceLabel(marketDataSource)} / ${optionDataSourceLabel(optionDataSource)} · ${strategyModesLabel(strategyModes)}`}
              className="embedded-disclosure"
            >
              <div className="three">
                <label>
                  {t('scanner2.aiModel')}
                  <select value={aiProvider} onChange={(event) => setAiProvider(event.target.value)}>
                    {providers.map((provider) => (
                      <option key={provider.name} value={provider.name}>
                        {provider.server_managed === false ? (provider.label || provider.raw_name || provider.name) : provider.name}
                        {provider.model ? ` · ${provider.model}` : ''}
                        {provider.provider_type === 'claude' ? ' · Claude' : ' · OpenAI'}
                        {provider.server_managed === false ? ` · ${t('scanner2.myKey')}` : ` · ${t('scanner2.platform')}`}
                        {provider.configured === false ? ` · ${t('scanner2.notConfigured')}` : ''}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  {t('scanner2.marketDataSource')}
                  <select value={marketDataSource} onChange={(event) => setMarketDataSource(event.target.value)}>
                    {marketDataSourceItems.filter(([value]) => canTrade || value !== 'longbridge').map(([value, label]) => (
                      <option key={value} value={value}>{label}</option>
                    ))}
                  </select>
                </label>
                <label>
                  {t('scanner2.optionDataSource')}
                  <select value={optionDataSource} onChange={(event) => setOptionDataSource(event.target.value)}>
                    {optionDataSourceItems.filter(([value]) => canTrade || value !== 'longbridge').map(([value, label]) => (
                      <option key={value} value={value}>{label}</option>
                    ))}
                  </select>
                </label>
              </div>
              {useAi && selectedProvider && (
                <div className={`permission-note ${selectedProvider.configured === false ? 'is-danger' : ''}`}>
                  <strong>
                    {selectedProvider.configured === false
                      ? t('scanner2.serverModelNotConfigured')
                      : selectedProvider.server_managed === false ? t('scanner2.usingYourAiKey') : t('scanner2.aiKeyManagedByPlatform')}
                  </strong>
                  <p>
                    {selectedProvider.configured === false
                    ? t('scanner2.contactAdminConfigureKey')
                    : selectedProvider.server_managed === false
                      ? t('scanner2.usingYourKeyDesc')
                          .replace('{model}', selectedProvider.model || selectedProvider.name)
                          .replace('{compat}', selectedProvider.provider_type === 'claude' ? 'Claude-compatible' : 'OpenAI-compatible')
                      : t('scanner2.usingPlatformQuotaDesc').replace('{model}', selectedProvider.model || selectedProvider.name)}
                  </p>
                </div>
              )}
              {canTrade && marketDataSource === 'longbridge' && (
                <label>
                  {t('scanner2.longbridgeAccount')}
                  <select value={longbridgeAccount} onChange={(event) => setLongbridgeAccount(event.target.value)}>
                    <option value="">{t('scanner2.noAccountDefaultApi')}</option>
                    {accounts.map((account) => (
                      <option key={account.name} value={account.name}>{account.label || account.name}</option>
                    ))}
                  </select>
                </label>
              )}
              {!canTrade && (
                <div className="permission-note">
                  <strong>{t('scanner2.analyzerAccessOpen')}</strong>
                  <p>{t('scanner2.analyzerAccessOpenDesc')}</p>
                </div>
              )}
              <div className="switches">
                <Toggle checked={useAi} onChange={setUseAi} label={t('scanner2.enableAi')} />
                <Toggle checked={council} onChange={setCouncil} label={t('scanner2.council')} />
              </div>
              <div className="module-grid">
                {analysisModuleItems.map(([key, label]) => (
                  <Toggle
                    key={key}
                    checked={analysisModules[key] ?? true}
                    onChange={(checked) => setAnalysisModules({ ...analysisModules, [key]: checked })}
                    label={label}
                  />
                ))}
              </div>
              <div className="strategy-selector">
                <div className="strategy-selector-head">
                  <strong>{t('scanner2.strategyMode')}</strong>
                  <div>
                    <button type="button" className="ghost compact" onClick={() => setStrategyModes(strategyModeItems.map(([value]) => value))}>{t('scanner2.selectAll')}</button>
                    <button type="button" className="ghost compact" onClick={() => setStrategyModes(['single_leg'])}>{t('scanner2.singleLeg')}</button>
                  </div>
                </div>
                <div className="strategy-pills">
                  {strategyModeItems.map(([value, label]) => (
                    <button
                      key={value}
                      type="button"
                      className={`route-pill compact ${strategyModes.includes(value) ? 'active' : ''}`}
                      onClick={() => toggleStrategyMode(value)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <p className="strategy-hint">
                  {t('scanner2.strategyHint')}
                </p>
              </div>
            </DisclosurePanel>
            {scanBlockers.length > 0 && (
              <div className="run-blockers">
                <strong>{t('scanner2.cannotScanYet')}</strong>
                <ul>{scanBlockers.map((item) => <li key={item}>{item}</li>)}</ul>
              </div>
            )}
            <button className="primary" disabled={scanBlockers.length > 0}>
              {loading && <Loader2 className="spin" size={14} />}
              {loading ? t('scanner2.scanning') : t('scanner2.startScan')}
            </button>
          </form>

          <div className="quick-list">
            <div className="quick-list-head">
              <strong>{t('scanner2.promptExamples')}</strong>
              {quickPrompts.length > PROMPT_PREVIEW_LIMIT && (
                <button className="ghost compact" type="button" onClick={() => setQuickPromptsExpanded(!quickPromptsExpanded)}>
                  {quickPromptsExpanded ? t('scanner2.collapse') : t('scanner2.expandN').replace('{n}', quickPrompts.length)}
                </button>
              )}
            </div>
            {visibleQuickPrompts.map((prompt) => (
              <button key={prompt} type="button" onClick={() => setQuery(prompt)}>{prompt}</button>
            ))}
          </div>
        </aside>

        <section className="workspace">
          {error && <div className="error">{error}</div>}

          <div className="metrics">
            <Metric label={t('scanner2.technicalBias')} value={payload?.technical_bias ?? '--'} />
            <Metric label={t('scanner2.lastPriceRef')} value={fmt(daily.close)} sub={pct(daily.day_pct)} />
            <Metric label={t('scanner2.marketSource')} value={marketDataSourceLabel(actualMarketDataSource)} sub={payload?.longbridge_account || activeScan?.longbridge_account || '--'} tone={actualMarketDataSource === 'yfinance' ? 'warning' : ['longbridge', 'thetadata'].includes(actualMarketDataSource) ? 'ok' : 'muted'} />
            <Metric label={t('scanner2.gexRegime')} value={gexRegimeLabel(gexContext.regime)} sub={`${gexWallLabel(gexContext.nearest_wall)} ${pct(gexContext.nearest_wall_distance_pct)}`} tone={gexContext.regime === 'negative_gamma' ? 'warning' : gexContext.regime === 'positive_gamma' ? 'ok' : 'muted'} />
          </div>

          <DisclosurePanel title={t('scanner2.diagnosticMetrics')} summary={`${t('scanner2.analysisStatus')}: ${runStatusLabel(activeScan?.status) ?? '--'}`} className="diagnostic-metrics-panel">
            <div className="metrics compact-metrics">
              <Metric label={t('scanner2.intradayVwap')} value={pct(intraday.vs_vwap_pct)} sub={`RSI ${fmt(daily.rsi14)}`} />
              <Metric label={t('scanner2.optionSource')} value={optionDataSourceLabel(actualOptionDataSource)} sub={actualOptionDataSource === 'thetadata' ? 'ThetaData' : '--'} tone={actualOptionDataSource === 'thetadata' ? 'ok' : actualOptionDataSource === 'yfinance' ? 'warning' : 'muted'} />
              <Metric label={t('scanner2.analysisStatus')} value={runStatusLabel(activeScan?.status) ?? '--'} sub={activeScan ? `${stageLabel(activeScan.stage)} · ${activeScan.progress ?? 0}%` : '--'} />
              <Metric label={t('scanner2.strategyTag')} value={topPick?.strategy_tag ?? '--'} sub={`${t('scanner2.probBreakeven')} ${pct(topPick?.probability_breakeven)}`} />
              <Metric label={t('scanner2.regimeGate')} value={decisionGateLabel(decisionGate)} sub={decisionGateSubLabel(decisionGate)} tone={decisionGateTone(decisionGate)} />
              <Metric label={t('scanner2.decisionConsistency')} value={decisionConsistencyLabel(decisionConsistency)} sub={decisionConsistency?.primary_contract || decisionConsistency?.top_ranked_contract || '--'} tone={decisionConsistencyTone(decisionConsistency)} />
              <Metric label={t('scanner2.structureValidation')} value={decisionValidationLabel(payload?.decision_validation_status)} sub={decisionValidationSubLabel(payload?.decision_validation)} tone={decisionValidationTone(payload?.decision_validation)} />
            </div>
          </DisclosurePanel>

          <div className="panel answer-card">
            <div className="answer-head">
              <SectionTitle title={observeOnly && !hasValidatedSelection ? t('scanner2.observeConclusion') : t('scanner2.finalPlan')} />
              <div className="action-row">
                <CopyableId value={activeScan?.locator_id || result?.locator_id} label={t('scanner2.scanInstance')} />
                <button className="ghost" onClick={runScan} disabled={loading}>{t('scanner2.rescan')}</button>
              </div>
            </div>
            {activeScan && ['queued', 'running'].includes(activeScan.status) && (
              <ProgressBar progress={activeScan.progress} stage={activeScan.stage} />
            )}
            {result ? (
              <>
                {primaryStrategySelection
                  ? <StrategyRecommendationCard candidate={primaryStrategySelection} label={t('scanner2.finalStructurePlan')} />
                  : <TopRecommendationCard candidate={topPick} payload={payload} label={topPickLabel} observeOnly={observeOnly && !hasValidatedSelection} />}
                <DecisionConsistencyNotice gate={decisionGate} consistency={decisionConsistency} />
                {!primaryStrategySelection && topStrategyCandidate && <StrategyRecommendationCard candidate={topStrategyCandidate} label={observeOnly ? t('scanner2.structureCandidateObserve') : t('scanner2.structureCandidateExp')} />}
                <Markdownish text={result.answer} />
              </>
            ) : <EmptyState scan={activeScan} />}
          </div>

          <DisclosurePanel title={t('scanner2.analysisHistory')} summary={`${scanHistory.length} · ${t('scanner2.pageN').replace('{n}', scanHistoryPage + 1)}`} className="history-panel">
            <div className="answer-head">
              <div className="action-row">
                <button className="ghost compact" type="button" disabled={scanHistoryPage <= 0} onClick={() => refreshHistory(scanHistoryPage - 1)}>{t('scanner2.prevPage')}</button>
                <span className="muted">{t('scanner2.pageN').replace('{n}', scanHistoryPage + 1)}</span>
                <button className="ghost compact" type="button" disabled={!scanHistoryHasNext} onClick={() => refreshHistory(scanHistoryPage + 1)}>{t('scanner2.nextPage')}</button>
                <button
                  className={`ghost compact ${scanHistoryStarredOnly ? 'active' : ''}`}
                  type="button"
                  onClick={() => {
                    setScanHistoryStarredOnly(!scanHistoryStarredOnly);
                    refreshHistory(0, !scanHistoryStarredOnly);
                  }}
                >
                  <Star size={14} /> {t('scanner2.starred')}
                </button>
                <button className="ghost" onClick={() => refreshHistory(scanHistoryPage)}>{t("quota.refresh")}</button>
              </div>
            </div>
            <form className="history-search" onSubmit={submitHistorySearch}>
              <input
                value={scanHistoryQuery}
                onChange={(event) => setScanHistoryQuery(event.target.value)}
                placeholder={t('scanner2.historySearchPlaceholder')}
              />
              <input
                value={scanHistoryTag}
                onChange={(event) => setScanHistoryTag(event.target.value)}
                placeholder={t('scanner2.tagFilterPlaceholder')}
              />
              <button className="primary compact" type="submit">{t("common.search")}</button>
              <button className="ghost compact" type="button" onClick={clearHistorySearch}>
                <X size={14} /> {t('common.clear')}
              </button>
            </form>
            <div className="history-list">
              {scanHistory.map((scan) => (
                <article key={scan.id} className={`history-item ${activeScan?.id === scan.id ? 'active' : ''}`}>
                  <div className="history-item-actions">
                    <button
                      type="button"
                      className={`icon-button star-button ${scan.mark?.starred ? 'active' : ''}`}
                      title={scan.mark?.starred ? t('scanner2.unstar') : t('scanner2.starThis')}
                      onClick={() => markHistory(scan.id, { starred: !scan.mark?.starred, note: scan.mark?.note || '', tags: scan.mark?.tags || [] })}
                    >
                      <Star size={16} fill={scan.mark?.starred ? 'currentColor' : 'none'} />
                    </button>
                    <button type="button" className="icon-button" aria-label={t('scanner2.editNoteTags')} title={t('scanner2.editNoteTagsShort')} onClick={() => openMarkEditor(scan)}>
                      <Edit3 size={14} />
                    </button>
                    {scan.mark?.starred && (
                      <button type="button" className="icon-button" aria-label={t('scanner2.createSuggestedFromStar')} title={t('scanner2.createSuggestedFromStarTitle')} onClick={() => createTriggerFromHistory(scan)}>
                        <Bell size={14} />
                      </button>
                    )}
                  </div>
                  <button type="button" className="history-item-main" onClick={() => openHistory(scan)}>
                    <span>{runStatusLabel(scan.status)} · {scan.progress ?? 0}%</span>
                    <strong>{scan.payload?.symbol || scan.symbol || scan.query.slice(0, 12)}</strong>
                    <small>{stageLabel(scan.stage)} · {marketDataSourceLabel(scan.market_data_source || scan.payload?.market_data_source)} · {scan.ai_provider} · {formatTime(scan.created_at)}</small>
                    {scan.mark?.note && <em className="history-note">{scan.mark.note}</em>}
                  </button>
                  {scan.mark?.tags?.length ? (
                    <div className="history-tags">
                      {scan.mark.tags.map((tag) => (
                        <span key={`${scan.id}-${tag}`}><Tag size={11} /> {tag}</span>
                      ))}
                    </div>
                  ) : null}
                  <CopyableId value={scan.locator_id || scan.id} label={t('scanner2.scanInstance')} compact />
                </article>
              ))}
              {!scanHistory.length && <p className="muted">{t('scanner2.noHistory')}</p>}
            </div>
          </DisclosurePanel>

          {markEditor && (
            <div className="modal-backdrop" role="presentation" onMouseDown={() => setMarkEditor(null)}>
              <form className="mark-editor" onSubmit={submitMarkEditor} onMouseDown={(event) => event.stopPropagation()}>
                <div className="answer-head">
                  <SectionTitle title={t('scanner2.editFavoriteMark')} />
                  <button className="icon-button" type="button" aria-label={t('scanner2.closeNoteEditor')} title={t('common.back')} onClick={() => setMarkEditor(null)}>
                    <X size={16} />
                  </button>
                </div>
                <div className="mark-editor-target">
                  <strong>{markEditor.symbol || t('scanner2.analysisInstance')}</strong>
                  <span>{markEditor.locatorId}</span>
                </div>
                <label className="terms-check">
                  <input
                    type="checkbox"
                    checked={markEditor.starred}
                    onChange={(event) => setMarkEditor((current) => (current ? { ...current, starred: event.target.checked } : current))}
                  />
                  <span>{t('scanner2.addToStarred')}</span>
                </label>
                <label>
                  {t('scanner2.note')}
                  <textarea
                    rows="4"
                    value={markEditor.note}
                    onChange={(event) => setMarkEditor((current) => (current ? { ...current, note: event.target.value } : current))}
                    placeholder={t('scanner2.notePlaceholder')}
                  />
                </label>
                <label>
                  {t('scanner2.tags')}
                  <input
                    value={markEditor.tags}
                    onChange={(event) => setMarkEditor((current) => (current ? { ...current, tags: event.target.value } : current))}
                    placeholder={t('scanner2.tagsPlaceholder')}
                  />
                </label>
                <div className="action-row">
                  <button className="primary" type="submit">{t("common.save")}</button>
                  <button
                    className="ghost"
                    type="button"
                    onClick={() => setMarkEditor((current) => (current ? { ...current, note: '', tags: '' } : current))}
                  >
                    {t('scanner2.clearNoteTags')}
                  </button>
                </div>
              </form>
            </div>
          )}

          {result && (
            <>
              <div className="panel result-tabs-panel">
                <div className="detail-tabs scanner-result-tabs">
                  {scannerResultTabs.map(([key, label]) => (
                    <button key={key} type="button" className={scannerResultTab === key ? 'active' : ''} onClick={() => setScannerResultTab(key)}>
                      {label}
                    </button>
                  ))}
                </div>
                {scannerResultTab === 'charts' && (
                  <div className="chart-row">
                    <ChartCard title={t('scanner2.dailyCloseChart')} data={result?.charts?.daily ?? []} field="close" />
                    <ChartCard title={t('scanner2.intradayVwapChart')} data={result?.charts?.intraday ?? []} field="price" secondField="vwap" />
                  </div>
                )}
                {scannerResultTab === 'candidates' && (
                  <div className="panel embedded-panel">
                    <div className="answer-head">
                      <SectionTitle title={t('scanner2.candidatePool')} />
                      <label className="inline-select">
                        {t('scanner2.sort')}
                        <select value={candidateSort} onChange={(event) => setCandidateSort(event.target.value)}>
                          {candidateSortItems.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                        </select>
                      </label>
                    </div>
                    <CandidateTable candidates={sortedCandidates.slice(0, 24)} topPick={topPick} />
                  </div>
                )}
                {scannerResultTab === 'strategies' && (
                  <div className="panel embedded-panel">
                    <div className="answer-head">
                      <SectionTitle title={t('scanner2.strategyStructureCandidates')} />
                      <span className="muted">{t('scanner2.analysisOnlyNoLive')}</span>
                    </div>
                    <StrategyCandidateTable candidates={strategyCandidates.slice(0, 24)} />
                  </div>
                )}
                {scannerResultTab === 'trace' && (
                  <AnalysisTracePanel trace={payload?.analysis_trace} fallbackTitle={t('scanner2.scanDecisionTrace')} />
                )}
                {scannerResultTab === 'details' && (
                  <div className="lower-grid">
                    <div className="panel">
                      <SectionTitle title={t('scanner2.scanIntent')} />
                      <dl className="intent">
                        <Pair k={t('scanner2.maxAsk')} v={fmt(intent.max_ask)} />
                        <Pair k={t('scanner2.daysWindow')} v={`${intent.min_days ?? '--'}-${intent.max_days ?? '--'} ${t('scanner2.days')}`} />
                        <Pair k={t('scanner2.lotteryPref')} v={intent.lottery ? t('scanner2.yes') : t('scanner2.no')} />
                        <Pair k={t('scanner2.cheapPref')} v={intent.cheap ? t('scanner2.yes') : t('scanner2.no')} />
                        <Pair k={t('scanner2.dayTrade')} v={intent.day_trade ? t('scanner2.yes') : t('scanner2.no')} />
                        <Pair k={t('scanner2.directionPref')} v={intent.preferred_side ?? t('scanner2.auto')} />
                        <Pair k={t('scanner2.timeBasis')} v={intent.time_basis || 'America/New_York'} />
                        <Pair k={t('scanner2.requestedDate')} v={intent.requested_date_et || '--'} />
                        <Pair k={t('scanner2.horizonSemantics')} v={intent.horizon_label || '--'} />
                        <Pair k={t('scanner2.strategyMode')} v={strategyModesLabel(payload?.strategy_modes || strategyModes)} />
                        <Pair k={t('scanner2.plannerSource')} v={planner.source || '--'} />
                        <Pair k={t('scanner2.plannerConfidence')} v={planner.confidence != null ? pct(Number(planner.confidence) * 100) : '--'} />
                        <Pair k={t('scanner2.toolchain')} v={enabledToolNames(toolPlan)} />
                      </dl>
                      {planner.reasoning && <p className="planner-note">{planner.reasoning}</p>}
                    </div>
                    <div className="panel">
                      <SectionTitle title={t('scanner2.regimeGateTitle')} />
                      <dl className="intent">
                        <Pair k={t('scanner2.shouldTrade')} v={decisionGate?.should_trade === false ? t('scanner2.no') : t('scanner2.observable')} />
                        <Pair k={t('scanner2.regimeState')} v={decisionRegimeLabel(decisionGate?.regime)} />
                        <Pair k={t('scanner2.autoExecute')} v={decisionGate?.allow_auto_trade === false ? t('scanner2.no') : t('scanner2.allowed')} />
                        <Pair k={t('scanner2.executionPref')} v={preferredExecutionLabel(decisionGate?.preferred_execution)} />
                        <Pair k={t('scanner2.candidateCount')} v={decisionGate?.candidate_count ?? '--'} />
                        <Pair k={t('scanner2.confidence')} v={decisionGate?.confidence != null ? pct(Number(decisionGate.confidence) * 100) : '--'} />
                      </dl>
                      <DecisionNotes gate={decisionGate} />
                    </div>
                    <div className="panel">
                      <SectionTitle title={t('scanner2.decisionConsistency')} />
                      <dl className="intent">
                        <Pair k={t('scanner2.status')} v={decisionConsistencyLabel(decisionConsistency)} />
                        <Pair k={t('scanner2.primaryContract')} v={decisionConsistency?.primary_contract || '--'} />
                        <Pair k={t('scanner2.topRanked')} v={decisionConsistency?.top_ranked_contract || '--'} />
                        <Pair k={t('scanner2.aiSelected')} v={decisionConsistency?.ai_selected_contract || '--'} />
                        <Pair k={t('scanner2.scoreGap')} v={fmt(decisionConsistency?.score_gap)} />
                        <Pair k={t('scanner2.source')} v={primarySourceLabel(decisionConsistency?.primary_source)} />
                        <Pair k={t('scanner2.structureValidation')} v={decisionValidationLabel(payload?.decision_validation_status)} />
                      </dl>
                      {decisionConsistency?.message && <p className="planner-note">{decisionConsistency.message}</p>}
                      {payload?.decision_validation?.warnings?.length ? <div className="mini-list decision-notes">{payload.decision_validation.warnings.map((item) => <span key={item} className="warning-scope">{item}</span>)}</div> : null}
                    </div>
                    <div className="panel">
                      <SectionTitle title={t('scanner2.intradayTools')} />
                      <dl className="intent">
                        <Pair k={<>15m <Term name="ORB">ORB</Term></>} v={intradayTools.opening_ranges?.['15m']?.state ?? '--'} />
                        <Pair k="EMA9/20" v={intradayTools.ema_trend?.state ?? '--'} />
                        <Pair k={<Term name="RVOL">RVOL</Term>} v={fmt(intradayTools.relative_volume?.rvol_time_adjusted)} />
                        <Pair k="MACD" v={intradayTools.macd_momentum?.state ?? '--'} />
                        <Pair k={<Term name="ORH">ORH</Term>} v={fmt(intradayTools.opening_ranges?.['15m']?.high)} />
                        <Pair k={<Term name="ORL">ORL</Term>} v={fmt(intradayTools.opening_ranges?.['15m']?.low)} />
                      </dl>
                    </div>
                    <div className="panel">
                      <SectionTitle title={t('scanner2.riskScenario')} />
                      <dl className="intent">
                        <Pair k={t('scanner2.maxLossPerContractShort')} v={`$${fmt(topPick?.risk_plan?.max_loss_per_contract)}`} />
                        <Pair k={t('scanner2.stopPrice')} v={fmt(topPick?.risk_plan?.stop_loss_option_price)} />
                        <Pair k={t('scanner2.takeProfitRange')} v={`${fmt(topPick?.risk_plan?.take_profit_1)} / ${fmt(topPick?.risk_plan?.take_profit_2)}`} />
                        <Pair k={t('scanner2.latestExit')} v={topPick?.risk_plan?.latest_exit ?? '--'} />
                        <Pair k={t('scanner2.plus2pctTheory')} v={fmt(topPick?.scenario_prices?.['underlying_+2pct_now'])} />
                        <Pair k={t('scanner2.minus2pctTheory')} v={fmt(topPick?.scenario_prices?.['underlying_-2pct_now'])} />
                        <Pair k={t('scanner2.oneDayDecay')} v={fmt(topPick?.scenario_prices?.one_day_decay)} />
                        <Pair k={t('scanner2.rewardRiskScore')} v={fmt(topPick?.reward_risk_score)} />
                      </dl>
                    </div>
                    <div className="panel">
                      <SectionTitle title={t('scanner2.relatedNews')} />
                      <p className="muted">{t('scanner2.source')}: {payload?.latest_news_source || 'none'}</p>
                      <div className="news">
                        {(payload?.latest_news_titles ?? []).slice(0, 8).map((item) => (
                          <a key={`${item.title}-${item.published_at}`} href={item.url || '#'} target="_blank" rel="noreferrer">
                            <span>{item.title}</span>
                            <small>{item.published_at}</small>
                          </a>
                        ))}
                        {!payload?.latest_news_titles?.length && <p className="muted">{t('scanner2.noNews')}</p>}
                      </div>
                    </div>
                  </div>
                )}
                {scannerResultTab === 'raw' && (
                  <LazyJsonPanel title={t('scanner2.rawAnalysisData')} data={{ payload, charts: result?.charts, activeScan }} />
                )}
              </div>
            </>
          )}
        </section>
      </div>
    </main>
  );
}

function ScannerPageHeader({ payload, canTrade, onOpenGuide, onOpenPresetGuide, onOpenWatchlists, onOpenBetaLottery, onOpenTrading }) {
  const { embedded } = useAppShell();
  const heroSymbol = payload?.symbol || window._t('scanner.ready');
  if (!embedded) return null;
  return (
    <PageHeader
      eyebrow="Scan Console"
      title={t("scanner.title")}
      subtitle={t("scanner.subtitle")}
      meta={
        <span className="context-bar-clock" title={payload?.symbol || ''}>
          <span className={`context-bar-dot ${payload?.symbol ? 'live' : 'idle'}`} aria-hidden />
          {heroSymbol}
        </span>
      }
      actions={
        <>
          <button className="ghost" type="button" onClick={onOpenGuide}>{t("scanner.guide")}</button>
          <button className="ghost" type="button" onClick={onOpenPresetGuide}>{t("scanner.presets")}</button>
          <button className="ghost" type="button" onClick={onOpenBetaLottery}>{t("scanner.lottery")}</button>
          <button className="primary" type="button" disabled={!canTrade} title={canTrade ? t('scanner2.enterLiveTrading') : t('scanner2.noLivePermission')} onClick={onOpenTrading}>{t("scanner.trading")}</button>
        </>
      }
    />
  );
}
