import React, { useState } from 'react';
import { stageLabel } from '../utils/display.js';

export function SectionTitle({ title }) {
  return <h2 className="section-title">{title}</h2>;
}

export function Toggle({ checked, onChange, label, disabled = false }) {
  return (
    <button type="button" className={`toggle ${checked ? 'on' : ''}`} disabled={disabled} onClick={() => onChange(!checked)}>
      <span>{label}</span><i />
    </button>
  );
}

export function Metric({ label, value, sub, tone = '' }) {
  return (
    <div className={`metric ${tone ? `metric-${tone}` : ''}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {sub && <small>{sub}</small>}
    </div>
  );
}

export function CollapsiblePanel({ title, open, onToggle, children }) {
  return (
    <div className="auth-panel collapsible-panel">
      <button className="collapsible-head" type="button" onClick={onToggle}>
        <span>{title}</span>
        <strong>{open ? window._t('commonui.collapse') : window._t('commonui.expand')}</strong>
      </button>
      {open && <div className="collapsible-body">{children}</div>}
    </div>
  );
}

export function DisclosurePanel({ title, summary, defaultOpen = false, className = '', children, actions, bare = false }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className={`${bare ? '' : 'panel'} disclosure-panel ${open ? 'open' : 'closed'} ${className}`}>
      <div className="disclosure-head">
        <button type="button" className="disclosure-toggle" onClick={() => setOpen((value) => !value)} aria-expanded={open}>
          <span className="disclosure-caret">{open ? '▾' : '▸'}</span>
          <span>
            <strong>{title}</strong>
            {summary && <small>{summary}</small>}
          </span>
        </button>
        {actions && <div className="disclosure-actions">{actions}</div>}
      </div>
      {open && <div className="disclosure-body">{children}</div>}
    </section>
  );
}

export function ProgressBar({ progress = 0, stage }) {
  const safeProgress = Math.max(0, Math.min(Number(progress) || 0, 100));
  return (
    <div className="progress-wrap">
      <div>
        <span>{stageLabel(stage)}</span>
        <strong>{safeProgress}%</strong>
      </div>
      <i><b style={{ width: `${safeProgress}%` }} /></i>
    </div>
  );
}

export function Pair({ k, v }) {
  return (
    <>
      <dt>{k}</dt>
      <dd>{v}</dd>
    </>
  );
}
