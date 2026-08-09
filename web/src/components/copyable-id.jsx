import React, { useState } from 'react';
import { Copy } from 'lucide-react';
import { shortLocator } from '../utils/display.js';

export function CopyableId({ value, label = window._t('copyable.instance'), compact = false }) {
  const [copied, setCopied] = useState(false);
  const text = String(value || '').trim();
  if (!text) return null;
  async function copy(event) {
    event.stopPropagation();
    event.preventDefault();
    await copyToClipboard(text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  }
  return (
    <button
      className={`copy-id ${compact ? 'compact' : ''}`}
      type="button"
      title={`${window._t('copyable.copyLabel')}${label}: ${text}`}
      onClick={copy}
    >
      <Copy size={compact ? 12 : 14} />
      <span>{copied ? window._t('copyable.copied') : `${label} ${shortLocator(text)}`}</span>
    </button>
  );
}

async function copyToClipboard(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const input = document.createElement('textarea');
  input.value = text;
  input.setAttribute('readonly', 'true');
  input.style.position = 'fixed';
  input.style.opacity = '0';
  document.body.appendChild(input);
  input.select();
  document.execCommand('copy');
  document.body.removeChild(input);
}
