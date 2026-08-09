import React from 'react';

/**
 * Unified page header that replaces per-page .hero blocks.
 *
 * Usage:
 *   <PageHeader
 *     eyebrow="Scan Console"
 *     title="实时扫描"
 *     subtitle="..."
 *     meta={<MarketClock ... />}
 *     actions={<><button className="ghost">导出</button><button className="primary">运行</button></>}
 *   />
 */
export function PageHeader({ eyebrow, title, subtitle, meta, actions }) {
  return (
    <header className="page-header">
      <div className="page-header-text">
        {eyebrow ? <span className="page-header-eyebrow">{eyebrow}</span> : null}
        {title ? <h1 className="page-header-title">{title}</h1> : null}
        {subtitle ? <p className="page-header-subtitle">{subtitle}</p> : null}
      </div>
      {meta ? <div className="page-header-meta">{meta}</div> : null}
      {actions ? <div className="page-header-actions">{actions}</div> : null}
    </header>
  );
}
