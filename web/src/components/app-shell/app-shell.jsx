import React, { useMemo } from 'react';
import { AppShellContext } from './app-shell-context.js';
import { AppShellSidebar } from './sidebar.jsx';
import { ContextBar } from './context-bar.jsx';

// Sidebar IA: grouped, semantic.
const NAV_GROUPS = [
  {
    label: '市场',
    items: [
      { id: 'scanner', label: '扫描器', iconId: 'scanner', path: '/' },
      { id: 'watchlists', label: '机会雷达', iconId: 'watchlist', path: '/watchlists' },
      { id: 'chat', label: 'AI 对话', iconId: 'chat', path: '/chat' },
    ],
  },
  {
    label: '交易',
    items: [
      { id: 'trading', label: '实例与复盘', iconId: 'trading', path: null, openVia: 'page', requires: 'trade' },
      { id: 'auto-trade', label: '全自动交易', iconId: 'autoTrade', path: '/auto-trade', requires: 'trade' },
      { id: 'accounts', label: '账户与连接', iconId: 'accounts', path: '/accounts' },
    ],
  },
  {
    label: '运营',
    items: [
      { id: 'wait-triggers', label: 'Wait Trigger', iconId: 'waitTrigger', path: '/wait-triggers' },
      { id: 'notifications', label: '通知中心', iconId: 'notifications', path: '/notifications' },
    ],
  },
  {
    label: '帮助',
    items: [
      { id: 'usage', label: '额度', iconId: 'gauge', path: '/quota' },
      { id: 'guide', label: '使用指南', iconId: 'guide', path: '/guide' },
    ],
  },
];

export function AppShell({
  currentTab,
  onNavigate,
  session,
  routeMode,
  onRouteModeChange,
  onLogout,
  marketClock,
  clockTick,
  contextChips,
  children,
}) {
  const isAdmin = Boolean(session?.is_admin);
  const canTrade = Boolean(session?.can_trade);

  const navGroups = useMemo(
    () =>
      NAV_GROUPS.map((group) => ({
        ...group,
        items: group.items.filter((it) => {
          if (it.requires === 'admin') return isAdmin;
          if (it.requires === 'trade') return canTrade;
          return true;
        }),
      })).filter((group) => group.items.length > 0),
    [isAdmin, canTrade],
  );

  const ctx = useMemo(
    () => ({ embedded: true, currentTab, onNavigate }),
    [currentTab, onNavigate],
  );

  return (
    <AppShellContext.Provider value={ctx}>
      <div className="app-shell">
        <AppShellSidebar
          currentTab={currentTab}
          onNavigate={onNavigate}
          navGroups={navGroups}
          session={session}
          routeMode={routeMode}
          onRouteModeChange={onRouteModeChange}
          onLogout={onLogout}
        />
        <div className="app-shell-main">
          <ContextBar marketClock={marketClock} clockTick={clockTick} chips={contextChips} />
          {children}
        </div>
      </div>
    </AppShellContext.Provider>
  );
}
