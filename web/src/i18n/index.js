// i18n single source of truth.
// The JS dictionaries (zh-CN.js / en-US.js) are the ONLY locale data. Importing
// this module (done once at the top of main.jsx) registers the window._t API so
// the small set of components that call window._t keep working unchanged, while
// new code can `import { t } from '../i18n/index.js'`.
import zh from './zh-CN.js';
import en from './en-US.js';

const LOCALES = { zh, en };
const KEY = 'ai_option_locale';

function detectLocale() {
  try {
    const stored = localStorage.getItem(KEY);
    if (stored && LOCALES[stored]) return stored;
  } catch { /* localStorage may be blocked */ }
  if (typeof navigator !== 'undefined' && (navigator.language || '').toLowerCase().startsWith('en')) return 'en';
  return 'zh';
}

let _locale = detectLocale();

export function getLocale() { return _locale; }

export function setLocale(lang) {
  _locale = LOCALES[lang] ? lang : 'zh';
  try { localStorage.setItem(KEY, _locale); } catch { /* noop */ }
  return _locale;
}

function lookup(dict, keys) {
  let value = dict;
  for (const k of keys) {
    if (value == null) return null;
    value = value[k];
  }
  return value;
}

export function t(path) {
  const keys = String(path).split('.');
  let value = lookup(LOCALES[_locale] || zh, keys);
  // Fall back to Chinese when a key is missing from the active (non-zh) locale.
  if (value == null && _locale !== 'zh') value = lookup(zh, keys);
  return typeof value === 'string' ? value : path;
}

// Register the legacy global API used by components that call window._t. The
// inline stub in index.html provides the same names for the brief window before
// this bundle evaluates; we overwrite it here with the real implementation.
if (typeof window !== 'undefined') {
  window._t = t;
  window._getLocale = getLocale;
  window._setLocale = setLocale;
}
