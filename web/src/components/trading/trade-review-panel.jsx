import React, { useEffect, useState } from 'react';
import { SectionTitle } from '../common.jsx';
import { t } from '../../i18n/index.js';

const TERMINAL_STATES = new Set(['closed', 'reviewed']);

function isClosed(activeRun) {
  if (!activeRun) return false;
  const lifecycle = activeRun?.trade_instance?.lifecycle_state || activeRun?.lifecycle_state;
  return TERMINAL_STATES.has(lifecycle);
}

function verdictTone(verdict) {
  switch ((verdict || '').toLowerCase()) {
    case 'win':
      return 'positive';
    case 'loss':
      return 'negative';
    case 'breakeven':
      return 'neutral';
    default:
      return 'neutral';
  }
}

function ScoreBadge({ score }) {
  if (typeof score !== 'number') return null;
  const tone = score >= 75 ? 'positive' : score >= 50 ? 'neutral' : 'negative';
  return <span className={`badge ${tone}`}>{t('trading2.scoreShort')} {Math.round(score)}</span>;
}

function ReviewBullets({ title, items }) {
  if (!Array.isArray(items) || items.length === 0) return null;
  return (
    <div className="review-block">
      <strong>{title}</strong>
      <ul>
        {items.map((item, idx) => (
          <li key={`${title}-${idx}`}>{String(item)}</li>
        ))}
      </ul>
    </div>
  );
}

export function TradeReviewPanel({ api, activeRun }) {
  const [review, setReview] = useState(null);
  const [status, setStatus] = useState('idle');
  const [error, setError] = useState('');

  const runId = activeRun?.id;
  const closed = isClosed(activeRun);

  useEffect(() => {
    if (!runId || !closed) {
      setReview(null);
      setStatus('idle');
      setError('');
      return undefined;
    }
    let cancelled = false;
    setStatus('loading');
    setError('');
    fetch(`/api/trading/runs/${encodeURIComponent(runId)}/review`, {
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
    })
      .then(async (response) => {
        if (cancelled) return;
        if (response.status === 404) {
          setReview(null);
          setStatus('pending');
          return;
        }
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body?.detail || `${t('trading2.requestFailed')}${response.status}`);
        }
        const body = await response.json();
        if (cancelled) return;
        setReview(body);
        setStatus('ready');
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err?.message || String(err));
        setStatus('error');
      });
    return () => {
      cancelled = true;
    };
  }, [runId, closed]);

  if (!closed) return null;

  const reviewBody = review?.review || {};
  const facts = review?.facts || {};

  return (
    <div className="panel answer-card">
      <div className="answer-head">
        <SectionTitle title={t('trading2.reviewAiSummary')} />
        <span className="muted">
          {review?.review_status === 'completed' && review?.reviewed_at
            ? `${t('trading2.generated')} · ${new Date(review.reviewed_at).toLocaleString()}`
            : review?.review_status === 'skipped'
            ? t('trading2.reviewSkipped')
            : review?.review_status === 'failed'
            ? t('trading2.reviewFailedRetry')
            : review?.review_status === 'processing'
            ? t('trading2.generating')
            : status === 'pending'
            ? t('trading2.queuedWaitWorker')
            : ''}
        </span>
      </div>

      {status === 'loading' && <p className="muted">{t('trading2.loadingReview')}</p>}
      {status === 'error' && <p className="error">{error}</p>}

      {status === 'pending' && (
        <p className="muted">
          {t('trading2.reviewPendingNote1')}
          <code> AI_OPTION_ENABLE_POST_MORTEM_WORKER </code> {t('trading2.reviewPendingNote2')}
        </p>
      )}

      {review && review.review_status === 'skipped' && (
        <p className="muted">
          {t('trading2.reasonLabel')}<code>{review.review_error || 'below_pnl_and_holding_thresholds'}</code>
          {t('trading2.reviewSkippedNote')} <code>AI_OPTION_POST_MORTEM_PNL_THRESHOLD</code> /{' '}
          <code>AI_OPTION_POST_MORTEM_HOLDING_MIN</code> {t('trading2.adjustSuffix')}
        </p>
      )}

      {review && review.review_status === 'failed' && review.review_error && (
        <p className="error">{t('trading2.lastFailure')}{review.review_error}</p>
      )}

      {review && review.review_status === 'completed' && (
        <>
          <div className="review-head">
            <span className={`badge ${verdictTone(reviewBody.verdict)}`}>
              {reviewBody.verdict || '—'}
            </span>
            <ScoreBadge score={reviewBody.score} />
            {typeof review.realized_pnl === 'number' && (
              <span className="muted">{t('trading2.realizedPnlLabel')}{review.realized_pnl.toFixed(2)}</span>
            )}
            {typeof review.holding_minutes === 'number' && (
              <span className="muted">{t('trading2.holdingLabel')}{review.holding_minutes} {t('trading2.minutesUnit')}</span>
            )}
            {review.exit_reason && (
              <span className="muted">{t('trading2.exitReasonLabel')}{review.exit_reason}</span>
            )}
          </div>

          {reviewBody.summary && <p className="review-summary">{reviewBody.summary}</p>}

          <div className="review-grid">
            <ReviewBullets title={t('trading2.whatWentRight')} items={reviewBody.what_went_right} />
            <ReviewBullets title={t('trading2.whatWentWrong')} items={reviewBody.what_went_wrong} />
            <ReviewBullets title={t('trading2.lessons')} items={reviewBody.lessons} />
            <ReviewBullets title={t('trading2.suggestedChanges')} items={reviewBody.suggested_changes} />
          </div>

          <details className="review-meta">
            <summary className="muted">{t('trading2.expandFactsCard')}</summary>
            <pre className="review-facts">{JSON.stringify(facts, null, 2)}</pre>
          </details>
        </>
      )}
    </div>
  );
}
