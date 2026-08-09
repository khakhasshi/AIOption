import React, { useMemo, useState } from 'react';
import { SectionTitle } from './common.jsx';
import { t } from '../i18n/index.js';
import { councilModeLabel, entryOrderTypeLabel, environmentLabel, eventTypeLabel, fmt, lifecycleLabel, protectionStateLabel, shortId, strategyModesLabel, triggerSourceLabel } from '../utils/display.js';

export function VirtualList({ items = [], itemHeight = 120, maxHeight = 480, className = '', empty = null, renderItem }) {
  const [scrollTop, setScrollTop] = useState(0);
  const safeItems = Array.isArray(items) ? items : [];
  const totalHeight = safeItems.length * itemHeight;
  const viewportHeight = Math.min(maxHeight, Math.max(itemHeight, totalHeight));
  const visibleCount = Math.max(1, Math.ceil(viewportHeight / itemHeight) + 2);
  const startIndex = Math.max(0, Math.floor(scrollTop / itemHeight) - 1);
  const endIndex = Math.min(safeItems.length, startIndex + visibleCount);
  const visibleItems = safeItems.slice(startIndex, endIndex);
  const topSpacer = startIndex * itemHeight;
  const bottomSpacer = Math.max(0, totalHeight - topSpacer - visibleItems.length * itemHeight);

  if (!safeItems.length) {
    return <div className={className}>{empty}</div>;
  }

  return (
    <div
      className={`virtual-list ${className}`.trim()}
      style={{ maxHeight: `${maxHeight}px`, height: `${viewportHeight}px` }}
      onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
    >
      <div className="virtual-list-inner" style={{ height: `${totalHeight}px` }}>
        <div style={{ height: `${topSpacer}px` }} />
        {visibleItems.map((item, index) => (
          <div
            key={item?.id || item?.instance_id || item?.contract_symbol || item?.symbol || `${startIndex + index}`}
            className="virtual-list-item"
            style={{ height: `${itemHeight}px` }}
          >
            {renderItem(item, startIndex + index)}
          </div>
        ))}
        <div style={{ height: `${bottomSpacer}px` }} />
      </div>
    </div>
  );
}

export function LazyJsonPanel({ title, data }) {
  const [open, setOpen] = useState(false);
  const [pretty, setPretty] = useState(true);
  const jsonText = useMemo(() => {
    if (!open) return '';
    try {
      return JSON.stringify(data, null, pretty ? 2 : 0);
    } catch {
      return String(data ?? '');
    }
  }, [data, open, pretty]);

  return (
    <div className="panel json-panel">
      <div className="answer-head">
        <SectionTitle title={title} />
        <div className="action-row">
          <button className="ghost compact" type="button" onClick={() => setPretty((value) => !value)} disabled={!open}>
            {pretty ? t('scanner2.trace.compact') : t('scanner2.trace.pretty')}
          </button>
          <button className="ghost" type="button" onClick={() => setOpen((value) => !value)}>
            {open ? t('scanner2.trace.collapseRaw') : t('scanner2.trace.expandRaw')}
          </button>
        </div>
      </div>
      {!open ? (
        <p className="muted">{t('scanner2.trace.rawHint')}</p>
      ) : (
        <pre className="json-pre"><code>{jsonText}</code></pre>
      )}
    </div>
  );
}

