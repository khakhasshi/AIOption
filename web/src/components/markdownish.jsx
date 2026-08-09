import React from 'react';

function inlineMarkdown(text, keyPrefix) {
  const parts = [];
  const regex = /(\*\*([^*]+)\*\*|\*([^*]+)\*|`([^`]+)`)/g;
  let last = 0, match, idx = 0;
  while ((match = regex.exec(text)) !== null) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    const token = match[0];
    const k = `${keyPrefix}-${idx++}`;
    if (token.startsWith('**'))      parts.push(<strong key={k}>{match[2]}</strong>);
    else if (token.startsWith('*')) parts.push(<em key={k}>{match[3]}</em>);
    else                            parts.push(<code key={k}>{match[4]}</code>);
    last = match.index + token.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

export function Markdownish({ text }) {
  if (!text) return null;
  const lines = text.split('\n');
  const elements = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    // fenced code block
    if (line.startsWith('```')) {
      const codeLines = [];
      i++;
      while (i < lines.length && !lines[i].startsWith('```')) { codeLines.push(lines[i]); i++; }
      elements.push(<pre key={`pre-${i}`}><code>{codeLines.join('\n')}</code></pre>);
      i++; continue;
    }
    // headings
    const h3m = line.match(/^### (.+)/);
    const h2m = line.match(/^## (.+)/);
    const h1m = line.match(/^# (.+)/);
    if (h3m) { elements.push(<h3 key={i}>{inlineMarkdown(h3m[1], i)}</h3>); i++; continue; }
    if (h2m) { elements.push(<h2 key={i}>{inlineMarkdown(h2m[1], i)}</h2>); i++; continue; }
    if (h1m) { elements.push(<h1 key={i}>{inlineMarkdown(h1m[1], i)}</h1>); i++; continue; }
    // pipe table: header row, separator row (|---|---|), then body rows
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?[\s:-]*-[\s:|-]*\|?\s*$/.test(lines[i + 1]) && lines[i + 1].includes('-')) {
      const splitRow = (row) => row.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      const headers = splitRow(line);
      i += 2; // skip header + separator
      const bodyRows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        bodyRows.push(splitRow(lines[i]));
        i++;
      }
      elements.push(
        <div className="md-table-wrap" key={`tbl-${i}`}>
          <table className="md-table">
            <thead>
              <tr>{headers.map((h, hi) => <th key={hi}>{inlineMarkdown(h, `${i}-h-${hi}`)}</th>)}</tr>
            </thead>
            <tbody>
              {bodyRows.map((r, ri) => (
                <tr key={ri}>{r.map((c, ci) => <td key={ci}>{inlineMarkdown(c, `${i}-${ri}-${ci}`)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      );
      continue;
    }
    // bullet list (collect consecutive items)
    if (/^[-*] /.test(line)) {
      const items = [];
      while (i < lines.length && /^[-*] /.test(lines[i])) {
        items.push(<li key={i}>{inlineMarkdown(lines[i].slice(2), i)}</li>);
        i++;
      }
      elements.push(<ul key={`ul-${i}`}>{items}</ul>);
      continue;
    }
    // blank line
    if (line.trim() === '') { i++; continue; }
    // paragraph
    elements.push(<p key={i}>{inlineMarkdown(line, i)}</p>);
    i++;
  }
  return <div className="markdownish">{elements}</div>;
}
