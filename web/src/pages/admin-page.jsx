import React, { useEffect, useMemo, useState } from 'react';
import { Activity, AlertTriangle, ArrowRight, ChevronDown, ChevronRight, Cpu, Database, Gauge, RotateCw, Search, ShieldCheck, Sparkles, Users, WalletCards } from 'lucide-react';
import { Toggle } from '../components/common.jsx';
import { CopyableId } from '../components/copyable-id.jsx';
import { SessionAndRouteBar } from '../components/session-route-bar.jsx';
import { ServerHealthPanel } from '../components/server-health-panel.jsx';
import { bjIsoToLocalInput, formatBjDisplay, localInputToBjIso, userRemainingInputValue, userRemainingLabel } from '../utils/display.js';
import { PageHeader } from '../components/app-shell/page-header.jsx';
import { useAppShell } from '../components/app-shell/app-shell-context.js';

const t = (p) => window._t(p);

const quotaFields = [
  ['max_daily_scans', 'admin2.quotaDailyScans', 'daily_scans'],
  ['max_daily_ai_scans', 'admin2.quotaDailyAiScans', 'daily_ai_scans'],
  ['max_daily_ai_chat', 'admin2.quotaDailyAiChat', 'daily_ai_chat'],
  ['max_watchlists', 'admin2.quotaWatchlists', 'watchlists'],
  ['max_scan_loop_instances', 'admin2.quotaRadarInstances', 'scan_loop_instances'],
  ['max_notification_channels', 'admin2.quotaNotificationChannels', 'notification_channels'],
  ['max_longbridge_accounts', 'admin2.quotaLongbridgeAccounts', 'longbridge_accounts'],
];

const LOTTERY_STATUS_META = {
  draft: { label: 'admin2.lotteryDraft', tone: 'off' },
  open: { label: 'admin2.lotteryOpen', tone: 'ok' },
  drawn: { label: 'admin2.lotteryDrawn', tone: 'warning' },
  announced: { label: 'admin2.lotteryAnnounced', tone: 'ok' },
};

function limitLabel(value) {
  const numberValue = Number(value);
  return numberValue < 0 ? t('admin2.unlimited') : `${Number.isFinite(numberValue) ? numberValue : 0}`;
}

function usageRatio(user, field, usageKey) {
  const limit = Number(user[field] ?? user.limits?.[field] ?? 0);
  const used = Number(user.usage?.[usageKey] ?? 0);
  if (limit < 0) return { used, limit, label: `${used} / ${t('admin2.unlimited')}`, percent: 8, over: false };
  const safeLimit = Math.max(limit, 0);
  const percent = safeLimit > 0 ? Math.min(100, Math.round((used / safeLimit) * 100)) : 100;
  return { used, limit: safeLimit, label: `${used} / ${safeLimit}`, percent, over: used >= safeLimit };
}

function aggregateQuota(users, field, usageKey) {
  return users.reduce(
    (acc, user) => {
      const limit = Number(user[field] ?? user.limits?.[field] ?? 0);
      const used = Number(user.usage?.[usageKey] ?? 0);
      acc.used += Number.isFinite(used) ? used : 0;
      if (limit < 0) acc.unlimited += 1;
      else acc.limit += Number.isFinite(limit) ? Math.max(limit, 0) : 0;
      return acc;
    },
    { used: 0, limit: 0, unlimited: 0 },
  );
}

function percentLabel(value, total) {
  if (!total) return 0;
  return Math.min(100, Math.round((Number(value || 0) / Number(total || 1)) * 100));
}

function healthLabel(serverHealth) {
  if (!serverHealth) return { label: t('admin2.syncing'), tone: 'warning' };
  if (serverHealth.status === 'ok') return { label: t('admin2.online'), tone: 'ok' };
  return { label: t('admin2.degraded'), tone: 'danger' };
}

function DashboardMetric({ icon: Icon, label, value, detail, tone = 'ok' }) {
  return (
    <div className={`admin-kpi-card ${tone}`}>
      <div className="admin-kpi-icon"><Icon size={18} /></div>
      <span>{label}</span>
      <strong>{value}</strong>
      {detail && <small>{detail}</small>}
    </div>
  );
}

