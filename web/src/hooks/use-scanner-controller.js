import { useEffect, useState } from 'react';
import {
  SCAN_HISTORY_PAGE_SIZE,
  SCAN_POLL_INTERVAL_MS,
  defaultAnalysisModules,
  quickPrompts,
} from '../config.js';
import { t } from '../i18n/index.js';
import { buildScanBlockers } from '../utils/display.js';
import { normalizeStrategyModes } from '../utils/trading-inputs.js';
import { useVisibilityInterval } from './use-visibility-interval.js';

const VALID_SORT_KEYS = new Set(['decision_score', 'analysis_score', 'execution_score', 'liquidity_score', 'delta', 'theta_per_day']);
const VALID_STRATEGY_MODES = new Set(['single_leg', 'spread', 'straddle', 'iron_condor', 'butterfly']);

function readInitialFromUrl() {
  if (typeof window === 'undefined') return {};
  try {
    const params = new URLSearchParams(window.location.search);
    const next = {};
    const sym = (params.get('symbol') || '').trim().toUpperCase();
    if (sym && /^[A-Z0-9.\-]{1,12}$/.test(sym)) next.symbol = sym;
    const sort = params.get('sort');
    if (sort && VALID_SORT_KEYS.has(sort)) next.sort = sort;
    const modes = (params.get('modes') || '').split(',').map((m) => m.trim()).filter((m) => VALID_STRATEGY_MODES.has(m));
    if (modes.length) next.modes = modes;
    return next;
  } catch {
    return {};
  }
}

function writeUrlParam(key, value, { defaultValue } = {}) {
  if (typeof window === 'undefined') return;
  try {
    const url = new URL(window.location.href);
    const isDefault = value === defaultValue || value === '' || value == null
      || (Array.isArray(value) && value.length === 0);
    if (isDefault) {
      url.searchParams.delete(key);
    } else {
      url.searchParams.set(key, Array.isArray(value) ? value.join(',') : String(value));
    }
    const nextUrl = url.pathname + (url.search ? url.search : '') + url.hash;
    if (nextUrl !== window.location.pathname + window.location.search + window.location.hash) {
      window.history.replaceState({}, '', nextUrl);
    }
  } catch {
    /* ignore */
  }
}

