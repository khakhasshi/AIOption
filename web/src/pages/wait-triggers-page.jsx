import React, { useEffect, useMemo, useState } from 'react';
import { Bell, FlaskConical, RefreshCw, Sparkles, Trash2 } from 'lucide-react';
import { PageHeader } from '../components/app-shell/page-header.jsx';
import { useAppShell } from '../components/app-shell/app-shell-context.js';
import { Toggle } from '../components/common.jsx';
import { t, getLocale } from '../i18n/index.js';

const TRIGGER_TYPES = [
  { value: 'underlying_price' },
  { value: 'technical_indicator' },
  { value: 'option_quote' },
  { value: 'rescan_score' },
];

function triggerTypeLabel(value) {
  return TRIGGER_TYPES.some((item) => item.value === value) ? t(`notify.waitTrigger.type.${value}`) : (value || '--');
}

function triggerTypeDesc(value) {
  return TRIGGER_TYPES.some((item) => item.value === value) ? t(`notify.waitTrigger.typeDesc.${value}`) : '';
}

const TECH_FIELDS = [
  ['last', 'Last'],
  ['underlying_vs_vwap_pct', null],
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

const OPTION_FIELDS = [
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

const MARKET_POLICIES = [
  'regular_only',
  'include_extended',
  'next_open',
  'eod_review',
  'always_calendar',
];

function marketPolicyLabel(value) {
  return MARKET_POLICIES.includes(value) ? t(`notify.waitTrigger.marketPolicy.${value}`) : (value || t('notify.waitTrigger.marketPolicy.regular_only'));
}

const OPERATORS = ['>=', '<=', '>', '<'];

function operatorLabel(op) {
  return t(`notify.waitTrigger.operator.${{ '>=': 'gte', '<=': 'lte', '>': 'gt', '<': 'lt' }[op] || op}`);
}

const EMPTY_DRAFT = {
  symbolsRaw: '',
  type: 'underlying_price',
  field: 'last',
  contractSymbol: '',
  operator: '>=',
  value: '',
  marketPolicy: 'regular_only',
  intervalSeconds: '300',
  cooldownSeconds: '1800',
  maxTriggerCount: '3',
  expiresAt: '',
  namePrefix: '',
};

// Six curated presets. Each fills the draft form (user adjusts symbol/value
// before submit). `value` may be left blank if the user must enter their own
// price level.
const PRESET_TEMPLATES = [
  {
    id: 'orb-break',
    labelKey: 'orbBreak',
    patch: {
      type: 'underlying_price',
      operator: '>=',
      value: '',
      marketPolicy: 'regular_only',
      intervalSeconds: '60',
      cooldownSeconds: '1800',
      maxTriggerCount: '2',
    },
  },
  {
    id: 'support-break',
    labelKey: 'supportBreak',
    patch: {
      type: 'underlying_price',
      operator: '<=',
      value: '',
      marketPolicy: 'regular_only',
      intervalSeconds: '60',
      cooldownSeconds: '1800',
      maxTriggerCount: '3',
    },
  },
  {
    id: 'vwap-weak',
    labelKey: 'vwapWeak',
    patch: {
      type: 'technical_indicator',
      field: 'underlying_vs_vwap_pct',
      operator: '<=',
      value: '-0.8',
      marketPolicy: 'regular_only',
      intervalSeconds: '120',
      cooldownSeconds: '1800',
      maxTriggerCount: '3',
    },
  },
  {
    id: 'vwap-strong',
    labelKey: 'vwapStrong',
    patch: {
      type: 'technical_indicator',
      field: 'underlying_vs_vwap_pct',
      operator: '>=',
      value: '0.8',
      marketPolicy: 'regular_only',
      intervalSeconds: '120',
      cooldownSeconds: '1800',
      maxTriggerCount: '3',
    },
  },
  {
    id: 'volume-burst',
    labelKey: 'volumeBurst',
    patch: {
      type: 'technical_indicator',
      field: 'volume',
      operator: '>=',
      value: '2000000',
      marketPolicy: 'regular_only',
      intervalSeconds: '60',
      cooldownSeconds: '900',
      maxTriggerCount: '3',
    },
  },
  {
    id: 'rescan-score',
    labelKey: 'rescanScore',
    patch: {
      type: 'rescan_score',
      operator: '>=',
      value: '80',
      marketPolicy: 'regular_only',
      intervalSeconds: '300',
      cooldownSeconds: '3600',
      maxTriggerCount: '3',
    },
  },
];

function parseSymbols(raw) {
  return String(raw || '')
    .split(/[\s,，;；]+/)
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);
}

function typeLabel(value) {
  return triggerTypeLabel(value);
}

function fieldLabel(type, field) {
  const source = type === 'option_quote' ? OPTION_FIELDS : TECH_FIELDS;
  const entry = source.find(([k]) => k === field);
  if (!entry) return field || '';
  return entry[1] == null ? t('notify.waitTrigger.field.vwapDistance') : entry[1];
}

function policyLabel(value) {
  return marketPolicyLabel(value);
}

function formatTimestamp(value) {
  if (!value) return '--';
  try {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString(getLocale() === 'en' ? 'en-US' : 'zh-CN', { hour12: false });
  } catch { return value; }
}

function channelTypeLabel(type) {
  const known = ['email', 'webhook', 'telegram', 'whatsapp', 'slack', 'feishu'];
  if (type === 'email') return t('notify.waitTrigger.channelType.email');
  if (type === 'feishu') return '飞书';
  if (known.includes(type)) {
    return { webhook: 'Webhook', telegram: 'Telegram', whatsapp: 'WhatsApp', slack: 'Slack' }[type];
  }
  return type || t('notify.waitTrigger.channelType.fallback');
}

export function WaitTriggersPage({ api, session, onOpenNotifications }) {
  const { embedded } = useAppShell();
  const canTrade = Boolean(session?.can_analyze ?? true);

  const [triggers, setTriggers] = useState([]);
  const [channels, setChannels] = useState([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [info, setInfo] = useState('');
  const [draft, setDraft] = useState(EMPTY_DRAFT);
  const [selectedChannelIds, setSelectedChannelIds] = useState([]);
  const [testingId, setTestingId] = useState('');
  const [testResults, setTestResults] = useState({});
  const [filterSymbol, setFilterSymbol] = useState('');

  useEffect(() => {
    refreshAll();
  }, []);

  async function refreshAll() {
    setLoading(true);
    setError('');
    try {
      const [tRows, cRows] = await Promise.all([
        api('/api/scan-triggers'),
        api('/api/notification-channels'),
      ]);
      setTriggers(Array.isArray(tRows) ? tRows : []);
      setChannels(Array.isArray(cRows) ? cRows : []);
    } catch (e) {
      setError(e.message || t('notify.waitTrigger.loadFailed'));
    } finally {
      setLoading(false);
    }
  }

  const symbols = useMemo(() => parseSymbols(draft.symbolsRaw), [draft.symbolsRaw]);
  const enabledChannels = useMemo(() => channels.filter((c) => c.enabled !== false), [channels]);

  function toggleChannel(channelId) {
    setSelectedChannelIds((current) =>
      current.includes(channelId)
        ? current.filter((id) => id !== channelId)
        : [...current, channelId],
    );
  }

  function applyTemplate(template) {
    setDraft((d) => ({
      ...EMPTY_DRAFT,
      symbolsRaw: d.symbolsRaw,
      ...template.patch,
      field: template.patch.field ?? (template.patch.type === 'technical_indicator' ? 'underlying_vs_vwap_pct' : 'last'),
      namePrefix: t(`notify.waitTrigger.preset.${template.labelKey}.name`),
    }));
    setInfo(t('notify.waitTrigger.templateApplied').replace('{label}', t(`notify.waitTrigger.preset.${template.labelKey}.label`)));
    setError('');
  }

  function buildCondition(symbol) {
    const numericValue = Number(draft.value);
    const condition = {
      type: draft.type,
      symbol,
      operator: draft.operator,
      value: Number.isFinite(numericValue) ? numericValue : draft.value,
    };
    if (draft.type === 'technical_indicator' || draft.type === 'option_quote') {
      condition.field = draft.field;
    }
    if (draft.type === 'option_quote' && draft.contractSymbol.trim()) {
      condition.contract_symbol = draft.contractSymbol.trim();
    }
    return condition;
  }

  function buildName(symbol) {
    const prefix = draft.namePrefix.trim();
    const conditionLabel = `${typeLabel(draft.type)} ${draft.operator} ${draft.value}`;
    if (prefix) return `${prefix} · ${symbol}`;
    return `${symbol} · ${conditionLabel}`;
  }

  async function submitCreate(event) {
    event.preventDefault();
    setError('');
    setInfo('');
    if (!symbols.length) {
      setError(t('notify.waitTrigger.symbolRequired'));
      return;
    }
    if (!draft.value && draft.type !== 'rescan_score') {
      setError(t('notify.waitTrigger.valueRequired'));
      return;
    }
    setBusy(true);
    try {
      const results = await Promise.allSettled(
        symbols.map((symbol) => api('/api/scan-triggers', {
          method: 'POST',
          body: JSON.stringify({
            name: buildName(symbol),
            symbol,
            condition: buildCondition(symbol),
            notification_channel_ids: selectedChannelIds,
            enabled: true,
            check_interval_seconds: Number(draft.intervalSeconds) || 300,
            cooldown_seconds: Number(draft.cooldownSeconds) || 1800,
            max_trigger_count: Number(draft.maxTriggerCount) || 3,
            market_policy: draft.marketPolicy,
            expires_at: draft.expiresAt || null,
          }),
        })),
      );
      const ok = results.filter((r) => r.status === 'fulfilled').length;
      const fail = results.length - ok;
      if (ok) {
        setInfo(t('notify.waitTrigger.created').replace('{ok}', ok) + (fail ? t('notify.waitTrigger.createdFailSuffix').replace('{fail}', fail) : ''));
        setDraft({ ...EMPTY_DRAFT, type: draft.type, field: draft.field, marketPolicy: draft.marketPolicy });
        await refreshAll();
      }
      if (fail) {
        const firstError = results.find((r) => r.status === 'rejected');
        setError(firstError?.reason?.message || t('notify.waitTrigger.createFailedN').replace('{fail}', fail));
      }
    } catch (e) {
      setError(e.message || t('notify.waitTrigger.createFailed'));
    } finally {
      setBusy(false);
    }
  }

  async function toggleEnabled(trigger) {
    try {
      await api(`/api/scan-triggers/${encodeURIComponent(trigger.id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled: !trigger.enabled }),
      });
      await refreshAll();
    } catch (e) { setError(e.message); }
  }

  async function deleteTrigger(trigger) {
    if (!confirm(t('notify.waitTrigger.confirmDelete').replace('{name}', trigger.name))) return;
    try {
      await api(`/api/scan-triggers/${encodeURIComponent(trigger.id)}`, { method: 'DELETE' });
      await refreshAll();
    } catch (e) { setError(e.message); }
  }

  async function runTest(trigger) {
    setTestingId(trigger.id);
    try {
      const result = await api(`/api/scan-triggers/${encodeURIComponent(trigger.id)}/test`, { method: 'POST' });
      setTestResults((cur) => ({ ...cur, [trigger.id]: result }));
    } catch (e) {
      setError(e.message || t('notify.waitTrigger.testFailed'));
    } finally {
      setTestingId('');
    }
  }

  const filteredTriggers = useMemo(() => {
    const symbolFilter = filterSymbol.trim().toUpperCase();
    if (!symbolFilter) return triggers;
    return triggers.filter((t) => String(t.symbol || '').toUpperCase().includes(symbolFilter));
  }, [triggers, filterSymbol]);

  const channelsById = useMemo(() => {
    const map = new Map();
    channels.forEach((c) => map.set(c.id, c));
    return map;
  }, [channels]);

  return (
    <main className="shell wait-triggers-shell">
      {embedded && (
        <PageHeader
          eyebrow="Alerts"
          title="Wait Trigger"
          subtitle={t('notify.waitTrigger.subtitle')}
          meta={
            <span className="page-header-chip">{t('notify.waitTrigger.rulesChip').replace('{rules}', triggers.length).replace('{channels}', enabledChannels.length)}</span>
          }
          actions={
            <>
              <button className="ghost" type="button" onClick={refreshAll} disabled={loading}>
                <RefreshCw size={14} /> {loading ? t('notify.waitTrigger.refreshing') : t('notify.refresh')}
              </button>
              {onOpenNotifications && (
                <button className="ghost" type="button" onClick={onOpenNotifications}>{t('notify.title')}</button>
              )}
            </>
          }
        />
      )}

      {error && <div className="error-banner">{error}</div>}
      {info && <div className="info-banner">{info}</div>}

      <div className="wait-triggers-grid">
        <section className="panel wait-triggers-form-panel">
          <header className="panel-head">
            <strong>{t('notify.waitTrigger.createNew')}</strong>
            <small>{t('notify.waitTrigger.createNewHint')}</small>
          </header>
          <div className="preset-templates">
            <div className="preset-templates-head">
              <Sparkles size={14} /> <strong>{t('notify.waitTrigger.presetTemplates')}</strong>
              <small>{t('notify.waitTrigger.presetTemplatesHint')}</small>
            </div>
            <div className="preset-templates-grid">
              {PRESET_TEMPLATES.map((tpl) => (
                <button
                  key={tpl.id}
                  type="button"
                  className="preset-card"
                  onClick={() => applyTemplate(tpl)}
                  title={t(`notify.waitTrigger.preset.${tpl.labelKey}.hint`)}
                >
                  <strong>{t(`notify.waitTrigger.preset.${tpl.labelKey}.label`)}</strong>
                  <small>{t(`notify.waitTrigger.preset.${tpl.labelKey}.hint`)}</small>
                </button>
              ))}
            </div>
          </div>
          <form className="wait-trigger-form" onSubmit={submitCreate}>
            <label className="span-2">
              {t('notify.waitTrigger.symbolsLabel')}
              <input
                value={draft.symbolsRaw}
                placeholder={t('notify.waitTrigger.symbolsPlaceholder')}
                onChange={(e) => setDraft({ ...draft, symbolsRaw: e.target.value })}
                autoFocus
              />
              {symbols.length > 0 && (
                <div className="symbol-chips">
                  {symbols.map((s) => (
                    <span className="symbol-chip" key={s}>{s}</span>
                  ))}
                </div>
              )}
            </label>

            <label>
              {t('notify.waitTrigger.alertType')}
              <select
                value={draft.type}
                onChange={(e) => {
                  const type = e.target.value;
                  setDraft((d) => ({
                    ...d,
                    type,
                    field: type === 'option_quote' ? 'bid_ask_spread_pct' : type === 'technical_indicator' ? 'underlying_vs_vwap_pct' : '',
                  }));
                }}
              >
                {TRIGGER_TYPES.map((item) => <option key={item.value} value={item.value}>{triggerTypeLabel(item.value)}</option>)}
              </select>
              <small>{triggerTypeDesc(draft.type)}</small>
            </label>

            {(draft.type === 'technical_indicator' || draft.type === 'option_quote') && (
              <label>
                {t('notify.waitTrigger.field')}
                <select value={draft.field} onChange={(e) => setDraft({ ...draft, field: e.target.value })}>
                  {(draft.type === 'option_quote' ? OPTION_FIELDS : TECH_FIELDS).map(([k, l]) => (
                    <option key={k} value={k}>{l == null ? t('notify.waitTrigger.field.vwapDistance') : l}</option>
                  ))}
                </select>
              </label>
            )}

            {draft.type === 'option_quote' && (
              <label className="span-2">
                {t('notify.waitTrigger.contractLabel')}
                <input
                  value={draft.contractSymbol}
                  placeholder={t('notify.waitTrigger.contractPlaceholder')}
                  onChange={(e) => setDraft({ ...draft, contractSymbol: e.target.value })}
                />
              </label>
            )}

            <label>
              {t('notify.waitTrigger.operatorLabel')}
              <select value={draft.operator} onChange={(e) => setDraft({ ...draft, operator: e.target.value })}>
                {OPERATORS.map((op) => <option key={op} value={op}>{operatorLabel(op)}</option>)}
              </select>
            </label>

            <label>
              {draft.type === 'rescan_score' ? t('notify.waitTrigger.scoreThreshold') : t('notify.waitTrigger.triggerValue')}
              <input
                value={draft.value}
                inputMode="decimal"
                placeholder={draft.type === 'rescan_score' ? t('notify.waitTrigger.valuePlaceholderScore') : draft.type === 'technical_indicator' ? t('notify.waitTrigger.valuePlaceholderTech') : draft.type === 'option_quote' ? t('notify.waitTrigger.valuePlaceholderOption') : t('notify.waitTrigger.valuePlaceholderPrice')}
                onChange={(e) => setDraft({ ...draft, value: e.target.value })}
              />
            </label>

            <label>
              {t('notify.waitTrigger.marketPolicyLabel')}
              <select value={draft.marketPolicy} onChange={(e) => setDraft({ ...draft, marketPolicy: e.target.value })}>
                {MARKET_POLICIES.map((k) => <option key={k} value={k}>{marketPolicyLabel(k)}</option>)}
              </select>
            </label>

            <label>
              {t('notify.waitTrigger.checkInterval')}
              <input type="number" min="60" value={draft.intervalSeconds} onChange={(e) => setDraft({ ...draft, intervalSeconds: e.target.value })} />
            </label>

            <label>
              {t('notify.waitTrigger.cooldown')}
              <input type="number" min="60" value={draft.cooldownSeconds} onChange={(e) => setDraft({ ...draft, cooldownSeconds: e.target.value })} />
            </label>

            <label>
              {t('notify.waitTrigger.maxTriggers')}
              <input type="number" min="1" value={draft.maxTriggerCount} onChange={(e) => setDraft({ ...draft, maxTriggerCount: e.target.value })} />
            </label>

            <label className="span-2">
              {t('notify.waitTrigger.namePrefixLabel')}
              <input value={draft.namePrefix} onChange={(e) => setDraft({ ...draft, namePrefix: e.target.value })} placeholder={t('notify.waitTrigger.namePrefixPlaceholder')} />
            </label>

            <div className="span-2 channel-picker">
              <div className="channel-picker-head">
                <strong>{t('notify.waitTrigger.channels')}</strong>
                <small>{t('notify.waitTrigger.channelsHint')}</small>
              </div>
              {enabledChannels.length === 0 && (
                <p className="muted">
                  {t('notify.waitTrigger.noChannels')}
                  {onOpenNotifications && (
                    <button type="button" className="ghost compact" onClick={onOpenNotifications}>{t('notify.waitTrigger.goCreateChannel')}</button>
                  )}
                </p>
              )}
              <div className="channel-grid">
                {enabledChannels.map((c) => {
                  const checked = selectedChannelIds.includes(c.id);
                  return (
                    <label key={c.id} className={`channel-card ${checked ? 'checked' : ''}`}>
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleChannel(c.id)}
                      />
                      <span className="channel-card-body">
                        <strong>{c.label || c.id}</strong>
                        <small>{channelTypeLabel(c.type)}{c.email ? ` · ${c.email}` : c.to ? ` · ${c.to}` : c.chat_id ? ` · ${c.chat_id}` : ''}</small>
                      </span>
                    </label>
                  );
                })}
              </div>
            </div>

            <div className="span-2 form-actions">
              <button className="primary" type="submit" disabled={busy || !symbols.length}>
                <Bell size={14} /> {busy ? t('notify.waitTrigger.creating') : symbols.length > 1 ? t('notify.waitTrigger.createN').replace('{n}', symbols.length) : t('notify.waitTrigger.createOne')}
              </button>
              <button className="ghost" type="button" onClick={() => { setDraft(EMPTY_DRAFT); setSelectedChannelIds([]); }}>
                {t('notify.waitTrigger.resetForm')}
              </button>
            </div>
          </form>
        </section>

        <section className="panel wait-triggers-list-panel">
          <header className="panel-head wait-triggers-list-head">
            <div>
              <strong>{t('notify.waitTrigger.existingAlerts')}</strong>
              <small>{filteredTriggers.length} / {triggers.length}</small>
            </div>
            <input
              className="filter-input"
              placeholder={t('notify.waitTrigger.filterPlaceholder')}
              value={filterSymbol}
              onChange={(e) => setFilterSymbol(e.target.value)}
            />
          </header>

          {filteredTriggers.length === 0 ? (
            <div className="empty-state">
              <p>{triggers.length ? t('notify.waitTrigger.noMatch') : t('notify.waitTrigger.noTriggers')}</p>
            </div>
          ) : (
            <div className="trigger-table">
              {filteredTriggers.map((trigger) => {
                const cond = trigger.condition || {};
                const channelLabels = (trigger.notification_channel_ids || [])
                  .map((id) => channelsById.get(id))
                  .filter(Boolean);
                const testResult = testResults[trigger.id];
                return (
                  <article key={trigger.id} className={`trigger-row ${trigger.enabled ? '' : 'paused'}`}>
                    <div className="trigger-row-main">
                      <div className="trigger-row-title">
                        <strong>{trigger.symbol || cond.symbol || '--'}</strong>
                        <span className="trigger-name">{trigger.name}</span>
                      </div>
                      <div className="trigger-row-meta">
                        <span>
                          {typeLabel(cond.type)}
                          {cond.field ? ` · ${fieldLabel(cond.type, cond.field)}` : ''}
                          {cond.contract_symbol ? ` · ${cond.contract_symbol}` : ''}
                          {' '}{cond.operator || '>='}{' '}{cond.value ?? '--'}
                        </span>
                        <span className="dot">·</span>
                        <span>{policyLabel(trigger.market_policy)}</span>
                        <span className="dot">·</span>
                        <span>{t('notify.waitTrigger.everyCooldown').replace('{interval}', trigger.check_interval_seconds || 300).replace('{cooldown}', trigger.cooldown_seconds || 1800)}</span>
                        <span className="dot">·</span>
                        <span>{t('notify.waitTrigger.triggeredCount').replace('{count}', trigger.trigger_count || 0).replace('{max}', trigger.max_trigger_count || 3)}</span>
                      </div>
                      <div className="trigger-row-meta">
                        <span>{t('notify.waitTrigger.nextCheck').replace('{time}', formatTimestamp(trigger.next_check_at))}</span>
                        {trigger.last_triggered_at && (
                          <>
                            <span className="dot">·</span>
                            <span>{t('notify.waitTrigger.lastTriggered').replace('{time}', formatTimestamp(trigger.last_triggered_at))}</span>
                          </>
                        )}
                      </div>
                      <div className="trigger-row-channels">
                        {channelLabels.length === 0 ? (
                          <span className="channel-pill muted">{t('notify.waitTrigger.defaultChannel')}</span>
                        ) : channelLabels.map((c) => (
                          <span key={c.id} className="channel-pill" title={c.id}>
                            {channelTypeLabel(c.type)} · {c.label || c.id}
                          </span>
                        ))}
                      </div>
                      {testResult && (
                        <div className={`trigger-row-test ${testResult.matched ? 'matched' : ''}`}>
                          {testResult.matched ? t('notify.waitTrigger.testMatched') : t('notify.waitTrigger.testNotMatched')} · {t('notify.waitTrigger.currentValue')} {testResult.current_value ?? '--'}
                          {testResult.reason ? ` · ${testResult.reason}` : ''}
                        </div>
                      )}
                    </div>
                    <div className="trigger-row-actions">
                      <Toggle
                        checked={Boolean(trigger.enabled)}
                        onChange={() => toggleEnabled(trigger)}
                        label={trigger.enabled ? t('notify.waitTrigger.enabled') : t('notify.waitTrigger.paused')}
                      />
                      <button
                        className="ghost compact"
                        type="button"
                        disabled={testingId === trigger.id}
                        onClick={() => runTest(trigger)}
                        title={t('notify.waitTrigger.testCondition')}
                      >
                        <FlaskConical size={14} /> {testingId === trigger.id ? t('notify.waitTrigger.testing') : t('notify.test')}
                      </button>
                      <button
                        className="ghost compact danger"
                        type="button"
                        onClick={() => deleteTrigger(trigger)}
                        title={t('notify.delete')}
                      >
                        <Trash2 size={14} /> {t('notify.delete')}
                      </button>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
