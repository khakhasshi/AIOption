import React from 'react';
import { Send } from 'lucide-react';
import { t } from '../../i18n/index.js';
import { SectionTitle } from '../common.jsx';
import { gexRegimeLabel, gexWallLabel } from '../../utils/display.js';

function eventContextLine(event) {
  const payload = event?.payload || {};
  if (event?.source_type === 'scan_loop_digest') {
    const symbols = Array.isArray(payload.symbols) ? payload.symbols.join(', ') : '';
    return `${t('watchlist2.dailyDigest')} ${payload.count || 0} ${t('watchlist2.candidates')}${symbols ? ` · ${symbols}` : ''}`;
  }
  if (event?.source_type === 'scan_loop_report') {
    return `${t('watchlist2.radarReport')} ${t('watchlist2.scanned')} ${payload.scanned_count || 0} · ${t('watchlist2.prefiltered')} ${payload.matched_count || 0} · ${t('watchlist2.triggered')} ${payload.triggered_count || 0}`;
  }
  if (payload.symbol) return `${payload.symbol}${payload.alert_mode ? ` · ${payload.alert_mode}` : ''}`;
  return '';
}

function gexCheckText(check) {
  const field = String(check?.field || '');
  const actual = check?.actual;
  const expected = Array.isArray(check?.expected) ? check.expected.join('/') : check?.expected;
  if (field === 'gex.regime') return `GEX ${check.operator} ${gexRegimeLabel(check.expected)}, ${t('watchlist2.currentlyLabel')} ${gexRegimeLabel(actual)}`;
  if (field === 'gex.nearest_wall') return `Wall ${check.operator} ${gexWallLabel(check.expected)}, ${t('watchlist2.currentlyLabel')} ${gexWallLabel(actual)}`;
  if (field === 'gex.nearest_wall_distance_pct') return `${t('watchlist2.wallDistance')} ${check.operator} ${check.expected}%, ${t('watchlist2.currentlyLabel')} ${actual ?? '--'}%`;
  if (field === 'gex.trend_acceleration_risk') return `${t('watchlist2.trendAccelRisk')} ${check.operator} ${expected || '--'}, ${t('watchlist2.currentlyLabel')} ${actual || '--'}`;
  if (field === 'gex.pinning_risk') return `${t('watchlist2.pinningRisk')} ${check.operator} ${expected || '--'}, ${t('watchlist2.currentlyLabel')} ${actual || '--'}`;
  return '';
}

function gexAuditLine(event) {
  const checks = event?.payload?.alert?.checks || [];
  const parts = checks.map(gexCheckText).filter(Boolean);
  return parts.length ? parts.join(' · ') : '';
}

export function NotificationEventsPanel({
  events = [],
  busyEventId = '',
  onSend,
}) {
  return (
    <section className="panel">
      <SectionTitle title={t('watchlist2.notificationEvents')} />
      <div className="run-list">
        {events.map((event) => {
          const contextLine = eventContextLine(event);
          const auditLine = gexAuditLine(event);
          return (
            <article key={event.id} className="run-card">
              <div className="run-card-head">
                <strong>{event.title}</strong>
                <span>{event.status} · {event.attempts || 0} {t('watchlist2.attempts')}</span>
              </div>
              <p>{event.body}</p>
              {contextLine && <small>{contextLine}</small>}
              {auditLine && <small>{auditLine}</small>}
              {event.last_error && <small>{event.last_error}</small>}
              {event.status !== 'sent' && (
                <button className="ghost compact" type="button" disabled={busyEventId === event.id} onClick={() => onSend(event.id)}>
                  <Send size={14} /> {event.status === 'failed' ? t('watchlist2.retrySend') : t('watchlist2.sendNow')}
                </button>
              )}
            </article>
          );
        })}
        {!events.length && <p className="muted">{t('watchlist2.noNotificationEvents')}</p>}
      </div>
    </section>
  );
}
