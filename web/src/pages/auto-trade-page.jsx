import React, { useState, useEffect } from 'react';
import { PageHeader } from '../components/app-shell/page-header.jsx';
import { TradeInstanceDetail } from '../components/trade-instance-detail.jsx';
import { DisclosurePanel, Metric, SectionTitle } from '../components/common.jsx';
import { useAutoTradeController } from '../hooks/use-auto-trade-controller.js';
import { t } from '../i18n/index.js';

const PRESETS = ['conservative', 'balanced', 'aggressive'];

function StatusBadge({ status }) {
  const map = {
    active: { cls: 'live', label: t('autoTrade.statusActive') },
    paused: { cls: 'idle', label: t('autoTrade.statusPaused') },
    stopped: { cls: 'idle', label: t('autoTrade.statusStopped') },
  };
  const s = map[status] || map.stopped;
  return <span className="context-bar-clock"><span className={`context-bar-dot ${s.cls}`} aria-hidden />{s.label}</span>;
}

function InstanceForm({ ctrl, providers, accounts }) {
  const { form, updateForm, addSymbol, removeSymbol, saveForm, closeForm, busy } = ctrl;
  const [symbolDraft, setSymbolDraft] = useState('');
  const longbridgeAccounts = (accounts || []).map((a) => a.name || a.account || a);

  return (
    <div className="panel control-panel">
      <SectionTitle title={form.id ? t('autoTrade.editInstance') : t('autoTrade.newInstance')} />
      <div className="form">
        <label className="form-field">
          <span className="field-label">{t('autoTrade.nameLabel')}</span>
          <input type="text" value={form.name} placeholder={t('autoTrade.namePlaceholder')}
            onChange={(e) => updateForm({ name: e.target.value })} />
        </label>

        <div className="form-field">
          <span className="field-label">{t('autoTrade.symbolsLabel')}</span>
          <input type="text" value={symbolDraft} placeholder={t('autoTrade.symbolsPlaceholder')}
            onChange={(e) => setSymbolDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addSymbol(symbolDraft); setSymbolDraft(''); } }} />
          <div className="strategy-pills">
            {form.symbols.map((s) => (
              <button key={s} type="button" className="route-pill compact" onClick={() => removeSymbol(s)}>{s} ✕</button>
            ))}
          </div>
        </div>

        <label className="form-field">
          <span className="field-label">{t('autoTrade.intervalLabel')}</span>
          <input type="number" min="1" max="240" value={form.interval_minutes}
            onChange={(e) => updateForm({ interval_minutes: e.target.value })} />
        </label>

        <label className="form-field">
          <span className="field-label">{t('autoTrade.totalCapitalLabel')}</span>
          <input type="number" min="0" step="100" value={form.total_capital}
            onChange={(e) => updateForm({ total_capital: e.target.value })} />
          <span className="strategy-hint">{t('autoTrade.totalCapitalHint')}</span>
        </label>

        <DisclosurePanel title={t('scanner2.advancedFilters')} summary={`${form.risk_preset} · ${form.session_policy}`} className="embedded-disclosure">
          <label className="form-field">
            <span className="field-label">{t('autoTrade.presetLabel')}</span>
            <select value={form.risk_preset} onChange={(e) => updateForm({ risk_preset: e.target.value })}>
              {PRESETS.map((p) => <option key={p} value={p}>{t(`autoTrade.preset${p[0].toUpperCase()}${p.slice(1)}`)}</option>)}
            </select>
          </label>

          <label className="form-field">
            <span className="field-label">{t('autoTrade.sessionPolicyLabel')}</span>
            <select value={form.session_policy} onChange={(e) => updateForm({ session_policy: e.target.value })}>
              <option value="regular_only">{t('autoTrade.sessionRegularOnly')}</option>
              <option value="include_extended">{t('autoTrade.sessionExtended')}</option>
            </select>
          </label>

          <label className="form-field">
            <span className="field-label">{t('autoTrade.aiProviderLabel')}</span>
            <select value={form.ai_provider} onChange={(e) => updateForm({ ai_provider: e.target.value })}>
              {(providers || []).map((p) => {
                const id = p.id || p.provider || p;
                return <option key={id} value={id}>{p.label || p.name || id}</option>;
              })}
              {!(providers || []).length && <option value="deepseek">deepseek</option>}
            </select>
          </label>

          <label className="form-field">
            <span className="field-label">{t('autoTrade.brokerLabel')}</span>
            <span className="strategy-hint">
              <input type="checkbox" checked={form.use_broker} onChange={(e) => updateForm({ use_broker: e.target.checked })} />
              {form.use_broker ? t('autoTrade.liveBadge') : t('autoTrade.brokerNone')}
            </span>
          </label>

          {form.use_broker && (
            <label className="form-field">
              <span className="field-label">{t('autoTrade.brokerAccountLabel')}</span>
              <select value={form.broker_account || ''} onChange={(e) => updateForm({ broker_account: e.target.value })}>
                <option value="">--</option>
                {longbridgeAccounts.map((a) => <option key={a} value={a}>{a}</option>)}
              </select>
            </label>
          )}
        </DisclosurePanel>

        <div className="action-row">
          <button type="button" className="ghost" onClick={closeForm} disabled={busy}>{t('autoTrade.cancel')}</button>
          <button type="button" className="primary" onClick={saveForm} disabled={busy}>
            {busy ? t('autoTrade.saving') : t('autoTrade.save')}
          </button>
        </div>
      </div>
    </div>
  );
}

