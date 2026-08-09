import React, { useEffect, useMemo, useState } from 'react';
import { Bell, BadgeCheck, BookOpen, Pencil, RefreshCw, RotateCcw, Send, Trash2 } from 'lucide-react';
import { MarketClock } from '../components/market-clock.jsx';
import { SessionAndRouteBar } from '../components/session-route-bar.jsx';
import { SectionTitle, Toggle } from '../components/common.jsx';
import { formatTime } from '../utils/display.js';
import { PageHeader } from '../components/app-shell/page-header.jsx';
import { useAppShell } from '../components/app-shell/app-shell-context.js';
import { t } from '../i18n/index.js';

const emptyChannelForm = {
  type: 'email',
  label: '',
  email: '',
  provider: 'generic',
  url: '',
  secret: '',
  header_name: 'X-AI-Option-Signature',
  bot_token: '',
  chat_id: '',
  phone_number_id: '',
  access_token: '',
  to: '',
  template_name: '',
  template_language: 'en_US',
  template_variables: '',
  enabled: true,
};

const webhookProviders = [
  ['generic', 'Webhook'],
  ['slack', 'Slack'],
  ['discord', 'Discord'],
  ['telegram', 'Telegram'],
  ['whatsapp', 'WhatsApp'],
  ['feishu', '飞书'],
];

const channelKinds = [
  ['email', 'Email'],
  ['slack', 'Slack'],
  ['telegram', 'Telegram'],
  ['whatsapp', 'WhatsApp'],
  ['discord', 'Discord'],
  ['feishu', '飞书'],
  ['generic', 'Webhook'],
];

function channelKindFromForm(form) {
  return form.type === 'email' ? 'email' : form.provider || 'generic';
}

function formPatchForChannelKind(kind) {
  if (kind === 'email') return { type: 'email', provider: 'generic' };
  return { type: 'webhook', provider: kind || 'generic' };
}

function webhookProviderLabel(provider) {
  return webhookProviders.find(([value]) => value === provider)?.[1] || 'Webhook';
}

function providerLabel(channel) {
  if (channel?.type === 'email') return 'Email';
  const provider = channel?.config?.provider || 'generic';
  return webhookProviderLabel(provider);
}

function formFromChannel(channel) {
  const config = channel?.config || {};
  return {
    ...emptyChannelForm,
    type: channel?.type || 'email',
    label: channel?.label || '',
    email: config.email || '',
    provider: config.provider || 'generic',
    url: config.url || '',
    secret: '',
    header_name: config.header_name || 'X-AI-Option-Signature',
    bot_token: '',
    chat_id: config.chat_id || '',
    phone_number_id: config.phone_number_id || '',
    access_token: '',
    to: config.to || '',
    template_name: config.template_name || '',
    template_language: config.template_language || 'en_US',
    template_variables: Array.isArray(config.template_variables) ? config.template_variables.join('\n') : '',
    enabled: Boolean(channel?.enabled),
  };
}

function channelPayload(form) {
  if (form.type === 'email') {
    return {
      type: 'email',
      label: form.label.trim() || form.email.trim(),
      email: form.email.trim(),
      enabled: Boolean(form.enabled),
    };
  }
  return {
    type: 'webhook',
    label: form.label.trim() || webhookProviderLabel(form.provider),
    provider: form.provider,
    url: form.url.trim(),
    secret: form.secret.trim(),
    header_name: form.header_name.trim() || 'X-AI-Option-Signature',
    bot_token: form.bot_token.trim(),
    chat_id: form.chat_id.trim(),
    phone_number_id: form.phone_number_id.trim(),
    access_token: form.access_token.trim(),
    to: form.to.trim(),
    template_name: form.template_name.trim(),
    template_language: form.template_language.trim() || 'en_US',
    template_variables: form.template_variables.split('\n').map((item) => item.trim()).filter(Boolean),
    enabled: Boolean(form.enabled),
  };
}

function statusTone(status) {
  if (status === 'sent') return 'ok';
  if (status === 'failed') return 'danger';
  return 'warning';
}

