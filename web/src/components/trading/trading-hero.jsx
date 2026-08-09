import React from 'react';
import { MarketClock } from '../market-clock.jsx';
import { SessionAndRouteBar } from '../session-route-bar.jsx';
import { useAppShell } from '../app-shell/app-shell-context.js';
import { PageHeader } from '../app-shell/page-header.jsx';
import { t } from '../../i18n/index.js';

export function TradingHero({ clockTick, config, detailMode, marketClock, onBack, onLogout, onRouteModeChange, readiness, routeMode, session }) {
  const { embedded } = useAppShell();
  const brokerLabel = config.broker === 'alpaca' ? 'Alpaca' : (config.broker === 'usmart' ? 'uSMART' : 'Longbridge');

  if (embedded) {
    return (
      <PageHeader
        eyebrow={`Trade Console · ${brokerLabel}`}
        title={detailMode ? t('trading2.heroDetailTitle') : t('trading2.heroTitle')}
        subtitle={
          detailMode
            ? t('trading2.heroDetailSubtitle')
            : config.multi_instance_enabled
              ? t('trading2.heroMultiSubtitle')
              : t('trading2.heroSingleSubtitle').replace('{time}', config.run_time_et)
        }
        meta={
          <span className="context-bar-clock">
            <span className={`context-bar-dot ${config.live_enabled ? 'live' : 'idle'}`} aria-hidden />
            {config.live_enabled ? t('trading2.executionOn') : t('trading2.executionOff')}
          </span>
        }
        actions={
          detailMode ? <button className="ghost" type="button" onClick={onBack}>{t('trading2.backToLive')}</button> : null
        }
      />
    );
  }

  return (
<section className="hero">
  <div>
    <div className="eyebrow">Trade Instance Console · {brokerLabel}</div>
    <h1>{detailMode ? t('trading2.heroDetailTitle') : t('trading2.heroTitleFull')}</h1>
    <p>{detailMode ? t('trading2.heroDetailDesc') : config.multi_instance_enabled ? t('trading2.heroMultiDesc') : t('trading2.heroSingleDesc').replace('{time}', config.run_time_et)}</p>
  </div>
  <MarketClock clock={readiness?.market_clock || marketClock} tick={clockTick} />
  <div className="hero-card">
    <span><i className="pulse" /> {t('trading2.instanceExecution')}</span>
    <strong>{config.live_enabled ? t('trading2.onState') : t('trading2.offState')}</strong>
  </div>
  <SessionAndRouteBar session={session} routeMode={routeMode} onRouteModeChange={onRouteModeChange} onLogout={onLogout} />
  <button className="ghost nav-action" type="button" onClick={onBack}>{detailMode ? t('trading2.backToLive') : t('trading2.backToScanner')}</button>
</section>
  );
}
