import { strategyModeItems } from '../config.js';

export function parseUniverseInput(input) {
  return normalizeUniverse(String(input || '').split(/[,，;；\s]+/));
}

export function normalizeUniverse(value) {
  const source = Array.isArray(value) ? value : String(value || '').split(/[,，;；\s]+/);
  const output = [];
  source.forEach((item) => {
    const symbol = normalizeSymbol(item);
    if (symbol && !output.includes(symbol)) output.push(symbol);
  });
  return output;
}

export function mergeUniverse(current, additions) {
  return normalizeUniverse([...current, ...additions]);
}

export function normalizeSymbol(item) {
  const symbol = String(item || '')
    .trim()
    .toUpperCase()
    .replace(/^\$/, '')
    .replace(/\.US$/, '');
  if (!/^[A-Z0-9][A-Z0-9.-]{0,11}$/.test(symbol)) return '';
  return symbol;
}

export function normalizeStrategyModes(value) {
  const allowed = strategyModeItems.map(([mode]) => mode);
  const source = Array.isArray(value) ? value : String(value || '').split(/[,，;；\s]+/);
  const output = [];
  source.forEach((item) => {
    const mode = String(item || '').trim();
    if (allowed.includes(mode) && !output.includes(mode)) output.push(mode);
  });
  return output.length ? output : ['single_leg'];
}