export function NotificationCenterPage({
  api,
  session,
  routeMode,
  onRouteModeChange,
  onLogout,
  onBack,
  onGuide,
  marketClock,
  clockTick,
}) {
  const [channels, setChannels] = useState([]);
  const [events, setEvents] = useState([]);
  const [logs, setLogs] = useState([]);
  const [selectedChannelId, setSelectedChannelId] = useState('');
  const [editingChannelId, setEditingChannelId] = useState('');
  const [form, setForm] = useState(emptyChannelForm);
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [testingChannelId, setTestingChannelId] = useState('');
  const [busyEventId, setBusyEventId] = useState('');
  const [error, setError] = useState('');
  const [processResult, setProcessResult] = useState(null);

  const selectedChannel = useMemo(
    () => channels.find((channel) => channel.id === selectedChannelId) || channels[0] || null,
    [channels, selectedChannelId],
  );

  useEffect(() => {
    refreshAll();
  }, []);

  useEffect(() => {
    if (!selectedChannel?.id) {
      setPreview(null);
      setLogs([]);
      return;
    }
    refreshChannelDetail(selectedChannel.id);
  }, [selectedChannel?.id]);

  async function refreshAll(nextSelectedId = selectedChannelId) {
    setError('');
    const [channelRows, eventRows, logRows] = await Promise.all([
      api('/api/notification-channels'),
      api('/api/notification-events?limit=30'),
      api('/api/notification-delivery-logs?limit=60'),
    ]);
    setChannels(channelRows);
    setEvents(eventRows);
    setLogs(logRows);
    const nextChannel = channelRows.find((channel) => channel.id === nextSelectedId) || channelRows[0] || null;
    setSelectedChannelId(nextChannel?.id || '');
  }

  async function refreshChannelDetail(channelId) {
    if (!channelId) return;
    try {
      const [previewRow, logRows] = await Promise.all([
        api(`/api/notification-channels/${encodeURIComponent(channelId)}/payload-preview`),
        api(`/api/notification-channels/${encodeURIComponent(channelId)}/delivery-logs?limit=40`),
      ]);
      setPreview(previewRow);
      setLogs(logRows);
    } catch (detailError) {
      setPreview(null);
      setError(detailError.message);
    }
  }

  async function saveChannel(event) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      const endpoint = editingChannelId ? `/api/notification-channels/${encodeURIComponent(editingChannelId)}` : '/api/notification-channels';
      const row = await api(endpoint, {
        method: editingChannelId ? 'PATCH' : 'POST',
        body: JSON.stringify(channelPayload(form)),
      });
      setEditingChannelId('');
      setForm({ ...emptyChannelForm, type: form.type });
      await refreshAll(row.id);
    } catch (formError) {
      setError(formError.message);
    } finally {
      setBusy(false);
    }
  }

  function editChannel(channel) {
    setEditingChannelId(channel.id);
    setSelectedChannelId(channel.id);
    setForm(formFromChannel(channel));
  }

  function cancelEdit() {
    setEditingChannelId('');
    setForm(emptyChannelForm);
  }

  async function toggleChannel(channel) {
    setError('');
    try {
      const row = await api(`/api/notification-channels/${encodeURIComponent(channel.id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled: !channel.enabled }),
      });
      setChannels((current) => current.map((item) => (item.id === row.id ? row : item)));
      await refreshAll(row.id);
    } catch (toggleError) {
      setError(toggleError.message);
    }
  }

  async function deleteChannel(channel) {
    if (!window.confirm(t('notify.confirmDelete').replace('{label}', channel.label))) return;
    setBusy(true);
    setError('');
    try {
      await api(`/api/notification-channels/${encodeURIComponent(channel.id)}`, { method: 'DELETE' });
      if (editingChannelId === channel.id) cancelEdit();
      await refreshAll('');
    } catch (deleteError) {
      setError(deleteError.message);
    } finally {
      setBusy(false);
    }
  }

  async function testChannel(channelId) {
    setTestingChannelId(channelId);
    setError('');
    try {
      const result = await api(`/api/notification-channels/${encodeURIComponent(channelId)}/test`, { method: 'POST' });
      await refreshAll(channelId);
      if (result.event?.status !== 'sent') {
        setError(result.event?.last_error || t('notify.testFailed'));
      }
    } catch (testError) {
      setError(testError.message);
    } finally {
      setTestingChannelId('');
    }
  }

  async function sendEvent(eventId) {
    setBusyEventId(eventId);
    setError('');
    try {
      const result = await api(`/api/notification-events/${encodeURIComponent(eventId)}/send`, { method: 'POST' });
      await refreshAll(selectedChannelId);
      if (result.status !== 'sent') {
        setError(result.last_error || t('notify.sendFailed'));
      }
    } catch (sendError) {
      setError(sendError.message);
    } finally {
      setBusyEventId('');
    }
  }

  async function retryPending() {
    setBusy(true);
    setError('');
    try {
      const result = await api('/api/notification-events/process', { method: 'POST' });
      setProcessResult(result);
      await refreshAll(selectedChannelId);
    } catch (retryError) {
      setError(retryError.message);
    } finally {
      setBusy(false);
    }
  }

  const telegramKeepsToken = Boolean(editingChannelId && selectedChannel?.config?.bot_token_configured);
  const whatsappKeepsToken = Boolean(editingChannelId && selectedChannel?.config?.access_token_configured);
  const canSubmit = form.type === 'email'
    ? form.email.trim().includes('@')
    : (form.provider === 'telegram'
      ? (form.bot_token.trim() || telegramKeepsToken) && form.chat_id.trim()
      : form.provider === 'whatsapp'
        ? form.phone_number_id.trim() && (form.access_token.trim() || whatsappKeepsToken) && form.to.trim()
        : form.url.trim().startsWith('http'));

  return (
    <main className="shell notification-shell">
      <NotifPageHeader onGuide={onGuide} />
      <section className="hero app-hero">
        <div className="hero-brand">
          <div className="radar-logo"><Bell size={28} /></div>
          <div>
            <div className="eyebrow">Notification Center</div>
            <h1>{t('notify.title')}</h1>
            <p>{t('notify.subtitle')}</p>
          </div>
        </div>
        <div className="hero-controls">
          <MarketClock clock={marketClock} tick={clockTick} />
          <SessionAndRouteBar session={session} routeMode={routeMode} onRouteModeChange={onRouteModeChange} onLogout={onLogout} />
          <div className="hero-actions">
            <button className="ghost nav-action" type="button" onClick={onGuide}><BookOpen size={14} /> {t('notify.setupGuide')}</button>
            <button className="ghost nav-action" type="button" onClick={onBack}>{t('notify.backToRadar')}</button>
          </div>
        </div>
      </section>

      {error && <div className="error-banner">{error}</div>}

      <div className="notification-layout">
        <section className="panel notification-editor-panel">
          <SectionTitle title={editingChannelId ? t('notify.editChannel') : t('notify.addChannel')} />
          <form className="form" onSubmit={saveChannel}>
            <div className="two">
              <label>
                {t('notify.type')}
                <select
                  value={channelKindFromForm(form)}
                  onChange={(event) => setForm({ ...emptyChannelForm, ...formPatchForChannelKind(event.target.value) })}
                >
                  {channelKinds.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </label>
              <label>
                {t('notify.name')}
                <input value={form.label} onChange={(event) => setForm((current) => ({ ...current, label: event.target.value }))} placeholder={t('notify.namePlaceholder')} />
              </label>
            </div>
            {form.type === 'email' ? (
              <label>
                {t('notify.recipientEmail')}
                <input value={form.email} onChange={(event) => setForm((current) => ({ ...current, email: event.target.value }))} placeholder="name@example.com" />
              </label>
            ) : (
              <>
                <div className="channel-kind-note">
                  <strong>{webhookProviderLabel(form.provider)}</strong>
                  <span>{form.provider === 'generic' ? t('notify.genericPayloadNote') : t('notify.platformPayloadNote')}</span>
                </div>
                {form.provider === 'telegram' ? (
                  <div className="two">
                    <label>
                      Bot Token
                      <input value={form.bot_token} onChange={(event) => setForm((current) => ({ ...current, bot_token: event.target.value }))} placeholder={editingChannelId ? t('notify.keepIfBlank') : '123:abc'} />
                    </label>
                    <label>
                      Chat ID
                      <input value={form.chat_id} onChange={(event) => setForm((current) => ({ ...current, chat_id: event.target.value }))} placeholder="-100..." />
                    </label>
                  </div>
                ) : form.provider === 'whatsapp' ? (
                  <>
                    <div className="two">
                      <label>
                        Phone Number ID
                        <input value={form.phone_number_id} onChange={(event) => setForm((current) => ({ ...current, phone_number_id: event.target.value }))} placeholder="Meta phone number id" />
                      </label>
                      <label>
                        {t('notify.recipientNumber')}
                        <input value={form.to} onChange={(event) => setForm((current) => ({ ...current, to: event.target.value }))} placeholder={t('notify.recipientNumberPlaceholder')} />
                      </label>
                    </div>
                    <label>
                      Access Token
                      <input value={form.access_token} onChange={(event) => setForm((current) => ({ ...current, access_token: event.target.value }))} placeholder={editingChannelId ? t('notify.keepIfBlank') : 'WhatsApp Cloud API token'} />
                    </label>
                    <div className="two">
                      <label>
                        Template Name
                        <input value={form.template_name} onChange={(event) => setForm((current) => ({ ...current, template_name: event.target.value }))} placeholder={t('notify.templateNamePlaceholder')} />
                      </label>
                      <label>
                        Template Language
                        <input value={form.template_language} onChange={(event) => setForm((current) => ({ ...current, template_language: event.target.value }))} placeholder="en_US / zh_CN" />
                      </label>
                    </div>
                    <label>
                      Template Variables
                      <textarea rows={3} value={form.template_variables} onChange={(event) => setForm((current) => ({ ...current, template_variables: event.target.value }))} placeholder={t('notify.templateVarsPlaceholder')} />
                    </label>
                  </>
                ) : (
                  <label>
                    Webhook URL
                    <input value={form.url} onChange={(event) => setForm((current) => ({ ...current, url: event.target.value }))} placeholder="https://example.com/hook" />
                  </label>
                )}
                <div className="two">
                  <label>
                    {t('notify.signatureSecret')}
                    <input value={form.secret} onChange={(event) => setForm((current) => ({ ...current, secret: event.target.value }))} placeholder={editingChannelId ? t('notify.keepIfBlank') : t('notify.optional')} />
                  </label>
                  <label>
                    {t('notify.signatureHeader')}
                    <input value={form.header_name} onChange={(event) => setForm((current) => ({ ...current, header_name: event.target.value }))} />
                  </label>
                </div>
              </>
            )}
            <Toggle checked={form.enabled} onChange={(value) => setForm((current) => ({ ...current, enabled: value }))} label={t('notify.enableChannel')} />
            <button className="primary" type="submit" disabled={busy || !canSubmit}>
              <BadgeCheck size={16} /> {editingChannelId ? t('notify.saveChannel') : t('notify.addChannel')}
            </button>
            {editingChannelId && (
              <button className="ghost" type="button" disabled={busy} onClick={cancelEdit}>{t('notify.cancelEdit')}</button>
            )}
          </form>
        </section>

        <section className="panel">
          <div className="notification-section-head">
            <SectionTitle title={t('notify.channelList')} />
            <button className="ghost compact" type="button" disabled={busy} onClick={() => refreshAll(selectedChannelId)}>
              <RefreshCw size={14} /> {t('notify.refresh')}
            </button>
          </div>
          <div className="notification-channel-grid">
            {channels.map((channel) => (
              <article key={channel.id} className={`notification-channel-card ${selectedChannel?.id === channel.id ? 'active' : ''} ${channel.enabled ? '' : 'disabled'}`}>
                <button className="notification-channel-main" type="button" onClick={() => setSelectedChannelId(channel.id)}>
                  <span>{providerLabel(channel)} · {channel.enabled ? 'enabled' : 'disabled'}</span>
                  <strong>{channel.label}</strong>
                  <small>{channel.verified_at ? `${t('notify.verifiedAt')} ${formatTime(channel.verified_at)}` : t('notify.unverified')}{channel.last_error ? ` · ${channel.last_error}` : ''}</small>
                </button>
                <div className="notification-channel-actions">
                  <button className="ghost icon" type="button" title={channel.enabled ? t('notify.disable') : t('notify.enable')} onClick={() => toggleChannel(channel)}>
                    <Bell size={14} />
                  </button>
                  <button className="ghost icon" type="button" title={t('notify.edit')} onClick={() => editChannel(channel)}>
                    <Pencil size={14} />
                  </button>
                  <button className="ghost icon" type="button" title={t('notify.test')} disabled={testingChannelId === channel.id || !channel.enabled} onClick={() => testChannel(channel.id)}>
                    <Send size={14} />
                  </button>
                  <button className="ghost icon" type="button" title={t('notify.delete')} disabled={busy} onClick={() => deleteChannel(channel)}>
                    <Trash2 size={14} />
                  </button>
                </div>
              </article>
            ))}
            {!channels.length && <p className="muted">{t('notify.noChannels')}</p>}
          </div>
        </section>

        <section className="panel notification-preview-panel">
          <SectionTitle title={t('notify.payloadPreview')} />
          {preview ? (
            <pre className="json-preview">{JSON.stringify(preview, null, 2)}</pre>
          ) : (
            <p className="muted">{t('notify.previewHint')}</p>
          )}
        </section>

        <section className="panel notification-log-panel">
          <div className="notification-section-head">
            <SectionTitle title={t('notify.deliveryLogs')} />
            <button className="ghost compact" type="button" disabled={busy} onClick={retryPending}>
              <RotateCcw size={14} /> {t('notify.retryQueue')}
            </button>
          </div>
          {processResult && (
            <div className="notification-result-line">
              {t('notify.queued')} {processResult.queued || 0} · {t('notify.sent')} {processResult.sent || 0} · {t('notify.failed')} {processResult.failed || 0} · {t('notify.skipped')} {processResult.skipped || 0}
            </div>
          )}
          <div className="notification-log-list">
            {logs.map((log) => (
              <article key={log.id} className={`notification-log-row ${statusTone(log.status)}`}>
                <div>
                  <strong>{log.status} · {log.provider || log.channel_type}</strong>
                  <small>{formatTime(log.created_at)} · attempt {log.attempt} · event {String(log.event_id || '').slice(0, 8)}</small>
                </div>
                <span>{log.error || log.response_summary || 'accepted'}</span>
              </article>
            ))}
            {!logs.length && <p className="muted">{t('notify.noLogs')}</p>}
          </div>
        </section>

        <section className="panel notification-events-wide">
          <SectionTitle title={t('notify.notificationEvents')} />
          <div className="notification-event-list">
            {events.map((event) => (
              <article key={event.id} className={`notification-event-row ${statusTone(event.status)}`}>
                <div>
                  <strong>{event.title}</strong>
                  <small>{event.status} · {event.attempts || 0} {t('notify.times')} · {formatTime(event.sent_at || event.created_at)}</small>
                  {event.last_error && <span>{event.last_error}</span>}
                </div>
                {event.status !== 'sent' && (
                  <button className="ghost compact" type="button" disabled={busyEventId === event.id} onClick={() => sendEvent(event.id)}>
                    <Send size={14} /> {event.status === 'failed' ? t('notify.retry') : t('notify.send')}
                  </button>
                )}
              </article>
            ))}
            {!events.length && <p className="muted">{t('notify.noEvents')}</p>}
          </div>
        </section>
      </div>
    </main>
  );
}

function NotifPageHeader({ onGuide }) {
  const { embedded } = useAppShell();
  if (!embedded) return null;
  return (
    <PageHeader
      eyebrow="Notification Center"
      title={t('notify.title')}
      subtitle={t('notify.headerSubtitle')}
      actions={
        <button className="ghost" type="button" onClick={onGuide}>
          <BookOpen size={14} /> {t('notify.setupGuide')}
        </button>
      }
    />
  );
}