function AdminHeroDashboard({ summary, quotaTotals, serverHealth, betaLottery, onOpenAiCosts, refreshUsers }) {
  const health = healthLabel(serverHealth);
  const scanPressure = percentLabel(quotaTotals.dailyScans.used, quotaTotals.dailyScans.limit);
  const aiPressure = percentLabel(quotaTotals.dailyAi.used, quotaTotals.dailyAi.limit);
  const channelPressure = percentLabel(quotaTotals.channels.used, quotaTotals.channels.limit);
  const lotteryText = `${betaLottery?.entry_count ?? 0} ${t('admin2.signupsUnit')} · ${betaLottery?.winner_count ?? 0} ${t('admin2.winnersUnit')}`;
  return (
    <section className="admin-command-deck">
      <div className="admin-command-primary">
        <div className="admin-command-gridline" />
        <div className="admin-command-title">
          <span><Sparkles size={14} /> Command Deck</span>
          <h2>{t('admin2.commandDeckTitle')}</h2>
          <p>{t('admin2.commandDeckDesc')}</p>
        </div>
        <div className="admin-command-actions">
          <button className="primary compact" type="button" onClick={onOpenAiCosts}><WalletCards size={15} /> {t('admin2.aiCostCenter')}</button>
          <button className="ghost compact" type="button" onClick={refreshUsers}><Activity size={15} /> {t('admin2.syncStatus')}</button>
        </div>
        <div className="admin-signal-strip">
          <span className={health.tone}>{t('admin2.system')} {health.label}</span>
          <span>{summary.active}/{summary.total} {t('admin2.usersActive')}</span>
          <span>{summary.quotaWarnings ? `${summary.quotaWarnings} ${t('admin2.quotaAlertsCount')}` : t('admin2.quotaNormal')}</span>
          <span>{lotteryText}</span>
        </div>
      </div>
      <div className="admin-kpi-grid">
        <DashboardMetric icon={Users} label={t('admin2.activeUsers')} value={summary.active} detail={`${t('admin2.totalUsers')} ${summary.total}`} />
        <DashboardMetric icon={ShieldCheck} label={t('admin2.admins')} value={summary.admins} detail={`${summary.trade} ${t('admin2.liveAccounts')}`} />
        <DashboardMetric icon={Gauge} label={t('admin2.scanPressure')} value={`${scanPressure}%`} detail={`${quotaTotals.dailyScans.used}/${quotaTotals.dailyScans.limit || t('admin2.unlimited')}`} tone={scanPressure > 85 ? 'warning' : 'ok'} />
        <DashboardMetric icon={Cpu} label={t('admin2.aiQuota')} value={`${aiPressure}%`} detail={`${quotaTotals.dailyAi.used}/${quotaTotals.dailyAi.limit || t('admin2.unlimited')}`} tone={aiPressure > 85 ? 'warning' : 'ok'} />
        <DashboardMetric icon={Database} label={t('admin2.quotaWatchlists')} value={quotaTotals.watchlists.used} detail={`${t('admin2.totalQuota')} ${quotaTotals.watchlists.limit || t('admin2.unlimited')}`} />
        <DashboardMetric icon={AlertTriangle} label={t('admin2.alerts')} value={summary.quotaWarnings} detail={`${t('admin2.notificationPressure')} ${channelPressure}%`} tone={summary.quotaWarnings ? 'danger' : 'ok'} />
      </div>
    </section>
  );
}

function UserQuotaGrid({ user, updateUser }) {
  return (
    <div className="admin-quota-grid">
      {quotaFields.map(([field, label, usageKey]) => {
        const ratio = usageRatio(user, field, usageKey);
        return (
          <label className={`quota-cell ${ratio.over ? 'quota-hot' : ''}`} key={field}>
            <span>{t(label)}</span>
            <div className="quota-meter">
              <i style={{ '--quota-pct': `${ratio.percent}%` }} />
            </div>
            <div className="quota-edit-row">
              <small>{ratio.label}</small>
              <input
                type="number"
                min="-1"
                max="1000000"
                step="1"
                defaultValue={user[field] ?? user.limits?.[field] ?? 0}
                disabled={!user.editable}
                onBlur={(event) => updateUser(user.username, { [field]: Number(event.target.value || 0) })}
              />
            </div>
          </label>
        );
      })}
    </div>
  );
}

