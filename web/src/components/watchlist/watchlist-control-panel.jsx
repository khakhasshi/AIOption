import React from 'react';
import { Bell, ListChecks, Radar } from 'lucide-react';
import { t } from '../../i18n/index.js';
import { SectionTitle, Toggle } from '../common.jsx';
import { marketDataSourceItems, optionDataSourceItems } from '../../config.js';
import { aiScanPolicyLabels, scanLoopRuleTemplates } from './use-watchlist-radar.js';

const technicalFields = [
  ['underlying_vs_vwap_pct', t('watchlist2.field.vwapDistance')],
  ['vwap', 'VWAP'],
  ['orb_high', 'ORB High'],
  ['orb_low', 'ORB Low'],
  ['ema_20', 'EMA20'],
  ['ema_50', 'EMA50'],
  ['ema_200', 'EMA200'],
  ['rsi', 'RSI'],
  ['atr', 'ATR'],
];

const optionFields = [
  ['ask', 'Ask'],
  ['bid', 'Bid'],
  ['mid', 'Mid'],
  ['bid_ask_spread_pct', 'Bid/Ask Spread %'],
  ['volume', 'Volume'],
  ['open_interest', 'OI'],
];

const operators = ['>=', '<=', '>', '<', '==', '!='];

function channelProvider(channel) {
  return channel?.type === 'email' ? 'email' : channel?.config?.provider || 'generic';
}

function recommendedChannelIds(channels, mode) {
  const enabled = channels.filter((channel) => channel.enabled);
  const byProvider = (providers) => enabled.filter((channel) => providers.includes(channelProvider(channel))).map((channel) => channel.id);
  const email = byProvider(['email']);
  const instant = byProvider(['telegram', 'whatsapp', 'feishu', 'slack', 'discord']);
  if (mode === 'quiet') return email.length ? email.slice(0, 1) : enabled.slice(0, 1).map((channel) => channel.id);
  if (mode === 'digest') return [...new Set([...email.slice(0, 1), ...byProvider(['feishu', 'slack']).slice(0, 1)])];
  return [...new Set([...instant.slice(0, 2), ...email.slice(0, 1)])];
}

