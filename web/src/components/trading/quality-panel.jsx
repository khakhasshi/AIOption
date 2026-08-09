import React from 'react';
import { Metric, SectionTitle } from '../common.jsx';
import { pct } from '../../utils/display.js';
import { t } from '../../i18n/index.js';

export function QualityPanel({ aiQuality, refreshAiQuality }) {
  return (
<div className="panel quality-panel">
  <div className="answer-head">
    <SectionTitle title={t('trading2.aiDecisionQuality')} />
    <button className="ghost" type="button" onClick={refreshAiQuality}>{t('trading2.refreshQuality')}</button>
  </div>
  <div className="readiness-grid">
    <Metric label={t('trading2.reviewSamples')} value={aiQuality?.sample_size ?? 0} sub={t('trading2.recent50')} />
    <Metric label={t('trading2.avgConfidence')} value={pct(aiQuality?.avg_confidence)} sub={t('trading2.aiConfidence')} />
    <Metric label={t('trading2.avgReturn')} value={pct(aiQuality?.avg_return_pct)} sub={t('trading2.estimatedReturn')} />
    <Metric label={t('trading2.winRate')} value={pct(aiQuality?.win_rate)} sub={`${t('trading2.deviation')} ${pct(aiQuality?.avg_confidence_vs_return)}`} />
  </div>
  <div className="quality-buckets">
    {(aiQuality?.buckets || []).map((bucket) => (
      <div className="quality-bucket" key={bucket.key}>
        <strong>{bucket.label}</strong>
        <span>{bucket.count} {t('trading2.instancesUnit')}</span>
        <small>{t('trading2.returnShort')} {pct(bucket.avg_return_pct)} · {t('trading2.winRateShort')} {pct(bucket.win_rate)}</small>
      </div>
    ))}
  </div>
  {!aiQuality?.sample_size && <p className="muted">{t('trading2.qualityEmpty')}</p>}
</div>
  );
}
