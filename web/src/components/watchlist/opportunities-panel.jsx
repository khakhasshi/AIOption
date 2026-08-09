import React from 'react';
import { RefreshCw } from 'lucide-react';
import { t } from '../../i18n/index.js';
import { SectionTitle } from '../common.jsx';
import { OpportunityCard } from './opportunity-card.jsx';

export function OpportunitiesPanel({
  opportunities = [],
  opportunityDetails = {},
  expandedOpportunityId = '',
  detailBusyId = '',
  busyOpportunityId = '',
  followupBusy = false,
  onProcessFollowups,
  onMarkWatching,
  onMarkActive,
  onAdjustRiskPlan,
  onPause,
  onResume,
  onReview,
  onArchive,
  onToggleDetail,
  onOpenDetailPage,
  onOpenDetailModal,
}) {
  return (
    <section className="panel">
      <div className="answer-head">
        <SectionTitle title={t('watchlist2.recentOpportunities')} />
        {onProcessFollowups && (
          <button className="ghost compact" type="button" disabled={followupBusy} onClick={onProcessFollowups}>
            <RefreshCw size={14} /> {t('watchlist2.refreshTracking')}
          </button>
        )}
      </div>
      <div className="run-list">
        {opportunities.map((item) => (
          <OpportunityCard
            key={item.id}
            item={item}
            detail={opportunityDetails[item.id]}
            expanded={expandedOpportunityId === item.id}
            busy={busyOpportunityId === item.id}
            detailBusy={detailBusyId === item.id}
            onMarkWatching={onMarkWatching}
            onMarkActive={onMarkActive}
            onAdjustRiskPlan={onAdjustRiskPlan}
            onPause={onPause}
            onResume={onResume}
            onReview={onReview}
            onArchive={onArchive}
            onToggleDetail={onToggleDetail}
            onOpenDetailPage={onOpenDetailPage}
            onOpenDetailModal={onOpenDetailModal}
          />
        ))}
        {!opportunities.length && (
          <div className="empty actionable-empty compact">
            <h3>{t('watchlist2.noOpportunitiesTitle')}</h3>
            <p>{t('watchlist2.noOpportunitiesDesc')}</p>
          </div>
        )}
      </div>
    </section>
  );
}
