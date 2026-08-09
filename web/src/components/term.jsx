import React from 'react';
import { t } from '../i18n/index.js';

// Trader / options jargon dictionary. Wrap any occurrence in <Term name="ORB">ORB</Term>
// and the user gets a native browser tooltip (title attribute) plus a dotted underline
// so they know hovering reveals the definition. Definitions live in the i18n
// dictionaries under scanner2.term.* and are resolved at render time so they
// follow the active locale.
const TERM_KEYS = {
  ORB: 'orb',
  ORH: 'orh',
  ORL: 'orl',
  VWAP: 'vwap',
  RVOL: 'rvol',
  POC: 'poc',
  VAH: 'vah',
  VAL: 'val',
  GEX: 'gex',
  'Call Wall': 'callWall',
  'Put Wall': 'putWall',
  'Gamma Flip': 'gammaFlip',
  IV: 'iv',
  HIRO: 'hiro',
  Delta: 'delta',
  Theta: 'theta',
  Vega: 'vega',
  Gamma: 'gamma',
  DTE: 'dte',
  TP: 'tp',
  TP1: 'tp1',
  TP2: 'tp2',
  Stop: 'stop',
  OI: 'oi',
};

export function Term({ name, children, definition }) {
  const key = TERM_KEYS[name] ?? TERM_KEYS[String(children || '').trim()];
  const text = definition ?? (key ? t(`scanner2.term.${key}`) : null);
  if (!text) return <>{children ?? name}</>;
  return (
    <abbr className="term-abbr" title={text}>
      {children ?? name}
    </abbr>
  );
}

export const termDictionary = TERM_KEYS;
