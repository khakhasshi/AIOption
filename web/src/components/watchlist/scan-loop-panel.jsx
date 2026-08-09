import React from 'react';
import { CopyableId } from '../copyable-id.jsx';
import { Bell, FlaskConical, Play } from 'lucide-react';
import { t } from '../../i18n/index.js';
import { DisclosurePanel, SectionTitle } from '../common.jsx';
import { formatTime, runStatusLabel } from '../../utils/display.js';

function digestSummary(run) {
  const count = Number(run?.summary?.digest_count || 0);
  if (!count) return '';
  const eventId = run?.summary?.digest_notification_event_id;
  return `${t('watchlist2.dailyDigest')} ${count} ${t('watchlist2.candidates')}${eventId ? ` · ${t('watchlist2.notice')} ${eventId}` : ''}`;
}

function freshnessLabel(freshness) {
  return {
    fresh: t('watchlist2.dataFresh'),
    stale: t('watchlist2.dataStale'),
    partial_failed: t('watchlist2.dataPartialFailed'),
    data_unavailable: t('watchlist2.dataUnavailable'),
  }[freshness] || freshness || 'unknown';
}

function checkSummary(result) {
  const checks = Array.isArray(result?.checks) ? result.checks : [];
  if (!checks.length) return result?.reason || '';
  return checks.slice(0, 3).map((check) => `${check.field} ${check.operator} ${check.expected}: ${check.matched ? t('watchlist2.hit') : check.actual === null ? t('watchlist2.missingData') : `${t('watchlist2.currentlyLabel')} ${check.actual}`}`).join(' / ');
}

function ruleStatusClass(status) {
  if (status === 'would_notify') return 'matched';
  if (status === 'data_unavailable') return 'missing';
  if (['cooldown', 'limit_blocked', 'market_blocked'].includes(status)) return 'blocked';
  return '';
}

function explainReasons(reasons = []) {
  const labels = {
    max_ai_scans_per_day: t('watchlist2.reason.max_ai_scans_per_day'),
    alert_not_matched: t('watchlist2.reason.alert_not_matched'),
    market_not_regular_open: t('watchlist2.reason.market_not_regular_open'),
    alert_mode_silent_log: t('watchlist2.reason.alert_mode_silent_log'),
    alert_mode_best_per_run: t('watchlist2.reason.alert_mode_best_per_run'),
    max_alerts_per_day: t('watchlist2.reason.max_alerts_per_day'),
    cooldown: t('watchlist2.reason.cooldown'),
    data_unavailable: t('watchlist2.dataUnavailable'),
    ai_scan_policy_alert_not_matched: t('watchlist2.reason.ai_scan_policy_alert_not_matched'),
    ai_scan_policy_top_n: t('watchlist2.reason.ai_scan_policy_top_n'),
    ai_scan_policy_smart_budget: t('watchlist2.reason.ai_scan_policy_smart_budget'),
    ai_scan_policy_not_selected: t('watchlist2.reason.ai_scan_policy_not_selected'),
  };
  return reasons.map((reason) => labels[reason] || reason).join(' / ');
}

function aiDecisionLabel(decision = {}, fallbackReasons = []) {
  if (decision.selected) return `${t('watchlist2.aiSelected')}${decision.rank ? ` · Rank ${decision.rank}` : ''}`;
  const reason = decision.suppressed_reason || decision.reason || fallbackReasons[0] || '';
  const labels = {
    selected_for_ai_scan: t('watchlist2.aiSelected'),
    prefilter_not_matched: t('watchlist2.aiReason.prefilter_not_matched'),
    data_unavailable: t('watchlist2.missingData'),
    ai_scan_policy_alert_not_matched: t('watchlist2.aiReason.alert_not_matched'),
    ai_scan_policy_top_n: t('watchlist2.reason.ai_scan_policy_top_n'),
    ai_scan_policy_smart_budget: t('watchlist2.reason.ai_scan_policy_smart_budget'),
    max_ai_scans_per_day: t('watchlist2.aiReason.max_ai_scans_per_day'),
    ai_scan_policy_not_selected: t('watchlist2.reason.ai_scan_policy_not_selected'),
  };
  return labels[reason] || reason || t('watchlist2.notSelectedForAi');
}

