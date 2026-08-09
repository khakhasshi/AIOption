import React from 'react';

// Lightweight skeleton placeholder. Use to give weight to loading regions instead of a bare spinner.
// Example:
//   <Skeleton variant="row" count={4} />
//   <Skeleton variant="card" />
//   <Skeleton variant="text" width="40%" />
export function Skeleton({ variant = 'text', count = 1, width, height, className = '' }) {
  const items = Array.from({ length: Math.max(1, count) });
  const baseClass = `skeleton skeleton-${variant} ${className}`.trim();
  const style = {};
  if (width) style.width = typeof width === 'number' ? `${width}px` : width;
  if (height) style.height = typeof height === 'number' ? `${height}px` : height;
  return (
    <div className="skeleton-stack" role="status" aria-busy="true" aria-live="polite">
      {items.map((_, idx) => (
        <span key={idx} className={baseClass} style={style} />
      ))}
      <span className="sr-only">加载中…</span>
    </div>
  );
}