export function WatchlistControlPanel({
  busy = false,
  watchlistForm,
  setWatchlistForm,
  instanceForm,
  setInstanceForm,
  providers = [],
  channels = [],
  selectedWatchlist = null,
  editingWatchlistId = '',
  editingInstanceId = '',
  onCreateWatchlist,
  onCancelWatchlistEdit,
  onCreateInstance,
  onCancelInstanceEdit,
  onApplyInstanceTemplate,
  onOpenNotifications,
}) {
  const selectedChannels = instanceForm.notification_channel_ids || [];
  const enabledChannels = channels.filter((channel) => channel.enabled);

  function toggleChannel(channelId) {
    setInstanceForm((current) => {
      const selected = Array.isArray(current.notification_channel_ids) ? current.notification_channel_ids : [];
      const next = selected.includes(channelId) ? selected.filter((id) => id !== channelId) : [...selected, channelId];
      return { ...current, notification_channel_ids: next };
    });
  }

  function applyChannelRecommendation(mode) {
    setInstanceForm((current) => ({ ...current, notification_channel_ids: recommendedChannelIds(channels, mode) }));
  }

  return (
    <aside className="panel control-panel">
      <SectionTitle title={t('watchlist2.startLoop')} />
      <div className="starter-guide">
        <strong>{selectedWatchlist ? t('watchlist2.configuringInstance') : t('watchlist2.createPoolFirst')}</strong>
        <small>
          {selectedWatchlist
            ? t('watchlist2.configHint').replace('{name}', selectedWatchlist.name)
            : t('watchlist2.createPoolHint')}
        </small>
      </div>
      <form className="form" onSubmit={onCreateWatchlist}>
        <label>
          {t('watchlist2.poolName')}
          <input value={watchlistForm.name} onChange={(event) => setWatchlistForm((current) => ({ ...current, name: event.target.value }))} />
        </label>
        <label>
          Symbols
          <textarea rows={3} value={watchlistForm.symbols} onChange={(event) => setWatchlistForm((current) => ({ ...current, symbols: event.target.value }))} />
        </label>
        <button className="primary" type="submit" disabled={busy}>
          <ListChecks size={16} /> {editingWatchlistId ? t('watchlist2.savePool') : t('watchlist2.createPool')}
        </button>
        {editingWatchlistId && (
          <button className="ghost" type="button" disabled={busy} onClick={onCancelWatchlistEdit}>
            {t('watchlist2.cancelEdit')}
          </button>
        )}
      </form>

      <form className="form compact-form" onSubmit={onCreateInstance}>
        {selectedWatchlist && (
          <div className="permission-note compact-note">
            <strong>{t('watchlist2.currentPool')}{selectedWatchlist.name}</strong>
            <small>{selectedWatchlist.symbols.join(', ')}</small>
          </div>
        )}
        {!selectedWatchlist && (
          <div className="warning-banner">{t('watchlist2.needPoolBeforeInstance')}</div>
        )}
        <label>
          {t('watchlist2.instanceName')}
          <input value={instanceForm.name} onChange={(event) => setInstanceForm((current) => ({ ...current, name: event.target.value }))} />
        </label>
        <label>
          {t('watchlist2.instanceSymbols')}
          <textarea rows={2} value={instanceForm.symbols || ''} onChange={(event) => setInstanceForm((current) => ({ ...current, symbols: event.target.value }))} placeholder={t('watchlist2.symbolsPlaceholder')} />
        </label>
        <div className="rule-template-grid">
          {scanLoopRuleTemplates.map((template) => (
            <button
              key={template.id}
              className={`rule-template ${template.id.startsWith('ai_') ? 'ai-designed' : ''} ${template.id.startsWith('credit_') ? 'credit-designed' : ''} ${instanceForm.rule_template === template.id ? 'active' : ''}`}
              type="button"
              onClick={() => onApplyInstanceTemplate?.(template.id)}
            >
              <strong>
                {template.title}
                {template.id.startsWith('ai_') && <em>AI</em>}
                {template.id.startsWith('credit_') && <em className="credit">CR</em>}
              </strong>
              <span>{template.desc}</span>
              {template.params && <small>{template.params}</small>}
            </button>
          ))}
        </div>
        <div className="two">
          <label>
            {t('watchlist2.intervalMinutes')}
            <input type="number" min="5" value={instanceForm.interval_minutes} onChange={(event) => setInstanceForm((current) => ({ ...current, interval_minutes: event.target.value }))} />
          </label>
          <label>
            {t('watchlist2.prefilterRvol')}
            <input type="number" step="0.1" value={instanceForm.min_rvol} onChange={(event) => setInstanceForm((current) => ({ ...current, min_rvol: event.target.value }))} />
          </label>
        </div>
        <label>
          {t('watchlist2.alertRvol')}
          <input type="number" step="0.1" value={instanceForm.alert_min_rvol} onChange={(event) => setInstanceForm((current) => ({ ...current, alert_min_rvol: event.target.value }))} />
        </label>
        <div className="rule-builder">
          <div className="rule-builder-head">
            <strong>{t('watchlist2.ruleBuilder')}</strong>
            <div>
              <label>
                {t('watchlist2.prefilter')}
                <select value={instanceForm.prefilter_logic} onChange={(event) => setInstanceForm((current) => ({ ...current, prefilter_logic: event.target.value }))}>
                  <option value="and">AND</option>
                  <option value="or">OR</option>
                </select>
              </label>
              <label>
                {t('watchlist2.alert')}
                <select value={instanceForm.alert_logic} onChange={(event) => setInstanceForm((current) => ({ ...current, alert_logic: event.target.value }))}>
                  <option value="and">AND</option>
                  <option value="or">OR</option>
                </select>
              </label>
            </div>
          </div>
          <div className="toggle-row">
            <Toggle checked={instanceForm.price_rule_enabled} onChange={(value) => setInstanceForm((current) => ({ ...current, price_rule_enabled: value }))} label={t('watchlist2.priceCondition')} />
            <Toggle checked={instanceForm.technical_rule_enabled} onChange={(value) => setInstanceForm((current) => ({ ...current, technical_rule_enabled: value }))} label={t('watchlist2.technicalIndicator')} />
          </div>
          {instanceForm.price_rule_enabled && (
            <div className="rule-row">
              <select value={instanceForm.price_rule_field} onChange={(event) => setInstanceForm((current) => ({ ...current, price_rule_field: event.target.value }))}>
                <option value="last">Last</option>
                <option value="price">Price</option>
              </select>
              <select value={instanceForm.price_rule_operator} onChange={(event) => setInstanceForm((current) => ({ ...current, price_rule_operator: event.target.value }))}>
                {operators.map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
              <input type="number" step="0.01" value={instanceForm.price_rule_value} onChange={(event) => setInstanceForm((current) => ({ ...current, price_rule_value: event.target.value }))} placeholder={t('watchlist2.pricePlaceholder')} />
            </div>
          )}
          {instanceForm.technical_rule_enabled && (
            <div className="rule-row">
              <select value={instanceForm.technical_rule_field} onChange={(event) => setInstanceForm((current) => ({ ...current, technical_rule_field: event.target.value }))}>
                {technicalFields.map(([field, label]) => <option key={field} value={field}>{label}</option>)}
              </select>
              <select value={instanceForm.technical_rule_operator} onChange={(event) => setInstanceForm((current) => ({ ...current, technical_rule_operator: event.target.value }))}>
                {operators.map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
              <input type="number" step="0.01" value={instanceForm.technical_rule_value} onChange={(event) => setInstanceForm((current) => ({ ...current, technical_rule_value: event.target.value }))} placeholder={t('watchlist2.indicatorPlaceholder')} />
            </div>
          )}
          <div className="toggle-row">
            <Toggle checked={instanceForm.option_rule_enabled} onChange={(value) => setInstanceForm((current) => ({ ...current, option_rule_enabled: value }))} label={t('watchlist2.optionQuote')} />
          </div>
          {instanceForm.option_rule_enabled && (
            <div className="rule-row">
              <select value={instanceForm.option_rule_field} onChange={(event) => setInstanceForm((current) => ({ ...current, option_rule_field: event.target.value }))}>
                {optionFields.map(([field, label]) => <option key={field} value={field}>{label}</option>)}
              </select>
              <select value={instanceForm.option_rule_operator} onChange={(event) => setInstanceForm((current) => ({ ...current, option_rule_operator: event.target.value }))}>
                {operators.map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
              <input type="number" step="0.01" value={instanceForm.option_rule_value} onChange={(event) => setInstanceForm((current) => ({ ...current, option_rule_value: event.target.value }))} placeholder={t('watchlist2.quotePlaceholder')} />
            </div>
          )}
        </div>
        <label>
          {t('watchlist2.strategyStructure')}
          <select value={instanceForm.strategy_family} onChange={(event) => setInstanceForm((current) => ({ ...current, strategy_family: event.target.value }))}>
            <option value="spread">Debit Spread</option>
            <option value="single_leg">{t('watchlist2.singleLeg')}</option>
            <option value="credit_spread">Credit Spread</option>
            <option value="income">Income / Iron Condor</option>
          </select>
        </label>
        <div className="toggle-row">
          <Toggle checked={instanceForm.gex_filter_enabled} onChange={(value) => setInstanceForm((current) => ({ ...current, gex_filter_enabled: value }))} label={t('watchlist2.gexPrefilter')} />
        </div>
        {instanceForm.gex_filter_enabled && (
          <>
            <div className="two">
              <label>
                {t('watchlist2.gexRegimeLabel')}
                <select value={instanceForm.gex_regime} onChange={(event) => setInstanceForm((current) => ({ ...current, gex_regime: event.target.value }))}>
                  <option value="any">{t('watchlist2.anyAvailableGex')}</option>
                  <option value="negative_gamma">{t('watchlist2.negativeGamma')}</option>
                  <option value="positive_gamma">{t('watchlist2.positiveGamma')}</option>
                  <option value="neutral">{t('watchlist2.gexNeutral')}</option>
                  <option value="mixed">{t('watchlist2.gexMixed')}</option>
                </select>
              </label>
              <label>
                {t('watchlist2.wallType')}
                <select value={instanceForm.gex_wall_type} onChange={(event) => setInstanceForm((current) => ({ ...current, gex_wall_type: event.target.value }))}>
                  <option value="any">{t('watchlist2.anyWall')}</option>
                  <option value="call_wall">Call Wall</option>
                  <option value="put_wall">Put Wall</option>
                </select>
              </label>
            </div>
            <div className="two">
              <label>
                {t('watchlist2.wallDistancePct')}
                <input type="number" min="0" step="0.1" value={instanceForm.gex_max_wall_distance_pct} onChange={(event) => setInstanceForm((current) => ({ ...current, gex_max_wall_distance_pct: event.target.value }))} placeholder={t('watchlist2.optional')} />
              </label>
              <label>
                {t('watchlist2.structuralRisk')}
                <select value={instanceForm.gex_structural_risk} onChange={(event) => setInstanceForm((current) => ({ ...current, gex_structural_risk: event.target.value }))}>
                  <option value="any">{t('watchlist2.noLimit')}</option>
                  <option value="trend_acceleration">{t('watchlist2.trendAccelMedium')}</option>
                  <option value="pinning_high">Pinning medium+</option>
                </select>
              </label>
            </div>
            <div className="toggle-row">
              <Toggle checked={instanceForm.gex_alert_rule_enabled} onChange={(value) => setInstanceForm((current) => ({ ...current, gex_alert_rule_enabled: value }))} label={t('watchlist2.gexAlsoInAlert')} />
            </div>
          </>
        )}
        <label>
          {t('watchlist2.alertMode')}
          <select value={instanceForm.alert_mode} onChange={(event) => setInstanceForm((current) => ({ ...current, alert_mode: event.target.value }))}>
            <option value="best_per_run">{t('watchlist2.bestPerRun')}</option>
            <option value="all_matches">{t('watchlist2.allMatches')}</option>
            <option value="silent_log">{t('watchlist2.silentLog')}</option>
            <option value="daily_digest">{t('watchlist2.dailyDigestOpt')}</option>
          </select>
        </label>
        <div className="notification-bind-box">
          <div className="notification-bind-head">
            <strong>{t('watchlist2.notificationChannels')}</strong>
            <small>{selectedChannels.length ? t('watchlist2.selectedN').replace('{n}', selectedChannels.length) : enabledChannels.length ? t('watchlist2.bindAtLeastOne') : t('watchlist2.noChannelsConfigured')}</small>
            <button className="ghost compact" type="button" onClick={onOpenNotifications}>
              <Bell size={14} /> {t('watchlist2.manage')}
            </button>
          </div>
          <div className="notification-recommend-row">
            <button className="ghost compact" type="button" disabled={!channels.some((channel) => channel.enabled)} onClick={() => applyChannelRecommendation('quiet')}>{t('watchlist2.lowNoise')}</button>
            <button className="ghost compact" type="button" disabled={!channels.some((channel) => channel.enabled)} onClick={() => applyChannelRecommendation('instant')}>{t('watchlist2.strongAlert')}</button>
            <button className="ghost compact" type="button" disabled={!channels.some((channel) => channel.enabled)} onClick={() => applyChannelRecommendation('digest')}>{t('watchlist2.digest')}</button>
          </div>
          <div className="channel-picker">
            {channels.map((channel) => {
              const selected = (instanceForm.notification_channel_ids || []).includes(channel.id);
              const provider = channelProvider(channel);
              return (
                <button
                  key={channel.id}
                  type="button"
                  className={`channel-pick ${selected ? 'active' : ''} ${channel.enabled ? '' : 'disabled'}`}
                  onClick={() => toggleChannel(channel.id)}
                >
                  <span>{selected ? t('watchlist2.selectedState') : t('watchlist2.selectable')}</span>
                  <strong>{channel.label}</strong>
                  <small>{provider} · {channel.enabled ? t('watchlist2.enabled') : t('watchlist2.disabled')}{channel.last_error ? ` · ${channel.last_error}` : ''}</small>
                </button>
              );
            })}
            {!channels.length && (
              <button className="channel-pick empty" type="button" onClick={onOpenNotifications}>
                <span>{t('watchlist2.notConfigured')}</span>
                <strong>{t('watchlist2.addChannelInCenter')}</strong>
                <small>{t('watchlist2.channelProviders')}</small>
              </button>
            )}
          </div>
        </div>
        <div className="two">
          <label>
            {t('watchlist2.eodTime')}
            <input value={instanceForm.eod_run_time_et} onChange={(event) => setInstanceForm((current) => ({ ...current, eod_run_time_et: event.target.value }))} />
          </label>
          <label>
            {t('watchlist2.reviewMode')}
            <select value={instanceForm.weekend_review_enabled ? 'weekend' : instanceForm.eod_review_enabled ? 'eod' : 'regular'} onChange={(event) => setInstanceForm((current) => ({
              ...current,
              eod_review_enabled: event.target.value === 'eod' || event.target.value === 'weekend',
              weekend_review_enabled: event.target.value === 'weekend',
            }))}>
              <option value="regular">{t('watchlist2.intradayOnly')}</option>
              <option value="eod">{t('watchlist2.intradayPlusEod')}</option>
              <option value="weekend">{t('watchlist2.intradayPlusEodWeekend')}</option>
            </select>
          </label>
        </div>
        <div className="two">
          <label>
            {t('watchlist2.dailyAlerts')}
            <input type="number" min="0" value={instanceForm.max_alerts_per_day} onChange={(event) => setInstanceForm((current) => ({ ...current, max_alerts_per_day: event.target.value }))} />
          </label>
          <label>
            {t('watchlist2.dailyAiScans')}
            <input type="number" min="0" value={instanceForm.max_ai_scans_per_day} onChange={(event) => setInstanceForm((current) => ({ ...current, max_ai_scans_per_day: event.target.value }))} />
          </label>
        </div>
        <div className="two">
          <label>
            {t('watchlist2.aiScanPolicy')}
            <select value={instanceForm.ai_scan_policy} onChange={(event) => setInstanceForm((current) => ({ ...current, ai_scan_policy: event.target.value }))}>
              {Object.entries(aiScanPolicyLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <label>
            {t('watchlist2.topNPerRun')}
            <input type="number" min="1" max="50" value={instanceForm.ai_scan_top_n} onChange={(event) => setInstanceForm((current) => ({ ...current, ai_scan_top_n: event.target.value }))} />
          </label>
        </div>
        <small className="muted">{t('watchlist2.aiPolicyHint')}</small>
        <label>
          AI Provider
          <select value={instanceForm.ai_provider} onChange={(event) => setInstanceForm((current) => ({ ...current, ai_provider: event.target.value }))}>
            {providers.map((provider) => <option key={provider.name} value={provider.name}>{provider.label || provider.name}</option>)}
          </select>
        </label>
        <div className="two">
          <label>
            {t('watchlist2.marketDataSource')}
            <select value={instanceForm.market_data_source || 'yfinance'} onChange={(event) => setInstanceForm((current) => ({ ...current, market_data_source: event.target.value }))}>
              {marketDataSourceItems.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <label>
            {t('watchlist2.optionDataSource')}
            <select value={instanceForm.option_data_source || 'thetadata'} onChange={(event) => setInstanceForm((current) => ({ ...current, option_data_source: event.target.value }))}>
              {optionDataSourceItems.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
        </div>
        <div className="toggle-row">
          <Toggle checked={instanceForm.use_ai} onChange={(value) => setInstanceForm((current) => ({ ...current, use_ai: value }))} label={t('watchlist2.aiScan')} />
          <Toggle checked={instanceForm.council} onChange={(value) => setInstanceForm((current) => ({ ...current, council: value }))} label={t('watchlist2.council')} />
        </div>
        <button className="primary" type="submit" disabled={busy || !selectedWatchlist}>
          <Radar size={16} /> {editingInstanceId ? t('watchlist2.saveInstance') : t('watchlist2.createInstance')}
        </button>
        {editingInstanceId && (
          <button className="ghost" type="button" disabled={busy} onClick={onCancelInstanceEdit}>
            {t('watchlist2.cancelInstanceEdit')}
          </button>
        )}
      </form>
    </aside>
  );
}