export function useScannerController({ api, providers = [], session }) {
  const initialUrl = readInitialFromUrl();
  const [query, setQuery] = useState(quickPrompts[1]);
  const [symbol, setSymbol] = useState(initialUrl.symbol || '');
  const [aiProvider, setAiProvider] = useState('deepseek');
  const [longbridgeAccount, setLongbridgeAccount] = useState('');
  const [marketDataSource, setMarketDataSource] = useState('yfinance');
  const [optionDataSource, setOptionDataSource] = useState('thetadata');
  const [useAi, setUseAi] = useState(true);
  const [council, setCouncil] = useState(true);
  const [analysisModules, setAnalysisModules] = useState(defaultAnalysisModules);
  const [analysisPresets, setAnalysisPresets] = useState([]);
  const [quickPromptsExpanded, setQuickPromptsExpanded] = useState(false);
  const [strategyModes, setStrategyModes] = useState(initialUrl.modes || ['single_leg']);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);
  const [activeScan, setActiveScan] = useState(null);
  const [scanHistory, setScanHistory] = useState([]);
  const [scanHistoryPage, setScanHistoryPage] = useState(0);
  const [scanHistoryHasNext, setScanHistoryHasNext] = useState(false);
  const [scanHistoryStarredOnly, setScanHistoryStarredOnly] = useState(false);
  const [scanHistoryQuery, setScanHistoryQuery] = useState('');
  const [scanHistoryTag, setScanHistoryTag] = useState('');
  const [scanTriggers, setScanTriggers] = useState([]);
  const [testingTriggerId, setTestingTriggerId] = useState('');
  const [triggerTestResults, setTriggerTestResults] = useState({});
  const [scannerResultTab, setScannerResultTab] = useState('charts');
  const [candidateSort, setCandidateSort] = useState(initialUrl.sort || 'decision_score');

  // Two-way URL sync for shareable scanner state — back/refresh restores selection.
  useEffect(() => { writeUrlParam('symbol', symbol); }, [symbol]);
  useEffect(() => { writeUrlParam('sort', candidateSort, { defaultValue: 'decision_score' }); }, [candidateSort]);
  useEffect(() => { writeUrlParam('modes', strategyModes); }, [strategyModes]);

  const scanPollEnabled = Boolean(
    session.authenticated && activeScan && ['queued', 'running'].includes(activeScan.status),
  );

  // Real-time scan progress via SSE, with polling as a fallback. When the
  // EventSource is connected we keep a slow poll (30s) purely as a
  // reconciliation safety net; if SSE never connects or errors out, we fall
  // back to the normal fast poll. refreshScan() is idempotent, so a stray
  // poll landing alongside an SSE update is harmless.
  const [sseConnected, setSseConnected] = useState(false);
  const activeScanId = activeScan?.id;
  useEffect(() => {
    if (!scanPollEnabled || !activeScanId) {
      setSseConnected(false);
      return undefined;
    }
    if (typeof EventSource === 'undefined') return undefined;
    let closed = false;
    const source = new EventSource(`/api/scans/${encodeURIComponent(activeScanId)}/events`);
    source.onopen = () => { if (!closed) setSseConnected(true); };
    source.onmessage = (evt) => {
      if (closed) return;
      // Event carries {status, stage, progress}; re-fetch the full run so the
      // existing refreshScan logic (result/error/history) stays the single
      // source of truth.
      refreshScan(activeScanId).catch(() => {});
      try {
        const data = JSON.parse(evt.data);
        if (data?.status === 'succeeded' || data?.status === 'failed') {
          closed = true;
          source.close();
        }
      } catch { /* keep stream open on a malformed frame */ }
    };
    source.onerror = () => {
      // Network/proxy drop or no-Redis backend ending the stream: close and let
      // the fast poll below take over.
      if (!closed) setSseConnected(false);
      closed = true;
      source.close();
    };
    return () => { closed = true; source.close(); setSseConnected(false); };
  }, [scanPollEnabled, activeScanId]);

  useVisibilityInterval(
    () => {
      if (activeScan?.id) refreshScan(activeScan.id);
    },
    sseConnected ? 30000 : SCAN_POLL_INTERVAL_MS,
    { enabled: scanPollEnabled },
  );

  async function refreshAnalysisPresets() {
    try {
      const rows = await api('/api/analysis-presets');
      setAnalysisPresets(rows);
    } catch {
      setAnalysisPresets([]);
    }
  }

  async function refreshHistory(page = scanHistoryPage, starredOnly = scanHistoryStarredOnly, queryText = scanHistoryQuery, tagText = scanHistoryTag) {
    const offset = Math.max(Number(page) || 0, 0) * SCAN_HISTORY_PAGE_SIZE;
    const params = new URLSearchParams({
      limit: String(SCAN_HISTORY_PAGE_SIZE + 1),
      offset: String(offset),
    });
    if (starredOnly) params.set('starred', 'true');
    if (queryText.trim()) params.set('query', queryText.trim());
    if (tagText.trim()) params.set('tag', tagText.trim());
    const rows = await api(`/api/scans?${params.toString()}`);
    setScanHistory(rows.slice(0, SCAN_HISTORY_PAGE_SIZE));
    setScanHistoryHasNext(rows.length > SCAN_HISTORY_PAGE_SIZE);
    setScanHistoryPage(Math.max(Number(page) || 0, 0));
  }

  async function markHistory(scanId, payload) {
    await api(`/api/scans/${encodeURIComponent(scanId)}/mark`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    });
    await refreshHistory(scanHistoryPage, scanHistoryStarredOnly);
  }

  async function refreshScan(scanId) {
    const row = await api(`/api/scans/${encodeURIComponent(scanId)}`);
    setActiveScan(row);
    if (row.status === 'succeeded' && row.result) {
      setResult(row.result);
      setStrategyModes(normalizeStrategyModes(row.strategy_modes || row.result?.payload?.strategy_modes || ['single_leg']));
      setLoading(false);
      await refreshHistory(scanHistoryPage);
    }
    if (row.status === 'failed') {
      setError(row.error || t('scanner2.scanFailed'));
      setLoading(false);
      await refreshHistory(scanHistoryPage);
    }
  }

  async function refreshTriggers() {
    try {
      const rows = await api('/api/scan-triggers');
      setScanTriggers(rows);
    } catch {
      setScanTriggers([]);
    }
  }

  async function runScan(event) {
    event?.preventDefault();
    const blockers = buildScanBlockers({ loading, query, marketDataSource, longbridgeAccount, useAi, aiProvider, providers, canTrade: session.can_trade, strategyModes });
    if (blockers.length) {
      setError(blockers.join('；'));
      return;
    }
    setLoading(true);
    setError('');
    try {
      const row = await api('/api/scans', {
        method: 'POST',
        body: JSON.stringify({
          query,
          symbol: symbol.trim() || null,
          ai_provider: aiProvider,
          longbridge_account: marketDataSource === 'longbridge' ? (longbridgeAccount || null) : null,
          market_data_source: marketDataSource,
          option_data_source: optionDataSource,
          use_ai: useAi,
          council,
          analysis_modules: analysisModules,
          strategy_modes: strategyModes,
        }),
      });
      setActiveScan(row);
      setResult(null);
      await refreshHistory(0);
      await refreshScan(row.id);
    } catch (scanError) {
      setError(scanError.message);
      setLoading(false);
    }
  }

  function applyAnalysisPreset(preset) {
    const targetSymbol = symbol.trim() || 'NVDA';
    const template = String(preset?.query_template || '').replaceAll('{symbol}', targetSymbol);
    if (template) setQuery(template);
    if (Array.isArray(preset?.strategy_modes) && preset.strategy_modes.length) {
      setStrategyModes(normalizeStrategyModes(preset.strategy_modes));
    }
    if (preset?.analysis_modules) {
      setAnalysisModules({ ...defaultAnalysisModules, ...preset.analysis_modules });
    }
    setUseAi(false);
    setError('');
  }

  async function openHistory(row) {
    setActiveScan(row);
    setQuery(row.query);
    setSymbol(row.symbol || '');
    setAiProvider(row.ai_provider);
    const rowSource = row.market_data_source || row.result?.market_data_source || row.payload?.market_data_source || 'yfinance';
    setMarketDataSource(rowSource);
    const rowOptionSource = row.option_data_source || row.result?.option_data_source || row.payload?.option_data_source || 'thetadata';
    setOptionDataSource(rowOptionSource);
    setStrategyModes(normalizeStrategyModes(row.strategy_modes || row.result?.payload?.strategy_modes || row.payload?.strategy_modes || ['single_leg']));
    if (row.market_data_source === 'longbridge' && row.longbridge_account && row.longbridge_account !== 'yfinance') {
      setLongbridgeAccount(row.longbridge_account);
    }
    if (row.result) {
      setResult(row.result);
      setLoading(false);
      setError('');
    } else if (row.status === 'failed') {
      setError(row.error || t('scanner2.scanFailed'));
      setLoading(false);
    }
    if (['queued', 'running'].includes(row.status)) {
      setLoading(true);
      refreshScan(row.id).catch(() => null);
      return;
    }
    if (row.status === 'succeeded' && !row.result) {
      try {
        const detail = await api(`/api/scans/${encodeURIComponent(row.id)}`);
        setActiveScan(detail);
        if (detail.result) {
          setResult(detail.result);
          setStrategyModes(normalizeStrategyModes(detail.strategy_modes || detail.result?.payload?.strategy_modes || ['single_leg']));
          setLoading(false);
          setError('');
        }
      } catch (detailError) {
        setError(detailError.message || t('scanner2.loadHistoryDetailFailed'));
        setLoading(false);
      }
    }
  }

  async function createWaitTriggerFromScan(scan, payload = {}) {
    const symbol = (payload.symbol || scan.symbol || scan.payload?.symbol || '').trim().toUpperCase();
    if (!symbol) {
      throw new Error(t('scanner2.missingSymbolForTrigger'));
    }
    const triggerType = payload.type || 'underlying_price';
    const fallbackValue = triggerType === 'rescan_score' ? 75 : (scan.payload?.last_price ?? scan.payload?.underlying_price ?? 0);
    const triggerValue = Number(payload.value ?? fallbackValue);
    const condition = {
      type: triggerType,
      symbol,
      operator: payload.operator || '>=',
      value: triggerValue,
      market_session: 'regular',
    };
    if (triggerType === 'technical_indicator' || triggerType === 'option_quote') {
      condition.field = payload.field;
      condition.label = payload.label || payload.field;
      if (triggerType === 'option_quote' && payload.contract_symbol) {
        condition.contract_symbol = payload.contract_symbol;
      }
    }
    if (triggerType === 'rescan_score') {
      condition.scan_id = scan.id;
      condition.query = scan.query;
      condition.ai_provider = scan.ai_provider;
      condition.longbridge_account = scan.longbridge_account;
      condition.market_data_source = scan.market_data_source;
      condition.option_data_source = scan.option_data_source;
      condition.use_ai = scan.use_ai ?? true;
      condition.council = scan.council ?? true;
      condition.analysis_modules = scan.analysis_modules || {};
      condition.strategy_modes = scan.strategy_modes || [];
      condition.score_field = payload.score_field || 'decision_score';
    }
    const row = await api('/api/scan-triggers', {
      method: 'POST',
      body: JSON.stringify({
        name: payload.name || (
          triggerType === 'rescan_score'
            ? `${symbol} ${t('scanner2.rescanScoreAlert')}`
            : triggerType === 'technical_indicator'
              ? `${symbol} ${t('scanner2.technicalAlert')}`
              : triggerType === 'option_quote'
                ? `${symbol} ${t('scanner2.optionQuoteAlert')}`
                : `${symbol} ${t('scanner2.priceAlert')}`
        ),
        symbol,
        scan_id: scan.id,
        locator_id: scan.locator_id || scan.id,
        condition,
        notification_channel_ids: payload.notification_channel_ids || [],
        enabled: true,
        check_interval_seconds: Number(payload.check_interval_seconds || 300),
        cooldown_seconds: Number(payload.cooldown_seconds || 1800),
        max_trigger_count: Number(payload.max_trigger_count || 3),
        market_policy: payload.market_policy || (triggerType === 'option_quote' ? 'regular_only' : 'regular_only'),
        opening_grace_minutes: Number(payload.opening_grace_minutes || 10),
      }),
    });
    await refreshTriggers();
    return row;
  }

  async function toggleTrigger(triggerId, enabled) {
    await api(`/api/scan-triggers/${encodeURIComponent(triggerId)}`, {
      method: 'PATCH',
      body: JSON.stringify({ enabled }),
    });
    await refreshTriggers();
  }

  async function deleteTrigger(triggerId) {
    await api(`/api/scan-triggers/${encodeURIComponent(triggerId)}`, { method: 'DELETE' });
    await refreshTriggers();
  }

  async function testTrigger(triggerId) {
    setTestingTriggerId(triggerId);
    setError('');
    try {
      const result = await api(`/api/scan-triggers/${encodeURIComponent(triggerId)}/test`, { method: 'POST' });
      setTriggerTestResults((current) => ({ ...current, [triggerId]: result }));
      return result;
    } catch (testError) {
      setError(testError.message || t('scanner2.testTriggerFailed'));
      throw testError;
    } finally {
      setTestingTriggerId('');
    }
  }

  function toggleStrategyMode(mode) {
    setStrategyModes((current) => {
      const normalized = normalizeStrategyModes(current);
      if (normalized.includes(mode)) {
        const next = normalized.filter((item) => item !== mode);
        return next.length ? next : ['single_leg'];
      }
      return normalizeStrategyModes([...normalized, mode]);
    });
  }

  return {
    activeScan,
    aiProvider,
    analysisModules,
    analysisPresets,
    applyAnalysisPreset,
    candidateSort,
    council,
    error,
    loading,
    longbridgeAccount,
    marketDataSource,
    optionDataSource,
    openHistory,
    query,
    quickPromptsExpanded,
    refreshAnalysisPresets,
    refreshHistory,
    refreshTriggers,
    result,
    runScan,
    scanHistory,
    scanHistoryHasNext,
    scanHistoryPage,
    scanHistoryQuery,
    scanHistoryTag,
    scanTriggers,
    scannerResultTab,
    setAiProvider,
    setAnalysisModules,
    setCandidateSort,
    setCouncil,
    setError,
    setLongbridgeAccount,
    setMarketDataSource,
    setOptionDataSource,
    setQuery,
    setQuickPromptsExpanded,
    setScannerResultTab,
    setStrategyModes,
    setSymbol,
    setUseAi,
    setScanHistoryStarredOnly,
    setScanHistoryQuery,
    setScanHistoryTag,
    strategyModes,
    symbol,
    toggleStrategyMode,
    testTrigger,
    testingTriggerId,
    triggerTestResults,
    useAi,
    markHistory,
    scanHistoryStarredOnly,
    createWaitTriggerFromScan,
    toggleTrigger,
    deleteTrigger,
  };
}
