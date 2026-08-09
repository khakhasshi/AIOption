import React, { Suspense, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';
import { createApiClient } from './api.js';
import { LoginPage } from './pages/access-pages.jsx';
import { AppShell } from './components/app-shell/app-shell.jsx';
import { ErrorBoundary } from './components/error-boundary.jsx';
import { useScannerController } from './hooks/use-scanner-controller.js';
import { useVisibilityInterval } from './hooks/use-visibility-interval.js';
import {
  emptyAccount,
  emptyAuthUserForm,
  emptyProvider,
  emptyUserProvider,
} from './config.js';
import {
  clearRouteCookie,
  clearSessionCache,
  getClientUserId,
  getRouteMode,
  hasSeenOnboardingGuide,
  loadSessionCache,
  markOnboardingGuideSeen,
  normalizeRouteMode,
  saveSessionCache,
  setRouteCookie,
} from './session.js';
import { userErrorLabel } from './utils/display.js';
import { lazyWithRetry, installChunkErrorRecovery } from './utils/lazy-with-retry.js';
import './i18n/index.js';

// Lazy-loaded route components. Each lazy() spawns a separate Vite chunk so
// the first paint only ships LoginPage + shell, not the entire app graph.
// lazyWithRetry() retries the dynamic import and force-reloads on a stale-chunk
// 404 after a deploy, so users don't get stuck on a "Failed to fetch module".
const AdminPage = lazyWithRetry(() => import('./pages/access-pages.jsx').then((m) => ({ default: m.AdminPage })));
const AdminAiCostPage = lazyWithRetry(() => import('./pages/access-pages.jsx').then((m) => ({ default: m.AdminAiCostPage })));
const AdminPermissionPage = lazyWithRetry(() => import('./pages/access-pages.jsx').then((m) => ({ default: m.AdminPermissionPage })));
const BetaLotteryPage = lazyWithRetry(() => import('./pages/access-pages.jsx').then((m) => ({ default: m.BetaLotteryPage })));
const RulePresetGuidePage = lazyWithRetry(() => import('./pages/access-pages.jsx').then((m) => ({ default: m.RulePresetGuidePage })));
const TradePermissionPage = lazyWithRetry(() => import('./pages/access-pages.jsx').then((m) => ({ default: m.TradePermissionPage })));
const QuotaPage = lazyWithRetry(() => import('./pages/quota-page.jsx').then((m) => ({ default: m.QuotaPage })));
const UsageGuidePage = lazyWithRetry(() => import('./pages/access-pages.jsx').then((m) => ({ default: m.UsageGuidePage })));
const ScannerPage = lazyWithRetry(() => import('./pages/scanner-page.jsx').then((m) => ({ default: m.ScannerPage })));
const NotificationCenterPage = lazyWithRetry(() => import('./pages/notification-center-page.jsx').then((m) => ({ default: m.NotificationCenterPage })));
const NotificationGuidePage = lazyWithRetry(() => import('./pages/notification-guide-page.jsx').then((m) => ({ default: m.NotificationGuidePage })));
const OpportunityDetailPage = lazyWithRetry(() => import('./pages/opportunity-detail-page.jsx').then((m) => ({ default: m.OpportunityDetailPage })));
const ProductSitePage = lazyWithRetry(() => import('./pages/product-site-page.jsx').then((m) => ({ default: m.ProductSitePage })));
const TradingPage = lazyWithRetry(() => import('./pages/trading-page.jsx').then((m) => ({ default: m.TradingPage })));
const AutoTradePage = lazyWithRetry(() => import('./pages/auto-trade-page.jsx').then((m) => ({ default: m.AutoTradePage })));
const WatchlistPage = lazyWithRetry(() => import('./pages/watchlist-page.jsx').then((m) => ({ default: m.WatchlistPage })));
const AccountsPage = lazyWithRetry(() => import('./pages/accounts-page.jsx').then((m) => ({ default: m.AccountsPage })));
const WaitTriggersPage = lazyWithRetry(() => import('./pages/wait-triggers-page.jsx').then((m) => ({ default: m.WaitTriggersPage })));
const ChatPage = lazyWithRetry(() => import('./pages/chat-page.jsx').then((m) => ({ default: m.ChatPage })));

function RouteFallback() {
  return <div className="route-suspense-fallback">{window._t("common.loading")}</div>;
}

const clientUserId = getClientUserId();
const { api, authApi } = createApiClient({
  clientUserId,
  onAuthExpired: () => window.dispatchEvent(new CustomEvent('ai-option-auth-expired')),
  formatError: userErrorLabel,
});

function App() {
  const [page, setPage] = useState('scanner');
  const [providers, setProviders] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [auth, setAuth] = useState(null);
  const [routeMode, setRouteMode] = useState(getRouteMode());
  const [providerForm, setProviderForm] = useState(emptyProvider);
  const [userProviderForm, setUserProviderForm] = useState(emptyUserProvider);
  const [accountForm, setAccountForm] = useState(emptyAccount);
  const [authLoading, setAuthLoading] = useState(false);
  const [authUsers, setAuthUsers] = useState([]);
  const [betaLotteryAdmin, setBetaLotteryAdmin] = useState(null);
  const [serverHealth, setServerHealth] = useState(null);
  const [authUserForm, setAuthUserForm] = useState(emptyAuthUserForm);
  const [accountPanelOpen, setAccountPanelOpen] = useState(false);
  const [providerPanelOpen, setProviderPanelOpen] = useState(false);
  const [userProviderPanelOpen, setUserProviderPanelOpen] = useState(false);
  const [marketClock, setMarketClock] = useState(null);
  const [clockTick, setClockTick] = useState(Date.now());
  const [oauthConfig, setOauthConfig] = useState({ enabled: false, providers: [] });
  const [turnstileConfig, setTurnstileConfig] = useState({ enabled: false });
  const [session, setSession] = useState(() => loadSessionCache() ?? { loading: true, authenticated: false, username: '', can_analyze: false, can_trade: false, is_admin: false, broker_api_enabled: false, limits: {} });
  const [routePath, setRoutePath] = useState(window.location.pathname);

  const scanner = useScannerController({ api, providers, session });
  const {
    activeScan,
    aiProvider,
    analysisModules,
    analysisPresets,
    applyAnalysisPreset,
    candidateSort,
    council,
    createWaitTriggerFromScan,
    deleteTrigger,
    error,
    loading,
    longbridgeAccount,
    marketDataSource,
    markHistory,
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
    scanHistoryStarredOnly,
    scanTriggers,
    scannerResultTab,
    setAiProvider,
    setAnalysisModules,
    setCandidateSort,
    setCouncil,
    setError,
    setLongbridgeAccount,
    setMarketDataSource,
    setQuery,
    setQuickPromptsExpanded,
    setScannerResultTab,
    setScanHistoryStarredOnly,
    setStrategyModes,
    setSymbol,
    setUseAi,
    strategyModes,
    symbol,
    toggleStrategyMode,
    toggleTrigger,
    testTrigger,
    testingTriggerId,
    triggerTestResults,
    useAi,
  } = scanner;

  useEffect(() => {
    refreshAppSession();
    const onExpired = () => {
      clearSessionCache();
      setSession({ loading: false, authenticated: false, username: '', can_analyze: false, can_trade: false, is_admin: false, limits: {} });
    };
    window.addEventListener('ai-option-auth-expired', onExpired);
    return () => window.removeEventListener('ai-option-auth-expired', onExpired);
  }, []);

  useEffect(() => {
    const onPopState = () => setRoutePath(window.location.pathname);
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, []);

  useEffect(() => {
    // Public, unauthenticated: which Sign-in-with buttons to render. Failures
    // leave OAuth disabled so the password form still works.
    authApi('/api/auth/oauth/config')
      .then((row) => setOauthConfig({ enabled: Boolean(row?.enabled), providers: Array.isArray(row?.providers) ? row.providers : [] }))
      .catch(() => setOauthConfig({ enabled: false, providers: [] }));
  }, []);

  useEffect(() => {
    // Public, unauthenticated: whether the Cloudflare Turnstile captcha guards
    // login. Failures leave it disabled so login is never blocked by a config
    // fetch error.
    authApi('/api/auth/turnstile/config')
      .then((row) => setTurnstileConfig({ enabled: Boolean(row?.enabled), site_key: row?.site_key || '' }))
      .catch(() => setTurnstileConfig({ enabled: false }));
  }, []);

  useEffect(() => {
    if (!session.authenticated) return;
    refreshProviders();
    refreshAnalysisPresets();
    if (session.can_trade && session.broker_api_enabled) {
      refreshAccounts();
    } else {
      setAccounts([]);
      setAuth(null);
      setLongbridgeAccount('');
      if (marketDataSource === 'longbridge') setMarketDataSource('yfinance');
    }
    if (session.is_admin) refreshAdminPanels();
    refreshHistory();
    refreshTriggers();
    refreshMarketClock();
  }, [session.authenticated, session.can_trade, session.is_admin, session.broker_api_enabled]);

  useEffect(() => {
    if (!session.authenticated) return;
    syncRouteState(routeMode, { refreshSession: false });
  }, [routeMode, session.authenticated]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setClockTick(Date.now());
    }, 1000);
    return () => window.clearInterval(timer);
  }, []);

  useVisibilityInterval(refreshMarketClock, 60000, { enabled: session.authenticated });

  // Warm up the lazy route chunks shortly after login so switching tabs feels
  // instant instead of waiting on a network round-trip for each chunk. Runs
  // during browser idle time to avoid competing with the first paint.
  useEffect(() => {
    if (!session.authenticated) return undefined;
    const prefetch = () => {
      const loaders = [
        () => import('./pages/scanner-page.jsx'),
        () => import('./pages/watchlist-page.jsx'),
        () => import('./pages/chat-page.jsx'),
        () => import('./pages/accounts-page.jsx'),
        () => import('./pages/wait-triggers-page.jsx'),
        () => import('./pages/notification-center-page.jsx'),
      ];
      if (session.can_trade) loaders.push(() => import('./pages/trading-page.jsx'));
      if (session.can_trade) loaders.push(() => import('./pages/auto-trade-page.jsx'));
      loaders.forEach((load) => { load().catch(() => {}); });
    };
    const ric = window.requestIdleCallback;
    if (typeof ric === 'function') {
      const handle = ric(prefetch, { timeout: 3000 });
      return () => { try { window.cancelIdleCallback?.(handle); } catch {} };
    }
    const timer = window.setTimeout(prefetch, 1200);
    return () => window.clearTimeout(timer);
  }, [session.authenticated, session.can_trade]);

  useEffect(() => {
    if (!session.authenticated || !session.can_trade || !session.broker_api_enabled) return;
    refreshAuth(longbridgeAccount);
  }, [longbridgeAccount, session.authenticated, session.can_trade, session.broker_api_enabled]);

  async function refreshAppSession() {
    try {
      const row = await authApi('/api/auth/me');
      const next = {
        loading: false,
        authenticated: Boolean(row.authenticated),
        username: row.username || '',
        can_analyze: Boolean(row.can_analyze),
        can_trade: Boolean(row.can_trade),
        is_admin: Boolean(row.is_admin),
        broker_api_enabled: Boolean(row.broker_api_enabled),
        limits: row.limits || {},
      };
      saveSessionCache(next);
      setSession(next);
    } catch {
      setBetaLotteryAdmin(null);
      clearSessionCache();
      setSession({ loading: false, authenticated: false, username: '', can_analyze: false, can_trade: false, is_admin: false, limits: {} });
    }
  }

  async function loginApp(username, password, acceptedTerms, turnstileToken) {
    const row = await authApi('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password, accepted_terms: Boolean(acceptedTerms), route_mode: routeMode, turnstile_token: turnstileToken || null }),
    });
    const loginUsername = row.username || username;
    syncRouteState(routeMode, { refreshSession: false });
    const next = {
      loading: false,
      authenticated: Boolean(row.authenticated),
      username: loginUsername,
      can_analyze: Boolean(row.can_analyze),
      can_trade: Boolean(row.can_trade),
      is_admin: Boolean(row.is_admin),
      broker_api_enabled: Boolean(row.broker_api_enabled),
      limits: row.limits || {},
    };
    saveSessionCache(next);
    setSession(next);
    if (row.authenticated && !hasSeenOnboardingGuide(loginUsername)) {
      markOnboardingGuideSeen(loginUsername);
      window.history.pushState({}, '', '/guide');
      setRoutePath('/guide');
      setPage('scanner');
    }
  }

  async function oauthLoginApp(provider, credential, nonce, acceptedTerms, turnstileToken) {
    const row = await authApi('/api/auth/oauth/login', {
      method: 'POST',
      body: JSON.stringify({ provider, credential, nonce, accepted_terms: Boolean(acceptedTerms), route_mode: routeMode, turnstile_token: turnstileToken || null }),
    });
    const loginUsername = row.username || '';
    syncRouteState(routeMode, { refreshSession: false });
    const next = {
      loading: false,
      authenticated: Boolean(row.authenticated),
      username: loginUsername,
      can_analyze: Boolean(row.can_analyze),
      can_trade: Boolean(row.can_trade),
      is_admin: Boolean(row.is_admin),
      broker_api_enabled: Boolean(row.broker_api_enabled),
      limits: row.limits || {},
    };
    saveSessionCache(next);
    setSession(next);
    if (row.authenticated && !hasSeenOnboardingGuide(loginUsername)) {
      markOnboardingGuideSeen(loginUsername);
      window.history.pushState({}, '', '/guide');
      setRoutePath('/guide');
      setPage('scanner');
    }
  }

  async function logoutApp() {
    await authApi('/api/auth/logout', { method: 'POST' }).catch(() => null);
    clearSessionCache();
    setRouteMode('auto');
    clearRouteCookie();
    setBetaLotteryAdmin(null);
    setSession({ loading: false, authenticated: false, username: '', can_analyze: false, can_trade: false, is_admin: false, limits: {} });
  }

  async function changeRouteMode(nextRouteMode) {
    const cleaned = normalizeRouteMode(nextRouteMode);
    setRouteMode(cleaned);
    syncRouteState(cleaned, { refreshSession: session.authenticated });
  }

  async function syncRouteState(nextRouteMode, { refreshSession = true } = {}) {
    setRouteCookie(nextRouteMode);
    if (!refreshSession) return;
    try {
      const row = await authApi('/api/auth/me');
      setSession((current) => ({
        ...current,
        loading: false,
        authenticated: Boolean(row.authenticated),
        username: row.username || current.username,
        can_analyze: Boolean(row.can_analyze),
        can_trade: Boolean(row.can_trade),
        is_admin: Boolean(row.is_admin),
        broker_api_enabled: Boolean(row.broker_api_enabled),
        limits: row.limits || {},
      }));
      if (session.authenticated) {
        await Promise.allSettled([refreshProviders(), refreshHistory(), refreshMarketClock()]);
        if (row.can_trade && row.broker_api_enabled) await refreshAccounts();
        if (session.is_admin) await refreshAdminPanels();
      }
    } catch {
      // keep the current UI state; the next request will fail if the route is invalid
    }
  }

  async function refreshProviders() {
    const rows = await api('/api/providers');
    setProviders(rows);
    const selected = rows.find((item) => item.name === aiProvider);
    const firstConfigured = rows.find((item) => item.configured !== false);
    if (!selected && rows[0]) {
      setAiProvider((firstConfigured || rows[0]).name);
      return;
    }
    if (selected?.configured === false && firstConfigured) {
      setAiProvider(firstConfigured.name);
    }
  }

  async function refreshAuthUsers() {
    const rows = await api('/api/auth/users');
    setAuthUsers(rows);
  }

  async function refreshBetaLotteryAdmin() {
    try {
      const row = await api('/api/beta-lottery/admin');
      setBetaLotteryAdmin(row);
    } catch {
      setBetaLotteryAdmin(null);
    }
  }

  async function betaLotteryAction(action, extra = {}) {
    try {
      const row = await api('/api/beta-lottery/admin/action', { method: 'POST', body: JSON.stringify({ action, ...extra }) });
      setBetaLotteryAdmin(row);
    } catch (err) {
      window.alert(`${window._t('appmain.actionFailed')}${err.message || err}`);
    }
  }

  async function refreshServerHealth() {
    try {
      const row = await api('/api/admin/server-health');
      setServerHealth(row);
    } catch {
      setServerHealth(null);
    }
  }

  async function refreshAdminPanels() {
    await Promise.allSettled([refreshAuthUsers(), refreshBetaLotteryAdmin(), refreshServerHealth()]);
  }

  async function refreshAccounts() {
    const rows = await api('/api/longbridge/accounts');
    setAccounts(rows);
    if (!rows.some((item) => item.name === longbridgeAccount)) {
      const defaultAccount = rows.find((item) => item.sdk_credentials_configured) || rows.find((item) => item.is_default) || rows[0];
      setLongbridgeAccount(defaultAccount?.name || '');
    }
  }

  async function refreshAuth(accountName = longbridgeAccount) {
    if (!accountName || !session.can_trade) {
      setAuth(null);
      return;
    }
    const state = await api(`/api/longbridge/status?account=${encodeURIComponent(accountName)}`);
    setAuth(state);
    await refreshAccounts();
  }

  async function forceRefreshAuth() {
    if (!longbridgeAccount || !session.can_trade) return;
    setAuthLoading(true);
    setError('');
    try {
      const state = await api(`/api/longbridge/status?account=${encodeURIComponent(longbridgeAccount)}&force=true`);
      setAuth(state);
      await refreshAccounts();
    } catch (authError) {
      setError(authError.message);
    } finally {
      setAuthLoading(false);
    }
  }

  async function refreshMarketClock() {
    try {
      const row = await api('/api/market-clock');
      setMarketClock(row);
    } catch {
      setMarketClock((current) => current);
    }
  }

  async function addProvider(event) {
    event.preventDefault();
    await api('/api/providers', {
      method: 'POST',
      body: JSON.stringify(providerForm),
    });
    setProviderForm(emptyProvider);
    await refreshProviders();
  }

  async function addUserProvider(event) {
    event.preventDefault();
    setError('');
    try {
      await api('/api/user-providers', {
        method: 'POST',
        body: JSON.stringify(userProviderForm),
      });
      const nextName = `user:${String(userProviderForm.name || '').trim().toLowerCase()}`;
      setUserProviderForm(emptyUserProvider);
      await refreshProviders();
      if (nextName !== 'user:') setAiProvider(nextName);
    } catch (providerError) {
      setError(providerError.message);
    }
  }

  async function addLongbridgeAccount(event) {
    event.preventDefault();
    try {
      const rows = await api('/api/longbridge/accounts', {
        method: 'POST',
        body: JSON.stringify({
          name: accountForm.name,
          label: accountForm.label || null,
          app_key: accountForm.app_key,
          app_secret: accountForm.app_secret,
          access_token: accountForm.access_token,
          set_default: accountForm.set_default,
        }),
      });
      setAccounts(rows);
      const created = rows.find((item) => item.label === accountForm.label || item.label === accountForm.name.trim().toLowerCase()) || rows.at(-1);
      setLongbridgeAccount(created?.name || '');
      setAccountForm(emptyAccount);
    } catch (accountError) {
      setError(accountError.message);
    }
  }

  async function deleteLongbridgeAccount(name) {
    const rows = await api(`/api/longbridge/accounts/${encodeURIComponent(name)}`, { method: 'DELETE' });
    setAccounts(rows);
    const defaultAccount = rows.find((item) => item.is_default) || rows[0];
    setLongbridgeAccount(defaultAccount?.name || '');
  }

  async function setDefaultLongbridgeAccount(name) {
    const rows = await api(`/api/longbridge/accounts/${encodeURIComponent(name)}/default`, { method: 'POST' });
    setAccounts(rows);
    setLongbridgeAccount(name);
  }

  async function deleteProvider(name) {
    await api(`/api/providers/${encodeURIComponent(name)}`, { method: 'DELETE' });
    await refreshProviders();
  }

  async function deleteUserProvider(name) {
    const rawName = String(name || '').replace(/^user:/, '');
    await api(`/api/user-providers/${encodeURIComponent(rawName)}`, { method: 'DELETE' });
    if (aiProvider === name) setAiProvider('deepseek');
    await refreshProviders();
  }

  async function addAuthUser(event) {
    event.preventDefault();
    setError('');
    try {
      const rows = await api('/api/auth/users', {
        method: 'POST',
        body: JSON.stringify(authUserForm),
      });
      setAuthUsers(rows);
      setAuthUserForm(emptyAuthUserForm);
    } catch (authUserError) {
      setError(authUserError.message);
    }
  }

  async function updateAuthUser(username, changes) {
    setError('');
    try {
      const rows = await api(`/api/auth/users/${encodeURIComponent(username)}`, {
        method: 'PATCH',
        body: JSON.stringify(changes),
      });
      setAuthUsers(rows);
    } catch (authUserError) {
      setError(authUserError.message);
    }
  }

  async function deleteAuthUser(username) {
    const confirmed = window.confirm(window._t('appmain.confirmDeleteUser').replace('{username}', username));
    if (!confirmed) return;
    setError('');
    try {
      const rows = await api(`/api/auth/users/${encodeURIComponent(username)}`, { method: 'DELETE' });
      setAuthUsers(rows);
    } catch (authUserError) {
      setError(authUserError.message);
    }
  }

  const canTrade = Boolean(session.can_trade);
  const canAdmin = Boolean(session.is_admin);
  const siteRoute = routePath === '/site' || routePath === '/product';
  const adminRoute = routePath === '/admin' || routePath.startsWith('/admin?');
  const adminAiCostRoute = routePath === '/admin/ai-costs' || routePath === '/ai-usage';
  const accountsRoute = routePath === '/accounts';
  const autoTradeRoute = routePath === '/auto-trade' || routePath.startsWith('/auto-trade/');
  const autoTradeDetailId = autoTradeRoute && routePath.startsWith('/auto-trade/')
    ? decodeURIComponent(routePath.slice('/auto-trade/'.length))
    : '';
  const waitTriggersRoute = routePath === '/wait-triggers';

  function navigateShellTab(item) {
    if (!item) return;
    switch (item.id) {
      case 'scanner':
        window.history.pushState({}, '', '/');
        setRoutePath('/');
        setPage('scanner');
        break;
      case 'trading':
        window.history.pushState({}, '', '/');
        setRoutePath('/');
        setPage('trading');
        break;
      case 'watchlists':
        window.history.pushState({}, '', '/watchlists');
        setRoutePath('/watchlists');
        break;
      case 'notifications':
        window.history.pushState({}, '', '/notifications');
        setRoutePath('/notifications');
        break;
      case 'accounts':
        window.history.pushState({}, '', '/accounts');
        setRoutePath('/accounts');
        break;
      case 'auto-trade':
        window.history.pushState({}, '', '/auto-trade');
        setRoutePath('/auto-trade');
        break;
      case 'wait-triggers':
        window.history.pushState({}, '', '/wait-triggers');
        setRoutePath('/wait-triggers');
        break;
      case 'chat':
        window.history.pushState({}, '', '/chat');
        setRoutePath('/chat');
        break;
      case 'ai-costs':
        window.history.pushState({}, '', '/admin/ai-costs');
        setRoutePath('/admin/ai-costs');
        break;
      case 'admin':
        window.history.pushState({}, '', '/admin');
        setRoutePath('/admin');
        break;
      case 'quota':
        window.history.pushState({}, '', '/admin?tab=quota');
        setRoutePath('/admin?tab=quota');
        break;
      case 'guide':
        window.history.pushState({}, '', '/guide');
        setRoutePath('/guide');
        break;
      case 'usage':
        window.history.pushState({}, '', '/quota');
        setRoutePath('/quota');
        break;
      default:
        if (item.path) {
          window.history.pushState({}, '', item.path);
          setRoutePath(item.path);
        }
    }
  }

  function renderInShell(currentTab, content) {
    return (
      <AppShell
        currentTab={currentTab}
        onNavigate={navigateShellTab}
        session={session}
        routeMode={routeMode}
        onRouteModeChange={changeRouteMode}
        onLogout={logoutApp}
        marketClock={marketClock}
        clockTick={clockTick}
      >
        {/* Per-page boundary: a crash in one page renders an inline error
            inside the shell (sidebar/nav stay usable) instead of white-screening
            the whole app via the single root boundary. key=currentTab resets
            the boundary when navigating to a different tab. */}
        <ErrorBoundary key={currentTab} onReset={() => setRoutePath(window.location.pathname)}>
          {content}
        </ErrorBoundary>
      </AppShell>
    );
  }
  const betaLotteryRoute = routePath === '/beta-lottery';
  const guideRoute = routePath === '/guide';
  const quotaRoute = routePath === '/quota';
  const presetGuideRoute = routePath === '/rule-presets';
  const watchlistsRoute = routePath === '/watchlists';
  const chatRoute = routePath === '/chat';
  const notificationGuideRoute = routePath === '/notifications/guide';
  const opportunityRouteId = routePath.match(/^\/opportunities\/([^/]+)$/)?.[1] || '';
  const instanceRouteId = routePath.match(/^\/trading\/instances\/([^/]+)$/)?.[1] || '';

  if (siteRoute) {
    return (
      <ProductSitePage
        onOpenApp={() => {
          window.history.pushState({}, '', '/');
          setRoutePath('/');
        }}
      />
    );
  }

  if (betaLotteryRoute) {
    return (
      <BetaLotteryPage
        onBack={openScannerPage}
        onLogin={() => {
          window.history.pushState({}, '', '/');
          setRoutePath('/');
        }}
        authApi={authApi}
        routeMode={routeMode}
        onRouteModeChange={changeRouteMode}
      />
    );
  }

  if (session.loading || !session.authenticated) {
    return <LoginPage loading={session.loading} onLogin={loginApp} onOAuthLogin={oauthLoginApp} oauthConfig={oauthConfig} turnstileConfig={turnstileConfig} routeMode={routeMode} onRouteModeChange={changeRouteMode} />;
  }

  if (adminRoute || adminAiCostRoute) {
    if (!canAdmin) {
      return <AdminPermissionPage onBack={openScannerPage} session={session} onLogout={logoutApp} routeMode={routeMode} onRouteModeChange={changeRouteMode} />;
    }
    if (adminAiCostRoute) {
      return renderInShell(
        'ai-costs',
        <AdminAiCostPage
          api={api}
          session={session}
          routeMode={routeMode}
          onRouteModeChange={changeRouteMode}
          onLogout={logoutApp}
          onBack={() => {
            window.history.pushState({}, '', '/admin');
            setRoutePath('/admin');
          }}
        />,
      );
    }
    const urlParams = new URLSearchParams(routePath.includes('?') ? routePath.slice(routePath.indexOf('?')) : '');
    const adminTab = urlParams.get('tab') || 'overview';
    return renderInShell(
      'admin',
      <AdminPage
        initialTab={adminTab}
        users={authUsers}
        betaLottery={betaLotteryAdmin}
        serverHealth={serverHealth}
        form={authUserForm}
        setForm={setAuthUserForm}
        addUser={addAuthUser}
        updateUser={updateAuthUser}
        deleteUser={deleteAuthUser}
        refreshUsers={refreshAdminPanels}
        onLotteryAction={betaLotteryAction}
        onBack={openScannerPage}
        onOpenAiCosts={() => {
          window.history.pushState({}, '', '/admin/ai-costs');
          setRoutePath('/admin/ai-costs');
        }}
        session={session}
        onLogout={logoutApp}
        error={error}
        routeMode={routeMode}
        onRouteModeChange={changeRouteMode}
      />,
    );
  }

  function openGuidePage() {
    window.history.pushState({}, '', '/guide');
    setRoutePath('/guide');
  }

  function openPresetGuidePage() {
    window.history.pushState({}, '', '/rule-presets');
    setRoutePath('/rule-presets');
  }

  function openBetaLotteryPage() {
    window.history.pushState({}, '', '/beta-lottery');
    setRoutePath('/beta-lottery');
  }

  function openWatchlistsPage() {
    window.history.pushState({}, '', '/watchlists');
    setRoutePath('/watchlists');
    setPage('scanner');
  }

  function openNotificationsPage() {
    window.history.pushState({}, '', '/notifications');
    setRoutePath('/notifications');
    setPage('scanner');
  }

  function openNotificationGuidePage() {
    window.history.pushState({}, '', '/notifications/guide');
    setRoutePath('/notifications/guide');
    setPage('scanner');
  }

  function openOpportunityDetailPage(opportunity) {
    const opportunityId = typeof opportunity === 'string' ? opportunity : opportunity?.id;
    if (!opportunityId) return;
    window.history.pushState({}, '', `/opportunities/${encodeURIComponent(opportunityId)}`);
    setRoutePath(`/opportunities/${encodeURIComponent(opportunityId)}`);
    setPage('scanner');
  }

  function openScannerPage() {
    window.history.pushState({}, '', '/');
    setRoutePath('/');
    setPage('scanner');
  }

  if (autoTradeRoute) {
    if (!canTrade) {
      return <TradePermissionPage onBack={openScannerPage} session={session} onLogout={logoutApp} routeMode={routeMode} onRouteModeChange={changeRouteMode} />;
    }
    return renderInShell(
      'auto-trade',
      <AutoTradePage
        api={api}
        providers={providers}
        accounts={accounts}
        fallbackProvider={aiProvider}
        fallbackAccount={longbridgeAccount}
        detailId={autoTradeDetailId}
        onOpenDetail={(id) => {
          const path = `/auto-trade/${encodeURIComponent(id)}`;
          window.history.pushState({}, '', path);
          setRoutePath(path);
        }}
        onBackToList={() => {
          window.history.pushState({}, '', '/auto-trade');
          setRoutePath('/auto-trade');
        }}
      />,
    );
  }

  if (accountsRoute) {
    return renderInShell(
      'accounts',
      <AccountsPage
        api={api}
        session={session}
        providers={providers}
        refreshProviders={refreshProviders}
        onNotifications={openNotificationsPage}
      />,
    );
  }

  if (waitTriggersRoute) {
    return renderInShell(
      'wait-triggers',
      <WaitTriggersPage
        api={api}
        session={session}
        onOpenNotifications={openNotificationsPage}
      />,
    );
  }

  if (chatRoute) {
    return renderInShell(
      'chat',
      <ChatPage
        api={api}
        clientUserId={clientUserId}
        session={session}
        providers={providers}
        accounts={accounts}
        routeMode={routeMode}
        onRouteModeChange={changeRouteMode}
        onLogout={logoutApp}
        marketClock={marketClock}
        clockTick={clockTick}
      />,
    );
  }

  if (watchlistsRoute) {
    return renderInShell(
      'watchlists',
      <WatchlistPage
        api={api}
        providers={providers}
        session={session}
        routeMode={routeMode}
        onRouteModeChange={changeRouteMode}
        onLogout={logoutApp}
        onBack={openScannerPage}
        marketClock={marketClock}
        clockTick={clockTick}
        triggers={scanTriggers}
        refreshTriggers={refreshTriggers}
        toggleTrigger={toggleTrigger}
        deleteTrigger={deleteTrigger}
        testTrigger={testTrigger}
        testingTriggerId={testingTriggerId}
        triggerTestResults={triggerTestResults}
        onNotifications={openNotificationsPage}
        onOpenOpportunityDetailPage={openOpportunityDetailPage}
      />,
    );
  }

  if (opportunityRouteId) {
    return (
      <OpportunityDetailPage
        api={api}
        opportunityId={opportunityRouteId}
        session={session}
        routeMode={routeMode}
        onRouteModeChange={changeRouteMode}
        onLogout={logoutApp}
        onBack={openWatchlistsPage}
        onGuide={openNotificationGuidePage}
        marketClock={marketClock}
        clockTick={clockTick}
      />
    );
  }

  if (notificationGuideRoute) {
    return (
      <NotificationGuidePage
        session={session}
        routeMode={routeMode}
        onRouteModeChange={changeRouteMode}
        onLogout={logoutApp}
        onBack={openWatchlistsPage}
        onNotifications={openNotificationsPage}
      />
    );
  }

  if (routePath === '/notifications') {
    return renderInShell(
      'notifications',
      <NotificationCenterPage
        api={api}
        session={session}
        routeMode={routeMode}
        onRouteModeChange={changeRouteMode}
        onLogout={logoutApp}
        onBack={openWatchlistsPage}
        onGuide={openNotificationGuidePage}
        marketClock={marketClock}
        clockTick={clockTick}
      />,
    );
  }

  if (instanceRouteId && !canTrade) {
    return <TradePermissionPage onBack={openScannerPage} session={session} onLogout={logoutApp} routeMode={routeMode} onRouteModeChange={changeRouteMode} />;
  }

  if (instanceRouteId) {
    return renderInShell(
      'trading',
      <TradingPage
        api={api}
        providers={providers}
        accounts={accounts}
        analysisPresets={analysisPresets}
        fallbackProvider={aiProvider}
        fallbackAccount={longbridgeAccount}
        marketClock={marketClock}
        clockTick={clockTick}
        onBack={() => {
          window.history.pushState({}, '', '/');
          setRoutePath('/');
          setPage('trading');
        }}
        session={session}
        onLogout={logoutApp}
        onPresetGuide={openPresetGuidePage}
        onBetaLottery={openBetaLotteryPage}
        detailRunId={decodeURIComponent(instanceRouteId)}
        detailMode
        routeMode={routeMode}
        onRouteModeChange={changeRouteMode}
      />,
    );
  }

  if (guideRoute) {
    return (
      <UsageGuidePage
        onBack={openScannerPage}
        onPresetGuide={openPresetGuidePage}
        session={session}
        onLogout={logoutApp}
        routeMode={routeMode}
        onRouteModeChange={changeRouteMode}
      />
    );
  }

  if (quotaRoute) {
    return renderInShell(
      'usage',
      <QuotaPage />,
    );
  }

  if (presetGuideRoute) {
    return (
      <RulePresetGuidePage
        presets={analysisPresets}
        onBack={openScannerPage}
        onApplyPreset={(preset) => {
          applyAnalysisPreset(preset);
          openScannerPage();
        }}
        onBetaLottery={openBetaLotteryPage}
        session={session}
        onLogout={logoutApp}
        routeMode={routeMode}
        onRouteModeChange={changeRouteMode}
      />
    );
  }

  if (page === 'trading') {
    if (!canTrade) {
      return <TradePermissionPage onBack={openScannerPage} session={session} onLogout={logoutApp} routeMode={routeMode} onRouteModeChange={changeRouteMode} />;
    }
    return renderInShell(
      'trading',
      <TradingPage
        api={api}
        providers={providers}
        accounts={accounts}
        analysisPresets={analysisPresets}
        fallbackProvider={aiProvider}
        fallbackAccount={longbridgeAccount}
        marketClock={marketClock}
        clockTick={clockTick}
        onBack={() => setPage('scanner')}
        session={session}
        onLogout={logoutApp}
        onPresetGuide={openPresetGuidePage}
        onBetaLottery={openBetaLotteryPage}
        routeMode={routeMode}
        onRouteModeChange={changeRouteMode}
      />,
    );
  }

  return renderInShell(
    'scanner',
    <ScannerPage
      accountForm={accountForm}
      accountPanelOpen={accountPanelOpen}
      accounts={accounts}
      activeScan={activeScan}
      addLongbridgeAccount={addLongbridgeAccount}
      addProvider={addProvider}
      addUserProvider={addUserProvider}
      aiProvider={aiProvider}
      analysisModules={analysisModules}
      analysisPresets={analysisPresets}
      applyAnalysisPreset={applyAnalysisPreset}
      auth={auth}
      authLoading={authLoading}
      candidateSort={candidateSort}
      changeRouteMode={changeRouteMode}
      clockTick={clockTick}
      council={council}
      deleteLongbridgeAccount={deleteLongbridgeAccount}
      deleteProvider={deleteProvider}
      deleteUserProvider={deleteUserProvider}
      error={error}
      forceRefreshAuth={forceRefreshAuth}
      loading={loading}
      longbridgeAccount={longbridgeAccount}
      logoutApp={logoutApp}
      marketClock={marketClock}
      marketDataSource={marketDataSource}
      markHistory={markHistory}
      onOpenBetaLottery={openBetaLotteryPage}
      onOpenGuide={openGuidePage}
      onOpenPresetGuide={openPresetGuidePage}
      onOpenTrading={() => setPage('trading')}
      onOpenWatchlists={openWatchlistsPage}
      openHistory={openHistory}
      providerForm={providerForm}
      providerPanelOpen={providerPanelOpen}
      providers={providers}
      query={query}
      quickPromptsExpanded={quickPromptsExpanded}
      refreshHistory={refreshHistory}
      refreshTriggers={refreshTriggers}
      result={result}
      routeMode={routeMode}
      runScan={runScan}
      scanHistory={scanHistory}
      scanHistoryHasNext={scanHistoryHasNext}
      scanHistoryPage={scanHistoryPage}
      scanHistoryStarredOnly={scanHistoryStarredOnly}
      scanTriggers={scanTriggers}
      scannerResultTab={scannerResultTab}
      session={session}
      setAccountForm={setAccountForm}
      setAccountPanelOpen={setAccountPanelOpen}
      setAiProvider={setAiProvider}
      setAnalysisModules={setAnalysisModules}
      setCandidateSort={setCandidateSort}
      setCouncil={setCouncil}
      setDefaultLongbridgeAccount={setDefaultLongbridgeAccount}
      setLongbridgeAccount={setLongbridgeAccount}
      setMarketDataSource={setMarketDataSource}
      setProviderForm={setProviderForm}
      setProviderPanelOpen={setProviderPanelOpen}
      setError={setError}
      setQuery={setQuery}
      setQuickPromptsExpanded={setQuickPromptsExpanded}
      setScannerResultTab={setScannerResultTab}
      setScanHistoryStarredOnly={setScanHistoryStarredOnly}
      setStrategyModes={setStrategyModes}
      setSymbol={setSymbol}
      setUseAi={setUseAi}
      setUserProviderForm={setUserProviderForm}
      setUserProviderPanelOpen={setUserProviderPanelOpen}
      strategyModes={strategyModes}
      symbol={symbol}
      toggleStrategyMode={toggleStrategyMode}
      createWaitTriggerFromScan={createWaitTriggerFromScan}
      toggleTrigger={toggleTrigger}
      deleteTrigger={deleteTrigger}
      testTrigger={testTrigger}
      testingTriggerId={testingTriggerId}
      triggerTestResults={triggerTestResults}
      useAi={useAi}
      userProviderForm={userProviderForm}
      userProviderPanelOpen={userProviderPanelOpen}
    />,
  );
}





createRoot(document.getElementById('root')).render(
  <ErrorBoundary>
    <Suspense fallback={<RouteFallback />}>
      <App />
    </Suspense>
  </ErrorBoundary>,
);

// Recover from stale-chunk failures that bypass React (Vite preload errors,
// unhandled dynamic-import rejections) by reloading once to fetch fresh assets.
installChunkErrorRecovery();
