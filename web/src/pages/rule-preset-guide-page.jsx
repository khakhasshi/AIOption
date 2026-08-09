import React from 'react';
import { SessionAndRouteBar } from '../components/session-route-bar.jsx';
import { strategyModeItems } from '../config.js';
import { normalizeStrategyModes } from '../utils/trading-inputs.js';
import { t, getLocale } from '../i18n/index.js';

export function RulePresetGuidePage({ presets, onBack, onApplyPreset, session, onLogout, routeMode, onRouteModeChange }) {
  const strategyModeLabelMap = Object.fromEntries(strategyModeItems);
  const rows = Array.isArray(presets) ? presets : [];
  const isEn = String(getLocale()).startsWith('en');
  const presetLabel = (p) => (isEn && p.label_en) ? p.label_en : p.label;
  const presetDesc = (p) => (isEn && p.description_en) ? p.description_en : p.description;

  return (
    <main className="shell guide-shell">
      <section className="hero">
        <div className="hero-brand">
          <img className="site-logo" src="/logo.png" alt="AI Option Scanner" />
          <div>
            <div className="eyebrow">Rule Presets · Non-AI Playbook</div>
            <h1>{t('site.preset.heroTitle')}</h1>
            <p>{t('site.preset.heroDesc')}</p>
          </div>
        </div>
        <SessionAndRouteBar session={session} routeMode={routeMode} onRouteModeChange={onRouteModeChange} onLogout={onLogout} />
        <button className="ghost nav-action" type="button" onClick={onBack}>{t('site.preset.backToAnalyzer')}</button>
      </section>

      <section className="guide-hero panel preset-guide-hero">
        <div>
          <span className="guide-step">20</span>
          <h2>{t('site.preset.purposeTitle')}</h2>
          <p>{t('site.preset.purposeBody')}</p>
        </div>
        <div className="guide-prompt">
          <strong>{t('site.preset.howToLabel')}</strong>
          <code>{t('site.preset.howTo1')}</code>
          <code>{t('site.preset.howTo2')}</code>
        </div>
      </section>

      <section className="preset-guide-toolbar panel">
        <div>
          <strong>{t('site.preset.totalLabel')}</strong>
          <span>{t('site.preset.totalValue').replace('{n}', rows.length || 0)}</span>
        </div>
        <div>
          <strong>{t('site.preset.coverageLabel')}</strong>
          <span>{t('site.preset.coverageValue')}</span>
        </div>
        <div>
          <strong>{t('site.preset.scenarioLabel')}</strong>
          <span>{t('site.preset.scenarioValue')}</span>
        </div>
      </section>

      <div className="preset-guide-grid">
        {rows.map((preset, index) => {
          const modes = normalizeStrategyModes(preset.strategy_modes).map((mode) => strategyModeLabelMap[mode] || mode);
          // Aliases and query_template are Chinese matching keywords / AI-input
          // text (not display copy), so hide them in English mode.
          const aliases = isEn ? [] : (Array.isArray(preset.aliases) ? preset.aliases.slice(0, 6) : []);
          return (
            <article className="panel preset-guide-card" key={preset.key || preset.label || index}>
              <div className="preset-card-head">
                <span className="guide-step">{String(index + 1).padStart(2, '0')}</span>
                <div>
                  <h2>{presetLabel(preset) || preset.key || `${t('site.preset.presetFallback')} ${index + 1}`}</h2>
                  <p>{presetDesc(preset) || t('site.preset.descFallback')}</p>
                </div>
              </div>
              <div className="preset-meta">
                {modes.map((mode) => <span key={mode}>{mode}</span>)}
              </div>
              {aliases.length > 0 && (
                <div className="preset-aliases">
                  {aliases.map((alias) => <span key={alias}>{alias}</span>)}
                </div>
              )}
              {!isEn && <code className="preset-query">{preset.query_template || ''}</code>}
              <button className="ghost" type="button" onClick={() => onApplyPreset(preset)}>{t('site.preset.usePreset')}</button>
            </article>
          );
        })}
        {!rows.length && (
          <section className="panel preset-empty">
            <h2>{t('site.preset.emptyTitle')}</h2>
            <p>{t('site.preset.emptyBody')}</p>
          </section>
        )}
      </div>
    </main>
  );
}