function budgetSummary(summary = {}, limits = {}) {
  const budget = summary.ai_scan_budget || limits.ai_scan_budget || {};
  if (!Object.keys(budget).length) return '';
  return `${t('watchlist2.aiQuota')} ${budget.used_before_run ?? 0}/${budget.max_per_day ?? 0} ${t('watchlist2.used')} · ${t('watchlist2.thisRun')} ${budget.used_this_run ?? 0} · ${t('watchlist2.remaining')} ${budget.remaining_after_run ?? budget.remaining_ai_scans ?? 0}`;
}

function costSummary(summary = {}) {
  const projection = summary.ai_cost_projection || {};
  const actual = summary.ai_cost_actual || {};
  const projected = Number(projection.estimated_cost_cny || 0);
  const actualCost = Number(actual.estimated_cost_cny || 0);
  const calls = Number(actual.calls || 0);
  if (!projected && !actualCost && !calls) return '';
  return `${t('watchlist2.estimated')} ¥${projected.toFixed(4)} · ${t('watchlist2.actual')} ¥${actualCost.toFixed(4)}${calls ? ` · ${calls} ${t('watchlist2.calls')}` : ''}`;
}

function snapshotSummary(snapshot = {}) {
  const fields = [
    ['last', 'Last'],
    ['rvol', 'RVOL'],
    ['underlying_vs_vwap_pct', 'VWAP%'],
    ['vwap', 'VWAP'],
    ['bid_ask_spread_pct', 'Spread%'],
    ['volume', 'Vol'],
    ['open_interest', 'OI'],
    ['gex_regime', 'GEX'],
  ];
  return fields
    .filter(([field]) => snapshot[field] !== undefined && snapshot[field] !== null && snapshot[field] !== '')
    .slice(0, 6)
    .map(([field, label]) => `${label} ${snapshot[field]}`)
    .join(' · ');
}

function RuleTestPanel({ result }) {
  if (!result) return null;
  const summary = result.summary || {};
  return (
    <div className="rule-test-panel">
      <div className="rule-test-head">
        <strong>{t('watchlist2.ruleTest')} · {result.instance_name || '--'}</strong>
        <small>{result.market_state || 'unknown'} · {formatTime(result.generated_at)} · {result.notification_policy}</small>
      </div>
      <div className="mini-list">
        <span>{t('watchlist2.stocks')} {summary.symbols ?? 0}</span>
        <span>{t('watchlist2.prefilterHit')} {summary.prefilter_matched ?? 0}</span>
        <span>{t('watchlist2.alertHit')} {summary.alert_matched ?? 0}</span>
        <span>{t('watchlist2.willEnterAi')} {summary.would_submit_ai ?? 0}</span>
        <span>{t('watchlist2.willAlert')} {summary.would_notify ?? 0}</span>
        <span>{t('watchlist2.missingData')} {summary.data_unavailable ?? 0}</span>
        <span>{t('watchlist2.aiPolicyShort')} {summary.ai_scan_policy || 'prefilter_matched'}</span>
        <span>Top N {summary.ai_scan_top_n ?? '--'}</span>
      </div>
      {budgetSummary(summary, result.limits) && <small className="muted">{budgetSummary(summary, result.limits)}</small>}
      {costSummary(summary) && <small className="muted">{t('watchlist2.costColon')}{costSummary(summary)}</small>}
      <div className="rule-test-grid">
        {(result.items || []).map((item) => (
          <article key={item.symbol} className={`rule-test-card ${ruleStatusClass(item.status)}`}>
            <div className="run-card-head">
              <strong>{item.symbol}</strong>
              <span>{item.label}</span>
            </div>
            <small>{snapshotSummary(item.snapshot_summary) || item.data_quality?.explanation || '--'}</small>
            <div className="mini-list">
              <span className={item.prefilter_matched ? 'positive' : 'warning'}>{t('watchlist2.prefilter')} {item.prefilter_matched ? t('watchlist2.hit') : t('watchlist2.miss')}</span>
              <span className={item.alert_matched ? 'positive' : 'warning'}>{t('watchlist2.alert')} {item.alert_matched ? t('watchlist2.hit') : t('watchlist2.miss')}</span>
              <span className={item.would_submit_ai ? 'positive' : ''}>AI {item.would_submit_ai ? t('watchlist2.willScan') : t('watchlist2.wontScan')}</span>
              <span className={item.would_notify ? 'positive' : item.suppressed_reasons?.length ? 'warning' : ''}>{t('watchlist2.notification')} {item.would_notify ? t('watchlist2.willSend') : t('watchlist2.wontSend')}</span>
            </div>
            <div className="audit-strip">
              <span className={item.ai_scan_decision?.candidate ? 'positive' : 'muted'}>{t('watchlist2.candidate')} {item.ai_scan_decision?.candidate ? t('watchlist2.yes') : t('watchlist2.no')}</span>
              <span>Score {Number(item.ai_scan_decision?.score ?? item.score ?? 0).toFixed(2)}</span>
              <span>{aiDecisionLabel(item.ai_scan_decision, item.suppressed_reasons)}</span>
            </div>
            <small>{t('watchlist2.prefilterColon')}{checkSummary(item.prefilter_result) || '--'}</small>
            <small>{t('watchlist2.alertColon')}{checkSummary(item.alert_result) || '--'}</small>
            {item.missing_fields?.length > 0 && <small>{t('watchlist2.missingFields')}{item.missing_fields.join(', ')}</small>}
            {item.suppressed_reasons?.length > 0 && <small>{t('watchlist2.blocked')}{explainReasons(item.suppressed_reasons)}</small>}
            {item.explanation && <small>{item.explanation}</small>}
          </article>
        ))}
      </div>
    </div>
  );
}

