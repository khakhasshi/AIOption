import React from 'react';
import { SessionAndRouteBar } from '../components/session-route-bar.jsx';
import { t } from '../i18n/index.js';

export function TradePermissionPage({ onBack, session, onLogout, routeMode, onRouteModeChange }) {

  return (
    <main className="shell guide-shell">
      <section className="hero">
        <div className="hero-brand">
          <img className="site-logo" src="/logo.png" alt="AI Option Scanner" />
          <div>
            <div className="eyebrow">Permission · Trade Instance</div>
            <h1>{t('admin2.tradePermClosedTitle')}</h1>
            <p>{t('admin2.tradePermClosedDesc')}</p>
          </div>
        </div>
        <SessionAndRouteBar session={session} routeMode={routeMode} onRouteModeChange={onRouteModeChange} onLogout={onLogout} />
        <button className="ghost nav-action" type="button" onClick={onBack}>{t('admin2.backToScanner')}</button>
      </section>
      <section className="guide-hero panel">
        <div>
          <span className="guide-step">{t('admin2.permission')}</span>
          <h2>{t('admin2.fullScannerStillAvailable')}</h2>
          <p>{t('admin2.fullScannerDesc')}</p>
        </div>
        <div className="guide-prompt">
          <strong>{t('admin2.currentlyAvailable')}</strong>
          <code>{t('admin2.availableFeatures')}</code>
          <strong>{t('admin2.requiresAuth')}</strong>
          <code>{t('admin2.authFeatures')}</code>
        </div>
      </section>
    </main>
  );
}

export function AdminPermissionPage({ onBack, session, onLogout, routeMode, onRouteModeChange }) {
  return (
    <main className="shell guide-shell">
      <section className="hero">
        <div className="hero-brand">
          <img className="site-logo" src="/logo.png" alt="AI Option Scanner" />
          <div>
            <div className="eyebrow">Admin · Permission Required</div>
            <h1>{t("misc.noPermission")}</h1>
            <p>{t('admin2.adminPermDesc')}</p>
          </div>
        </div>
        <SessionAndRouteBar session={session} routeMode={routeMode} onRouteModeChange={onRouteModeChange} onLogout={onLogout} />
        <button className="ghost nav-action" type="button" onClick={onBack}>{t('admin2.backToScanner')}</button>
      </section>
    </main>
  );
}
