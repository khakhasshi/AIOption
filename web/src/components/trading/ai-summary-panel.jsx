import React from 'react';
import { ProgressBar, SectionTitle } from '../common.jsx';
import { advisorStatusLabel, councilModeLabel, pct, previewText, selectionSourceLabel } from '../../utils/display.js';
import { t } from '../../i18n/index.js';

export function AiSummaryPanel({ activeRun, advisorReports, councilMode, refreshTradingRuns, selections }) {
  return (
<div className="panel answer-card">
  <div className="answer-head">
    <SectionTitle title={t('trading2.councilSummary')} />
    <button className="ghost" type="button" onClick={refreshTradingRuns}>{t('trading2.refresh')}</button>
  </div>
  {activeRun && <ProgressBar progress={activeRun.progress} stage={activeRun.stage} />}
  <p className="muted">
    {activeRun?.council?.summary || t('trading2.waitingCreateInstance')}
    {councilMode && <span className="mode-chip">{councilModeLabel(councilMode)}</span>}
  </p>
  {advisorReports.length > 0 && (
    <div className="advisor-strip">
      {advisorReports.map((report) => (
        <article className="advisor-note" key={report.key || report.advisor}>
          <strong>{report.advisor || report.key}</strong>
          <span>{advisorStatusLabel(report.status || 'succeeded')}</span>
          <small>{previewText(report.report, 220)}</small>
        </article>
      ))}
    </div>
  )}
  <div className="trade-grid">
    {selections.map((selection) => (
      <article className="trade-card" key={selection.contract_symbol}>
        <strong>{selection.contract_symbol}</strong>
        <span>{selection.symbol} · {pct(selection.allocation_pct * 100)} · {t('trading2.stopLossShort')} {pct(selection.stop_loss_pct)}</span>
        <span className={`source-chip ${selection.selection_source || 'unknown'}`}>{selectionSourceLabel(selection.selection_source)}</span>
        <small>{selection.reason || t('trading2.aiSelected')}</small>
      </article>
    ))}
  </div>
</div>
  );
}
