import React from 'react';
import { routeItems } from '../../config.js';
import { normalizeRouteMode } from '../../session.js';
import { NavIcon } from './nav-icons.jsx';

import { LocaleToggle } from '../../i18n/LocaleToggle.jsx';

const t = (p) => window._t(p);

const NAV_KEY_MAP = {
  '市场': 'nav.market', '交易': 'nav.trading', '运营': 'nav.ops', '帮助': 'nav.help',
  '扫描器': 'nav.scanner', '机会雷达': 'nav.watchlist', 'AI 对话': 'nav.chat',
  '实例与复盘': 'nav.tradingLog', '账户与连接': 'nav.accounts',
  '全自动交易': 'nav.autoTrade',
  'Wait Trigger': 'nav.waitTriggers', '通知中心': 'nav.notifications',
  '使用指南': 'nav.usageGuide', '额度': 'nav.quota',
};

export function AppShellSidebar({
  currentTab,
  onNavigate,
  navGroups,
  session,
  routeMode,
  onRouteModeChange,
  onLogout,
}) {
  const isAdmin = Boolean(session?.is_admin);

  const tx = (cn) => (NAV_KEY_MAP[cn] ? t(NAV_KEY_MAP[cn]) : cn);
  return (
    <aside className="app-shell-sidebar">
      <nav className="app-shell-nav">
        {navGroups.map((group) => (
          <React.Fragment key={group.label}>
            <div className="app-shell-nav-group">{tx(group.label)}</div>
            {group.items.map((item) => (
              <SidebarItem
                key={item.id}
                item={{ ...item, label: tx(item.label) }}
                active={item.id === currentTab}
                onClick={() => onNavigate(item)}
              />
            ))}
          </React.Fragment>
        ))}
      </nav>

      <div className="app-shell-footer">
        <div className="app-shell-account">
          <div className="app-shell-account-avatar">{(session?.username || '?').slice(0, 1).toUpperCase()}</div>
          <div className="app-shell-account-meta">
            <span title={session?.username || ''}>{session?.username || t('appshell.notLoggedIn')}</span>
            <small>{routeLabel(routeMode)}</small>
          </div>
          {isAdmin && (
            <button
              type="button"
              className="app-shell-account-admin"
              title={t('appshell.adminConsole')}
              aria-label={t('appshell.adminConsole')}
              onClick={() => onNavigate({ id: 'admin', path: '/admin' })}
            >
              <NavIcon id="admin" className="app-shell-admin-gear" />
            </button>
          )}
        </div>

        <div className="app-shell-route-pills">
          {routeItems.map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={`app-shell-route-pill ${routeMode === value ? 'active' : ''}`}
              onClick={() => onRouteModeChange(value)}
              title={label}
            >
              {label}
            </button>
          ))}
        </div>

        <LocaleToggle className="app-shell-locale-toggle" />

        <button type="button" className="app-shell-logout" onClick={onLogout}>
          {window._t('login.logout')}
        </button>
      </div>
    </aside>
  );
}

function SidebarItem({ item, active, onClick }) {
  return (
    <button
      type="button"
      className={`app-shell-nav-item ${active ? 'active' : ''}`}
      onClick={onClick}
    >
      <NavIcon id={item.iconId} />
      <span className="app-shell-nav-label">{item.label}</span>
      {item.badge ? <span className="app-shell-nav-badge">{item.badge}</span> : null}
    </button>
  );
}

function routeLabel(routeMode) {
  const match = routeItems.find(([value]) => value === normalizeRouteMode(routeMode));
  return match?.[1] || t('appshell.autoRoute');
}