export function AnalysisTracePanel({ trace, fallbackTitle = t('scanner2.trace.fallbackTitle') }) {
  const safeTrace = trace && typeof trace === 'object' ? trace : null;
  const stages = Array.isArray(safeTrace?.stages) ? safeTrace.stages : [];
  if (!stages.length) {
    return (
      <div className="panel trace-panel">
        <SectionTitle title={fallbackTitle} />
        <p className="muted">{t('scanner2.trace.empty')}</p>
      </div>
    );
  }
  return (
    <div className="panel trace-panel">
      <div className="answer-head">
        <SectionTitle title={fallbackTitle} />
        <span className="muted">{safeTrace.kind || 'trace'} · v{safeTrace.version || 1}</span>
      </div>
      {safeTrace.summary && <p className="trace-summary">{safeTrace.summary}</p>}
      <div className="trace-timeline">
        {stages.map((stage, index) => (
          <article className={`trace-stage ${traceStatusTone(stage.status)}`} key={stage.key || `${stage.title}-${index}`}>
            <div className="trace-stage-index">{String(index + 1).padStart(2, '0')}</div>
            <div className="trace-stage-body">
              <div className="trace-stage-head">
                <div>
                  <strong>{stage.title || stage.key || t('scanner2.trace.stageN').replace('{n}', index + 1)}</strong>
                  <span>{stage.summary || '--'}</span>
                </div>
                <em>{traceStatusLabel(stage.status)}</em>
              </div>
              <div className="trace-items">
                {(stage.items || []).map((item, itemIndex) => (
                  <div className="trace-item" key={`${item.label || item.key || itemIndex}`}>
                    <span>{item.label || item.key || t('scanner2.trace.fieldN').replace('{n}', itemIndex + 1)}</span>
                    <strong>{traceValue(item.value)}</strong>
                  </div>
                ))}
              </div>
              {!!(stage.evidence || []).length && (
                <div className="trace-evidence">
                  {(stage.evidence || []).map((item, evidenceIndex) => (
                    <div className="trace-evidence-row" key={`${item.field || evidenceIndex}`}>
                      <strong>{item.field || '--'}</strong>
                      <span>{traceValue(item.value)}</span>
                      <small>{item.supports || '--'}</small>
                    </div>
                  ))}
                </div>
              )}
              {!!(stage.notes || []).length && (
                <div className="mini-list trace-notes">
                  {(stage.notes || []).map((note, noteIndex) => <span key={`${note}-${noteIndex}`}>{note}</span>)}
                </div>
              )}
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}

export function buildTradeInstanceTrace(instance = {}) {
  if (!instance || typeof instance !== 'object') return null;
  const basic = instance.basic_info || {};
  const intent = instance.strategy_intent || {};
  const snapshot = instance.candidate_snapshot || {};
  const decision = instance.ai_decision || {};
  const risk = instance.risk_plan || {};
  const execution = instance.execution_plan || {};
  const protection = instance.protection_status || {};
  const events = Array.isArray(instance.event_timeline) ? instance.event_timeline : [];
  const advisors = Array.isArray(decision.advisor_reports) ? decision.advisor_reports : [];
  return {
    version: 1,
    kind: 'trade_instance',
    generated_at: instance.updated_at,
    summary: `${t('scanner2.trace.instancePrefix').replace('{id}', shortId(instance.instance_id))} · ${lifecycleLabel(instance.lifecycle_state)}`,
    stages: [
      {
        key: 'instance_intent',
        title: t('scanner2.trace.instanceIntent'),
        status: 'passed',
        summary: `${triggerSourceLabel(basic.trigger_source)} · ${strategyModesLabel(basic.strategy_modes || intent.strategy_modes || [])}`,
        items: [
          { label: t('scanner2.trace.account'), value: basic.account_name || '--' },
          { label: t('scanner2.trace.environment'), value: environmentLabel(basic.paper_or_live) },
          { label: t('scanner2.trace.universe'), value: (basic.universe || []).length },
          { label: 'Top N', value: basic.top_n ?? '--' },
          { label: t('scanner2.trace.capitalCap'), value: `$${fmt(basic.total_capital)}` },
          { label: t('scanner2.trace.entryMethod'), value: entryOrderTypeLabel(basic.entry_order_type) },
        ],
        notes: compactTraceNotes([intent.prompt_template], 2),
      },
      {
        key: 'scan_snapshot',
        title: t('scanner2.trace.candidateGen'),
        status: snapshot.contract_candidates || snapshot.strategy_candidates ? 'passed' : 'warning',
        summary: t('scanner2.trace.snapshotSummary')
          .replace('{symbols}', snapshot.symbols_scanned || 0)
          .replace('{single}', snapshot.contract_candidates || 0)
          .replace('{struct}', snapshot.strategy_candidates || 0),
        items: [
          { label: t('scanner2.trace.scannedSymbols'), value: snapshot.symbols_scanned || 0 },
          { label: t('scanner2.trace.withCandidates'), value: snapshot.symbols_with_candidates || 0 },
          { label: t('scanner2.trace.singleLegCandidates'), value: snapshot.contract_candidates || 0 },
          { label: t('scanner2.trace.strategyCandidates'), value: snapshot.strategy_candidates || 0 },
          { label: 'Top K', value: snapshot.top_k_per_symbol || 0 },
        ],
      },
      {
        key: 'advisor_decision',
        title: t('scanner2.trace.aiDecision'),
        status: decision.selection_count ? 'passed' : 'warning',
        summary: decision.summary || t('scanner2.trace.aiDecisionWaiting'),
        items: [
          { label: t('scanner2.trace.mode'), value: councilModeLabel(decision.council_mode) },
          { label: t('scanner2.trace.selected'), value: decision.selection_count || 0 },
          { label: t('scanner2.trace.rejected'), value: decision.rejected_count || 0 },
          { label: t('scanner2.trace.advisors'), value: advisors.length },
          { label: t('scanner2.trace.validation'), value: decision.post_validation?.status || '--' },
        ],
        notes: compactTraceNotes([...(decision.risk_notes || []), decision.post_validation?.reason], 8),
      },
      {
        key: 'risk_execution',
        title: t('scanner2.trace.riskExecution'),
        status: risk.planned_contracts || risk.strategy_tracking_count ? 'passed' : 'warning',
        summary: t('scanner2.trace.riskSummary')
          .replace('{positions}', risk.planned_contracts || risk.strategy_tracking_count || 0)
          .replace('{protection}', protectionStateLabel(protection.state)),
        items: [
          { label: t('scanner2.trace.plannedRisk'), value: `$${fmt(risk.planned_premium_at_risk)}` },
          { label: t('scanner2.trace.maxLoss'), value: `$${fmt(risk.max_loss_if_all_premiums_lost)}` },
          { label: t('scanner2.trace.protectionState'), value: protectionStateLabel(protection.state) },
          { label: t('scanner2.trace.unprotectedQty'), value: protection.unprotected_quantity || 0 },
          { label: t('scanner2.trace.softwareStop'), value: execution.software_stop_enabled ? t('scanner2.on') : t('scanner2.off') },
          { label: t('scanner2.trace.softwareTakeProfit'), value: execution.software_take_profit_enabled ? t('scanner2.on') : t('scanner2.off') },
        ],
        notes: compactTraceNotes([protection.stop_failure_reason], 4),
      },
      {
        key: 'event_timeline',
        title: t('scanner2.trace.lifecycleEvents'),
        status: events.length ? 'passed' : 'skipped',
        summary: t('scanner2.trace.eventsSummary')
          .replace('{count}', events.length)
          .replace('{state}', lifecycleLabel(instance.lifecycle_state)),
        items: [
          { label: t('scanner2.trace.currentState'), value: lifecycleLabel(instance.lifecycle_state) },
          { label: t('scanner2.trace.eventCount'), value: events.length },
          { label: t('scanner2.trace.lastEvent'), value: eventTypeLabel(events[events.length - 1]?.event_type) },
          { label: t('scanner2.trace.updatedAt'), value: instance.updated_at || '--' },
        ],
        notes: compactTraceNotes(events.slice(-6).map((event) => event.message), 6),
      },
    ],
  };
}

function compactTraceNotes(notes = [], limit = 6) {
  return (Array.isArray(notes) ? notes : []).filter((item) => item !== undefined && item !== null && item !== '').map(String).slice(0, limit);
}

function traceValue(value) {
  if (value === null || value === undefined || value === '') return '--';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : fmt(value);
  if (typeof value === 'boolean') return value ? t('scanner2.yes') : t('scanner2.no');
  if (Array.isArray(value)) return value.length ? value.join(' / ') : '--';
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function traceStatusLabel(status) {
  const labels = {
    passed: t('scanner2.trace.statusPassed'),
    warning: t('scanner2.trace.statusWarning'),
    blocked: t('scanner2.trace.statusBlocked'),
    skipped: t('scanner2.trace.statusSkipped'),
    failed: t('scanner2.trace.statusFailed'),
  };
  return labels[String(status || '').toLowerCase()] || String(status || '--');
}

function traceStatusTone(status) {
  const value = String(status || '').toLowerCase();
  if (value === 'passed') return 'ok';
  if (value === 'blocked' || value === 'failed') return 'danger';
  if (value === 'warning') return 'warning';
  return 'muted';
}
