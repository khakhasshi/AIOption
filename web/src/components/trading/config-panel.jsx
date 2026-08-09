import React from 'react';
import { SectionTitle, Toggle } from '../common.jsx';
import { TradingUniverseEditor } from '../trading-universe-editor.jsx';
import { PROMPT_PREVIEW_LIMIT, analysisModuleItems, marketDataSourceItems, optionDataSourceItems, orderTypeItems, strategyModeItems } from '../../config.js';
import { normalizeStrategyModes } from '../../utils/trading-inputs.js';
import { t } from '../../i18n/index.js';

export function TradingConfigPanel({
  accounts,
  analysisPresets,
  applyTradingPreset,
  brokerAccounts = [],
  canRunNow,
  config,
  createBlockers,
  hasMultiLegStrategy,
  onPresetGuide,
  providers,
  resetScheduleSlots,
  runTradingNow,
  running,
  saveTradingConfig,
  saving,
  scheduleSlotsForConfig,
  selectedStrategyModes,
  setConfig,
  setMultiInstanceEnabled,
  setSingleInstanceEnabled,
  strategyAnalysisOnly,
  toggleTradingStrategyMode,
  updateScheduleSlot,
  visibleTradingPresets,
}) {
  const alpacaAccounts = brokerAccounts.filter((account) => account.broker === 'alpaca');
  const usmartAccounts = brokerAccounts.filter((account) => account.broker === 'usmart');
  // Alpaca and uSMART both use broker_account (broker_store); Longbridge uses longbridge_account.
  const usesBrokerAccount = config.broker === 'alpaca' || config.broker === 'usmart';
  const activeBrokerAccounts = config.broker === 'usmart' ? usmartAccounts : alpacaAccounts;
  const selectedBrokerLabel = usesBrokerAccount
    ? (activeBrokerAccounts.find((account) => account.name === config.broker_account)?.label || config.broker_account || t('trading2.notSelected'))
    : (accounts.find((account) => account.name === config.longbridge_account)?.label || config.longbridge_account || t('trading2.notSelected'));
  const brokerDisplay = config.broker === 'alpaca' ? 'Alpaca' : (config.broker === 'usmart' ? 'uSMART' : 'Longbridge');

  return (
<aside className="panel control-panel">
  <SectionTitle title={t('trading2.instanceSettings')} />
  <form className="form" onSubmit={saveTradingConfig}>
    <div className="switches">
      <Toggle checked={config.live_enabled} onChange={(checked) => setConfig({ ...config, live_enabled: checked })} label={t('trading2.enableLive')} />
      <Toggle
        checked={config.use_ai !== false}
        onChange={(checked) => setConfig({ ...config, use_ai: checked, council: checked ? config.council !== false : false })}
        label={t('trading2.enableAi')}
      />
      <Toggle
        checked={config.use_ai !== false && config.council !== false}
        disabled={config.use_ai === false}
        onChange={(checked) => setConfig({ ...config, council: checked })}
        label={t('trading2.council')}
      />
      <Toggle checked={config.ai_adjust_allocation} onChange={(checked) => setConfig({ ...config, ai_adjust_allocation: checked })} label={t('trading2.aiAdjustAllocation')} />
      <Toggle checked={config.ai_adjust_stop_loss} onChange={(checked) => setConfig({ ...config, ai_adjust_stop_loss: checked })} label={t('trading2.aiAdjustStopLoss')} />
      <Toggle checked={config.ai_adjust_take_profit} onChange={(checked) => setConfig({ ...config, ai_adjust_take_profit: checked })} label={t('trading2.aiAdjustTakeProfit')} />
      <Toggle checked={config.software_stop_enabled} onChange={(checked) => setConfig({ ...config, software_stop_enabled: checked })} label={t('trading2.softwareStop')} />
      <Toggle checked={config.software_take_profit_enabled} onChange={(checked) => setConfig({ ...config, software_take_profit_enabled: checked })} label={t('trading2.softwareTakeProfit')} />
      <Toggle checked={config.tiered_take_profit_enabled} onChange={(checked) => setConfig({ ...config, tiered_take_profit_enabled: checked })} label={t('trading2.tieredTakeProfit')} />
      <Toggle checked={config.risk_require_protection_for_market_order} onChange={(checked) => setConfig({ ...config, risk_require_protection_for_market_order: checked })} label={t('trading2.marketNeedsProtection')} />
      <Toggle checked={config.low_gate_enabled} onChange={(checked) => setConfig({ ...config, low_gate_enabled: checked })} label={t('trading2.lowGate')} />
      <Toggle checked={config.strategy_auto_execute_enabled} onChange={(checked) => setConfig({ ...config, strategy_auto_execute_enabled: checked })} label={t('trading2.strategyAutoExecute')} />
      <Toggle checked={config.strategy_unwind_on_failure} onChange={(checked) => setConfig({ ...config, strategy_unwind_on_failure: checked })} label={t('trading2.unwindOnFailure')} />
    </div>
    <div className="three">
      <label>{t('trading2.totalCapitalUsd')}<input type="number" value={config.total_capital} onChange={(e) => setConfig({ ...config, total_capital: Number(e.target.value) })} /></label>
      <label>{t('trading2.topN')}<input type="number" value={config.top_n} onChange={(e) => setConfig({ ...config, top_n: Number(e.target.value) })} /></label>
      <label title={t('trading2.maxPerSymbolHint')}>
        {t('trading2.maxPerSymbol')}<input type="number" min="0" max="20" value={config.max_per_symbol ?? 1} onChange={(e) => setConfig({ ...config, max_per_symbol: Number(e.target.value) })} />
      </label>
      {config.single_instance_enabled !== false && (
        <label>{t('trading2.singleRunTimeEt')}<input value={config.run_time_et} onChange={(e) => setConfig({ ...config, run_time_et: e.target.value })} /></label>
      )}
      <label>{t('trading2.entryOrderType')}
        <select value={config.entry_order_type || 'market'} onChange={(e) => setConfig({ ...config, entry_order_type: e.target.value })}>
          {orderTypeItems.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      <label>{t('trading2.exitOrderType')}
        <select value={config.exit_order_type || 'market'} onChange={(e) => setConfig({ ...config, exit_order_type: e.target.value })}>
          {orderTypeItems.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
    </div>
    <div className="strategy-selector">
      <div className="strategy-selector-head">
        <strong>{t('trading2.instanceMode')}</strong>
        <div className="switches compact-switches">
          <Toggle checked={config.single_instance_enabled !== false} onChange={setSingleInstanceEnabled} label={t('trading2.singleInstance')} />
          <Toggle checked={config.multi_instance_enabled} onChange={setMultiInstanceEnabled} label={t('trading2.multiSlot')} />
        </div>
      </div>
    </div>
    {config.multi_instance_enabled && (
      <div className="schedule-slot-editor">
        <div className="strategy-selector-head">
          <strong>{t('trading2.slotProfile')}</strong>
          <div className="schedule-profile-actions">
            <select value={config.schedule_profile || 'balanced_multi_slot'} onChange={(e) => setConfig({ ...config, schedule_profile: e.target.value })}>
              <option value="balanced_multi_slot">{t('trading2.profileBalanced')}</option>
              <option value="aggressive_event_day">{t('trading2.profileAggressive')}</option>
              <option value="conservative_day_session">{t('trading2.profileConservative')}</option>
            </select>
            <button type="button" className="ghost compact" onClick={resetScheduleSlots}>{t('trading2.resetSlots')}</button>
          </div>
        </div>
        <div className="schedule-slot-list">
          {scheduleSlotsForConfig().map((slot) => (
            <div key={slot.slot_id} className="schedule-slot-row">
              <div className="schedule-slot-top">
                <Toggle checked={slot.enabled !== false} onChange={(checked) => updateScheduleSlot(slot.slot_id, { enabled: checked })} label={slot.label || slot.slot_id} />
                <span className="muted">{slot.slot_id}</span>
              </div>
              <div className="three">
                <label>{t('trading2.slotTriggerTime')}<input value={slot.time_et || ''} onChange={(e) => updateScheduleSlot(slot.slot_id, { time_et: e.target.value })} /></label>
                <label>{t('trading2.slotAction')}
                  <select value={slot.action || 'open_or_adjust'} onChange={(e) => updateScheduleSlot(slot.slot_id, { action: e.target.value })}>
                    <option value="scan_open">{t('trading2.actionScanOpen')}</option>
                    <option value="open_or_adjust">{t('trading2.actionOpenOrAdjust')}</option>
                    <option value="reduce_or_exit">{t('trading2.actionReduceOrExit')}</option>
                    <option value="risk_review">{t('trading2.actionRiskReview')}</option>
                  </select>
                </label>
                <label>{t('trading2.slotCapitalPct')}
                  <input type="number" min="0" max="1" step="0.05" value={slot.capital_pct ?? 0} onChange={(e) => updateScheduleSlot(slot.slot_id, { capital_pct: Number(e.target.value) })} />
                </label>
              </div>
              <div className="schedule-slot-modes">
                {strategyModeItems.map(([mode, label]) => (
                  <button
                    key={`${slot.slot_id}-${mode}`}
                    type="button"
                    className={`route-pill compact ${Array.isArray(slot.strategy_modes) && slot.strategy_modes.includes(mode) ? 'active' : ''}`}
                    onClick={() => {
                      const modes = Array.isArray(slot.strategy_modes) ? slot.strategy_modes : [];
                      const next = modes.includes(mode) ? modes.filter((item) => item !== mode) : [...modes, mode];
                      updateScheduleSlot(slot.slot_id, { strategy_modes: normalizeStrategyModes(next.length ? next : ['single_leg']) });
                    }}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    )}
    <div className="three">
      <label>{t('trading2.defaultStopLossPct')}<input type="number" value={config.default_stop_loss_pct} onChange={(e) => setConfig({ ...config, default_stop_loss_pct: Number(e.target.value) })} /></label>
      {!config.tiered_take_profit_enabled && (
        <label>{t('trading2.defaultTakeProfitPct')}<input type="number" value={config.default_take_profit_pct ?? 30} onChange={(e) => setConfig({ ...config, default_take_profit_pct: Number(e.target.value) })} /></label>
      )}
      {config.tiered_take_profit_enabled && (
        <>
          <label>{t('trading2.tp1Pct')}<input type="number" value={config.default_take_profit_1_pct ?? 20} onChange={(e) => setConfig({ ...config, default_take_profit_1_pct: Number(e.target.value) })} /></label>
          <label>{t('trading2.tp2Pct')}<input type="number" value={config.default_take_profit_2_pct ?? 35} onChange={(e) => setConfig({ ...config, default_take_profit_2_pct: Number(e.target.value) })} /></label>
        </>
      )}
      <label>{t('trading2.aiModel')}
        <select value={config.ai_provider} onChange={(e) => setConfig({ ...config, ai_provider: e.target.value })}>
          {providers.map((provider) => <option key={provider.name} value={provider.name}>{provider.name}</option>)}
        </select>
      </label>
      <label>{t('trading2.marketDataSource')}
        <select value={config.market_data_source || 'yfinance'} onChange={(e) => setConfig({ ...config, market_data_source: e.target.value })}>
          {marketDataSourceItems.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      <label>{t('trading2.optionDataSource')}
        <select value={config.option_data_source || 'thetadata'} onChange={(e) => setConfig({ ...config, option_data_source: e.target.value })}>
          {optionDataSourceItems.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      <label>{t('trading2.broker')}
        <select
          value={config.broker || 'longbridge'}
          onChange={(e) => {
            const broker = e.target.value;
            const pool = broker === 'usmart' ? usmartAccounts : alpacaAccounts;
            const defaultBrokerAccount = pool.find((account) => account.is_default) || pool[0];
            const usesAccount = broker === 'alpaca' || broker === 'usmart';
            setConfig({ ...config, broker, broker_account: usesAccount ? (config.broker_account || defaultBrokerAccount?.name || '') : config.broker_account });
          }}
        >
          <option value="longbridge">Longbridge</option>
          <option value="alpaca">Alpaca</option>
          <option value="usmart">uSMART</option>
        </select>
      </label>
      {!usesBrokerAccount && (
        <label>{t('trading2.longbridgeAccount')}
          <select value={config.longbridge_account || ''} onChange={(e) => setConfig({ ...config, longbridge_account: e.target.value })}>
            <option value="">{t('trading2.selectAccount')}</option>
            {accounts.map((account) => <option key={account.name} value={account.name}>{account.label || account.name}</option>)}
          </select>
        </label>
      )}
      {usesBrokerAccount && (
        <label>{config.broker === 'usmart' ? t('trading2.usmartAccount') : t('trading2.alpacaAccount')}
          <select value={config.broker_account || ''} onChange={(e) => setConfig({ ...config, broker_account: e.target.value })}>
            <option value="">{config.broker === 'usmart' ? t('trading2.selectUsmartAccount') : t('trading2.selectAlpacaAccount')}</option>
            {activeBrokerAccounts.map((account) => (
              <option key={account.ref || account.name} value={account.name}>
                {account.label || account.name}{account.paper ? ' · Paper' : ' · Live'}{account.is_default ? t('trading2.defaultSuffix') : ''}
              </option>
            ))}
          </select>
        </label>
      )}
    </div>
    <div className="status-box neutral-box">
      <strong>{t('trading2.tradeChannel')}{brokerDisplay} · {selectedBrokerLabel}</strong>
      <p>{t('trading2.tradeChannelNote')}</p>
      {usesBrokerAccount && !activeBrokerAccounts.length && (
        <small>{config.broker === 'usmart' ? t('trading2.noUsmartAccount') : t('trading2.noAlpacaAccount')}</small>
      )}
    </div>
    <div className="strategy-selector">
      <div className="strategy-selector-head">
        <strong>{t('trading2.strategyMode')}</strong>
        <div>
          <button type="button" className="ghost compact" onClick={() => setConfig({ ...config, strategy_modes: strategyModeItems.map(([value]) => value) })}>{t('trading2.selectAll')}</button>
          <button type="button" className="ghost compact" onClick={() => setConfig({ ...config, strategy_modes: ['single_leg'] })}>{t('trading2.singleLeg')}</button>
        </div>
      </div>
      <div className="strategy-pills">
        {strategyModeItems.map(([value, label]) => (
          <button
            key={value}
            type="button"
            className={`route-pill compact ${selectedStrategyModes.includes(value) ? 'active' : ''}`}
            onClick={() => toggleTradingStrategyMode(value)}
          >
            {label}
          </button>
        ))}
      </div>
      <p className="strategy-hint">
        {hasMultiLegStrategy
          ? (config.strategy_auto_execute_enabled
            ? t('trading2.hintMultiAuto')
            : t('trading2.hintMultiManual'))
          : t('trading2.hintSingleLeg')}
      </p>
    </div>
    <label>{t('trading2.waitForFillSeconds')}
      <input type="number" min="0" max="60" value={config.wait_for_fill_seconds} onChange={(e) => setConfig({ ...config, wait_for_fill_seconds: Number(e.target.value) })} />
    </label>
    <div className="subsection-title">{t('trading2.riskBreakers')}</div>
    <div className="three">
      <label>{t('trading2.maxDailyRuns')}<input type="number" min="1" max="20" value={config.risk_max_daily_runs} onChange={(e) => setConfig({ ...config, risk_max_daily_runs: Number(e.target.value) })} /></label>
      <label>{t('trading2.maxConsecutiveFailures')}<input type="number" min="1" max="10" value={config.risk_max_consecutive_failures} onChange={(e) => setConfig({ ...config, risk_max_consecutive_failures: Number(e.target.value) })} /></label>
      <label>{t('trading2.maxUnprotectedQty')}<input type="number" min="0" max="1000" value={config.risk_max_unprotected_quantity} onChange={(e) => setConfig({ ...config, risk_max_unprotected_quantity: Number(e.target.value) })} /></label>
    </div>
    <label>{t('trading2.maxSingleStopLossPct')}
      <input type="number" min="1" max="95" value={config.risk_max_single_stop_loss_pct} onChange={(e) => setConfig({ ...config, risk_max_single_stop_loss_pct: Number(e.target.value) })} />
    </label>
    <div className="form-field">
      <span className="field-label">{t('trading2.universe')}</span>
      <TradingUniverseEditor
        value={config.universe}
        onChange={(universe) => setConfig({ ...config, universe })}
      />
    </div>
    {visibleTradingPresets.length > 0 && (
      <div className="strategy-selector compact">
        <div className="strategy-selector-head">
          <strong>{t('trading2.rulePresets')}</strong>
          <div>
            <span className="muted">{t('trading2.rulePresetsNote')}</span>
            {analysisPresets.length > PROMPT_PREVIEW_LIMIT && onPresetGuide && (
              <button className="ghost compact" type="button" onClick={onPresetGuide}>{t('trading2.viewAll')}</button>
            )}
          </div>
        </div>
        <div className="strategy-pills">
          {visibleTradingPresets.map((preset) => (
            <button
              key={preset.key}
              type="button"
              className="route-pill compact"
              title={preset.description}
              onClick={() => applyTradingPreset(preset)}
            >
              {preset.label}
            </button>
          ))}
        </div>
        <p className="strategy-hint">{t('trading2.rulePresetsHint')}</p>
      </div>
    )}
    <label>{t('trading2.analysisPrompt')}
      <textarea rows={4} value={config.prompt_template} onChange={(e) => setConfig({ ...config, prompt_template: e.target.value })} />
    </label>
    <div className="module-grid">
      {analysisModuleItems.map(([key, label]) => (
        <Toggle
          key={key}
          checked={config.analysis_modules?.[key] ?? true}
          onChange={(checked) => setConfig({ ...config, analysis_modules: { ...config.analysis_modules, [key]: checked } })}
          label={label}
        />
      ))}
    </div>
    <button className="primary" disabled={saving}>{saving ? t('trading2.saving') : t('trading2.saveConfig')}</button>
    <button className="danger" type="button" disabled={!canRunNow} title={canRunNow ? t('trading2.createInstanceNow') : createBlockers.join('；')} onClick={runTradingNow}>
      {running ? t('trading2.creatingInstance') : t('trading2.createInstance')}
    </button>
    {!canRunNow && (
      <div className="run-blockers">
        <strong>{t('trading2.cannotCreateYet')}</strong>
        <ul>{createBlockers.slice(0, 4).map((item) => <li key={item}>{item}</li>)}</ul>
      </div>
    )}
  </form>
</aside>
  );
}