function LiveStartDialog({ onConfirm, onCancel }) {
  const [phrase, setPhrase] = useState('');
  const required = t('autoTrade.liveConfirmPhrase');
  return (
    <div className="panel status-box warning-box">
      <SectionTitle title={t('autoTrade.liveConfirmTitle')} />
      <p>{t('autoTrade.liveConfirmBody').replace('{phrase}', required)}</p>
      <input type="text" value={phrase} placeholder={t('autoTrade.liveConfirmPlaceholder')}
        onChange={(e) => setPhrase(e.target.value)} />
      <div className="action-row">
        <button type="button" className="ghost" onClick={onCancel}>{t('autoTrade.cancel')}</button>
        <button type="button" className="primary" disabled={phrase.trim() !== required}
          onClick={() => onConfirm(phrase.trim())}>{t('autoTrade.start')}</button>
      </div>
    </div>
  );
}

function InstanceRow({ instance, active, onSelect, onEdit, onStart, onPause, onStop, onDelete }) {
  const stop = (fn) => (e) => { e.stopPropagation(); fn(); };
  return (
    <tr
      className={`at-row ${active ? 'active' : ''}`}
      onClick={() => onSelect(instance.id)}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(instance.id); } }}
      tabIndex={0}
      role="button"
    >
      <td className="at-row-name">
        <strong>{instance.name}</strong>
        {instance.id && <div className="muted">{t('autoTrade.instanceId')}: <code>{instance.id}</code></div>}
      </td>
      <td className="at-row-symbols">{(instance.symbols || []).join(', ') || '--'}</td>
      <td>
        <span className={`tag ${instance.use_broker ? 'tag-live' : 'tag-paper'}`}>
          {instance.use_broker ? t('autoTrade.liveBadge') : t('autoTrade.dryRunBadge')}
        </span>
      </td>
      <td><StatusBadge status={instance.status} /></td>
      <td className="at-row-actions" onClick={(e) => e.stopPropagation()}>
        <button type="button" className="ghost compact at-detail-link" onClick={stop(() => onSelect(instance.id))}>{t('autoTrade.viewDetail')}</button>
        {instance.status !== 'active'
          ? <button type="button" className="primary compact" onClick={stop(() => onStart(instance))}>{t('autoTrade.start')}</button>
          : <button type="button" className="ghost compact" onClick={stop(() => onPause(instance.id))}>{t('autoTrade.pause')}</button>}
        {instance.status === 'active' && <button type="button" className="ghost compact" onClick={stop(() => onStop(instance.id))}>{t('autoTrade.stop')}</button>}
        <button type="button" className="ghost compact" onClick={stop(() => onEdit(instance))}>{t('autoTrade.editInstance')}</button>
        <button type="button" className="danger compact-danger" onClick={stop(() => onDelete(instance.id))}>{t('autoTrade.delete')}</button>
      </td>
    </tr>
  );
}

function CycleRow({ cycle, onView }) {
  const orders = cycle?.summary?.orders ?? 0;
  const thesis = cycle?.summary?.error || cycle?.error || cycle?.summary?.thesis || cycle?.summary?.run_status || cycle?.status || '';
  return (
    <tr>
      <td>{t('autoTrade.cycleIndex').replace('{n}', cycle.cycle_index)}</td>
      <td>{cycle.intraday_phase || '--'}</td>
      <td><span className={`tag ${cycle.dry_run ? 'tag-paper' : 'tag-live'}`}>{cycle.dry_run ? t('autoTrade.dryRunBadge') : t('autoTrade.liveBadge')}</span></td>
      <td>{cycle.status}</td>
      <td>{orders}</td>
      <td className="muted">{String(thesis).slice(0, 80)}</td>
      <td><button type="button" className="ghost compact" onClick={() => onView(cycle)}>{t('autoTrade.viewCycle')}</button></td>
    </tr>
  );
}

