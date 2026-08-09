import { useCallback, useEffect, useRef, useState } from 'react';
import { useVisibilityInterval } from './use-visibility-interval.js';
import { t } from '../i18n/index.js';

const POLL_MS = 15000;
const MAX_SYMBOLS = 8;

const EMPTY_FORM = {
  id: '',
  name: '',
  symbols: [],
  interval_minutes: 5,
  risk_preset: 'conservative',
  total_capital: 3000,
  session_policy: 'regular_only',
  use_broker: false,
  broker: 'longbridge',
  broker_account: '',
  ai_provider: 'deepseek',
};

export function useAutoTradeController({ api, accounts = [], providers = [], fallbackProvider, fallbackAccount }) {
  const [instances, setInstances] = useState([]);
  const [selectedId, setSelectedId] = useState('');
  const [cycles, setCycles] = useState([]);
  const [form, setForm] = useState(EMPTY_FORM);
  const [formOpen, setFormOpen] = useState(false);
  const [cycleDetail, setCycleDetail] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const pollInflight = useRef(false);

  const refreshInstances = useCallback(async () => {
    if (pollInflight.current) return;
    pollInflight.current = true;
    try {
      const rows = await api('/api/auto-trade/instances?limit=50');
      setInstances(Array.isArray(rows) ? rows : []);
      setError('');
    } catch (e) {
      setError(e?.message || String(e));
    } finally {
      pollInflight.current = false;
    }
  }, [api]);

  const refreshCycles = useCallback(async (instanceId) => {
    const id = instanceId || selectedId;
    if (!id) { setCycles([]); return; }
    try {
      const detail = await api(`/api/auto-trade/instances/${encodeURIComponent(id)}?cycles=30`);
      setCycles(Array.isArray(detail?.cycles) ? detail.cycles : []);
    } catch (e) {
      setError(e?.message || String(e));
    }
  }, [api, selectedId]);

  useEffect(() => { refreshInstances(); }, [refreshInstances]);
  useEffect(() => { if (selectedId) refreshCycles(selectedId); }, [selectedId, refreshCycles]);

  // Poll the list (status/next_run) and the open instance's cycles.
  useVisibilityInterval(() => {
    refreshInstances();
    if (selectedId) refreshCycles(selectedId);
  }, POLL_MS);

  const selected = instances.find((i) => i.id === selectedId) || null;

  function openNewForm() {
    setForm({
      ...EMPTY_FORM,
      ai_provider: fallbackProvider || 'deepseek',
      broker_account: fallbackAccount || '',
    });
    setFormOpen(true);
  }

  function openEditForm(instance) {
    setForm({
      id: instance.id,
      name: instance.name || '',
      symbols: instance.symbols || [],
      interval_minutes: instance.interval_minutes || 5,
      risk_preset: instance.risk_preset || 'conservative',
      total_capital: instance.total_capital ?? 3000,
      session_policy: instance.session_policy || 'regular_only',
      use_broker: Boolean(instance.use_broker),
      broker: instance.broker || 'longbridge',
      broker_account: instance.broker_account || '',
      ai_provider: instance.ai_provider || 'deepseek',
    });
    setFormOpen(true);
  }

  function closeForm() { setFormOpen(false); }

  function updateForm(patch) { setForm((f) => ({ ...f, ...patch })); }

  function addSymbol(raw) {
    const sym = String(raw || '').trim().toUpperCase();
    if (!sym) return;
    setForm((f) => {
      if (f.symbols.includes(sym) || f.symbols.length >= MAX_SYMBOLS) return f;
      return { ...f, symbols: [...f.symbols, sym] };
    });
  }

  function removeSymbol(sym) {
    setForm((f) => ({ ...f, symbols: f.symbols.filter((s) => s !== sym) }));
  }

  async function saveForm() {
    if (!form.symbols.length) { setError(t('autoTrade.needSymbol')); return null; }
    setBusy(true);
    setError('');
    const payload = {
      name: form.name || 'Auto-Trade',
      symbols: form.symbols,
      interval_minutes: Number(form.interval_minutes) || 5,
      risk_preset: form.risk_preset,
      total_capital: Number(form.total_capital) || 0,
      session_policy: form.session_policy,
      use_broker: Boolean(form.use_broker),
      broker: form.broker,
      broker_account: form.use_broker ? form.broker_account : null,
      ai_provider: form.ai_provider,
      config: {},
    };
    try {
      const path = form.id
        ? `/api/auto-trade/instances/${encodeURIComponent(form.id)}`
        : '/api/auto-trade/instances';
      const row = await api(path, { method: form.id ? 'PUT' : 'POST', body: JSON.stringify(payload) });
      await refreshInstances();
      setFormOpen(false);
      if (row?.id) setSelectedId(row.id);
      return row;
    } catch (e) {
      setError(e?.message || String(e));
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function deleteInstance(instanceId) {
    setBusy(true);
    try {
      await api(`/api/auto-trade/instances/${encodeURIComponent(instanceId)}`, { method: 'DELETE' });
      if (selectedId === instanceId) { setSelectedId(''); setCycles([]); }
      await refreshInstances();
    } catch (e) {
      setError(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  async function startInstance(instance, confirmation) {
    setBusy(true);
    setError('');
    try {
      const body = JSON.stringify({ confirmation: confirmation || null });
      await api(`/api/auto-trade/instances/${encodeURIComponent(instance.id)}/start`, { method: 'POST', body });
      await refreshInstances();
      return true;
    } catch (e) {
      setError(e?.message || String(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function instanceAction(instanceId, action) {
    setBusy(true);
    try {
      await api(`/api/auto-trade/instances/${encodeURIComponent(instanceId)}/${action}`, { method: 'POST' });
      await refreshInstances();
    } catch (e) {
      setError(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  async function openCycleDetail(cycle) {
    if (!selectedId || !cycle?.id) return;
    try {
      const detail = await api(
        `/api/auto-trade/instances/${encodeURIComponent(selectedId)}/cycles/${encodeURIComponent(cycle.id)}`,
      );
      setCycleDetail(detail);
    } catch (e) {
      setError(e?.message || String(e));
    }
  }

  function closeCycleDetail() { setCycleDetail(null); }

  return {
    instances,
    selected,
    selectedId,
    setSelectedId,
    cycles,
    form,
    formOpen,
    cycleDetail,
    error,
    busy,
    openNewForm,
    openEditForm,
    closeForm,
    updateForm,
    addSymbol,
    removeSymbol,
    saveForm,
    deleteInstance,
    startInstance,
    pauseInstance: (id) => instanceAction(id, 'pause'),
    stopInstance: (id) => instanceAction(id, 'stop'),
    openCycleDetail,
    closeCycleDetail,
    refreshInstances,
    refreshCycles,
  };
}
