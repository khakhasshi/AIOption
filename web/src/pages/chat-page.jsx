import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, Loader2, Plus, Send, Square, Trash2, Wrench, X } from 'lucide-react';
import { Markdownish } from '../components/markdownish.jsx';


function normalizeSession(raw) {
  return {
    id: raw.id,
    title: raw.title || window._t('chat.newSession'),
    provider: raw.provider || '',
    account: raw.account || '',
    tools: raw.tools == null ? null : raw.tools,
    messageCount: raw.message_count || 0,
    createdAt: raw.created_at || '',
    updatedAt: raw.updated_at || '',
  };
}

function TraceBlock({ traces, done }) {
  const [open, setOpen] = useState(!done);
  useEffect(() => {
    if (done) setOpen(false);
  }, [done]);
  if (!traces.length) return null;
  const failed = traces.filter((t) => ['error', 'partial', 'empty'].includes(t.status)).length;
  return (
    <div className="chat-trace-block">
      <button className="chat-trace-toggle" onClick={() => setOpen(o => !o)}>
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <span>{window._t('chat.toolCalls')} ({traces.length})</span>
        {!open && done && <span className={`chat-trace-status ${failed ? 'warning' : 'ok'}`}>{failed ? `${failed} issue` : 'done'}</span>}
        {!done && <Loader2 className="spin" size={10} />}
      </button>
      {open && (
        <div className="chat-trace-list">
          {traces.map((t, i) => (
            <div key={i} className={`chat-trace-item ${t.status || ''}`}>
              <span className="chat-trace-tool">{t.tool || t.kind}</span>
              <span className="chat-trace-summary">
                {t.status === 'fetching' && window._t('chat.requesting')}
                {t.status === 'done' && (t.result?.price ? `${t.result.price}` : t.result?.note || '✓')}
                {t.status === 'partial' && `⚠️ ${t.note || t.result?.quote_error_reason || window._t('chat.partialData')}`}
                {t.status === 'empty' && window._t('chat.noData')}
                {t.status === 'error' && `${t.error || window._t('chat.failed')}`}
                {t.kind === 'symbols_detected' && `${window._t('chat.detected')}${(t.data?.symbols || []).join(', ')}`}
                {t.kind === 'note' && t.data?.text}
                {t.kind === 'ai_start' && `${window._t('chat.calling')}${t.data?.provider || 'AI'}`}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function DataTables({ tables }) {
  const [open, setOpen] = useState(false);
  if (!tables || !tables.length) return null;
  return (
    <div className="chat-data-tables">
      <button className="chat-trace-toggle" onClick={() => setOpen((o) => !o)}>
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <span>{window._t('chat.rawData')} ({tables.length})</span>
      </button>
      {open && (
        <div className="chat-data-tables-list">
          {tables.map((tbl, ti) => (
            <div className="chat-data-table" key={ti}>
              <div className="chat-data-table-title">{tbl.title}</div>
              <div className="md-table-wrap">
                <table className="md-table">
                  <thead>
                    <tr>{(tbl.columns || []).map((c, ci) => <th key={ci}>{c}</th>)}</tr>
                  </thead>
                  <tbody>
                    {(tbl.rows || []).map((row, ri) => (
                      <tr key={ri}>{row.map((cell, ci) => <td key={ci}>{String(cell)}</td>)}</tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function ChatPage({ api, clientUserId, session, providers = [], accounts = [] }) {

  const [sessions, setSessions] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [messagesById, setMessagesById] = useState({}); // sessionId -> messages[]
  const [loadingSessions, setLoadingSessions] = useState(true);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [streamingText, setStreamingText] = useState('');
  const [streamingTraces, setStreamingTraces] = useState([]);
  const [streamingDone, setStreamingDone] = useState(false);
  const [streamingTables, setStreamingTables] = useState([]);
  const [streamingInstanceId, setStreamingInstanceId] = useState('');
  const [toolCatalog, setToolCatalog] = useState([]);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const listRef = useRef(null);
  const abortRef = useRef(null);
  const inputRef = useRef(null);

  const activeSession = useMemo(() => sessions.find((s) => s.id === activeId) || sessions[0] || null, [sessions, activeId]);
  const messages = useMemo(() => (activeSession ? messagesById[activeSession.id] || [] : []), [activeSession, messagesById]);

  const sdkAccounts = useMemo(() => accounts.filter((a) => a.sdk_credentials_configured), [accounts]);

  // Load the user's own sessions from the server (scoped per authenticated user).
  useEffect(() => {
    let alive = true;
    (async () => {
      setLoadingSessions(true);
      try {
        const data = await api('/api/chat/sessions');
        if (!alive) return;
        const rows = Array.isArray(data?.sessions) ? data.sessions.map(normalizeSession) : [];
        if (rows.length === 0) {
          const created = await api('/api/chat/sessions', { method: 'POST', body: JSON.stringify({}) });
          if (!alive) return;
          const s = normalizeSession(created);
          setSessions([s]);
          setActiveId(s.id);
          setMessagesById({ [s.id]: [] });
        } else {
          setSessions(rows);
          setActiveId((prev) => (prev && rows.some((r) => r.id === prev) ? prev : rows[0].id));
        }
      } catch {
        if (alive) setSessions([]);
      } finally {
        if (alive) setLoadingSessions(false);
      }
    })();
    return () => { alive = false; };
  }, [api]);

  // Lazy-load messages for the active session the first time it is opened.
  useEffect(() => {
    if (!activeSession) return;
    if (messagesById[activeSession.id] !== undefined) return;
    let alive = true;
    (async () => {
      setLoadingMessages(true);
      try {
        const data = await api(`/api/chat/sessions/${encodeURIComponent(activeSession.id)}/messages`);
        if (!alive) return;
        const rows = Array.isArray(data?.messages) ? data.messages : [];
        setMessagesById((prev) => ({ ...prev, [activeSession.id]: rows }));
      } catch {
        if (alive) setMessagesById((prev) => ({ ...prev, [activeSession.id]: [] }));
      } finally {
        if (alive) setLoadingMessages(false);
      }
    })();
    return () => { alive = false; };
  }, [activeSession, messagesById, api]);

  // Load available tool chain once.
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const rows = await api('/api/chat/tools');
        if (alive && Array.isArray(rows)) setToolCatalog(rows);
      } catch {}
    })();
    return () => { alive = false; };
  }, [api]);

  // Auto-scroll to bottom.
  useEffect(() => {
    if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [messages, streamingText, streamingTraces, activeId]);

  const allToolIds = useMemo(() => toolCatalog.map((t) => t.id), [toolCatalog]);
  const enabledToolIds = useMemo(() => {
    if (!activeSession) return allToolIds;
    return activeSession.tools == null ? allToolIds : activeSession.tools;
  }, [activeSession, allToolIds]);

  const patchActive = useCallback((patch) => {
    const sid = activeSession?.id;
    if (!sid) return;
    setSessions((prev) => prev.map((s) => (s.id === sid ? { ...s, ...patch } : s)));
    const body = {};
    if ('provider' in patch) body.provider = patch.provider;
    if ('account' in patch) body.account = patch.account;
    if ('tools' in patch) body.tools = patch.tools;
    if ('title' in patch) body.title = patch.title;
    if (Object.keys(body).length === 0) return;
    api(`/api/chat/sessions/${encodeURIComponent(sid)}`, { method: 'PATCH', body: JSON.stringify(body) }).catch(() => {});
  }, [activeSession?.id, api]);

  const newSession = useCallback(async () => {
    const defaults = {
      account: activeSession?.account || (sdkAccounts.find((a) => a.is_default) || sdkAccounts[0])?.name || '',
      provider: activeSession?.provider || '',
      tools: activeSession?.tools ?? null,
    };
    try {
      const created = await api('/api/chat/sessions', { method: 'POST', body: JSON.stringify(defaults) });
      const s = normalizeSession(created);
      setSessions((prev) => [s, ...prev]);
      setMessagesById((prev) => ({ ...prev, [s.id]: [] }));
      setActiveId(s.id);
      setSidebarOpen(false);
    } catch {}
  }, [activeSession, sdkAccounts, api]);

  const deleteSession = useCallback(async (id) => {
    try { await api(`/api/chat/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }); } catch {}
    setMessagesById((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
    setSessions((prev) => {
      const next = prev.filter((s) => s.id !== id);
      if (next.length === 0) {
        api('/api/chat/sessions', { method: 'POST', body: JSON.stringify({}) })
          .then((created) => {
            const s = normalizeSession(created);
            setSessions([s]);
            setMessagesById({ [s.id]: [] });
            setActiveId(s.id);
          })
          .catch(() => {});
        return prev.filter((s) => s.id !== id);
      }
      if (id === activeId) setActiveId(next[0].id);
      return next;
    });
  }, [api, activeId]);

  const toggleTool = useCallback((toolId) => {
    const current = enabledToolIds;
    const next = current.includes(toolId) ? current.filter((t) => t !== toolId) : [...current, toolId];
    patchActive({ tools: next });
  }, [enabledToolIds, patchActive]);

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || sending || !activeSession) return;
    const sessionId = activeSession.id;
    const userMsg = { role: 'user', text };
    const priorMessages = messagesById[sessionId] || [];
    const isFirst = priorMessages.length === 0;

    setMessagesById((prev) => ({ ...prev, [sessionId]: [...(prev[sessionId] || []), userMsg] }));
    if (isFirst) {
      const title = text.replace(/\s+/g, ' ').slice(0, 22) + (text.length > 22 ? '…' : '');
      setSessions((prev) => prev.map((s) => (s.id === sessionId ? { ...s, title } : s)));
    }
    setInput('');
    setSending(true);
    setStreamingText('');
    setStreamingTraces([]);
    setStreamingDone(false);
    setStreamingTables([]);

    // Generate the turn instance id on the client so it can be shown the moment
    // the user hits send — even if the connection drops before the first SSE
    // event arrives. The backend persists this same id.
    let instanceId = '';
    try {
      instanceId = (crypto?.randomUUID?.() || '').replace(/-/g, '').slice(0, 12);
    } catch {
      instanceId = '';
    }
    if (!instanceId) {
      instanceId = (Date.now().toString(36) + Math.random().toString(36).slice(2)).slice(0, 12);
    }
    setStreamingInstanceId(instanceId);

    const body = {
      message: text,
      session_id: sessionId,
      ai_provider: activeSession.provider || undefined,
      longbridge_account: activeSession.account || undefined,
      tools: activeSession.tools == null ? undefined : activeSession.tools,
      history: priorMessages.slice(-12).map((m) => ({ role: m.role, text: m.text })),
      instance_id: instanceId,
      locale: (window._getLocale && window._getLocale()) || 'zh',
    };

    // Accumulators live outside the try so that a stream/network error which
    // happens *after* the model already produced output does not discard the
    // answer. Some proxies close the SSE connection abruptly once the response
    // finishes, which surfaces as a "network error" on reader.read(); in that
    // case we still want to keep whatever the assistant already sent.
    let fullText = '';
    const allTraces = [];
    let dataTables = [];
    let gotDone = false;
    let committed = false;

    const controller = new AbortController();
    abortRef.current = controller;

    const commitAssistant = (extra) => {
      if (committed) return;
      committed = true;
      const finalText = (fullText || window._t('chat.noReply')) + (extra || '');
      const assistantMsg = { role: 'assistant', text: finalText, traces: allTraces, tables: dataTables, instanceId };
      setMessagesById((prev) => ({ ...prev, [sessionId]: [...(prev[sessionId] || []), assistantMsg] }));
      setSessions((prev) => {
        const idx = prev.findIndex((s) => s.id === sessionId);
        if (idx < 0) return prev;
        const moved = prev[idx];
        return [moved, ...prev.slice(0, idx), ...prev.slice(idx + 1)];
      });
    };

    try {
      const resp = await fetch('/api/chat', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-AI-Option-User': clientUserId || '' },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || `${window._t('chat.requestFailedStatus')}${resp.status}`);
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop() || '';
        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed.startsWith('data: ')) continue;
          try {
            const obj = JSON.parse(trimmed.slice(6));
            if (obj.type === 'trace') {
              if (obj.kind === 'turn') {
                instanceId = obj.data?.instance_id || '';
                setStreamingInstanceId(instanceId);
                continue;
              }
              const trace = { ...obj, status: obj.data?.status || obj.kind };
              if (obj.kind === 'tool_call') {
                trace.tool = obj.data?.tool || '';
                trace.status = obj.data?.status || '';
                trace.result = obj.data?.result;
                trace.error = obj.data?.error;
                trace.note = obj.data?.note;
              }
              if (obj.kind === 'data_tables') {
                dataTables = obj.data?.tables || [];
                setStreamingTables(dataTables);
              } else {
                allTraces.push(trace);
                setStreamingTraces([...allTraces]);
              }
            } else if (obj.type === 'token') {
              fullText += obj.text;
              setStreamingText(fullText);
            } else if (obj.type === 'done') {
              gotDone = true;
              setStreamingDone(true);
            }
          } catch {}
        }
      }

      commitAssistant();
    } catch (err) {
      // User manually stopped the run: keep whatever was already streamed and
      // mark it as interrupted instead of showing a network-error bubble.
      if (err?.name === 'AbortError') {
        commitAssistant(gotDone ? '' : window._t('chat.manualStop'));
      } else if (gotDone || fullText.trim()) {
        // If the model already produced an answer (or signalled done) before the
        // connection dropped, keep the answer instead of replacing it with an
        // error bubble. Only show a hard error when we truly got nothing.
        commitAssistant(gotDone ? '' : window._t('chat.earlyStop'));
      } else {
        const errMsg = { role: 'assistant', text: `${window._t('chat.requestFailedColon')}${err.message || window._t('chat.networkInterrupted')}`, error: true, instanceId };
        setMessagesById((prev) => ({ ...prev, [sessionId]: [...(prev[sessionId] || []), errMsg] }));
      }
    } finally {
      abortRef.current = null;
      setSending(false);
      setStreamingText('');
      setStreamingTraces([]);
      setStreamingDone(false);
      setStreamingTables([]);
      setStreamingInstanceId('');
    }
  }, [input, sending, activeSession, messagesById, clientUserId]);

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const onKeyDown = useCallback((e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }, [send]);

  const clearActive = useCallback(async () => {
    const sid = activeSession?.id;
    if (!sid) return;
    try { await api(`/api/chat/sessions/${encodeURIComponent(sid)}/messages`, { method: 'DELETE' }); } catch {}
    setMessagesById((prev) => ({ ...prev, [sid]: [] }));
    setSessions((prev) => prev.map((s) => (s.id === sid ? { ...s, title: window._t('chat.newSession') } : s)));
  }, [activeSession?.id, api]);

  return (
    <div className="chat-layout">
      <div className="chat-shell">
        <div className="chat-toolbar">
          <button className="ghost compact chat-mobile-sessions" onClick={() => setSidebarOpen((o) => !o)} title={window._t('chat.sessionsSettings')}>
            <Wrench size={13} /> {window._t('chat.sessionsSettings')}
          </button>
          <span className="chat-active-title">{activeSession?.title || window._t('chat.newSession')}</span>
          <div className="chat-toolbar-spacer" />
          {messages.length > 0 && (
            <button className="ghost compact" onClick={clearActive} disabled={sending}>{window._t('chat.clearSession')}</button>
          )}
          <button className="ghost compact" onClick={newSession} disabled={sending}>
            <Plus size={13} /> {window._t('chat.newSession')}
          </button>
        </div>

        <div className="chat-messages" ref={listRef}>
          {messages.length === 0 && !sending && !loadingMessages && (
            <div className="chat-empty">
              <div className="chat-empty-icon">💬</div>
              <h2>{window._t('chat.emptyTitle')}</h2>
              <p>{window._t('chat.emptyDesc')}</p>
              <div className="chat-prompts">
                <button onClick={() => setInput(window._t('chat.examPrompt1'))}>{window._t('chat.prompt1')}</button>
                <button onClick={() => setInput(window._t('chat.examPrompt2'))}>{window._t('chat.prompt2')}</button>
                <button onClick={() => setInput(window._t('chat.examPrompt3'))}>{window._t('chat.prompt3')}</button>
                <button onClick={() => setInput(window._t('chat.examPrompt4'))}>{window._t('chat.prompt4')}</button>
              </div>
            </div>
          )}
          {messages.length === 0 && loadingMessages && (
            <div className="chat-empty"><Loader2 className="spin" size={20} /></div>
          )}
          {messages.map((msg, i) => (
            <div key={i} className={`chat-bubble ${msg.role}`}>
              <div className="chat-bubble-label">
                <span>{msg.role === 'user' ? window._t('chat.you') : 'AI'}</span>
                {msg.role === 'assistant' && (msg.instanceId || msg.instance_id) && (
                  <span className="chat-instance-id" title={window._t('chat.instanceId')}>#{msg.instanceId || msg.instance_id}</span>
                )}
              </div>
              {msg.role === 'assistant' && msg.traces?.length > 0 && (
                <TraceBlock traces={msg.traces} done={true} />
              )}
              {msg.role === 'assistant' && msg.tables?.length > 0 && (
                <DataTables tables={msg.tables} />
              )}
              <div className="chat-bubble-body">
                {msg.role === 'assistant' ? <Markdownish text={msg.text} /> : <p>{msg.text}</p>}
              </div>
            </div>
          ))}
          {sending && (
            <div className="chat-bubble assistant streaming">
              <div className="chat-bubble-label">
                <span>AI</span>
                {streamingInstanceId && (
                  <span className="chat-instance-id" title={window._t('chat.instanceId')}>#{streamingInstanceId}</span>
                )}
              </div>
              <TraceBlock traces={streamingTraces} done={streamingDone} />
              {streamingTables.length > 0 && <DataTables tables={streamingTables} />}
              <div className="chat-bubble-body">
                {streamingText ? (
                  <Markdownish text={streamingText} />
                ) : (
                  <span className="chat-streaming-hint">
                    <Loader2 className="spin" size={14} />
                    {streamingTraces.some((t) => t.kind === 'ai_start')
                      ? window._t('chat.generating')
                      : window._t('chat.callingTools')}
                  </span>
                )}
                {streamingDone && !streamingText && <span>{window._t('chat.noReply')}</span>}
              </div>
              {!streamingDone && streamingText && <span className="chat-cursor-blink">|</span>}
            </div>
          )}
        </div>

        <div className="chat-input-bar">
          <textarea
            ref={inputRef}
            className="chat-input"
            placeholder={window._t('chat.inputPlaceholder')}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            rows={1}
            disabled={sending}
          />
          <div className="chat-input-actions">
            {sending ? (
              <button className="ghost compact chat-stop-btn" onClick={stop} title={window._t('chat.stopAnswer')}>
                <Square size={13} />
                <span>{window._t('chat.stop')}</span>
              </button>
            ) : (
              <button className="primary compact chat-send-btn" onClick={send} disabled={!input.trim()}>
                <Send size={14} />
              </button>
            )}
          </div>
        </div>
      </div>

      <aside className={`chat-sidebar ${sidebarOpen ? 'open' : ''}`}>
        <div className="chat-sidebar-mobile-head">
          <span>{window._t('chat.sessionsSettings')}</span>
          <button className="ghost compact" onClick={() => setSidebarOpen(false)}><X size={14} /></button>
        </div>

        <section className="chat-side-section">
          <div className="chat-side-head">
            <span>{window._t('chat.sessionList')}</span>
            <button className="ghost compact" onClick={newSession} disabled={sending} title={window._t('chat.newSessionTitle')}><Plus size={13} /></button>
          </div>
          <div className="chat-session-list">
            {loadingSessions && sessions.length === 0 && (
              <div className="chat-side-hint"><Loader2 className="spin" size={14} /> {window._t('chat.loadingSessions')}</div>
            )}
            {sessions.map((s) => (
              <div key={s.id} className={`chat-session-item ${s.id === activeSession?.id ? 'active' : ''}`}>
                <button className="chat-session-pick" onClick={() => { setActiveId(s.id); setSidebarOpen(false); }}>
                  <span className="chat-session-title">{s.title || window._t('chat.newSession')}</span>
                  <span className="chat-session-meta">{(messagesById[s.id]?.length ?? s.messageCount) || 0}{window._t('chat.msgCount')}{s.updatedAt ? ` · ${new Date(s.updatedAt).toLocaleDateString()}` : ''}</span>
                </button>
                <button
                  className="chat-session-del"
                  title={window._t('chat.deleteSession')}
                  onClick={() => deleteSession(s.id)}
                  disabled={sending && s.id === activeSession?.id}
                >
                  <Trash2 size={13} />
                </button>
              </div>
            ))}
          </div>
        </section>

        <details className="chat-side-section chat-settings-section">
          <summary>
            <span>{window._t('chat.sessionsSettings')}</span>
            <small>{enabledToolIds.length}/{toolCatalog.length || '--'} tools</small>
          </summary>

          <div className="chat-side-field">
            <div className="chat-side-head"><span>{window._t('chat.lbAccount')}</span></div>
            <select
              className="chat-side-select"
              value={activeSession?.account || ''}
              onChange={(e) => patchActive({ account: e.target.value })}
              disabled={sending}
            >
              <option value="">{window._t('chat.defaultAccount')}</option>
              {sdkAccounts.map((a) => (
                <option key={a.name} value={a.name}>{a.label || a.name}{a.is_default ? window._t('chat.suffixDefault') : ''}</option>
              ))}
            </select>
            {sdkAccounts.length === 0 && <p className="chat-side-hint">{window._t('chat.noLbAccount')}</p>}
          </div>

          <div className="chat-side-field">
            <div className="chat-side-head"><span>{window._t('chat.aiModel')}</span></div>
            <select
              className="chat-side-select"
              value={activeSession?.provider || ''}
              onChange={(e) => patchActive({ provider: e.target.value })}
              disabled={sending}
            >
              <option value="">{window._t('chat.defaultProvider')}</option>
              {providers.map((p) => (
                <option key={p.name} value={p.name} disabled={p.configured === false}>
                  {p.label || p.name}{p.configured === false ? window._t('chat.suffixUnconfigured') : ''}
                </option>
              ))}
            </select>
          </div>

          <div className="chat-tool-list">
            {toolCatalog.map((t) => {
              const on = enabledToolIds.includes(t.id);
              const blocked = t.requires_account && sdkAccounts.length === 0;
              return (
                <label key={t.id} className={`chat-tool-item ${blocked ? 'blocked' : ''}`}>
                  <input
                    type="checkbox"
                    checked={on}
                    disabled={sending || blocked}
                    onChange={() => toggleTool(t.id)}
                  />
                  <span className="chat-tool-text">
                    <span className="chat-tool-name">{t.label}</span>
                    <span className="chat-tool-desc">{t.description}{blocked ? window._t('chat.suffixNeedAccount') : ''}</span>
                  </span>
                </label>
              );
            })}
            {toolCatalog.length === 0 && <p className="chat-side-hint">{window._t('chat.loadingTools')}</p>}
          </div>
        </details>
      </aside>

      {sidebarOpen && <div className="chat-sidebar-backdrop" onClick={() => setSidebarOpen(false)} />}
    </div>
  );
}
