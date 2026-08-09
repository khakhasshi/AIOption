import React, { useMemo, useState } from 'react';
import { Plus, X } from 'lucide-react';
import { defaultTradingConfig } from '../config.js';
import { mergeUniverse, normalizeUniverse, parseUniverseInput } from '../utils/trading-inputs.js';

export function TradingUniverseEditor({ value, onChange }) {
  const symbols = useMemo(() => normalizeUniverse(value), [value]);
  const [draft, setDraft] = useState('');

  function addSymbols(text) {
    const additions = parseUniverseInput(text);
    if (!additions.length) return;
    onChange(mergeUniverse(symbols, additions));
    setDraft('');
  }

  function removeSymbol(symbol) {
    onChange(symbols.filter((item) => item !== symbol));
  }

  function handleInputChange(event) {
    const next = event.target.value.toUpperCase();
    if (/[,\s;，；]/.test(next)) {
      addSymbols(next);
      return;
    }
    setDraft(next);
  }

  function handleKeyDown(event) {
    if (['Enter', 'Tab', ',', '，', ';', '；', ' '].includes(event.key)) {
      if (draft.trim()) {
        event.preventDefault();
        addSymbols(draft);
      }
    }
    if (event.key === 'Backspace' && !draft && symbols.length) {
      event.preventDefault();
      removeSymbol(symbols[symbols.length - 1]);
    }
  }

  function handlePaste(event) {
    const pasted = event.clipboardData.getData('text');
    if (!pasted) return;
    event.preventDefault();
    addSymbols(pasted);
  }

  return (
    <div className="universe-editor">
      <div className="universe-head">
        <strong>{symbols.length} symbols</strong>
        <div>
          <button type="button" className="ghost compact" onClick={() => onChange(defaultTradingConfig.universe)}>{window._t('universe.defaultPool')}</button>
          <button type="button" className="ghost compact" onClick={() => onChange([])} disabled={!symbols.length}>{window._t('universe.clear')}</button>
        </div>
      </div>
      <div className="symbol-tags">
        {symbols.map((symbol) => (
          <button type="button" className="symbol-tag" key={symbol} onClick={() => removeSymbol(symbol)} title={`${window._t('universe.remove')}${symbol}`}>
            <span>{symbol}</span>
            <X size={12} strokeWidth={2.4} />
          </button>
        ))}
        <div className="symbol-entry">
          <input
            value={draft}
            onChange={handleInputChange}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            placeholder="AAPL, NVDA, SPY"
            autoCapitalize="characters"
            spellCheck="false"
          />
          <button type="button" onClick={() => addSymbols(draft)} disabled={!draft.trim()} title={window._t('universe.addToPool')}>
            <Plus size={14} strokeWidth={2.4} />
          </button>
        </div>
      </div>
    </div>
  );
}
