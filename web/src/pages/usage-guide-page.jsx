import React from 'react';
import { SessionAndRouteBar } from '../components/session-route-bar.jsx';
import { t } from '../i18n/index.js';

export function UsageGuidePage({ onBack, onPresetGuide, session, onLogout, routeMode, onRouteModeChange }) {
  return (
    <main className="shell guide-shell">
      <section className="hero">
        <div className="hero-brand">
          <img className="site-logo" src="/logo.png" alt="AI Option Scanner" />
          <div>
            <div className="eyebrow">Welcome · AI Option Scanner</div>
            <h1>{t('site.guide.heroTitle')}</h1>
            <p>{t('site.guide.heroDesc')}</p>
          </div>
        </div>
        <SessionAndRouteBar session={session} routeMode={routeMode} onRouteModeChange={onRouteModeChange} onLogout={onLogout} />
        <button className="primary nav-action" type="button" onClick={onBack}>{t('site.guide.startAnalysis')}</button>
      </section>

      <section className="guide-hero panel">
        <div>
          <span className="guide-step">01</span>
          <h2>{t('site.guide.s1Title')}</h2>
          <p>{t('site.guide.s1Body')}</p>
        </div>
        <div className="guide-prompt">
          <strong>{t('site.guide.s1PromptLabel')}</strong>
          <code>{t('site.guide.s1Example1')}</code>
          <code>{t('site.guide.s1Example2')}</code>
        </div>
      </section>

      <section className="preset-guide-toolbar panel onboarding-toolbar">
        <div>
          <strong>{t('site.guide.tb1Title')}</strong>
          <span>{t('site.guide.tb1Body')}</span>
        </div>
        <div>
          <strong>{t('site.guide.tb2Title')}</strong>
          <span>{t('site.guide.tb2Body')}</span>
        </div>
        <div>
          <strong>{t('site.guide.tb3Title')}</strong>
          <span>{t('site.guide.tb3Body')}</span>
        </div>
      </section>

      <div className="guide-grid">
        <section className="panel guide-card">
          <span className="guide-step">02</span>
          <h2>{t('site.guide.c2Title')}</h2>
          <div className="guide-list">
            <p><strong>{t('site.guide.c2c1Label')}</strong>{t('site.guide.c2c1Body')}</p>
            <p><strong>Longbridge API</strong>{t('site.guide.c2c2Body')}</p>
            <p><strong>auto</strong>{t('site.guide.c2c3Body')}</p>
            <p><strong>{t('site.guide.c2c4Label')}</strong>{t('site.guide.c2c4Body')}</p>
          </div>
        </section>

        <section className="panel guide-card">
          <span className="guide-step">03</span>
          <h2>{t('site.guide.c3Title')}</h2>
          <div className="guide-list">
            <p><strong>{t('site.guide.c3c1Label')}</strong>{t('site.guide.c3c1Body')}</p>
            <p><strong>{t('site.guide.c3c2Label')}</strong>{t('site.guide.c3c2Body')}</p>
            <p><strong>{t('site.guide.c3c3Label')}</strong>{t('site.guide.c3c3Body')}</p>
            <p><strong>{t('site.guide.c3c4Label')}</strong>{t('site.guide.c3c4Body')}</p>
          </div>
        </section>

        <section className="panel guide-card">
          <span className="guide-step">04</span>
          <h2>{t('site.guide.c4Title')}</h2>
          <div className="guide-list">
            <p><strong>{t('site.guide.c4c1Label')}</strong>{t('site.guide.c4c1Body')}</p>
            <p><strong>{t('site.guide.c4c2Label')}</strong>{t('site.guide.c4c2Body')}</p>
            <p><strong>{t('site.guide.c4c3Label')}</strong>{t('site.guide.c4c3Body')}</p>
            <p><strong>{t('site.guide.c4c4Label')}</strong>{t('site.guide.c4c4Body')}</p>
          </div>
        </section>

        <section className="panel guide-card">
          <span className="guide-step">05</span>
          <h2>{t('site.guide.c5Title')}</h2>
          <div className="guide-list">
            <p>{t('site.guide.c5p1')}</p>
            <p>{t('site.guide.c5p2')}</p>
            <p>{t('site.guide.c5p3')}</p>
          </div>
        </section>

        <section className="panel guide-card">
          <span className="guide-step">06</span>
          <h2>{t('site.guide.c6Title')}</h2>
          <div className="guide-list">
            <p>{t('site.guide.c6p1')}</p>
            <p>{t('site.guide.c6p2')}</p>
            <button className="ghost compact guide-inline-action" type="button" onClick={onPresetGuide}>{t('site.guide.viewPresets')}</button>
          </div>
        </section>

        <section className="panel guide-card warning-card">
          <span className="guide-step">!</span>
          <h2>{t('site.guide.c7Title')}</h2>
          <div className="guide-list">
            <p>{t('site.guide.c7p1')}</p>
            <p>{t('site.guide.c7p2')}</p>
            <p>{t('site.guide.c7p3')}</p>
          </div>
        </section>
      </div>
    </main>
  );
}