function UserRow({ user, updateUser, deleteUser, expanded, onToggle }) {
  const hotCount = quotaFields.filter(([field, , usageKey]) => usageRatio(user, field, usageKey).over).length;
  return (
    <div className={`uadmin-card ${user.expired ? 'is-expired' : ''} ${expanded ? 'is-open' : ''}`}>
      <button
        type="button"
        className="uadmin-card-summary"
        aria-expanded={expanded}
        onClick={onToggle}
      >
        <span className="uadmin-card-caret">{expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}</span>
        <span className="uadmin-card-id">
          <strong>{user.username}</strong>
          <small>{user.source === 'env' ? t('admin2.envConfig') : t('admin2.databaseSource')} · {user.is_admin ? t('admin2.adminRole') : t('admin2.normalUser')} · {userRemainingLabel(user)}</small>
        </span>
        <span className="uadmin-card-tags">
          <span className={`uadmin-tag ${user.can_analyze ? 'is-on' : 'is-off'}`}>{user.can_analyze ? t('admin2.analyze') : t('admin2.noAnalyze')}</span>
          <span className={`uadmin-tag ${user.can_trade ? 'is-on' : 'is-off'}`}>{user.can_trade ? t('admin2.live') : t('admin2.noLive')}</span>
          <span className={`uadmin-tag ${user.expired ? 'is-off' : 'is-on'}`}>{user.expired ? t('admin2.expired') : t('admin2.valid')}</span>
          {hotCount > 0 && <span className="uadmin-tag is-hot">{hotCount} {t('admin2.quotaFull')}</span>}
        </span>
      </button>
      {expanded && (
        <div className="uadmin-card-body">
          <div className="admin-permission-grid">
            <Toggle
              checked={Boolean(user.can_analyze)}
              disabled={!user.editable}
              onChange={(checked) => updateUser(user.username, { can_analyze: checked })}
              label={t('admin2.analyze')}
            />
            <Toggle
              checked={Boolean(user.can_trade)}
              disabled={!user.editable}
              onChange={(checked) => updateUser(user.username, { can_trade: checked })}
              label={t('admin2.live')}
            />
            <Toggle
              checked={Boolean(user.is_admin)}
              disabled={!user.editable}
              onChange={(checked) => updateUser(user.username, { is_admin: checked })}
              label={t('admin2.adminRole')}
            />
            <label className="user-expiry">
              <span>{t("admin.remainingDays")}</span>
              <input
                type="number"
                min="0"
                max="3650"
                step="0.25"
                defaultValue={userRemainingInputValue(user)}
                disabled={!user.editable}
                onBlur={(event) => updateUser(user.username, { remaining_days: Number(event.target.value || 0) })}
              />
            </label>
          </div>
          <UserQuotaGrid user={user} updateUser={updateUser} />
          <div className="admin-user-actions">
            <button className="ghost" type="button" disabled={!user.editable} onClick={() => deleteUser(user.username)}>{t("admin.delete")}</button>
          </div>
        </div>
      )}
    </div>
  );
}

function LotteryControlPanel({ betaLottery, onLotteryAction, refreshUsers }) {
  const status = betaLottery?.status || (betaLottery?.announced ? 'announced' : 'open');
  const meta = LOTTERY_STATUS_META[status] || LOTTERY_STATUS_META.draft;
  const [busy, setBusy] = useState('');
  const run = async (action, confirmText) => {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(action);
    try {
      await onLotteryAction(action);
    } finally {
      setBusy('');
    }
  };
  const disabled = Boolean(busy);
  return (
    <div className="lottery-lifecycle">
      <div className="lottery-lifecycle-head">
        <div>
          <strong>{t("admin.lotteryLifecycle")}</strong>
          <p>{t('admin2.lotteryLifecycleDesc')}</p>
        </div>
        <span className={`lottery-status-badge ${meta.tone}`}>{t(meta.label)}</span>
      </div>
      <div className="lottery-lifecycle-actions">
        <button className="primary compact" type="button" disabled={disabled || status === 'open'} onClick={() => run('open')}>{t("admin.openReg")}</button>
        <button className="ghost compact" type="button" disabled={disabled || status !== 'open'} onClick={() => run('close', t('admin2.confirmPauseReg'))}>{t("admin.pauseReg")}</button>
        <button className="ghost compact" type="button" disabled={disabled || status === 'announced'} onClick={() => run('draw', t('admin2.confirmDraw'))}>{t("admin.draw")}</button>
        <button className="primary compact" type="button" disabled={disabled || status === 'announced'} onClick={() => run('publish', t('admin2.confirmPublish'))}>{t("admin.publish")}</button>
        <button className="ghost compact danger" type="button" disabled={disabled} onClick={() => run('reset', t('admin2.confirmReset'))}>{t("admin.reset")}</button>
        <button className="ghost compact" type="button" disabled={disabled} onClick={refreshUsers}>{t('admin2.refresh')}</button>
      </div>
    </div>
  );
}

