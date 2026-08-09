import React from 'react';
import { getLocale, setLocale } from './index.js';

// Tiny toggle widget — a pill button switching between 中 / EN. The toggle
// reloads the page so every t() / window._t() call re-evaluates against the new
// dictionary (locale is fixed per page load, so no reactive re-render needed).
export function LocaleToggle({ className = '' }) {
  const isZh = !String(getLocale()).startsWith('en');
  const flip = () => {
    setLocale(isZh ? 'en' : 'zh');
    window.location.reload();
  };
  return (
    <button
      type="button"
      className={`locale-toggle ${className}`}
      onClick={flip}
      title={isZh ? 'Switch to English' : '切换为中文'}
    >
      {isZh ? 'EN' : '中'}
    </button>
  );
}
