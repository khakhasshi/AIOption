import { useEffect, useRef, useState } from 'react';
import {
  PROMPT_PREVIEW_LIMIT,
  PROTECTION_POLL_INTERVAL_MS,
  TRADING_POLL_INTERVAL_MS,
  defaultAnalysisModules,
  defaultScheduleSlots,
  defaultTradingConfig,
} from '../config.js';
import { useVisibilityInterval } from './use-visibility-interval.js';
import {
  buildBlockedStrategyItems,
  buildInstanceListStats,
  instanceFilterMatch,
  mergeRunSummary,
  normalizeRunId,
  runNeedsProtectionPolling,
  tradingCreateBlockers,
  typedTradingConfirmation,
  userErrorLabel,
} from '../utils/display.js';
import { normalizeStrategyModes, normalizeSymbol } from '../utils/trading-inputs.js';
import { loadPageCache, savePageCache } from '../session.js';
import { t } from '../i18n/index.js';

export function useTradingController({
  api,
  accounts = [],
  analysisPresets = [],
  fallbackProvider,
  providers = [],
  detailRunId = '',
  detailMode = false,
  onBack,
  routeMode,
}) {
  const [config, setConfig] = useState(() => {
    const cached = loadPageCache(`trading_config:${routeMode}`);
    return cached ? { ...defaultTradingConfig, ...cached } : defaultTradingConfig;
  });
  const [brokerAccounts, setBrokerAccounts] = useState([]);
  const [readiness, setReadiness] = useState(null);
  const [snapshots, setSnapshots] = useState(null);
  const [runs, setRuns] = useState(() => loadPageCache(`trading_runs:${routeMode}`) ?? []);
  const [activeRun, setActiveRun] = useState(() => {
    const cachedRuns = loadPageCache(`trading_runs:${routeMode}`);
    return (cachedRuns?.[0]) ?? null;
  });
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [flattening, setFlattening] = useState(false);
  const [flattenResult, setFlattenResult] = useState(null);
  const [monitorResult, setMonitorResult] = useState(null);
  const [aiQuality, setAiQuality] = useState(null);
  const [instanceActionRunning, setInstanceActionRunning] = useState(false);
  const [instanceActionResult, setInstanceActionResult] = useState(null);
  const [instanceDetailTab, setInstanceDetailTab] = useState('overview');
  const [instanceListFilter, setInstanceListFilter] = useState('all');
  const [instanceListExpanded, setInstanceListExpanded] = useState(false);
  const [selectedRunIds, setSelectedRunIds] = useState([]);
  const [error, setError] = useState('');
  // Polling guard — coalesces overlapping ticks so a slow backend doesn't queue duplicate requests.
  const pollInflightRef = useRef(false);

  useEffect(() => {
    refreshTradingConfig();
    refreshTradingReadiness();
    refreshTradingSnapshots(false);
    refreshTradingRuns();
    refreshBrokerAccounts();
  }, [routeMode]);

  useEffect(() => {
    if (detailRunId) refreshTradingRun(detailRunId).catch((detailError) => setError(detailError.message));
  }, [detailRunId]);

  const needsProtectionPolling = runNeedsProtectionPolling(activeRun);
  const tradingPollEnabled = Boolean(
    activeRun && (['queued', 'running'].includes(activeRun.status) || needsProtectionPolling),
  );
  const tradingPollDelay = needsProtectionPolling ? PROTECTION_POLL_INTERVAL_MS : TRADING_POLL_INTERVAL_MS;
  // Polling uses light mode — backend skips scan_results/council/selections (≈50–150 KB) on every tick.
  useVisibilityInterval(
    async () => {
      if (!activeRun?.id) return;
      if (pollInflightRef.current) return; // skip if previous tick still running
      pollInflightRef.current = true;
      try {
        await refreshTradingRun(activeRun.id, { light: true });
      } catch {
        // swallow — next tick will retry
      } finally {
        pollInflightRef.current = false;
      }
    },
    tradingPollDelay,
    { enabled: tradingPollEnabled },
  );

  useEffect(() => {
    setSelectedRunIds((current) => current.filter((runId) => runs.some((run) => run.id === runId)));
  }, [runs]);

  useEffect(() => {
    setInstanceListExpanded(false);
  }, [instanceListFilter]);

  useEffect(() => {
    setConfig((current) => {
      const nextProvider = providers.some((item) => item.name === current.ai_provider)
        ? current.ai_provider
        : (providers[0]?.name || fallbackProvider || 'deepseek');
      const hasSelectedAccount = accounts.some((item) => item.name === current.longbridge_account);
      const nextAccount = hasSelectedAccount
        ? current.longbridge_account
        : (current.longbridge_account ? '' : (accounts.find((item) => item.is_default)?.name || accounts[0]?.name || ''));
      const usesBrokerAccount = current.broker === 'alpaca' || current.broker === 'usmart';
      const brokerPool = brokerAccounts.filter((item) => item.broker === current.broker);
      const hasSelectedBrokerAccount = brokerPool.some((item) => item.name === current.broker_account);
      const nextBrokerAccount = usesBrokerAccount
        ? (hasSelectedBrokerAccount ? current.broker_account : (brokerPool.find((item) => item.is_default)?.name || brokerPool[0]?.name || current.broker_account || ''))
        : current.broker_account;
      if (nextProvider === current.ai_provider && nextAccount === current.longbridge_account && nextBrokerAccount === current.broker_account && current.broker) return current;
      return { ...current, ai_provider: nextProvider, longbridge_account: nextAccount, broker_account: nextBrokerAccount, broker: current.broker || 'longbridge' };
    });
  }, [providers, accounts, brokerAccounts, fallbackProvider]);

  async function refreshBrokerAccounts() {
    try {
      const rows = await api('/api/brokers/accounts');
      setBrokerAccounts(Array.isArray(rows) ? rows : []);
    } catch {
      setBrokerAccounts([]);
    }
  }

  async function refreshTradingConfig() {
    const row = await api('/api/trading/config');
    const next = {
      ...defaultTradingConfig,
      ...row,
      ai_provider: row.ai_provider || fallbackProvider,
      broker: row.broker || 'longbridge',
      broker_account: row.broker_account || '',
      longbridge_account: row.longbridge_account || '',
      universe: row.universe?.length ? row.universe : defaultTradingConfig.universe,
      analysis_modules: { ...defaultAnalysisModules, ...(row.analysis_modules || {}) },
      strategy_modes: normalizeStrategyModes(row.strategy_modes || ['single_leg']),
    };
    savePageCache(`trading_config:${routeMode}`, next);
    setConfig(next);
  }

  async function refreshTradingReadiness() {
    const row = await api('/api/trading/readiness');
    setReadiness(row);
  }

  async function refreshTradingSnapshots(refresh = true) {
    try {
      const row = await api(`/api/trading/snapshots?days=30&refresh=${refresh ? 'true' : 'false'}`);
      setSnapshots(row);
    } catch (snapshotError) {
      setSnapshots((current) => current ? { ...current, error: snapshotError.message } : { error: snapshotError.message, curve: [] });
    }
  }

  async function refreshTradingRuns() {
    const rows = await api('/api/trading/runs?limit=20');
    savePageCache(`trading_runs:${routeMode}`, rows);
    setRuns(rows);
    setActiveRun((current) => {
      if (!current) return detailRunId ? null : rows[0] || null;
      const summary = rows.find((row) => row.id === current.id);
      return summary ? mergeRunSummary(current, summary) : (detailRunId ? current : rows[0] || null);
    });
    refreshAiQuality().catch(() => null);
  }

  async function refreshAiQuality() {
    const row = await api('/api/trading/ai-quality?limit=50');
    setAiQuality(row);
  }

  async function refreshTradingRun(runId, { light = false } = {}) {
    const safeRunId = normalizeRunId(runId);
    if (!safeRunId) return null;
    const path = light
      ? `/api/trading/runs/${encodeURIComponent(safeRunId)}?light=true`
      : `/api/trading/runs/${encodeURIComponent(safeRunId)}`;
    const row = await api(path);
    if (light) {
      // Merge: keep prior entry-time blobs that the light response intentionally omits.
      setActiveRun((current) => {
        if (!current || current.id !== row.id) return row;
        return {
          ...row,
          scan_results: row.scan_results ?? current.scan_results,
          council: row.council ?? current.council,
          selections: row.selections ?? current.selections,
        };
      });
    } else {
      setActiveRun(row);
    }
    if (!['queued', 'running'].includes(row.status)) {
      setRunning(false);
      await refreshTradingRuns();
      await refreshTradingReadiness();
      await refreshTradingSnapshots();
      await refreshAiQuality();
    }
    return row;
  }

  async function runMonitorOnce() {
    setError('');
    try {
      const result = await api('/api/trading/monitor', { method: 'POST' });
      setMonitorResult(result);
      if (activeRun) await refreshTradingRun(activeRun.id);
      await refreshTradingRuns();
      await refreshTradingReadiness();
      await refreshTradingSnapshots();
    } catch (monitorError) {
      setError(monitorError.message);
    }
  }

  async function flattenAllPositions() {
    setError('');
    setFlattenResult(null);
    const usesBrokerAccount = config.broker === 'alpaca' || config.broker === 'usmart';
    const brokerLabel = config.broker === 'alpaca' ? 'Alpaca' : (config.broker === 'usmart' ? 'uSMART' : 'Longbridge');
    const accountLabel = usesBrokerAccount ? config.broker_account : config.longbridge_account;
    if (!typedTradingConfirmation('全平', t('trading2.confirmFlattenAll').replace('{broker}', brokerLabel).replace('{account}', accountLabel || '--'))) return;
    setFlattening(true);
    try {
      const result = await api('/api/trading/flatten', {
        method: 'POST',
        body: JSON.stringify({ confirmation: '全平' }),
      });
      setFlattenResult(result);
      await refreshTradingSnapshots(true);
      await refreshTradingReadiness();
      await refreshTradingRuns();
      await refreshAiQuality();
    } catch (flattenError) {
      setError(flattenError.message);
    } finally {
      setFlattening(false);
    }
  }

  async function cancelInstanceOrders() {
    if (!activeRun?.id) return;
    setError('');
    setInstanceActionResult(null);
    if (!typedTradingConfirmation('撤实例', t('trading2.confirmCancelOrders'))) return;
    setInstanceActionRunning(true);
    try {
      const result = await api(`/api/trading/runs/${encodeURIComponent(activeRun.id)}/cancel-orders`, {
        method: 'POST',
        body: JSON.stringify({ confirmation: '撤实例' }),
      });
      setInstanceActionResult({ action: 'cancel', ...result });
      await refreshTradingRun(activeRun.id);
      await refreshTradingRuns();
      await refreshAiQuality();
    } catch (actionError) {
      setError(actionError.message);
    } finally {
      setInstanceActionRunning(false);
    }
  }

  async function flattenCurrentInstance() {
    if (!activeRun?.id) return;
    setError('');
    setInstanceActionResult(null);
    if (!typedTradingConfirmation('平实例', t('trading2.confirmFlattenInstance'))) return;
    setInstanceActionRunning(true);
    try {
      const result = await api(`/api/trading/runs/${encodeURIComponent(activeRun.id)}/flatten`, {
        method: 'POST',
        body: JSON.stringify({ confirmation: '平实例' }),
      });
      setInstanceActionResult({ action: 'flatten', ...result });
      await refreshTradingRun(activeRun.id);
      await refreshTradingRuns();
      await refreshTradingSnapshots(true);
      await refreshTradingReadiness();
      await refreshAiQuality();
    } catch (actionError) {
      setError(actionError.message);
    } finally {
      setInstanceActionRunning(false);
    }
  }

  async function resetCurrentInstanceRisk() {
    if (!activeRun?.id) return;
    setError('');
    setInstanceActionResult(null);
    const confirmed = window.confirm(t('trading2.confirmResetRisk'));
    if (!confirmed) return;
    setInstanceActionRunning(true);
    try {
      const result = await api(`/api/trading/runs/${encodeURIComponent(activeRun.id)}/reset-risk`, {
        method: 'POST',
        body: JSON.stringify({ confirmation: '初始化风控' }),
      });
      setInstanceActionResult({ action: 'reset-risk', ...result });
      await refreshTradingRun(activeRun.id);
      await refreshTradingRuns();
      await refreshAiQuality();
    } catch (actionError) {
      setError(actionError.message);
    } finally {
      setInstanceActionRunning(false);
    }
  }

  async function deleteCurrentInstance() {
    if (!activeRun?.id) return;
    setError('');
    setInstanceActionResult(null);
    const runId = activeRun.id;
    const confirmed = window.confirm(t('trading2.confirmDeleteInstance'));
    if (!confirmed) return;
    setInstanceActionRunning(true);
    try {
      const result = await api(`/api/trading/runs/${encodeURIComponent(runId)}/delete`, {
        method: 'POST',
        body: JSON.stringify({ confirmation: '删除实例' }),
      });
      setInstanceActionResult({ action: 'delete', ...result });
      const rows = await api('/api/trading/runs?limit=20');
      setRuns(rows);
      setActiveRun(rows[0] || null);
      await refreshTradingReadiness();
      await refreshTradingSnapshots(false);
      await refreshAiQuality();
      if (detailMode) onBack();
    } catch (actionError) {
      setError(actionError.message);
    } finally {
      setInstanceActionRunning(false);
    }
  }

  async function bulkDeleteInstances() {
    const ids = selectedRunIds.filter((runId) => runs.some((run) => run.id === runId));
    if (!ids.length) return;
    setError('');
    setInstanceActionResult(null);
    const confirmed = window.confirm(t('trading2.confirmBulkDelete').replace('{n}', ids.length));
    if (!confirmed) return;
    setInstanceActionRunning(true);
    try {
      const result = await api('/api/trading/runs/bulk-delete', {
        method: 'POST',
        body: JSON.stringify({ confirmation: '批量删除实例', run_ids: ids }),
      });
      setInstanceActionResult({ action: 'bulk-delete', ...result });
      setSelectedRunIds([]);
      const rows = await api('/api/trading/runs?limit=20');
      setRuns(rows);
      setActiveRun((current) => current ? (rows.find((row) => row.id === current.id) || rows[0] || null) : rows[0] || null);
      await refreshTradingReadiness();
      await refreshTradingSnapshots(false);
      await refreshAiQuality();
    } catch (actionError) {
      setError(actionError.message);
    } finally {
      setInstanceActionRunning(false);
    }
  }

  function openInstanceDetailPage(runId = activeRun?.id) {
    const safeRunId = normalizeRunId(runId) || activeRun?.id;
    if (!safeRunId) return;
    const path = `/trading/instances/${encodeURIComponent(safeRunId)}`;
    window.history.pushState({}, '', path);
    window.dispatchEvent(new Event('popstate'));
  }

  async function saveTradingConfig(event) {
    event?.preventDefault();
    setSaving(true);
    setError('');
    try {
      const row = await persistTradingConfig();
      setConfig((current) => ({ ...current, ...row }));
      await refreshTradingReadiness();
      await refreshTradingSnapshots();
      return row;
    } catch (saveError) {
      setError(saveError.message);
      if (!event) throw saveError;
      return null;
    } finally {
      setSaving(false);
    }
  }

  async function persistTradingConfig() {
    return api('/api/trading/config', {
      method: 'PUT',
      body: JSON.stringify(config),
    });
  }

  async function runTradingNow() {
    setRunning(true);
    setError('');
    try {
      const savedConfig = await saveTradingConfig();
      if (!savedConfig) {
        setRunning(false);
        return;
      }
      const ready = await api('/api/trading/readiness');
      setReadiness(ready);
      if (!ready.readiness?.ok) {
        throw new Error((ready.readiness?.issues || ['trading readiness check failed']).join('；'));
      }
      const row = await api('/api/trading/run-now', { method: 'POST' });
      setActiveRun(row);
      await refreshTradingRuns();
      await refreshTradingRun(row.id);
    } catch (runError) {
      setError(userErrorLabel(runError.message));
      setRunning(false);
    }
  }

  function toggleTradingStrategyMode(mode) {
    setConfig((current) => {
      const normalized = normalizeStrategyModes(current.strategy_modes);
      const next = normalized.includes(mode)
        ? normalized.filter((item) => item !== mode)
        : [...normalized, mode];
      return { ...current, strategy_modes: normalizeStrategyModes(next.length ? next : ['single_leg']) };
    });
  }

  function scheduleSlotsForConfig(current = config) {
    const slots = Array.isArray(current.schedule_slots) && current.schedule_slots.length ? current.schedule_slots : defaultScheduleSlots;
    return slots.map((slot, index) => ({ ...slot, slot_id: slot.slot_id || `slot_${index + 1}` }));
  }

  function updateScheduleSlot(slotId, patch) {
    setConfig((current) => {
      const slots = scheduleSlotsForConfig(current).map((slot) => (slot.slot_id === slotId ? { ...slot, ...patch } : slot));
      return { ...current, schedule_slots: slots };
    });
  }

  function resetScheduleSlots() {
    setConfig((current) => ({ ...current, schedule_slots: defaultScheduleSlots.map((slot) => ({ ...slot })) }));
  }

  function setSingleInstanceEnabled(enabled) {
    setConfig((current) => ({
      ...current,
      single_instance_enabled: enabled,
      schedule_profile: enabled || current.multi_instance_enabled ? current.schedule_profile : 'single_run',
    }));
  }

  function setMultiInstanceEnabled(enabled) {
    setConfig((current) => {
      if (enabled) {
        return {
          ...current,
          multi_instance_enabled: true,
          single_instance_enabled: false,
          schedule_profile: current.schedule_profile && current.schedule_profile !== 'single_run' ? current.schedule_profile : 'balanced_multi_slot',
          schedule_slots: scheduleSlotsForConfig(current),
        };
      }
      return {
        ...current,
        multi_instance_enabled: false,
        schedule_profile: current.single_instance_enabled !== false ? 'single_run' : current.schedule_profile,
      };
    });
  }

  function applyTradingPreset(preset) {
    const targetSymbol = normalizeSymbol(config.universe?.[0]) || 'NVDA';
    const template = String(preset?.query_template || '').replaceAll('{symbol}', targetSymbol);
    setConfig((current) => ({
      ...current,
      prompt_template: template || current.prompt_template,
      strategy_modes: Array.isArray(preset?.strategy_modes) && preset.strategy_modes.length
        ? normalizeStrategyModes(preset.strategy_modes)
        : current.strategy_modes,
      analysis_modules: preset?.analysis_modules
        ? { ...defaultAnalysisModules, ...preset.analysis_modules }
        : current.analysis_modules,
    }));
    setError('');
  }

  const selections = activeRun?.selections ?? [];
  const orders = activeRun?.orders ?? [];
  const scans = activeRun?.scan_results ?? [];
  const advisorReports = activeRun?.council?.advisor_reports ?? [];
  const councilMode = activeRun?.council?.council_mode;
  const tradeInstance = activeRun?.trade_instance ?? {};
  const instanceRisk = tradeInstance.risk_plan ?? {};
  const instanceExecution = tradeInstance.execution_plan ?? {};
  const strategyPositions = Array.isArray(instanceRisk.strategy_positions) ? instanceRisk.strategy_positions : [];
  const strategyExecutionOrders = Array.isArray(instanceExecution.strategy_orders) ? instanceExecution.strategy_orders : [];
  const protection = tradeInstance.protection_status ?? {};
  const blockedStrategyItems = buildBlockedStrategyItems({
    orders,
    strategyPositions,
    protectionContracts: Array.isArray(protection.contracts) ? protection.contracts : [],
  });
  const timeline = Array.isArray(tradeInstance.event_timeline) ? tradeInstance.event_timeline.slice(-6).reverse() : [];
  const readinessState = readiness?.readiness;
  const readinessIssues = readinessState?.issues ?? [];
  const readinessWarnings = readinessState?.warnings ?? [];
  const strategySnapshot = snapshots?.strategy ?? {};
  const executionSnapshot = snapshots?.executions ?? {};
  const curve = snapshots?.curve ?? [];
  const createBlockers = tradingCreateBlockers({ saving, running, config, readinessState });
  const canRunNow = createBlockers.length === 0;
  const instanceStats = buildInstanceListStats(runs);
  const filteredRuns = runs.filter((run) => instanceFilterMatch(run, instanceListFilter));
  const visibleRuns = instanceListExpanded ? filteredRuns : filteredRuns.slice(0, 3);
  const visibleRunIds = visibleRuns.map((run) => run.id);
  const selectedRunCount = selectedRunIds.length;
  const selectedVisibleRunCount = visibleRunIds.filter((runId) => selectedRunIds.includes(runId)).length;
  const allVisibleRunsSelected = visibleRunIds.length > 0 && selectedVisibleRunCount === visibleRunIds.length;
  const selectedStrategyModes = normalizeStrategyModes(config.strategy_modes);
  const hasMultiLegStrategy = selectedStrategyModes.some((mode) => mode !== 'single_leg');
  const strategyAnalysisOnly = hasMultiLegStrategy && !config.strategy_auto_execute_enabled;
  const visibleTradingPresets = Array.isArray(analysisPresets) ? analysisPresets.slice(0, PROMPT_PREVIEW_LIMIT) : [];
  const nextRunLabel = config.multi_instance_enabled ? t('trading2.nextSlotEt') : t('trading2.nextCreateEt');
  const nextRunSub = readiness?.next_run_slot?.label
    ? `${readiness.next_run_slot.label} · ${readiness.next_run_slot.time_et || 'America/New_York'}`
    : readiness?.next_run_at_et
      ? 'America/New_York'
      : '--';

  return {
    activeRun,
    advisorReports,
    aiQuality,
    allVisibleRunsSelected,
    applyTradingPreset,
    blockedStrategyItems,
    brokerAccounts,
    bulkDeleteInstances,
    canRunNow,
    cancelInstanceOrders,
    config,
    councilMode,
    createBlockers,
    curve,
    deleteCurrentInstance,
    error,
    executionSnapshot,
    filteredRuns,
    flattenAllPositions,
    flattenCurrentInstance,
    flattenResult,
    flattening,
    hasMultiLegStrategy,
    instanceActionResult,
    instanceActionRunning,
    instanceDetailTab,
    instanceListExpanded,
    instanceListFilter,
    instanceRisk,
    instanceStats,
    monitorResult,
    nextRunLabel,
    nextRunSub,
    openInstanceDetailPage,
    orders,
    protection,
    readiness,
    readinessIssues,
    readinessState,
    readinessWarnings,
    refreshAiQuality,
    refreshTradingReadiness,
    refreshTradingRun,
    refreshTradingRuns,
    refreshTradingSnapshots,
    resetCurrentInstanceRisk,
    resetScheduleSlots,
    runMonitorOnce,
    runTradingNow,
    running,
    saveTradingConfig,
    saving,
    scans,
    scheduleSlotsForConfig,
    selectedRunCount,
    selectedRunIds,
    selectedStrategyModes,
    selections,
    setConfig,
    setInstanceDetailTab,
    setInstanceListExpanded,
    setInstanceListFilter,
    setMultiInstanceEnabled,
    setSelectedRunIds,
    setSingleInstanceEnabled,
    snapshots,
    strategyAnalysisOnly,
    strategyExecutionOrders,
    strategyPositions,
    strategySnapshot,
    timeline,
    toggleTradingStrategyMode,
    tradeInstance,
    updateScheduleSlot,
    visibleRunIds,
    visibleRuns,
    visibleTradingPresets,
  };
}
