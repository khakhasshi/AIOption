import React from 'react';
import { TradeInstanceDetail } from '../components/trade-instance-detail.jsx';
import { DisclosurePanel } from '../components/common.jsx';
import { t } from '../i18n/index.js';
import { ActiveInstancePanel } from '../components/trading/active-instance-panel.jsx';
import { AiSummaryPanel } from '../components/trading/ai-summary-panel.jsx';
import { TradingConfigPanel } from '../components/trading/config-panel.jsx';
import { ExecutionRecordsPanel } from '../components/trading/execution-records-panel.jsx';
import { InstanceListPanel } from '../components/trading/instance-list-panel.jsx';
import { InstanceDataPanels } from '../components/trading/instance-data-panels.jsx';
import { QualityPanel } from '../components/trading/quality-panel.jsx';
import { ReadinessPanel } from '../components/trading/readiness-panel.jsx';
import { RiskActionsPanel } from '../components/trading/risk-actions-panel.jsx';
import { SnapshotsPanel } from '../components/trading/snapshots-panel.jsx';
import { TradeReviewPanel } from '../components/trading/trade-review-panel.jsx';
import { TradingHero } from '../components/trading/trading-hero.jsx';
import { WorkspaceMetrics } from '../components/trading/workspace-metrics.jsx';
import { useTradingController } from '../hooks/use-trading-controller.js';

