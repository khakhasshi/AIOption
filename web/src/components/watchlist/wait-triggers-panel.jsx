import React from 'react';
import { FlaskConical, Trash2 } from 'lucide-react';
import { t } from '../../i18n/index.js';
import { SectionTitle } from '../common.jsx';
import { formatTime } from '../../utils/display.js';

function triggerTypeLabel(type) {
  return {
    underlying_price: t('watchlist2.triggerType.underlying_price'),
    technical_indicator: t('watchlist2.triggerType.technical_indicator'),
    option_quote: t('watchlist2.triggerType.option_quote'),
    rescan_score: t('watchlist2.triggerType.rescan_score'),
  }[type] || t('watchlist2.triggerType.underlying_price');
}

function conditionLabel(trigger) {
  const condition = trigger?.condition || {};
  const field = condition.field ? ` · ${condition.label || condition.field}` : '';
  const contract = condition.contract_symbol ? ` · ${condition.contract_symbol}` : '';
  return `${triggerTypeLabel(condition.type)}${field}${contract} · ${trigger?.symbol || condition.symbol || '--'} ${condition.operator || '>='} ${condition.value ?? '--'}`;
}

function marketPolicyLabel(policy) {
  return {
    regular_only: t('watchlist2.policy.regular_only'),
    include_extended: t('watchlist2.policy.include_extended'),
    next_open: t('watchlist2.policy.next_open'),
    eod_review: t('watchlist2.policy.eod_review'),
    always_calendar: t('watchlist2.policy.always_calendar'),
  }[policy] || t('watchlist2.policy.regular_only');
}

function testResultLabel(result) {
  if (!result) return '';
  const quality = result.data_quality || result.snapshot?.data_quality;
  const qualityText = quality ? ` · ${quality.label || quality.status}${quality.source ? ` · ${quality.source}` : ''}${quality.explanation ? ` · ${quality.explanation}` : ''}` : '';
  if (result.matched) return `${t('watchlist2.testHit')} · ${t('watchlist2.currentValueShort')} ${result.current_value ?? '--'}${qualityText}`;
  return `${t('watchlist2.testMiss')} · ${result.reason || t('watchlist2.currentValueShort')} ${result.current_value ?? '--'}${qualityText}`;
}

export function WaitTriggersPanel({
  triggers = [],
  onRefresh,
  onToggle,
  onDelete,
  onTest,
  testingTriggerId = '',
  testResults = {},
}) {
  return (
    <section className="panel">
      <div className="answer-head">
        <SectionTitle title="Wait Trigger" />
        <button className="ghost compact" type="button" onClick={onRefresh}>{t('watchlist2.refresh')}</button>
      </div>
      <div className="trigger-list">
        {triggers.map((trigger) => (
          <article key={trigger.id} className="trigger-card">
            <button className="trigger-main" type="button" onClick={() => onToggle(trigger.id, !trigger.enabled)}>
              <span>{trigger.enabled ? t('watchlist2.enabled') : t('watchlist2.pause')} · {trigger.status}</span>
              <strong>{trigger.name}</strong>
              <small>{conditionLabel(trigger)} · {marketPolicyLabel(trigger.market_policy)} · {t('watchlist2.next')} {trigger.next_check_at ? formatTime(trigger.next_check_at) : '--'}</small>
              {testResultLabel(testResults[trigger.id]) && <small>{testResultLabel(testResults[trigger.id])}</small>}
            </button>
            <div className="trigger-actions">
              <button className="icon-button" type="button" aria-label={t('watchlist2.testTrigger')} title={t('watchlist2.testTrigger')} disabled={testingTriggerId === trigger.id} onClick={() => onTest?.(trigger.id)}>
                <FlaskConical size={14} />
              </button>
              <button className="icon-button" type="button" aria-label={t('watchlist2.deleteTrigger')} title={t('watchlist2.deleteTrigger')} onClick={() => onDelete(trigger.id)}>
                <Trash2 size={14} />
              </button>
            </div>
          </article>
        ))}
        {!triggers.length && (
          <div className="empty actionable-empty compact">
            <h3>{t('watchlist2.noWaitTriggerTitle')}</h3>
            <p>{t('watchlist2.noWaitTriggerDesc')}</p>
          </div>
        )}
      </div>
    </section>
  );
}