export function ScanLoopPanel({
  busy = false,
  ruleTestBusy = false,
  ruleTestResult = null,
  notificationPreview = null,
  notificationPreviewBusy = false,
  selectedInstance = null,
  channels = [],
  runs = [],
  onRunSelectedInstance,
  onTestRules,
  onTestNotificationPayload,
}) {
  const channelById = new Map(channels.map((channel) => [channel.id, channel]));
  const boundChannels = (selectedInstance?.notification_channel_ids || []).map((id) => channelById.get(id)).filter(Boolean);
  return (
    <section className="panel">
      <div className="answer-head">
        <SectionTitle title={t('watchlist2.runAndAlert')} />
        <div className="action-row">
          <button className="ghost compact" type="button" disabled={ruleTestBusy || !selectedInstance} onClick={onTestRules}>
            <FlaskConical size={14} /> {t('watchlist2.testRules')}
          </button>
          <button className="ghost compact" type="button" disabled={notificationPreviewBusy || !selectedInstance} onClick={onTestNotificationPayload}>
            <Bell size={14} /> {t('watchlist2.testNotification')}
          </button>
          <button className="primary compact" type="button" disabled={busy || !selectedInstance} onClick={onRunSelectedInstance}>
            <Play size={16} /> {t('watchlist2.runNow')}
          </button>
        </div>
      </div>
      {selectedInstance && (
        <div className="permission-note">
          <strong>{selectedInstance.name}</strong>
          <p>{t('watchlist2.observeOnlyNote')}</p>
          <small>
            {t('watchlist2.notificationChannelsColon')}{boundChannels.length
              ? boundChannels.map((channel) => `${channel.label}(${channel.config?.provider || channel.type}${channel.enabled ? '' : `/${t('watchlist2.disabledSuffix')}`})`).join(' / ')
              : t('watchlist2.notBoundNoDelivery')}
          </small>
          {(selectedInstance.eod_review_enabled || selectedInstance.weekend_review_enabled) && (
            <small>
              {t('watchlist2.autoReviewColon')}{selectedInstance.eod_review_enabled ? `${t('watchlist2.eodClose')} ${selectedInstance.eod_run_time_et || '16:20'} ET` : t('watchlist2.notEnabled')}
              {selectedInstance.weekend_review_enabled ? ` · ${t('watchlist2.weekendPlanOn')}` : ''}
            </small>
          )}
        </div>
      )}
      {!selectedInstance && (
        <div className="empty actionable-empty">
          <h3>{t('watchlist2.noSelectedInstanceTitle')}</h3>
          <p>{t('watchlist2.noSelectedInstanceDesc')}</p>
        </div>
      )}
      <DisclosurePanel
        title={t('watchlist2.testResultsAndRuns')}
        summary={`${runs.length} runs${ruleTestResult ? ' · rule test ready' : ''}${notificationPreview ? ' · notification preview' : ''}`}
        className="embedded-disclosure"
      >
        {notificationPreview && (
          <div className="notification-preview-mini">
            <div className="rule-test-head">
              <strong>{t('watchlist2.notificationPayload')} · {notificationPreview.instance_name || '--'}</strong>
              <small>{notificationPreview.channel_count || 0} {t('watchlist2.channels')}</small>
            </div>
            <div className="notification-preview-mini-grid">
              {(notificationPreview.channels || []).map((item) => (
                <article key={item.id} className={`notification-preview-mini-card ${item.enabled ? '' : 'disabled'}`}>
                  <strong>{item.label}</strong>
                  <small>{item.provider} · {item.enabled ? t('watchlist2.enabled') : t('watchlist2.disabled')}</small>
                  <pre>{JSON.stringify(item.preview, null, 2)}</pre>
                </article>
              ))}
              {!notificationPreview.channels?.length && <p className="muted">{t('watchlist2.noBoundChannelsInstance')}</p>}
            </div>
          </div>
        )}
        <RuleTestPanel result={ruleTestResult} />
        <div className="run-list">
          {runs.map((run) => {
          const digest = digestSummary(run);
          return (
            <article key={run.id} className="run-card">
              <div className="run-card-head">
                <strong>{runStatusLabel(run.status)}</strong>
                <span>{run.market_state || 'unknown'} · {formatTime(run.created_at)}</span>
              </div>
              <p>
                {t('watchlist2.scanned')} {run.scanned_count} · {t('watchlist2.matched')} {run.matched_count} · {t('watchlist2.alerted')} {run.alerted_count}
                {run.summary?.data_unavailable_count ? ` · ${t('watchlist2.dataMissing')} ${run.summary.data_unavailable_count}` : ''}
              </p>
              {run.summary?.ai_scan_policy && (
                <small>
                  {t('watchlist2.aiScanColon')}{run.summary.ai_scan_policy}
                  {run.summary.ai_scan_top_n ? ` · Top ${run.summary.ai_scan_top_n}` : ''}
                  {run.summary.ai_scan_candidate_count ? ` · ${t('watchlist2.candidate')} ${run.summary.ai_scan_candidate_count}` : ''}
                  {run.summary.ai_scan_selected_count ? ` · ${t('watchlist2.selected')} ${run.summary.ai_scan_selected_count}` : ''}
                </small>
              )}
              {budgetSummary(run.summary) && <small>{budgetSummary(run.summary)}</small>}
              {costSummary(run.summary) && <small>{t('watchlist2.aiCostColon')}{costSummary(run.summary)}</small>}
              {digest && <small>{digest}</small>}
              {run.data_freshness?.freshness_status && (
                <small>
                  {t('watchlist2.dataQualityColon')}{freshnessLabel(run.data_freshness.freshness_status)}
                  {run.data_freshness.sources?.length ? ` · ${run.data_freshness.sources.join(', ')}` : ''}
                </small>
              )}
              {Array.isArray(run.data_freshness?.explanations) && run.data_freshness.explanations.slice(0, 2).map((text) => (
                <small key={`${run.id}-${text}`}>{text}</small>
              ))}
              {run.summary?.review_only && <small>{t('watchlist2.reviewModeNote')}</small>}
              {run.status === 'partial_failed' && <small>{t('watchlist2.partialFailedNote')}</small>}
              {Array.isArray(run.items) && run.items.length > 0 && (
                <div className="run-item-grid">
                  {run.items.slice(0, 8).map((item) => (
                    <div key={item.id || `${run.id}-${item.symbol}`} className={`run-item ${item.triggered ? 'matched' : item.status === 'data_unavailable' ? 'missing' : ''}`}>
                      <strong>{item.symbol}</strong>
                      <span>{item.triggered ? t('watchlist2.alertHitShort') : item.prefilter_status || item.status}</span>
	                      <small>{t('watchlist2.prefilterColon')}{checkSummary(item.prefilter_result) || '--'}</small>
	                      <small>{t('watchlist2.alertColon')}{(item.trigger_reasons || []).join(' / ') || checkSummary(item.recommendation?.alert_result) || item.error || '--'}</small>
	                      {item.recommendation?.ai_scan_decision && (
	                        <small>
	                          AI: {aiDecisionLabel(item.recommendation.ai_scan_decision, [item.recommendation.ai_scan_suppressed_reason].filter(Boolean))}
	                          {item.recommendation.ai_scan_decision.rank ? ` · Rank ${item.recommendation.ai_scan_decision.rank}` : ''}
	                        </small>
	                      )}
	                    </div>
                  ))}
                </div>
              )}
              <CopyableId value={run.id} label={t('watchlist2.runInstance')} compact />
            </article>
          );
          })}
          {!runs.length && selectedInstance && (
            <div className="empty actionable-empty compact">
              <h3>{t('watchlist2.instanceNeverRanTitle')}</h3>
              <p>{t('watchlist2.instanceNeverRanDesc')}</p>
            </div>
          )}
        </div>
      </DisclosurePanel>
    </section>
  );
}