export function TradingPage({ api, providers, accounts, analysisPresets = [], fallbackProvider, fallbackAccount, marketClock, clockTick, onBack, session, onLogout, onPresetGuide, detailRunId = '', detailMode = false, routeMode, onRouteModeChange }) {
  const {
    activeRun,
    advisorReports,
    aiQuality,
    allVisibleRunsSelected,
    applyTradingPreset,
    blockedStrategyItems,
    brokerAccounts,
    bulkDeleteInstances,
    canRunNow,
    cancelInstanceOrders,
    config,
    councilMode,
    createBlockers,
    curve,
    deleteCurrentInstance,
    error,
    executionSnapshot,
    filteredRuns,
    flattenAllPositions,
    flattenCurrentInstance,
    flattenResult,
    flattening,
    hasMultiLegStrategy,
    instanceActionResult,
    instanceActionRunning,
    instanceDetailTab,
    instanceListExpanded,
    instanceListFilter,
    instanceRisk,
    instanceStats,
    monitorResult,
    nextRunLabel,
    nextRunSub,
    openInstanceDetailPage,
    orders,
    protection,
    readiness,
    readinessIssues,
    readinessState,
    readinessWarnings,
    refreshAiQuality,
    refreshTradingReadiness,
    refreshTradingRun,
    refreshTradingRuns,
    refreshTradingSnapshots,
    resetCurrentInstanceRisk,
    resetScheduleSlots,
    runMonitorOnce,
    runTradingNow,
    running,
    saveTradingConfig,
    saving,
    scans,
    scheduleSlotsForConfig,
    selectedRunCount,
    selectedRunIds,
    selectedStrategyModes,
    selections,
    setConfig,
    setInstanceDetailTab,
    setInstanceListExpanded,
    setInstanceListFilter,
    setMultiInstanceEnabled,
    setSelectedRunIds,
    setSingleInstanceEnabled,
    snapshots,
    strategyAnalysisOnly,
    strategyExecutionOrders,
    strategyPositions,
    strategySnapshot,
    timeline,
    toggleTradingStrategyMode,
    tradeInstance,
    updateScheduleSlot,
    visibleRunIds,
    visibleRuns,
    visibleTradingPresets,
  } = useTradingController({
    accounts,
    analysisPresets,
    api,
    detailMode,
    detailRunId,
    fallbackProvider,
    onBack,
    providers,
    routeMode,
  });

  return (
    <main className="shell">
      <TradingHero
        clockTick={clockTick}
        config={config}
        detailMode={detailMode}
        marketClock={marketClock}
        onBack={onBack}
        onLogout={onLogout}
        onRouteModeChange={onRouteModeChange}
        readiness={readiness}
        routeMode={routeMode}
        session={session}
      />

      <div className={`trading-layout ${detailMode ? 'detail-mode' : ''}`}>
        {!detailMode && (
          <TradingConfigPanel
            accounts={accounts}
            analysisPresets={analysisPresets}
            applyTradingPreset={applyTradingPreset}
            brokerAccounts={brokerAccounts}
            canRunNow={canRunNow}
            config={config}
            createBlockers={createBlockers}
            hasMultiLegStrategy={hasMultiLegStrategy}
            onPresetGuide={onPresetGuide}
            providers={providers}
            resetScheduleSlots={resetScheduleSlots}
            runTradingNow={runTradingNow}
            running={running}
            saveTradingConfig={saveTradingConfig}
            saving={saving}
            scheduleSlotsForConfig={scheduleSlotsForConfig}
            selectedStrategyModes={selectedStrategyModes}
            setConfig={setConfig}
            setMultiInstanceEnabled={setMultiInstanceEnabled}
            setSingleInstanceEnabled={setSingleInstanceEnabled}
            strategyAnalysisOnly={strategyAnalysisOnly}
            toggleTradingStrategyMode={toggleTradingStrategyMode}
            updateScheduleSlot={updateScheduleSlot}
            visibleTradingPresets={visibleTradingPresets}
          />
        )}

        <section className="workspace">
          {error && <div className="error">{error}</div>}
          <WorkspaceMetrics
            activeRun={activeRun}
            aiQuality={aiQuality}
            config={config}
            hasMultiLegStrategy={hasMultiLegStrategy}
            readinessState={readinessState}
            strategyAnalysisOnly={strategyAnalysisOnly}
          />

          <InstanceListPanel
            activeRun={activeRun}
            allVisibleRunsSelected={allVisibleRunsSelected}
            bulkDeleteInstances={bulkDeleteInstances}
            deleteCurrentInstance={deleteCurrentInstance}
            filteredRuns={filteredRuns}
            instanceActionResult={instanceActionResult}
            instanceActionRunning={instanceActionRunning}
            instanceListExpanded={instanceListExpanded}
            instanceListFilter={instanceListFilter}
            instanceStats={instanceStats}
            openInstanceDetailPage={openInstanceDetailPage}
            refreshTradingRun={refreshTradingRun}
            refreshTradingRuns={refreshTradingRuns}
            resetCurrentInstanceRisk={resetCurrentInstanceRisk}
            selectedRunCount={selectedRunCount}
            selectedRunIds={selectedRunIds}
            setInstanceListExpanded={setInstanceListExpanded}
            setInstanceListFilter={setInstanceListFilter}
            setSelectedRunIds={setSelectedRunIds}
            visibleRunIds={visibleRunIds}
            visibleRuns={visibleRuns}
          />

          {activeRun && (
            <ActiveInstancePanel
              activeRun={activeRun}
              blockedStrategyItems={blockedStrategyItems}
              openInstanceDetailPage={openInstanceDetailPage}
              protection={protection}
              tradeInstance={tradeInstance}
              instanceRisk={instanceRisk}
              timeline={timeline}
            />
          )}

          {activeRun && (
            <TradeInstanceDetail
              instance={tradeInstance}
              activeTab={instanceDetailTab}
              onTabChange={setInstanceDetailTab}
              startCollapsed={!detailMode}
            />
          )}

          <RiskActionsPanel
            activeRun={activeRun}
            cancelInstanceOrders={cancelInstanceOrders}
            config={config}
            flattenAllPositions={flattenAllPositions}
            flattenCurrentInstance={flattenCurrentInstance}
            flattenResult={flattenResult}
            flattening={flattening}
            instanceActionResult={instanceActionResult}
            instanceActionRunning={instanceActionRunning}
          />

          <AiSummaryPanel
            activeRun={activeRun}
            advisorReports={advisorReports}
            councilMode={councilMode}
            refreshTradingRuns={refreshTradingRuns}
            selections={selections}
          />

          <TradeReviewPanel api={api} activeRun={activeRun} />

          <DisclosurePanel
            title={t('trading2.runtimeDiagnostics')}
            summary={`${readinessState} · orders ${orders?.length || 0} · snapshots ${snapshots?.length || 0}`}
            className="trading-diagnostics-group"
            bare
          >
            <ReadinessPanel
              config={config}
              nextRunLabel={nextRunLabel}
              nextRunSub={nextRunSub}
              readiness={readiness}
              readinessIssues={readinessIssues}
              readinessState={readinessState}
              readinessWarnings={readinessWarnings}
              refreshTradingReadiness={refreshTradingReadiness}
            />

            <QualityPanel
              aiQuality={aiQuality}
              refreshAiQuality={refreshAiQuality}
            />

            <SnapshotsPanel
              curve={curve}
              refreshTradingSnapshots={refreshTradingSnapshots}
              snapshots={snapshots}
              strategySnapshot={strategySnapshot}
            />

            <ExecutionRecordsPanel
              monitorResult={monitorResult}
              orders={orders}
              runMonitorOnce={runMonitorOnce}
              strategyPositions={strategyPositions}
            />

            <InstanceDataPanels
              executionSnapshot={executionSnapshot}
              scans={scans}
              strategyExecutionOrders={strategyExecutionOrders}
            />
          </DisclosurePanel>
        </section>
      </div>
    </main>
  );
}
