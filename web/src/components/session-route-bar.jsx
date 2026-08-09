import React from 'react';
import { routeItems } from '../config.js';
import { normalizeRouteMode } from '../session.js';
import { useAppShell } from './app-shell/app-shell-context.js';

export function SessionAndRouteBar({ session, routeMode, onRouteModeChange, onLogout }) {
  const { embedded } = useAppShell();
  if (embedded) return null;
  return (
    <div className="session-card session-route-card">
      <div className="session-route-meta">
        <span>{session?.username || '--'}</span>
        <small>{routeLabel(routeMode)}</small>
      </div>
      <div className="route-switcher">
        {routeItems.map(([value, label]) => (
          <button
            key={value}
            type="button"
            className={`route-pill compact ${routeMode === value ? 'active' : ''}`}
            onClick={() => onRouteModeChange(value)}
          >
            {label}
          </button>
        ))}
      </div>
      {session?.is_admin && <a className="ghost compact" href="/admin/ai-costs">{window._t('routebar.aiCosts')}</a>}
      <button className="ghost" type="button" onClick={onLogout}>{window._t('routebar.logout')}</button>
    </div>
  );
}

function routeLabel(routeMode) {
  const match = routeItems.find(([value]) => value === normalizeRouteMode(routeMode));
  return match?.[1] || window._t('routebar.autoRoute');
}
