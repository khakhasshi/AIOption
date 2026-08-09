import React, { useMemo } from 'react';
import { t } from '../i18n/index.js';

function sessionLabel(clock) {
  const state = clock?.session_state || clock?.market_state;
  return {
    regular_open: t('appshell.sessionRegularOpen'),
    premarket: t('appshell.sessionPremarket'),
    afterhours: t('appshell.sessionClosed'),
    closed_today: t('appshell.sessionClosed'),
    weekend: t('appshell.sessionWeekend'),
    holiday: t('appshell.sessionHoliday'),
  }[state] || (clock?.is_market_open_regular ? t('appshell.sessionRegularOpen') : t('appshell.marketClosed'));
}

function shortEtTime(value) {
  if (!value) return '--';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '--';
  return date.toLocaleString('zh-CN', {
    timeZone: 'America/New_York',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

export function MarketClock({ clock, tick }) {
  const current = useMemo(() => {
    if (!clock?.now_et) return new Date(tick);
    const base = new Date(clock.now_et).getTime();
    if (!Number.isFinite(base)) return new Date(tick);
    return new Date(base + (tick - base));
  }, [clock?.now_et, tick]);
  const time = current.toLocaleString('zh-CN', {
    timeZone: 'America/New_York',
    hour12: false,
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    timeZoneName: 'short',
  });
  const isTradingDay = clock?.is_trading_day;
  const nextLabel = clock?.is_market_open_regular
    ? `${t('appshell.nextClose')} ${shortEtTime(clock?.next_regular_close_at_et)}`
    : `${t('appshell.nextOpen')} ${shortEtTime(clock?.next_regular_open_at_et)}`;
  const sessionText = `${sessionLabel(clock)} · ${nextLabel}`;
  const detail = clock?.is_early_close
    ? `${sessionText} · ${t('appshell.earlyClose')} ${clock.early_close_reason || ''}`.trim()
    : sessionText;
  return (
    <div className={`market-clock ${isTradingDay ? 'open-day' : 'closed-day'}`}>
      <span>{t('appshell.easternTime')}</span>
      <strong>{time}</strong>
      <small>{isTradingDay ? detail : `${detail}${clock?.trading_day_reason ? ` · ${clock.trading_day_reason}` : ''}`}</small>
    </div>
  );
}