export function AutoTradePage({ api, providers = [], accounts = [], fallbackProvider, fallbackAccount, detailId = '', onOpenDetail, onBackToList }) {
  const ctrl = useAutoTradeController({ api, accounts, providers, fallbackProvider, fallbackAccount });
  const {
    instances, selected, selectedId, setSelectedId, cycles,
    formOpen, openNewForm, openEditForm, error,
    deleteInstance, startInstance, pauseInstance, stopInstance,
    cycleDetail, openCycleDetail, closeCycleDetail, refreshInstances,
  } = ctrl;
  const [liveDialog, setLiveDialog] = useState(null);
  const detailMode = Boolean(detailId);

  // Keep the controller's selection in sync with the URL-driven detail id so
  // the detail view loads the right instance + cycles on direct navigation.
  useEffect(() => {
    if (detailId && detailId !== selectedId) setSelectedId(detailId);
  }, [detailId, selectedId, setSelectedId]);

  function openDetail(id) {
    setSelectedId(id);
    if (onOpenDetail) onOpenDetail(id);
  }

  function handleStart(instance) {
    if (instance.use_broker) { setLiveDialog(instance); return; }
    startInstance(instance, null);
  }

  function handleDelete(id) {
    // eslint-disable-next-line no-alert
    if (typeof window !== 'undefined' && !window.confirm(t('autoTrade.confirmDelete'))) return;
    deleteInstance(id);
  }

  return (
    <main className="shell">
      <PageHeader
        eyebrow={t('autoTrade.eyebrow')}
        title={t('autoTrade.heroTitle')}
        subtitle={t('autoTrade.heroSubtitle')}
        actions={
          <>
            <button type="button" className="ghost" onClick={refreshInstances}>{t('autoTrade.refresh')}</button>
            <button type="button" className="primary" onClick={openNewForm}>{t('autoTrade.newInstance')}</button>
          </>
        }
      />

      <div className={`trading-layout ${formOpen ? '' : 'detail-mode'}`}>
        {formOpen && <InstanceForm ctrl={ctrl} providers={providers} accounts={accounts} />}

        <section className="workspace">
          {error && <div className="error">{error}</div>}

          {liveDialog && (
            <LiveStartDialog
              onCancel={() => setLiveDialog(null)}
              onConfirm={async (phrase) => { const ok = await startInstance(liveDialog, phrase); if (ok) setLiveDialog(null); }}
            />
          )}

          {!detailMode && (
          <div className="panel instance-list-panel">
            <div className="answer-head">
              <SectionTitle title={t('autoTrade.heroTitle')} />
            </div>
            {!instances.length && <p className="muted">{t('autoTrade.noInstances')}</p>}
            {instances.length > 0 && (
              <table className="at-table at-instance-table">
                <thead>
                  <tr>
                    <th>{t('autoTrade.colName')}</th>
                    <th>{t('autoTrade.colSymbols')}</th>
                    <th>{t('autoTrade.colMode')}</th>
                    <th>{t('autoTrade.colStatus')}</th>
                    <th>{t('autoTrade.colActions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {instances.map((inst) => (
                    <InstanceRow
                      key={inst.id}
                      instance={inst}
                      active={inst.id === selectedId}
                      onSelect={openDetail}
                      onEdit={openEditForm}
                      onStart={handleStart}
                      onPause={pauseInstance}
                      onStop={stopInstance}
                      onDelete={handleDelete}
                    />
                  ))}
                </tbody>
              </table>
            )}
          </div>
          )}

          {detailMode && (
            <div className="answer-head">
              <button type="button" className="ghost" onClick={onBackToList}>{t('autoTrade.backToList')}</button>
              {selected && (
                <div className="at-detail-actions">
                  {selected.status !== 'active'
                    ? <button type="button" className="primary compact" onClick={() => handleStart(selected)}>{t('autoTrade.start')}</button>
                    : <button type="button" className="ghost compact" onClick={() => pauseInstance(selected.id)}>{t('autoTrade.pause')}</button>}
                  {selected.status === 'active' && <button type="button" className="ghost compact" onClick={() => stopInstance(selected.id)}>{t('autoTrade.stop')}</button>}
                  <button type="button" className="ghost compact" onClick={() => openEditForm(selected)}>{t('autoTrade.editInstance')}</button>
                  <button type="button" className="danger compact-danger" onClick={() => handleDelete(selected.id)}>{t('autoTrade.delete')}</button>
                </div>
              )}
            </div>
          )}

          {detailMode && !selected && (
            <div className="panel"><p className="muted">{t('autoTrade.noInstances')}</p></div>
          )}

          {detailMode && selected && (
            <div className="panel">
              <SectionTitle title={selected.name} />
              {selected.id && <p className="strategy-hint">{t('autoTrade.instanceId')}: <code>{selected.id}</code></p>}
              {selected.halted_reason && (
                <p className="strategy-hint" style={{ color: 'var(--danger, #d9534f)' }}>
                  {t('autoTrade.haltedBanner').replace('{reason}', selected.halted_reason)}
                </p>
              )}
              <div className="instance-grid">
                <Metric label={t('autoTrade.instanceId')} value={selected.id || '--'} />
                <Metric label={t('autoTrade.totalCapitalLabel')} value={`$${Number(selected.total_capital || 0).toLocaleString()}`}
                  sub={selected.caps ? t('autoTrade.sessionBudgetSub').replace('{amount}', `$${Math.round(Number(selected.total_capital || 0) * (selected.caps.session_capital_budget_pct || 0)).toLocaleString()}`) : ''} />
                <Metric label={t('autoTrade.dailyPnl')}
                  value={`$${Number(selected.realized_pnl_today || 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
                  tone={Number(selected.realized_pnl_today || 0) < 0 ? 'danger' : (Number(selected.realized_pnl_today || 0) > 0 ? 'ok' : '')} />
                <Metric label={t('autoTrade.nextRun')} value={selected.next_run_at || '--'} />
                <Metric label={t('autoTrade.lastRun')} value={selected.last_run_at || '--'} />
                <Metric label={t('autoTrade.cyclesToday')} value={selected.cycles_today ?? 0} />
                <Metric label={t('autoTrade.ordersToday')} value={selected.orders_today ?? 0} />
                {selected.caps && <Metric label={t('autoTrade.capOrderCycles')} value={selected.caps.max_order_cycles_per_session} />}
                {selected.caps && <Metric label={t('autoTrade.capOpenPositions')} value={selected.caps.max_open_positions} />}
                {selected.caps && <Metric label={t('autoTrade.capCapital')} value={`${Math.round((selected.caps.session_capital_budget_pct || 0) * 100)}%`} />}
                {selected.caps?.max_daily_loss_pct != null && (
                  <Metric label={t('autoTrade.capDailyLoss')} value={`${Math.round((selected.caps.max_daily_loss_pct || 0) * 100)}%`} />
                )}
              </div>
              <p className="strategy-hint">{t('autoTrade.intelligentModeNote')}</p>
            </div>
          )}

          {detailMode && selected && (
            <div className="panel">
              <SectionTitle title={t('autoTrade.cyclesTitle')} />
              {!cycles.length && <p className="muted">{t('autoTrade.noCycles')}</p>}
              {cycles.length > 0 && (
                <table className="at-table">
                  <thead>
                    <tr>
                      <th>{t('autoTrade.cycleIndex').replace('{n}', '#')}</th>
                      <th>{t('autoTrade.phaseLabel')}</th>
                      <th>{t('autoTrade.brokerLabel')}</th>
                      <th>{t('autoTrade.cycleStatus')}</th>
                      <th>{t('autoTrade.cycleOrders')}</th>
                      <th>{t('autoTrade.cycleThesis')}</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {cycles.map((c) => <CycleRow key={c.id} cycle={c} onView={openCycleDetail} />)}
                  </tbody>
                </table>
              )}
            </div>
          )}

          {detailMode && cycleDetail && (
            <div className="panel">
              <div className="answer-head">
                <SectionTitle title={t('autoTrade.cycleDetailTitle')} />
                <button type="button" className="ghost" onClick={closeCycleDetail}>{t('autoTrade.backToList')}</button>
              </div>
              <h4>{t('autoTrade.linkedRun')}</h4>
              {(cycleDetail.runs && cycleDetail.runs.length)
                ? cycleDetail.runs.map((run) => (
                    <TradeInstanceDetail key={run.id} instance={run} activeTab="overview" onTabChange={() => {}} startCollapsed />
                  ))
                : <p className="muted">{t('autoTrade.noLinkedRun')}</p>}
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