function LotteryConfigForm({ betaLottery, onLotteryAction }) {
  const config = betaLottery?.config || {};
  const buildState = () => ({
    slot_count: String(config.slot_count ?? betaLottery?.slot_count ?? 15),
    user_valid_days: String(config.user_valid_days ?? betaLottery?.user_valid_days ?? 7),
    registration_start_at: bjIsoToLocalInput(betaLottery?.registration_start_at_bj),
    announce_at: bjIsoToLocalInput(betaLottery?.announce_at_bj),
    limits: { ...(config.limits || {}) },
  });
  const [draft, setDraft] = useState(buildState);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setDraft(buildState());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [betaLottery]);

  const setLimit = (field, value) => {
    setDraft((prev) => ({ ...prev, limits: { ...prev.limits, [field]: value } }));
  };

  const save = async () => {
    setBusy(true);
    setSaved(false);
    try {
      const limits = {};
      quotaFields.forEach(([field]) => {
        const raw = draft.limits[field];
        if (raw !== '' && raw !== undefined && raw !== null) {
          limits[field] = Number(raw);
        }
      });
      await onLotteryAction('set_config', {
        slot_count: Number(draft.slot_count),
        user_valid_days: Number(draft.user_valid_days),
        registration_start_at: localInputToBjIso(draft.registration_start_at),
        announce_at: localInputToBjIso(draft.announce_at),
        limits,
      });
      setSaved(true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="lottery-config" id="lottery-config">
      <div className="lottery-config-head">
        <div>
          <strong>{t("admin.lotteryConfig")}</strong>
          <p>{t('admin2.lotteryConfigDesc')}</p>
        </div>
        {saved && <span className="lottery-status-badge ok">{t("admin.saved")}</span>}
      </div>
      <div className="lottery-config-grid">
        <label className="lottery-config-field">
          <span>{t('admin2.lotteryStartTime')}</span>
          <input
            type="datetime-local"
            value={draft.registration_start_at}
            onChange={(event) => setDraft((prev) => ({ ...prev, registration_start_at: event.target.value }))}
          />
        </label>
        <label className="lottery-config-field">
          <span>{t('admin2.lotteryDrawTime')}</span>
          <input
            type="datetime-local"
            value={draft.announce_at}
            onChange={(event) => setDraft((prev) => ({ ...prev, announce_at: event.target.value }))}
          />
        </label>
        <label className="lottery-config-field">
          <span>{t("admin.slotCount")}</span>
          <input
            type="number"
            min="1"
            max="500"
            step="1"
            value={draft.slot_count}
            onChange={(event) => setDraft((prev) => ({ ...prev, slot_count: event.target.value }))}
          />
        </label>
        <label className="lottery-config-field">
          <span>{t("admin.userValidDays")}</span>
          <input
            type="number"
            min="0.25"
            max="3650"
            step="0.25"
            value={draft.user_valid_days}
            onChange={(event) => setDraft((prev) => ({ ...prev, user_valid_days: event.target.value }))}
          />
        </label>
      </div>
      <div className="lottery-config-limits">
        <span className="lottery-config-subtitle">{t('admin2.perUserQuota')}</span>
        <div className="lottery-config-grid">
          {quotaFields.map(([field, label]) => (
            <label key={field} className="lottery-config-field">
              <span>{t(label)}</span>
              <input
                type="number"
                min="-1"
                step="1"
                value={draft.limits[field] ?? ''}
                placeholder={t('admin2.defaultPlaceholder')}
                onChange={(event) => setLimit(field, event.target.value)}
              />
            </label>
          ))}
        </div>
      </div>
      <div className="lottery-config-actions">
        <button className="primary compact" type="button" disabled={busy} onClick={save}>{busy ? t('admin2.savingEllipsis') : t('admin2.saveConfig')}</button>
      </div>
    </div>
  );
}

function QuotaUsagePanel() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const fetchUsage = async () => {
    setLoading(true);
    setError('');
    try {
      const res = await fetch('/api/auth/me/usage', { credentials: 'same-origin' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
    } catch (ex) {
      setError(ex.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchUsage(); }, []);

  if (loading) return <p className="muted" style={{ margin: '10px 0' }}>{t('admin2.loadingQuota')}</p>;
  if (error) return <p className="muted" style={{ margin: '10px 0' }}>{t('admin2.loadFailed')}{error} <button className="ghost compact" type="button" onClick={fetchUsage}>{t('common.retry')}</button></p>;
  if (!data?.resources?.length) return <p className="muted" style={{ margin: '10px 0' }}>{t('admin2.noQuotaData')}</p>;

  return (
    <div className="quota-usage-panel">
      <div className="quota-usage-head">
        <div>
          <h3>{t('admin2.currentQuota')}</h3>
          <p>{t('admin2.quotaResetNote')}</p>
        </div>
        <button className="ghost compact" type="button" onClick={fetchUsage} title={t('admin2.refreshQuota')}>
          <RotateCw size={13} /> {t('admin2.refresh')}
        </button>
      </div>
      <div className="quota-usage-grid">
        {data.resources.map((r) => {
          const pct = r.limit > 0 ? Math.min(100, Math.round((r.usage / r.limit) * 100)) : 0;
          const tone = r.limit >= 0 && r.usage >= r.limit ? 'is-exhausted' : pct >= 80 ? 'is-near' : 'is-ok';
          return (
            <div key={r.key} className={`quota-usage-card ${tone}`}>
              <span className="quota-usage-card-label">{r.label}</span>
              <strong className="quota-usage-card-value">
                {r.usage}
                <small>/{r.limit >= 0 ? r.limit : t('admin2.unlimited')}</small>
              </strong>
              {r.limit > 0 && (
                <div className="quota-usage-card-bar">
                  <i style={{ width: `${pct}%` }} />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function AdminPage({ initialTab, users, betaLottery, serverHealth, form, setForm, addUser, updateUser, deleteUser, refreshUsers, onBack, onOpenAiCosts, onLotteryAction, session, onLogout, error, routeMode, onRouteModeChange }) {

  const [activeTab, setActiveTab] = useState(initialTab || 'overview');
  const [userSearch, setUserSearch] = useState('');
  const [expandedUsers, setExpandedUsers] = useState(() => new Set());

  const summary = useMemo(() => {
    const total = users.length;
    const active = users.filter((user) => !user.expired).length;
    const admins = users.filter((user) => user.is_admin && !user.expired).length;
    const trade = users.filter((user) => user.can_trade && !user.expired).length;
    const quotaWarnings = users.filter((user) => quotaFields.some(([field, , usageKey]) => usageRatio(user, field, usageKey).over)).length;
    return { total, active, admins, trade, quotaWarnings };
  }, [users]);
  const quotaTotals = useMemo(() => ({
    dailyScans: aggregateQuota(users, 'max_daily_scans', 'daily_scans'),
    dailyAi: aggregateQuota(users, 'max_daily_ai_scans', 'daily_ai_scans'),
    dailyChat: aggregateQuota(users, 'max_daily_ai_chat', 'daily_ai_chat'),
    watchlists: aggregateQuota(users, 'max_watchlists', 'watchlists'),
    loops: aggregateQuota(users, 'max_scan_loop_instances', 'scan_loop_instances'),
    channels: aggregateQuota(users, 'max_notification_channels', 'notification_channels'),
    accounts: aggregateQuota(users, 'max_longbridge_accounts', 'longbridge_accounts'),
  }), [users]);

  const filteredUsers = useMemo(() => {
    const q = userSearch.trim().toLowerCase();
    if (!q) return users;
    return users.filter((user) => user.username.toLowerCase().includes(q));
  }, [users, userSearch]);

  const toggleUser = (username) => {
    setExpandedUsers((prev) => {
      const next = new Set(prev);
      if (next.has(username)) next.delete(username);
      else next.add(username);
      return next;
    });
  };
  const allExpanded = filteredUsers.length > 0 && filteredUsers.every((u) => expandedUsers.has(u.username));
  const toggleAll = () => {
    setExpandedUsers(() => (allExpanded ? new Set() : new Set(filteredUsers.map((u) => u.username))));
  };

  const tabs = [
    ['overview', window._t('admin.overview')],
    ['users', `${window._t('admin.users')} (${users.length})`],
    ['lottery', window._t('admin.lottery')],
    ['quota', window._t('nav.quota')],
  ];

  const lotteryStatus = betaLottery?.status || (betaLottery?.announced ? 'announced' : 'open');
  const lotteryMeta = LOTTERY_STATUS_META[lotteryStatus] || LOTTERY_STATUS_META.draft;

  return (
    <main className="shell guide-shell">
      <AdminPageHeader onOpenAiCosts={onOpenAiCosts} onRefresh={refreshUsers} />
      <section className="hero app-hero">
        <div className="hero-brand">
          <img className="site-logo" src="/logo.png" alt="AI Option Scanner" />
          <div>
            <div className="eyebrow">Admin · Access Control</div>
            <h1>{t("admin.title")}</h1>
            <p>{t('admin2.adminHeroDesc')}</p>
          </div>
        </div>
        <div className="hero-controls">
          <SessionAndRouteBar session={session} routeMode={routeMode} onRouteModeChange={onRouteModeChange} onLogout={onLogout} />
          <div className="hero-actions">
            <button className="ghost nav-action" type="button" onClick={onBack}>{t('admin2.backToScanner')}</button>
            <button className="ghost nav-action" type="button" onClick={onOpenAiCosts}>{t('admin2.aiCostCenter')}</button>
            <button className="ghost nav-action" type="button" onClick={refreshUsers}>{t('admin2.refresh')}</button>
          </div>
        </div>
      </section>
      {error && <div className="error">{error}</div>}

      <nav className="admin-tabs">
        {tabs.map(([key, label]) => (
          <button
            key={key}
            type="button"
            className={`admin-tab ${activeTab === key ? 'active' : ''}`}
            onClick={() => setActiveTab(key)}
          >
            {label}
          </button>
        ))}
      </nav>

      {activeTab === 'overview' && (
        <>
          <AdminHeroDashboard
            summary={summary}
            quotaTotals={quotaTotals}
            serverHealth={serverHealth}
            betaLottery={betaLottery}
            onOpenAiCosts={onOpenAiCosts}
            refreshUsers={refreshUsers}
          />
          <section className="admin-summary-grid">
            <div className="metric"><span>{t('admin2.totalUsers')}</span><strong>{summary.total}</strong></div>
            <div className="metric"><span>{t('admin2.activeUsers')}</span><strong>{summary.active}</strong></div>
            <div className="metric"><span>{t('admin2.admins')}</span><strong>{summary.admins}</strong></div>
            <div className="metric"><span>{t('admin2.tradePermission')}</span><strong>{summary.trade}</strong></div>
            <div className={`metric ${summary.quotaWarnings ? 'warning' : ''}`}><span>{t('admin2.quotaAlerts')}</span><strong>{summary.quotaWarnings}</strong></div>
          </section>
          <section className="guide-hero panel">
            <div>
              <span className="guide-step">{t('admin2.permissionAndQuota')}</span>
              <h2>{t('admin2.operationRules')}</h2>
              <p>{t('admin2.operationRulesDesc')}</p>
            </div>
            <div className="guide-prompt">
              <strong>{t('admin2.defaultQuota')}</strong>
              <code>{t('admin2.defaultQuotaLine1').replace('{scans}', form.max_daily_scans).replace('{ai}', form.max_daily_ai_scans).replace('{chat}', form.max_daily_ai_chat)}</code>
              <code>{t('admin2.defaultQuotaLine2').replace('{watchlists}', form.max_watchlists).replace('{radar}', form.max_scan_loop_instances).replace('{notify}', form.max_notification_channels)}</code>
            </div>
          </section>
          <ServerHealthPanel snapshot={serverHealth} onRefresh={refreshUsers} />
        </>
      )}

      {activeTab === 'users' && (
        <section className="panel admin-panel">
          <div className="admin-panel-head">
            <div>
              <h2>{t("admin.userMgmt")}</h2>
              <p>{t('admin2.userMgmtDesc')}</p>
            </div>
            <a className="ghost" href="https://open.longbridge.com/" target="_blank" rel="noreferrer">{t('admin2.lbOpenPlatform')}</a>
          </div>
          <div className="admin-user-controls">
            <label className="admin-user-search">
              <Search size={14} />
              <input
                type="search"
                placeholder={t('admin2.searchUserPlaceholder')}
                value={userSearch}
                onChange={(event) => setUserSearch(event.target.value)}
              />
            </label>
            <span className="admin-user-count">{filteredUsers.length} / {users.length}</span>
            <button className="ghost compact" type="button" onClick={toggleAll} disabled={!filteredUsers.length}>
              {allExpanded ? t('admin2.collapseAll') : t('admin2.expandAll')}
            </button>
          </div>
          <div className="uadmin-list">
            {filteredUsers.map((user) => (
              <UserRow
                key={user.username}
                user={user}
                updateUser={updateUser}
                deleteUser={deleteUser}
                expanded={expandedUsers.has(user.username)}
                onToggle={() => toggleUser(user.username)}
              />
            ))}
            {!filteredUsers.length && <p className="uadmin-empty">{users.length ? t('admin2.noMatchingUsers') : t('admin2.noUserRecords')}</p>}
          </div>
          <form className="account-form admin-create-form" onSubmit={addUser}>
            <div className="admin-form-head">
              <strong>{t("admin.addUser")}</strong>
              <span>{t('admin2.addUserHint')}</span>
            </div>
            <div className="two">
              <input placeholder={t('admin2.usernamePlaceholder')} value={form.username} onChange={(event) => setForm({ ...form, username: event.target.value.trim().toLowerCase() })} required />
              <input type="password" placeholder={t('admin2.initialPasswordPlaceholder')} value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} autoComplete="new-password" required />
            </div>
            <div className="admin-permission-grid">
              <Toggle checked={form.can_analyze} onChange={(checked) => setForm({ ...form, can_analyze: checked })} label={t('admin2.enableAnalyze')} />
              <Toggle checked={form.can_trade} onChange={(checked) => setForm({ ...form, can_trade: checked })} label={t('admin2.enableLive')} />
              <Toggle checked={form.is_admin} onChange={(checked) => setForm({ ...form, is_admin: checked })} label={t('admin2.setAsAdmin')} />
              <label className="form-row-inline">
                {t('admin2.remainingDaysLabel')}
                <input type="number" min="0" max="3650" step="0.25" value={form.remaining_days} onChange={(event) => setForm({ ...form, remaining_days: event.target.value })} required />
              </label>
            </div>
            <div className="admin-quota-grid">
              {quotaFields.map(([field, label]) => (
                <label className="quota-cell" key={field}>
                  <span>{t(label)}</span>
                  <input type="number" min="-1" max="1000000" step="1" value={form[field]} onChange={(event) => setForm({ ...form, [field]: Number(event.target.value || 0) })} />
                  <small>{limitLabel(form[field])}</small>
                </label>
              ))}
            </div>
            <button>{t("admin.addUser")}</button>
          </form>
        </section>
      )}

      {activeTab === 'lottery' && (
        <section className="panel admin-panel">
          <div className="admin-panel-head">
            <div>
              <h2>{t('admin2.betaLottery')}</h2>
              <p>
                {t('admin2.regPageLink')}<code>{window.location.origin}/beta-lottery</code>{t('admin2.lotterySlotsInfo').replace('{slots}', betaLottery?.slot_count ?? 15).replace('{days}', betaLottery?.user_valid_days ?? 7)}
                {t('admin2.regStartBj')}{' '}
                {formatBjDisplay(betaLottery?.registration_start_at_bj) || <span className="muted">{t('admin2.notSet')}</span>}
                {' '}· {t('admin2.drawTimeBj')}{' '}
                {formatBjDisplay(betaLottery?.announce_at_bj) || <span className="muted">{t('admin2.notSet')}</span>}
                {' '}
                <a href="#lottery-config" className="mini-edit-link">{t('admin2.editTimeSlots')}</a>
              </p>
            </div>
            <span className={`lottery-status-badge ${lotteryMeta.tone}`}>{t(lotteryMeta.label)}</span>
          </div>
          <LotteryConfigForm betaLottery={betaLottery} onLotteryAction={onLotteryAction} />
          <LotteryControlPanel betaLottery={betaLottery} onLotteryAction={onLotteryAction} refreshUsers={refreshUsers} />
          <div className="beta-lottery-meta">
            <span>{t('admin2.signups')} {betaLottery?.entry_count ?? 0}</span>
            <span>{t('admin2.winners')} {betaLottery?.winner_count ?? 0}</span>
            <span>{betaLottery?.announced ? t('admin2.drawn') : t('admin2.notDrawn')}</span>
          </div>
          <div className="beta-lottery-admin-grid">
            <div className="beta-lottery-list">
              <strong>{t('admin2.signupRecords')}</strong>
              {(betaLottery?.entries || []).slice(0, 40).map((entry) => (
                <div className={`beta-entry-row ${entry.selected ? 'selected' : ''}`} key={entry.id}>
                  <div>
                    <strong>{entry.nickname || t('admin2.groupMember')}</strong>
                    <span>{entry.contact || t('admin2.noContact')} · {entry.ip_address || '--'} · {entry.ip_location || t('admin2.unknown')}</span>
                    <small>{entry.user_agent || '--'}</small>
                  </div>
                  <div className="beta-entry-tags">
                    <span>{entry.route_mode || 'auto'}</span>
                    <span>{entry.selected ? t('admin2.won') : t('admin2.notWon')}</span>
                    {entry.fingerprint && <span title={t('admin2.deviceFingerprint')}>FP {entry.fingerprint.slice(0, 8)}</span>}
                    <CopyableId value={entry.entry_token} label="Token" compact />
                  </div>
                </div>
              ))}
              {!betaLottery?.entries?.length && <p className="muted">{t('admin2.noLotteryEntries')}</p>}
            </div>
            <div className="beta-lottery-list">
              <strong>{t('admin2.testAccounts')}</strong>
              {(betaLottery?.slots || []).map((slot) => (
                <div className="beta-slot-row" key={slot.slot_number}>
                  <div>
                    <strong>Slot {slot.slot_number}</strong>
                    <span>{slot.username}</span>
                    <small>{slot.assigned_entry_id ? `${t('admin2.assigned')} #${slot.assigned_entry_id}` : t('admin2.pendingAssign')}</small>
                  </div>
                  <div className="beta-slot-actions">
                    <CopyableId value={slot.username} label={t('admin2.account')} compact />
                    <CopyableId value={slot.password} label={t('admin2.password')} compact />
                  </div>
                </div>
              ))}
            </div>
          </div>
        </section>
      )}

      {activeTab === 'quota' && (
        <section className="panel admin-panel">
          <div className="admin-panel-head">
            <div>
              <h2>{t('admin2.currentQuota')}</h2>
              <p>{t('admin2.quotaResetNote')}</p>
            </div>
          </div>
          <QuotaUsagePanel />
        </section>
      )}
    </main>
  );
}

function AdminPageHeader({ onOpenAiCosts, onRefresh }) {
  const { embedded } = useAppShell();
  if (!embedded) return null;
  return (
    <PageHeader
      eyebrow="Admin · Access Control"
      title={t('admin2.adminConsole')}
      subtitle={t('admin2.adminConsoleSubtitle')}
      actions={
        <>
          <button className="ghost" type="button" onClick={onOpenAiCosts}>{t('admin2.aiCostCenter')}</button>
          <button className="ghost" type="button" onClick={onRefresh}>{t('admin2.refresh')}</button>
        </>
      }
    />
  );
}
