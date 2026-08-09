import React from 'react';
import { t } from '../i18n/index.js';
import { PageHeader } from '../components/app-shell/page-header.jsx';
import { useAppShell } from '../components/app-shell/app-shell-context.js';
import { useWatchlistRadar } from '../components/watchlist/use-watchlist-radar.js';
import { WatchlistOverviewPanel } from '../components/watchlist/watchlist-overview-panel.jsx';
import { WatchlistControlPanel } from '../components/watchlist/watchlist-control-panel.jsx';
import { RadarOperationsPanel } from '../components/watchlist/radar-operations-panel.jsx';
import { ScanLoopPanel } from '../components/watchlist/scan-loop-panel.jsx';
import { OpportunitiesPanel } from '../components/watchlist/opportunities-panel.jsx';
import { WaitTriggersPanel } from '../components/watchlist/wait-triggers-panel.jsx';
import { NotificationEventsPanel } from '../components/watchlist/notification-events-panel.jsx';
import { OpportunityDetailModal } from '../components/watchlist/opportunity-detail-modal.jsx';

export function WatchlistPage({
  api,
  providers = [],
  triggers = [],
  refreshTriggers = () => {},
  toggleTrigger,
  deleteTrigger,
  testTrigger,
  testingTriggerId = '',
  triggerTestResults = {},
  onNotifications,
  onOpenOpportunityDetailPage,
}) {
  const { embedded } = useAppShell();
  const radar = useWatchlistRadar({ api, providers, refreshTriggers });

  return (
    <main className="shell watchlist-shell">
      {embedded && (
        <PageHeader
          eyebrow="Opportunity Radar"
          title={t('watchlist2.radarTitle')}
          subtitle={t('watchlist2.radarSubtitle')}
          actions={
            <>
              <button className="ghost" type="button" onClick={() => radar.refreshAll()} disabled={radar.busy}>
                {radar.busy ? t('watchlist2.refreshing') : t('watchlist2.refreshAll')}
              </button>
              <button className="ghost" type="button" onClick={onNotifications}>{t('watchlist2.notificationCenter')}</button>
            </>
          }
        />
      )}

      {radar.error && <div className="error-banner">{radar.error}</div>}

      <div className="watchlist-layout">
        <aside className="watchlist-side">
          <WatchlistOverviewPanel
            busy={radar.busy}
            watchlists={radar.watchlists}
            instances={radar.visibleInstances}
            allInstances={radar.instances}
            channels={radar.channels}
            selectedWatchlistId={radar.selectedWatchlistId}
            selectedInstanceId={radar.selectedInstanceId}
            onRefresh={() => radar.refreshAll()}
            onSelectWatchlist={radar.selectWatchlist}
            onEditWatchlist={radar.editWatchlist}
            onDuplicateWatchlist={radar.duplicateWatchlist}
            onDeleteWatchlist={radar.deleteWatchlist}
            onSelectInstance={radar.selectInstance}
            onEditInstance={radar.editInstance}
            onDuplicateInstance={radar.duplicateInstance}
            onDeleteInstance={radar.deleteInstance}
            onToggleInstanceStatus={radar.toggleInstanceStatus}
          />
          <WatchlistControlPanel
            busy={radar.busy}
            watchlistForm={radar.watchlistForm}
            setWatchlistForm={radar.setWatchlistForm}
            instanceForm={radar.instanceForm}
            setInstanceForm={radar.setInstanceForm}
            providers={providers}
            channels={radar.channels}
            selectedWatchlist={radar.selectedWatchlist}
            editingWatchlistId={radar.editingWatchlistId}
            editingInstanceId={radar.editingInstanceId}
            onCreateWatchlist={radar.createWatchlist}
            onCancelWatchlistEdit={radar.cancelWatchlistEdit}
            onCreateInstance={radar.createInstance}
            onCancelInstanceEdit={radar.cancelInstanceEdit}
            onApplyInstanceTemplate={radar.applyInstanceTemplate}
            onOpenNotifications={onNotifications}
          />
        </aside>

        <div className="watchlist-main">
          <RadarOperationsPanel
            health={radar.observationHealth}
            busy={radar.observationBusy}
            onRefresh={radar.refreshObservationHealth}
            onRunDueCycle={radar.runObservationDueCycle}
          />
          <ScanLoopPanel
            busy={radar.busy}
            ruleTestBusy={radar.ruleTestBusy}
            ruleTestResult={radar.ruleTestResult}
            notificationPreview={radar.notificationPreview}
            notificationPreviewBusy={radar.notificationPreviewBusy}
            selectedInstance={radar.selectedInstance}
            channels={radar.channels}
            runs={radar.runs}
            onRunSelectedInstance={radar.runSelectedInstance}
            onTestRules={radar.testSelectedInstanceRules}
            onTestNotificationPayload={radar.testSelectedInstanceNotificationPayload}
          />
          <OpportunitiesPanel
            opportunities={radar.opportunities}
            opportunityDetails={radar.opportunityDetails}
            expandedOpportunityId={radar.expandedOpportunityId}
            detailBusyId={radar.opportunityDetailBusyId}
            busyOpportunityId={radar.busyOpportunityId}
            followupBusy={radar.followupBusy}
            onProcessFollowups={radar.processOpportunityFollowups}
            onMarkWatching={radar.markOpportunityWatching}
            onMarkActive={radar.markOpportunityActive}
            onAdjustRiskPlan={radar.adjustOpportunityRiskPlan}
            onPause={radar.pauseOpportunity}
            onResume={radar.resumeOpportunity}
            onReview={radar.reviewOpportunity}
            onArchive={radar.archiveOpportunity}
            onToggleDetail={radar.toggleOpportunityDetail}
            onOpenDetailPage={onOpenOpportunityDetailPage}
            onOpenDetailModal={radar.openOpportunityModal}
          />
          <WaitTriggersPanel
            triggers={triggers}
            onRefresh={refreshTriggers}
            onToggle={toggleTrigger}
            onDelete={deleteTrigger}
            onTest={testTrigger}
            testingTriggerId={testingTriggerId}
            testResults={triggerTestResults}
          />
          <NotificationEventsPanel
            events={radar.events}
            busyEventId={radar.notificationBusyId}
            onSend={radar.sendNotificationEvent}
          />
        </div>
      </div>

      {radar.selectedOpportunityId && (
        <OpportunityDetailModal
          opportunity={radar.selectedOpportunity}
          onClose={radar.closeOpportunityModal}
          onSaveRiskPlan={radar.saveOpportunityRiskPlan}
        />
      )}
    </main>
  );
}
