import React from 'react';
import { t } from '../../i18n/index.js';

const ET_OPTIONS = { timeZone: 'America/New_York', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' };
const ET_DATE_OPTIONS = { timeZone: 'America/New_York', month: '2-digit', day: '2-digit' };

function formatEt(tick) {
  try {
    const d = tick instanceof Date ? tick : new Date(tick || Date.now());
    return `${new Intl.DateTimeFormat('en-US', ET_DATE_OPTIONS).format(d)} ${new Intl.DateTimeFormat('en-US', ET_OPTIONS).format(d)} ET`;
  } catch {
    return '';
  }
}

function statusFromClock(clock) {
  if (!clock) return { tone: 'idle', label: t('appshell.marketUnknown') };
  const phase = String(clock.phase || clock.session || '').toLowerCase();
  if (clock.is_open || phase === 'regular' || phase === 'rth') return { tone: 'live', label: t('appshell.statusRegular') };
  if (phase === 'pre' || phase === 'pre_market' || phase === 'premarket') return { tone: 'idle', label: t('appshell.statusPre') };
  if (phase === 'post' || phase === 'after_hours' || phase === 'aft') return { tone: 'idle', label: t('appshell.statusPost') };
  if (clock.next_open) return { tone: 'idle', label: t('appshell.marketClosed') };
  return { tone: 'idle', label: t('appshell.marketClosed') };
}

/**
 * Thin status strip pinned to the top of the main pane.
 * Shows ET clock + market state + optional chips supplied by the page.
 */
export function ContextBar({ marketClock, clockTick, chips }) {
  const status = statusFromClock(marketClock);
  return (
    <div className="context-bar">
      <span className={`context-bar-dot ${status.tone}`} aria-hidden />
      <span className="context-bar-clock">
        {formatEt(clockTick)}
        <small>· {status.label}</small>
      </span>
      <span className="context-bar-spacer" />
      {Array.isArray(chips)
        ? chips.filter(Boolean).map((chip, idx) => (
            <span key={idx} className={`context-bar-chip ${chip.tone || ''}`}>{chip.label}</span>
          ))
        : null}
    </div>
  );
}
