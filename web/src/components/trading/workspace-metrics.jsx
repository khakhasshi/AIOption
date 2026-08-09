import React from 'react';
import { Metric } from '../common.jsx';
import { fmt, instanceLifecycleLabel, lifecycleTone, pct, runLifecycle, runProtection, runTaskStatusLabel, stageLabel, strategyModesLabel } from '../../utils/display.js';
import { t } from '../../i18n/index.js';

export function WorkspaceMetrics({ activeRun, aiQuality, config, hasMultiLegStrategy, readinessState, strategyAnalysisOnly }) {
  return (
<div className="metrics">
  <Metric label={t('trading2.executionSwitch')} value={config.live_enabled ? t('trading2.on') : t('trading2.off')} sub={readinessState?.ok ? t('trading2.ready') : t('trading2.needsCheck')} tone={readinessState?.ok ? 'ok' : config.live_enabled ? 'warning' : 'muted'} />
  <Metric label={t('trading2.capital')} value={`$${fmt(config.total_capital)}`} tone={Number(config.total_capital) > 0 ? 'ok' : 'danger'} />
  <Metric label={t('trading2.universe')} value={Array.isArray(config.universe) ? config.universe.length : 0} />
  <Metric label={t('trading2.topN')} value={config.top_n} />
  <Metric label={t('trading2.strategyMode')} value={strategyModesLabel(config.strategy_modes || ['single_leg'])} sub={hasMultiLegStrategy ? (strategyAnalysisOnly ? t('trading2.structureAnalysisMode') : t('trading2.strategyAutoOrder')) : t('trading2.singleLegAutoOrder')} />
  <Metric label={t('trading2.stopLoss')} value={`${fmt(config.default_stop_loss_pct)}%`} />
  <Metric
    label={t('trading2.takeProfit')}
    value={config.tiered_take_profit_enabled ? `TP1 ${fmt(config.default_take_profit_1_pct ?? 20)}%` : `${fmt(config.default_take_profit_pct ?? 30)}%`}
    sub={config.tiered_take_profit_enabled ? `TP2 ${fmt(config.default_take_profit_2_pct ?? 35)}%` : t('trading2.oneShotTakeProfit')}
  />
  <Metric label={t('trading2.latestInstance')} value={activeRun ? instanceLifecycleLabel(activeRun) : '--'} sub={activeRun ? `${runTaskStatusLabel(activeRun.status)} · ${stageLabel(activeRun.stage)} · ${activeRun.progress}%` : '--'} tone={activeRun ? lifecycleTone(runLifecycle(activeRun), runProtection(activeRun)) : 'muted'} />
  <Metric label={t('trading2.aiSamples')} value={aiQuality?.sample_size ?? 0} sub={`${t('trading2.winRateShort')} ${pct(aiQuality?.win_rate)}`} />
</div>
  );
}
